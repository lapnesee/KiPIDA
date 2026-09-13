"""A net's pours mesh as one copper union per layer, on one aligned lattice.

Once the board reader stopped dropping islands and multi-layer zones (on the
reference board it returned 24 zones where the file holds 40), meshing each zone
on its own lattice stopped being tenable: two overlapping zones of a net would
both conduct through the copper they share, and islands would sit on lattices
offset from the index tracks attach through. The row-vectorised cell fractions
that replaced one Shapely intersection per edge must give the same numbers.
"""

import math
import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


def _board(zones):
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
        mesh = HybridMesher(_board([Zone("VCC", "F.Cu", filled_polygon=ring)]),
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
        both = HybridMesher(_board([left, right]), grid_step_mm=0.2).build_mesh("VCC")
        single = HybridMesher(_board([union]), grid_step_mm=0.2).build_mesh("VCC")
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
        mesh = HybridMesher(_board(islands), grid_step_mm=0.5).build_mesh("VCC")
        xs = [coord[0] for coord in mesh.node_coords.values()]
        self.assertLess(min(xs), 5.0)
        self.assertGreater(max(xs), 11.0)

    def test_the_lattice_sits_on_multiples_of_the_step(self):
        from ingest.board_reader import Zone
        from mesh_hybrid import HybridMesher

        step = 0.3
        zone = Zone("VCC", "F.Cu", filled_polygon=[(1.07, 2.11), (5.5, 2.11), (5.5, 6.2), (1.07, 6.2)])
        mesh = HybridMesher(_board([zone]), grid_step_mm=step).build_mesh("VCC")
        for x, y, _layer in mesh.node_coords.values():
            self.assertAlmostEqual(x / step, round(x / step), delta=0.01)
            self.assertAlmostEqual(y / step, round(y / step), delta=0.01)


if __name__ == "__main__":
    unittest.main()
