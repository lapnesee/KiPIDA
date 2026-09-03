"""Tests for the quantified DC advisor (advisor/dc_advisor.py).

Scope: the mesh->solve->post-process chain against Ohm's law, the physical
invariants of loss ranking and width sizing, and the two contractual promises
of the what-if path -- that it does not mutate the caller's board, and that
``Remediation.verified`` tells the truth about whether a re-simulation ran.
"""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


def _make_board_one_track(
    length_mm=10.0, width_mm=1.0, thickness_mm=0.035,
    layer_name="F.Cu", net="VCC",
):
    """A single straight track: the geometry whose resistance is exactly known."""
    from ingest.board_reader import (
        BoardBounds, CopperLayer, ParsedBoard, Segment, Stackup,
    )
    stackup = Stackup(
        layers=[CopperLayer(0, layer_name, "signal", thickness_mm)],
        total_thickness_mm=1.6,
        copper_layer_count=1,
    )
    return ParsedBoard(
        pcb_path=None,
        stackup=stackup,
        bounds=BoardBounds(x_min=0, y_min=0, x_max=length_mm, y_max=5),
        footprints=[],
        segments=[Segment(net, width_mm, layer_name, (0.0, 0.0), (length_mm, 0.0))],
        vias=[],
        zones=[],
    )


class _SolvedTrack:
    """Build, solve and expose the one-track board for a given current."""

    def __init__(self, current_a=1.0, length_mm=10.0, width_mm=1.0,
                 thickness_mm=0.035, net="VCC", rail_v=5.0):
        from advisor.dc_advisor import build_net_mesh, find_node_at
        from solver import Solver

        self.board = _make_board_one_track(
            length_mm=length_mm, width_mm=width_mm,
            thickness_mm=thickness_mm, net=net,
        )
        self.net = net
        self.mesh = build_net_mesh(self.board, net)
        self.source_node = find_node_at(self.mesh, 0.0, 0.0)
        self.load_node = find_node_at(self.mesh, length_mm, 0.0)
        self.sources = [{"node_id": self.source_node, "voltage": rail_v}]
        self.loads = [{"node_id": self.load_node, "current": current_a}]
        self.voltages = Solver().solve(self.mesh, self.sources, self.loads)


class DcAdvisorTestBase(unittest.TestCase):
    def setUp(self):
        try:
            import numpy  # noqa: F401
            import scipy  # noqa: F401
            import shapely  # noqa: F401
        except ImportError:
            self.skipTest("numpy/scipy/shapely not available")


class OhmsLawEndToEndTests(DcAdvisorTestBase):
    def test_drop_matches_analytical_resistance_times_current(self):
        """Mesh -> solve -> load_drop_v must reproduce R*I to within ppm."""
        from advisor.dc_advisor import load_drop_v
        from ingest.track_resistance import segment_resistance

        current = 1.5
        solved = _SolvedTrack(current_a=current)
        expected = segment_resistance(10.0, 1.0, 0.035) * current
        measured = load_drop_v(solved.voltages, solved.sources, solved.loads)
        self.assertAlmostEqual(measured, expected, delta=expected * 1e-6)

    def test_no_load_current_gives_no_drop(self):
        from advisor.dc_advisor import load_drop_v
        solved = _SolvedTrack(current_a=0.0)
        self.assertEqual(load_drop_v(solved.voltages, solved.sources, solved.loads), 0.0)


class BranchLossTests(DcAdvisorTestBase):
    def test_total_dissipated_power_equals_drop_times_current(self):
        """Energy conservation: sum(R*I^2) == V_drop * I on a series path."""
        from advisor.dc_advisor import load_drop_v, rank_branch_losses

        current = 2.0
        solved = _SolvedTrack(current_a=current)
        losses = rank_branch_losses(solved.mesh, solved.voltages)
        total_power = sum(item.power_w for item in losses)
        drop = load_drop_v(solved.voltages, solved.sources, solved.loads)
        self.assertAlmostEqual(total_power, drop * current, delta=total_power * 1e-6)

    def test_losses_are_sorted_worst_first(self):
        from advisor.dc_advisor import rank_branch_losses
        from ingest.board_reader import Segment
        from advisor.dc_advisor import build_net_mesh, find_node_at
        from solver import Solver

        # Two series segments of very different width -> very different loss.
        board = _make_board_one_track(length_mm=10.0, width_mm=1.0)
        board.segments = [
            Segment("VCC", 1.0, "F.Cu", (0.0, 0.0), (5.0, 0.0)),
            Segment("VCC", 0.1, "F.Cu", (5.0, 0.0), (10.0, 0.0)),
        ]
        mesh = build_net_mesh(board, "VCC")
        sources = [{"node_id": find_node_at(mesh, 0.0, 0.0), "voltage": 5.0}]
        loads = [{"node_id": find_node_at(mesh, 10.0, 0.0), "current": 1.0}]
        losses = rank_branch_losses(mesh, Solver().solve(mesh, sources, loads))

        self.assertEqual(len(losses), 2)
        self.assertGreaterEqual(losses[0].power_w, losses[1].power_w)
        # The narrow segment is 10x more resistive, so it must dominate.
        self.assertGreater(losses[0].resistance_ohm, losses[1].resistance_ohm)

    def test_dominant_path_share_returns_smallest_covering_prefix(self):
        from advisor.dc_advisor import BranchLoss, dominant_path_share

        losses = [
            BranchLoss(0, 1.0, 1.0, 70.0, "seg:F.Cu", 0, 1),
            BranchLoss(1, 1.0, 1.0, 20.0, "seg:F.Cu", 1, 2),
            BranchLoss(2, 1.0, 1.0, 10.0, "seg:F.Cu", 2, 3),
        ]
        # 70 alone is < 80%; 70+20 = 90% >= 80% -> two branches.
        self.assertEqual(len(dominant_path_share(losses, fraction=0.8)), 2)
        self.assertEqual(len(dominant_path_share(losses, fraction=0.5)), 1)


class WidthSizingTests(DcAdvisorTestBase):
    def test_halving_the_target_drop_doubles_the_required_width(self):
        """R proportional to 1/w, verified analytically rather than by fixture."""
        from advisor.dc_advisor import required_width_for_target_drop

        width = required_width_for_target_drop(
            current_width_mm=0.25, actual_drop_v=0.30, target_drop_v=0.15,
        )
        self.assertAlmostEqual(width, 0.50, places=12)

    def test_target_at_or_above_actual_drop_raises(self):
        from advisor.dc_advisor import required_width_for_target_drop
        with self.assertRaises(ValueError):
            required_width_for_target_drop(0.25, 0.10, 0.10)


class WhatIfTests(DcAdvisorTestBase):
    def _widen_twofold(self, current_a=1.0):
        from advisor.dc_advisor import simulate_width_change
        solved = _SolvedTrack(current_a=current_a)
        outcome = simulate_width_change(
            solved.board, solved.net, "", solved.sources, solved.loads,
            segment_predicate=lambda seg: seg.net_name == "VCC",
            new_width_mm=2.0,
        )
        return solved, outcome

    def test_doubling_width_halves_the_resimulated_drop(self):
        """The central promise: re-simulation agrees with physics, not itself."""
        solved, outcome = self._widen_twofold()
        self.assertTrue(outcome.converged)
        self.assertAlmostEqual(
            outcome.resimulated_drop_v, outcome.baseline_drop_v / 2.0,
            delta=outcome.baseline_drop_v * 0.01,
        )

    def test_first_order_prediction_is_accurate_on_a_linear_path(self):
        _solved, outcome = self._widen_twofold()
        self.assertLess(outcome.prediction_error_pct, 1.0)

    def test_caller_board_is_not_mutated(self):
        solved, _outcome = self._widen_twofold()
        self.assertEqual([seg.width_mm for seg in solved.board.segments], [1.0])


class RemediationTests(DcAdvisorTestBase):
    def _build(self, verify):
        from advisor.dc_advisor import build_dc_remediations
        solved = _SolvedTrack(current_a=2.0)
        baseline = solved.voltages[solved.load_node]
        del baseline  # only solved for its side effects; target is set below
        return build_dc_remediations(
            solved.board, solved.net, "", solved.sources, solved.loads,
            target_drop_v=0.005, verify=verify,
        )

    def test_verified_flag_and_wording_match_whether_a_resolve_ran(self):
        verified = self._build(verify=True)
        estimated = self._build(verify=False)

        self.assertTrue(verified)
        self.assertTrue(verified[0].verified)
        self.assertIn("re-simulated", verified[0].predicted_gain)
        self.assertEqual(verified[0].action, "WIDEN_TRACK")
        self.assertEqual(verified[0].unit, "mm")
        self.assertGreater(verified[0].proposed_value, verified[0].current_value)

        self.assertTrue(estimated)
        self.assertFalse(estimated[0].verified)
        self.assertIn("estimate", estimated[0].predicted_gain)

    def test_drop_already_within_target_produces_no_advice(self):
        from advisor.dc_advisor import build_dc_remediations
        solved = _SolvedTrack(current_a=1.0)
        self.assertEqual(
            build_dc_remediations(
                solved.board, solved.net, "", solved.sources, solved.loads,
                target_drop_v=10.0, verify=False,
            ),
            [],
        )

    def test_net_without_copper_degrades_cleanly(self):
        from advisor.dc_advisor import build_dc_remediations
        board = _make_board_one_track()
        self.assertEqual(
            build_dc_remediations(
                board, "NO_SUCH_NET", "", [], [],
                target_drop_v=0.01, verify=True,
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
