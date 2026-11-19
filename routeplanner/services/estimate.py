"""Estimated fuel stops for stretches of a route with no station in the price data.

Inside each stretch that is too long for the vehicle, virtual stops are placed on the route and priced at the
average price of their state in the data (or the national median). Real stations elsewhere are unchanged.
"""

import itertools
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy.spatial import cKDTree

from .cities import CityIndex, get_cities
from .corridor import RoutePoints
from .optimizer import Candidate, Stop
from .stations import StationIndex, to_xyz

GAP_FACTOR = 0.9  # a stretch needing over 90% of the full-tank range counts as a gap, leaving a margin for detours
MAX_SPACING_MILES = 100.0
MERGE_TOLERANCE_MILES = 1e-6


@dataclass(frozen=True, slots=True)
class Estimated:
    """Optimizer key for a virtual stop."""

    mile: float
    lat: float
    lon: float
    city: str
    state: str
    price: float
    source: str


StatePrices = tuple[dict[str, tuple[float, int]], float]  # {state: (average, station count)}, national median

_prices: dict[int, StatePrices] = {}
_town_index: CityIndex | None = None
_town_tree: cKDTree | None = None


def reset() -> None:
    """Forget the state-average prices (the station index was reloaded)."""
    _prices.clear()


def state_prices(index: StationIndex) -> StatePrices:
    key = id(index)
    if key not in _prices:
        by_state: dict[str, list[float]] = {}
        for row, price in zip(index.rows, index.price, strict=True):
            by_state.setdefault(row["state"], []).append(float(price))
        averages = {state: (float(np.mean(prices)), len(prices)) for state, prices in by_state.items()}
        _prices.clear()
        _prices[key] = (averages, float(np.median(index.price)) if len(index.price) else 0.0)
    return _prices[key]


def _towns_at(lats: NDArray[np.float64], lons: NDArray[np.float64]) -> list[tuple[str, str]]:
    """Return the nearest named town (name, state) for each point."""
    global _town_index, _town_tree
    if _town_tree is None or _town_index is None:
        _town_index = get_cities("all")
        _town_tree = cKDTree(_town_index.xyz)
    _, nearest = _town_tree.query(to_xyz(np.asarray(lats), np.asarray(lons)))
    return [(_town_index.names[int(i)], _town_index.states[int(i)]) for i in np.atleast_1d(nearest)]


def find_gaps(
    stations: list[float] | list[tuple[float, float]], total: float, max_range: float, reserve: float
) -> list[tuple[float, float]]:
    """Return the stretches (from_mile, to_mile) the vehicle cannot reliably cover with the real stations.

    Stations are mile markers or (mile, one-way detour) pairs. A leg counts the miles along the route plus the
    detours at both ends, and stations whose round trip needs over half the tank are ignored. The last stretch must
    also leave `reserve` miles in the tank.
    """
    points = sorted(s if isinstance(s, tuple) else (float(s), 0.0) for s in stations)
    points = [(mile, detour) for mile, detour in points if mile <= total and 2 * detour <= max_range / 2]
    points = [(0.0, 0.0), *points, (float(total), 0.0)]
    gaps = []
    for k, ((a, detour_a), (b, detour_b)) in enumerate(itertools.pairwise(points)):
        need = b - a + detour_a + detour_b + (reserve if k == len(points) - 2 else 0.0)
        if need > GAP_FACTOR * max_range:
            gaps.append((a, b))
    return gaps


def fill_gaps(
    route: RoutePoints,
    total: float,
    stations: list[tuple[float, float]],
    max_range: float,
    reserve: float,
    index: StationIndex,
) -> tuple[list[Candidate], list[tuple[float, float]]]:
    """Return virtual stop candidates inside every gap, and the gaps."""
    gaps = find_gaps(stations, total, max_range, reserve)
    if not gaps:
        return [], []
    averages, median = state_prices(index)
    step = min(MAX_SPACING_MILES, max_range / 4)
    marks: list[float] = []
    for a, b in gaps:
        inner = max(0, int(np.ceil((b - a) / step)) - 1)
        marks += [a + (b - a) * k / (inner + 1) for k in range(1, inner + 1)]
        if a == 0.0:
            marks.append(0.0)
        if b == total:
            marks.append(total)
    marks = sorted(set(marks))
    at = np.clip(np.searchsorted(route.cum_miles, marks), 0, len(route.coords) - 1)
    points = route.coords[at]
    candidates: list[Candidate] = []
    for mile, (lon, lat), (city, state) in zip(marks, points, _towns_at(points[:, 1], points[:, 0]), strict=True):
        if state in averages:
            price, source = averages[state][0], f"average of {averages[state][1]} {state} stations in the price data"
        else:
            price, source = median, "national median of the price data"
        candidates.append((mile, price, Estimated(mile, float(lat), float(lon), city, state, price, source), 0.0))
    return candidates, gaps


def merge_top_ups(plan: list[Stop], start_range: float, max_range: float) -> list[Stop]:
    """Merge fuel bought at neighbouring estimated stops with the same price when the tank allows it.

    The optimizer may split fuel arbitrarily between stops that share a state price; merging keeps the cost and
    removes tiny top-ups. Real stations are never changed.
    """
    changed = True
    while changed:
        changed = False
        fuel, position = start_range, 0.0
        on_arrival, after_buying = [], []
        for stop in plan:
            fuel -= stop.mile - position + stop.detour_miles / 2
            on_arrival.append(fuel)
            fuel += stop.miles_bought
            after_buying.append(fuel)
            fuel -= stop.detour_miles / 2
            position = stop.mile
        for i in range(len(plan) - 1):
            first, second = plan[i], plan[i + 1]
            if not (isinstance(first.station, Estimated) and isinstance(second.station, Estimated)):
                continue
            if first.station.price != second.station.price:
                continue
            if on_arrival[i + 1] - first.miles_bought >= -MERGE_TOLERANCE_MILES:
                second.miles_bought += first.miles_bought
                del plan[i]
            elif after_buying[i] + second.miles_bought <= max_range + MERGE_TOLERANCE_MILES:
                first.miles_bought += second.miles_bought
                del plan[i + 1]
            else:
                continue
            changed = True
            break
    return plan
