"""A modelled pour must have the sheet resistance of real copper.

The mesher converted resistivity with the factor for an area (1e6) where the
sheet formula needs the factor for a length (1e3), so every pour was a thousand
times more resistive than copper. Nothing caught it, because nothing compared
the model against a number that exists outside it.

The consequence was not subtle: the advisor computed 6.2244 V of drop on
+5V_RAIL, against 0.0077 V from the production mesher for the same copper -- a
drop larger than the rail's own supply -- and every verdict it reached about
where the loss lived was decided by the error rather than by the board.
"""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

# One ounce of copper, in mm, and the resistivity the mesher uses.
OUNCE_MM = 0.035
RHO_OHM_M = 1.72e-8


class APourHasTheSheetResistanceOfCopper(unittest.TestCase):
    def setUp(self):
        try:
            import numpy, shapely  # noqa: F401
        except ImportError:
            self.skipTest("numpy/shapely not available")

    def _square_pour_mesh(self, grid_step_mm):
        from ingest.board_reader import (
            BoardBounds, CopperLayer, ParsedBoard, Stackup, Zone,
        )
        from mesh_hybrid import HybridMesher

        board = ParsedBoard(
            pcb_path=None,
            stackup=Stackup(
                layers=[CopperLayer(0, "F.Cu", "signal", OUNCE_MM)],
                total_thickness_mm=1.6, copper_layer_count=1,
            ),
            bounds=BoardBounds(0, 0, 10, 10),
            footprints=[], segments=[], vias=[],
            zones=[Zone("VCC", "F.Cu", filled_polygon=[
                (1.0, 1.0), (9.0, 1.0), (9.0, 9.0), (1.0, 9.0),
            ])],
        )
        return HybridMesher(board, grid_step_mm=grid_step_mm).build_mesh("VCC")

    def test_one_cell_conducts_one_square_of_copper(self):
        # R_square = rho / t = 1.72e-8 / 35e-6 = 0.491 mohm, so a square cell
        # must conduct 1 / 0.491e-3 = 2035 S. The bug gave 2.03 S.
        mesh = self._square_pour_mesh(0.1)
        expected = 1.0 / (RHO_OHM_M / (OUNCE_MM * 1.0e-3))

        interior = [
            branch for branch in mesh.branches
            if branch.kind == "lateral" and branch.resistance_ohm > 0
        ]
        self.assertTrue(interior, "the pour produced no lateral edges")
        # Cells fully inside the pour have frac == 1; edge cells are clipped,
        # so the fully-covered ones are the maximum-conductance population.
        best = min(branch.resistance_ohm for branch in interior)
        self.assertAlmostEqual(1.0 / best, expected, delta=0.01 * expected)

    def test_the_answer_does_not_depend_on_the_grid(self):
        # Sheet resistance is per square, so refining the grid must not change
        # the conductance of a cell: G = sigma * t either way. A formula that
        # smuggled in a length would fail this.
        coarse = self._square_pour_mesh(0.2)
        fine = self._square_pour_mesh(0.1)

        def best(mesh):
            return min(
                branch.resistance_ohm for branch in mesh.branches
                if branch.kind == "lateral" and branch.resistance_ohm > 0
            )

        self.assertAlmostEqual(best(coarse), best(fine), delta=0.01 * best(fine))

    def test_thicker_copper_conducts_proportionally_better(self):
        # The relation the INCREASE_COPPER_WEIGHT action is sized on: R scales
        # as 1/thickness. If this ever stops holding, that advice is wrong too.
        from ingest.board_reader import (
            BoardBounds, CopperLayer, ParsedBoard, Stackup, Zone,
        )
        from mesh_hybrid import HybridMesher

        def best(thickness_mm):
            board = ParsedBoard(
                pcb_path=None,
                stackup=Stackup(
                    layers=[CopperLayer(0, "F.Cu", "signal", thickness_mm)],
                    total_thickness_mm=1.6, copper_layer_count=1,
                ),
                bounds=BoardBounds(0, 0, 10, 10),
                footprints=[], segments=[], vias=[],
                zones=[Zone("VCC", "F.Cu", filled_polygon=[
                    (1.0, 1.0), (9.0, 1.0), (9.0, 9.0), (1.0, 9.0),
                ])],
            )
            mesh = HybridMesher(board, grid_step_mm=0.2).build_mesh("VCC")
            return min(
                branch.resistance_ohm for branch in mesh.branches
                if branch.kind == "lateral" and branch.resistance_ohm > 0
            )

        self.assertAlmostEqual(
            best(OUNCE_MM) / best(2 * OUNCE_MM), 2.0, delta=0.01,
        )


if __name__ == "__main__":
    unittest.main()
