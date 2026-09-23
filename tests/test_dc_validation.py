"""Analytical validation tests for the Phase 1 DC engine.

All tests run without KiCad / kipy / wx.  They verify:
  - ingest.track_resistance formulae against analytically known values
  - HybridMesher produces exactly one branch with the correct resistance
    for a single straight segment
"""

import sys
import os
import math
import unittest

# Ensure repository root is importable when run from the tests/ directory
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


# ---------------------------------------------------------------------------
# Helper: build a minimal ParsedBoard with one straight segment
# ---------------------------------------------------------------------------

def _make_board_one_track(
    length_mm=10.0, width_mm=1.0, thickness_mm=0.035,
    layer_name="F.Cu", net="VCC",
):
    from ingest.board_reader import (
        BoardBounds, CopperLayer, ParsedBoard, Segment, Stackup,
    )
    stackup = Stackup(
        layers=[CopperLayer(
            layer_id=0,
            name=layer_name,
            layer_type="signal",
            thickness_mm=thickness_mm,
        )],
        total_thickness_mm=1.6,
        copper_layer_count=1,
    )
    seg = Segment(
        net_name=net,
        width_mm=width_mm,
        layer=layer_name,
        start=(0.0, 0.0),
        end=(length_mm, 0.0),
    )
    bounds = BoardBounds(x_min=0, y_min=0, x_max=length_mm, y_max=5)
    return ParsedBoard(
        pcb_path=None,
        stackup=stackup,
        bounds=bounds,
        footprints=[],
        segments=[seg],
        vias=[],
        zones=[],
    )


def _make_board_two_tracks(net="VCC"):
    """Two segments in series sharing a junction node."""
    from ingest.board_reader import (
        BoardBounds, CopperLayer, ParsedBoard, Segment, Stackup,
    )
    stackup = Stackup(
        layers=[CopperLayer(0, "F.Cu", "signal", 0.035)],
        total_thickness_mm=1.6,
        copper_layer_count=1,
    )
    segs = [
        Segment(net_name=net, width_mm=1.0, layer="F.Cu",
                start=(0.0, 0.0), end=(5.0, 0.0)),
        Segment(net_name=net, width_mm=1.0, layer="F.Cu",
                start=(5.0, 0.0), end=(10.0, 0.0)),
    ]
    bounds = BoardBounds(0, 0, 10, 5)
    return ParsedBoard(
        pcb_path=None, stackup=stackup, bounds=bounds,
        footprints=[], segments=segs, vias=[], zones=[],
    )


# ---------------------------------------------------------------------------
# Tests: ingest.track_resistance
# ---------------------------------------------------------------------------

class TestSegmentResistanceFormula(unittest.TestCase):
    """Verify segment_resistance() against textbook values."""

    def _R(self, **kw):
        from ingest.track_resistance import segment_resistance
        return segment_resistance(**kw)

    def test_10mm_1mm_35um(self):
        # R = 1.72e-8 × 0.010 / (0.001 × 35e-6)
        expected = 1.72e-8 * 0.010 / (0.001 * 35e-6)
        self.assertAlmostEqual(self._R(length_mm=10, width_mm=1.0, thickness_mm=0.035),
                               expected, places=12)

    def test_custom_rho(self):
        from ingest.track_resistance import segment_resistance
        rho = 1.0e-8
        expected = rho * 0.01 / (0.001 * 35e-6)
        self.assertAlmostEqual(
            segment_resistance(10, 1.0, 0.035, rho=rho), expected, places=12
        )


class TestViaResistanceFormula(unittest.TestCase):

    def test_barrel_is_the_plated_annulus(self):
        # R = rho * h / (pi * (r_out^2 - r_in^2)), 1.6 mm board, 0.3 mm drill,
        # 25 um plating: about 1.1 mOhm.
        from ingest.track_resistance import via_resistance
        r_in = 0.15e-3
        r_out = r_in + 25e-6
        expected = 1.72e-8 * 1.6e-3 / (math.pi * (r_out ** 2 - r_in ** 2))
        self.assertAlmostEqual(
            via_resistance(height_mm=1.6, drill_mm=0.3, plating_mm=0.025),
            expected, delta=1e-9 * expected,
        )


# ---------------------------------------------------------------------------
# Tests: HybridMesher — single segment
# ---------------------------------------------------------------------------

class TestHybridMesherSingleSegment(unittest.TestCase):

    def setUp(self):
        # Skip if shapely not available (mesh_hybrid imports it lazily via ingest)
        try:
            import shapely  # noqa: F401
        except ImportError:
            self.skipTest("shapely not available")

    def test_two_nodes_one_branch(self):
        from mesh_hybrid import HybridMesher
        board = _make_board_one_track()
        mesh = HybridMesher(board).build_mesh("VCC")
        self.assertEqual(len(mesh.nodes), 2)
        self.assertEqual(len(mesh.branches), 1)

    def test_branch_resistance_matches_formula(self):
        from mesh_hybrid import HybridMesher
        from ingest.track_resistance import segment_resistance
        board = _make_board_one_track(length_mm=10.0, width_mm=1.0, thickness_mm=0.035)
        mesh = HybridMesher(board).build_mesh("VCC")
        expected = segment_resistance(10.0, 1.0, 0.035)
        self.assertAlmostEqual(mesh.branches[0].resistance_ohm, expected, places=12)

    def test_wrong_net_empty_mesh(self):
        from mesh_hybrid import HybridMesher
        board = _make_board_one_track(net="VCC")
        mesh = HybridMesher(board).build_mesh("GND")
        self.assertEqual(len(mesh.nodes), 0)
        self.assertEqual(len(mesh.branches), 0)

    def test_coo_arrays_consistent(self):
        """COO arrays must agree with branches count (4 entries per branch)."""
        from mesh_hybrid import HybridMesher
        board = _make_board_one_track()
        mesh = HybridMesher(board).build_mesh("VCC")
        n_branches = len(mesh.branches)
        self.assertEqual(len(mesh.G_coo_data), 4 * n_branches)
        self.assertEqual(len(mesh.G_coo_row), 4 * n_branches)
        self.assertEqual(len(mesh.G_coo_col), 4 * n_branches)

    def test_two_segments_in_series(self):
        """Two collinear segments share a junction — expect 3 nodes, 2 branches."""
        from mesh_hybrid import HybridMesher
        board = _make_board_two_tracks()
        mesh = HybridMesher(board).build_mesh("VCC")
        self.assertEqual(len(mesh.nodes), 3)
        self.assertEqual(len(mesh.branches), 2)

    def test_two_series_total_resistance(self):
        """Total resistance of two equal serial segments = 2× one segment."""
        from mesh_hybrid import HybridMesher
        from ingest.track_resistance import segment_resistance
        board = _make_board_two_tracks()
        mesh = HybridMesher(board).build_mesh("VCC")
        total_R = sum(b.resistance_ohm for b in mesh.branches)
        expected = 2 * segment_resistance(5.0, 1.0, 0.035)
        self.assertAlmostEqual(total_R, expected, places=12)

    def test_diagonal_segment(self):
        """A diagonal segment: length = √(3² + 4²) = 5 mm."""
        from ingest.board_reader import (
            BoardBounds, CopperLayer, ParsedBoard, Segment, Stackup,
        )
        from mesh_hybrid import HybridMesher
        from ingest.track_resistance import segment_resistance
        stackup = Stackup(
            [CopperLayer(0, "F.Cu", "signal", 0.035)], 1.6, 1
        )
        seg = Segment("NET", 0.5, "F.Cu", (0.0, 0.0), (3.0, 4.0))
        board = ParsedBoard(None, stackup, BoardBounds(0, 0, 5, 5), [], [seg], [], [])
        mesh = HybridMesher(board).build_mesh("NET")
        expected = segment_resistance(5.0, 0.5, 0.035)
        self.assertAlmostEqual(mesh.branches[0].resistance_ohm, expected, places=12)

    def test_log_callback_called(self):
        from mesh_hybrid import HybridMesher
        messages = []
        board = _make_board_one_track()
        HybridMesher(board, log_callback=messages.append).build_mesh("VCC")
        self.assertTrue(any("VCC" in m for m in messages))

    def test_coo_symmetry(self):
        """The COO Laplacian is symmetric: each off-diagonal entry appears twice."""
        from mesh_hybrid import HybridMesher
        board = _make_board_one_track()
        mesh = HybridMesher(board).build_mesh("VCC")
        data = list(zip(mesh.G_coo_row, mesh.G_coo_col, mesh.G_coo_data))
        # Off-diagonal entries come in (u,v,-g) and (v,u,-g) pairs
        off_diag = [(r, c, d) for r, c, d in data if r != c]
        self.assertEqual(len(off_diag), 2)  # one branch → two off-diagonal entries
        # They should have equal negative conductance
        self.assertAlmostEqual(off_diag[0][2], off_diag[1][2])
        self.assertLess(off_diag[0][2], 0)


# ---------------------------------------------------------------------------
# Tests: HybridMesher — vias
# ---------------------------------------------------------------------------

class TestHybridMesherVia(unittest.TestCase):

    def setUp(self):
        try:
            import shapely  # noqa: F401
        except ImportError:
            self.skipTest("shapely not available")

    def _board_with_via(self, net="VCC"):
        from ingest.board_reader import (
            BoardBounds, CopperLayer, ParsedBoard, Stackup, Via,
        )
        stackup = Stackup(
            layers=[
                CopperLayer(0, "F.Cu", "signal", 0.035),
                CopperLayer(2, "B.Cu", "signal", 0.035),
            ],
            total_thickness_mm=1.6,
            copper_layer_count=2,
        )
        via = Via(
            net_name=net, position=(5.0, 5.0),
            size_mm=0.6, drill_mm=0.3, layers=["F.Cu", "B.Cu"],
        )
        bounds = BoardBounds(0, 0, 10, 10)
        return ParsedBoard(None, stackup, bounds, [], [], [via], [])

    @staticmethod
    def _barrels(mesh):
        # The via's land is meshed as copper on each layer too; the barrel is
        # the vertical branch between them.
        return [branch for branch in mesh.branches if branch.kind == "vertical"]

    def test_via_one_barrel_between_its_two_lands(self):
        from mesh_hybrid import HybridMesher
        board = self._board_with_via()
        mesh = HybridMesher(board).build_mesh("VCC")
        barrels = self._barrels(mesh)
        self.assertEqual(len(barrels), 1)
        layers = {mesh.node_coords[barrels[0].node_a][2], mesh.node_coords[barrels[0].node_b][2]}
        self.assertEqual(layers, {0, 2})
        # DC-1 fix: the via's 0.6 mm land covers several 0.1 mm cells, so the
        # barrel's per-layer node now stitches to every one of them instead of
        # just the nearest -- "contact:via:..." branches alongside the land's
        # own "pad-copper:" cut-cell edges, not just the latter.
        self.assertTrue(all(
            branch.geometry_source.startswith("pad-copper:")
            or branch.geometry_source.startswith("contact:via:")
            for branch in mesh.branches if branch.kind != "vertical"
        ))
        self.assertTrue(any(
            branch.geometry_source.startswith("contact:via:") for branch in mesh.branches
        ))

    def test_finer_grid_adds_parallel_stitches_not_one_longer_one(self):
        # DC-1 regression. Before this fix a via/track end joined the plane at
        # the single nearest lattice node, so refining the grid moved that
        # node no closer to the real contact and only added a fictitious
        # constriction resistance: on P02 Rev E's /J2_PD_HV (3 A) the worst
        # load's drop rose 26.49 -> 28.64 -> 31.81 -> 33.07 mV from 0.1 to
        # 0.035 mm, "independent of the load sampling" (2026-09-22
        # pdhv_grid.py; re-measured 2026-09-23 after this fix, see the
        # DC-1 commit message for the post-fix table). The real board is not
        # available in CI, so this checks the mechanism on the same synthetic
        # 0.6 mm via land: refining the grid must add more parallel stitches
        # under the unchanged physical land, not replace one with another.
        from mesh_hybrid import HybridMesher

        board = self._board_with_via()
        coarse = HybridMesher(board, grid_step_mm=0.1).build_mesh("VCC")
        fine = HybridMesher(board, grid_step_mm=0.03).build_mesh("VCC")
        coarse_stitches = sum(
            1 for b in coarse.branches if b.geometry_source.startswith("contact:via:")
        )
        fine_stitches = sum(
            1 for b in fine.branches if b.geometry_source.startswith("contact:via:")
        )
        self.assertGreater(coarse_stitches, 0)
        self.assertGreater(fine_stitches, coarse_stitches)

    def test_via_resistance_positive(self):
        from mesh_hybrid import HybridMesher
        board = self._board_with_via()
        barrel = self._barrels(HybridMesher(board).build_mesh("VCC"))[0]
        self.assertGreater(barrel.resistance_ohm, 0)
        self.assertLess(barrel.resistance_ohm, 1e-2)

    def test_the_annular_ring_is_not_taken_for_the_barrel_wall(self):
        # A 0.6/0.3 mm via has a 0.15 mm annular ring. Reading that as plating
        # made the barrel ten times too conductive; the file records no plating,
        # so the barrel uses the shared fallback whatever the pad size.
        import math

        from ingest.track_resistance import via_resistance
        from mesh_hybrid import HybridMesher
        from physical_constants import PLATING_THICKNESS_FALLBACK_MM as t

        branch = self._barrels(HybridMesher(self._board_with_via()).build_mesh("VCC"))[0]
        self.assertAlmostEqual(
            branch.resistance_ohm, via_resistance(1.6, 0.3, t), delta=1e-9,
        )
        self.assertAlmostEqual(branch.cross_section_mm2, math.pi * (0.3 * t + t * t), places=9)


class TestRasterViaBarrel(unittest.TestCase):
    def test_the_barrel_is_sized_from_the_drill_not_the_via_pad(self):
        try:
            import matplotlib  # noqa: F401
            from shapely.geometry import Polygon
        except ImportError:
            self.skipTest("matplotlib/shapely not available")
        import math

        from application.dc_controller import (
            DCBoardSnapshot, DCNetSnapshot, DCPointSnapshot, DCViaSnapshot,
        )
        from mesh import Mesher
        from physical_constants import COPPER_RESISTIVITY_OHM_MM as rho
        from physical_constants import PLATING_THICKNESS_FALLBACK_MM as t

        point = DCPointSnapshot(0.0, 0.0)
        via = DCViaSnapshot(
            net=DCNetSnapshot("VCC"), position=point, start=point, width=600_000,
            drill_size=DCPointSnapshot(300_000, 300_000), layers=(0, 31),
        )
        square = Polygon([(-1, -1), (1, -1), (1, 1), (-1, 1)])
        stackup = {
            "copper": {0: {"thickness_mm": 0.035}, 31: {"thickness_mm": 0.035}},
            "resistivity": rho,
            "substrate": [{"thickness_mm": 1.5, "between": [0, 31]}],
        }
        mesh = Mesher(DCBoardSnapshot((), (via,))).generate_mesh(
            "VCC", {0: square, 31: square}, stackup, grid_size_mm=0.5,
        )
        barrels = [branch for branch in mesh.branches if branch.kind == "via"]
        self.assertEqual(len(barrels), 1)
        expected = rho * 1.5 / (math.pi * (0.3 * t + t * t))
        self.assertAlmostEqual(barrels[0].resistance_ohm, expected, delta=1e-6 * expected)


# ---------------------------------------------------------------------------
# Tests: HybridMesher — zone cut-cell
# ---------------------------------------------------------------------------

class TestHybridMesherZoneCutCell(unittest.TestCase):
    """Zone cut-cell meshing with a synthetic filled polygon."""

    def setUp(self):
        try:
            import shapely  # noqa: F401
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("shapely or numpy not available")

    def _board_with_zone(self, poly_pts, net="GND", layer="F.Cu", thickness=0.035):
        from ingest.board_reader import (
            BoardBounds, CopperLayer, ParsedBoard, Stackup, Zone,
        )
        stackup = Stackup(
            layers=[CopperLayer(0, layer, "signal", thickness)],
            total_thickness_mm=1.6,
            copper_layer_count=1,
        )
        zone = Zone(net_name=net, layer=layer, filled_polygon=poly_pts)
        return ParsedBoard(
            pcb_path=None, stackup=stackup,
            bounds=BoardBounds(0, 0, 10, 10),
            footprints=[], segments=[], vias=[], zones=[zone],
        )

    def test_wrong_net_no_edges(self):
        """Zone not on requested net must produce no edges."""
        from mesh_hybrid import HybridMesher
        pts = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
        board = self._board_with_zone(pts, net="GND")
        mesh = HybridMesher(board, grid_step_mm=0.5).build_mesh("+5V_RAIL")
        self.assertEqual(len(mesh.branches), 0)

    def test_outline_fallback(self):
        """If filled_polygon is empty, outline_polygon is used as fallback."""
        from ingest.board_reader import (
            BoardBounds, CopperLayer, ParsedBoard, Stackup, Zone,
        )
        from mesh_hybrid import HybridMesher
        stackup = Stackup(
            layers=[CopperLayer(0, "F.Cu", "signal", 0.035)],
            total_thickness_mm=1.6, copper_layer_count=1,
        )
        pts = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
        zone = Zone(net_name="GND", layer="F.Cu",
                    filled_polygon=[],    # empty — should fall back to outline
                    outline_polygon=pts)
        board = ParsedBoard(
            pcb_path=None, stackup=stackup,
            bounds=BoardBounds(0, 0, 5, 5),
            footprints=[], segments=[], vias=[], zones=[zone],
        )
        mesh = HybridMesher(board, grid_step_mm=0.5).build_mesh("GND")
        self.assertGreater(len(mesh.branches), 0)


if __name__ == "__main__":
    unittest.main()
