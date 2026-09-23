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


class CFDFeedsThermalOnlyWhenTheFlowIsForced(unittest.TestCase):
    """One-way CFD -> thermal coupling is only sound in forced flow.

    Under buoyancy the velocity is caused by the temperature field the thermal
    solve produces, so running CFD first against a cold board would hand
    thermal a velocity derived from an answer that does not exist yet.
    """

    def _request(self, patches):
        from application.campaign_controller import CampaignRunRequest
        from application.thermal_controller import ThermalRunRequest
        from models import EnclosureCFDSettings

        settings = EnclosureCFDSettings()
        settings.patches = list(patches)
        cfd = type("CFDReq", (), {"settings": settings})()
        thermal = ThermalRunRequest(settings=None, board_model=None)
        return CampaignRunRequest(domain_requests={"CFD": cfd, "THERMAL": thermal})

    def _order(self, request):
        from application.campaign_controller import CampaignEngine

        engine = CampaignEngine(domain_engines={})
        descriptors = engine._order_for_cfd_coupling(request, lambda _m: None)
        return [d.analysis_id for d in descriptors]

    def test_a_fan_hoists_cfd_ahead_of_thermal(self):
        from models import CFDBoundaryPatch

        order = self._order(self._request([
            CFDBoundaryPatch("Fan", "FAN", "XMIN", 0.5, 0.5, 0.5, 0.5, 0.8, 25.0),
            CFDBoundaryPatch("Out", "OUTLET", "XMAX", 0.5, 0.5, 0.5, 0.5),
        ]))
        self.assertLess(order.index("CFD"), order.index("THERMAL"))

    def test_buoyancy_only_keeps_the_registry_order(self):
        from models import CFDBoundaryPatch

        order = self._order(self._request([
            CFDBoundaryPatch("Vent", "VENT", "XMAX", 0.5, 0.5, 0.5, 0.5),
        ]))
        self.assertLess(order.index("THERMAL"), order.index("CFD"))

    def test_a_velocity_reaches_the_thermal_request(self):
        from application.campaign_controller import CampaignEngine
        from models import CFDBoundaryPatch, EnclosureCFDResult

        request = self._request([
            CFDBoundaryPatch("Fan", "FAN", "XMIN", 0.5, 0.5, 0.5, 0.5, 0.8, 25.0),
        ])
        engine = CampaignEngine(domain_engines={})
        engine._couple_cfd_into_thermal(
            request,
            EnclosureCFDResult(
                board_free_stream_velocity_m_s=0.42, board_free_stream_cells=17,
            ),
            lambda _m: None,
        )
        self.assertAlmostEqual(
            request.domain_requests["THERMAL"].air_velocity_m_s, 0.42,
        )

    def test_forced_flow_also_hands_over_the_enclosure(self):
        # COH-4: the enclosure ambient-rise correction rides the same
        # forced-flow gate as the free-stream speed (it needs THERMAL not to
        # have run yet, same as the velocity), so it is expected to reach
        # THERMAL whenever the velocity does.
        from application.campaign_controller import CampaignEngine
        from models import CFDBoundaryPatch, EnclosureCFDResult

        request = self._request([
            CFDBoundaryPatch("Fan", "FAN", "XMIN", 0.5, 0.5, 0.5, 0.5, 0.8, 25.0),
        ])
        CampaignEngine(domain_engines={})._couple_cfd_into_thermal(
            request,
            EnclosureCFDResult(board_free_stream_velocity_m_s=0.42, board_free_stream_cells=17),
            lambda _m: None,
        )
        self.assertIs(
            request.domain_requests["THERMAL"].enclosure,
            request.domain_requests["CFD"].settings,
        )

    def test_an_unsampled_field_leaves_thermal_untouched(self):
        # A mesh too coarse to offer cells clear of every solid must not
        # silently hand thermal a zero and call it a resolved velocity.
        from application.campaign_controller import CampaignEngine
        from models import CFDBoundaryPatch, EnclosureCFDResult

        request = self._request([
            CFDBoundaryPatch("Fan", "FAN", "XMIN", 0.5, 0.5, 0.5, 0.5, 0.8, 25.0),
        ])
        CampaignEngine(domain_engines={})._couple_cfd_into_thermal(
            request,
            EnclosureCFDResult(
                board_free_stream_velocity_m_s=0.0, board_free_stream_cells=0,
            ),
            lambda _m: None,
        )
        self.assertIsNone(request.domain_requests["THERMAL"].air_velocity_m_s)


    def test_buoyant_flow_hands_nothing_to_a_thermal_run_already_done(self):
        # P02 Rev E, 2026-09-22 21:58: sealed enclosure, CFD after THERMAL,
        # and the log still said thermal "keeps its configured airflow".
        from application.campaign_controller import CampaignEngine
        from models import EnclosureCFDResult

        request = self._request([])
        logs = []
        CampaignEngine(domain_engines={})._couple_cfd_into_thermal(
            request,
            EnclosureCFDResult(
                board_free_stream_velocity_m_s=0.15, board_free_stream_cells=40,
            ),
            logs.append,
        )
        self.assertIsNone(request.domain_requests["THERMAL"].air_velocity_m_s)
        self.assertEqual(logs, [])

class TheDialogHandsTheVelocityToThermal(unittest.TestCase):
    """The coupling has to live on the path the user actually takes.

    It was built in CampaignEngine first, but nothing reaches CampaignEngine:
    on_build_campaign_report consolidates finished results instead of running
    the engine, so grep finds no production caller. The rule is re-tested here
    against the dialog method, which the Run-Thermal button does reach.

    The method touches only cfd_result, _cfd_forced_flow and log, so it is
    exercised against a stub rather than a constructed wx.Dialog. Importing
    ui.main_dialog can still lose the wx-stub race that test_campaign_button
    documents, hence the skip.
    """

    def setUp(self):
        try:
            from ui.main_dialog import KiPIDA_MainDialog
        except Exception as exc:  # pragma: no cover - depends on import order
            self.skipTest(f"ui.main_dialog unavailable: {exc}")
        self.method = KiPIDA_MainDialog._cfd_free_stream_for_thermal

    def _stub(self, forced, velocity, cells):
        import types

        from models import EnclosureCFDResult

        return types.SimpleNamespace(
            cfd_result=EnclosureCFDResult(
                board_free_stream_velocity_m_s=velocity,
                board_free_stream_cells=cells,
            ),
            _cfd_forced_flow=forced,
            log=lambda _message: None,
        )

    def test_forced_flow_hands_over_the_speed(self):
        self.assertAlmostEqual(self.method(self._stub(True, 0.31, 22)), 0.31)

    def test_buoyancy_hands_over_nothing(self):
        self.assertIsNone(self.method(self._stub(False, 0.31, 22)))

    def test_an_unsampled_field_hands_over_nothing(self):
        self.assertIsNone(self.method(self._stub(True, 0.0, 0)))

    def test_no_cfd_run_yet_hands_over_nothing(self):
        import types

        stub = types.SimpleNamespace(log=lambda _m: None)
        self.assertIsNone(self.method(stub))


class TheDialogHandsTheEnclosureToThermal(unittest.TestCase):
    """COH-4: same gating rule as the free-stream speed above, on the panel's
    enclosure settings instead -- only once the user has actually run the
    enclosure CFD this session, not from its always-present board-fitted
    defaults."""

    def setUp(self):
        try:
            from ui.main_dialog import KiPIDA_MainDialog
        except Exception as exc:  # pragma: no cover - depends on import order
            self.skipTest(f"ui.main_dialog unavailable: {exc}")
        self.method = KiPIDA_MainDialog._enclosure_profile_for_thermal

    def test_a_run_cfd_result_hands_over_the_enclosure(self):
        import types

        from models import EnclosureCFDResult, EnclosureCFDSettings

        settings = EnclosureCFDSettings()
        stub = types.SimpleNamespace(
            cfd_result=EnclosureCFDResult(),
            cfd_panel=types.SimpleNamespace(get_settings=lambda: settings),
        )
        self.assertIs(self.method(stub), settings)

    def test_no_cfd_run_yet_hands_over_nothing(self):
        import types

        stub = types.SimpleNamespace(cfd_result=None)
        self.assertIsNone(self.method(stub))


class UnderResolvedMeshesAreReported(unittest.TestCase):
    """At six cells across a channel the solver produced no boundary layer."""

    def _adapt(self, cells_across):
        from analysis_adapters import adapt_cfd_result
        from models import EnclosureCFDResult

        mesh = type("Mesh", (), {
            "shape": (40, cells_across, 40), "cell_count": 40 * cells_across * 40,
        })()
        # The reference board's own figures, so the assertions below are about
        # a case that actually occurred rather than an invented one.
        return adapt_cfd_result(mesh, EnclosureCFDResult(
            converged=True,
            maximum_solid_temperature_c=339.177,
            maximum_air_temperature_c=186.994,
        ))

    def test_mesh_resolution_is_a_limitation_not_a_scored_finding(self):
        # docs/audit-2026-09-23.md CFD-4/COH-9: mesh adequacy is not a defect
        # this run discovered -- CFD-1 and CFD-2 were, and both are fixed --
        # so it must not score or rank as one. Neither CFD-003 nor CFD-004
        # exists as a finding at any resolution.
        for cells_across in (6, 10, 14, 20):
            rule_ids = {f.rule_id for f in self._adapt(cells_across).findings}
            self.assertNotIn("CFD-003", rule_ids)
            self.assertNotIn("CFD-004", rule_ids)

    def test_a_coarse_enclosure_is_recorded_as_a_limitation(self):
        limitations = " ".join(self._adapt(6).limitations).lower()
        self.assertIn("mesh resolution", limitations)

    def test_a_resolved_enclosure_is_not(self):
        limitations = " ".join(self._adapt(20).limitations).lower()
        self.assertNotIn("mesh resolution", limitations)

    def test_a_hot_solid_is_marked_not_usable_not_denied_its_surface_film(self):
        """The claims that outlived the code, asserted on rather than assumed.

        This used to say the solver had "no surface film coefficient" for
        four commits after solid-air faces got one, then blamed an
        "unresolved buoyant plume" for the same number -- both wrong; the
        real causes were CFD-1 (an obstacle collapsing to an isolated cell)
        and CFD-2 (non-conservative energy advection), both fixed above. On
        the reference board this read 339 C against 72.6 C from the 3D
        thermal analysis of the same board at the same power.
        """
        adapted = self._adapt(10)
        metrics = {m.key: m for m in adapted.metrics}
        self.assertEqual(metrics["maximum_solid_temperature"].status, "NOT_USABLE")
        self.assertEqual(metrics["maximum_air_temperature"].status, "NOT_USABLE")
        text = " ".join(adapted.limitations)
        self.assertNotIn("no surface film", text)
        self.assertNotIn("buoyant plume", text)
        self.assertIn("surface film", text)
        self.assertIn("not usable", text)

    def test_the_reference_board_resolution_is_flagged(self):
        # p02_alimentation meshes to 24 x 20 x 10. An earlier threshold warned
        # only below 10, so exactly 10 slipped through silently -- and 10 cells
        # is the 7.1% case. Warning has to start where the error does.
        limitations = " ".join(self._adapt(10).limitations).lower()
        self.assertIn("mesh resolution", limitations)


class SealedEnclosuresReportNoMassBalance(unittest.TestCase):
    """A sealed box has no through-flow, so 0% is not a passed check.

    The reference board logged "mass error=0%" for a buoyancy-only enclosure,
    which reads exactly like the tautology this replaced.
    """

    def test_a_sealed_enclosure_marks_the_balance_inapplicable(self):
        from cfd_mesh import CFDMeshGenerator
        from cfd_model import CFDObstacle, EnclosureModel

        settings = EnclosureCFDSettings()
        settings.geometry.width_mm = 30.0
        settings.geometry.depth_mm = 30.0
        settings.geometry.height_mm = 30.0
        settings.solver.cell_size_mm = 6.0
        settings.solver.max_iterations = 6
        model = EnclosureModel(
            dimensions_mm=(30.0, 30.0, 30.0),
            obstacles=[CFDObstacle("HOT", (12, 12, 12, 18, 18, 18), 5.0, 0.5)],
        )
        mesh = CFDMeshGenerator().generate_mesh(model, settings)

        result = EnclosureCFDSolver().solve(mesh, settings)

        self.assertFalse(result.mass_balance_applicable)

    def test_the_conservation_finding_does_not_fire_on_a_sealed_box(self):
        from analysis_adapters import adapt_cfd_result
        from models import EnclosureCFDResult

        adapted = adapt_cfd_result(None, EnclosureCFDResult(
            converged=True, mass_balance_error_pct=0.0,
            mass_balance_applicable=False,
        ))
        mass = [
            f for f in adapted.findings
            if f.rule_id == "CFD-002" and "Mass" in f.title
        ]
        self.assertEqual(mass, [])


class SolidFacesGetASurfaceFilm(unittest.TestCase):
    """A solid shedding heat into air needs a film, not cell conduction.

    Without one the only paths off a component are conduction into still air
    and advection between cells, and at millimetre resolution neither carries
    the heat: the reference board reported a 339 C solid against 72.6 C from
    the 3D thermal analysis of the same board at the same power.
    """

    def _hot_box(self, film_h):
        from cfd_mesh import CFDMeshGenerator
        from cfd_model import CFDObstacle, EnclosureModel
        from cfd_solver import EnclosureCFDSolver

        settings = EnclosureCFDSettings()
        settings.geometry.width_mm = 40.0
        settings.geometry.depth_mm = 40.0
        settings.geometry.height_mm = 40.0
        settings.solver.cell_size_mm = 5.0
        settings.solver.max_iterations = 20
        model = EnclosureModel((40.0, 40.0, 40.0), obstacles=[
            CFDObstacle("HOT", (15, 15, 15, 25, 25, 25), 8.0, 2.0),
        ])
        mesh = CFDMeshGenerator().generate_mesh(model, settings)
        solver = EnclosureCFDSolver()
        solver._film_coefficient = staticmethod(lambda *a, **k: film_h)
        return solver.solve(mesh, settings).maximum_solid_temperature_c

    def test_a_better_film_gives_a_cooler_solid(self):
        # Two positive coefficients rather than one and zero: h = 0 makes the
        # solid adiabatic and the matrix singular, which is a property of the
        # test rather than of the physics.
        self.assertLess(self._hot_box(film_h=50.0), self._hot_box(film_h=5.0))

    def test_the_film_is_a_series_path_not_a_replacement_short(self):
        # The film must not make the interface conduct better than a solid
        # weld: a physical bound the arithmetic has to respect.
        from cfd_solver import EnclosureCFDSolver

        film = EnclosureCFDSolver._film_coefficient(
            delta_t_k=50.0, length_m=0.005, speed_m_s=0.1,
            ambient_c=25.0, emissivity=0.0,
        )
        self.assertGreater(film, 0.0)
        self.assertLess(film, 500.0)


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
        settings.solver.include_buoyancy = False
        model = EnclosureModel((30.0, 30.0, 30.0), patches=[
            CFDBoundaryPatch("Fan", "FAN", "XMIN", 0.5, 0.5, 0.5, 0.5, 0.8, 25.0),
            CFDBoundaryPatch("Outlet", "OUTLET", "XMAX", 0.5, 0.5, 0.5, 0.5),
        ])
        mesh = CFDMeshGenerator().generate_mesh(model, settings)

        result = EnclosureCFDSolver().solve(mesh, settings)

        # The threshold has to sit well above float noise, or the test would
        # still pass against the old tautology: that reported 4e-14 %, which is
        # dutifully greater than zero.
        #
        # It used to assert > 1.0 because this case reported about 10%. Solving
        # the pressure Poisson system with the sparse backend instead of Jacobi
        # sweeps took that to ~0.13%, so the old threshold now fails on an
        # improvement. What the test is for is unchanged -- the figure must be
        # measured rather than imposed -- so the bound moves to what separates a
        # real measurement from 1e-14, not to what the solver happens to score.
        self.assertGreater(result.mass_balance_error_pct, 1.0e-6)

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
        settings.solver.include_buoyancy = False
        model = EnclosureModel((30.0, 30.0, 30.0), patches=[
            CFDBoundaryPatch("Fan", "FAN", "XMIN", 0.5, 0.5, 0.5, 0.5, 0.8, 25.0),
            CFDBoundaryPatch("Outlet", "OUTLET", "XMAX", 0.5, 0.5, 0.5, 0.5),
        ])
        mesh = CFDMeshGenerator().generate_mesh(model, settings)

        result = EnclosureCFDSolver().solve(mesh, settings)

        # Measured against the start, not the midpoint. Twelve iterations is
        # too few for the flow to have settled, and the residual genuinely
        # rises again mid-run as the velocity field grows before it plateaus
        # (0.0667 -> 0.00087 -> 0.0016 here, flat at 0.0014 by 60 iterations).
        # Asserting monotonicity would be asserting something untrue.
        continuity = result.residuals.continuity
        self.assertLess(continuity[-1], 0.1 * continuity[0])

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

    def _forced_case(self, iterations):
        from cfd_mesh import CFDMeshGenerator
        from cfd_model import EnclosureModel
        from models import CFDBoundaryPatch

        settings = EnclosureCFDSettings()
        settings.geometry.width_mm = 30.0
        settings.geometry.depth_mm = 30.0
        settings.geometry.height_mm = 30.0
        settings.solver.cell_size_mm = 6.0
        settings.solver.max_iterations = iterations
        settings.solver.include_buoyancy = False
        model = EnclosureModel((30.0, 30.0, 30.0), patches=[
            CFDBoundaryPatch("Fan", "FAN", "XMIN", 0.5, 0.5, 0.5, 0.5, 0.8, 25.0),
            CFDBoundaryPatch("Outlet", "OUTLET", "XMAX", 0.5, 0.5, 0.5, 0.5),
        ])
        mesh = CFDMeshGenerator().generate_mesh(model, settings)
        return EnclosureCFDSolver().solve(mesh, settings)

    def test_a_settled_forced_run_can_actually_converge(self):
        # `converged` was unreachable for the life of this solver. The residual
        # summed over the six prescribed outer layers, where the projection has
        # no degrees of freedom, so it floored at ~1e-2 regardless of effort.
        self.assertTrue(self._forced_case(250).converged)

    def test_excluding_prescribed_cells_did_not_hide_a_real_error(self):
        # The guard against narrowing the measurement until it passes: mass
        # balance is an independent physical check that was NOT changed. If
        # restricting the residual had masked a genuine divergence problem,
        # this number would have moved. It did not (0.0845% before and after).
        result = self._forced_case(250)
        self.assertLess(result.mass_balance_error_pct, 1.0)
        self.assertGreater(result.mass_balance_error_pct, 1.0e-6)

if __name__ == "__main__":
    unittest.main()
