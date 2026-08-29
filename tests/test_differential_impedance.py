import unittest

from shapely.geometry import Polygon

from differential_impedance import DifferentialGeometrySnapshot, DifferentialImpedanceSolver
from models import (
    DifferentialAnalysisSettings, DifferentialPairCandidate,
    StackupLayerModel, StackupProfile,
)


def segment(y, width=0.1, layer=0):
    return {
        "start": (0.0, y), "end": (10.0, y), "width_mm": width,
        "layer_id": layer, "length_mm": 10.0,
    }


class DifferentialImpedanceTests(unittest.TestCase):
    def setUp(self):
        self.stackup = StackupProfile(
            source="IMPORTED", trustworthy=True,
            layers=[
                StackupLayerModel("F.Cu", "COPPER", 0.035, layer_id=0),
                StackupLayerModel("Prepreg", "DIELECTRIC", 0.2, material="FR4", epsilon_r=4.2),
                StackupLayerModel("In1.Cu", "COPPER", 0.035, layer_id=1),
            ],
        )
        self.pair = DifferentialPairCandidate(
            "USB", "USB_DP", "USB_DM", interface="USB", target_impedance_ohm=90.0,
        )
        self.settings = DifferentialAnalysisSettings(pairs=[self.pair])

    def test_microstrip_uses_adjacent_filled_ground_plane(self):
        snapshot = DifferentialGeometrySnapshot(
            tracks_by_net={"USB_DP": [segment(0.0)], "USB_DM": [segment(0.3)]},
            zones_by_net={"GND": {1: Polygon([(-1, -2), (11, -2), (11, 2), (-1, 2)])}},
        )
        result = DifferentialImpedanceSolver(
            snapshot, self.stackup, self.settings
        ).solve_pair(self.pair)
        self.assertEqual(len(result.sections), 1)
        section = result.sections[0]
        self.assertEqual(section.topology, "MICROSTRIP")
        self.assertEqual(section.reference_below, "GND")
        self.assertGreaterEqual(section.reference_coverage_pct, 99.0)
        self.assertGreater(section.differential_impedance_ohm, 0.0)
        self.assertTrue(section.trustworthy)
        self.assertTrue(result.trustworthy)

    def test_missing_plane_does_not_claim_a_trusted_impedance(self):
        snapshot = DifferentialGeometrySnapshot(
            tracks_by_net={"USB_DP": [segment(0.0)], "USB_DM": [segment(0.3)]},
            zones_by_net={},
        )
        result = DifferentialImpedanceSolver(
            snapshot, self.stackup, self.settings
        ).solve_pair(self.pair)
        self.assertEqual(result.status, "NO_DATA")
        self.assertFalse(result.trustworthy)
        self.assertTrue(any("reference plane" in warning.lower() for warning in result.warnings))

    def test_tighter_coupling_reduces_differential_impedance(self):
        _, close = DifferentialImpedanceSolver._microstrip(0.1, 0.1, 0.035, 0.2, 4.2)
        _, far = DifferentialImpedanceSolver._microstrip(0.1, 0.4, 0.035, 0.2, 4.2)
        self.assertLess(close, far)

    def test_default_stackup_is_always_reported_as_estimate(self):
        untrusted = StackupProfile(
            layers=self.stackup.layers, source="DEFAULT", trustworthy=False,
            warnings=["generic stackup"],
        )
        snapshot = DifferentialGeometrySnapshot(
            tracks_by_net={"USB_DP": [segment(0.0)], "USB_DM": [segment(0.3)]},
            zones_by_net={"GND": {1: Polygon([(-1, -2), (11, -2), (11, 2), (-1, 2)])}},
        )
        result = DifferentialImpedanceSolver(snapshot, untrusted, self.settings).solve_pair(self.pair)
        self.assertEqual(result.status, "ESTIMATE")
        self.assertFalse(result.trustworthy)

    def test_inner_layer_uses_ground_planes_above_and_below(self):
        stackup = StackupProfile(
            source="KICAD_IPC", trustworthy=True,
            layers=[
                StackupLayerModel("In1.Cu", "COPPER", 0.035, layer_id=1),
                StackupLayerModel("Prepreg A", "DIELECTRIC", 0.15, epsilon_r=4.0),
                StackupLayerModel("In2.Cu", "COPPER", 0.035, layer_id=2),
                StackupLayerModel("Prepreg B", "DIELECTRIC", 0.15, epsilon_r=4.0),
                StackupLayerModel("In3.Cu", "COPPER", 0.035, layer_id=3),
            ],
        )
        plane = Polygon([(-1, -2), (11, -2), (11, 2), (-1, 2)])
        snapshot = DifferentialGeometrySnapshot(
            tracks_by_net={
                "USB_DP": [segment(0.0, layer=2)],
                "USB_DM": [segment(0.3, layer=2)],
            },
            zones_by_net={"GND": {1: plane, 3: plane}},
        )
        result = DifferentialImpedanceSolver(snapshot, stackup, self.settings).solve_pair(self.pair)
        section = result.sections[0]
        self.assertEqual(section.topology, "STRIPLINE")
        self.assertEqual(section.reference_above, "GND")
        self.assertEqual(section.reference_below, "GND")
        self.assertGreater(section.differential_impedance_ohm, 0.0)


if __name__ == "__main__":
    unittest.main()
