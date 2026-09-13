"""A raster result states the error its narrowest track carries (audit R2).

The bounds come from validation/dc_raster_fidelity.py; one row is re-measured
here so the table cannot drift from the measurement it quotes.
"""

import os
import sys
import unittest
from types import SimpleNamespace

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from raster_fidelity import (  # noqa: E402
    TRACK_RESISTANCE_RATIO, is_oblique, ratio_bounds, track_limitation, width_in_steps,
)


class Bounds(unittest.TestCase):
    def test_a_measured_width_quotes_its_own_row(self):
        straight, diagonal = ratio_bounds(2.5)
        self.assertEqual((straight, diagonal), ((0.83, 1.25), (1.18, 1.77)))

    def test_between_two_measured_widths_the_envelope_of_both_is_quoted(self):
        straight, diagonal = ratio_bounds(1.8)
        self.assertEqual(straight, (0.67, 1.50))
        self.assertEqual(diagonal, (1.06, 2.12))

    def test_decimal_widths_do_not_slip_into_the_row_beneath(self):
        # 0.15 / 0.1 is 1.4999999999999998 in floating point.
        self.assertEqual(width_in_steps(0.15, 0.1), 1.5)
        self.assertIn("x0.75 to x1.50", track_limitation(0.15, 0.1, 0))

    def test_at_one_step_a_diagonal_track_may_carry_nothing(self):
        self.assertIsNone(ratio_bounds(1.0)[1])
        self.assertIn("carrying no current", track_limitation(0.1, 0.1, 2))

    def test_below_one_step_nothing_is_claimed(self):
        text = track_limitation(0.08, 0.1, 0)
        self.assertIn("unbounded", text)
        self.assertNotIn("x0.", text)

    def test_wide_straight_tracks_need_no_statement_but_oblique_ones_do(self):
        self.assertIsNone(track_limitation(0.5, 0.1, 0))
        self.assertIn("x1.18", track_limitation(0.5, 0.1, 3))
        self.assertIsNone(track_limitation(1.2, 0.1, 3))

    def test_oblique_means_off_both_grid_axes(self):
        self.assertFalse(is_oblique((0, 0), (5, 0)))
        self.assertFalse(is_oblique((0, 0), (0, -5)))
        self.assertTrue(is_oblique((0, 0), (5, 5)))
        self.assertTrue(is_oblique((0, 0), (5, 1)))

    def test_the_table_is_the_measurement(self):
        try:
            import matplotlib  # noqa: F401
            import scipy  # noqa: F401
        except ImportError:
            self.skipTest("the raster mesher needs matplotlib and scipy")
        sys.path.insert(0, os.path.join(_root, "validation"))
        from dc_raster_fidelity import ALIGNMENTS, mesh_resistance
        from physical_constants import COPPER_RESISTIVITY_OHM_MM
        from dc_raster_fidelity import THICKNESS_MM

        width, step = 0.25, 0.1
        row = next(item for item in TRACK_RESISTANCE_RATIO if item[0] == 2.5)
        for angle, expected in ((0.0, row[1]), (45.0, row[2])):
            ratios = []
            for alignment in ALIGNMENTS:
                resistance, gap = mesh_resistance(width, step, alignment, angle)
                ratios.append(resistance / (COPPER_RESISTIVITY_OHM_MM * gap / (width * THICKNESS_MM)))
            self.assertAlmostEqual(min(ratios), expected[0], delta=0.006)
            self.assertAlmostEqual(max(ratios), expected[1], delta=0.006)


class RasterResultsStateTheirError(unittest.TestCase):
    def test_a_raster_dc_rail_publishes_its_track_error(self):
        from analysis_adapters import adapt_dc_result

        result = adapt_dc_result({"+3V3": {
            "stats": (3.29, 3.3, 0.01), "grid_size_mm": 0.1, "engine": "raster",
            "track_summary": {"track_count": 12, "finest_width_mm": 0.15, "oblique_count": 4},
        }}, 5.0)
        metrics = {metric.key: metric for metric in result.metrics}
        self.assertEqual(metrics["rail_1_narrowest_track_steps"].value, 1.5)
        self.assertEqual(metrics["rail_1_narrowest_track_steps"].status, "WARN")
        self.assertEqual(metrics["rail_1_oblique_tracks"].value, 4)
        self.assertTrue(any("x0.75 to x1.50" in item and "4 oblique" in item for item in result.limitations))

    def test_a_hybrid_dc_rail_has_no_raster_error_to_state(self):
        from analysis_adapters import adapt_dc_result

        result = adapt_dc_result({"+3V3": {
            "stats": (3.29, 3.3, 0.01), "grid_size_mm": 0.1, "engine": "hybrid",
            "track_summary": {"track_count": 12, "finest_width_mm": 0.15, "oblique_count": 4},
        }}, 5.0)
        self.assertFalse(any("narrowest_track" in metric.key for metric in result.metrics))
        self.assertFalse(any("grid alignment" in item for item in result.limitations))

    def test_the_ac_sweep_states_its_track_error(self):
        from analysis_adapters import adapt_ac_result

        final = SimpleNamespace(
            target_impedance_ohm=0.1, worst_impedance_ohm=0.05, meets_target=True,
            worst_frequency_hz=1e6, frequencies_hz=[1e5, 1e6], impedance_ohm=[0.01, 0.05],
            effective_grid_size_mm=0.5, mesh_node_count=100,
            track_summary={"track_count": 30, "finest_width_mm": 0.2, "oblique_count": 7},
        )
        result = adapt_ac_result(final)
        self.assertTrue(any(
            item.startswith("The AC mesh ran at 0.5 mm") and "0.4 grid steps" in item
            for item in result.limitations
        ))
        metrics = {metric.key: metric for metric in result.metrics}
        self.assertEqual(metrics["narrowest_track_steps"].value, 0.4)

    def test_the_raster_engine_carries_the_captured_summary(self):
        from application.dc_controller import DCSolverEngine

        request = SimpleNamespace(track_summary_by_rail={
            "VCC": {"track_count": 3, "finest_width_mm": 0.2, "oblique_count": 1},
        })
        rail = SimpleNamespace(net_name="VCC")
        self.assertEqual(
            DCSolverEngine()._rail_extras(rail, request, None)["track_summary"]["finest_width_mm"], 0.2,
        )


class LiveTrackSummary(unittest.TestCase):
    def test_arcs_and_off_axis_tracks_count_as_oblique(self):
        from extractor import GeometryExtractor

        def track(start, end, width, mid=None):
            item = SimpleNamespace(
                start=SimpleNamespace(x=start[0], y=start[1]), end=SimpleNamespace(x=end[0], y=end[1]),
                width=width, net=SimpleNamespace(name="VCC"), layer=0,
            )
            if mid is not None:
                item.mid = SimpleNamespace(x=mid[0], y=mid[1])
            return item

        tracks = [
            track((0, 0), (10_000_000, 0), 300_000),
            track((0, 0), (5_000_000, 5_000_000), 200_000),
            track((0, 0), (2_000_000, 2_000_000), 250_000, mid=(1_500_000, 500_000)),
        ]
        extractor = GeometryExtractor(board=None)
        extractor._get_items_for_net = lambda attribute, net: tracks if attribute == "tracks" else []
        self.assertEqual(
            extractor.get_track_summary("VCC"),
            {"track_count": 3, "finest_width_mm": 0.2, "oblique_count": 2},
        )


if __name__ == "__main__":
    unittest.main()
