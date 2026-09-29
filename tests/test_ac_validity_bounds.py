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

    def test_rising_edge_extrapolates_crossing_below_quasi_static_limit(self):
        # AC-1 (docs/audit-2026-09-23.md, P02 Rev E /VBUS_SYS, 2026-09-22
        # campaign): worst |Z| 0.2947 ohm landed on the last swept point
        # (100 MHz), still rising, target 0.39226 ohm. Extrapolating the
        # rise linearly crosses the target near 0.39226/0.2947 x 100 MHz
        # = 133 MHz, below the ~140 MHz quasi-static limit for a ~100 mm
        # span -- so the PASS the swept points show is not established.
        from ac_solver import ACSolver
        from models import ACAnalysisSettings

        class _Net:
            node_count = 10
            requested_grid_size_mm = 0.5
            effective_grid_size_mm = 0.5
            quasi_static_limit_hz = 140e6

        frequencies = [1e3, 50e6, 100e6]
        impedances = [0.001, 0.20, 0.2947]
        result = ACSolver._summarize(
            frequencies, impedances,
            ACAnalysisSettings(target_impedance_ohm=0.39226), _Net(),
        )
        self.assertTrue(result.meets_target)
        self.assertTrue(result.worst_at_sweep_edge)
        self.assertAlmostEqual(result.extrapolated_crossing_hz, 133.1e6, delta=0.5e6)
        self.assertLess(result.extrapolated_crossing_hz, result.quasi_static_limit_hz)


class TransientBandwidthTests(unittest.TestCase):
    """A target holds flat over the step's bandwidth only (ac_model.transient_bandwidth)."""

    def setUp(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("NumPy not available")

    def test_the_converter_edge_bounds_the_flat_target(self):
        # P02 Rev E, 2026-09-29 18:47: /VBUS_SYS's 0.0889 ohm target comes
        # from U4's 4.5 A chopped step; its buck leg Q10/Q11 switches
        # /SYS_5V_SW1, whose EMC source puts a 10 ns edge on it. The sweep
        # failed only from 46 MHz, reaching 0.2204 ohm at 100 MHz.
        from types import SimpleNamespace as NS
        from ac_model import transient_bandwidth
        from ac_solver import ACSolver
        from models import ACAnalysisSettings, EMCSignalSource

        u4 = NS(
            name="U4 buck-boost 5 V machine", reg_type="SWITCHING", input_ref_des="Q10",
            output_ref_des="U4", thermal_ref_des="U4", output_rail_name="/SYS_5V_PRE_SENSE",
            loss_model={"controller_ref_des": "Q10", "mosfets": [{"ref_des": "Q10"}, {"ref_des": "Q11"}]},
        )
        vbus = NS(net_name="/VBUS_SYS", loads=[], child_regulators=[u4])
        pre = NS(net_name="/SYS_5V_PRE_SENSE", loads=[NS(total_current=4.5)], child_regulators=[])
        source = EMCSignalSource(
            "U4 SW1 buck leg (Q10/Q11/L1)", "/SYS_5V_SW1", kind="SWITCHING", rise_time_ns=10.0,
        )
        bandwidth, provenance = transient_bandwidth(
            vbus, ACAnalysisSettings(), all_rails=[vbus, pre], emc_sources=[source],
            pad_nets_by_ref={"Q10": {"/VBUS_SYS", "/SYS_5V_SW1"}, "Q11": {"GND", "/SYS_5V_SW1"}},
        )
        self.assertAlmostEqual(bandwidth, 35e6)
        self.assertIn("10 ns edge", provenance)

        class _Net:
            node_count = 10
            requested_grid_size_mm = 0.5
            effective_grid_size_mm = 0.5
            quasi_static_limit_hz = 140e6

        frequencies, impedances = [1e6, 46.4e6, 100e6], [0.005, 0.095, 0.2204]
        flat = ACSolver._summarize(
            frequencies, impedances, ACAnalysisSettings(target_impedance_ohm=0.0888889), _Net(),
        )
        banded = ACSolver._summarize(
            frequencies, impedances,
            ACAnalysisSettings(target_impedance_ohm=0.0888889, transient_bandwidth_hz=bandwidth),
            _Net(),
        )
        self.assertFalse(flat.meets_target)
        # 0.2204 ohm at 100 MHz against 0.0889 x 100/35 = 0.254 ohm allowed.
        self.assertTrue(banded.meets_target)

    def test_the_converter_target_starts_at_its_switching_frequency(self):
        # P02 Rev E: /VBUS_SYS's 0.0889 ohm target, U4's 4.5 A chopped step,
        # applied from 1 kHz, three decades below U4's 400 kHz. Its USB-C
        # source was measured to put 362 mOhm at 21 kHz
        # (scratchpad/audit/ac/source_lumped.py): a failure against the step
        # that has no content there, a pass against the 2.04 A average's.
        from types import SimpleNamespace as NS
        from ac_model import converter_band_floor
        from ac_solver import ACSolver
        from models import ACAnalysisSettings

        u4 = NS(
            name="U4 buck-boost 5 V machine", reg_type="SWITCHING", efficiency=1.0,
            output_rail_name="/SYS_5V_PRE_SENSE",
            loss_model={"switching_frequency_hz": {"value": 400000.0}},
        )
        vbus = NS(net_name="/VBUS_SYS", nominal_voltage=20.0, loads=[], child_regulators=[u4])
        pre = NS(net_name="/SYS_5V_PRE_SENSE", nominal_voltage=9.0667, child_regulators=[],
                 loads=[NS(total_current=4.5)])
        floor_hz, below, _provenance = converter_band_floor(
            vbus, ACAnalysisSettings(), all_rails=[vbus, pre],
        )
        self.assertEqual(floor_hz, 400e3)
        self.assertAlmostEqual(below, 0.392, places=3)  # 20 V x 2 % / (50 % of 2.04 A)

        class _Net:
            node_count = 10
            requested_grid_size_mm = 0.5
            effective_grid_size_mm = 0.5
            quasi_static_limit_hz = 140e6

        frequencies, impedances = [1e3, 21e3, 1e6], [0.1, 0.362, 0.02]
        flat = ACSolver._summarize(
            frequencies, impedances, ACAnalysisSettings(target_impedance_ohm=0.0888889), _Net(),
        )
        floored = ACSolver._summarize(
            frequencies, impedances,
            ACAnalysisSettings(
                target_impedance_ohm=0.0888889, target_floor_hz=floor_hz,
                target_below_floor_ohm=below,
            ),
            _Net(),
        )
        self.assertFalse(flat.meets_target)
        self.assertTrue(floored.meets_target)


if __name__ == "__main__":
    unittest.main()
