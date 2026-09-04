"""A direct solve that cannot fit must not lose the analysis.

A 12,304,864-node thermal mesh -- reachable once an explicit RAM budget is
honoured -- exhausted Pardiso ("error code -2", out of memory) and killed the
whole coupled run. The system is a symmetric positive-definite Laplacian that
a preconditioned CG solves without any factorisation memory at all, so the
failure was about the choice of solver, not the board.
"""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


def _laplacian(n):
    """1-D Laplacian with a Dirichlet anchor: SPD, like the thermal matrix."""
    import numpy as np
    import scipy.sparse

    main = np.full(n, 2.0)
    off = np.full(n - 1, -1.0)
    matrix = scipy.sparse.diags([off, main, off], [-1, 0, 1], format="csr")
    return matrix


class IterativeFallbackTests(unittest.TestCase):
    def setUp(self):
        try:
            import numpy, scipy  # noqa: F401
        except ImportError:
            self.skipTest("NumPy/SciPy are not installed in this interpreter")
        from compute_backend import SparseComputeBackend
        from runtime_config import RuntimeComputeSettings

        self.backend = SparseComputeBackend(RuntimeComputeSettings(backend="CPU"))

    def test_iterative_matches_the_direct_answer(self):
        # The fallback must not change the physics, only the method.
        import numpy as np
        import scipy.sparse.linalg

        matrix = _laplacian(400)
        rhs = np.ones(400)
        direct = scipy.sparse.linalg.spsolve(matrix.tocsc(), rhs)
        values, _elapsed, name = self.backend._solve_cpu_iterative(
            matrix, rhs, "SPD",
        )
        np.testing.assert_allclose(values, direct, rtol=1e-6)
        self.assertEqual(name, "CPU_SCIPY_CG")

    def test_a_failing_direct_solve_falls_back_and_says_so(self):
        import numpy as np

        matrix = _laplacian(300)
        rhs = np.ones(300)
        logs = []
        self.backend.log_callback = logs.append

        import compute_backend

        original = compute_backend.pypardiso
        compute_backend.pypardiso = None

        def _boom(*_args, **_kwargs):
            raise RuntimeError("The Pardiso solver failed with error code -2.")

        original_spsolve = compute_backend.scipy.sparse.linalg.spsolve
        compute_backend.scipy.sparse.linalg.spsolve = _boom
        try:
            solution = self.backend._solve_cpu(matrix, rhs, "SPD")
        finally:
            compute_backend.scipy.sparse.linalg.spsolve = original_spsolve
            compute_backend.pypardiso = original

        self.assertEqual(solution.metadata.solver_method, "ITERATIVE")
        self.assertEqual(solution.metadata.backend, "CPU_SCIPY_CG")
        self.assertTrue(np.all(np.isfinite(solution.values)))
        self.assertTrue(any("retrying iteratively" in entry for entry in logs))

    def test_a_general_system_uses_bicgstab(self):
        import numpy as np

        matrix = _laplacian(200)
        _values, _elapsed, name = self.backend._solve_cpu_iterative(
            matrix, np.ones(200), "GENERAL",
        )
        self.assertEqual(name, "CPU_SCIPY_BICGSTAB")


if __name__ == "__main__":
    unittest.main()
