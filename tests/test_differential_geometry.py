import unittest

from shapely.geometry import Polygon

from differential_geometry import (
    GEOMETRY_AUTO, GEOMETRY_JLCPCB_COPLANAR,
    GroundedCoplanarDifferentialSolver, geometry_label, normalize_geometry,
)
from differential_impedance import DifferentialGeometrySnapshot, DifferentialImpedanceSolver
from models import DifferentialAnalysisSettings, DifferentialPairCandidate, StackupLayerModel, StackupProfile
from reference_plane_analyzer import ReferencePlaneContext


class DifferentialGeometryTests(unittest.TestCase):
    def setUp(self):
        GroundedCoplanarDifferentialSolver._solve_cached.cache_clear()
        self.stackup = StackupProfile(source="KICAD_IPC", trustworthy=True, layers=[
            StackupLayerModel("F.Cu", "COPPER", 0.035, layer_id=0),
            StackupLayerModel("FR4", "DIELECTRIC", 0.20, epsilon_r=4.2),
            StackupLayerModel("B.Cu", "COPPER", 0.035, layer_id=31),
        ])

    def test_jlcpcb_coplanar_geometry_is_exposed(self):
        self.assertEqual(normalize_geometry("jlcpcb_coplanar"), GEOMETRY_JLCPCB_COPLANAR)
        self.assertIn("JLCPCB", geometry_label(GEOMETRY_JLCPCB_COPLANAR))
        self.assertEqual(normalize_geometry("unsupported"), GEOMETRY_AUTO)

    def test_grounded_coplanar_solution_is_physical(self):
        result = GroundedCoplanarDifferentialSolver.solve(0.145, 0.18, 0.15, 0.0994, 4.1, 0.035)
        self.assertGreater(result.differential_impedance_ohm, 60.0)
        self.assertLess(result.differential_impedance_ohm, 130.0)
        self.assertAlmostEqual(result.odd_mode_impedance_ohm * 2.0, result.differential_impedance_ohm)
        self.assertGreater(result.effective_epsilon_r, 1.0)
        self.assertLess(result.effective_epsilon_r, 4.1)

    def test_closer_coplanar_ground_changes_differential_impedance(self):
        close = GroundedCoplanarDifferentialSolver.solve(0.145, 0.18, 0.08, 0.0994, 4.1, 0.035)
        far = GroundedCoplanarDifferentialSolver.solve(0.145, 0.18, 0.33, 0.0994, 4.1, 0.035)
        self.assertLess(close.differential_impedance_ohm, far.differential_impedance_ohm)
        self.assertGreater(far.differential_impedance_ohm - close.differential_impedance_ohm, 2.0)

    def test_impedance_solver_routes_coplanar_mode_to_field_solver(self):
        pair = DifferentialPairCandidate("USB", "USB_DP", "USB_DM", target_impedance_ohm=90.0)
        settings = DifferentialAnalysisSettings(
            pairs=[pair], geometry_mode=GEOMETRY_JLCPCB_COPLANAR, coplanar_ground_gap_mm=0.15,
        )
        segment = lambda y: {"start": (0.0, y), "end": (10.0, y), "width_mm": 0.145,
                             "layer_id": 0, "length_mm": 10.0}
        snapshot = DifferentialGeometrySnapshot(
            tracks_by_net={"USB_DP": [segment(0.0)], "USB_DM": [segment(0.325)]},
        )
        stackup = StackupProfile(source="KICAD_IPC", trustworthy=True, layers=[
            StackupLayerModel("F.Cu", "COPPER", 0.035, layer_id=0),
            StackupLayerModel("3313", "DIELECTRIC", 0.0994, epsilon_r=4.1),
            StackupLayerModel("In1.Cu", "COPPER", 0.0152, layer_id=1),
        ])
        solver = DifferentialImpedanceSolver(snapshot, stackup, settings)
        solver.plane_analyzer = type("PlaneAnalyzer", (), {"analyze": lambda self, *_args: ReferencePlaneContext(
            topology="MICROSTRIP", reference_below="GND", distance_below_mm=0.0994,
            epsilon_r_below=4.1, coverage_below_pct=100.0, trustworthy=True,
        )})()
        result = solver.solve_pair(pair)
        self.assertEqual(result.sections[0].topology, "COPLANAR_MICROSTRIP")
        self.assertAlmostEqual(result.sections[0].ground_clearance_mm, 0.15)
        self.assertGreater(result.weighted_impedance_ohm, 0.0)

    def test_measured_coplanar_gap_overrides_design_intent(self):
        pair = DifferentialPairCandidate("USB", "USB_DP", "USB_DM", target_impedance_ohm=90.0)
        settings = DifferentialAnalysisSettings(
            pairs=[pair], geometry_mode=GEOMETRY_JLCPCB_COPLANAR,
            coplanar_ground_gap_mm=0.15,
        )
        segment = lambda y: {"start": (0.0, y), "end": (10.0, y), "width_mm": 0.145,
                             "layer_id": 0, "length_mm": 10.0}
        snapshot = DifferentialGeometrySnapshot(
            tracks_by_net={"USB_DP": [segment(0.0)], "USB_DM": [segment(0.325)]},
        )
        stackup = StackupProfile(source="KICAD_IPC", trustworthy=True, layers=[
            StackupLayerModel("F.Cu", "COPPER", 0.035, layer_id=0),
            StackupLayerModel("3313", "DIELECTRIC", 0.0994, epsilon_r=4.1),
            StackupLayerModel("In1.Cu", "COPPER", 0.0152, layer_id=1),
        ])
        solver = DifferentialImpedanceSolver(snapshot, stackup, settings)
        solver.plane_analyzer = type("PlaneAnalyzer", (), {"analyze": lambda self, *_args: ReferencePlaneContext(
            topology="MICROSTRIP", reference_below="GND", distance_below_mm=0.0994,
            epsilon_r_below=4.1, coverage_below_pct=100.0, trustworthy=True,
        )})()
        solver._measured_coplanar_ground_gap = lambda *_args: 0.20
        section = solver.solve_pair(pair).sections[0]
        self.assertAlmostEqual(section.ground_clearance_mm, 0.20)
        self.assertTrue(any("measured gap is used" in warning for warning in section.warnings))

    def test_continuous_same_layer_ground_allows_unbacked_cpw(self):
        pair = DifferentialPairCandidate("USB", "USB_DP", "USB_DM", target_impedance_ohm=90.0)
        settings = DifferentialAnalysisSettings(
            pairs=[pair], geometry_mode=GEOMETRY_JLCPCB_COPLANAR,
            coplanar_ground_gap_mm=0.20,
        )
        segment = lambda y: {"start": (0.0, y), "end": (10.0, y), "width_mm": 0.11,
                             "layer_id": 0, "length_mm": 10.0}
        # Same-layer GND follows the two outer sides with 0.20 mm clearance;
        # the adjacent copper layer intentionally has no GND zone.
        same_layer_ground = Polygon([
            (-1.0, -2.0), (11.0, -2.0), (11.0, -0.255), (-1.0, -0.255)
        ]).union(Polygon([
            (-1.0, 0.635), (11.0, 0.635), (11.0, 2.0), (-1.0, 2.0)
        ]))
        snapshot = DifferentialGeometrySnapshot(
            tracks_by_net={"USB_DP": [segment(0.0)], "USB_DM": [segment(0.38)]},
            zones_by_net={"GND": {0: same_layer_ground}},
        )
        solver = DifferentialImpedanceSolver(snapshot, self.stackup, settings)
        section = solver.solve_pair(pair).sections[0]
        self.assertEqual(section.topology, "COPLANAR_WAVEGUIDE")
        self.assertGreaterEqual(section.reference_coverage_pct, 99.0)
        self.assertGreater(section.differential_impedance_ohm, 0.0)
        self.assertTrue(section.trustworthy)
        self.assertFalse(any(
            "No continuous adjacent ground plane" in warning
            for warning in section.warnings
        ))

    def test_isolated_same_layer_ground_does_not_clear_unreferenced(self):
        pair = DifferentialPairCandidate("USB", "USB_DP", "USB_DM", target_impedance_ohm=90.0)
        settings = DifferentialAnalysisSettings(
            pairs=[pair], geometry_mode=GEOMETRY_JLCPCB_COPLANAR,
            coplanar_ground_gap_mm=0.20,
        )
        segment = lambda y: {"start": (0.0, y), "end": (10.0, y), "width_mm": 0.11,
                             "layer_id": 0, "length_mm": 10.0}
        small_island = Polygon([(-0.2, -0.5), (0.2, -0.5), (0.2, -0.255), (-0.2, -0.255)])
        snapshot = DifferentialGeometrySnapshot(
            tracks_by_net={"USB_DP": [segment(0.0)], "USB_DM": [segment(0.38)]},
            zones_by_net={"GND": {0: small_island}},
        )
        result = DifferentialImpedanceSolver(snapshot, self.stackup, settings).solve_pair(pair)
        self.assertEqual(result.status, "NO_DATA")
        self.assertEqual(result.sections[0].topology, "UNREFERENCED")


if __name__ == "__main__":
    unittest.main()
