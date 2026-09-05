"""The stitching-via and copper-weight actions are re-simulated, not asserted.

Both used to be first-order estimates that said so, on the grounds that neither
had a re-simulable form: "there is no equivalent for 'add a via near this one'
that does not require inventing a position", and no equivalent for "the same
pour, thicker". Both objections have answers. The position that invents nothing
is the one the via already occupies, and a thicker pour is a stackup with one
layer re-thicknessed -- which the mesher already reads.

Saying so was weaker than being checked, and the check earns its keep: the
first-order sizing over-promises, by more the more it is asked for.
"""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

_POUR = [(2, 2), (8, 2), (8, 8), (2, 8)]
_STEP = 0.5


def _board(copper_mm=0.14, via_count=2, foreign_vias=0, drill_mm=0.10):
    """Two pours joined by a via field.

    A thin barrel puts the loss in the vias; a fat one leaves it in the pour.
    The advisor picks its action from where the loss sits, so the two cases
    need different copper, not different assertions.
    """
    from ingest.board_reader import (
        BoardBounds, CopperLayer, ParsedBoard, Stackup, Via, Zone,
    )
    stackup = Stackup(
        layers=[
            CopperLayer(0, "F.Cu", "signal", copper_mm),
            CopperLayer(2, "B.Cu", "signal", copper_mm),
        ],
        total_thickness_mm=1.6, copper_layer_count=2,
    )
    vias = [
        Via("VCC", (5.0 + 0.5 * i, 5.0), drill_mm + 0.05, drill_mm,
            ["F.Cu", "B.Cu"])
        for i in range(via_count)
    ] + [
        Via("GND", (3.0 + 0.5 * i, 3.0), 0.15, 0.10, ["F.Cu", "B.Cu"])
        for i in range(foreign_vias)
    ]
    return ParsedBoard(
        pcb_path=None, stackup=stackup, bounds=BoardBounds(0, 0, 10, 10),
        footprints=[], segments=[], vias=vias,
        zones=[Zone("VCC", "F.Cu", filled_polygon=_POUR),
               Zone("VCC", "B.Cu", filled_polygon=_POUR)],
    )


def _terminals(board):
    """Source on one pour's far corner, load on the other's, so current vias."""
    from advisor.dc_advisor import build_net_mesh, find_node_at

    mesh = build_net_mesh(board, "VCC", "", grid_step_mm=_STEP)
    return (
        [{"node_id": find_node_at(mesh, 2.0, 2.0, 0), "voltage": 5.0}],
        [{"node_id": find_node_at(mesh, 8.0, 8.0, 2), "current": 3.0}],
    )


class WhatIfActionsAreResimulated(unittest.TestCase):
    def setUp(self):
        try:
            import numpy, scipy, shapely  # noqa: F401
        except ImportError:
            self.skipTest("numpy/scipy/shapely not available")

    def test_the_stitching_via_action_is_verified_only_when_asked(self):
        from advisor.dc_advisor import build_dc_remediations

        board = _board()
        sources, loads = _terminals(board)
        kwargs = dict(target_drop_v=0.004, grid_step_mm=_STEP)

        fast = build_dc_remediations(board, "VCC", "", sources, loads,
                                     verify=False, **kwargs)
        checked = build_dc_remediations(board, "VCC", "", sources, loads,
                                        verify=True, **kwargs)

        self.assertEqual([r.action for r in fast], ["ADD_STITCHING_VIAS"])
        self.assertEqual([r.action for r in checked], ["ADD_STITCHING_VIAS"])
        self.assertFalse(fast[0].verified)
        self.assertIn("not re-simulated", fast[0].predicted_gain)
        self.assertTrue(checked[0].verified)
        self.assertIn("re-simulated", checked[0].predicted_gain)
        # The fast path must still size the same fix; only its evidence differs.
        self.assertEqual(fast[0].proposed_value, checked[0].proposed_value)

    def test_the_copper_weight_action_is_verified_only_when_asked(self):
        from advisor.dc_advisor import build_dc_remediations

        # Fat barrels and thin copper: the loss is in the pour, so this is
        # the action the advisor reaches for.
        board = _board(copper_mm=0.035, via_count=8, drill_mm=0.8)
        sources, loads = _terminals(board)
        kwargs = dict(target_drop_v=0.006, grid_step_mm=_STEP)

        fast = build_dc_remediations(board, "VCC", "", sources, loads,
                                     verify=False, **kwargs)
        checked = build_dc_remediations(board, "VCC", "", sources, loads,
                                        verify=True, **kwargs)

        self.assertEqual(fast[0].action, "INCREASE_COPPER_WEIGHT")
        self.assertFalse(fast[0].verified)
        self.assertEqual(checked[0].action, "INCREASE_COPPER_WEIGHT")
        self.assertTrue(checked[0].verified)
        # The undercut caveat survives verification: it is not what was checked.
        self.assertIn("undercut", checked[0].predicted_gain)

    def test_the_first_order_sizing_over_promises(self):
        # This is what the item was for. Both sizings attribute the drop in
        # proportion to dissipated power and scale that share down, which
        # assumes the rest of the network holds still. It does not: the pour
        # keeps its spreading resistance whatever the barrels do. So the
        # re-simulated drop lands *above* the promise, and further above it the
        # more the estimate is asked to deliver.
        from advisor.dc_advisor import simulate_via_addition

        board = _board()
        sources, loads = _terminals(board)
        modest = simulate_via_addition(
            board, "VCC", "", sources, loads, "F.Cu-B.Cu", 4,
            predicted_drop_v=0.003, grid_step_mm=_STEP,
        )
        aggressive = simulate_via_addition(
            board, "VCC", "", sources, loads, "F.Cu-B.Cu", 16,
            predicted_drop_v=0.0015, grid_step_mm=_STEP,
        )

        self.assertTrue(modest.converged and aggressive.converged)
        self.assertGreater(modest.resimulated_drop_v, modest.predicted_drop_v)
        self.assertGreater(aggressive.resimulated_drop_v,
                           aggressive.predicted_drop_v)
        self.assertGreater(aggressive.prediction_error_pct,
                           modest.prediction_error_pct)
        # Still worth proposing: more vias do lower the drop, just not to where
        # the estimate said they would.
        self.assertLess(aggressive.resimulated_drop_v, modest.resimulated_drop_v)

    def test_the_caller_s_board_survives_both_what_ifs(self):
        # simulate_width_change promises the caller's ParsedBoard is identical
        # afterwards. These two must keep that promise, and a via of another
        # net must not be swept into the field being grown.
        from advisor.dc_advisor import (
            _board_with_copper_weight, _board_with_extra_vias,
        )

        board = _board(foreign_vias=3)
        vias_before = list(board.vias)
        thickness_before = [layer.thickness_mm for layer in board.stackup.layers]

        grown = _board_with_extra_vias(board, "VCC", "F.Cu-B.Cu", 4)
        thicker = _board_with_copper_weight(board, "F.Cu", 0.070)

        self.assertEqual(board.vias, vias_before)
        self.assertEqual(
            [layer.thickness_mm for layer in board.stackup.layers],
            thickness_before,
        )
        self.assertEqual(len(grown.vias), len(vias_before) + 4)
        self.assertEqual(sum(1 for v in grown.vias if v.net_name == "GND"), 3)
        self.assertEqual(
            [layer.thickness_mm for layer in thicker.stackup.layers],
            [0.070, thickness_before[1]],
        )
        # The finished board height is the via barrel's length; a question
        # about a pour must not silently re-price every via.
        self.assertEqual(thicker.stackup.total_thickness_mm,
                         board.stackup.total_thickness_mm)


if __name__ == "__main__":
    unittest.main()
