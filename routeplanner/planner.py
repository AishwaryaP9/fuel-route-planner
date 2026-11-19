"""Plan a trip: resolve both places, make one routing call, then plan fuel and list cities for every route."""

import hashlib
import json
import math
import time
from typing import Any, cast

import numpy as np
from django.conf import settings
from django.core.cache import cache

from .services import corridor, estimate, optimizer
from .services.cities import get_cities
from .services.corridor import RoutePoints
from .services.geocode import Location, resolve_location
from .services.optimizer import Candidate, Stop
from .services.routing import Route, fetch_routes
from .services.stations import StationIndex, get_index

GRID_MARGIN_MILES = 0.5  # covers the optimizer's 0.2-mile rounding when the starting fuel is raised
MERGE_GAP_TOLERANCE = 1e-6
MAX_GAPS_NAMED = 3
ESTIMATED_STOP_NAME = "Estimated stop (no station in the price data)"

Json = dict[str, Any]
Gap = tuple[float, float]


class PlanError(Exception):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def _try_plan(
    candidates: list[Candidate], total: float, max_range: float, initial_range: float, include_detour: bool
) -> list[Stop]:
    """Run the optimizer once.

    Billing is tank-neutral: the vehicle must arrive with the fuel it started with, so every mile driven is bought.
    """
    if include_detour:
        return optimizer.plan_stops_exact(candidates, total, max_range, initial_range, final_reserve=initial_range)
    return optimizer.plan_stops(
        [c[:3] for c in candidates],
        total,
        max_range,
        initial_range,
        min_savings=settings.MIN_PRICE_SAVINGS_PER_GALLON,
        final_reserve=initial_range,
    )


def _first_reach(candidates: list[Candidate], include_detour: bool) -> float | None:
    """Return the fuel (miles) needed to reach the nearest pump, or None without stations."""
    return min((c[0] + (c[3] if include_detour else 0.0) for c in candidates), default=None)


def _solve(
    candidates: list[Candidate],
    total: float,
    mpg: float,
    max_range: float,
    initial_range: float,
    include_detour: bool,
) -> tuple[list[Stop] | None, float]:
    """Plan with the requested starting fuel; if no station is in reach, retry with just enough to reach one."""
    attempts = [initial_range]
    reach = _first_reach(candidates, include_detour)
    if reach is not None and initial_range < reach <= max_range:
        raised_gallons = math.ceil((reach + GRID_MARGIN_MILES) / mpg * 10) / 10  # rounded up to 0.1 gal
        attempts.append(min(max_range, raised_gallons * mpg))
    for used in attempts:
        try:
            return _try_plan(candidates, total, max_range, used, include_detour), used
        except optimizer.InfeasibleRoute:
            continue
    return None, initial_range


def _search_plan(
    route_points: RoutePoints,
    route: Route,
    index: StationIndex,
    mpg: float,
    max_range: float,
    initial_range: float,
    include_detour: bool,
    allow_estimated: bool,
) -> tuple[list[Stop] | None, float, float, list[Gap], list[Candidate]]:
    """Return (plan or None, starting range used, search radius, gaps filled with estimates, real candidates).

    With detours billed the search starts wider, since the optimizer weighs each detour against the price; without,
    a far station would look free. If no plan exists, stretches without stations get estimated stops.
    """
    steps = settings.DETOUR_CORRIDOR_STEPS_MILES if include_detour else settings.CORRIDOR_STEPS_MILES
    plan: list[Stop] | None = None
    used_range = initial_range
    gaps: list[Gap] = []
    first: tuple[float, list[Candidate]] | None = None
    candidates: list[Candidate] = []
    radius = steps[0]
    for radius in steps:
        near = corridor.stations_near_route(route_points, index, radius)
        candidates = [(mile, float(index.price[i]), (i, distance), distance) for mile, i, distance in near]
        first = first or (radius, candidates)
        plan, used_range = _solve(candidates, route.distance_miles, mpg, max_range, initial_range, include_detour)
        if plan is not None:
            break
    real_candidates = candidates
    if plan is None and allow_estimated and first is not None:
        radius, candidates = first
        # Gaps count the detours, so stations too far off the route for this tank cannot hide a gap.
        spots = [(c[0], c[3] if include_detour else 0.0) for c in candidates]
        virtual, gaps = estimate.fill_gaps(route_points, route.distance_miles, spots, max_range, initial_range, index)
        if virtual:
            plan, used_range = _solve(
                candidates + virtual, route.distance_miles, mpg, max_range, initial_range, include_detour
            )
            if plan is not None:
                plan = estimate.merge_top_ups(plan, used_range, max_range)
    return plan, used_range, radius, gaps, real_candidates


def _stop_rows(plan: list[Stop], index: StationIndex, mpg: float) -> tuple[list[Json], dict[str, float | int]]:
    """Return the stop rows for the response and the running totals."""
    stops: list[Json] = []
    totals: dict[str, float | int] = {
        "cost": 0.0,
        "gallons": 0.0,
        "detour_miles": 0.0,
        "detour_cost": 0.0,
        "estimated_count": 0,
        "estimated_cost": 0.0,
    }
    for order, stop in enumerate(plan, 1):
        price = stop.price
        gallons = stop.miles_bought / mpg
        cost = gallons * price
        totals["gallons"] += gallons
        totals["cost"] += cost
        totals["detour_miles"] += stop.detour_miles
        totals["detour_cost"] += stop.detour_miles / mpg * price
        if isinstance(stop.station, estimate.Estimated):
            virtual = stop.station
            totals["estimated_count"] += 1
            totals["estimated_cost"] += cost
            place: Json = {
                "station_id": None,
                "name": ESTIMATED_STOP_NAME,
                "address": "",
                "city": virtual.city,
                "state": virtual.state,
                "lat": round(virtual.lat, 5),
                "lon": round(virtual.lon, 5),
                "distance_from_route_miles": 0.0,
                "estimated": True,
                "price_source": virtual.source,
            }
        else:
            station_index, distance = cast(tuple[int, float], stop.station)  # real stations: (index, distance)
            row = index.rows[station_index]
            place = {
                "station_id": row["opis_id"],
                "name": row["name"],
                "address": row["address"],
                "city": row["city"],
                "state": row["state"],
                "lat": round(row["lat"], 5),
                "lon": round(row["lon"], 5),
                "distance_from_route_miles": round(distance, 1),
                "estimated": False,
            }
        stops.append(
            {
                "order": order,
                **place,
                "price_per_gallon": round(price, 3),
                "mile_marker": round(stop.mile, 1),
                "detour_miles": round(stop.detour_miles, 1),
                "detour_gallons": round(stop.detour_miles / mpg, 2),
                "gallons_purchased": round(gallons, 2),
                "cost_usd": round(cost, 2),
            }
        )
    return stops, totals


def _assumption_text(
    plan: list[Stop],
    mpg: float,
    initial_range: float,
    used_range: float,
    include_detour: bool,
    gaps: list[Gap],
    estimated_count: int,
    estimated_cost: float,
) -> str:
    if used_range > initial_range:
        start_text = (
            f"The requested {initial_range / mpg:g} gal ({initial_range:g} mi) cannot reach any fuel station in the "
            f"price data, so the plan assumes the vehicle starts with {used_range / mpg:g} gal ({used_range:g} mi), "
            f"enough to reach the first station at mile {plan[0].mile:.1f}. It refuels to the same level before "
            "arriving, so the cost covers every mile of the trip. "
        )
    else:
        start_text = (
            f"The vehicle starts with {used_range / mpg:g} gal ({used_range:g} mi) of fuel to reach the first "
            "station and refuels to the same level before arriving, so the cost covers every mile of the trip. "
            "Change it with starting_fuel_gallons. "
        )
    detour_text = (
        "Detour fuel to each station and back (2 x distance from the route) is included."
        if include_detour
        else "Detour fuel to the stations is NOT included (include_detour_fuel=false)."
    )
    estimate_text = (
        f" The price data has no station on {_gap_text(gaps)}, so {estimated_count} estimated stop(s) priced at the "
        f"state average are used there (${estimated_cost:.2f} of the total is an estimate)."
        if estimated_count
        else ""
    )
    return start_text + detour_text + estimate_text


def _fuel_plan(
    route_points: RoutePoints,
    route: Route,
    index: StationIndex,
    mpg: float,
    max_range: float,
    initial_range: float,
    include_detour: bool = True,
    allow_estimated: bool = True,
) -> tuple[Json | None, str | None, list[Gap]]:
    """Return (fuel plan, None, gaps with estimated stops) or (None, the reason no plan exists, [])."""
    plan, used_range, radius, gaps, real_candidates = _search_plan(
        route_points, route, index, mpg, max_range, initial_range, include_detour, allow_estimated
    )
    if plan is None:
        reason = _infeasible_reason(real_candidates, route.distance_miles, max_range, initial_range, include_detour)
        if allow_estimated:
            reason += "; estimated stops could not cover it either"
        return None, reason, []

    stops, totals = _stop_rows(plan, index, mpg)
    estimated_count = int(totals["estimated_count"])
    fuel = {
        "vehicle": {
            "max_range_miles": max_range,
            "mpg": mpg,
            "tank_capacity_gallons": round(max_range / mpg, 2),
            "starting_fuel_gallons": round(used_range / mpg, 2),
            "requested_starting_fuel_gallons": round(initial_range / mpg, 2),
            "starting_fuel_adjusted": used_range > initial_range,
        },
        "assumption": _assumption_text(
            plan, mpg, initial_range, used_range, include_detour, gaps, estimated_count, totals["estimated_cost"]
        ),
        "include_detour_fuel": include_detour,
        "detour_miles_total": round(totals["detour_miles"], 1),
        "detour_cost_usd": round(totals["detour_cost"], 2),
        "estimated_stops": estimated_count,
        "estimated_cost_usd": round(totals["estimated_cost"], 2),
        "fuel_needed_gallons": round((route.distance_miles + totals["detour_miles"]) / mpg, 2),
        "gallons_purchased": round(totals["gallons"], 2),
        "total_cost_usd": round(totals["cost"], 2),
        "station_search_radius_miles": radius,
        "stops": stops,
    }
    return fuel, None, gaps if estimated_count else []


def _gap_text(gaps: list[Gap]) -> str:
    """Describe the gaps, e.g. 'mile 258-1253'; adjacent gaps are merged and long lists summarised."""
    merged: list[Gap] = []
    for a, b in gaps:
        if merged and abs(merged[-1][1] - a) < MERGE_GAP_TOLERANCE:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    if len(merged) <= MAX_GAPS_NAMED:
        return ", ".join(f"mile {a:.0f}-{b:.0f}" for a, b in merged)
    total = sum(b - a for a, b in merged)
    return f"{len(merged)} stretches ({total:.0f} mi in total, between mile {merged[0][0]:.0f} and {merged[-1][1]:.0f})"


def _infeasible_reason(
    candidates: list[Candidate], total: float, max_range: float, initial_range: float, include_detour: bool
) -> str:
    reach = _first_reach(candidates, include_detour)
    if reach is None:
        return "no fuel station in the price data near this route"
    if reach > max(initial_range, max_range):
        return (
            f"the nearest fuel station in the price data is {reach:.0f} mi from the start, "
            f"more than the vehicle range ({max_range:.0f} mi on a full tank)"
        )
    start = max(initial_range, min(max_range, reach))  # the raised start that _solve tried
    gap = optimizer.largest_gap([c[0] for c in candidates], total, start, max_range, start)
    if gap:
        return (
            f"no fuel station in the price data between mile {gap[0]:.0f} and mile {gap[1]:.0f} "
            f"of this route ({gap[1] - gap[0]:.0f} mi gap, vehicle range is {max_range:.0f} mi)"
        )
    return (
        f"the stations near this route are too far off it for a {max_range:.0f}-mi tank "
        "(the drive to the station and back counts against the range)"
    )


def _cities_on_route(route_points: RoutePoints, scope: str, radius: float) -> list[Json]:
    city_index = get_cities(scope)
    cities: list[Json] = []
    seen: set[tuple[str, str]] = set()
    for mile, i, distance in corridor.points_near_route(route_points, city_index.xyz, radius):
        key = (city_index.names[i], city_index.states[i])
        if key in seen:
            continue
        seen.add(key)
        item: Json = {
            "city": city_index.names[i],
            "state": city_index.states[i],
            "lat": round(float(city_index.lat[i]), 4),
            "lon": round(float(city_index.lon[i]), 4),
            "mile_marker": round(mile, 1),
            "distance_from_route_miles": round(distance, 1),
        }
        if city_index.population is not None:
            item["population"] = city_index.population[i]
        cities.append(item)
    return cities


def _geojson(route: Route, start: Location, finish: Location, fuel: Json | None) -> Json:
    geometry = np.round(corridor.thin_geometry(route.coords), 5).tolist()
    features: list[Json] = [
        {
            "type": "Feature",
            "properties": {"kind": "route"},
            "geometry": {"type": "LineString", "coordinates": geometry},
        }
    ]
    for role, location in (("start", start), ("finish", finish)):
        features.append(
            {
                "type": "Feature",
                "properties": {"kind": role, "label": location.label},
                "geometry": {"type": "Point", "coordinates": [location.lon, location.lat]},
            }
        )
    for stop in fuel["stops"] if fuel else []:
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "kind": "fuel_stop",
                    "order": stop["order"],
                    "name": stop["name"],
                    "price_per_gallon": stop["price_per_gallon"],
                    "cost_usd": stop["cost_usd"],
                    "gallons": stop["gallons_purchased"],
                    "mile_marker": stop["mile_marker"],
                    "estimated": stop["estimated"],
                },
                "geometry": {"type": "Point", "coordinates": [stop["lon"], stop["lat"]]},
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _recommend(routes: list[Json]) -> tuple[int, str]:
    """Return (route id, reason) for the recommended route.

    Routes priced only from real stations come first. The cheapest of those wins only if it saves at least
    RECOMMEND_MIN_SAVINGS_USD over the fastest route, so a slower route is not chosen for a trivial saving.
    """
    fastest = min(routes, key=lambda r: r["duration_hours"])
    with_plan = [r for r in routes if r["fuel"]]
    if not with_plan:
        return fastest["route_id"], "fastest route (no route has a fuel plan)"
    real = [r for r in with_plan if not r["fuel"]["estimated_stops"]]
    cheapest = min(real or with_plan, key=lambda r: (r["fuel"]["total_cost_usd"], r["duration_hours"]))
    note = "" if real else " (part of its fuel uses estimated prices; no route is fully covered by the price data)"
    if cheapest is fastest:
        return fastest["route_id"], "fastest route, also the cheapest fuel" + note
    if not fastest["fuel"]:
        return cheapest["route_id"], "cheapest fuel (the fastest route has no fuel plan)" + note
    if real and fastest["fuel"]["estimated_stops"]:
        return (
            cheapest["route_id"],
            "cheapest route priced only from the price data (the fastest route needs estimated prices)",
        )
    saved = fastest["fuel"]["total_cost_usd"] - cheapest["fuel"]["total_cost_usd"]
    extra_hours = cheapest["duration_hours"] - fastest["duration_hours"]
    limit = settings.RECOMMEND_MIN_SAVINGS_USD
    if saved >= limit:
        return (
            cheapest["route_id"],
            f"cheapest fuel: saves ${saved:.2f} vs the fastest route (+{extra_hours:.2f} h)" + note,
        )
    return fastest["route_id"], f"fastest route: the cheapest-fuel route saves only ${saved:.2f} (< ${limit:g})" + note


def _plan_key(start: Location, finish: Location, **params: object) -> str:
    raw = json.dumps(
        [round(start.lat, 4), round(start.lon, 4), round(finish.lat, 4), round(finish.lon, 4), params], sort_keys=True
    )
    return "plan:" + hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()


def _warnings(routes: list[Json]) -> list[str]:
    """Collect route warnings; also removes the internal "_gaps" field from every route."""
    for route in routes:
        if not route["fuel"]:
            route.pop("_gaps")
    warnings = [
        f"route {route['route_id']}: no fueling plan - {route['fuel_error']}" for route in routes if route["fuel_error"]
    ]
    for route in routes:
        if not route["fuel"]:
            continue
        gaps = route.pop("_gaps")
        fuel = route["fuel"]
        if gaps:
            warnings.append(
                f"route {route['route_id']}: no fuel station in the price data on {_gap_text(gaps)}; "
                f"{fuel['estimated_stops']} estimated stop(s) at state-average prices are used "
                f"(${fuel['estimated_cost_usd']:.2f} of the cost is an estimate)"
            )
        vehicle = fuel["vehicle"]
        if vehicle["starting_fuel_adjusted"]:
            warnings.append(
                f"route {route['route_id']}: starting fuel raised from {vehicle['requested_starting_fuel_gallons']:g} "
                f"to {vehicle['starting_fuel_gallons']:g} gal to reach the first station "
                f"(mile {fuel['stops'][0]['mile_marker']:g})"
            )
    return warnings


def plan_trip(
    start_text: str,
    finish_text: str,
    mpg: float | None = None,
    max_range: float | None = None,
    starting_fuel_gallons: float | None = None,
    alternatives: int | None = None,
    city_radius: float | None = None,
    include_cities: bool = True,
    city_scope: str | None = None,
    include_detour_fuel: bool = True,
    allow_estimated_stops: bool = True,
) -> Json:
    started = time.perf_counter()
    mpg = mpg or settings.VEHICLE_MPG
    max_range = max_range or settings.VEHICLE_MAX_RANGE_MILES
    if starting_fuel_gallons is None:
        # At most a quarter of the tank: a 5-gal default would fill a small tank, forcing a refill at the destination.
        tank_gallons = max_range / mpg
        starting_fuel_gallons = min(
            settings.VEHICLE_STARTING_FUEL_GALLONS, tank_gallons * settings.STARTING_FUEL_MAX_TANK_SHARE
        )
    initial_range = min(starting_fuel_gallons * mpg, max_range)
    city_scope = city_scope or settings.CITY_SCOPE
    city_radius = city_radius or settings.CITY_RADIUS_MILES[city_scope]

    start, finish = resolve_location(start_text), resolve_location(finish_text)
    route_count = alternatives or settings.ROUTE_ALTERNATIVES
    key = _plan_key(
        start,
        finish,
        alt=route_count,
        mpg=mpg,
        rng=max_range,
        init=initial_range,
        scope=city_scope,
        rad=city_radius,
        cities=include_cities,
        detour=include_detour_fuel,
        estimated=allow_estimated_stops,
    )
    cached = cache.get(key)
    if cached:
        cached["start"]["query"], cached["finish"]["query"] = start.query, finish.query
        cached["meta"] = {
            "routing_api_calls": 0,
            "geocoding_api_calls": start.api_calls + finish.api_calls,
            "route_cache_hit": True,
            "plan_cache_hit": True,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        return cached

    route_set = fetch_routes((start.lat, start.lon), (finish.lat, finish.lon), route_count)
    index = get_index()

    routes: list[Json] = []
    for route_id, route in enumerate(route_set.routes, 1):
        route_points = corridor.prepare_route(route.coords, route.distance_miles)
        fuel, fuel_error, gaps = _fuel_plan(
            route_points, route, index, mpg, max_range, initial_range, include_detour_fuel, allow_estimated_stops
        )
        item: Json = {
            "route_id": route_id,
            "distance_miles": round(route.distance_miles, 1),
            "duration_hours": round(route.duration_hours, 2),
            "fuel": fuel,
            "fuel_error": fuel_error,
            "_gaps": gaps,
            "cities": _cities_on_route(route_points, city_scope, city_radius) if include_cities else None,
            "map_geojson": _geojson(route, start, finish, fuel),
        }
        item["cities_count"] = len(item["cities"]) if item["cities"] is not None else None
        routes.append(item)

    with_plan = [r for r in routes if r["fuel"]]
    fastest = min(routes, key=lambda r: r["duration_hours"])["route_id"]
    cheapest_route = (
        min(with_plan, key=lambda r: (r["fuel"]["total_cost_usd"], r["duration_hours"])) if with_plan else None
    )
    cheapest = cheapest_route["route_id"] if cheapest_route else None  # estimates included
    recommended, reason = _recommend(routes)
    for route_item in routes:
        route_item["is_fastest"] = route_item["route_id"] == fastest
        route_item["is_cheapest_fuel"] = route_item["route_id"] == cheapest
        route_item["is_recommended"] = route_item["route_id"] == recommended
    warnings = _warnings(routes)

    result = {
        "start": {"query": start.query, "label": start.label, "lat": round(start.lat, 5), "lon": round(start.lon, 5)},
        "finish": {
            "query": finish.query,
            "label": finish.label,
            "lat": round(finish.lat, 5),
            "lon": round(finish.lon, 5),
        },
        "recommended_route_id": recommended,
        "recommendation_reason": reason,
        "route_count": len(routes),
        "city_scope": city_scope if include_cities else None,
        "warnings": warnings,
        "routes": routes,
        "meta": {
            "routing_api_calls": route_set.api_calls,
            "geocoding_api_calls": start.api_calls + finish.api_calls,
            "route_cache_hit": route_set.cache_hit,
            "plan_cache_hit": False,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        },
    }
    cache.set(key, result, settings.ROUTE_CACHE_SECONDS)
    return result
