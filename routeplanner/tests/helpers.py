"""Test helpers: a fake OSRM response and a polyline encoder."""

from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any
from unittest import mock

import numpy as np
from django.conf import settings

MILES_PER_DEGREE_LAT = 69.0
MILES_PER_DEGREE_LON = 53.0
ROAD_FACTOR = 1.15
FAKE_SPEED_MPH = 62
METERS_PER_MILE = 1609.344


def encode_polyline(coords: Iterable[Sequence[float]], precision: int = 6) -> str:
    """Encode [lon, lat] points as a Google polyline (plain Python reference implementation)."""
    out: list[str] = []
    previous = (0, 0)
    for lon, lat in coords:
        current = (round(lat * 10**precision), round(lon * 10**precision))
        for delta in (current[0] - previous[0], current[1] - previous[1]):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        previous = current
    return "".join(out)


def fake_osrm(
    start: tuple[float, float], finish: tuple[float, float], n: int = 3000, routes: int = 3
) -> dict[str, Any]:
    """Return a fake OSRM response: a straight main route plus bowed alternatives."""
    t = np.linspace(0, 1, n)
    miles = np.hypot((finish[0] - start[0]) * MILES_PER_DEGREE_LAT, (finish[1] - start[1]) * MILES_PER_DEGREE_LON)
    miles *= ROAD_FACTOR
    payload_routes = []
    for k in range(routes):
        bow = k * 0.6 * np.sin(np.pi * t)  # sideways bulge in degrees
        lon = start[1] + (finish[1] - start[1]) * t
        lat = start[0] + (finish[0] - start[0]) * t + bow
        points = np.column_stack((lon, lat))
        geometry = (
            encode_polyline(points)
            if settings.OSRM_GEOMETRIES == "polyline6"
            else {"type": "LineString", "coordinates": points.tolist()}
        )
        route_miles = miles * (1 + 0.05 * k)
        payload_routes.append(
            {
                "distance": route_miles * METERS_PER_MILE,
                "duration": route_miles / FAKE_SPEED_MPH * 3600,
                "geometry": geometry,
            }
        )
    return {"code": "Ok", "routes": payload_routes}


@contextmanager
def osrm_returns(payload: dict[str, Any], status: int = 200) -> Iterator[mock.MagicMock]:
    """Patch the OSRM HTTP session; yields the mock so tests can count calls."""
    with mock.patch("routeplanner.services.routing._session.get") as get:
        get.return_value.status_code = status
        get.return_value.json.return_value = payload
        yield get
