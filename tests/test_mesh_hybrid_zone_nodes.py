"""Zone lattice nodes must follow the copper, not the bounding box.

`_mesh_zone_cutcell` grids the pour's *bounding box* and used to mint a node at
every lattice point of it. A pour rarely fills its own bounding box, so nodes
landed where the net has no copper: in the empty corners of a non-rectangular
pour, and inside every antipad hole. A branch only exists where a cell overlaps
copper, so none of those nodes could ever gain one, and each became a connected
component of exactly one node.

That is item A4: +3V3_MAIN on the reference board meshes to 902 connected
components, 900 of them isolated single nodes.

They are not only waste. Lattice nodes are indexed as plane nodes, and `probe`
reads that index to decide whether a pad meets copper at all -- so a phantom
node let a pad bind to a pour that is not there.
"""

import os
import sys
import unittest
from collections import defaultdict

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


def _board(zones=(), footprints=()):
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
        mesh = HybridMesher(_board([diamond]), grid_step_mm=0.1).build_mesh("VCC")

        self.assertEqual(_orphans(mesh), [])

    def test_an_antipad_hole_gets_no_nodes(self):
        from mesh_hybrid import HybridMesher

        mesh = HybridMesher(
            _board([_antipad_pour(0.6)]), grid_step_mm=0.1,
        ).build_mesh("VCC")

        self.assertEqual(_orphans(mesh), [])

    def test_dropping_them_removes_no_conduction_path(self):
        # The point of the fix is that it deletes nodes and nothing else. If a
        # branch count moved, the cut-cell conductances moved with it and the
        # rail's resistance would have changed.
        from mesh_hybrid import HybridMesher

        mesh = HybridMesher(
            _board([_antipad_pour(0.6)]), grid_step_mm=0.1,
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
            _board([_antipad_pour(1.0)], [footprint]), grid_step_mm=0.1,
        ).build_mesh("VCC")

        self.assertEqual(
            [b for b in mesh.branches if str(b.geometry_source).startswith("pad:")],
            [], "a pad in a clearance hole touches no copper of its net",
        )


if __name__ == "__main__":
    unittest.main()
