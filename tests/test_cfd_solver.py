import math
import time
import unittest

import numpy as np

from cfd_mesh import CFDMeshGenerator
from cfd_model import CFDObstacle, EnclosureModel
from cfd_solver import EnclosureCFDSolver
from models import CFDBoundaryPatch, EnclosureCFDSettings


class TestEnclosureCFDSolver(unittest.TestCase):
    def _settings(self):
        settings = EnclosureCFDSettings()
        settings.geometry.width_mm = 30.0
        settings.geometry.depth_mm = 30.0
        settings.geometry.height_mm = 30.0
        settings.geometry.wall_heat_transfer_w_m2k = 8.0
        settings.solver.cell_size_mm = 6.0
        settings.solver.max_iterations = 12
        settings.solver.pressure_iterations = 12
        settings.solver.tolerance = 1e-8
        settings.solver.pseudo_time_step_s = 0.002
        return settings

    def test_heated_solid_raises_solid_and_air_temperature(self):
        settings = self._settings()
        model = EnclosureModel(
            dimensions_mm=(30.0, 30.0, 30.0),
            obstacles=[CFDObstacle("HOT", (12, 12, 12, 18, 18, 18), 5.0, 0.5)],
        )
        mesh = CFDMeshGenerator().generate_mesh(model, settings)

        result = EnclosureCFDSolver().solve(mesh, settings)

        self.assertEqual(len(result.pressure_pa), mesh.cell_count)
        self.assertTrue(all(math.isfinite(value) for value in result.pressure_pa))
        self.assertGreater(result.maximum_solid_temperature_c, settings.ambient_c)
        self.assertGreater(result.maximum_air_temperature_c, settings.ambient_c)
        self.assertAlmostEqual(result.total_heat_w, 0.5)
        self.assertEqual(len(result.residuals.continuity), result.iterations)
        self.assertTrue(math.isfinite(result.energy_balance_error_pct))
        self.assertTrue(result.compute_backend.startswith("CPU_"))
        self.assertLess(result.compute_relative_residual, 1e-8)

    def test_forced_inlet_produces_a_finite_velocity_field(self):
        settings = self._settings()
        settings.solver.include_buoyancy = False
        inlet = CFDBoundaryPatch("Fan", "FAN", "XMIN", 0.5, 0.5, 0.5, 0.5, 0.8, 25.0)
        outlet = CFDBoundaryPatch("Outlet", "OUTLET", "XMAX", 0.5, 0.5, 0.5, 0.5)
        model = EnclosureModel((30.0, 30.0, 30.0), patches=[inlet, outlet])
        mesh = CFDMeshGenerator().generate_mesh(model, settings)

        result = EnclosureCFDSolver().solve(mesh, settings)

        self.assertGreater(result.maximum_velocity_m_s, 0.0)
        self.assertTrue(math.isfinite(result.mass_balance_error_pct))
        self.assertTrue(np.isfinite(np.asarray(result.velocity_u_m_s)).all())

    def test_energy_operator_is_not_assembled_cell_by_cell(self):
        # P02 Rev E, 2026-09-22 21:56: the 165 x 100 x 50 mm enclosure meshed to
        # 33 x 20 x 10 cells and took 129 s for 435 iterations; assembling the
        # energy equation one cell and face at a time cost 0.34-0.67 s per
        # call, nine tenths of the run. Whole-array assembly takes ~3 ms.
        settings = EnclosureCFDSettings()
        settings.geometry.width_mm = 165.0
        settings.geometry.depth_mm = 100.0
        settings.geometry.height_mm = 50.0
        settings.solver.cell_size_mm = 5.0
        model = EnclosureModel(
            dimensions_mm=(165.0, 100.0, 50.0),
            obstacles=[CFDObstacle("PCB", (5, 5, 18, 160, 95, 19.6), 0.3, 1.0)],
        )
        mesh = CFDMeshGenerator().generate_mesh(model, settings)
        self.assertEqual(mesh.shape, (33, 20, 10))
        solver = EnclosureCFDSolver()
        velocity = [np.zeros(mesh.shape) for _ in range(3)]
        previous = np.full(mesh.shape, settings.ambient_c + 10.0)

        started = time.perf_counter()
        temperature, _ = solver._solve_energy(mesh, settings, velocity, previous)
        elapsed = time.perf_counter() - started

        self.assertGreater(float(np.max(temperature)), settings.ambient_c)
        self.assertLess(elapsed, 0.15)

    def test_free_stream_is_sampled_over_and_under_a_flat_board(self):
        # P02 Rev E, 2026-09-22 21:58: a 1.6 mm board 15 mm up a 165 x 100 x
        # 50 mm enclosure gave no sample at all -- the slab stopped at the
        # board's own, solid, layer of cells -- and the report showed 0 m/s.
        settings = EnclosureCFDSettings()
        settings.solver.cell_size_mm = 5.0
        obstacles = [CFDObstacle("PCB", (0, 0, 15, 160, 100, 16.6), 0.6, 0.4)]
        obstacles += [
            CFDObstacle(f"U{i}", (10 + 15 * i, 40, 16.6, 16 + 15 * i, 46, 20), 8.0, 0.2)
            for i in range(10)
        ]
        mesh = CFDMeshGenerator().generate_mesh(
            EnclosureModel((165.0, 100.0, 50.0), obstacles=obstacles), settings,
        )
        speed = np.ones(mesh.shape)
        speed[:, :, -1] = 0.0  # the no-slip lid

        velocity, cells = EnclosureCFDSolver._board_free_stream(mesh, speed)

        self.assertGreater(cells, 0)
        self.assertEqual(velocity, 1.0)

    def test_cancellation_stops_before_iteration(self):
        settings = self._settings()
        model = EnclosureModel((30.0, 30.0, 30.0))
        mesh = CFDMeshGenerator().generate_mesh(model, settings)

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            EnclosureCFDSolver().solve(mesh, settings, cancel_callback=lambda: True)


if __name__ == "__main__":
    unittest.main()
