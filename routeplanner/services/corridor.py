"""Find the stations and towns along a route and their mile markers."""

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy.spatial import cKDTree

from .stations import EARTH_RADIUS_MILES, StationIndex, to_xyz


@dataclass(frozen=True, slots=True)
class RoutePoints:
    coords: NDArray[np.float64]  # (N, 2) of [lon, lat]
    cum_miles: NDArray[np.float64]  # distance along the route at each point
    tree: cKDTree


def prepare_route(coords: NDArray[np.float64], total_miles: float) -> RoutePoints:
    lon, lat = np.radians(coords[:, 0]), np.radians(coords[:, 1])
    dlat, dlon = np.diff(lat), np.diff(lon)
    haversine = np.sin(dlat / 2) ** 2 + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(dlon / 2) ** 2
    segment = 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(haversine))
    cum = np.concatenate(([0.0], np.cumsum(segment)))
    if cum[-1] > 0:  # scale to the routing engine's road distance
        cum *= total_miles / cum[-1]
    return RoutePoints(coords, cum, cKDTree(to_xyz(coords[:, 1], coords[:, 0])))


def points_near_route(
    route: RoutePoints, xyz: NDArray[np.float64], radius_miles: float
) -> list[tuple[float, int, float]]:
    """Return (mile marker, point index, distance from the route) for points within the radius, by mile marker."""
    if len(xyz) == 0:
        return []
    distance, nearest = route.tree.query(xyz, distance_upper_bound=radius_miles)
    hits = np.nonzero(np.isfinite(distance))[0]
    found = [(float(route.cum_miles[nearest[i]]), int(i), float(distance[i])) for i in hits]
    found.sort(key=lambda t: t[0])
    return found


def stations_near_route(route: RoutePoints, index: StationIndex, radius_miles: float) -> list[tuple[float, int, float]]:
    return points_near_route(route, index.xyz, radius_miles)


def thin_geometry(coords: NDArray[np.float64], max_points: int = 2000) -> NDArray[np.float64]:
    """Keep at most `max_points` evenly spaced points (and the last one) to keep the map payload small."""
    count = len(coords)
    if count <= max_points:
        return coords
    keep = np.unique(np.concatenate((np.linspace(0, count - 1, max_points).astype(int), [count - 1])))
    return coords[keep]
