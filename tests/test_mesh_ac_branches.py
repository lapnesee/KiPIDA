import os
import sys
import unittest

plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

from mesh import Mesh


class TestMeshACBranches(unittest.TestCase):
    def test_edge_retains_r_and_l_without_changing_dc_stamp(self):
        mesh = Mesh()
        mesh.add_edge_direct(0, 1, 4.0, inductance_h=0.5e-9, kind="via")

        self.assertEqual(mesh.G_coo_row, [0, 1, 0, 1])
        self.assertEqual(mesh.G_coo_col, [0, 1, 1, 0])
        self.assertEqual(mesh.G_coo_data, [4.0, 4.0, -4.0, -4.0])
        self.assertEqual(len(mesh.branches), 1)
        self.assertAlmostEqual(mesh.branches[0].resistance_ohm, 0.25)
        self.assertAlmostEqual(mesh.branches[0].inductance_h, 0.5e-9)
        self.assertEqual(mesh.branches[0].kind, "via")



def plane(layer, step=0.5, count=20):
    mesh = Mesh()
    mesh.grid_step = step
    mesh.grid_origin = (0.0, 0.0)
    for index in range(count * count):
        ix, iy = divmod(index, count)
        mesh.nodes.append(index)
        mesh.node_coords[index] = (ix * step, iy * step, layer)
        mesh.node_map[(ix, iy, layer)] = index
    return mesh


class TestCavityCapacitance(unittest.TestCase):
    def test_a_plane_pair_is_a_parallel_plate_capacitor(self):
        # The AC model had no plane-pair capacitance at all (open since the
        # 2026-09-04 audit). A 10 x 10 mm rail pour 0.1 mm over its return
        # with er 4.1 -- P02 Rev E's prepreg -- must total eps0 er A / h.
        from mesh import Mesher

        stackup = {"substrate": [{"between": [0, 1], "thickness_mm": 0.1, "epsilon_r": 4.1}]}
        pairs = Mesher.__new__(Mesher).cavity_capacitor_pairs(plane(0), plane(1), stackup)
        total = sum(capacitance for _rail, _ground, capacitance in pairs)
        expected = 8.8541878128e-12 * 4.1 * (10e-3 * 10e-3) / 0.1e-3
        self.assertAlmostEqual(total / expected, 1.0, places=6)

    def test_a_return_via_land_on_the_rail_layer_is_not_its_return(self):
        # P02 Rev E: GND has via lands on In2.Cu, the /VBUS_SYS pour's own
        # layer, so its return was found 0 mm away: no plane inductance on
        # either mesh and no cavity capacitance, and a worst case of
        # 81.6 mohm (PASS) instead of 271.8 mohm (FAIL). The return of the
        # pour is the GND plane 0.35 mm away on In1.Cu.
        import math
        from mesh import Mesher

        rail, ground = plane(2), plane(1)
        ground.node_map[(0, 0, 2)] = len(ground.nodes)
        ground.node_coords[len(ground.nodes)] = (0.0, 0.0, 2)
        ground.nodes.append(len(ground.nodes))
        rail.add_edge_direct(0, 1, 0.035 / 1.72e-5)
        stackup = {
            "substrate": [{"between": [1, 2], "thickness_mm": 0.35, "epsilon_r": 4.6}],
            "copper": {2: {"thickness_mm": 0.035}}, "resistivity": 1.72e-5,
        }
        mesher = Mesher.__new__(Mesher)

        mesher.assign_loop_inductance(rail, ground, stackup)

        self.assertAlmostEqual(rail.branches[0].inductance_h, 4e-7 * math.pi * 0.35e-3 / 2)
        self.assertEqual(len(mesher.cavity_capacitor_pairs(rail, ground, stackup)), 400)

if __name__ == "__main__":
    unittest.main()
