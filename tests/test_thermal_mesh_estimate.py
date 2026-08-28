import unittest

from thermal_mesh import estimate_thermal_mesh_cost


class ThermalMeshEstimateTests(unittest.TestCase):
    def test_estimate_scales_quadratically_with_grid_resolution(self):
        context = {"width_mm": 100, "height_mm": 80, "thermal_layers": 7}
        normal = estimate_thermal_mesh_cost(context, 1.0)
        fine = estimate_thermal_mesh_cost(context, 0.5)
        self.assertEqual(fine["nodes"], normal["nodes"] * 4)
        self.assertGreater(fine["cpu_bytes"], normal["cpu_bytes"] * 3)

    def test_super_resolution_is_estimated_without_clamping_to_point_one(self):
        context = {"width_mm": 10.0, "height_mm": 10.0, "thermal_layers": 3}
        super_fine = estimate_thermal_mesh_cost(context, 0.01)
        legacy_floor = estimate_thermal_mesh_cost(context, 0.1)
        self.assertEqual(super_fine["nodes"], legacy_floor["nodes"] * 100)

    def test_recommends_cuda_only_when_available_and_above_threshold(self):
        context = {
            "width_mm": 200, "height_mm": 100, "thermal_layers": 11,
            "cuda_available": True, "cuda_min_nodes": 100000,
        }
        self.assertEqual(estimate_thermal_mesh_cost(context, 1.0)["backend"], "CUDA")
        context["cuda_available"] = False
        self.assertEqual(estimate_thermal_mesh_cost(context, 1.0)["backend"], "CPU")

    def test_explicit_ram_ceiling_controls_the_thermal_node_limit(self):
        context = {
            "width_mm": 400, "height_mm": 300, "thermal_layers": 11,
            "cuda_available": True, "memory_limit_gib": 1.0,
        }
        estimate = estimate_thermal_mesh_cost(context, 0.5)
        self.assertLess(estimate["node_limit"], 1250000)
        self.assertTrue(estimate["exceeds_memory_limit"])


if __name__ == "__main__":
    unittest.main()
