import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    from shapely.geometry import box
except ImportError:
    box = None

from emc_analyzer import EMCAnalyzer, EMCGeometrySnapshot, EMCTrack, EMCVia, EMCFootprint
from extractor import GeometryExtractor
from models import (
    EMCAnalysisSettings, EMCSignalSource, DifferentialPairCandidate,
    ImpedanceSweepResult, StackupLayerModel, StackupProfile,
)


def stackup():
    return StackupProfile(layers=[
        StackupLayerModel("F.Cu", "COPPER", 0.035, layer_id=0),
        StackupLayerModel("Prepreg", "DIELECTRIC", 0.2, epsilon_r=4.4),
        StackupLayerModel("In1.GND", "COPPER", 0.035, layer_id=1),
        StackupLayerModel("Core", "DIELECTRIC", 1.0, epsilon_r=4.4),
        StackupLayerModel("B.Cu", "COPPER", 0.035, layer_id=31),
    ], source="TEST", trustworthy=True)


@unittest.skipUnless(box is not None, "Shapely is required for geometric EMC tests")
class TestEMCAnalyzer(unittest.TestCase):
    def settings(self):
        return EMCAnalysisSettings(
            sources=[EMCSignalSource("CLK", "SYS_CLK", "CLOCK", 25e6, 2.0),
                     EMCSignalSource("Buck SW", "BUCK_SW", "SWITCHING", 500e3, 5.0)],
        )

    def snapshot(self):
        return EMCGeometrySnapshot(
            bounds_mm=(0.0, 0.0, 100.0, 80.0),
            stackup=stackup(),
            tracks=[
                EMCTrack("SYS_CLK", (5.0, 10.0), (95.0, 10.0), 0.2, 0, 90.0),
                EMCTrack("BUCK_SW", (45.0, 40.0), (95.0, 40.0), 3.0, 0, 50.0),
                EMCTrack("USB_D+", (10.0, 30.0), (50.0, 30.0), 0.2, 0, 40.0),
                EMCTrack("USB_D-", (10.0, 30.3), (48.0, 30.3), 0.2, 0, 38.0),
            ],
            vias=[EMCVia("SYS_CLK", (50.0, 10.0), (0, 31)),
                  EMCVia("GND", (70.0, 70.0), (0, 31))],
            footprints=[EMCFootprint("U1", "MCU", (10.0, 10.0)),
                        EMCFootprint("C1", "100n", (18.0, 10.0)),
                        EMCFootprint("J1", "USB", (2.0, 30.0))],
            zones_by_net={"GND": {1: box(0.0, 0.0, 45.0, 80.0)},
                          "BUCK_SW": {0: box(40.0, 35.0, 50.0, 45.0)}},
        )

    def test_detects_plane_void_switching_area_and_missing_return_via(self):
        result = EMCAnalyzer(self.snapshot(), self.settings()).analyze()
        rules = {finding.rule_id for finding in result.findings}
        self.assertIn("GP-001", rules)
        self.assertIn("SW-002", rules)
        self.assertIn("RP-001", rules)
        self.assertLess(result.risk_score, 100)
        self.assertTrue(result.probe_points)

    def test_live_board_capture_converts_ipc_values_before_worker_use(self):
        net = SimpleNamespace(name="SYS_CLK")
        track = SimpleNamespace(
            net=net,
            start=SimpleNamespace(x=1_000_000, y=2_000_000),
            end=SimpleNamespace(x=11_000_000, y=2_000_000),
            width=200_000,
            layer=0,
        )
        pad = SimpleNamespace(net=net, position=SimpleNamespace(x=1_000_000, y=2_000_000))
        footprint = SimpleNamespace(
            reference="U1", value="MCU", position=SimpleNamespace(x=1_000_000, y=2_000_000),
            pads=[pad],
        )
        board = SimpleNamespace(tracks=[track], vias=[], zones=[], footprints=[footprint])
        settings = self.settings()
        with patch.object(GeometryExtractor, "get_board_bounds", return_value=(0.0, 0.0, 20.0, 10.0)), \
             patch.object(GeometryExtractor, "get_stackup_profile", return_value=stackup()):
            captured = EMCGeometrySnapshot.capture(board, settings)
        self.assertEqual(captured.tracks[0].start, (1.0, 2.0))
        self.assertEqual(captured.tracks[0].end, (11.0, 2.0))
        self.assertEqual(captured.footprints[0].reference, "U1")

    def test_differential_skew_and_pdn_result_are_reused(self):
        pair = DifferentialPairCandidate("USB", "USB_D+", "USB_D-", interface="USB_HS")
        ac = ImpedanceSweepResult(
            [1e6], [0.2 + 0j], target_impedance_ohm=0.05,
            worst_frequency_hz=1e6, worst_impedance_ohm=0.2,
        )
        result = EMCAnalyzer(
            self.snapshot(), self.settings(), differential_pairs=[pair],
            ac_results=[("3V3", ac)],
        ).analyze()
        rules = {finding.rule_id for finding in result.findings}
        self.assertIn("DP-001", rules)
        self.assertIn("PD-001", rules)

    def test_emission_output_is_explicitly_relative(self):
        result = EMCAnalyzer(self.snapshot(), self.settings()).analyze()
        self.assertTrue(result.frequency_risks)
        self.assertTrue(result.cavity_resonances_hz)
        self.assertTrue(any("cannot certify" in item for item in result.limitations))
        self.assertTrue(result.test_plan)


if __name__ == "__main__":
    unittest.main()
