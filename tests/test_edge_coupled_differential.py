"""Tests for the 2-D quasi-static edge-coupled field solver that replaces
the IPC-D-317A closed-form microstrip/stripline formulas as the primary
impedance calculation in DifferentialImpedanceSolver._solve_section.
"""

import unittest

from shapely.geometry import Polygon

from differential_geometry import EdgeCoupledDifferentialSolver, GroundedCoplanarDifferentialSolver
from differential_impedance import DifferentialGeometrySnapshot, DifferentialImpedanceSolver
from models import DifferentialAnalysisSettings, DifferentialPairCandidate, StackupLayerModel, StackupProfile


def segment(y, width=0.1, layer=0):
    return {
        "start": (0.0, y), "end": (10.0, y), "width_mm": width,
        "layer_id": layer, "length_mm": 10.0,
    }


class EdgeCoupledFieldSolverTests(unittest.TestCase):
    def test_microstrip_impedance_is_physically_plausible(self):
        # Typical USB-like microstrip: 0.2mm width, 0.15mm gap, FR4 4.2,
        # 0.2mm to reference plane. Differential impedance for a coupled
        # 90-ohm-class pair should land well within a sane 80-120 ohm band.
        result = EdgeCoupledDifferentialSolver.solve_microstrip(
            0.2, 0.15, 0.035, 0.2, 4.2,
        )
        self.assertGreater(result.differential_impedance_ohm, 80.0)
        self.assertLess(result.differential_impedance_ohm, 120.0)
        self.assertAlmostEqual(
            result.odd_mode_impedance_ohm * 2.0, result.differential_impedance_ohm,
        )
        self.assertGreater(result.effective_epsilon_r, 1.0)
        self.assertLess(result.effective_epsilon_r, 4.2)

    def test_field_solve_agrees_with_closed_form_within_ipc_domain(self):
        # w/h = 0.15/0.15 = 1.0, squarely inside the IPC-D-317A validity
        # range (0.1-3). The two methods should agree to within ~25%.
        width, gap, thickness, height, epsilon = 0.15, 0.15, 0.035, 0.15, 4.2
        field = EdgeCoupledDifferentialSolver.solve_microstrip(
            width, gap, thickness, height, epsilon,
        )
        _, closed_form_zdiff = DifferentialImpedanceSolver._microstrip(
            width, gap, thickness, height, epsilon,
        )
        relative_error = abs(field.differential_impedance_ohm - closed_form_zdiff) / closed_form_zdiff
        self.assertLess(relative_error, 0.25)

    def test_stripline_symmetric_vs_asymmetric_produce_distinct_values(self):
        symmetric = EdgeCoupledDifferentialSolver.solve_stripline(
            0.15, 0.15, 0.035, 0.15, 4.0, 0.15, 4.0,
        )
        asymmetric = EdgeCoupledDifferentialSolver.solve_stripline(
            0.15, 0.15, 0.035, 0.30, 4.0, 0.10, 4.0,
        )
        self.assertGreater(symmetric.differential_impedance_ohm, 0.0)
        self.assertGreater(asymmetric.differential_impedance_ohm, 0.0)
        self.assertNotAlmostEqual(
            symmetric.differential_impedance_ohm, asymmetric.differential_impedance_ohm,
            delta=0.5,
        )

    def test_degenerate_geometry_raises(self):
        with self.assertRaises(ValueError):
            EdgeCoupledDifferentialSolver.solve_microstrip(0.2, 0.15, 0.035, 0.0, 4.2)

    def test_shared_sor_core_matches_coplanar_solver_pre_refactor_result(self):
        # Non-regression check for the field_solver_2d.py extraction: this
        # exact value (92.12018601552057 ohm) was captured from
        # GroundedCoplanarDifferentialSolver.solve() before its internals
        # were moved into field_solver_2d.py. Any drift means the refactor
        # changed numerics, not just code organization.
        GroundedCoplanarDifferentialSolver._solve_cached.cache_clear()
        result = GroundedCoplanarDifferentialSolver.solve(0.145, 0.18, 0.15, 0.0994, 4.1, 0.035)
        self.assertAlmostEqual(result.differential_impedance_ohm, 92.12018601552057, places=6)
        self.assertAlmostEqual(result.effective_epsilon_r, 2.7903490791896037, places=6)


class EdgeCoupledIntegrationTests(unittest.TestCase):
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

    def test_valid_microstrip_section_uses_field_solve_without_fallback_warning(self):
        snapshot = DifferentialGeometrySnapshot(
            tracks_by_net={"USB_DP": [segment(0.0)], "USB_DM": [segment(0.3)]},
            zones_by_net={"GND": {1: Polygon([(-1, -2), (11, -2), (11, 2), (-1, 2)])}},
        )
        result = DifferentialImpedanceSolver(
            snapshot, self.stackup, self.settings
        ).solve_pair(self.pair)
        section = result.sections[0]
        self.assertGreater(section.two_d_impedance_ohm, 0.0)
        self.assertGreater(section.effective_epsilon_r, 0.0)
        self.assertFalse(any("falling back" in warning for warning in section.warnings))

    def test_degenerate_section_falls_back_to_closed_form_with_explicit_warning(self):
        # epsilon_r_below=0.5 is unphysical and rejected by the field solver's
        # validity guard, but the closed-form formula has no such guard and
        # still produces a (less trustworthy) estimate — exactly the
        # scenario the explicit fallback warning exists to flag.
        solver = DifferentialImpedanceSolver(
            DifferentialGeometrySnapshot(), self.stackup, self.settings,
        )
        solver.plane_analyzer = type("PlaneAnalyzer", (), {"analyze": lambda self, *_args: __import__(
            "reference_plane_analyzer"
        ).ReferencePlaneContext(
            topology="MICROSTRIP", reference_below="GND", distance_below_mm=0.2,
            epsilon_r_below=0.5, coverage_below_pct=100.0, trustworthy=True,
        )})()
        section = solver._solve_section(segment(0.0), segment(0.3), 0.2, 10.0)
        self.assertGreater(section.differential_impedance_ohm, 0.0)
        self.assertTrue(any("falling back" in warning for warning in section.warnings))


if __name__ == "__main__":
    unittest.main()
