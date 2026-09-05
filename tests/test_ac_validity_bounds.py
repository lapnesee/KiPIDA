"""The sweep must say where its numbers stop deserving equal confidence.

Two caveats the metrics alone cannot show, both seen on the real
p02_alimentation run: the worst impedance landed exactly on the last swept
point (100 MHz), and every point near it comes from a lumped quasi-static
mesh that omits plane resonances.
"""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


class QuasiStaticLimitTests(unittest.TestCase):
    def setUp(self):
        try:
            import numpy, shapely, matplotlib  # noqa: F401
        except ImportError:
            self.skipTest("ac_model dependencies not available")

    def test_limit_matches_the_lambda_over_ten_formula(self):
        import math

        from ac_model import LIGHT_SPEED_M_S, quasi_static_limit_hz

        span_mm, epsilon = 80.0, 4.4
        expected = LIGHT_SPEED_M_S / (10.0 * (span_mm / 1000.0) * math.sqrt(epsilon))
        self.assertAlmostEqual(quasi_static_limit_hz(span_mm, epsilon), expected, places=6)

    def test_a_smaller_board_stays_valid_higher(self):
        from ac_model import quasi_static_limit_hz

        self.assertGreater(
            quasi_static_limit_hz(20.0, 4.4), quasi_static_limit_hz(80.0, 4.4),
        )

    def test_unusable_span_reports_no_bound(self):
        from ac_model import quasi_static_limit_hz

        self.assertEqual(quasi_static_limit_hz(0.0, 4.4), 0.0)
        self.assertEqual(quasi_static_limit_hz(None, 4.4), 0.0)


class _Sweep:
    """Minimal stand-in for ImpedanceSweepResult's read fields."""

    def __init__(self, *, worst_at_edge, frequencies, limit_hz=0.0, beyond=0):
        self.worst_at_sweep_edge = worst_at_edge
        self.frequencies_hz = frequencies
        self.quasi_static_limit_hz = limit_hz
        self.points_beyond_quasi_static = beyond
        self.excluded_ports = []


class ValidityLimitationTests(unittest.TestCase):
    def test_worst_on_the_last_point_is_called_a_lower_bound(self):
        from analysis_adapters import _ac_validity_limitations

        notes = _ac_validity_limitations(
            _Sweep(worst_at_edge=True, frequencies=[1e3, 1e6, 1e8])
        )
        self.assertTrue(any("lower bound" in note for note in notes))
        self.assertTrue(any("1e+08" in note for note in notes))

    def test_worst_inside_the_window_says_nothing(self):
        from analysis_adapters import _ac_validity_limitations

        notes = _ac_validity_limitations(
            _Sweep(worst_at_edge=False, frequencies=[1e3, 1e6, 1e8])
        )
        self.assertEqual(notes, [])

    def test_points_above_the_limit_are_flagged_with_a_count(self):
        from analysis_adapters import _ac_validity_limitations

        notes = _ac_validity_limitations(_Sweep(
            worst_at_edge=False, frequencies=[1e6, 1e8],
            limit_hz=180e6, beyond=3,
        ))
        self.assertTrue(any("3 swept point(s)" in note for note in notes))
        self.assertTrue(any("180.0 MHz" in note for note in notes))


class SweepSummaryTests(unittest.TestCase):
    def setUp(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("NumPy not available")

    def test_summary_counts_points_past_the_limit(self):
        # The real case: a 100 MHz sweep on an 80 mm board, whose limit is
        # about 180 MHz, so nothing is beyond it -- but the worst case still
        # sits on the closing point.
        from ac_solver import ACSolver
        from models import ACAnalysisSettings

        class _Net:
            node_count = 10
            requested_grid_size_mm = 0.5
            effective_grid_size_mm = 0.5
            quasi_static_limit_hz = 180e6

        frequencies = [1e3, 1e6, 1e8]
        impedances = [0.01, 0.05, 0.36]          # still climbing at the end
        result = ACSolver._summarize(
            frequencies, impedances, ACAnalysisSettings(), _Net(),
        )
        self.assertTrue(result.worst_at_sweep_edge)
        self.assertEqual(result.points_beyond_quasi_static, 0)
        self.assertAlmostEqual(result.quasi_static_limit_hz, 180e6)


if __name__ == "__main__":
    unittest.main()
