"""Client for the OSRM routing API."""

import hashlib
import logging
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np
import requests
from django.conf import settings
from django.core.cache import cache
from numpy.typing import NDArray

from . import polyline

log = logging.getLogger(__name__)
_session = requests.Session()  # reuses the connection for later calls
_session.headers["User-Agent"] = settings.HTTP_USER_AGENT

METERS_PER_MILE = 1609.344

# One lock per cache key, so identical concurrent requests in a process share one OSRM call.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class RoutingError(RuntimeError):
    """An error whose message is safe to show to API clients."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class Route:
    coords: NDArray[np.float64]  # (N, 2) of [lon, lat]
    distance_miles: float
    duration_hours: float


@dataclass(frozen=True, slots=True)
class RouteSet:
    routes: list[Route]
    api_calls: int
    cache_hit: bool


def _coords(geometry: str | dict[str, Any]) -> NDArray[np.float64]:
    if isinstance(geometry, str):
        return polyline.decode(geometry, 6 if settings.OSRM_GEOMETRIES == "polyline6" else 5)
    return np.asarray(geometry["coordinates"], dtype=float)


def _request(start: tuple[float, float], finish: tuple[float, float], alternatives: int) -> dict[str, Any]:
    url = f"{settings.OSRM_BASE_URL}/route/v1/driving/{start[1]:.6f},{start[0]:.6f};{finish[1]:.6f},{finish[0]:.6f}"
    params: dict[str, Any] = {"overview": "full", "geometries": settings.OSRM_GEOMETRIES, "steps": "false"}
    if alternatives > 1:
        params["alternatives"] = alternatives - 1  # OSRM counts the extra routes beyond the main one
    try:
        resp = _session.get(url, params=params, timeout=settings.HTTP_TIMEOUT)
    except requests.RequestException as exc:
        log.warning("OSRM request failed: %s", exc)
        raise RoutingError("routing service is unreachable or too slow; please retry shortly", 503) from exc
    if resp.status_code == 429:
        log.warning("OSRM rate-limited the request (429)")
        raise RoutingError("routing service is busy (rate limited); please retry shortly", 503)
    try:
        data: dict[str, Any] = resp.json()
    except ValueError as exc:
        log.warning("OSRM returned non-JSON (HTTP %s)", resp.status_code)
        raise RoutingError("routing service returned an invalid response") from exc
    if data.get("code") == "NoRoute":
        raise RoutingError("no drivable route between these two locations", 422)
    if resp.status_code >= 400 or data.get("code") != "Ok" or not data.get("routes"):
        log.warning("OSRM error HTTP %s: %s %s", resp.status_code, data.get("code"), data.get("message"))
        raise RoutingError("routing service could not compute a route")
    return data


def fetch_routes(start: tuple[float, float], finish: tuple[float, float], alternatives: int | None = None) -> RouteSet:
    """Return the main route and its alternatives for (lat, lon) points.

    One HTTP request returns every alternative with its geometry, so a whole plan costs a single routing call
    (none when cached). OSRM may return fewer alternatives than requested.
    """
    alternatives = settings.ROUTE_ALTERNATIVES if alternatives is None else alternatives
    key_text = (
        f"{start[0]:.4f},{start[1]:.4f}|{finish[0]:.4f},{finish[1]:.4f}|{alternatives}|{settings.OSRM_GEOMETRIES}"
    )
    key = "routes:" + hashlib.sha1(key_text.encode(), usedforsecurity=False).hexdigest()
    hit = cache.get(key)
    if hit:
        return RouteSet(hit, 0, True)

    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        hit = cache.get(key)  # another thread may have filled it while this one waited
        if hit:
            return RouteSet(hit, 0, True)
        data = _request(start, finish, alternatives)
        routes = [
            Route(_coords(r["geometry"]), r["distance"] / METERS_PER_MILE, r["duration"] / 3600.0)
            for r in data["routes"]
        ]
        cache.set(key, routes, settings.ROUTE_CACHE_SECONDS)
    with _locks_guard:
        _locks.pop(key, None)
    return RouteSet(routes, 1, False)
