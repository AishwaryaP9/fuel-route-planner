import json
import math
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse, QueryDict
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .planner import PlanError, plan_trip
from .services import places
from .services.geocode import LocationError
from .services.routing import RoutingError

# name: (min, max, whole number?)
LIMITS: dict[str, tuple[float, float, bool]] = {
    "mpg": (0.5, 200.0, False),
    # Below 50 mi a vehicle would need an estimated stop every few miles: megabyte answers and seconds of work.
    "max_range_miles": (50.0, 2000.0, False),
    "starting_fuel_gallons": (0.0, 1000.0, False),
    "alternatives": (1, 3, True),
    "city_radius_miles": (0.1, 50.0, False),
}
TRUE, FALSE = {"true", "1", "yes", "on"}, {"false", "0", "no", "off"}
# Optional fields that change the plan; map links repeat them so the map page shows the same plan as the JSON.
PLAN_FIELDS = (
    "mpg",
    "max_range_miles",
    "starting_fuel_gallons",
    "alternatives",
    "city_scope",
    "city_radius_miles",
    "include_cities",
    "include_detour_fuel",
    "allow_estimated_stops",
)
TANK_TOLERANCE_GALLONS = 1e-9
RETRY_AFTER_SECONDS = "5"

Params = QueryDict | dict[str, Any]


def _params(request: HttpRequest) -> Params:
    if request.method == "POST" and request.content_type == "application/json":
        try:
            data = json.loads(request.body or b"{}")
        except ValueError:
            raise PlanError("invalid JSON body", 400) from None
        if not isinstance(data, dict):
            raise PlanError('JSON body must be an object, e.g. {"start": "Dallas, TX", "finish": "Chicago, IL"}', 400)
        return data
    return request.POST if request.method == "POST" else request.GET


def _num(data: Params, key: str) -> float | int | None:
    raw = data.get(key)
    if raw in (None, ""):
        return None
    low, high, whole = LIMITS[key]
    try:
        if isinstance(raw, bool):
            raise ValueError
        value = float(raw)
    except (TypeError, ValueError):
        raise PlanError(f"'{key}' must be a number", 400) from None
    if not math.isfinite(value) or not low <= value <= high:
        raise PlanError(f"'{key}' must be a finite number between {low:g} and {high:g}", 400)
    if whole:
        if value != int(value):
            raise PlanError(f"'{key}' must be a whole number", 400)
        return int(value)
    return value


def _bool(data: Params, key: str, default: bool) -> bool:
    raw = data.get(key)
    if raw in (None, ""):
        return default
    text = str(raw).strip().lower()
    if text in TRUE:
        return True
    if text in FALSE:
        return False
    raise PlanError(f"'{key}' must be true or false", 400)


def _scope(data: Params) -> str | None:
    scope = data.get("city_scope")
    if scope in (None, ""):
        return None
    if scope not in ("major", "all"):
        raise PlanError("'city_scope' must be 'major' or 'all'", 400)
    return str(scope)


def _run(request: HttpRequest) -> tuple[Params, dict[str, Any]]:
    data = _params(request)
    start, finish = data.get("start"), data.get("finish")
    if not isinstance(start, str) or not isinstance(finish, str) or not start.strip() or not finish.strip():
        raise PlanError("'start' and 'finish' are required text values (e.g. 'Chicago, IL' or '41.88,-87.63')", 400)
    mpg, max_range = _num(data, "mpg"), _num(data, "max_range_miles")
    start_gallons = _num(data, "starting_fuel_gallons")
    if start_gallons is not None:
        tank = (max_range or settings.VEHICLE_MAX_RANGE_MILES) / (mpg or settings.VEHICLE_MPG)
        if start_gallons > tank + TANK_TOLERANCE_GALLONS:
            raise PlanError(
                f"'starting_fuel_gallons' cannot exceed the tank capacity ({tank:g} gal = max_range_miles / mpg)", 400
            )
    alternatives = _num(data, "alternatives")
    try:
        return data, plan_trip(
            start,
            finish,
            mpg=mpg,
            max_range=max_range,
            starting_fuel_gallons=start_gallons,
            alternatives=int(alternatives) if alternatives is not None else None,
            city_radius=_num(data, "city_radius_miles"),
            city_scope=_scope(data),
            include_cities=_bool(data, "include_cities", True),
            include_detour_fuel=_bool(data, "include_detour_fuel", True),
            allow_estimated_stops=_bool(data, "allow_estimated_stops", True),
        )
    except LocationError as exc:
        raise PlanError(str(exc), 400) from None
    except RoutingError as exc:
        raise PlanError(str(exc), exc.status) from None


def _plan_query(data: Params, result: dict[str, Any]) -> dict[str, Any]:
    """Return the query parameters that reproduce this plan."""
    query: dict[str, Any] = {"start": result["start"]["query"], "finish": result["finish"]["query"]}
    for key in PLAN_FIELDS:
        value = data.get(key)
        if value not in (None, ""):
            query[key] = str(value).lower() if isinstance(value, bool) else value
    return query


def _error(exc: PlanError) -> JsonResponse:
    response = JsonResponse({"error": str(exc)}, status=exc.status)
    if exc.status == 503:
        response["Retry-After"] = RETRY_AFTER_SECONDS
    return response


def _form_defaults() -> dict[str, str | bool]:
    """Return the defaults shown in the search card, the same ones the planner uses."""
    return {
        "max_range_miles": f"{settings.VEHICLE_MAX_RANGE_MILES:g}",
        "mpg": f"{settings.VEHICLE_MPG:g}",
        "starting_fuel_gallons": "",  # empty means the planner's default
        "alternatives": str(settings.ROUTE_ALTERNATIVES),
        "city_scope": settings.CITY_SCOPE,
        "city_radius_miles": f"{settings.CITY_RADIUS_MILES[settings.CITY_SCOPE]:g}",
        "include_cities": True,
        "include_detour_fuel": True,
        "allow_estimated_stops": True,
    }


def _form_values(data: Params) -> dict[str, Any]:
    """Return the values shown in the search card; invalid values are shown as typed."""
    defaults = _form_defaults()
    form: dict[str, Any] = {"start": str(data.get("start") or ""), "finish": str(data.get("finish") or "")}
    for key, default in defaults.items():
        raw = data.get(key)
        if isinstance(default, bool):
            try:
                form[key] = _bool(data, key, default)
            except PlanError:
                form[key] = default
        elif raw in (None, ""):
            form[key] = default
        else:
            form[key] = str(raw).lower() if isinstance(raw, bool) else str(raw)
    form["options_open"] = any(form[key] != value for key, value in defaults.items())
    form["defaults"] = defaults
    return form


def _selected_route_id(request: HttpRequest, result: dict[str, Any], route_ids: set[int]) -> int:
    recommended: int = result["recommended_route_id"]
    try:
        selected = int(request.GET.get("route") or recommended)
    except ValueError:
        return recommended
    return selected if selected in route_ids else recommended


@require_http_methods(["GET", "HEAD", "POST"])
def route_view(request: HttpRequest) -> JsonResponse:
    """Return the proposed routes with fuel stops, total cost, cities and GeoJSON."""
    try:
        data, result = _run(request)
        pretty = _bool(request.GET, "pretty", False)
    except PlanError as exc:
        return _error(exc)
    base = _plan_query(data, result)
    result["map_html_url"] = request.build_absolute_uri("/api/route/map/?" + urlencode(base))
    for route in result["routes"]:
        query = urlencode({**base, "route": route["route_id"]})
        route["map_html_url"] = request.build_absolute_uri("/api/route/map/?" + query)
    return JsonResponse(
        result,
        json_dumps_params={
            "indent": 2 if pretty else None,
            "allow_nan": False,
            "separators": None if pretty else (",", ":"),
        },
    )


@require_http_methods(["GET", "HEAD", "POST"])
def map_view(request: HttpRequest) -> HttpResponse:
    """Render the search page, with the plan when a start and finish are given.

    Planning errors are shown inside the search card with the same HTTP status as the JSON API.
    """
    try:
        data = _params(request)
    except PlanError:
        data = {}
    context: dict[str, Any] = {
        "form": _form_values(data),
        "result": None,
        "error": None,
        "suggestions": places.page_list(),  # filtered in the browser: typing makes no requests
    }
    status = 200
    if any(str(data.get(key) or "").strip() for key in ("start", "finish")):
        try:
            data, result = _run(request)
        except PlanError as exc:
            context["error"], status = str(exc), exc.status
        else:
            by_id = {route["route_id"]: route for route in result["routes"]}
            selected = _selected_route_id(request, result, set(by_id))
            context.update(
                result=result,
                route=by_id[selected],
                geojson=by_id[selected]["map_geojson"],
                others=[
                    {"route_id": route["route_id"], "geojson": route["map_geojson"]}
                    for route in result["routes"]
                    if route["route_id"] != selected
                ],
                base_qs=urlencode(_plan_query(data, result)),  # without "route": the tabs add it
            )
    response = render(request, "routeplanner/map.html", context, status=status)
    if status == 503:
        response["Retry-After"] = RETRY_AFTER_SECONDS
    # OpenStreetMap blocks tile requests without a Referer, and Django's default "same-origin" policy removes it.
    # The browser default sends only the origin, never the path or query string.
    response["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response
