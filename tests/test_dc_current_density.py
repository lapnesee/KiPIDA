from types import SimpleNamespace
import unittest

from shapely.geometry import box

from application.dc_current_density import calculate_current_density
from mesh import Mesh
from models import DCSolveResult


class DCCurrentDensityTests(unittest.TestCase):
    @staticmethod
    def planar_mesh(current=0.2, resistance=0.5):
        mesh = Mesh()
        mesh.nodes = [0, 1]
        mesh.node_coords = {0: (0.0, 0.0, 0), 1: (0.1, 0.0, 0)}
        mesh.node_map = {(0, 0, 0): 0, (1, 0, 0): 1}
        mesh.grid_origin = (0.0, 0.0)
        mesh.grid_step = 0.1
        mesh.add_edge_direct(0, 1, 1.0 / resistance, kind="lateral")
        detailed = DCSolveResult(
            branch_currents_a=[current],
            branch_losses_w=[current * current * resistance],
        )
        return mesh, detailed

    @staticmethod
    def stackup(thickness=0.035):
        return {
            "copper": {0: {"name": "F.Cu", "thickness_mm": thickness}},
            "trustworthy": True, "warnings": [],
        }

    def test_uniform_branch_matches_analytic_density(self):
        mesh, detailed = self.planar_mesh()
        result = calculate_current_density(mesh, detailed, self.stackup())
        self.assertAlmostEqual(result.maximum_planar_a_per_mm2, 57.142857142857, places=9)
        self.assertAlmostEqual(result.percentile_99_5_a_per_mm2, 57.142857142857, places=9)

    def test_double_copper_thickness_halves_density(self):
        mesh, detailed = self.planar_mesh()
        thin = calculate_current_density(mesh, detailed, self.stackup(0.035))
        thick = calculate_current_density(mesh, detailed, self.stackup(0.070))
        self.assertAlmostEqual(thick.maximum_planar_a_per_mm2, thin.maximum_planar_a_per_mm2 / 2.0)

    def test_branch_power_matches_i_squared_r(self):
        mesh, detailed = self.planar_mesh(current=0.2, resistance=0.5)
        result = calculate_current_density(mesh, detailed, self.stackup())
        self.assertAlmostEqual(result.planar_samples[0].loss_w, 0.2 * 0.2 * 0.5)
        self.assertAlmostEqual(
            result.planar_samples[0].current_a ** 2 * mesh.branches[0].resistance_ohm,
            result.planar_samples[0].loss_w,
        )

    def test_routes_zones_and_overlap_are_classified(self):
        mesh, detailed = self.planar_mesh()
        geometry = {
            "track": {0: box(0.0, -0.1, 0.1, 0.1)},
            "zone": {0: box(0.0, -0.1, 0.1, 0.1)},
        }
        result = calculate_current_density(mesh, detailed, self.stackup(), geometry)
        self.assertEqual(result.planar_samples[0].copper_kind, "TRACK+ZONE")
        self.assertEqual(result.maximum_track_a_per_mm2, result.maximum_planar_a_per_mm2)
        self.assertEqual(result.maximum_zone_a_per_mm2, result.maximum_planar_a_per_mm2)

    def test_missing_or_invalid_geometry_is_reported(self):
        mesh, detailed = self.planar_mesh()
        result = calculate_current_density(
            mesh, detailed, {"copper": {0: {"thickness_mm": 0.0}}, "trustworthy": False},
        )
        self.assertFalse(result.planar_samples)
        self.assertTrue(any("Missing or invalid copper thickness" in item for item in result.warnings))
        self.assertTrue(any("not verified" in item for item in result.warnings))

    def test_vertical_branches_are_not_mixed_with_planar_density(self):
        mesh = Mesh()
        mesh.nodes = [0, 1]
        mesh.node_coords = {0: (2.0, 3.0, 0), 1: (2.0, 3.0, 31)}
        mesh.grid_step = 0.1
        mesh.add_edge_direct(
            0, 1, 10.0, kind="via", cross_section_mm2=0.01,
            geometry_source="PCB_DRILL+ESTIMATED_PLATING_0.025MM",
        )
        detailed = DCSolveResult(branch_currents_a=[0.2], branch_losses_w=[0.004])
        result = calculate_current_density(mesh, detailed, self.stackup())
        self.assertFalse(result.planar_samples)
        self.assertEqual(len(result.vertical_samples), 1)
        self.assertAlmostEqual(result.maximum_via_current_a, 0.2)
        self.assertAlmostEqual(result.maximum_via_a_per_mm2, 20.0)
        self.assertTrue(any("plating-thickness estimate" in item for item in result.warnings))

    def test_disconnected_nodes_do_not_create_false_density(self):
        mesh, _detailed = self.planar_mesh()
        result = calculate_current_density(
            mesh, DCSolveResult(branch_currents_a=[0.0], branch_losses_w=[0.0]),
            self.stackup(),
        )
        self.assertEqual(result.maximum_planar_a_per_mm2, 0.0)
        self.assertTrue(all(value == 0.0 for value in result.node_density_a_per_mm2.values()))

    def test_right_angle_map_does_not_invent_vector_peak(self):
        mesh = Mesh()
        mesh.nodes = [0, 1, 2]
        mesh.node_coords = {
            0: (0.0, 0.0, 0), 1: (0.1, 0.0, 0), 2: (0.1, 0.1, 0),
        }
        mesh.grid_step = 0.1
        mesh.add_edge_direct(0, 1, 2.0, kind="lateral")
        mesh.add_edge_direct(1, 2, 2.0, kind="lateral")
        result = calculate_current_density(
            mesh, DCSolveResult(branch_currents_a=[0.2, 0.2]), self.stackup(),
        )
        self.assertAlmostEqual(
            result.node_density_a_per_mm2[1], result.maximum_planar_a_per_mm2,
        )


if __name__ == "__main__":
    unittest.main()
