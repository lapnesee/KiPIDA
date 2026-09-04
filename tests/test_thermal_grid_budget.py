"""Grid coarsening must reach the report, and an explicit budget must govern.

Both come from the real p02_alimentation board: its hotspot moved 7.4 C
between a 0.5 mm and a 0.1 mm mesh, so a silently coarsened grid is not a
detail; and a declared 16 GiB budget allows 13.4 M nodes while the default
ceiling refused everything past 4 M, making the setting a no-op.
"""

import os
import sys
import unittest
from dataclasses import dataclass

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


@dataclass
class _Thermal:
    adaptive_grid: bool = False
    requested_grid_size_mm: float = 0.0
    effective_grid_size_mm: float = 0.0
    mesh_node_count: int = 0


class GridCoarseningReportedTests(unittest.TestCase):
    def test_a_coarsened_grid_is_stated_with_both_resolutions(self):
        from analysis_adapters import _thermal_grid_limitations

        notes = _thermal_grid_limitations(_Thermal(
            adaptive_grid=True, requested_grid_size_mm=0.05,
            effective_grid_size_mm=0.0894, mesh_node_count=3_900_000,
        ))
        self.assertEqual(len(notes), 1)
        self.assertIn("0.05 mm", notes[0])
        self.assertIn("0.0894 mm", notes[0])
        self.assertIn("3,900,000", notes[0])

    def test_an_honoured_grid_says_nothing(self):
        from analysis_adapters import _thermal_grid_limitations

        self.assertEqual(_thermal_grid_limitations(_Thermal()), [])
        self.assertEqual(
            _thermal_grid_limitations(_Thermal(
                adaptive_grid=True, requested_grid_size_mm=0.1,
                effective_grid_size_mm=0.1,
            )),
            [],
        )


@dataclass
class _Compute:
    backend: str = "AUTO"
    cuda_enabled: bool = True
    memory_limit_gib: float = 0.0
    cpu_multithread: bool = True
    cpu_threads: int = 0


class NodeBudgetTests(unittest.TestCase):
    def setUp(self):
        try:
            import numpy, shapely  # noqa: F401
        except ImportError:
            self.skipTest("thermal mesh dependencies not available")
        from thermal_mesh import ThermalMesher

        self.ThermalMesher = ThermalMesher

    def _limit(self, gib):
        mesher = self.ThermalMesher(compute_settings=_Compute(memory_limit_gib=gib))
        return mesher._node_limit()[0]

    def test_an_explicit_budget_cannot_raise_the_limit_past_the_ceiling(self):
        # This briefly asserted the opposite. Letting a declared 16 GiB reach
        # the 13.4 M nodes it nominally affords drove the process past 44 GB
        # without finishing: HOST_PEAK_BYTES_PER_NODE covers the mesh, not the
        # working set of everything downstream of it. The ceiling is the
        # backstop for that gap.
        limit = self._limit(16.0)
        self.assertEqual(limit, self.ThermalMesher.HARD_CUDA_NODE_LIMIT)

    def test_no_declared_budget_keeps_the_conservative_default(self):
        limit = self._limit(0.0)
        self.assertEqual(limit, self.ThermalMesher.CUDA_NODE_LIMIT)

    def test_a_small_budget_still_binds(self):
        # The budget bounds the mesh in both directions; it is not a bypass.
        self.assertEqual(self._limit(1.0), int(1.0 * (1024 ** 3) // 1280))


if __name__ == "__main__":
    unittest.main()
