"""Tests for quantitative NEXT/FEXT crosstalk primitives (crosstalk_2d.py).

Scope: physical invariants and one Phase 2b non-regression check, not an
exhaustive parameter matrix.
"""

import unittest

from crosstalk_2d import (
    CoupledModeResult,
    SymmetricCoupledLineSolver,
    far_end_crosstalk_ratio,
    near_end_crosstalk_coefficient,
    near_end_crosstalk_ratio,
)
from differential_geometry import EdgeCoupledDifferentialSolver


class NearEndCoefficientTests(unittest.TestCase):
    def test_zero_when_even_equals_odd_impedance(self):
        modes = CoupledModeResult(
            odd_mode_impedance_ohm=50.0, even_mode_impedance_ohm=50.0,
            odd_mode_velocity_m_s=1.5e8, even_mode_velocity_m_s=1.5e8,
        )
        self.assertEqual(near_end_crosstalk_coefficient(modes), 0.0)

    def test_tighter_gap_increases_coupling(self):
        loose = SymmetricCoupledLineSolver.solve_microstrip_pair(0.2, 0.5, 0.035, 0.2, 4.2)
        tight = SymmetricCoupledLineSolver.solve_microstrip_pair(0.2, 0.1, 0.035, 0.2, 4.2)
        self.assertGreater(
            near_end_crosstalk_coefficient(tight),
            near_end_crosstalk_coefficient(loose),
        )


class FarEndCrosstalkTests(unittest.TestCase):
    def test_zero_when_even_and_odd_velocity_are_equal(self):
        # Kf_per_m = 0.5*(1/v_even - 1/v_odd) must vanish exactly when the
        # two modal velocities coincide -- a formula-level invariant.
        modes = CoupledModeResult(
            odd_mode_impedance_ohm=45.0, even_mode_impedance_ohm=55.0,
            odd_mode_velocity_m_s=1.55e8, even_mode_velocity_m_s=1.55e8,
        )
        fext = far_end_crosstalk_ratio(modes, coupled_length_m=0.05, rise_time_s=1e-9)
        self.assertEqual(fext, 0.0)

    def test_solved_homogeneous_stripline_has_zero_fext(self):
        # A homogeneous symmetric stripline (same epsilon and spacing above
        # and below) has no dielectric inhomogeneity between the even and
        # odd modes, so a *solved* cross-section -- not just the formula in
        # isolation -- must reach v_even == v_odd and therefore zero FEXT.
        # This is the physical case field_solver_2d.dielectric_map() got
        # wrong before its conductor-thickness-band fix (it left that row
        # band at vacuum instead of substrate epsilon, producing a spurious
        # ~3.7% velocity spread here).
        modes = SymmetricCoupledLineSolver.solve_stripline_pair(
            width_mm=0.15, gap_mm=0.15, copper_thickness_mm=0.035,
            height_above_mm=0.15, epsilon_above=4.0,
            height_below_mm=0.15, epsilon_below=4.0,
        )
        self.assertAlmostEqual(modes.even_mode_velocity_m_s, modes.odd_mode_velocity_m_s, places=6)
        fext = far_end_crosstalk_ratio(modes, coupled_length_m=0.05, rise_time_s=1e-9)
        self.assertEqual(fext, 0.0)


class NearEndRatioSaturationTests(unittest.TestCase):
    def test_ratio_saturates_to_coefficient_for_long_coupled_length(self):
        modes = SymmetricCoupledLineSolver.solve_microstrip_pair(0.2, 0.15, 0.035, 0.2, 4.2)
        kb_sat = near_end_crosstalk_coefficient(modes)
        # A coupled length many times the saturation length must not exceed Kb_sat.
        ratio = near_end_crosstalk_ratio(modes, coupled_length_m=10.0, rise_time_s=1e-12)
        self.assertAlmostEqual(ratio, kb_sat, places=9)


class InputGuardTests(unittest.TestCase):
    def test_non_positive_rise_time_raises_on_both_ratio_functions(self):
        modes = CoupledModeResult(
            odd_mode_impedance_ohm=45.0, even_mode_impedance_ohm=55.0,
            odd_mode_velocity_m_s=1.5e8, even_mode_velocity_m_s=1.6e8,
        )
        with self.assertRaises(ValueError):
            near_end_crosstalk_ratio(modes, coupled_length_m=0.05, rise_time_s=0.0)
        with self.assertRaises(ValueError):
            far_end_crosstalk_ratio(modes, coupled_length_m=0.05, rise_time_s=-1e-9)


class Phase2bNonRegressionTests(unittest.TestCase):
    def test_odd_mode_solve_unaffected_by_even_mode_extension(self):
        # Same geometry as
        # test_edge_coupled_differential.EdgeCoupledFieldSolverTests
        # .test_microstrip_impedance_is_physically_plausible -- confirms the
        # _solve_common extraction did not perturb the odd-mode result.
        result = EdgeCoupledDifferentialSolver.solve_microstrip(0.2, 0.15, 0.035, 0.2, 4.2)
        self.assertGreater(result.differential_impedance_ohm, 80.0)
        self.assertLess(result.differential_impedance_ohm, 120.0)


if __name__ == "__main__":
    unittest.main()
