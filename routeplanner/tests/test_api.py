import itertools
import json
import re
import threading
from unittest import mock
from urllib.parse import urlparse

import requests
from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from routeplanner.services import geocode, polyline, routing, stations

from .helpers import encode_polyline, fake_osrm, osrm_returns


class PolylineTests(SimpleTestCase):
    def test_decodes_google_reference_example(self):
        pts = polyline.decode("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)  # lat,lon pairs from Google docs
        expected = [(-120.2, 38.5), (-120.95, 40.7), (-126.453, 43.252)]
        for got, want in zip(pts, expected, strict=True):
            self.assertAlmostEqual(got[0], want[0], places=5)
            self.assertAlmostEqual(got[1], want[1], places=5)

    def test_round_trips_precision_6_points(self):
        import numpy as np

        rng = np.random.default_rng(1)
        pts = np.round(np.column_stack((rng.uniform(-125, -67, 5000), rng.uniform(25, 49, 5000))), 6)
        out = polyline.decode(encode_polyline(pts), 6)
        self.assertEqual(out.shape, pts.shape)
        self.assertLess(abs(out - pts).max(), 1e-6)

    def test_empty_polyline_has_no_points(self):
        self.assertEqual(polyline.decode("").shape, (0, 2))


class GeocodeTests(SimpleTestCase):
    def setUp(self):  # any real network call fails the test
        p = mock.patch("routeplanner.services.geocode.requests.get", side_effect=AssertionError("network call"))
        p.start()
        self.addCleanup(p.stop)

    def test_resolves_lat_lon_text(self):
        self.assertEqual(geocode.resolve_location("41.88,-87.63").source, "coordinates")

    def test_resolves_city_and_state_offline(self):
        for q in ("Chicago, IL", "chicago, illinois", "Chicago IL", "Austin, Texas, USA", "Washington, DC"):
            loc = geocode.resolve_location(q)
            self.assertEqual((loc.source, loc.api_calls), ("city", 0), q)

    def test_resolves_city_name_written_without_space(self):
        self.assertEqual(geocode.resolve_location("De Forest, WI").source, "city")

    def test_resolves_zip_codes(self):
        self.assertEqual(geocode.resolve_location("10 Main St, 90210").source, "zip")
        self.assertEqual(geocode.resolve_location("60601").source, "zip")

    def test_rejects_ambiguous_city_without_state(self):
        with self.assertRaisesMessage(geocode.LocationError, "ambiguous"):
            geocode.resolve_location("Springfield")

    def test_rejects_foreign_and_invalid_places(self):
        for bad in (
            "43.65,-79.38",  # Toronto
            "49.28,-123.12",  # Vancouver
            "25.67,-100.31",  # Monterrey
            "51.5,-0.12",  # London UK
            "Toronto, ON",
            "Chicago, XX",
            "00000",
            "",
            "   ",
            "a" * 500,
            "'; DROP TABLE x;--, IL",
        ):
            with self.assertRaises(geocode.LocationError, msg=bad):
                geocode.resolve_location(bad)

    def test_accepts_remote_us_coordinates(self):
        self.assertEqual(geocode.resolve_location("40.95,-119.0").source, "coordinates")  # Black Rock Desert, NV

    def test_typo_suggests_close_city_instead_of_guessing(self):
        # Nominatim matches loose words ('collage station' gave a place 1,700 mi away), so a close city name is
        # suggested instead. setUp makes any network call fail, so these must be answered offline.
        cases = {
            "collage station": "College Station (AR, TX)",
            "Collage Station, TX": "'College Station, TX'",
            "Dalas, TX": "'Dallas, TX'",
            "Venuss": "Venus (FL, PA, TX)",
        }
        for text, hint in cases.items():
            with self.assertRaises(geocode.LocationError, msg=text) as ctx:
                geocode.resolve_location(text)
            self.assertIn("did you mean", str(ctx.exception))
            self.assertIn(hint, str(ctx.exception))

    def test_name_with_no_close_city_still_uses_nominatim(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = []
        with mock.patch("routeplanner.services.geocode.requests.get", return_value=resp) as g:
            with self.assertRaisesMessage(geocode.LocationError, "could not find 'Qwxzzy Vvvb'"):
                geocode.resolve_location("Qwxzzy Vvvb")
        self.assertEqual(g.call_count, 1)

    def test_nominatim_fallback_counts_one_call_and_is_cached(self):
        cache.clear()
        resp = mock.Mock(status_code=200)
        resp.json.return_value = [{"lat": "41.8781", "lon": "-87.6298", "display_name": "Willis Tower, Chicago"}]
        with mock.patch("routeplanner.services.geocode.requests.get", return_value=resp) as g:
            a = geocode.resolve_location("233 S Wacker Dr Willis Tower")
            b = geocode.resolve_location("233 S Wacker Dr Willis Tower")
        self.assertEqual((a.api_calls, b.api_calls, g.call_count), (1, 0, 1))


class RouteApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("import_fuel_prices", verbosity=0)
        stations.clear_index()

    def setUp(self):
        cache.clear()

    def _get(self, start, finish, status=200, **extra):
        s, f = geocode.resolve_location(start), geocode.resolve_location(finish)
        with osrm_returns(fake_osrm((s.lat, s.lon), (f.lat, f.lon)), status) as g:
            resp = self.client.get("/api/route/", {"start": start, "finish": finish, **extra})
        return resp, g

    def test_long_trip_has_multiple_stops_and_consistent_cost(self):
        resp, g = self._get("New York, NY", "Los Angeles, CA")
        self.assertEqual(resp.status_code, 200, resp.content)
        d = resp.json()
        self.assertEqual(g.call_count, 1)  # exactly one routing call for ALL alternatives
        self.assertEqual(d["meta"]["routing_api_calls"], 1)
        self.assertEqual(d["route_count"], 3)
        self.assertEqual(sum(r["is_recommended"] for r in d["routes"]), 1)
        rec = next(r for r in d["routes"] if r["route_id"] == d["recommended_route_id"])
        fast = next(r for r in d["routes"] if r["is_fastest"])
        cheap = min(d["routes"], key=lambda r: r["fuel"]["total_cost_usd"])
        saved = fast["fuel"]["total_cost_usd"] - cheap["fuel"]["total_cost_usd"]
        self.assertIs(rec, cheap if saved >= 5 else fast)  # cheapest only if it saves >= $5 over the fastest
        self.assertTrue(d["recommendation_reason"])
        for r in d["routes"]:
            self.assertGreater(r["cities_count"], 8)
            self.assertTrue(all(c["population"] >= 15000 for c in r["cities"]))
            marks = [c["mile_marker"] for c in r["cities"]]
            self.assertEqual(marks, sorted(marks))
        fuel, stops = rec["fuel"], rec["fuel"]["stops"]
        self.assertGreaterEqual(len(stops), 2)
        self.assertAlmostEqual(sum(s["cost_usd"] for s in stops), fuel["total_cost_usd"], delta=0.05 * len(stops))
        # starting fuel is replaced before arrival, so every mile is billed, including detours to the stations
        self.assertGreater(fuel["detour_miles_total"], 0)
        self.assertAlmostEqual(
            fuel["detour_miles_total"], sum(s["detour_miles"] for s in stops), delta=0.1 * len(stops)
        )
        self.assertAlmostEqual(
            fuel["gallons_purchased"], (rec["distance_miles"] + fuel["detour_miles_total"]) / 10, delta=0.1
        )
        self.assertAlmostEqual(fuel["fuel_needed_gallons"], fuel["gallons_purchased"], delta=0.1)
        self.assertEqual(fuel["vehicle"]["starting_fuel_gallons"], 5)
        self.assertEqual(fuel["vehicle"]["tank_capacity_gallons"], 50)
        marks = [s["mile_marker"] for s in stops]
        self.assertEqual(marks, sorted(marks))
        self.assertTrue(all(b - a <= 500 for a, b in zip([0, *marks], marks, strict=False)))
        self.assertEqual(rec["map_geojson"]["features"][0]["geometry"]["type"], "LineString")
        self.assertIn("assumption", fuel)

    @override_settings(OSRM_GEOMETRIES="geojson")
    def test_supports_geojson_geometry_setting(self):
        cache.clear()
        resp, g = self._get("Dallas, TX", "Chicago, IL")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(g.call_args.kwargs["params"]["geometries"], "geojson")

    def test_requests_polyline6_by_default(self):
        _, g = self._get("Dallas, TX", "Chicago, IL")
        self.assertEqual(g.call_args.kwargs["params"]["geometries"], "polyline6")
        self.assertEqual(g.call_args.kwargs["timeout"], (3.05, 8))

    def test_repeat_is_cached_and_new_vehicle_reuses_route(self):
        self._get("Dallas, TX", "Chicago, IL")
        resp, g = self._get("Dallas, TX", "Chicago, IL")
        self.assertEqual(g.call_count, 0)
        self.assertTrue(resp.json()["meta"]["plan_cache_hit"])
        resp, g = self._get("Dallas, TX", "Chicago, IL", mpg=5)  # new plan, same OSRM route
        self.assertEqual(g.call_count, 0)
        m = resp.json()["meta"]
        self.assertTrue(m["route_cache_hit"])
        self.assertFalse(m["plan_cache_hit"])

    def test_short_trip_is_billed_for_every_mile(self):
        resp, _ = self._get("Dallas, TX", "Austin, TX")
        for r in resp.json()["routes"]:
            fuel = r["fuel"]
            self.assertGreaterEqual(len(fuel["stops"]), 1)
            self.assertGreater(fuel["total_cost_usd"], 0)
            billed_miles = r["distance_miles"] + fuel["detour_miles_total"]
            self.assertAlmostEqual(fuel["gallons_purchased"], billed_miles / 10, delta=0.1)

    def test_detour_fuel_can_be_switched_off(self):
        on, _ = self._get("Dallas, TX", "Chicago, IL")
        off, _ = self._get("Dallas, TX", "Chicago, IL", include_detour_fuel="false")
        r_on, r_off = on.json()["routes"][0], off.json()["routes"][0]
        self.assertTrue(r_on["fuel"]["include_detour_fuel"])
        self.assertFalse(r_off["fuel"]["include_detour_fuel"])
        self.assertEqual(r_off["fuel"]["detour_miles_total"], 0)
        self.assertAlmostEqual(r_off["fuel"]["gallons_purchased"], r_off["distance_miles"] / 10, delta=0.1)
        self.assertIn("include_detour_fuel=false", off.json()["map_html_url"])

    def test_starting_fuel_changes_stops_not_gallons_billed(self):
        billed = []
        for gal in (0, 5, 20):
            resp, _ = self._get("Dallas, TX", "Austin, TX", starting_fuel_gallons=gal)
            r = resp.json()["routes"][0]
            self.assertEqual(r["fuel"]["vehicle"]["starting_fuel_gallons"], gal)
            billed.append(r["fuel"]["gallons_purchased"])
        self.assertAlmostEqual(min(billed), max(billed), delta=0.1)

    def test_default_starting_fuel_is_capped_for_small_tanks(self):
        # 50-mi tank = 5 gal: the 5-gal default would be a full tank (forcing a refill at the destination),
        # so the default is a quarter of the tank. An explicit value is used as sent.
        resp, _ = self._get("Dallas, TX", "Austin, TX", max_range_miles=50)
        v = resp.json()["routes"][0]["fuel"]["vehicle"]
        self.assertEqual(v["requested_starting_fuel_gallons"], 1.25)
        resp, _ = self._get("Dallas, TX", "Austin, TX", max_range_miles=50, starting_fuel_gallons=5)
        self.assertEqual(resp.json()["routes"][0]["fuel"]["vehicle"]["requested_starting_fuel_gallons"], 5)
        resp, _ = self._get("Dallas, TX", "Austin, TX")  # default 500-mi tank: still 5 gal
        self.assertEqual(resp.json()["routes"][0]["fuel"]["vehicle"]["requested_starting_fuel_gallons"], 5)

    def test_empty_start_tank_buys_at_start(self):
        resp, _ = self._get("Dallas, TX", "Austin, TX", starting_fuel_gallons=0)
        fuel = resp.json()["routes"][0]["fuel"]
        self.assertGreaterEqual(len(fuel["stops"]), 1)
        self.assertLess(fuel["stops"][0]["mile_marker"], 1)

    def test_sparse_start_raises_starting_fuel_instead_of_far_detour(self):
        # the price data has no station near Houston; 5 gal (50 mi) cannot reach the first one on the route
        resp, _ = self._get("Houston, TX", "San Antonio, TX")
        d = resp.json()
        r = d["routes"][0]
        fuel, v = r["fuel"], r["fuel"]["vehicle"]
        self.assertTrue(v["starting_fuel_adjusted"])
        self.assertEqual(v["requested_starting_fuel_gallons"], 5)
        self.assertGreater(v["starting_fuel_gallons"], 5)
        self.assertEqual(fuel["station_search_radius_miles"], 25)  # normal detour corridor, not the 50-mi fallback
        self.assertTrue(all(s["distance_from_route_miles"] <= 25 for s in fuel["stops"]))
        self.assertLessEqual(
            fuel["stops"][0]["mile_marker"] + fuel["stops"][0]["detour_miles"] / 2, v["starting_fuel_gallons"] * 10
        )
        billed = (r["distance_miles"] + fuel["detour_miles_total"]) / 10
        self.assertAlmostEqual(fuel["gallons_purchased"], billed, delta=0.1)  # still tank-neutral
        self.assertIn("cannot reach any fuel station", fuel["assumption"])
        self.assertTrue(any("starting fuel raised" in w for w in d["warnings"]))

    def test_normal_start_is_not_adjusted(self):
        resp, _ = self._get("Dallas, TX", "Chicago, IL")
        d = resp.json()
        for r in d["routes"]:
            self.assertFalse(r["fuel"]["vehicle"]["starting_fuel_adjusted"])
            self.assertEqual(r["fuel"]["estimated_stops"], 0)
            self.assertFalse(any(s["estimated"] for s in r["fuel"]["stops"]))
        self.assertFalse(any("starting fuel raised" in w for w in d["warnings"]))

    def _get_with_gap(self, lo=200, hi=800, **extra):
        """Dallas -> Chicago (~920 mi) with all stations between mile lo and hi removed: a gap longer than the range."""
        from routeplanner.services import corridor

        real = corridor.stations_near_route

        def holed(rp, index, radius):
            return [t for t in real(rp, index, radius) if not lo <= t[0] <= hi]

        with mock.patch("routeplanner.planner.corridor.stations_near_route", side_effect=holed):
            return self._get("Dallas, TX", "Chicago, IL", **extra)

    def test_gap_without_stations_uses_estimated_stops(self):
        from routeplanner.services import estimate

        resp, _ = self._get_with_gap()
        d = resp.json()
        r = d["routes"][0]
        fuel = r["fuel"]
        self.assertIsNotNone(fuel, r["fuel_error"])
        est = [s for s in fuel["stops"] if s["estimated"]]
        self.assertGreaterEqual(len(est), 1)
        self.assertEqual(fuel["estimated_stops"], len(est))
        self.assertAlmostEqual(fuel["estimated_cost_usd"], sum(s["cost_usd"] for s in est), delta=0.05)
        avg, _ = estimate.state_prices(stations.get_index())
        for s in est:
            self.assertTrue(200 <= s["mile_marker"] <= 800)
            self.assertIsNone(s["station_id"])
            self.assertAlmostEqual(s["price_per_gallon"], avg[s["state"]][0], places=2)  # state average
        for s in fuel["stops"]:
            if not s["estimated"]:
                self.assertFalse(200 <= s["mile_marker"] <= 800)
        marks = [0, *[s["mile_marker"] for s in fuel["stops"]]]
        self.assertTrue(all(b - a <= 500 for a, b in itertools.pairwise(marks)))
        billed = (r["distance_miles"] + fuel["detour_miles_total"]) / 10
        self.assertAlmostEqual(fuel["gallons_purchased"], billed, delta=0.1)
        self.assertIn("estimated stop", fuel["assumption"])
        self.assertTrue(any("estimated stop" in w for w in d["warnings"]))
        self.assertTrue(any(f["properties"].get("estimated") for f in r["map_geojson"]["features"]))

    def test_estimated_stops_can_be_switched_off(self):
        resp, _ = self._get_with_gap(allow_estimated_stops="false")
        d = resp.json()
        r = d["routes"][0]
        self.assertIsNone(r["fuel"])
        self.assertIn("no fuel station", r["fuel_error"])
        self.assertIn("allow_estimated_stops=false", d["map_html_url"])

    def test_small_tank_gets_a_plan(self):
        # 50-mi tank (the API minimum): stations far off the route don't fit the round trip; gaps are found with detours
        resp, _ = self._get("Dallas, TX", "Chicago, IL", max_range_miles=50, starting_fuel_gallons=2)
        r = resp.json()["routes"][0]
        fuel = r["fuel"]
        self.assertIsNotNone(fuel, r["fuel_error"])
        tank, pos = fuel["vehicle"]["starting_fuel_gallons"] * 10, 0.0
        for s in fuel["stops"]:  # never empty at a pump, never above the 50-mi tank
            tank -= s["mile_marker"] - pos + s["detour_miles"] / 2
            pos = s["mile_marker"]
            self.assertGreater(tank, -0.6)
            tank += s["gallons_purchased"] * 10
            self.assertLess(tank, 50.6)
            tank -= s["detour_miles"] / 2
        self.assertGreater(tank - (r["distance_miles"] - pos), -0.6)
        billed = (r["distance_miles"] + fuel["detour_miles_total"]) / 10
        self.assertAlmostEqual(fuel["gallons_purchased"], billed, delta=0.1)

    def test_range_below_minimum_is_rejected(self):
        # a smaller tank would need an estimated stop every few miles
        for rng in (1, 20, 49.9):
            params = {"start": "Dallas, TX", "finish": "Austin, TX", "max_range_miles": rng}
            resp = self.client.get("/api/route/", params)
            self.assertEqual(resp.status_code, 400, rng)
            self.assertIn("between 50 and 2000", resp.json()["error"])

    def test_error_mentions_that_estimated_stops_were_tried(self):
        with mock.patch("routeplanner.planner.estimate.fill_gaps", return_value=([], [])):
            resp, _ = self._get_with_gap()
        r = resp.json()["routes"][0]
        self.assertIsNone(r["fuel"])
        self.assertIn("estimated stops could not cover it", r["fuel_error"])

    def test_gap_text_merges_and_summarises_gaps(self):
        from routeplanner.planner import _gap_text

        self.assertEqual(_gap_text([(258, 1253)]), "mile 258-1253")
        self.assertEqual(_gap_text([(11, 34), (34, 58), (90, 120)]), "mile 11-58, mile 90-120")  # adjacent merged
        many = [(k * 100, k * 100 + 30) for k in range(10)]
        self.assertEqual(_gap_text(many), "10 stretches (300 mi in total, between mile 0 and 930)")

    def test_estimated_stops_on_map_page(self):
        from routeplanner.services import corridor

        real = corridor.stations_near_route
        s, f = geocode.resolve_location("Dallas, TX"), geocode.resolve_location("Chicago, IL")
        with (
            mock.patch(
                "routeplanner.planner.corridor.stations_near_route",
                side_effect=lambda rp, i, r: [t for t in real(rp, i, r) if not 200 <= t[0] <= 800],
            ),
            osrm_returns(fake_osrm((s.lat, s.lon), (f.lat, f.lon))),
        ):
            resp = self.client.get("/api/route/map/", {"start": "Dallas, TX", "finish": "Chicago, IL"})
        self.assertContains(resp, "estimated price")
        self.assertContains(resp, "state-average prices")

    def test_recommendation_needs_a_real_saving(self):
        from routeplanner import planner

        def route(rid, hours, cost, est=0):
            fuel = {"total_cost_usd": cost, "estimated_stops": est} if cost else None
            return {"route_id": rid, "duration_hours": hours, "fuel": fuel}

        fast = route(1, 10.0, 300.0)
        with override_settings(RECOMMEND_MIN_SAVINGS_USD=5):
            self.assertEqual(planner._recommend([fast, route(2, 11.4, 299.31)])[0], 1)  # $0.69 saving: fastest
            self.assertEqual(planner._recommend([fast, route(2, 10.3, 280.0)])[0], 2)  # $20 saving: cheapest
            self.assertEqual(planner._recommend([route(1, 10.0, None), route(2, 12.0, 280.0)])[0], 2)
            self.assertEqual(planner._recommend([route(1, 10.0, None), route(2, 12.0, None)])[0], 1)
            self.assertEqual(planner._recommend([fast])[0], 1)

    def test_recommendation_prefers_real_prices_over_estimates(self):
        from routeplanner import planner

        def route(rid, hours, cost, est=0):
            return {"route_id": rid, "duration_hours": hours, "fuel": {"total_cost_usd": cost, "estimated_stops": est}}

        with override_settings(RECOMMEND_MIN_SAVINGS_USD=5):
            # route 2 looks $50 cheaper, but only thanks to estimated prices: the fully real route 1 wins
            rid, why = planner._recommend([route(1, 10.0, 300.0), route(2, 10.5, 250.0, est=2)])
            self.assertEqual(rid, 1)
            # the fastest needs estimates, a real route exists: the real one wins even for a tiny saving
            rid, why = planner._recommend([route(1, 10.0, 300.0, est=1), route(2, 10.5, 299.0)])
            self.assertEqual(rid, 2)
            self.assertIn("estimated prices", why)
            # every route needs estimates: normal rule, and the reason says so
            rid, why = planner._recommend([route(1, 10.0, 300.0, est=1), route(2, 10.5, 299.0, est=1)])
            self.assertEqual(rid, 1)
            self.assertIn("estimated prices", why)

    def test_compact_json_by_default_pretty_on_request(self):
        compact, _ = self._get("Dallas, TX", "Austin, TX")
        pretty, _ = self._get("Dallas, TX", "Austin, TX", pretty=1)
        self.assertNotIn(b"\n", compact.content)
        self.assertIn(b"\n", pretty.content)

    def test_head_request_is_allowed(self):
        with osrm_returns(fake_osrm((32.8, -96.8), (30.3, -97.7))):
            self.assertEqual(
                self.client.head("/api/route/", {"start": "Dallas, TX", "finish": "Austin, TX"}).status_code, 200
            )

    def test_city_scope_all_lists_more_towns(self):
        major, _ = self._get("Dallas, TX", "Chicago, IL")
        allc, _ = self._get("Dallas, TX", "Chicago, IL", city_scope="all")
        self.assertGreater(allc.json()["routes"][0]["cities_count"], major.json()["routes"][0]["cities_count"])

    def test_infeasible_plan_still_returns_routes_and_map(self):
        resp, _ = self._get_with_gap(allow_estimated_stops="false")  # no station from mile 200 to 800, no estimates
        self.assertEqual(resp.status_code, 200)
        d = resp.json()
        self.assertTrue(d["warnings"])
        for r in d["routes"]:
            self.assertIsNone(r["fuel"])
            self.assertIn("no fuel station", r["fuel_error"])
            self.assertTrue(r["map_geojson"]["features"])
        self.assertTrue(next(r for r in d["routes"] if r["is_recommended"])["is_fastest"])

    def test_map_page_renders_including_infeasible(self):
        from routeplanner.services import corridor

        real = corridor.stations_near_route
        s, f = geocode.resolve_location("Dallas, TX"), geocode.resolve_location("Chicago, IL")
        for extra, gap in (({}, False), ({"allow_estimated_stops": "false"}, True)):
            near = (lambda rp, i, r: [t for t in real(rp, i, r) if not 200 <= t[0] <= 800]) if gap else real
            with (
                osrm_returns(fake_osrm((s.lat, s.lon), (f.lat, f.lon))),
                mock.patch("routeplanner.planner.corridor.stations_near_route", side_effect=near),
            ):
                resp = self.client.get("/api/route/map/", {"start": "Dallas, TX", "finish": "Chicago, IL", **extra})
            self.assertContains(resp, "leaflet")
            self.assertContains(resp, "Cities on this route")
            if gap:
                self.assertContains(resp, "No fuel plan for this route")

    def test_map_links_carry_vehicle_fields(self):
        from urllib.parse import parse_qs, urlparse

        extra = {"mpg": 8, "max_range_miles": 300, "starting_fuel_gallons": 10, "include_cities": "false"}
        resp, _ = self._get("Dallas, TX", "Chicago, IL", **extra)
        d = resp.json()
        for url in [d["map_html_url"], *(r["map_html_url"] for r in d["routes"])]:
            q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            for k, v in extra.items():
                self.assertEqual(q[k], str(v), url)
        q = parse_qs(urlparse(d["routes"][1]["map_html_url"]).query)
        self.assertEqual(q["route"], ["2"])
        # the map page built from that link shows the same custom vehicle and the same cost
        page = self.client.get(urlparse(d["routes"][0]["map_html_url"]).path, q | {"route": ["1"]})
        self.assertContains(page, "300 mi</b> on a full tank")
        self.assertContains(page, f"${d['routes'][0]['fuel']['total_cost_usd']:.2f}")
        self.assertContains(page, "max_range_miles=300")  # route tabs keep the vehicle fields
        self.assertNotContains(page, "route=1&amp;route=")  # no duplicated route param in tabs

    def test_map_page_sends_referrer_for_osm_tiles(self):
        resp, _ = self._get("Dallas, TX", "Austin, TX")
        map_path = urlparse(resp.json()["map_html_url"]).path
        page = self.client.get(map_path, {"start": "Dallas, TX", "finish": "Austin, TX"})
        self.assertEqual(page["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertEqual(resp["Referrer-Policy"], "same-origin")  # JSON API keeps Django's stricter default

    def test_map_page_returns_html_errors(self):
        resp = self.client.get("/api/route/map/", {"start": "Dallas, TX"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("text/html", resp["Content-Type"])

    def test_validation_errors_are_400_never_500(self):
        base = {"start": "Dallas, TX", "finish": "Austin, TX"}
        bad = [
            {},
            {"start": "Dallas, TX"},
            {"start": "", "finish": "Austin, TX"},
            {**base, "mpg": "nan"},
            {**base, "mpg": "inf"},
            {**base, "mpg": "1e308"},
            {**base, "mpg": 0},
            {**base, "mpg": -3},
            {**base, "mpg": "abc"},
            {**base, "max_range_miles": "inf"},
            {**base, "max_range_miles": 0},
            {**base, "max_range_miles": 49},
            {**base, "starting_fuel_gallons": "nan"},
            {**base, "starting_fuel_gallons": -1},
            {**base, "starting_fuel_gallons": 60},  # default tank is 500 mi / 10 mpg = 50 gal
            {**base, "max_range_miles": 100, "starting_fuel_gallons": 20},  # tank = 10 gal
            {**base, "alternatives": "nan"},
            {**base, "alternatives": 1000},
            {**base, "alternatives": 2.7},
            {**base, "alternatives": 0},
            {**base, "city_radius_miles": "nan"},
            {**base, "city_radius_miles": 1e9},
            {**base, "city_scope": "x"},
            {**base, "include_cities": "maybe"},
            {**base, "include_detour_fuel": "maybe"},
            {**base, "allow_estimated_stops": "maybe"},
            {"start": "London, UK", "finish": "Austin, TX"},
            {"start": "43.65,-79.38", "finish": "Austin, TX"},
        ]
        for params in bad:
            resp = self.client.get("/api/route/", params)
            self.assertEqual(resp.status_code, 400, params)
            self.assertIn("error", resp.json())

    def test_rejects_invalid_json_bodies(self):
        for body in ("[]", '"text"', "123", "null", "{bad json", '{"start": ["x"], "finish": 5}'):
            resp = self.client.post("/api/route/", body, content_type="application/json")
            self.assertEqual(resp.status_code, 400, body)

    def test_accepts_json_and_form_posts(self):
        s, f = geocode.resolve_location("Dallas, TX"), geocode.resolve_location("Chicago, IL")
        for flag in (False, "false", "0", "no"):
            with osrm_returns(fake_osrm((s.lat, s.lon), (f.lat, f.lon))):
                resp = self.client.post(
                    "/api/route/",
                    {"start": "Dallas, TX", "finish": "Chicago, IL", "include_cities": flag},
                    content_type="application/json",
                )
            self.assertIsNone(resp.json()["routes"][0]["cities"], flag)
            cache.clear()

    def test_returns_405_for_unsupported_methods(self):
        self.assertEqual(self.client.put("/api/route/").status_code, 405)

    def test_maps_routing_failures_to_clean_errors(self):
        s, f = (32.8, -96.8), (41.9, -87.7)
        cases = [
            (dict(payload={"code": "NoRoute", "message": "x"}), 422),
            (dict(payload={"code": "Ok", "routes": []}), 502),
            (dict(payload={"message": "boom"}, status=500), 502),
            (dict(payload={"code": "TooBig"}, status=400), 502),
            (dict(payload={}, status=429), 503),
        ]
        for kwargs, want in cases:
            cache.clear()
            with osrm_returns(**kwargs):
                resp = self.client.get("/api/route/", {"start": "Dallas, TX", "finish": "Chicago, IL"})
            self.assertEqual(resp.status_code, want, kwargs)
            self.assertNotIn("router.project-osrm.org", resp.content.decode())
            if want == 503:
                self.assertEqual(resp["Retry-After"], "5")
        for exc in (requests.Timeout("slow http://router.project-osrm.org"), requests.ConnectionError("dns")):
            cache.clear()
            with mock.patch("routeplanner.services.routing._session.get", side_effect=exc):
                resp = self.client.get("/api/route/", {"start": "Dallas, TX", "finish": "Chicago, IL"})
            self.assertEqual(resp.status_code, 503)
            self.assertNotIn("router.project-osrm", resp.content.decode())
        del s, f

    def test_returns_502_when_routing_returns_non_json(self):
        with mock.patch("routeplanner.services.routing._session.get") as g:
            g.return_value.status_code = 200
            g.return_value.json.side_effect = ValueError("not json")
            resp = self.client.get("/api/route/", {"start": "Dallas, TX", "finish": "Chicago, IL"})
        self.assertEqual(resp.status_code, 502)


class ConcurrentRequestTests(SimpleTestCase):
    def test_concurrent_identical_requests_make_one_osrm_call(self):
        cache.clear()
        payload = fake_osrm((32.8, -96.8), (41.9, -87.7), n=500)
        results = []
        with osrm_returns(payload) as g:
            threads = [
                threading.Thread(target=lambda: results.append(routing.fetch_routes((32.8, -96.8), (41.9, -87.7))))
                for _ in range(12)
            ]
            [t.start() for t in threads]
            [t.join() for t in threads]
        self.assertEqual(g.call_count, 1)
        self.assertEqual(len(results), 12)


class SearchPageTests(TestCase):
    """The search page at "/": a search card over a map of the USA, plus the offline type-ahead endpoint."""

    @classmethod
    def setUpTestData(cls):
        call_command("import_fuel_prices", verbosity=0)
        stations.clear_index()

    def setUp(self):
        cache.clear()

    def _page(self, params, status=200):
        s, f = geocode.resolve_location("Austin, TX"), geocode.resolve_location("College Station, TX")
        with osrm_returns(fake_osrm((s.lat, s.lon), (f.lat, f.lon))) as g:
            resp = self.client.get("/", params)
        self.assertEqual(resp.status_code, status, resp.content[:300])
        return resp, g

    def test_home_is_an_empty_map_with_the_search_card(self):
        resp, g = self._page({})
        self.assertEqual(g.call_count, 0)  # no routing call before a search
        self.assertEqual(resp["Referrer-Policy"], "strict-origin-when-cross-origin")  # OSM tiles need it
        for name in (
            "start",
            "finish",
            "max_range_miles",
            "mpg",
            "starting_fuel_gallons",
            "alternatives",
            "city_scope",
            "city_radius_miles",
            "include_cities",
            "include_detour_fuel",
            "allow_estimated_stops",
        ):
            self.assertContains(resp, f'name="{name}"')
        self.assertContains(resp, 'value="500"')
        self.assertContains(resp, 'placeholder="5 (default)"')
        self.assertContains(resp, "Reset to defaults")
        self.assertNotContains(resp, "<aside>")  # no results sidebar yet
        self.assertNotContains(resp, "<details open>")  # options collapsed by default

    def test_search_from_the_form_matches_the_json_api(self):
        # what a browser sends without JS: every field, plus the hidden "false" twin before each ticked checkbox
        form = {
            "start": "Austin, TX",
            "finish": "College Station, TX",
            "max_range_miles": "500",
            "mpg": "10",
            "starting_fuel_gallons": "",
            "alternatives": "3",
            "city_scope": "major",
            "city_radius_miles": "5",
            "include_cities": ["false", "true"],
            "include_detour_fuel": ["false", "true"],
            "allow_estimated_stops": ["false", "true"],
        }
        resp, g = self._page(form)
        self.assertEqual(g.call_count, 1)
        self.assertContains(resp, "<aside>")
        self.assertContains(resp, 'value="Austin, TX"')
        api = self.client.get("/api/route/", {"start": "Austin, TX", "finish": "College Station, TX"}).json()
        rec = next(r for r in api["routes"] if r["is_recommended"])
        self.assertEqual(api["meta"]["routing_api_calls"], 0)  # same plan, served from the cache
        self.assertContains(resp, f'class="cost">${rec["fuel"]["total_cost_usd"]:.2f}')
        self.assertContains(resp, "Cities on this route")

    def test_unticked_boxes_really_switch_off(self):
        # an unticked checkbox sends only its hidden "false" twin
        resp, _ = self._page(
            {
                "start": "Austin, TX",
                "finish": "College Station, TX",
                "include_cities": "false",
                "include_detour_fuel": "false",
            }
        )
        self.assertNotContains(resp, "Cities on this route")
        self.assertContains(resp, "Detour fuel to the stations is NOT included")
        self.assertContains(resp, "<details open>")  # a non-default option keeps the panel open
        self.assertNotContains(resp, 'name="include_cities" value="true" checked')

    def test_errors_are_shown_in_the_card(self):
        cases = [
            ({"start": "Dallas, TX", "finish": "collage station"}, 400, "did you mean College Station (AR, TX)"),
            ({"start": "Dallas, TX", "finish": "Austin, TX", "max_range_miles": "20"}, 400, "between 50 and 2000"),
            ({"start": "Dallas, TX"}, 400, "required"),
        ]
        for params, status, text in cases:
            resp, g = self._page(params, status)
            self.assertEqual(g.call_count, 0)
            self.assertContains(resp, 'role="alert"', status_code=status)
            self.assertContains(resp, text, status_code=status)
            self.assertContains(resp, 'value="Dallas, TX"', status_code=status)  # the typed input is kept
            self.assertEqual(resp["Referrer-Policy"], "strict-origin-when-cross-origin")

    def test_place_suggestions_are_embedded_and_filtered_locally(self):
        boom = AssertionError("network call")
        with (
            mock.patch("requests.get", side_effect=boom),
            mock.patch("requests.post", side_effect=boom),
            mock.patch("requests.Session.request", side_effect=boom),
        ):
            resp, _ = self._page({})
        self.assertEqual(self.client.get("/api/places/", {"q": "aus"}).status_code, 404)  # endpoint removed
        html = resp.content.decode()
        script = html[html.index("<script>", html.index('id="places"')) :]
        self.assertNotIn("fetch(", script)
        self.assertNotIn("XMLHttpRequest", script)
        self.assertNotIn("sendBeacon", script)
        items = json.loads(re.search(r'<script id="places" type="application/json">(.*?)</script>', html).group(1))
        labels = [label for label, _ in items]
        self.assertEqual(len(labels), len(set(labels)))  # no duplicates
        self.assertIn("Dallas, TX", labels)
        self.assertIn(["Saint Louis, MO", "saint louis, mo"], items)  # the key matches "St. Lo" once normalised
        self.assertLessEqual(len(items), 2000)
        self.assertLess(labels.index("Houston, TX"), labels.index("College Station, TX"))  # most populous first
        self.assertFalse([x for x in labels if x.endswith((", PR", ", AK", ", HI"))])  # contiguous USA only
        with mock.patch("routeplanner.services.geocode.requests.get", side_effect=boom):
            for label in labels[:: len(labels) // 40]:  # a sample: every suggestion resolves offline when submitted
                self.assertEqual(geocode.resolve_location(label).source, "city", label)
