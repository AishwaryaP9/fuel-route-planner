"""Offline lists of US cities used to name the places a route passes through.

"major" uses geonamescache (population >= 15,000, with population); "all" uses every town in the ZIP database.
"""

from typing import Literal

import geonamescache
import numpy as np
import zipcodes
from numpy.typing import NDArray

from .geocode import STATE_CODES
from .stations import to_xyz

Scope = Literal["major", "all"]
SCOPES: tuple[Scope, ...] = ("major", "all")


class CityIndex:
    def __init__(
        self,
        names: list[str],
        states: list[str],
        lat: NDArray[np.float64],
        lon: NDArray[np.float64],
        population: list[int] | None = None,
    ) -> None:
        self.names = names
        self.states = states
        self.lat = lat
        self.lon = lon
        self.population = population
        self.xyz = to_xyz(lat, lon)


_cache: dict[str, CityIndex] = {}


def _major_cities() -> CityIndex:
    cities = [
        city
        for city in geonamescache.GeonamesCache().get_cities().values()
        if city["countrycode"] == "US" and city["admin1code"] in STATE_CODES
    ]
    return CityIndex(
        [city["name"] for city in cities],
        [city["admin1code"] for city in cities],
        np.array([city["latitude"] for city in cities]),
        np.array([city["longitude"] for city in cities]),
        [city["population"] for city in cities],
    )


def _all_towns() -> CityIndex:
    points: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for record in zipcodes.list_all():
        if not record["lat"] or not record["long"] or record.get("zip_code_type") == "PO BOX":
            continue
        points.setdefault((record["city"], record["state"]), []).append((float(record["lat"]), float(record["long"])))
    keys = list(points)
    return CityIndex(
        [name for name, _ in keys],
        [state for _, state in keys],
        np.array([sum(p[0] for p in points[key]) / len(points[key]) for key in keys]),
        np.array([sum(p[1] for p in points[key]) / len(points[key]) for key in keys]),
    )


def get_cities(scope: str = "major") -> CityIndex:
    if scope not in _cache:
        _cache[scope] = _major_cities() if scope == "major" else _all_towns()
    return _cache[scope]
