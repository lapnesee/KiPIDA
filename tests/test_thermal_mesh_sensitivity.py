"""The thermal result carries an error bar: the hotspot solved at twice the step (audit R7)."""

import os
import sys
import unittest
from types import SimpleNamespace

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


def _settings(check=True):
    from models import AirflowSettings, ThermalAnalysisSettings

    return ThermalAnalysisSettings(
        grid_size_mm=1.0, include_dc_copper_losses=False, mesh_sensitivity_check=check,
        airflow=AirflowSettings(),
    )


class EngineSolvesTwice(unittest.TestCase):
    def _engine(self, hotspots, grids):
        from application.thermal_controller import ThermalSolverEngine

        class Mesher:
            def __init__(self, **_kwargs):
                pass

            def generate_mesh(self, model, settings, progress_callback=None, air_velocity_m_s=None):
                grids.append(settings.grid_size_mm)
                return SimpleNamespace(nodes=[0], grid=settings.grid_size_mm)

        class Solver:
            def __init__(self, **_kwargs):
                pass

            def solve_with_surface_refinement(self, mesh, settings, progress_callback=None):
                return SimpleNamespace(
                    hotspot=SimpleNamespace(temperature_c=hotspots[mesh.grid]),
                    effective_grid_size_mm=mesh.grid,
                )

        return ThermalSolverEngine(mesher_factory=Mesher, solver_factory=Solver)

    def _request(self, **kwargs):
        from dataclasses import dataclass, field

        from application.thermal_controller import ThermalRunRequest

        @dataclass
        class Model:
            copper_losses: list = field(default_factory=list)

        return ThermalRunRequest(settings=kwargs.pop("settings", _settings()), board_model=Model(), **kwargs)

    def test_the_hotspot_is_solved_again_at_twice_the_step(self):
        grids = []
        engine = self._engine({1.0: 101.3, 2.0: 105.7}, grids)
        request = self._request()
        outcome = engine.solve(request, lambda _m: None, lambda *_: None, lambda: False)
        self.assertEqual(grids, [1.0, 2.0])
        self.assertAlmostEqual(outcome.mesh_sensitivity["delta_c"], 4.4, places=6)
        self.assertEqual(outcome.mesh_sensitivity["coarse_grid_mm"], 2.0)
        # The user's settings object is not the one coarsened.
        self.assertEqual(request.settings.grid_size_mm, 1.0)

    def test_turned_off_nothing_more_is_solved(self):
        grids = []
        outcome = self._engine({1.0: 101.3}, grids).solve(
            self._request(settings=_settings(check=False)), lambda _m: None, lambda *_: None, lambda: False,
        )
        self.assertEqual((grids, outcome.mesh_sensitivity), ([1.0], None))


class CoupledEngineAlsoSolvesTwice(unittest.TestCase):
    """TH-3 / COH-6: a campaign always runs the coupled path
    (ui/main_dialog.py:497), so the double-step check used to never run even
    though the P02 sidecar carries mesh_sensitivity_check=true."""

    def _request(self, **kwargs):
        from dataclasses import dataclass, field
        from application.thermal_controller import ThermalRunRequest

        @dataclass
        class Model:
            copper_losses: list = field(default_factory=list)
            components: list = field(default_factory=list)

        return ThermalRunRequest(
            settings=kwargs.pop("settings", _settings()), board_model=Model(), coupled=True,
            _dc_results={"5V": {"mesh": SimpleNamespace(branches=[], node_coords={})}},
            **kwargs,
        )

    def test_coupled_campaign_also_gets_the_double_step_check(self):
        from application.thermal_controller import ThermalSolverEngine

        grids = []

        class Mesher:
            def __init__(self, **_kwargs):
                pass

            def generate_mesh(self, model, settings, progress_callback=None, air_velocity_m_s=None):
                grids.append(settings.grid_size_mm)
                return SimpleNamespace(
                    nodes=[0], grid=settings.grid_size_mm, surface_h_w_m2k={}, heat_sources_w={},
                )

        class Solver:
            def __init__(self, **_kwargs):
                pass

            def solve_with_surface_refinement(self, mesh, settings, progress_callback=None):
                hotspots = {1.0: 101.3, 2.0: 105.7}
                return SimpleNamespace(
                    hotspot=SimpleNamespace(temperature_c=hotspots[mesh.grid]),
                    effective_grid_size_mm=mesh.grid,
                )

        class CoupledSolver:
            def __init__(self, **_kwargs):
                pass

            def solve(self, mesh, settings, rail_contexts, progress_callback=None, rails=None):
                thermal = SimpleNamespace(
                    hotspot=SimpleNamespace(temperature_c=101.3), effective_grid_size_mm=mesh.grid,
                )
                return SimpleNamespace(thermal=thermal, dc_results={}, iterations=1, converged=True)

        engine = ThermalSolverEngine(
            mesher_factory=Mesher, solver_factory=Solver, coupled_solver_factory=CoupledSolver,
        )
        outcome = engine.solve(self._request(), lambda _m: None, lambda *_: None, lambda: False)

        self.assertEqual(grids, [1.0, 2.0])
        self.assertAlmostEqual(outcome.mesh_sensitivity["delta_c"], 4.4, places=6)

    def test_a_failed_frozen_resolve_is_reported_not_verified(self):
        from application.thermal_controller import ThermalSolverEngine

        calls = {"n": 0}

        class FlakyMesher:
            def __init__(self, **_kwargs):
                pass

            def generate_mesh(self, model, settings, progress_callback=None, air_velocity_m_s=None):
                calls["n"] += 1
                # The fine mesh (call 1) must succeed for the coupled solve
                # itself to run; only the coarse, sensitivity-only re-solve
                # (call 2) fails here, mimicking a real solver error.
                if calls["n"] > 1:
                    raise RuntimeError("synthetic coarse-mesh failure")
                return SimpleNamespace(
                    nodes=[0], grid=settings.grid_size_mm, surface_h_w_m2k={}, heat_sources_w={},
                )

        class Solver:
            def __init__(self, **_kwargs):
                pass

            def solve_with_surface_refinement(self, mesh, settings, progress_callback=None):
                return SimpleNamespace(
                    hotspot=SimpleNamespace(temperature_c=101.3), effective_grid_size_mm=mesh.grid,
                )

        class CoupledSolver:
            def __init__(self, **_kwargs):
                pass

            def solve(self, mesh, settings, rail_contexts, progress_callback=None, rails=None):
                thermal = SimpleNamespace(
                    hotspot=SimpleNamespace(temperature_c=101.3), effective_grid_size_mm=mesh.grid,
                )
                return SimpleNamespace(thermal=thermal, dc_results={}, iterations=1, converged=True)

        engine = ThermalSolverEngine(
            mesher_factory=FlakyMesher, solver_factory=Solver, coupled_solver_factory=CoupledSolver,
        )
        outcome = engine.solve(self._request(), lambda _m: None, lambda *_: None, lambda: False)

        self.assertIn("not_verified_reason", outcome.mesh_sensitivity)


class AdapterStatesIt(unittest.TestCase):
    def _result(self, delta):
        from analysis_adapters import adapt_thermal_result

        return adapt_thermal_result(
            SimpleNamespace(converged=True, energy_balance_error_pct=0.0),
            mesh_sensitivity={
                "fine_grid_mm": 1.0, "coarse_grid_mm": 2.0,
                "fine_hotspot_c": 101.3, "coarse_hotspot_c": 101.3 + delta, "delta_c": delta,
            },
        )

    def test_more_than_two_degrees_is_a_finding_about_the_run(self):
        from analysis_contract import EvidenceConfidence

        result = self._result(4.4)
        finding = next(item for item in result.findings if item.rule_id == "TH-004")
        self.assertEqual(finding.confidence, EvidenceConfidence.DETERMINISTIC)
        self.assertIn("4.40 C apart", finding.description)
        metric = next(item for item in result.metrics if item.key == "hotspot_mesh_sensitivity")
        self.assertEqual((metric.value, metric.status), (4.4, "WARN"))

    def test_agreement_is_published_without_being_called_convergence(self):
        result = self._result(0.8)
        self.assertNotIn("TH-004", {item.rule_id for item in result.findings})
        self.assertTrue(any("does not prove" in item for item in result.limitations))

    def test_not_computed_means_not_mentioned(self):
        from analysis_adapters import adapt_thermal_result

        result = adapt_thermal_result(SimpleNamespace(converged=True, energy_balance_error_pct=0.0))
        self.assertFalse(any(item.key == "hotspot_mesh_sensitivity" for item in result.metrics))

    def test_a_skipped_coupled_check_says_not_verified_not_silence(self):
        from analysis_adapters import adapt_thermal_result

        result = adapt_thermal_result(
            SimpleNamespace(converged=True, energy_balance_error_pct=0.0),
            mesh_sensitivity={"not_verified_reason": "synthetic coarse-mesh failure"},
        )
        self.assertFalse(any(item.key == "hotspot_mesh_sensitivity" for item in result.metrics))
        self.assertTrue(any("not verified" in item for item in result.limitations))


class SettingIsDeclaredOnce(unittest.TestCase):
    def test_on_by_default_and_a_saved_choice_survives(self):
        import config_manager
        from models import ThermalAnalysisSettings

        self.assertTrue(ThermalAnalysisSettings().mesh_sensitivity_check)
        self.assertTrue(config_manager._dict_to_thermal_settings({}).mesh_sensitivity_check)
        saved = config_manager._thermal_settings_to_dict(ThermalAnalysisSettings(mesh_sensitivity_check=False))
        self.assertFalse(config_manager._dict_to_thermal_settings(saved).mesh_sensitivity_check)


if __name__ == "__main__":
    unittest.main()
