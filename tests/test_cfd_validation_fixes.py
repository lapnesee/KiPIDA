"""Regression guards for the defects found in docs/validation-cfd.md.

Each test here corresponds to something the CFD benchmark caught doing the
wrong thing on a duct with a known answer -- not to a hypothetical edge case.
"""

import unittest

from application.thermal_controller import thermal_cache_key
from cfd_solver import EnclosureCFDSolver
from models import EnclosureCFDSettings, ThermalAnalysisSettings
from thermal_mesh import ThermalMesher


class MassBalanceIsMeasuredNotImposed(unittest.TestCase):
    """The reported mass error must reflect what the solver actually produced.

    _pressure_projection overwrites the outlet velocity with
    incoming/outlet_area, so comparing the inflow against the *imposed* outflow
    is an identity. It reported 3.97e-14 % on a duct losing 5.2 % of its mass.
    """

    def test_the_error_reflects_the_outflow_before_the_fix_up(self):
        solver = EnclosureCFDSolver()
        solver._imposed_inflow = 1.0
        solver._natural_outflow = 0.95

        error = solver._flow_balance(mesh=None, velocity=None)

        self.assertAlmostEqual(error, 5.0, places=9)

    def test_a_perfect_projection_still_reports_zero(self):
        solver = EnclosureCFDSolver()
        solver._imposed_inflow = 1.0
        solver._natural_outflow = 1.0

        self.assertAlmostEqual(solver._flow_balance(None, None), 0.0, places=12)


class ForcedConvectionUsesTheSuppliedVelocity(unittest.TestCase):
    """A CFD free-stream speed must reach the surface coefficient.

    surface_coefficient already accepted air_velocity_m_s and documented itself
    as the CFD entry point, but nothing ever passed it.
    """

    def _settings(self):
        settings = ThermalAnalysisSettings()
        settings.airflow.mode = "NATURAL"
        settings.airflow.velocity_m_s = 0.0
        return settings

    def test_moving_air_cools_better_than_still_air(self):
        settings = self._settings()
        still = ThermalMesher.surface_coefficient(settings, "top", 40.0, 0.012)
        moving = ThermalMesher.surface_coefficient(
            settings, "top", 40.0, 0.012, air_velocity_m_s=0.5,
        )
        self.assertGreater(moving, still)

    def test_a_velocity_applies_even_when_the_mode_is_natural(self):
        # The CFD resolved a flow; the user's dropdown saying NATURAL must not
        # discard it, or the coupling would silently do nothing.
        settings = self._settings()
        natural_only = ThermalMesher.surface_coefficient(settings, "top", 40.0, 0.012)
        with_cfd = ThermalMesher.surface_coefficient(
            settings, "top", 40.0, 0.012, air_velocity_m_s=1.0,
        )
        self.assertGreater(with_cfd, natural_only)

    def test_a_custom_coefficient_still_overrides_everything(self):
        settings = self._settings()
        settings.airflow.mode = "CUSTOM"
        settings.airflow.custom_h_w_m2k = 12.0
        h = ThermalMesher.surface_coefficient(
            settings, "top", 40.0, 0.012, air_velocity_m_s=2.0,
        )
        self.assertAlmostEqual(h, 12.0)


class VelocityParticipatesInTheThermalCacheKey(unittest.TestCase):
    """Two runs differing only by CFD velocity have different surface physics.

    Sharing a cached mesh between them would silently reuse the wrong
    coefficients -- a correctness bug, not a performance one.
    """

    def test_two_velocities_do_not_share_a_cache_entry(self):
        settings = ThermalAnalysisSettings()
        still = thermal_cache_key(settings, None, False, [], None)
        moving = thermal_cache_key(settings, None, False, [], 0.4)
        self.assertNotEqual(still, moving)


class ResidualsAreDimensionless(unittest.TestCase):
    """continuity was a divergence in 1/s compared against a tolerance of 1e-4.

    It sat around 2.4 on the validation duct, so `converged` could never become
    True -- and adapt_cfd_result raises a HIGH finding on converged == False,
    meaning every run reported a numerics failure carrying no information.
    """

    def test_an_unconverged_forced_run_admits_a_nonzero_mass_error(self):
        # The end-to-end guard for the tautology: a 12-iteration solve cannot
        # have balanced the flow, so a mass error of exactly zero would mean the
        # diagnostic is measuring itself again.
        from cfd_mesh import CFDMeshGenerator
        from cfd_model import EnclosureModel
        from models import CFDBoundaryPatch

        settings = EnclosureCFDSettings()
        settings.geometry.width_mm = 30.0
        settings.geometry.depth_mm = 30.0
        settings.geometry.height_mm = 30.0
        settings.solver.cell_size_mm = 6.0
        settings.solver.max_iterations = 12
        settings.solver.pressure_iterations = 12
        settings.solver.include_buoyancy = False
        model = EnclosureModel((30.0, 30.0, 30.0), patches=[
            CFDBoundaryPatch("Fan", "FAN", "XMIN", 0.5, 0.5, 0.5, 0.5, 0.8, 25.0),
            CFDBoundaryPatch("Outlet", "OUTLET", "XMAX", 0.5, 0.5, 0.5, 0.5),
        ])
        mesh = CFDMeshGenerator().generate_mesh(model, settings)

        result = EnclosureCFDSolver().solve(mesh, settings)

        # The threshold has to sit well above float noise, or the test would
        # still pass against the old tautology: that reported 4e-14 %, which is
        # dutifully greater than zero. This case actually reports about 10 %.
        self.assertGreater(result.mass_balance_error_pct, 1.0)

    def test_residuals_fall_towards_the_tolerance(self):
        # Dimensionless residuals must decrease. Before normalisation the
        # continuity term was a divergence in 1/s sitting around 2.4, so it
        # could never approach a tolerance of 1e-4 and `converged` was
        # permanently False.
        from cfd_mesh import CFDMeshGenerator
        from cfd_model import EnclosureModel
        from models import CFDBoundaryPatch

        settings = EnclosureCFDSettings()
        settings.geometry.width_mm = 30.0
        settings.geometry.depth_mm = 30.0
        settings.geometry.height_mm = 30.0
        settings.solver.cell_size_mm = 6.0
        settings.solver.max_iterations = 12
        settings.solver.pressure_iterations = 12
        settings.solver.include_buoyancy = False
        model = EnclosureModel((30.0, 30.0, 30.0), patches=[
            CFDBoundaryPatch("Fan", "FAN", "XMIN", 0.5, 0.5, 0.5, 0.5, 0.8, 25.0),
            CFDBoundaryPatch("Outlet", "OUTLET", "XMAX", 0.5, 0.5, 0.5, 0.5),
        ])
        mesh = CFDMeshGenerator().generate_mesh(model, settings)

        result = EnclosureCFDSolver().solve(mesh, settings)

        continuity = result.residuals.continuity
        self.assertLess(continuity[-1], continuity[len(continuity) // 2])
        self.assertLess(continuity[-1], 1.0)

    def test_a_near_converged_run_is_not_reported_as_a_high_failure(self):
        # The validation duct ends at continuity=1.7e-2 with momentum and
        # energy at 1e-12: the inlet-cell artifact, not a stalled solve.
        # Grading it HIGH made every enclosure run cry wolf.
        from analysis_adapters import adapt_cfd_result
        from analysis_contract import FindingSeverity
        from models import CFDResidualHistory, EnclosureCFDResult

        result = EnclosureCFDResult(
            converged=False, iterations=3000,
            residuals=CFDResidualHistory(
                continuity=[1.0, 0.0169], momentum=[1.0, 1.29e-12],
                energy=[1.0, 1.5e-11],
            ),
        )

        adapted = adapt_cfd_result(mesh=None, domain_result=result)

        stalled = [f for f in adapted.findings if f.rule_id == "CFD-001"]
        self.assertEqual(len(stalled), 1)
        self.assertEqual(stalled[0].severity, FindingSeverity.MEDIUM)
        self.assertIn("continuity", stalled[0].description)

    def test_a_genuinely_stalled_run_is_still_high(self):
        from analysis_adapters import adapt_cfd_result
        from analysis_contract import FindingSeverity
        from models import CFDResidualHistory, EnclosureCFDResult

        result = EnclosureCFDResult(
            converged=False, iterations=250,
            residuals=CFDResidualHistory(
                continuity=[5.0, 4.2], momentum=[5.0, 3.1], energy=[5.0, 2.0],
            ),
        )

        adapted = adapt_cfd_result(mesh=None, domain_result=result)

        stalled = [f for f in adapted.findings if f.rule_id == "CFD-001"]
        self.assertEqual(stalled[0].severity, FindingSeverity.HIGH)

    def test_pressure_sweeps_default_to_the_validated_value(self):
        # 60 leaked 5.2 % of the mass; 240 leaks 1.6 %. See docs/validation-cfd.md.
        self.assertGreaterEqual(EnclosureCFDSettings().solver.pressure_iterations, 240)


if __name__ == "__main__":
    unittest.main()
