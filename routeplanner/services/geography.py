"""Offline check that a point is in the contiguous USA."""

import geonamescache
import numpy as np
import zipcodes
from scipy.spatial import cKDTree

from .stations import to_xyz

CONUS_BOX = (24.3, 49.6, -125.0, -66.8)  # min_lat, max_lat, min_lon, max_lon
NEAR_ZIP_MILES = 5.0
NON_CONUS_STATES = {"AK", "HI", "PR", "VI", "GU", "AS", "MP"}

_zip_tree: cKDTree | None = None
_city_tree: cKDTree | None = None
_city_info: list[tuple[str, str]] = []


def _build() -> None:
    global _zip_tree, _city_tree, _city_info
    zip_points = np.array(
        [
            (float(record["lat"]), float(record["long"]))
            for record in zipcodes.list_all()
            if record["lat"] and record["state"] not in NON_CONUS_STATES and record["zip_code_type"] != "PO BOX"
        ]
    )
    _zip_tree = cKDTree(to_xyz(zip_points[:, 0], zip_points[:, 1]))
    cities = list(geonamescache.GeonamesCache().get_cities().values())
    city_points = np.array([(city["latitude"], city["longitude"]) for city in cities])
    _city_tree = cKDTree(to_xyz(city_points[:, 0], city_points[:, 1]))
    _city_info = [(city["name"], city["countrycode"]) for city in cities]


def nearest_city(lat: float, lon: float) -> tuple[str, str, float]:
    """Return (name, country code, miles away) of the nearest city with at least 15,000 people, worldwide."""
    if _city_tree is None:
        _build()
    assert _city_tree is not None
    distance, i = _city_tree.query(to_xyz(np.array([lat]), np.array([lon])))
    name, country = _city_info[int(i[0])]
    return name, country, float(distance[0])


def in_usa(lat: float, lon: float) -> bool:
    """Return True if the point is in the contiguous USA."""
    if not (CONUS_BOX[0] <= lat <= CONUS_BOX[1] and CONUS_BOX[2] <= lon <= CONUS_BOX[3]):
        return False
    # The box alone also contains Toronto and Monterrey: a nearby US ZIP code or a US nearest city settles it.
    if _zip_tree is None:
        _build()
    assert _zip_tree is not None
    if _zip_tree.query(to_xyz(np.array([lat]), np.array([lon])))[0][0] <= NEAR_ZIP_MILES:
        return True
    return nearest_city(lat, lon)[1] == "US"
