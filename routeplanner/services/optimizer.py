"""Cheapest fuel stops along a route.

The vehicle holds at most `max_range` miles of fuel, starts with `initial_range` and must arrive with
`final_reserve` left. Stopping at a station `detour` miles off the route costs 2 x detour extra miles, which use
range and are bought like any other fuel; driving past costs nothing.
"""

from collections.abc import Hashable, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

EPS = 1e-9

Candidate = tuple[float, float, Hashable, float]  # mile, price per gallon, station key, one-way detour miles
CandidateNoDetour = tuple[float, float, Hashable]


class InfeasibleRoute(ValueError):
    pass


@dataclass
class Stop:
    station: Hashable
    mile: float
    price: float
    miles_bought: float  # includes this stop's detour
    detour_miles: float = 0.0  # round trip off the route and back


def largest_gap(
    miles: Sequence[float], total: float, initial_range: float, max_range: float, final_reserve: float = 0.0
) -> tuple[float, float] | None:
    """Return the first stretch (from_mile, to_mile) that cannot be driven, or None."""
    previous, limit = 0.0, initial_range
    for k, mile in enumerate([*miles, total]):
        need = mile - previous + (final_reserve if k == len(miles) else 0.0)
        if need > limit + EPS:
            return previous, mile
        previous, limit = mile, max_range
    return None


def check_feasible(
    miles: Sequence[float], total: float, initial_range: float, max_range: float, final_reserve: float = 0.0
) -> bool:
    return largest_gap(miles, total, initial_range, max_range, final_reserve) is None


def plan_stops(
    stations: Sequence[Candidate | CandidateNoDetour],
    total_miles: float,
    max_range: float = 500.0,
    initial_range: float = 500.0,
    min_savings: float = 0.0,
    final_reserve: float = 0.0,
) -> list[Stop]:
    """Plan stops with the greedy rule: buy just enough to reach a cheaper station, otherwise fill up.

    Without detours this is optimal when `min_savings` is 0; a few cents avoids a string of tiny top-ups.
    The reserve is planned as a virtual finish `final_reserve` miles past the real one.
    """
    ordered = sorted(
        ((s[0], s[1], s[2], float(s[3]) if len(s) > 3 else 0.0) for s in stations if s[0] <= total_miles),
        key=lambda s: s[0],
    )
    miles = [s[0] for s in ordered]
    if not check_feasible(miles, total_miles, initial_range, max_range, final_reserve):
        raise InfeasibleRoute("gap between fuel stations exceeds vehicle range")
    total_miles += final_reserve
    if total_miles <= initial_range:
        return []
    if not ordered:
        raise InfeasibleRoute("gap between fuel stations exceeds vehicle range")

    count = len(ordered)
    detours = [s[3] for s in ordered]

    def leg(i: int, j: int) -> float:
        """Fuel from the pump of station i to the pump of station j: back to the route, along it, off again."""
        return miles[j] - miles[i] + detours[i] + detours[j]

    reachable = [i for i in range(count) if miles[i] + detours[i] <= initial_range + EPS]
    if not reachable:
        raise InfeasibleRoute("no station reachable with the starting fuel")
    current = min(reachable, key=lambda i: (ordered[i][1], miles[i]))
    fuel = initial_range - miles[current] - detours[current]
    stops: list[Stop] = []

    while True:
        mile, price, key, detour = ordered[current]
        to_finish = total_miles - mile + detour
        cheaper = None
        for j in range(current + 1, count):
            if miles[j] - mile + detour > max_range + EPS:
                break
            if leg(current, j) <= max_range + EPS and ordered[j][1] < price - min_savings:
                cheaper = j
                break
        if cheaper is not None or to_finish <= max_range + EPS:
            need = leg(current, cheaper) if cheaper is not None else to_finish
            if fuel + 2 * detour >= need - EPS:  # enough fuel to drive past without stopping
                fuel = fuel + 2 * detour - need
                buy = 0.0
            else:
                buy = need - fuel
                fuel = 0.0
            if buy > EPS:
                stops.append(Stop(key, mile, price, buy, 2 * detour))
            if cheaper is None:
                return stops
            current = cheaper
            continue
        in_range = [j for j in range(current + 1, count) if leg(current, j) <= max_range + EPS]
        if not in_range:
            raise InfeasibleRoute("no station within range after the detour")
        buy = max_range - fuel
        following = min(in_range, key=lambda j: (ordered[j][1], miles[j]))
        fuel = max_range - leg(current, following)
        if buy > EPS:
            stops.append(Stop(key, mile, price, buy, 2 * detour))
        current = following


def plan_stops_exact(
    stations: Sequence[Candidate | CandidateNoDetour],
    total_miles: float,
    max_range: float = 500.0,
    initial_range: float = 500.0,
    final_reserve: float = 0.0,
    resolution: float = 0.2,
) -> list[Stop]:
    """Find the cheapest plan with detours by dynamic programming over the fuel level (stop or skip each station).

    Fuel is tracked on a 0.2-mile grid (0.02 gal at 10 mpg): fine enough for cent-level costs while keeping the state
    array small. Stations at the same spot are merged, keeping the cheapest, since the others can never win.
    """
    if total_miles + final_reserve <= initial_range:
        return []
    unit = resolution
    best_at_spot: dict[tuple[int, int], Candidate] = {}
    for s in stations:
        if s[0] > total_miles:
            continue
        detour = float(s[3]) if len(s) > 3 else 0.0
        spot = (round(s[0] / unit), round(detour / unit))
        if spot not in best_at_spot or s[1] < best_at_spot[spot][1]:
            best_at_spot[spot] = (s[0], s[1], s[2], detour)
    spots = sorted(best_at_spot.items())
    capacity = int(np.floor(max_range / unit + EPS))
    levels = np.arange(capacity + 1)

    def burn(cost: NDArray[np.float64], units: int) -> NDArray[np.float64]:
        """Use `units` of fuel; levels that would go below empty become impossible."""
        out = np.full(capacity + 1, np.inf)
        if 0 <= units <= capacity:
            out[: capacity + 1 - units] = cost[units:]
        return out

    cost = np.full(capacity + 1, np.inf)
    cost[min(capacity, round(initial_range / unit))] = 0.0
    position = 0
    trail = []
    for (spot_pos, detour_units), (mile, price, key, _detour) in spots:
        cost = burn(cost, spot_pos - position)
        position = spot_pos
        level_cost = levels * (price * unit)
        # Shift costs by the fuel price so a running minimum gives "buy up to this level" at the pump.
        at_pump = burn(cost, detour_units) - level_cost
        after_stop = burn(np.minimum.accumulate(at_pump) + level_cost, detour_units)
        trail.append((mile, price, key, detour_units, spot_pos, cost, after_stop, at_pump))
        cost = np.minimum(cost, after_stop)
    finish_pos = round((total_miles + final_reserve) / unit)
    cost = burn(cost, finish_pos - position)
    if not np.isfinite(cost.min()):
        raise InfeasibleRoute("gap between fuel stations exceeds vehicle range")

    state = int(np.argmin(cost))
    stops = []
    next_pos = finish_pos
    for mile, price, key, detour_units, spot_pos, before, after_stop, at_pump in reversed(trail):
        state += next_pos - spot_pos  # fuel on the route just after this station
        if after_stop[state] < before[state] - EPS:  # stopping here was strictly cheaper
            level = state + detour_units
            start = int(np.argmin(at_pump[: level + 1]))
            stops.append(Stop(key, mile, price, (level - start) * unit, 2 * detour_units * unit))
            state = start + detour_units
        next_pos = spot_pos
    stops.reverse()
    return stops
