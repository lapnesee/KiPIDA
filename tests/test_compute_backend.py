import unittest
from unittest.mock import patch

import numpy as np
import scipy.sparse

from compute_backend import SparseComputeBackend
from runtime_config import RuntimeComputeSettings


class ComputeBackendTests(unittest.TestCase):
    def setUp(self):
        self.matrix = scipy.sparse.diags(
            (-np.ones(31), 2.1 * np.ones(32), -np.ones(31)),
            offsets=(-1, 0, 1), format="csr",
        )
        self.rhs = np.ones(32)

    def test_cpu_backend_solves_with_small_residual(self):
        result = SparseComputeBackend(
            RuntimeComputeSettings(backend="CPU", cpu_multithread=False)
        ).solve(self.matrix, self.rhs, "SPD")
        self.assertTrue(result.metadata.backend.startswith("CPU_"))
        self.assertEqual(result.metadata.cpu_threads, 1)
        self.assertLess(result.metadata.relative_residual, 1.0e-10)

    def test_forced_cuda_requires_activation(self):
        settings = RuntimeComputeSettings(backend="CUDA", cuda_enabled=False)
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            SparseComputeBackend(settings).solve(self.matrix, self.rhs)

    @patch("compute_backend.cuda_diagnostics")
    def test_forced_cuda_reports_unavailable_device(self, diagnostics):
        diagnostics.return_value = {
            "available": False, "devices": [], "error": "driver missing",
        }
        settings = RuntimeComputeSettings(backend="CUDA", cuda_enabled=True)
        with self.assertRaisesRegex(RuntimeError, "driver missing"):
            SparseComputeBackend(settings).solve(self.matrix, self.rhs)

    @patch("compute_backend.cuda_diagnostics")
    def test_auto_falls_back_to_cpu_after_cuda_failure(self, diagnostics):
        diagnostics.return_value = {"available": True, "devices": [{}], "error": ""}
        settings = RuntimeComputeSettings(
            backend="AUTO", cuda_enabled=True, cuda_min_nodes=1,
        )
        backend = SparseComputeBackend(settings)
        with patch.object(backend, "_solve_cuda", side_effect=RuntimeError("GPU OOM")):
            result = backend.solve(self.matrix, self.rhs)
        self.assertTrue(result.metadata.backend.startswith("CPU_"))
        self.assertEqual(result.metadata.fallback_reason, "GPU OOM")


if __name__ == "__main__":
    unittest.main()
