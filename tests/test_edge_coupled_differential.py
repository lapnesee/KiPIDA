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
        # Non-regression check for the field_solver_2d.py extraction: these
        # values were captured from GroundedCoplanarDifferentialSolver.solve()
        # right after its internals were moved into field_solver_2d.py (pure
        # refactor, verified bit-identical to pre-extraction at the time),
        # then twice updated since as dielectric_map()'s physics changed.
        #
        # First update: the conductor-thickness row band (the trace-to-trace
        # gap at trace height) was moved from vacuum to the surrounding
        # substrate epsilon, moving Z_diff from 92.1 to 84.5 ohm.
        #
        # Second update (audit-2026-09-23.md SI-2): that "fix" was itself
        # wrong for this exposed (non-buried) geometry. A real coplanar pair
        # on the surface layer has air -- not substrate -- between and
        # beside the traces at copper height; only a buried/stripline
        # conductor's band is laminate-filled (dielectric_map() now tells
        # the two cases apart). Undoing the substrate fill here, plus
        # widening the mirror-boundary margin to scale with the gap
        # (10*h + gap, was max(4*h, 2*w) independent of it) and tightening
        # the grid to h/20 (was h/7), raised effective_epsilon_r from 3.31
        # back toward 3.02 and Z_diff from 84.5 to 88.97 ohm -- within 25%
        # of the IPC-D-317A estimate exercised in
        # test_field_solve_agrees_with_closed_form_within_ipc_domain, and in
        # the same direction as the P02 Rev E USB pair correction (see
        # test_matches_kirschning_jansen_microstrip_within_tolerance below):
        # 76.86 -> ~85 ohm once er, the field solver and the recommender are
        # all fixed. Any further drift here means a real change to the field
        # solver's physics, not just code organization.
        GroundedCoplanarDifferentialSolver._solve_cached.cache_clear()
        result = GroundedCoplanarDifferentialSolver.solve(0.145, 0.18, 0.15, 0.0994, 4.1, 0.035)
        self.assertAlmostEqual(result.differential_impedance_ohm, 88.96915239351569, places=6)
        self.assertAlmostEqual(result.effective_epsilon_r, 3.0207920690677543, places=6)

    def test_matches_cohn_coupled_stripline_reference(self):
        # Cohn (1955) exact zero-thickness edge-coupled symmetric stripline,
        # audit-2026-09-23.md SI-2 reference case (w=0.15, s=0.15, b=0.5 mm,
        # er=4.2): Zdiff=105.26 ohm. The solver is measured with a near-zero
        # (0.001 mm) trace thickness to match Cohn's zero-thickness
        # assumption. Post-fix (margin scaled to the gap, grid at h/20) this
        # reads 101.07 ohm, -4.0%: a real remaining discretization error at
        # this aggressive w/s/b aspect ratio (corner-singularity-limited,
        # confirmed by grid refinement in scratchpad probing), down from
        # -4.8..-5.0% pre-fix, not a physics bug -- so the tolerance below
        # is 6%, not the ~2% of the milder Kirschning-Jansen microstrip case.
        width, gap, thickness, half_height, epsilon = 0.15, 0.15, 0.001, 0.2495, 4.2
        result = EdgeCoupledDifferentialSolver.solve_stripline(
            width, gap, thickness, half_height, epsilon, half_height, epsilon,
        )
        cohn_zdiff = 105.26037885577016
        relative_error = abs(result.differential_impedance_ohm - cohn_zdiff) / cohn_zdiff
        self.assertLess(relative_error, 0.06)

    def test_matches_kirschning_jansen_microstrip_within_tolerance(self):
        # Kirschning-Jansen (1984) static coupled microstrip, zero
        # thickness, P02 Rev E's own w/s/h/er (audit-2026-09-23.md SI-2):
        # Zdiff=95.71 ohm. Post-fix this reads 95.99 ohm, +0.3%, comfortably
        # inside the ~2% SI-2 asks for (pre-fix microstrip error was
        # -4.9..-8.8% against this same formula family).
        width, gap, thickness, height, epsilon = 0.2, 0.25, 0.001, 0.0994, 4.1
        result = EdgeCoupledDifferentialSolver.solve_microstrip(
            width, gap, thickness, height, epsilon, include_solder_mask=False,
        )
        kj_zdiff = 95.70785518130758
        relative_error = abs(result.differential_impedance_ohm - kj_zdiff) / kj_zdiff
        self.assertLess(relative_error, 0.02)

    def test_matches_p02_reference_solver_with_real_copper_and_mask(self):
        # P02 Rev E USB pair as actually drawn (audit-2026-09-23.md SI-2):
        # w=0.200, s=0.250, t=0.035, h=0.0994 mm, er=4.1, 0.01 mm mask. An
        # independent Laplace reference (scratchpad/audit/si/ref_fd.py,
        # validated against Cohn and Kirschning-Jansen above) puts this
        # geometry at 85.36 ohm -- the tool published 75.6-76.7 ohm before
        # SI-1/SI-2 (er silently 4.4, and this solver's band-fill/margin/
        # grid bugs). Fixed, it reads 83.25 ohm here (-2.5%), inside +/-10%
        # of the 90-ohm USB target where the unfixed tool's ~76 ohm was not.
        result = EdgeCoupledDifferentialSolver.solve_microstrip(
            0.2, 0.25, 0.035, 0.0994, 4.1,
            include_solder_mask=True, solder_mask_thickness_mm=0.01,
        )
        reference_zdiff = 85.36
        relative_error = abs(result.differential_impedance_ohm - reference_zdiff) / reference_zdiff
        self.assertLess(relative_error, 0.03)
        self.assertGreater(result.differential_impedance_ohm, 90.0 * 0.9)


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
