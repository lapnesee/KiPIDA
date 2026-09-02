import unittest

from shapely.geometry import box

from em_field_solver import EMNearFieldSolver
from emc_analyzer import EMCFootprint, EMCGeometrySnapshot, EMCTrack
from field_probe import EMFieldMapProbe
from models import (
    EMCAnalysisSettings, EMCInductorModel, EMCSignalSource,
    StackupLayerModel, StackupProfile,
)
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
        self.assertEqual(len(result.source_contributions), 1)
        self.assertTrue(result.source_segments)
        self.assertEqual(result.frequency_mode, "FIRST_IN_BAND_HARMONICS")
        self.assertAlmostEqual(result.source_contributions[0].analyzed_frequency_hz, 50e6)
        self.assertEqual(result.source_contributions[0].harmonic_number, 2)

    def test_switcher_uses_first_harmonic_inside_compliance_band(self):
        profile = settings()
        profile.sources[0].frequency_hz = 600e3
        result = EMNearFieldSolver(
            snapshot(), profile,
            runtime_settings=RuntimeComputeSettings(backend="CPU"),
        ).solve()
        contribution = result.source_contributions[0]
        self.assertAlmostEqual(contribution.analyzed_frequency_hz, 30e6)
        self.assertEqual(contribution.harmonic_number, 50)

    def test_field_decreases_when_probe_height_increases(self):
        near = self.solve(1.0)
        far = self.solve(5.0)
        self.assertGreater(near.maximum_e_v_m, far.maximum_e_v_m)
        self.assertGreater(near.maximum_h_a_m, far.maximum_h_a_m)

    def test_excessive_grid_is_coarsened_to_memory_limit(self):
        profile = settings()
        profile.field_grid_size_mm = 0.01
        profile.field_maximum_cells = 1000
        result = EMNearFieldSolver(
            snapshot(), profile,
            runtime_settings=RuntimeComputeSettings(backend="CPU"),
        ).solve()
        self.assertLessEqual(
            len(result.x_coordinates_mm) * len(result.y_coordinates_mm), 1000,
        )
        self.assertGreater(result.effective_grid_size_mm, result.requested_grid_size_mm)
        self.assertTrue(any("automatically coarsened" in warning for warning in result.warnings))

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

    def test_differential_pair_cancellation_reduces_far_field(self):
        snap = snapshot()
        snap.tracks.append(EMCTrack("CLK_N", (2.0, 5.3), (18.0, 5.3), 0.2, 0, 16.0))
        single_settings = settings(height=5.0)
        differential_settings = settings(height=5.0)
        differential_settings.sources[0].negative_net_name = "CLK_N"
        single = EMNearFieldSolver(
            snap, single_settings, runtime_settings=RuntimeComputeSettings(backend="CPU"),
        ).solve()
        differential = EMNearFieldSolver(
            snap, differential_settings, runtime_settings=RuntimeComputeSettings(backend="CPU"),
        ).solve()
        self.assertLess(differential.maximum_e_v_m, single.maximum_e_v_m)
        self.assertLess(differential.maximum_h_a_m, single.maximum_h_a_m)

    def test_continuous_ground_plane_image_reduces_far_field(self):
        unreferenced = snapshot()
        referenced = snapshot()
        referenced.zones_by_net = {"GND": {31: box(0.0, 0.0, 20.0, 10.0)}}
        plain = EMNearFieldSolver(
            unreferenced, settings(height=5.0),
            runtime_settings=RuntimeComputeSettings(backend="CPU"),
        ).solve()
        with_return = EMNearFieldSolver(
            referenced, settings(height=5.0),
            runtime_settings=RuntimeComputeSettings(backend="CPU"),
        ).solve()
        self.assertLess(with_return.maximum_e_v_m, plain.maximum_e_v_m)
        self.assertLess(with_return.maximum_h_a_m, plain.maximum_h_a_m)

    def test_switching_zone_without_track_uses_pad_to_pad_geometry(self):
        snap = snapshot()
        snap.tracks = []
        snap.footprints = [
            EMCFootprint("U1", "BUCK", (4.0, 5.0), ("SW",), (("SW", 4.0, 5.0),)),
            EMCFootprint("L1", "1uH", (7.0, 5.0), ("SW",), (("SW", 7.0, 5.0),)),
        ]
        snap.zones_by_net = {"SW": {0: box(3.5, 4.5, 7.5, 5.5)}}
        profile = settings()
        profile.sources = [EMCSignalSource(
            "Buck SW", "SW", "SWITCHING", 600e3, 5.0,
            voltage_swing_v=12.0, current_a=4.0,
        )]
        result = EMNearFieldSolver(
            snap, profile, runtime_settings=RuntimeComputeSettings(backend="CPU"),
        ).solve()
        self.assertEqual(result.source_contributions[0].geometry_source, "PAD_TO_PAD_ZONE_PATH")
        self.assertTrue(any("at least 80% inside" in item for item in result.warnings))

    def test_disconnected_switching_copper_does_not_create_a_false_pad_diagonal(self):
        snap = snapshot()
        snap.tracks = []
        snap.footprints = [
            EMCFootprint("U1", "BUCK", (3.0, 5.0), ("SW",), (("SW", 3.0, 5.0),)),
            EMCFootprint("L1", "1uH", (17.0, 5.0), ("SW",), (("SW", 17.0, 5.0),)),
        ]
        snap.zones_by_net = {
            "SW": {0: box(2.5, 4.5, 3.5, 5.5).union(box(16.5, 4.5, 17.5, 5.5))}
        }
        profile = settings()
        profile.sources = [EMCSignalSource("Buck SW", "SW", "SWITCHING", 600e3, 5.0)]
        with self.assertRaisesRegex(ValueError, "No enabled EMI/EMC source"):
            EMNearFieldSolver(
                snap, profile, runtime_settings=RuntimeComputeSettings(backend="CPU"),
            ).solve()

    def test_inductor_ripple_adds_traceable_magnetic_contribution(self):
        snap = snapshot()
        snap.tracks = [EMCTrack("SW", (2.0, 5.0), (8.0, 5.0), 0.5, 0, 6.0)]
        snap.footprints = [
            EMCFootprint("L1", "2.2uH", (8.0, 5.0), ("SW",), (("SW", 8.0, 5.0),)),
        ]
        snap.inductors = [EMCInductorModel(
            "L1", mpn="SPM6530T-2R2M", source_name="Buck", switching_net="SW",
            inductance_h=2.2e-6, vin_v=12.0, vout_v=5.0,
            switching_frequency_hz=600e3, ripple_current_pp_a=2.2,
            width_mm=7.1, depth_mm=6.5, height_mm=3.0,
            shield_state="SHIELDED", parameter_confidence="HIGH",
        )]
        profile = settings(height=4.0)
        profile.sources = [EMCSignalSource(
            "Buck", "SW", "SWITCHING", 600e3, 10.0,
            voltage_swing_v=12.0, current_a=4.0,
        )]
        result = EMNearFieldSolver(
            snap, profile, runtime_settings=RuntimeComputeSettings(backend="CPU"),
        ).solve()
        self.assertEqual(len(result.inductor_contributions), 1)
        contribution = result.inductor_contributions[0]
        self.assertEqual(contribution.ref_des, "L1")
        self.assertGreater(contribution.maximum_h_a_m, 0.0)
        self.assertIsNone(contribution.attenuation_applied_db)
        self.assertEqual(contribution.model_confidence, "LOW")

    def test_verified_shield_attenuation_reduces_inductor_field(self):
        snap = snapshot()
        snap.tracks = []
        snap.footprints = [
            EMCFootprint("L1", "2.2uH", (8.0, 5.0), ("SW",), (("SW", 8.0, 5.0),)),
        ]
        base = EMCInductorModel(
            "L1", source_name="Buck", switching_net="SW", vin_v=12.0, vout_v=5.0,
            ripple_current_pp_a=2.2, width_mm=7.1, depth_mm=6.5, height_mm=3.0,
        )
        profile = settings(height=4.0)
        profile.sources = [EMCSignalSource("Buck", "SW", "SWITCHING", 600e3, 10.0)]
        snap.inductors = [base]
        plain = EMNearFieldSolver(
            snap, profile, runtime_settings=RuntimeComputeSettings(backend="CPU"),
        ).solve().inductor_contributions[0].maximum_h_a_m
        base.shielding_attenuation_db = 20.0
        shielded = EMNearFieldSolver(
            snap, profile, runtime_settings=RuntimeComputeSettings(backend="CPU"),
        ).solve().inductor_contributions[0].maximum_h_a_m
        self.assertAlmostEqual(shielded / plain, 0.1, places=3)


if __name__ == "__main__":
    unittest.main()
