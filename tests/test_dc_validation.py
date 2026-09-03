"""Analytical validation tests for the Phase 1 DC engine.

All tests run without KiCad / kipy / wx.  They verify:
  - ingest.track_resistance formulae against analytically known values
  - HybridMesher produces exactly one branch with the correct resistance
    for a single straight segment
  - Solver.solve() with use_precond=True agrees with the default backend
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

    def test_1mm_0_25mm_35um(self):
        expected = 1.72e-8 * 0.001 / (0.00025 * 35e-6)
        self.assertAlmostEqual(self._R(length_mm=1, width_mm=0.25, thickness_mm=0.035),
                               expected, places=12)

    def test_proportional_to_length(self):
        r1 = self._R(length_mm=5, width_mm=1, thickness_mm=0.035)
        r2 = self._R(length_mm=10, width_mm=1, thickness_mm=0.035)
        self.assertAlmostEqual(r2 / r1, 2.0, places=10)

    def test_proportional_to_inverse_width(self):
        r1 = self._R(length_mm=10, width_mm=1, thickness_mm=0.035)
        r2 = self._R(length_mm=10, width_mm=2, thickness_mm=0.035)
        self.assertAlmostEqual(r1 / r2, 2.0, places=10)

    def test_zero_length_raises(self):
        from ingest.track_resistance import segment_resistance
        with self.assertRaises(ValueError):
            segment_resistance(length_mm=0, width_mm=1, thickness_mm=0.035)

    def test_negative_width_raises(self):
        from ingest.track_resistance import segment_resistance
        with self.assertRaises(ValueError):
            segment_resistance(length_mm=1, width_mm=-1, thickness_mm=0.035)

    def test_zero_thickness_raises(self):
        from ingest.track_resistance import segment_resistance
        with self.assertRaises(ValueError):
            segment_resistance(length_mm=1, width_mm=1, thickness_mm=0.0)

    def test_custom_rho(self):
        from ingest.track_resistance import segment_resistance
        rho = 1.0e-8
        expected = rho * 0.01 / (0.001 * 35e-6)
        self.assertAlmostEqual(
            segment_resistance(10, 1.0, 0.035, rho=rho), expected, places=12
        )


class TestViaResistanceFormula(unittest.TestCase):
    """Verify via_resistance() order-of-magnitude and formula structure."""

    def _R(self, **kw):
        from ingest.track_resistance import via_resistance
        return via_resistance(**kw)

    def test_positive(self):
        self.assertGreater(self._R(height_mm=1.6, drill_mm=0.3, plating_mm=0.025), 0)

    def test_sub_milliohm(self):
        # A typical through-hole via should be in the single-digit mΩ range
        # (1.6mm board, 0.3mm drill, 25µm plating → ~1.1 mΩ by thin-annulus formula)
        self.assertLess(self._R(height_mm=1.6, drill_mm=0.3, plating_mm=0.025), 5e-3)

    def test_thicker_board_higher_resistance(self):
        r1 = self._R(height_mm=1.0, drill_mm=0.3, plating_mm=0.025)
        r2 = self._R(height_mm=2.0, drill_mm=0.3, plating_mm=0.025)
        self.assertGreater(r2, r1)

    def test_thicker_plating_lower_resistance(self):
        r1 = self._R(height_mm=1.6, drill_mm=0.3, plating_mm=0.025)
        r2 = self._R(height_mm=1.6, drill_mm=0.3, plating_mm=0.05)
        self.assertLess(r2, r1)

    def test_zero_drill_raises(self):
        from ingest.track_resistance import via_resistance
        with self.assertRaises(ValueError):
            via_resistance(height_mm=1.6, drill_mm=0.0)

    def test_zero_height_raises(self):
        from ingest.track_resistance import via_resistance
        with self.assertRaises(ValueError):
            via_resistance(height_mm=0.0, drill_mm=0.3)


class TestSpreadingResistanceFormula(unittest.TestCase):

    def _R(self, **kw):
        from ingest.track_resistance import spreading_resistance
        return spreading_resistance(**kw)

    def test_positive(self):
        self.assertGreater(self._R(thickness_mm=0.035, r1_mm=0.15, r2_mm=1.0), 0)

    def test_log_scaling(self):
        # R ∝ ln(r2/r1)
        r1 = self._R(thickness_mm=0.035, r1_mm=0.15, r2_mm=math.e * 0.15)
        r2 = self._R(thickness_mm=0.035, r1_mm=0.15, r2_mm=math.e ** 2 * 0.15)
        self.assertAlmostEqual(r2 / r1, 2.0, places=10)

    def test_r1_ge_r2_raises(self):
        from ingest.track_resistance import spreading_resistance
        with self.assertRaises(ValueError):
            spreading_resistance(thickness_mm=0.035, r1_mm=1.0, r2_mm=0.5)

    def test_zero_thickness_raises(self):
        from ingest.track_resistance import spreading_resistance
        with self.assertRaises(ValueError):
            spreading_resistance(thickness_mm=0.0, r1_mm=0.15, r2_mm=1.0)


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

    def test_via_two_nodes_one_branch(self):
        from mesh_hybrid import HybridMesher
        board = self._board_with_via()
        mesh = HybridMesher(board).build_mesh("VCC")
        self.assertEqual(len(mesh.nodes), 2)
        self.assertEqual(len(mesh.branches), 1)
        self.assertEqual(mesh.branches[0].kind, "vertical")

    def test_via_resistance_positive(self):
        from mesh_hybrid import HybridMesher
        board = self._board_with_via()
        mesh = HybridMesher(board).build_mesh("VCC")
        self.assertGreater(mesh.branches[0].resistance_ohm, 0)
        self.assertLess(mesh.branches[0].resistance_ohm, 1e-3)


# ---------------------------------------------------------------------------
# Tests: Solver — ILU preconditioner
# ---------------------------------------------------------------------------

class TestSolverPreconditioner(unittest.TestCase):

    def setUp(self):
        try:
            import scipy  # noqa: F401
        except ImportError:
            self.skipTest("scipy not available")

    def _three_node_mesh(self):
        """Resistor divider: 0 -[10Ω]- 1 -[10Ω]- 2."""
        from mesh import Mesh
        m = Mesh()
        m.nodes = [0, 1, 2]
        m.node_coords = {0: (0, 0, 0), 1: (5, 0, 0), 2: (10, 0, 0)}
        m.add_edge_direct(0, 1, 1.0 / 10.0)
        m.add_edge_direct(1, 2, 1.0 / 10.0)
        return m

    def _solve(self, use_precond):
        from solver import Solver
        mesh = self._three_node_mesh()
        # Both source and ground must be Dirichlet; loads carry current, not voltage.
        sources = [{"node_id": 0, "voltage": 5.0}, {"node_id": 2, "voltage": 0.0}]
        loads = []
        return Solver().solve(mesh, sources, loads, use_precond=use_precond)

    def test_direct_gives_midpoint(self):
        result = self._solve(False)
        self.assertAlmostEqual(result[0], 5.0, places=6)
        self.assertAlmostEqual(result[1], 2.5, places=5)
        self.assertAlmostEqual(result[2], 0.0, places=6)

    def test_precond_matches_direct(self):
        direct = self._solve(False)
        precond = self._solve(True)
        for node_id in [0, 1, 2]:
            self.assertAlmostEqual(
                direct.get(node_id, 0), precond.get(node_id, 0), places=5
            )

    def test_precond_false_is_default(self):
        """Calling solve() without use_precond must behave identically to False."""
        from solver import Solver
        mesh = self._three_node_mesh()
        sources = [{"node_id": 0, "voltage": 5.0}, {"node_id": 2, "voltage": 0.0}]
        loads = []
        s = Solver()
        result_default = s.solve(mesh, sources, loads)
        result_false = s.solve(mesh, sources, loads, use_precond=False)
        for node_id in [0, 1, 2]:
            self.assertAlmostEqual(
                result_default.get(node_id, 0),
                result_false.get(node_id, 0),
                places=8,
            )

    def test_larger_chain(self):
        """10-node resistor chain with preconditioner."""
        from mesh import Mesh
        from solver import Solver
        n = 10
        m = Mesh()
        m.nodes = list(range(n))
        m.node_coords = {i: (i, 0, 0) for i in range(n)}
        R = 1.0  # 1 Ω per segment
        for i in range(n - 1):
            m.add_edge_direct(i, i + 1, 1.0 / R)
        sources = [{"node_id": 0, "voltage": float(n - 1)}]
        loads = [{"node_id": n - 1, "voltage": 0.0}]
        direct = Solver().solve(m, sources, loads, use_precond=False)
        precond = Solver().solve(m, sources, loads, use_precond=True)
        for i in range(n):
            self.assertAlmostEqual(direct.get(i, 0), precond.get(i, 0), places=5)


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

    def test_square_zone_produces_edges(self):
        """A 2×2 mm filled square should produce horizontal and vertical edges."""
        from mesh_hybrid import HybridMesher
        pts = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
        board = self._board_with_zone(pts, net="GND")
        mesh = HybridMesher(board, grid_step_mm=0.5).build_mesh("GND")
        # With 0.5mm grid over [0,2]×[0,2]: 5×5 = 25 nodes max, many edges
        self.assertGreater(len(mesh.branches), 0)
        self.assertGreater(len(mesh.nodes), 1)

    def test_edge_conductances_positive(self):
        """All cut-cell edges must have positive conductance."""
        from mesh_hybrid import HybridMesher
        pts = [(0.0, 0.0), (3.0, 0.0), (3.0, 1.0), (0.0, 1.0)]
        board = self._board_with_zone(pts, net="GND")
        mesh = HybridMesher(board, grid_step_mm=0.5).build_mesh("GND")
        for branch in mesh.branches:
            self.assertGreater(branch.resistance_ohm, 0)

    def test_full_square_conductance_equals_analytical(self):
        """For a 1×1 cell fully inside the zone, g must equal σ·t (unit-aspect square).

        With grid_step=1.0 mm and a 2×2 zone, all internal cells are full-copper.
        Each horizontal edge models a 1×1×t block → G = σ·t·1/1 = σ·t.
        """
        from mesh_hybrid import HybridMesher, RHO_COPPER
        pts = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]
        board = self._board_with_zone(pts, net="GND", thickness=0.035)
        mesh = HybridMesher(board, grid_step_mm=1.0).build_mesh("GND")
        # σ in S/mm = 1/(ρ_Cu Ω·m) converted to per-mm: σ_mm = 1/(ρ * 1e6)
        sigma_mm = 1.0 / (RHO_COPPER * 1e6)
        g_expected = sigma_mm * 0.035  # one S/mm square → G = σ·t
        # At least some branches should match (full-copper interior edges)
        full_edges = [b for b in mesh.branches if abs(b.resistance_ohm - 1/g_expected) < 1e-6]
        self.assertGreater(len(full_edges), 0,
                           msg="No full-copper edge found — check cut-cell formula")

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
