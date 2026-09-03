"""A sweep must not be judged by a single-solve CUDA threshold.

On the real p02_alimentation board the AC network is 39,569 nodes against a
100,000-node default, so AUTO never reached the GPU and the accuracy audit
never ran -- while the log announced a CUDA attempt that the node count had
already ruled out.
"""

import os
import sys
import unittest
from dataclasses import dataclass

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


@dataclass
class _Settings:
    backend: str = "AUTO"
    cuda_min_nodes: int = 100000


class _Backend:
    def __init__(self, settings):
        self.settings = settings


class AmortisedThresholdTests(unittest.TestCase):
    def setUp(self):
        try:
            import numpy, scipy  # noqa: F401
        except ImportError:
            self.skipTest("NumPy/SciPy are not installed in this interpreter")
        from ac_solver import ACSolver

        self.solver = ACSolver.__new__(ACSolver)
        self.solver.log_callback = None
        self.solver.debug = False

    def _run(self, settings, solve_count):
        self.solver.compute_backend = _Backend(settings)
        self.solver._amortise_cuda_threshold(solve_count)
        return settings.cuda_min_nodes

    def test_the_real_board_now_clears_the_threshold(self):
        # 100,000 / 121 = 826, so a 39,569-node network reaches CUDA.
        settings = _Settings()
        lowered = self._run(settings, 121)
        self.assertLess(lowered, 39569)

    def test_never_below_the_floor(self):
        settings = _Settings(cuda_min_nodes=100000)
        self.assertGreaterEqual(self._run(settings, 100000), 10000)

    def test_an_explicit_backend_choice_is_left_alone(self):
        for backend in ("CPU", "CUDA"):
            settings = _Settings(backend=backend)
            self.assertEqual(self._run(settings, 121), 100000, backend)

    def test_a_single_solve_is_unchanged(self):
        settings = _Settings()
        self.assertEqual(self._run(settings, 1), 100000)


if __name__ == "__main__":
    unittest.main()
