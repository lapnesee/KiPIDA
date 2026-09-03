import unittest

from shapely.geometry import Polygon

from differential_impedance import DifferentialGeometrySnapshot, DifferentialImpedanceSolver
from models import (
    DifferentialAnalysisSettings, DifferentialPairCandidate,
    DifferentialSectionResult, StackupLayerModel, StackupProfile,
)
from reference_plane_analyzer import ReferencePlaneContext


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
        self.assertAlmostEqual(result.positive_length_mm, 10.0)
        self.assertAlmostEqual(result.negative_length_mm, 10.0)
        self.assertEqual(result.length_symmetry_status, "PASS")
        self.assertEqual(result.skew_limit_ps, 25.0)

    def test_excessive_length_skew_forces_pair_fail(self):
        # Mismatch must clear the USB 25 ps limit using the *effective*
        # dielectric constant from the 2-D microstrip field solve (~2.7 for
        # this geometry, lower than the substrate's raw 4.2 because part of
        # the field fringes through air above the trace). 6 mm of mismatch
        # gives comfortable margin over the ~4.5 mm failure threshold.
        short_negative = segment(0.3)
        short_negative.update(end=(4.0, 0.3), length_mm=4.0)
        snapshot = DifferentialGeometrySnapshot(
            tracks_by_net={"USB_DP": [segment(0.0)], "USB_DM": [short_negative]},
            zones_by_net={"GND": {1: Polygon([(-1, -2), (11, -2), (11, 2), (-1, 2)])}},
        )
        result = DifferentialImpedanceSolver(
            snapshot, self.stackup, self.settings
        ).solve_pair(self.pair)
        self.assertEqual(result.length_symmetry_status, "FAIL")
        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.shorter_net, "USB_DM")
        self.assertTrue(any("Length symmetry failed" in warning for warning in result.warnings))

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

    def test_any_unreferenced_routed_section_forces_pair_fail(self):
        snapshot = DifferentialGeometrySnapshot(
            tracks_by_net={"USB_DP": [segment(0.0)], "USB_DM": [segment(0.3)]},
        )
        solver = DifferentialImpedanceSolver(snapshot, self.stackup, self.settings)
        positive = segment(0.0)
        negative = segment(0.3)
        solver._match_sections = lambda *_args: [
            (positive, negative, 0.2, 9.0),
            (positive, negative, 0.2, 1.0),
        ]
        contexts = iter([
            ReferencePlaneContext(
                topology="MICROSTRIP", reference_below="GND",
                distance_below_mm=0.2, epsilon_r_below=4.2,
                coverage_below_pct=100.0, trustworthy=True,
            ),
            ReferencePlaneContext(
                topology="UNREFERENCED", warnings=[
                    "No continuous adjacent ground plane was found."
                ],
            ),
        ])
        solver.plane_analyzer = type(
            "PlaneAnalyzer", (), {"analyze": lambda self, *_args: next(contexts)}
        )()
        result = solver.solve_pair(self.pair)
        self.assertEqual(result.status, "FAIL")
        self.assertFalse(result.trustworthy)
        self.assertTrue(any("cannot qualify" in warning for warning in result.warnings))

    def test_sub_width_section_is_diagnostic_not_aggregate_impedance(self):
        snapshot = DifferentialGeometrySnapshot(
            tracks_by_net={"USB_DP": [segment(0.0)], "USB_DM": [segment(0.3)]},
        )
        solver = DifferentialImpedanceSolver(snapshot, self.stackup, self.settings)
        long_positive, long_negative = segment(0.0), segment(0.3)
        short_positive = dict(segment(0.0), end=(0.02, 0.0), length_mm=0.02)
        short_negative = dict(segment(0.3), end=(0.02, 0.3), length_mm=0.02)
        solver._match_sections = lambda *_args: [
            (long_positive, long_negative, 0.2, 10.0),
            (short_positive, short_negative, 0.2, 0.02),
        ]
        solver._solve_section = lambda positive, negative, gap, length: DifferentialSectionResult(
            layer_id=0, layer_name="F.Cu", length_mm=length,
            width_mm=0.10, gap_mm=gap, topology="MICROSTRIP",
            differential_impedance_ohm=90.0 if length > 1.0 else 300.0,
            reference_coverage_pct=100.0, trustworthy=True,
        )
        result = solver.solve_pair(self.pair)
        self.assertAlmostEqual(result.weighted_impedance_ohm, 90.0)
        self.assertAlmostEqual(result.maximum_impedance_ohm, 90.0)
        self.assertTrue(any(
            "localized discontinuity" in warning for warning in result.warnings
        ))

    def test_wide_connector_breakout_is_not_a_missing_ground_failure(self):
        settings = DifferentialAnalysisSettings(
            pairs=[self.pair], geometry_mode="JLCPCB_COPLANAR",
            coplanar_ground_gap_mm=0.20,
        )
        snapshot = DifferentialGeometrySnapshot(
            tracks_by_net={
                "USB_DP": [segment(0.0, width=0.13)],
                "USB_DM": [segment(1.90, width=0.13)],
            },
        )
        result = DifferentialImpedanceSolver(snapshot, self.stackup, settings).solve_pair(self.pair)
        self.assertEqual(result.sections[0].topology, "BREAKOUT_TRANSITION")
        self.assertFalse(any(
            "routed section(s) have no continuous reference plane" in warning
            for warning in result.warnings
        ))
        self.assertTrue(any("connector/breakout" in warning for warning in result.warnings))


if __name__ == "__main__":
    unittest.main()
