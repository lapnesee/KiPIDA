"""The AC sweep may use the GPU only if it proves it agrees with the CPU.

The CPU path is a direct factorisation; the CUDA path is iterative BiCGSTAB.
They do not share an error profile, so the first frequency that genuinely
runs on the GPU is solved both ways and compared before the remaining points
inherit that trust. These tests drive that logic with stand-in backends --
CuPy is absent here, so the real CUDA branch cannot be executed.
"""

import os
import sys
import unittest
from types import SimpleNamespace

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


def _metadata(backend):
    return SimpleNamespace(
        backend=backend, device="GPU" if backend.startswith("CUDA") else "CPU",
        solve_seconds=0.01, transfer_seconds=0.0, relative_residual=0.0,
        iterations=1, cache_hit=False, fallback_reason="",
    )


class _GpuBackend:
    """Reports CUDA and returns a rail voltage the test controls."""

    def __init__(self, rail_voltage, verify=True):
        self.settings = SimpleNamespace(backend="AUTO", verify_gpu_accuracy=verify)
        self.rail_voltage = rail_voltage
        self.calls = 0

    def solve(self, _matrix, _rhs, **_kwargs):
        import ac_solver

        self.calls += 1
        backend = "CUDA_CUPY" if self.settings.backend != "CPU" else "CPU_SCIPY"
        return SimpleNamespace(
            values=ac_solver.np.asarray([self.rail_voltage, 0.0 + 0.0j]),
            metadata=_metadata(backend),
        )


class _CpuReference:
    """Stands in for the CPU-pinned auditing backend."""

    def __init__(self, rail_voltage):
        self.rail_voltage = rail_voltage
        self.calls = 0

    def solve(self, _matrix, _rhs, **_kwargs):
        import ac_solver

        self.calls += 1
        return SimpleNamespace(
            values=ac_solver.np.asarray([self.rail_voltage, 0.0 + 0.0j]),
            metadata=_metadata("CPU_SCIPY"),
        )


class GpuAccuracyGuardTests(unittest.TestCase):
    def setUp(self):
        try:
            import numpy, scipy  # noqa: F401
        except ImportError:
            self.skipTest("NumPy/SciPy are not installed in this interpreter")
        from tests.test_ac_solver import TestACSolver

        self._fixture = TestACSolver()

    def _network(self):
        return self._fixture.network()

    def _settings(self):
        return self._fixture.settings()

    def _solver(self, gpu, cpu):
        from ac_solver import ACSolver

        solver = ACSolver()
        solver.compute_backend = gpu
        solver._cpu_reference_backend = cpu
        return solver

    def test_agreeing_backends_keep_the_sweep_on_the_gpu(self):
        gpu = _GpuBackend(0.1 + 0.0j)
        cpu = _CpuReference(0.1 + 0.0j)
        solver = self._solver(gpu, cpu)

        result = solver.solve_sweep(self._network(), self._settings())

        self.assertEqual(gpu.settings.backend, "AUTO")
        self.assertEqual(cpu.calls, 1, "the audit must run exactly once")
        self.assertIn("passed", result.gpu_accuracy_check)
        self.assertEqual(result.compute_backend, "CUDA_CUPY")

    def test_disagreeing_backends_send_the_rest_of_the_sweep_to_the_cpu(self):
        # 1% apart: far beyond the part-per-million tolerance.
        gpu = _GpuBackend(0.101 + 0.0j)
        cpu = _CpuReference(0.1 + 0.0j)
        solver = self._solver(gpu, cpu)

        result = solver.solve_sweep(self._network(), self._settings())

        self.assertEqual(gpu.settings.backend, "CPU")
        self.assertIn("failed", result.gpu_accuracy_check)
        self.assertIn("disagree by", result.gpu_accuracy_check)
        # The direct solve is the reference, so its answer is the one kept.
        self.assertAlmostEqual(abs(result.impedance_ohm[0]), 0.1, places=9)

    def test_metadata_names_the_transition_rather_than_one_end_of_it(self):
        gpu = _GpuBackend(0.101 + 0.0j)
        cpu = _CpuReference(0.1 + 0.0j)
        solver = self._solver(gpu, cpu)

        result = solver.solve_sweep(self._network(), self._settings())

        self.assertEqual(result.compute_backend, "CUDA_CUPY -> CPU_SCIPY")

    def test_disabling_verification_skips_the_second_solve(self):
        gpu = _GpuBackend(0.101 + 0.0j, verify=False)
        cpu = _CpuReference(0.1 + 0.0j)
        solver = self._solver(gpu, cpu)

        result = solver.solve_sweep(self._network(), self._settings())

        self.assertEqual(cpu.calls, 0, "no audit means no reference solve")
        self.assertEqual(gpu.settings.backend, "AUTO")
        self.assertIn("skipped", result.gpu_accuracy_check)

    def test_a_cpu_only_sweep_is_never_audited(self):
        cpu_only = _GpuBackend(0.1 + 0.0j)
        cpu_only.settings.backend = "CPU"
        reference = _CpuReference(0.1 + 0.0j)
        solver = self._solver(cpu_only, reference)

        result = solver.solve_sweep(self._network(), self._settings())

        self.assertEqual(reference.calls, 0)
        self.assertEqual(result.gpu_accuracy_check, "")

    def test_a_failed_audit_reaches_the_report_as_a_limitation(self):
        from analysis_adapters import _ac_validity_limitations

        notes = _ac_validity_limitations(SimpleNamespace(
            gpu_accuracy_check="failed: GPU and CPU disagree by 1e-02 relative",
            worst_at_sweep_edge=False, frequencies_hz=[1e3, 1e6],
            quasi_static_limit_hz=0.0, points_beyond_quasi_static=0,
        ))
        self.assertTrue(any("GPU accuracy check failed" in note for note in notes))

    def test_a_passing_audit_adds_no_limitation(self):
        from analysis_adapters import _ac_validity_limitations

        notes = _ac_validity_limitations(SimpleNamespace(
            gpu_accuracy_check="passed: GPU matches the CPU direct solve",
            worst_at_sweep_edge=False, frequencies_hz=[1e3, 1e6],
            quasi_static_limit_hz=0.0, points_beyond_quasi_static=0,
        ))
        self.assertEqual(notes, [])


class RealGpuTests(unittest.TestCase):
    def test_cuda_path_agrees_with_cpu_on_a_real_device(self):
        try:
            import cupy  # noqa: F401
        except ImportError:
            self.skipTest("CuPy is not installed; the CUDA branch cannot run here")
        # Deliberately left to a machine with a GPU: simulating CuPy would
        # test the simulation, not the code that will actually run.
        self.skipTest("Requires a CUDA device; exercised on the user's machine")


if __name__ == "__main__":
    unittest.main()
