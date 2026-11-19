"""In-memory index of fuel stations, loaded once per process."""

from dataclasses import dataclass
from typing import TypedDict

import numpy as np
from numpy.typing import ArrayLike, NDArray

from routeplanner.models import FuelStation

EARTH_RADIUS_MILES = 3958.8


class StationRow(TypedDict):
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price: float
    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class StationIndex:
    ids: NDArray[np.int64]
    lat: NDArray[np.float64]
    lon: NDArray[np.float64]
    price: NDArray[np.float64]
    xyz: NDArray[np.float64]
    rows: list[StationRow]


_index: StationIndex | None = None


def to_xyz(lat_deg: ArrayLike, lon_deg: ArrayLike) -> NDArray[np.float64]:
    """Convert degrees to 3-D points on a sphere of the Earth's radius, in miles.

    For the short distances queried here the straight (chord) distance equals the distance along the surface.
    """
    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    cos_lat = np.cos(lat)
    return EARTH_RADIUS_MILES * np.column_stack((cos_lat * np.cos(lon), cos_lat * np.sin(lon), np.sin(lat)))


def get_index() -> StationIndex:
    global _index
    if _index is None:
        rows: list[StationRow] = list(
            FuelStation.objects.values("opis_id", "name", "address", "city", "state", "price", "lat", "lon")
        )
        lat = np.array([row["lat"] for row in rows])
        lon = np.array([row["lon"] for row in rows])
        _index = StationIndex(
            ids=np.array([row["opis_id"] for row in rows]),
            lat=lat,
            lon=lon,
            price=np.array([row["price"] for row in rows]),
            xyz=to_xyz(lat, lon) if rows else np.empty((0, 3)),
            rows=rows,
        )
    return _index


def clear_index() -> None:
    global _index
    _index = None
    from . import estimate  # imported here to avoid a circular import; its state prices come from this index

    estimate.reset()
