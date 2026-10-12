"""Tests for the decision rules. Run: python -m unittest"""
import unittest
from datetime import date, timedelta

import gas
import trips

CFG = {**gas.load_config(), "margin": 0.65, "tank_gallons": 24}
TODAY = date(2026, 10, 9)  # a Friday


def series(prices, end=TODAY):
    """Daily prices ending on `end`."""
    start = end - timedelta(days=len(prices) - 1)
    return {start + timedelta(days=i): p for i, p in enumerate(prices)}


def flat_wholesale(price=3.00):
    return series([price] * 10)


class Rules(unittest.TestCase):
    def call(self, retail, wholesale):
        return gas.decide(retail, wholesale, CFG, TODAY)[0]

    def test_retail_spike_means_wait(self):
        retail = series([3.50, 3.50, 3.50, 3.52, 3.70])
        self.assertEqual(self.call(retail, flat_wholesale()), gas.WAIT)

    def test_long_slide_near_fair_means_fill(self):
        retail = series([3.90, 3.86, 3.83, 3.80, 3.78, 3.75, 3.73, 3.70])  # fair = 3.65
        self.assertEqual(self.call(retail, flat_wholesale()), gas.FILL)

    def test_long_slide_far_from_fair_is_half(self):
        retail = series([4.40, 4.36, 4.33, 4.30, 4.28, 4.25, 4.23, 4.20])
        self.assertEqual(self.call(retail, flat_wholesale()), gas.HALF)

    def test_slide_interrupted_by_a_rise_is_half(self):
        retail = series([3.90, 3.86, 3.83, 3.87, 3.78, 3.75, 3.73, 3.70])
        self.assertEqual(self.call(retail, flat_wholesale()), gas.HALF)

    def test_wholesale_rise_means_fill(self):
        wholesale = series([3.00] * 7 + [3.04, 3.08, 3.12])
        self.assertEqual(self.call(series([4.00] * 5), wholesale), gas.FILL)

    def test_stale_wholesale_rise_is_ignored(self):
        wholesale = series([3.00] * 7 + [3.04, 3.08, 3.12], end=TODAY - timedelta(days=9))
        self.assertEqual(self.call(series([4.00] * 5), wholesale), gas.HALF)

    def test_retail_spike_outranks_wholesale_rise(self):
        wholesale = series([3.00] * 7 + [3.04, 3.08, 3.12])
        retail = series([3.50, 3.50, 3.50, 3.52, 3.70])
        self.assertEqual(self.call(retail, wholesale), gas.WAIT)

    def test_short_history_is_half(self):
        call, reason = gas.decide(series([3.80, 3.79]), flat_wholesale(), CFG, TODAY)
        self.assertEqual(call, gas.HALF)
        self.assertIn("days of retail history", reason)

    def test_fresh_rbob_rise_means_fill(self):
        rbob = series([3.00, 3.04, 3.08, 3.12], end=TODAY - timedelta(days=1))
        call = gas.decide(series([4.00] * 5), flat_wholesale(), CFG, TODAY, rbob)[0]
        self.assertEqual(call, gas.FILL)


class Tank(unittest.TestCase):
    def test_no_truck_reading_suggests_half_a_tank(self):
        self.assertEqual(gas.tank_advice(gas.HALF, "Meh.", None, CFG), ("Meh. Buy about 12 gal.", False))

    def test_full_tank_is_quiet(self):
        reason, quiet = gas.tank_advice(gas.FILL, "Spike coming.", 90, CFG)
        self.assertTrue(quiet)
        self.assertIn("90% full", reason)

    def test_fill_says_how_much_room(self):
        reason, quiet = gas.tank_advice(gas.FILL, "Spike coming.", 25, CFG)
        self.assertFalse(quiet)
        self.assertIn("about 18 gal to fill", reason)

    def test_half_tank_never_overfills(self):
        self.assertIn("buy about 7 gal", gas.tank_advice(gas.HALF, "Meh.", 70, CFG)[0])

    def test_wait_on_empty_says_buy_a_little(self):
        self.assertIn("just enough to get by", gas.tank_advice(gas.WAIT, "Spiked.", 10, CFG)[0])


class RollingMargin(unittest.TestCase):
    def test_uses_fixed_margin_until_enough_history(self):
        margin, rolling = gas.current_margin(series([4.00] * 5), flat_wholesale(), CFG, TODAY)
        self.assertEqual((margin, rolling), (CFG["margin"], False))

    def test_tracks_the_low_end_of_the_recent_gap(self):
        # Gap over wholesale cycles between $0.90 and $1.30 for 40 days.
        retail = series([3.90 + 0.40 * (i % 5) / 4 for i in range(40)])
        margin, rolling = gas.current_margin(retail, series([3.00] * 40), CFG, TODAY)
        self.assertTrue(rolling)
        self.assertAlmostEqual(margin, 0.98, places=2)

    def test_fixed_mode_ignores_history(self):
        cfg = {**CFG, "margin_mode": "fixed"}
        margin, rolling = gas.current_margin(series([4.00] * 40), series([3.00] * 40), cfg, TODAY)
        self.assertEqual((margin, rolling), (CFG["margin"], False))


class FuelReading(unittest.TestCase):
    def test_old_readings_are_ignored(self):
        import json, tempfile
        from datetime import datetime, timezone
        from pathlib import Path
        read_at = datetime(2026, 10, 9, 10, 40, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            gas.FUEL_PATH, saved = Path(tmp) / "fuel.json", gas.FUEL_PATH
            try:
                self.assertIsNone(gas.read_fuel_percent())
                gas.FUEL_PATH.write_text(json.dumps({"percent": 62.0, "read_at": read_at.isoformat()}))
                self.assertEqual(gas.read_fuel_percent(read_at + timedelta(hours=1)), 62.0)
                self.assertIsNone(gas.read_fuel_percent(read_at + timedelta(hours=30)))
            finally:
                gas.FUEL_PATH = saved


class Scorecard(unittest.TestCase):
    def test_judges_calls_once_enough_days_have_passed(self):
        import sqlite3
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE calls (date TEXT, call TEXT, retail REAL, area TEXT)")
        area = gas.retail_area(CFG)
        d = lambda n: (TODAY - timedelta(days=n))
        retail = series([4.50, 4.45, 4.40, 4.38, 4.36, 4.35, 4.34, 4.33, 4.60, 4.58])
        db.executemany("INSERT INTO calls VALUES (?, ?, ?, ?)", [
            (d(9).isoformat(), gas.WAIT, 4.50, area),   # fell 15c -> right
            (d(6).isoformat(), gas.WAIT, 4.38, area),   # fell 5c -> right
            (d(5).isoformat(), gas.FILL, 4.36, area),   # rose 24c -> right
            (d(2).isoformat(), gas.FILL, 4.33, area),   # too recent to judge
            (d(8).isoformat(), gas.FILL, 4.45, "MI"),   # other area, ignored
        ])
        outcomes, summary = gas.score_calls(db, retail, CFG)
        self.assertEqual(summary["wait"], [2, 2])
        self.assertEqual(summary["fill"], [1, 1])
        self.assertNotIn(d(2), outcomes)
        self.assertEqual(outcomes[d(9)]["text"], "Fell 15¢ within 5 days")


class Trips(unittest.TestCase):
    CFG = {"trip_mpg": 20, "tank_gallons": 23, "trip_reserve_gallons": 3, "trip_stop_cost": 2.0}  # 400 mi range

    def stops(self, *pairs):
        return [{"name": "home" if mile == 0 else f"stop{mile}", "mile": mile, "price": price}
                for mile, price in pairs]

    def test_short_trip_buys_only_what_it_needs_at_home(self):
        plan = trips.fuel_plan(self.stops((0, 4.00), (50, 4.50)), 100, self.CFG)
        self.assertEqual([(b["name"], b["gallons"]) for b in plan], [("home", 5.0)])

    def test_waits_for_much_cheaper_gas_ahead(self):
        plan = trips.fuel_plan(self.stops((0, 4.80), (100, 4.00)), 300, self.CFG)
        self.assertEqual([(b["name"], b["gallons"]) for b in plan], [("home", 5.0), ("stop100", 10.0)])

    def test_does_not_stop_to_save_pennies(self):
        plan = trips.fuel_plan(self.stops((0, 4.02), (100, 4.00)), 300, self.CFG)
        self.assertEqual([b["name"] for b in plan], ["home"])

    def test_long_trip_fills_then_tops_up_at_the_cheaper_stop(self):
        plan = trips.fuel_plan(self.stops((0, 4.50), (300, 4.90), (380, 4.20)), 600, self.CFG)
        self.assertEqual([(b["name"], b["fill"]) for b in plan], [("home", False), ("stop380", False)])
        self.assertAlmostEqual(sum(b["gallons"] for b in plan), 30.0, places=1)

    def test_gap_longer_than_the_tank_has_no_plan(self):
        self.assertIsNone(trips.fuel_plan(self.stops((0, 4.00)), 900, self.CFG))

    def test_county_names_match_across_sources(self):
        self.assertEqual(trips.county_key("St. Clair County"), trips.county_key("Saint Clair"))

    def test_route_samples_become_county_legs(self):
        samples = [(0, ("MI", "Barry")), (8, ("MI", "Barry")), (16, ("MI", "Ionia")),
                   (24, None), (32, ("MI", "Ionia")), (40, ("MI", "Montcalm"))]
        legs = trips.legs_from_samples(samples, 45)
        self.assertEqual([(l["county"], l["start"], l["end"]) for l in legs],
                         [("Barry", 0.0, 12.0), ("Ionia", 12.0, 36.0), ("Montcalm", 36.0, 45)])


if __name__ == "__main__":
    unittest.main()
