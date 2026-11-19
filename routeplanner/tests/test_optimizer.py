import random

from django.test import SimpleTestCase

from routeplanner.services.optimizer import InfeasibleRoute, plan_stops, plan_stops_exact


def brute_force_cost(stations, total, max_range, initial):
    """Exact DP over integer miles (fuel in miles) for cross-checking the greedy."""
    INF = float("inf")
    pts = [*sorted(stations), (total, 0, None)]
    # state: (station index, fuel on arrival) -> min cost (per mile of range bought)
    best = {(-1, initial): 0.0}
    pos = {-1: 0}
    for i, (m, _p, _) in enumerate(pts):
        pos[i] = m
    for i in range(len(pts)):
        # arrive from any earlier state
        for (j, f), c in list(best.items()):
            if j >= i:
                continue
            d = pos[i] - pos[j]
            if d > max_range:
                continue
            # at j we may top up before leaving (j == -1 cannot buy)
            for buy in range(0, max_range - f + 1):
                if j == -1 and buy:
                    break
                nf = f + buy - d
                if nf < 0:
                    continue
                cost = c + (buy * pts[j][1] if j >= 0 else 0)
                key = (i, nf)
                if cost < best.get(key, INF):
                    best[key] = cost
    last = len(pts) - 1
    return min((c for (j, f), c in best.items() if j == last), default=INF)


class OptimizerTests(SimpleTestCase):
    def test_no_stop_when_within_starting_range(self):
        self.assertEqual(plan_stops([(100, 3.0, "a")], 400), [])

    def test_buys_only_at_the_cheapest_station(self):
        # 900 mi trip; cheap station at 400, expensive at 200 and 700
        s = [(200, 4.0, "a"), (400, 3.0, "b"), (700, 4.5, "c")]
        stops = plan_stops(s, 900)
        self.assertEqual([x.station for x in stops], ["b"])
        self.assertAlmostEqual(stops[0].miles_bought, 400)  # 900 total - 500 free start fuel

    def test_buy_just_enough_to_reach_cheaper_station(self):
        s = [(300, 3.5, "a"), (500, 3.0, "b")]
        stops = plan_stops(s, 900, initial_range=500)
        total = sum(x.miles_bought for x in stops)
        self.assertAlmostEqual(total, 400)

    def test_final_reserve_bills_every_mile(self):
        # 200 mi trip, 50 mi starting fuel that must be replaced: buys exactly 200 mi of fuel
        s = [(10, 3.0, "a"), (120, 3.5, "b")]
        stops = plan_stops(s, 200, max_range=500, initial_range=50, final_reserve=50)
        self.assertAlmostEqual(sum(x.miles_bought for x in stops), 200)
        self.assertEqual([x.station for x in stops], ["a"])  # cheapest station, bought once

    def test_final_reserve_infeasible_when_no_station_reachable(self):
        with self.assertRaises(InfeasibleRoute):
            plan_stops([(80, 3.0, "a")], 200, max_range=500, initial_range=50, final_reserve=50)

    def test_final_reserve_matches_exact_dp(self):
        rnd = random.Random(11)
        checked = 0
        for _ in range(400):
            max_range = rnd.choice([40, 60])
            reserve = rnd.randint(0, 10)
            total = rnd.randint(5, 220)
            n = rnd.randint(1, 12)
            st = sorted((rnd.randint(0, total), rnd.randint(25, 45) / 10, i) for i in range(n))
            try:
                stops = plan_stops(st, total, max_range, reserve, final_reserve=reserve)
            except InfeasibleRoute:
                continue
            self.assertAlmostEqual(sum(s.miles_bought for s in stops), total, places=6)
            greedy = sum(s.miles_bought * s.price for s in stops)
            exact = brute_force_cost(st, total + reserve, max_range, reserve)
            self.assertAlmostEqual(greedy, exact, places=6, msg=f"{st} total={total} R={max_range} res={reserve}")
            checked += 1
        self.assertGreater(checked, 50)

    def test_raises_when_a_gap_exceeds_the_range(self):
        with self.assertRaises(InfeasibleRoute):
            plan_stops([(100, 3.0, "a"), (800, 3.0, "b")], 1200)

    def test_matches_exact_dp_on_random_instances(self):
        rnd = random.Random(7)
        checked = 0
        for _ in range(150):
            max_range = rnd.choice([40, 60])
            total = rnd.randint(max_range + 1, 220)
            n = rnd.randint(1, 12)
            st = sorted((rnd.randint(1, total - 1), rnd.randint(25, 45) / 10, i) for i in range(n))
            try:
                stops = plan_stops(st, total, max_range, max_range)
            except InfeasibleRoute:
                continue
            greedy = sum(s.miles_bought * s.price for s in stops)
            exact = brute_force_cost(st, total, max_range, max_range)
            self.assertAlmostEqual(greedy, exact, places=6, msg=f"{st} total={total} R={max_range}")
            checked += 1
        self.assertGreater(checked, 50)


def exact_cost_with_detours(stations, total, max_range, initial, final_reserve):
    """Exact DP over integer miles with stop/skip per station. stations: (mile, price, key, detour)."""
    INF = float("inf")
    best = [INF] * (max_range + 1)  # best[f] = min cost with f miles of fuel, on the route
    best[initial] = 0.0
    pos = 0
    for m, p, _k, d in sorted(stations):
        nxt = [INF] * (max_range + 1)
        for f, c in enumerate(best):
            if c < INF and f - (m - pos) >= 0:
                nxt[f - (m - pos)] = min(nxt[f - (m - pos)], c)
        best, pos = nxt, m
        stopped = list(best)  # skip = keep; stop = drive off d, buy, drive back d
        for f, c in enumerate(best):
            if c == INF or f < d:
                continue
            for g in range(f - d, max_range + 1):  # fuel at the pump after buying
                if g - d < 0:
                    continue
                cost = c + (g - (f - d)) * p
                if cost < stopped[g - d]:
                    stopped[g - d] = cost
        best = stopped
    need = total - pos + final_reserve
    return min((c for f, c in enumerate(best) if f >= need), default=INF)


class DetourTests(SimpleTestCase):
    def test_detour_fuel_is_bought_and_billed(self):
        # one station 5 mi off the route: the 10 mi round trip is bought there
        stops = plan_stops_exact([(20, 3.0, "a", 5)], 200, max_range=500, initial_range=50, final_reserve=50)
        self.assertEqual(len(stops), 1)
        self.assertAlmostEqual(stops[0].detour_miles, 10)
        self.assertAlmostEqual(stops[0].miles_bought, 210)  # 200 mi route + 10 mi detour

    def test_detour_counts_against_range(self):
        # station at mile 48 is 5 mi off the route: 53 mi > 50 mi of starting fuel, so it can't be reached
        with self.assertRaises(InfeasibleRoute):
            plan_stops([(48, 3.0, "a", 5)], 200, max_range=500, initial_range=50, final_reserve=50)
        # same position on the route itself is fine
        self.assertEqual(
            len(plan_stops([(48, 3.0, "a", 0)], 200, max_range=500, initial_range=50, final_reserve=50)), 1
        )

    def test_passing_a_station_costs_no_detour(self):
        # cheap station first, pricier one later that is never needed: only one detour billed
        stops = plan_stops([(10, 3.0, "a", 4), (100, 4.0, "b", 9)], 300, 500, 50, final_reserve=50)
        self.assertEqual([x.station for x in stops], ["a"])
        self.assertAlmostEqual(sum(x.miles_bought for x in stops), 300 + 8)

    def test_exact_planner_matches_brute_force_with_detours(self):
        rnd = random.Random(5)
        checked = 0
        for _ in range(400):
            max_range = rnd.choice([40, 60])
            reserve = rnd.randint(0, 8)
            total = rnd.randint(20, 200)
            spots = {
                rnd.randint(0, total): (rnd.randint(25, 45) / 10, rnd.randint(0, 4)) for _ in range(rnd.randint(1, 12))
            }
            st = [(m, p, i, d) for i, (m, (p, d)) in enumerate(sorted(spots.items()))]
            exact = exact_cost_with_detours(st, total, max_range, reserve, reserve)
            try:
                stops = plan_stops_exact(st, total, max_range, reserve, reserve, resolution=1.0)
            except InfeasibleRoute:
                self.assertEqual(exact, float("inf"), st)
                continue
            self.assertAlmostEqual(sum(s.miles_bought * s.price for s in stops), exact, places=6, msg=str(st))
            # bought fuel = route + detours of the stops actually made (tank-neutral)
            self.assertAlmostEqual(
                sum(s.miles_bought for s in stops), total + sum(s.detour_miles for s in stops), places=6
            )
            checked += 1
        self.assertGreater(checked, 50)

    def test_exact_planner_without_detours_matches_greedy(self):
        rnd = random.Random(3)
        for _ in range(100):
            total = rnd.randint(61, 220)
            st = sorted((rnd.randint(0, total), rnd.randint(25, 45) / 10, i) for i in range(rnd.randint(2, 12)))
            try:
                greedy = plan_stops(st, total, 60, 10, final_reserve=10)
            except InfeasibleRoute:
                continue
            exact = plan_stops_exact(st, total, 60, 10, 10, resolution=1.0)
            self.assertAlmostEqual(
                sum(s.miles_bought * s.price for s in greedy), sum(s.miles_bought * s.price for s in exact), places=6
            )

    def test_co_located_stations_keep_only_cheapest(self):
        stops = plan_stops_exact([(20, 3.5, "dear", 2), (20, 3.0, "cheap", 2)], 200, 500, 50, 50)
        self.assertEqual([s.station for s in stops], ["cheap"])


class LargestGapTests(SimpleTestCase):
    def test_reports_the_first_gap_longer_than_the_range(self):
        from routeplanner.services.optimizer import largest_gap

        self.assertIsNone(largest_gap([100, 400], 700, 500, 500))
        self.assertEqual(largest_gap([100, 900], 1200, 500, 500), (100, 900))
        self.assertEqual(largest_gap([], 600, 500, 500), (0.0, 600))
        # last leg must keep the reserve: 300 mi from the last station + 250 reserve > 500
        self.assertEqual(largest_gap([100], 400, 500, 500, final_reserve=250), (100, 400))
        self.assertIsNone(largest_gap([100], 400, 500, 500, final_reserve=200))


class EstimateGapTests(SimpleTestCase):
    def test_find_gaps_reports_only_long_stretches(self):
        from routeplanner.services.estimate import find_gaps

        self.assertEqual(find_gaps([100, 300, 600], 900, 500, 50), [])
        self.assertEqual(find_gaps([100, 700], 900, 500, 50), [(100, 700)])
        self.assertEqual(find_gaps([], 300, 500, 50), [])
        self.assertEqual(find_gaps([], 600, 500, 50), [(0.0, 600)])
        self.assertEqual(find_gaps([100], 600, 500, 50), [(100, 600)])  # last leg needs 500 + 50 reserve

    def test_merging_top_ups_keeps_the_cost_with_fewer_stops(self):
        from routeplanner.services.estimate import Estimated, merge_top_ups
        from routeplanner.services.optimizer import Stop

        def est(mile, price):
            return Estimated(mile, 0, 0, "x", "OR", price, "test")

        # 0.4-mi top-up at mile 100, then 200 mi at mile 200 (same price): moved to one stop, same fuel bought
        plan = [Stop(est(100, 3.5), 100, 3.5, 4.0), Stop(est(200, 3.5), 200, 3.5, 200.0)]
        out = merge_top_ups(plan, 300, 500)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0].miles_bought, 204.0)
        # different prices are never merged
        plan = [Stop(est(100, 3.5), 100, 3.5, 4.0), Stop(est(200, 4.5), 200, 4.5, 200.0)]
        self.assertEqual(len(merge_top_ups(plan, 300, 500)), 2)
        # not enough fuel to skip the first stop, and the tank is too full to take the second: kept
        plan = [Stop(est(100, 3.5), 100, 3.5, 300.0), Stop(est(450, 3.5), 450, 3.5, 300.0)]
        self.assertEqual(len(merge_top_ups(plan, 100, 400)), 2)

    def test_find_gaps_counts_detours(self):
        from routeplanner.services.estimate import find_gaps

        # 20-mi tank: stations every 15 mi along the route but 12 mi off it are useless (24-mi round trip)
        far = [(m, 12.0) for m in range(15, 200, 15)]
        self.assertEqual(find_gaps(far, 200, 20, 2), [(0.0, 200.0)])
        # the same stations right on the route leave no gap (needs 15 mi per leg < 0.9 x 20)
        self.assertEqual(find_gaps([(m, 0.0) for m in range(15, 200, 15)], 200, 20, 2), [])
        # a leg of 10 mi along the route plus 4.5 + 4.5 mi of detours needs 19 mi > 0.9 x 20 = 18
        self.assertEqual(find_gaps([(5, 0.0), (15, 4.5), (25, 4.5)], 30, 20, 2), [(15, 25)])
