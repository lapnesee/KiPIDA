"""Pads are copper, and the mesher used to ignore them entirely.

HybridMesher modelled segments, vias and zones. A through-hole pad shorts every
copper layer it passes exactly as a via does, so leaving pads out meant a track
ending on one had no path to the plane beneath it.

On the reference board that stranded loads on four of seven rails, while the
production rasterising mesher -- which does index pads -- reported no exclusions
on the same copper. +3V3_MAIN's load sat at J6 pad 3, `thru_hole` on `*.Cu`,
0.02 mm above a plane it could not reach.
"""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


def _board(footprints=(), zones=(), segments=()):
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

        board = _board(
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

    def test_a_pad_touching_no_copper_is_left_out(self):
        # Meshing pads unconditionally moved +5V_RAIL's source Q3 onto three
        # one-node components while its load sat in the 113,504-node network,
        # turning a solvable rail unreachable: find_node_at picks the nearest
        # node, and a private node exactly on the pad beats the real one a
        # fraction of a millimetre away.
        from ingest.board_reader import Pad
        from mesh_hybrid import HybridMesher

        board = _board(footprints=[_footprint([
            Pad(number="3", net_name="VCC", pintype="passive",
                position=(5.0, 5.0), pad_type="thru_hole",
                layers=("*.Cu",), drill_mm=0.8),
        ])])
        mesh = HybridMesher(board).build_mesh("VCC")

        self.assertEqual(mesh.node_coords, {})

    def test_a_surface_pad_adds_no_vertical_path(self):
        from ingest.board_reader import Pad
        from mesh_hybrid import HybridMesher

        board = _board(footprints=[_footprint([
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

        board = _board(
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
        board = _board(
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


if __name__ == "__main__":
    unittest.main()
