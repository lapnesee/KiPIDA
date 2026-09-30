"""The hybrid DC mesher: pours, pads, vias and tracks as one network.

Five regression files merged into one (test triage, audit-2026-09-23.md):
track-to-plane junctions, pads, pour sheet resistance, lattice nodes that
follow the copper, and per-layer zone unions. Each section keeps the account
of the defect it locks in.
"""

import math
import os
import sys
import unittest
from collections import defaultdict

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


# ---------------------------------------------------------------------------
# From test_mesh_hybrid_junction.py
# ---------------------------------------------------------------------------
# Tracks, planes and vias must form one electrical network.
#
# Two defects kept them apart on the reference board, and together they made
# every rail unsolvable by the advisor:
#
# * track endpoints snap to 1 um while zone nodes sit on the 0.1 mm cut-cell
#   grid, so the two lattices only ever coincided by accident;
# * a through via created nodes on its two named layers only, skipping the
#   inner layers it physically shorts -- so tracks on F.Cu/B.Cu could not
#   reach the rail's plane on In2.Cu.
#
# One real rail split into 63,897 connected components as a result.


def _junction_board(segments=(), vias=(), zones=()):
    from ingest.board_reader import (
        BoardBounds, CopperLayer, ParsedBoard, Stackup,
    )
    stackup = Stackup(
        layers=[
            CopperLayer(0, "F.Cu", "signal", 0.035),
            CopperLayer(4, "In1.Cu", "power", 0.0152),
            CopperLayer(6, "In2.Cu", "power", 0.0152),
            CopperLayer(2, "B.Cu", "signal", 0.035),
        ],
        total_thickness_mm=1.6, copper_layer_count=4,
    )
    return ParsedBoard(
        pcb_path=None, stackup=stackup, bounds=BoardBounds(0, 0, 10, 10),
        footprints=[], segments=list(segments), vias=list(vias),
        zones=list(zones),
    )


class TrackToPlaneJunctionTests(unittest.TestCase):
    def setUp(self):
        try:
            import numpy, shapely  # noqa: F401
        except ImportError:
            self.skipTest("numpy/shapely not available")

    def test_a_track_ending_on_a_pour_shares_its_node(self):
        from ingest.board_reader import Segment, Zone
        from mesh_hybrid import HybridMesher

        # A track on F.Cu ending inside an F.Cu pour. Its endpoint is
        # deliberately off-grid (2.03 mm) so an exact-coordinate match cannot
        # succeed and only the proximity join can.
        board = _junction_board(
            segments=[Segment("VCC", 0.5, "F.Cu", (0.0, 2.03), (2.03, 2.03))],
            zones=[Zone("VCC", "F.Cu", filled_polygon=[
                (2.0, 0.0), (6.0, 0.0), (6.0, 4.0), (2.0, 4.0),
            ])],
        )
        mesh = HybridMesher(board, grid_step_mm=0.5).build_mesh("VCC")
        sources = {b.geometry_source.split(":")[0] for b in mesh.branches}
        self.assertIn("seg", sources)
        self.assertIn("zone", sources)

        # The shared node is what makes them one network: some node must carry
        # both a segment branch and a zone branch.
        by_node = {}
        for branch in mesh.branches:
            kind = branch.geometry_source.split(":")[0]
            by_node.setdefault(branch.node_a, set()).add(kind)
            by_node.setdefault(branch.node_b, set()).add(kind)
        self.assertTrue(
            any({"seg", "zone"} <= kinds for kinds in by_node.values()),
            "no node joins a track to the pour it lands on",
        )


class CopperLandTests(unittest.TestCase):
    """A track meets a pad or via land anywhere on it, not only at its centre."""

    def setUp(self):
        try:
            import numpy, shapely  # noqa: F401
        except ImportError:
            self.skipTest("numpy/shapely not available")

    @staticmethod
    def _connected(mesh, a, b):
        adjacency = {}
        for branch in mesh.branches:
            adjacency.setdefault(branch.node_a, set()).add(branch.node_b)
            adjacency.setdefault(branch.node_b, set()).add(branch.node_a)
        seen, stack = {a}, [a]
        while stack:
            for neighbour in adjacency.get(stack.pop(), ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        return b in seen

    def test_a_segment_shorter_than_the_snap_still_joins_its_ends(self):
        # P02 Rev E's /J1_PD_OUT: a 0.8 um segment whose ends round to two
        # nodes 1 um apart left R92 and U1 on a 478-node island.
        from ingest.board_reader import Segment
        from mesh_hybrid import HybridMesher

        board = _junction_board(segments=[
            Segment("VCC", 0.25, "F.Cu", (5.0, 5.0), (8.67, 5.0)),
            Segment("VCC", 0.25, "F.Cu", (8.67, 5.0), (8.669433, 5.000567)),
            Segment("VCC", 0.25, "F.Cu", (8.669433, 5.000567), (1.0, 5.000567)),
        ])
        mesh = HybridMesher(board, grid_step_mm=0.1).build_mesh("VCC")
        start = next(n for n, c in mesh.node_coords.items() if c[:2] == (5.0, 5.0))
        end = next(n for n, c in mesh.node_coords.items() if c[:2] == (1.0, 5.001))
        self.assertTrue(self._connected(mesh, start, end))

    def test_a_track_ending_off_centre_on_a_via_land_reaches_the_via(self):
        # R9's feed on +3V3AON stops 0.14 mm from a 0.7 mm via's centre.
        from ingest.board_reader import Segment, Via
        from mesh_hybrid import HybridMesher

        board = _junction_board(
            segments=[
                Segment("VCC", 0.25, "F.Cu", (1.0, 5.0), (4.9, 5.1)),
                Segment("VCC", 0.25, "B.Cu", (5.0, 5.0), (9.0, 5.0)),
            ],
            vias=[Via("VCC", (5.0, 5.0), 0.7, 0.3, ["F.Cu", "B.Cu"])],
        )
        mesh = HybridMesher(board, grid_step_mm=0.1).build_mesh("VCC")
        start = next(n for n, c in mesh.node_coords.items() if c[:2] == (1.0, 5.0) and c[2] == 0)
        end = next(n for n, c in mesh.node_coords.items() if c[:2] == (9.0, 5.0) and c[2] == 2)
        self.assertTrue(self._connected(mesh, start, end))

    def test_a_via_beside_the_axis_of_a_wide_track_is_joined_to_it(self):
        # P02 Rev E: eight vias sewn along the edge of the 1 mm J2_SRC_VBUS
        # track, 0.52 mm off its axis, reached it only through the pad at its
        # end, and the 3 A load current ran there and back through slivers.
        from ingest.board_reader import Segment, Via
        from mesh_hybrid import HybridMesher

        board = _junction_board(
            segments=[
                Segment("VCC", 1.0, "F.Cu", (1.0, 5.0), (9.0, 5.0)),
                Segment("VCC", 0.25, "B.Cu", (5.0, 5.52), (5.0, 9.0)),
            ],
            vias=[Via("VCC", (5.0, 5.52), 0.8, 0.4, ["F.Cu", "B.Cu"])],
        )
        mesh = HybridMesher(board, grid_step_mm=0.1).build_mesh("VCC")
        start = next(n for n, c in mesh.node_coords.items() if c[:2] == (1.0, 5.0) and c[2] == 0)
        end = next(n for n, c in mesh.node_coords.items() if c[:2] == (5.0, 9.0) and c[2] == 2)
        self.assertTrue(self._connected(mesh, start, end))

    def test_a_track_running_through_a_pad_is_joined_to_it(self):
        # On +3V3AON a B.Cu track from C5 runs through C7's pad and ends beyond.
        from ingest.board_reader import Footprint, Pad, Segment
        from mesh_hybrid import HybridMesher

        pad = Pad("1", "VCC", "passive", (5.0, 5.0), "smd", ("F.Cu",),
                  shape="roundrect", size_mm=(0.9, 0.95), angle_deg=270.0)
        board = _junction_board(segments=[Segment("VCC", 0.5, "F.Cu", (1.0, 5.0), (9.0, 5.0))])
        board.footprints.append(Footprint("C7", "", "", (5.0, 5.0), 270.0, "F.Cu", "", "", [pad]))
        mesh = HybridMesher(board, grid_step_mm=0.1).build_mesh("VCC")
        pad_nodes = [
            n for n, (x, y, layer) in mesh.node_coords.items()
            if layer == 0 and abs(x - 5.0) < 0.3 and abs(y - 5.0) < 0.3
        ]
        start = next(n for n, c in mesh.node_coords.items() if c[:2] == (1.0, 5.0))
        self.assertTrue(pad_nodes)
        self.assertTrue(all(self._connected(mesh, start, node) for node in pad_nodes))
        # Split at the pad, the track's pieces still add up to the whole track.
        from ingest.track_resistance import segment_resistance

        pieces = sum(b.resistance_ohm for b in mesh.branches if b.geometry_source == "seg:F.Cu")
        self.assertAlmostEqual(pieces, segment_resistance(8.0, 0.5, 0.035), delta=1e-12)

    def test_a_rotated_rectangular_pad_is_drawn_turned(self):
        from ingest.board_reader import Pad
        from mesh_hybrid import _pad_outline

        pad = Pad("1", "VCC", "passive", (10.0, 10.0), "smd", ("F.Cu",),
                  shape="rect", size_mm=(2.0, 0.5), angle_deg=90.0)
        xmin, ymin, xmax, ymax = _pad_outline(pad).bounds
        self.assertAlmostEqual(xmax - xmin, 0.5, places=9)
        self.assertAlmostEqual(ymax - ymin, 2.0, places=9)

    def test_a_pour_set_not_to_connect_stays_apart_from_the_pad(self):
        # The fill keeps its clearance around the pad; only copper touches.
        from ingest.board_reader import Footprint, Pad, Zone
        from mesh_hybrid import HybridMesher

        pad = Pad("1", "VCC", "passive", (5.0, 5.0), "smd", ("F.Cu",),
                  shape="rect", size_mm=(1.0, 1.0))
        # The pad spans x 4.5..5.5; the pour starts 0.3 mm further on.
        zone = Zone("VCC", "F.Cu", filled_polygon=[(5.8, 3.0), (8.0, 3.0), (8.0, 7.0), (5.8, 7.0)])
        board = _junction_board(zones=[zone])
        board.footprints.append(Footprint("U1", "", "", (5.0, 5.0), 0.0, "F.Cu", "", "", [pad]))
        mesh = HybridMesher(board, grid_step_mm=0.1).build_mesh("VCC")

        def node_near(x, y):
            return min(mesh.node_coords, key=lambda n: (mesh.node_coords[n][0] - x) ** 2 + (mesh.node_coords[n][1] - y) ** 2)

        self.assertFalse(self._connected(mesh, node_near(5.0, 5.0), node_near(7.0, 5.0)))
        # Bridged by a spoke, they join.
        spoke = Zone("VCC", "F.Cu", filled_polygon=[(5.4, 4.9), (5.9, 4.9), (5.9, 5.1), (5.4, 5.1)])
        board.zones.append(spoke)
        mesh = HybridMesher(board, grid_step_mm=0.1).build_mesh("VCC")
        self.assertTrue(self._connected(mesh, node_near(5.0, 5.0), node_near(7.0, 5.0)))


class ThroughViaSpanTests(unittest.TestCase):
    def setUp(self):
        try:
            import numpy, shapely  # noqa: F401
        except ImportError:
            self.skipTest("numpy/shapely not available")

    def test_a_through_via_touches_the_inner_layers_it_crosses(self):
        from ingest.board_reader import Via
        from mesh_hybrid import HybridMesher

        board = _junction_board(vias=[Via("VCC", (1.0, 1.0), 0.6, 0.3, ["F.Cu", "B.Cu"])])
        mesh = HybridMesher(board).build_mesh("VCC")

        layers = {coord[2] for coord in mesh.node_coords.values()}
        self.assertEqual(
            layers, {0, 4, 6, 2},
            "a via named F.Cu-B.Cu must still reach the inner layers it drills through",
        )
        # Three hops for four layers, chained rather than a single jump. The
        # via's land on each layer is meshed as copper around them.
        self.assertEqual(len([b for b in mesh.branches if b.kind == "vertical"]), 3)

    def test_the_barrel_resistance_is_shared_across_the_hops(self):
        # Splitting one barrel into N hops must not multiply its resistance.
        from ingest.board_reader import Via
        from ingest.track_resistance import via_resistance
        from mesh_hybrid import HybridMesher
        from physical_constants import PLATING_THICKNESS_FALLBACK_MM

        board = _junction_board(vias=[Via("VCC", (1.0, 1.0), 0.6, 0.3, ["F.Cu", "B.Cu"])])
        mesh = HybridMesher(board).build_mesh("VCC")

        total = sum(b.resistance_ohm for b in mesh.branches if b.kind == "vertical")
        # The plating is the shared fallback. This expectation used to be 0.15
        # mm -- the via's annular ring, which is not the barrel wall.
        expected = via_resistance(1.6, 0.3, PLATING_THICKNESS_FALLBACK_MM)
        self.assertAlmostEqual(total, expected, places=12)


# ---------------------------------------------------------------------------
# From test_mesh_hybrid_pads.py
# ---------------------------------------------------------------------------
# Pads are copper, and the mesher used to ignore them entirely.
#
# HybridMesher modelled segments, vias and zones. A through-hole pad shorts every
# copper layer it passes exactly as a via does, so leaving pads out meant a track
# ending on one had no path to the plane beneath it.
#
# On the reference board that stranded loads on four of seven rails, while the
# production rasterising mesher -- which does index pads -- reported no exclusions
# on the same copper. +3V3_MAIN's load sat at J6 pad 3, `thru_hole` on `*.Cu`,
# 0.02 mm above a plane it could not reach.


def _pads_board(footprints=(), zones=(), segments=()):
    from ingest.board_reader import (
        BoardBounds, CopperLayer, ParsedBoard, Stackup,
    )
    stackup = Stackup(
        layers=[
            CopperLayer(0, "F.Cu", "signal", 0.035),
            CopperLayer(4, "In1.Cu", "power", 0.0152),
            CopperLayer(2, "B.Cu", "signal", 0.035),
        ],
        total_thickness_mm=1.6, copper_layer_count=3,
    )
    return ParsedBoard(
        pcb_path=None, stackup=stackup, bounds=BoardBounds(0, 0, 20, 20),
        footprints=list(footprints), segments=list(segments), vias=[],
        zones=list(zones),
    )


def _footprint(pads):
    from ingest.board_reader import Footprint

    return Footprint(
        reference="J6", value="", lib_id="", position=(0.0, 0.0),
        rotation=0.0, layer="F.Cu", sch_path="", sch_sheet_file="", pads=pads,
    )


class ThroughHolePadsBridgeLayers(unittest.TestCase):
    def setUp(self):
        try:
            import numpy, shapely  # noqa: F401
        except ImportError:
            self.skipTest("numpy/shapely not available")

    def test_a_through_hole_pad_reaches_every_copper_layer(self):
        # The pad needs copper to meet: one touching nothing is skipped on
        # purpose, since minting nodes for it would capture load and source
        # lookups away from the real network.
        from ingest.board_reader import Pad, Segment
        from mesh_hybrid import HybridMesher

        board = _pads_board(
            footprints=[_footprint([
                Pad(number="3", net_name="VCC", pintype="passive",
                    position=(5.0, 5.0), pad_type="thru_hole",
                    layers=("*.Cu",), drill_mm=0.8),
            ])],
            segments=[Segment("VCC", 0.5, "F.Cu", (5.0, 5.0), (8.0, 5.0))],
        )
        mesh = HybridMesher(board).build_mesh("VCC")

        layers = {coord[2] for coord in mesh.node_coords.values()}
        self.assertEqual(layers, {0, 4, 2})

    def test_a_pad_on_copper_apart_from_the_source_is_stranded(self):
        from ingest.board_reader import Pad, Segment
        from mesh_hybrid import HybridMesher, stranded_pads

        board = _pads_board(
            footprints=[_footprint([
                Pad(number=str(index), net_name="VCC", pintype="passive", position=(x, 5.0),
                    layers=("F.Cu",), shape="rect", size_mm=(0.5, 0.5))
                for index, x in ((1, 2.0), (2, 8.0), (3, 5.0))
            ])],
            segments=[
                Segment("VCC", 0.25, "F.Cu", (2.0, 5.0), (4.0, 5.0)),
                Segment("VCC", 0.25, "F.Cu", (7.0, 5.0), (9.0, 5.0)),
            ],
        )
        mesh = HybridMesher(board).build_mesh("VCC")
        source = next(n for n, c in mesh.node_coords.items() if c[:2] == (4.0, 5.0))
        # J6.3 touches no copper at all (none such on P02 Rev E).
        self.assertEqual(stranded_pads(mesh, [source]), ["J6.2", "J6.3"])

    def test_a_pad_touching_no_copper_is_left_out(self):
        # Meshing pads unconditionally moved +5V_RAIL's source Q3 onto three
        # one-node components while its load sat in the 113,504-node network,
        # turning a solvable rail unreachable: find_node_at picks the nearest
        # node, and a private node exactly on the pad beats the real one a
        # fraction of a millimetre away.
        from ingest.board_reader import Pad
        from mesh_hybrid import HybridMesher

        board = _pads_board(footprints=[_footprint([
            Pad(number="3", net_name="VCC", pintype="passive",
                position=(5.0, 5.0), pad_type="thru_hole",
                layers=("*.Cu",), drill_mm=0.8),
        ])])
        mesh = HybridMesher(board).build_mesh("VCC")

        self.assertEqual(mesh.node_coords, {})

    def test_a_surface_pad_adds_no_vertical_path(self):
        from ingest.board_reader import Pad
        from mesh_hybrid import HybridMesher

        board = _pads_board(footprints=[_footprint([
            Pad(number="1", net_name="VCC", pintype="passive",
                position=(5.0, 5.0), pad_type="smd", layers=("F.Cu",)),
        ])])
        mesh = HybridMesher(board).build_mesh("VCC")

        self.assertEqual(
            [b for b in mesh.branches if b.geometry_source.startswith("pad:")], [],
        )

    def test_a_track_on_a_through_hole_pad_reaches_an_inner_plane(self):
        # The reference board's failure in miniature: a track on F.Cu ending on
        # a through-hole pad, and the rail's pour on an inner layer. Without
        # pads in the mesh the two are separate networks.
        from ingest.board_reader import Pad, Segment, Zone
        from mesh_hybrid import HybridMesher

        board = _pads_board(
            footprints=[_footprint([
                Pad(number="3", net_name="VCC", pintype="passive",
                    position=(5.0, 5.0), pad_type="thru_hole",
                    layers=("*.Cu",), drill_mm=0.8),
            ])],
            segments=[Segment("VCC", 0.5, "F.Cu", (5.0, 5.0), (9.0, 5.0))],
            zones=[Zone("VCC", "In1.Cu", filled_polygon=[
                (2.0, 2.0), (12.0, 2.0), (12.0, 12.0), (2.0, 12.0),
            ])],
        )
        mesh = HybridMesher(board, grid_step_mm=0.5).build_mesh("VCC")

        adjacency = {}
        for branch in mesh.branches:
            adjacency.setdefault(branch.node_a, set()).add(branch.node_b)
            adjacency.setdefault(branch.node_b, set()).add(branch.node_a)
        start = next(
            node for node, coord in mesh.node_coords.items()
            if coord[2] == 0 and abs(coord[0] - 9.0) < 1e-6
        )
        seen, stack = {start}, [start]
        while stack:
            for neighbour in adjacency.get(stack.pop(), ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        reached = {mesh.node_coords[node][2] for node in seen}
        self.assertIn(4, reached, "the track cannot reach the inner plane")


class PlaneJoinUsesTheCellDiagonal(unittest.TestCase):
    """Half the cell side rejects copper in the corners of every cell.

    Zone nodes sit on a square lattice of pitch `step`, so an arbitrary point
    inside a cell can be up to (step/2)*sqrt(2) from the nearest node. A 0.5*step
    threshold therefore refuses about a fifth of the cell area. J6 pad 3 landed
    0.0559 mm from its plane node against a 0.05 mm limit and stayed stranded.
    """

    def setUp(self):
        try:
            import numpy, shapely  # noqa: F401
        except ImportError:
            self.skipTest("numpy/shapely not available")

    def test_a_pad_in_the_corner_of_a_cell_still_joins_the_pour(self):
        from ingest.board_reader import Pad, Zone
        from mesh_hybrid import HybridMesher

        # Offset by 0.6 of a 0.5 mm cell in both axes: 0.42 mm from the nearest
        # lattice point, past 0.5*step (0.25) but inside the half-diagonal.
        board = _pads_board(
            footprints=[_footprint([
                Pad(number="3", net_name="VCC", pintype="passive",
                    position=(5.3, 5.3), pad_type="smd", layers=("In1.Cu",)),
            ])],
            zones=[Zone("VCC", "In1.Cu", filled_polygon=[
                (2.0, 2.0), (12.0, 2.0), (12.0, 12.0), (2.0, 12.0),
            ])],
        )
        mesh = HybridMesher(board, grid_step_mm=0.5).build_mesh("VCC")

        # The pad must not have minted a private node: every In1.Cu node should
        # belong to the pour's lattice.
        stray = [
            node for node, coord in mesh.node_coords.items()
            if coord[2] == 4 and not any(
                branch.node_a == node or branch.node_b == node
                for branch in mesh.branches
            )
        ]
        self.assertEqual(stray, [], "the pad created an isolated node beside the pour")


# ---------------------------------------------------------------------------
# From test_mesh_hybrid_sheet_resistance.py
# ---------------------------------------------------------------------------
# A modelled pour must have the sheet resistance of real copper.
#
# The mesher converted resistivity with the factor for an area (1e6) where the
# sheet formula needs the factor for a length (1e3), so every pour was a thousand
# times more resistive than copper. Nothing caught it, because nothing compared
# the model against a number that exists outside it.
#
# The consequence was not subtle: the advisor computed 6.2244 V of drop on
# +5V_RAIL, against 0.0077 V from the production mesher for the same copper -- a
# drop larger than the rail's own supply -- and every verdict it reached about
# where the loss lived was decided by the error rather than by the board.


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


# ---------------------------------------------------------------------------
# From test_mesh_hybrid_zone_nodes.py
# ---------------------------------------------------------------------------
# Zone lattice nodes must follow the copper, not the bounding box.
#
# `_mesh_zone_cutcell` grids the pour's *bounding box* and used to mint a node at
# every lattice point of it. A pour rarely fills its own bounding box, so nodes
# landed where the net has no copper: in the empty corners of a non-rectangular
# pour, and inside every antipad hole. A branch only exists where a cell overlaps
# copper, so none of those nodes could ever gain one, and each became a connected
# component of exactly one node.
#
# That is item A4: +3V3_MAIN on the reference board meshes to 902 connected
# components, 900 of them isolated single nodes.
#
# They are not only waste. Lattice nodes are indexed as plane nodes, and `probe`
# reads that index to decide whether a pad meets copper at all -- so a phantom
# node let a pad bind to a pour that is not there.


def _zone_nodes_board(zones=(), footprints=()):
    from ingest.board_reader import (
        BoardBounds, CopperLayer, ParsedBoard, Stackup,
    )
    stackup = Stackup(
        layers=[
            CopperLayer(0, "F.Cu", "signal", 0.035),
            CopperLayer(2, "B.Cu", "signal", 0.035),
        ],
        total_thickness_mm=1.6, copper_layer_count=2,
    )
    return ParsedBoard(
        pcb_path=None, stackup=stackup, bounds=BoardBounds(0, 0, 20, 20),
        footprints=list(footprints), segments=[], vias=[], zones=list(zones),
    )


def _orphans(mesh):
    """Nodes carrying no branch at all."""
    linked = set()
    for branch in mesh.branches:
        linked.add(branch.node_a)
        linked.add(branch.node_b)
    return [n for n in mesh.node_coords if n not in linked]


def _antipad_pour(hole_mm):
    """A square pour with a hole in it, written the way KiCad writes one.

    KiCad has no separate hole record: it walks the outer ring, slits inward
    and traverses the hole the other way round. Shapely reads that back as an
    interior ring once `buffer(0)` has repaired the self-touch, which is what
    the mesher relies on.
    """
    from ingest.board_reader import Zone

    low, high = 4.5 - hole_mm / 2, 4.5 + hole_mm / 2
    ring = [(2, 2), (7, 2), (7, 7), (2, 7)] + [
        (2, 4.5), (low, 4.5), (low, high), (high, high),
        (high, low), (low, low), (low, 4.5), (2, 4.5),
    ]
    return Zone("VCC", "F.Cu", filled_polygon=ring)


class LatticeNodesFollowTheCopper(unittest.TestCase):
    def setUp(self):
        try:
            import numpy, shapely  # noqa: F401
        except ImportError:
            self.skipTest("numpy/shapely not available")

    def test_the_empty_corners_of_a_pour_get_no_nodes(self):
        # A diamond has the bounding box of the square around it and half its
        # area. Every lattice point in the four corners used to become its own
        # component: 1,200 of them at a 0.1 mm step.
        from ingest.board_reader import Zone
        from mesh_hybrid import HybridMesher

        diamond = Zone("VCC", "F.Cu",
                       filled_polygon=[(4.5, 2), (7, 4.5), (4.5, 7), (2, 4.5)])
        mesh = HybridMesher(_zone_nodes_board([diamond]), grid_step_mm=0.1).build_mesh("VCC")

        self.assertEqual(_orphans(mesh), [])

    def test_an_antipad_hole_gets_no_nodes(self):
        from mesh_hybrid import HybridMesher

        mesh = HybridMesher(
            _zone_nodes_board([_antipad_pour(0.6)]), grid_step_mm=0.1,
        ).build_mesh("VCC")

        self.assertEqual(_orphans(mesh), [])

    def test_dropping_them_removes_no_conduction_path(self):
        # The point of the fix is that it deletes nodes and nothing else. If a
        # branch count moved, the cut-cell conductances moved with it and the
        # rail's resistance would have changed.
        from mesh_hybrid import HybridMesher

        mesh = HybridMesher(
            _zone_nodes_board([_antipad_pour(0.6)]), grid_step_mm=0.1,
        ).build_mesh("VCC")

        adjacency = defaultdict(set)
        for branch in mesh.branches:
            adjacency[branch.node_a].add(branch.node_b)
            adjacency[branch.node_b].add(branch.node_a)
        seen, stack = {next(iter(mesh.node_coords))}, [next(iter(mesh.node_coords))]
        while stack:
            for neighbour in adjacency[stack.pop()]:
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        self.assertEqual(len(seen), len(mesh.node_coords),
                         "the pour is one piece of copper and must mesh as one")

    def test_a_pad_over_a_hole_in_the_pour_does_not_join_it(self):
        # The damage, not just the waste. `probe` decides whether a pad touches
        # copper by looking for a lattice node near it; a phantom node inside
        # the hole made a pad sitting in that hole look connected to the pour,
        # and it then minted a barrel down to a node no current can reach.
        from ingest.board_reader import Footprint, Pad
        from mesh_hybrid import HybridMesher

        pad = Pad(number="1", net_name="VCC", pintype="passive",
                  position=(4.5, 4.5), pad_type="thru_hole",
                  layers=("*.Cu",), drill_mm=0.3)
        footprint = Footprint(
            reference="J1", value="", lib_id="", position=(0.0, 0.0),
            rotation=0.0, layer="F.Cu", sch_path="", sch_sheet_file="",
            pads=[pad],
        )
        mesh = HybridMesher(
            _zone_nodes_board([_antipad_pour(1.0)], [footprint]), grid_step_mm=0.1,
        ).build_mesh("VCC")

        self.assertEqual(
            [b for b in mesh.branches if str(b.geometry_source).startswith("pad:")],
            [], "a pad in a clearance hole touches no copper of its net",
        )


# ---------------------------------------------------------------------------
# From test_mesh_hybrid_zone_union.py
# ---------------------------------------------------------------------------
# A net's pours mesh as one copper union per layer, on one aligned lattice.
#
# Once the board reader stopped dropping islands and multi-layer zones (on the
# reference board it returned 24 zones where the file holds 40), meshing each zone
# on its own lattice stopped being tenable: two overlapping zones of a net would
# both conduct through the copper they share, and islands would sit on lattices
# offset from the index tracks attach through. The row-vectorised cell fractions
# that replaced one Shapely intersection per edge must give the same numbers.


def _zone_union_board(zones):
    from ingest.board_reader import BoardBounds, CopperLayer, ParsedBoard, Stackup

    return ParsedBoard(
        pcb_path=None,
        stackup=Stackup(layers=[CopperLayer(0, "F.Cu", "signal", 0.035)],
                        total_thickness_mm=1.6, copper_layer_count=1),
        bounds=BoardBounds(0, 0, 20, 20), footprints=[], segments=[], vias=[],
        zones=list(zones),
    )


def _zone_conductance(mesh):
    return sum(1.0 / branch.resistance_ohm for branch in mesh.branches
               if str(branch.geometry_source).startswith("zone:"))


class ZoneUnionMeshing(unittest.TestCase):
    def setUp(self):
        try:
            import numpy, shapely  # noqa: F401
        except ImportError:
            self.skipTest("numpy/shapely not available")

    def test_row_fractions_match_the_cell_by_cell_intersection(self):
        from shapely import Polygon, box, intersection
        from ingest.board_reader import Zone
        from mesh_hybrid import HybridMesher, RHO_COPPER

        ring = [(1.13, 1.07), (8.9, 1.4), (7.2, 6.3), (4.0, 8.7), (1.5, 5.1)]
        step = 0.25
        mesh = HybridMesher(_zone_union_board([Zone("VCC", "F.Cu", filled_polygon=ring)]),
                            grid_step_mm=step).build_mesh("VCC")
        copper = Polygon(ring)
        sigma_t = 0.035 / (RHO_COPPER * 1e3)
        checked = 0
        for branch in mesh.branches:
            xa, ya, _ = mesh.node_coords[branch.node_a]
            xb, yb, _ = mesh.node_coords[branch.node_b]
            if ya == yb:
                cell = box(min(xa, xb), ya - step / 2, max(xa, xb), ya + step / 2)
            else:
                cell = box(xa - step / 2, min(ya, yb), xa + step / 2, max(ya, yb))
            expected = sigma_t * intersection(copper, cell).area / (step * step)
            self.assertAlmostEqual(1.0 / branch.resistance_ohm, expected, delta=1e-9 * expected)
            checked += 1
        self.assertGreater(checked, 500)

    def test_overlapping_zones_do_not_count_shared_copper_twice(self):
        from ingest.board_reader import Zone
        from mesh_hybrid import HybridMesher

        left = Zone("VCC", "F.Cu", filled_polygon=[(1, 1), (6, 1), (6, 5), (1, 5)])
        right = Zone("VCC", "F.Cu", filled_polygon=[(4, 1), (9, 1), (9, 5), (4, 5)])
        union = Zone("VCC", "F.Cu", filled_polygon=[(1, 1), (9, 1), (9, 5), (1, 5)])
        both = HybridMesher(_zone_union_board([left, right]), grid_step_mm=0.2).build_mesh("VCC")
        single = HybridMesher(_zone_union_board([union]), grid_step_mm=0.2).build_mesh("VCC")
        self.assertEqual(len(both.branches), len(single.branches))
        self.assertAlmostEqual(_zone_conductance(both), _zone_conductance(single),
                               delta=1e-9 * _zone_conductance(single))

    def test_every_island_is_meshed(self):
        from ingest.board_reader import Zone
        from mesh_hybrid import HybridMesher

        islands = [
            Zone("VCC", "F.Cu", filled_polygon=[(1, 1), (4, 1), (4, 4), (1, 4)]),
            Zone("VCC", "F.Cu", filled_polygon=[(12, 12), (15, 12), (15, 15), (12, 15)]),
        ]
        mesh = HybridMesher(_zone_union_board(islands), grid_step_mm=0.5).build_mesh("VCC")
        xs = [coord[0] for coord in mesh.node_coords.values()]
        self.assertLess(min(xs), 5.0)
        self.assertGreater(max(xs), 11.0)

    def test_the_lattice_sits_on_multiples_of_the_step(self):
        from ingest.board_reader import Zone
        from mesh_hybrid import HybridMesher

        step = 0.3
        zone = Zone("VCC", "F.Cu", filled_polygon=[(1.07, 2.11), (5.5, 2.11), (5.5, 6.2), (1.07, 6.2)])
        mesh = HybridMesher(_zone_union_board([zone]), grid_step_mm=step).build_mesh("VCC")
        for x, y, _layer in mesh.node_coords.values():
            self.assertAlmostEqual(x / step, round(x / step), delta=0.01)
            self.assertAlmostEqual(y / step, round(y / step), delta=0.01)


if __name__ == "__main__":
    unittest.main()
