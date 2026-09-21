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

    def test_a_track_ending_off_centre_on_a_via_land_reaches_the_via(self):
        # R9's feed on +3V3AON stops 0.14 mm from a 0.7 mm via's centre.
        from ingest.board_reader import Segment, Via
        from mesh_hybrid import HybridMesher

        board = _board(
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

        board = _board(
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
        board = _board(segments=[Segment("VCC", 0.5, "F.Cu", (1.0, 5.0), (9.0, 5.0))])
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
        board = _board(zones=[zone])
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

        board = _board(vias=[Via("VCC", (1.0, 1.0), 0.6, 0.3, ["F.Cu", "B.Cu"])])
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

        board = _board(vias=[Via("VCC", (1.0, 1.0), 0.6, 0.3, ["F.Cu", "B.Cu"])])
        mesh = HybridMesher(board).build_mesh("VCC")

        total = sum(b.resistance_ohm for b in mesh.branches if b.kind == "vertical")
        # The plating is the shared fallback. This expectation used to be 0.15
        # mm -- the via's annular ring, which is not the barrel wall.
        expected = via_resistance(1.6, 0.3, PLATING_THICKNESS_FALLBACK_MM)
        self.assertAlmostEqual(total, expected, places=12)


if __name__ == "__main__":
    unittest.main()
