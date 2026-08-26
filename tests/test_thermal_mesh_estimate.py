import unittest

from thermal_mesh import estimate_thermal_mesh_cost


class ThermalMeshEstimateTests(unittest.TestCase):
    def test_estimate_scales_quadratically_with_grid_resolution(self):
        context = {"width_mm": 100, "height_mm": 80, "thermal_layers": 7}
        normal = estimate_thermal_mesh_cost(context, 1.0)
        fine = estimate_thermal_mesh_cost(context, 0.5)
        self.assertEqual(fine["nodes"], normal["nodes"] * 4)
        self.assertGreater(fine["cpu_bytes"], normal["cpu_bytes"] * 3)

    def test_recommends_cuda_only_when_available_and_above_threshold(self):
        context = {
            "width_mm": 200, "height_mm": 100, "thermal_layers": 11,
            "cuda_available": True, "cuda_min_nodes": 100000,
        }
        self.assertEqual(estimate_thermal_mesh_cost(context, 1.0)["backend"], "CUDA")
        context["cuda_available"] = False
        self.assertEqual(estimate_thermal_mesh_cost(context, 1.0)["backend"], "CPU")


if __name__ == "__main__":
    unittest.main()
