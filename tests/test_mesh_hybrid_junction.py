"""Tracks, planes and vias must form one electrical network.

Two defects kept them apart on the reference board, and together they made
every rail unsolvable by the advisor:

* track endpoints snap to 1 um while zone nodes sit on the 0.1 mm cut-cell
  grid, so the two lattices only ever coincided by accident;
* a through via created nodes on its two named layers only, skipping the
  inner layers it physically shorts -- so tracks on F.Cu/B.Cu could not
  reach the rail's plane on In2.Cu.

One real rail split into 63,897 connected components as a result.
"""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


def _board(segments=(), vias=(), zones=()):
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
        board = _board(
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


class ThroughViaSpanTests(unittest.TestCase):
    def setUp(self):
        try:
            import numpy, shapely  # noqa: F401
        except ImportError:
            self.skipTest("numpy/shapely not available")

    def test_a_through_via_touches_the_inner_layers_it_crosses(self):
        from ingest.board_reader import Via
        from mesh_hybrid import HybridMesher

        board = _board(vias=[Via("VCC", (1.0, 1.0), 0.6, 0.3, ["F.Cu", "B.Cu"])])
        mesh = HybridMesher(board).build_mesh("VCC")

        layers = {coord[2] for coord in mesh.node_coords.values()}
        self.assertEqual(
            layers, {0, 4, 6, 2},
            "a via named F.Cu-B.Cu must still reach the inner layers it drills through",
        )
        # Three hops for four layers, chained rather than a single jump.
        self.assertEqual(len(mesh.branches), 3)

    def test_the_barrel_resistance_is_shared_across_the_hops(self):
        # Splitting one barrel into N hops must not multiply its resistance.
        from ingest.board_reader import Via
        from ingest.track_resistance import via_resistance
        from mesh_hybrid import HybridMesher

        board = _board(vias=[Via("VCC", (1.0, 1.0), 0.6, 0.3, ["F.Cu", "B.Cu"])])
        mesh = HybridMesher(board).build_mesh("VCC")

        total = sum(b.resistance_ohm for b in mesh.branches)
        expected = via_resistance(1.6, 0.3, 0.15)
        self.assertAlmostEqual(total, expected, places=12)


if __name__ == "__main__":
    unittest.main()
