import unittest

import numpy as np

from compute_backend import cuda_diagnostics
from runtime_config import RuntimeComputeSettings
from solver import Solver


class StructuredPowerMesh:
    """Large planar rail with the same Laplacian structure as a copper mesh."""

    def __init__(self, width=256, height=256):
        node_count = width * height
        self.nodes = list(range(node_count))
        self.edges = []
        self.branches = []

        grid = np.arange(node_count, dtype=np.int64).reshape(height, width)
        horizontal_a = grid[:, :-1].reshape(-1)
        horizontal_b = grid[:, 1:].reshape(-1)
        vertical_a = grid[:-1, :].reshape(-1)
        vertical_b = grid[1:, :].reshape(-1)
        a = np.concatenate((horizontal_a, vertical_a))
        b = np.concatenate((horizontal_b, vertical_b))

        # Include a realistic conductance spread.  Jacobi-preconditioned CG
        # must converge without falling back merely because some copper paths
        # are much stronger than others.
        conductance = np.ones(len(a), dtype=np.float64)
        conductance[::97] = 1.0e3
        self.G_coo_row = np.concatenate((a, b, a, b))
        self.G_coo_col = np.concatenate((a, b, b, a))
        self.G_coo_data = np.concatenate((
            conductance, conductance, -conductance, -conductance,
        ))


@unittest.skipUnless(cuda_diagnostics()["available"], "CuPy CUDA runtime unavailable")
class CudaDCRailRegressionTests(unittest.TestCase):
    def test_large_five_volt_rail_converges_with_cg_and_matches_cpu(self):
        width = 256
        height = 256
        mesh = StructuredPowerMesh(width, height)
        sources = [
            {"node_id": row * width, "voltage": 5.0}
            for row in range(height)
        ]
        loads = [
            {"node_id": row * width + width - 1, "current": 4.0 / height}
            for row in range(height)
        ]

        cuda = Solver(compute_settings=RuntimeComputeSettings(
            backend="CUDA",
            cuda_enabled=True,
            cuda_min_nodes=1,
            solver_rtol=1.0e-8,
            solver_max_iterations=5000,
        ))
        cuda_values = cuda.solve(mesh, sources, loads)

        self.assertEqual(cuda.last_compute.backend, "CUDA_CUPY")
        self.assertEqual(cuda.last_compute.solver_method, "CG")
        self.assertTrue(cuda.last_compute.converged)
        self.assertFalse(cuda.last_compute.fallback_reason)
        self.assertLessEqual(cuda.last_compute.relative_residual, 5.0e-8)

        cpu = Solver(compute_settings=RuntimeComputeSettings(
            backend="CPU", cpu_multithread=True,
        ))
        cpu_values = cpu.solve(mesh, sources, loads)
        cuda_vector = np.fromiter((cuda_values[index] for index in range(width * height)), float)
        cpu_vector = np.fromiter((cpu_values[index] for index in range(width * height)), float)
        # Iterative CG and direct PARDISO need engineering-equivalent voltage
        # fields, not bit-level agreement on this deliberately ill-conditioned
        # conductance mesh.  Keep the absolute rail error below 0.2 mV.
        np.testing.assert_allclose(cuda_vector, cpu_vector, rtol=2.0e-4, atol=2.0e-4)

        left = np.mean(cuda_vector.reshape(height, width)[:, 0])
        right = np.mean(cuda_vector.reshape(height, width)[:, -1])
        self.assertAlmostEqual(left, 5.0, places=12)
        self.assertLess(right, left)


if __name__ == "__main__":
    unittest.main()
