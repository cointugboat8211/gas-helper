"""Tests for the decision rules. Run: python -m unittest"""
import unittest
from datetime import date, timedelta

import gas

CFG = gas.load_config()
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


if __name__ == "__main__":
    unittest.main()
