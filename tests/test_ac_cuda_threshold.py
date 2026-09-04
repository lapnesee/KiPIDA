"""A sweep must not be judged by the single-solve CUDA threshold.

On the real p02_alimentation board the AC network is 39,569 nodes against a
100,000-node cuda_min_nodes, so AUTO never reached the GPU and the accuracy
audit never ran -- while the log announced a CUDA attempt that the node count
had already ruled out.
"""

import os
import sys
import unittest
from dataclasses import dataclass, replace

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


@dataclass
class _Settings:
    backend: str = "AUTO"
    cuda_min_nodes: int = 100000
    cuda_min_nodes_sweep: int = 10000


class _NotADataclass:
    backend = "AUTO"
    cuda_min_nodes = 100000


class SweepThresholdTests(unittest.TestCase):
    def setUp(self):
        try:
            import numpy, scipy  # noqa: F401
        except ImportError:
            self.skipTest("NumPy/SciPy are not installed in this interpreter")
        from ac_solver import ACSolver

        self.swap = ACSolver.sweep_compute_settings

    def test_the_real_board_now_clears_the_bar(self):
        # 39,569 nodes sat below cuda_min_nodes and above the sweep threshold.
        swapped = self.swap(_Settings())
        self.assertLess(swapped.cuda_min_nodes, 39569)
        self.assertEqual(swapped.cuda_min_nodes, 10000)

    def test_the_callers_settings_are_not_mutated(self):
        # The DC and thermal paths share this object and are single solves;
        # they must keep the stricter bar.
        original = _Settings()
        self.swap(original)
        self.assertEqual(original.cuda_min_nodes, 100000)

    def test_an_explicit_backend_choice_is_left_alone(self):
        for backend in ("CPU", "CUDA"):
            settings = _Settings(backend=backend)
            self.assertIs(self.swap(settings), settings, backend)

    def test_a_stand_in_without_the_field_is_returned_unchanged(self):
        settings = _NotADataclass()
        self.assertIs(self.swap(settings), settings)

    def test_none_is_tolerated(self):
        self.assertIsNone(self.swap(None))


class RuntimeSettingsTests(unittest.TestCase):
    def test_the_sweep_threshold_survives_normalisation(self):
        from runtime_config import RuntimeComputeSettings

        settings = RuntimeComputeSettings(cuda_min_nodes_sweep=0).normalized()
        self.assertGreaterEqual(settings.cuda_min_nodes_sweep, 1)

    def test_an_older_settings_file_without_the_field_still_loads(self):
        from runtime_config import RuntimeComputeSettings

        fields = RuntimeComputeSettings.__dataclass_fields__
        payload = {"backend": "AUTO", "cuda_min_nodes": 100000}
        settings = RuntimeComputeSettings(**{
            key: value for key, value in payload.items() if key in fields
        })
        self.assertEqual(settings.cuda_min_nodes_sweep, 10000)


if __name__ == "__main__":
    unittest.main()
