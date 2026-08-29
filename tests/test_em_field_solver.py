import unittest

from em_field_solver import EMNearFieldSolver
from emc_analyzer import EMCGeometrySnapshot, EMCTrack
from field_probe import EMFieldMapProbe
from models import EMCAnalysisSettings, EMCSignalSource, StackupLayerModel, StackupProfile
from runtime_config import RuntimeComputeSettings


def snapshot():
    return EMCGeometrySnapshot(
        bounds_mm=(0.0, 0.0, 20.0, 10.0),
        stackup=StackupProfile(layers=[
            StackupLayerModel("F.Cu", "COPPER", 0.035, layer_id=0),
            StackupLayerModel("Core", "DIELECTRIC", 1.5, epsilon_r=4.2),
            StackupLayerModel("B.Cu", "COPPER", 0.035, layer_id=31),
        ]),
        tracks=[EMCTrack("CLK", (2.0, 5.0), (18.0, 5.0), 0.2, 0, 16.0)],
    )


def settings(height=2.0):
    return EMCAnalysisSettings(
        sources=[EMCSignalSource(
            "Clock", "CLK", "CLOCK", 25e6, 1.0,
            voltage_swing_v=3.3, current_a=0.05,
        )],
        field_probe_height_mm=height,
        field_grid_size_mm=1.0,
    )


class EMFieldSolverTests(unittest.TestCase):
    def solve(self, height=2.0):
        return EMNearFieldSolver(
            snapshot(), settings(height),
            runtime_settings=RuntimeComputeSettings(backend="CPU"),
        ).solve()

    def test_generates_finite_electric_and_magnetic_maps(self):
        result = self.solve()
        self.assertEqual(result.compute_backend, "CPU_NUMPY")
        self.assertGreater(result.maximum_e_v_m, 0.0)
        self.assertGreater(result.maximum_h_a_m, 0.0)
        self.assertEqual(len(result.electric_field_v_m), len(result.y_coordinates_mm))
        self.assertEqual(len(result.electric_field_v_m[0]), len(result.x_coordinates_mm))
        self.assertEqual(result.source_count, 1)

    def test_field_decreases_when_probe_height_increases(self):
        near = self.solve(1.0)
        far = self.solve(5.0)
        self.assertGreater(near.maximum_e_v_m, far.maximum_e_v_m)
        self.assertGreater(near.maximum_h_a_m, far.maximum_h_a_m)

    def test_rejects_an_excessive_grid(self):
        profile = settings()
        profile.field_grid_size_mm = 0.01
        profile.field_maximum_cells = 1000
        with self.assertRaisesRegex(ValueError, "increase the grid size"):
            EMNearFieldSolver(
                snapshot(), profile,
                runtime_settings=RuntimeComputeSettings(backend="CPU"),
            ).solve()

    def test_hover_probe_reads_the_nearest_cell(self):
        result = self.solve()
        probe = EMFieldMapProbe(
            result.x_coordinates_mm, result.y_coordinates_mm,
            result.electric_field_v_m, "E", "V/m", result.probe_height_mm,
            (0.1, 0.1, 0.8, 0.8), (0.0, 20.0), (10.0, 0.0),
        )
        reading = probe.sample(50, 50, 100, 100)
        self.assertIsNotNone(reading)
        self.assertEqual(reading.unit, "V/m")
        self.assertGreater(reading.value, 0.0)


if __name__ == "__main__":
    unittest.main()
