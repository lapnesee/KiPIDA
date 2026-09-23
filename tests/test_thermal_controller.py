import threading
from types import SimpleNamespace
import unittest

from application.thermal_controller import (
    ThermalAnalysisCancelled,
    ThermalAnalysisController,
    ThermalControllerCallbacks,
    ThermalRunOutcome,
    thermal_cache_key,
)


class FakeEngine:
    def solve(self, request, emit, progress, cancelled):
        emit("thermal worker started")
        progress(1, 2, "mesh")
        progress(2, 2, "solve")
        if cancelled():
            raise ThermalAnalysisCancelled("Thermal analysis cancelled.")
        return ThermalRunOutcome(
            mesh="mesh", result="result", coupled_result=None,
            system_results={}, cache_key="key", cache_value="cached",
            elapsed_seconds=0.1,
        )


class BlockingEngine:
    release = threading.Event()

    def solve(self, request, emit, progress, cancelled):
        self.release.wait(2.0)
        if cancelled():
            raise ThermalAnalysisCancelled("Thermal analysis cancelled.")
        return FakeEngine().solve(request, emit, progress, cancelled)


class ThermalControllerTests(unittest.TestCase):
    @staticmethod
    def callbacks(completed, errors, progress=None, logs=None):
        return ThermalControllerCallbacks(
            on_progress=lambda *args: (progress if progress is not None else []).append(args),
            on_complete=completed.append,
            on_error=errors.append,
            on_log=lambda message: (logs if logs is not None else []).append(message),
        )

    def test_analysis_runs_in_worker_and_marshals_every_callback(self):
        dispatched, completed, errors, progress, logs = [], [], [], [], []

        def dispatch(callback, *args):
            dispatched.append((callback, args))
            callback(*args)

        controller = ThermalAnalysisController(dispatch=dispatch, engine=FakeEngine())
        controller.start("request", self.callbacks(completed, errors, progress, logs))
        self.assertTrue(controller.wait(2.0))
        self.assertEqual(completed[0].result, "result")
        self.assertEqual(progress, [(1, 2, "mesh"), (2, 2, "solve")])
        self.assertEqual(logs, ["thermal worker started"])
        self.assertFalse(errors)
        self.assertEqual(len(dispatched), 4)

    def test_concurrent_start_is_rejected_and_cancel_is_typed(self):
        BlockingEngine.release.clear()
        controller = ThermalAnalysisController(engine=BlockingEngine())
        completed, errors = [], []
        controller.start("request", self.callbacks(completed, errors))
        with self.assertRaisesRegex(RuntimeError, "already running"):
            controller.start("other", self.callbacks([], []))
        self.assertTrue(controller.cancel())
        BlockingEngine.release.set()
        self.assertTrue(controller.wait(2.0))
        self.assertFalse(completed)
        self.assertIsInstance(errors[0], ThermalAnalysisCancelled)

    def test_solver_errors_cross_the_controller_boundary(self):
        class BrokenEngine:
            def solve(self, request, emit, progress, cancelled):
                raise ValueError("bad thermal model")

        completed, errors = [], []
        controller = ThermalAnalysisController(engine=BrokenEngine())
        controller.start("request", self.callbacks(completed, errors))
        self.assertTrue(controller.wait(2.0))
        self.assertFalse(completed)
        self.assertEqual(str(errors[0]), "bad thermal model")

    def test_cache_key_changes_with_physical_settings(self):
        component = SimpleNamespace(
            ref_des="U1", power_w=1.0, width_mm=2.0, depth_mm=3.0,
            height_mm=1.0, theta_jb_c_per_w=5.0, max_junction_c=125.0,
            enabled=True, model_source="user",
        )
        airflow = SimpleNamespace(
            mode="NATURAL", velocity_m_s=0.0, direction_deg=0.0,
            custom_h_w_m2k=5.0, expose_top=True, expose_bottom=True,
            expose_edges=True,
        )
        settings = SimpleNamespace(
            airflow=airflow, components=[component], grid_size_mm=1.0,
            ambient_c=25.0, include_radiation=True, emissivity=0.9,
            include_dc_copper_losses=False,
        )
        first = thermal_cache_key(settings, None, False, [])
        settings.ambient_c = 40.0
        second = thermal_cache_key(settings, None, False, [])
        self.assertNotEqual(first, second)


class EnclosureAirRiseTests(unittest.TestCase):
    def test_matches_p02_rev_e_measured_rise(self):
        # P02 Rev E sidecar: sealed 165 x 100 x 50 mm box, walls 5 W/m2K,
        # 4.9186 W total board heat (THERMAL.md) -> the audit's measured
        # ~16 K mean internal air rise (CFD-4). A_walls = 2*(0.165*0.100 +
        # 0.165*0.050 + 0.100*0.050) = 0.0595 m2; rise = 4.9186/(5*0.0595).
        from application.thermal_controller import enclosure_internal_air_rise_k
        from models import EnclosureCFDSettings, EnclosureGeometrySettings

        enclosure = EnclosureCFDSettings(geometry=EnclosureGeometrySettings(
            width_mm=165.0, depth_mm=100.0, height_mm=50.0,
            wall_heat_transfer_w_m2k=5.0,
        ))
        self.assertAlmostEqual(
            enclosure_internal_air_rise_k(enclosure, 4.9186), 16.53, places=1,
        )

    def test_no_heat_means_no_rise(self):
        from application.thermal_controller import enclosure_internal_air_rise_k
        from models import EnclosureCFDSettings

        self.assertEqual(enclosure_internal_air_rise_k(EnclosureCFDSettings(), 0.0), 0.0)


class EnclosureAmbientReachesTheSolve(unittest.TestCase):
    """COH-4: thermal used to ignore an enclosure entirely and solve at the
    bare outside ambient; a configured enclosure now shifts the ambient the
    mesh and solver actually see."""

    def _request(self, enclosure, apply_it=True):
        from dataclasses import dataclass, field

        from application.thermal_controller import ThermalRunRequest
        from models import ThermalAnalysisSettings, ThermalComponentModel

        @dataclass
        class Model:
            copper_losses: list = field(default_factory=list)
            components: list = field(default_factory=lambda: [
                ThermalComponentModel(ref_des="U1", power_w=4.9186, enabled=True),
            ])

        settings = ThermalAnalysisSettings(
            ambient_c=45.0, include_dc_copper_losses=False,
            apply_enclosure_ambient=apply_it,
        )
        return ThermalRunRequest(settings=settings, board_model=Model(), enclosure=enclosure)

    def _engine(self, seen):
        from application.thermal_controller import ThermalSolverEngine

        class Mesher:
            def __init__(self, **_kwargs):
                pass

            def generate_mesh(self, model, settings, progress_callback=None, air_velocity_m_s=None):
                seen["ambient_c"] = settings.ambient_c
                return SimpleNamespace(nodes=[0])

        class Solver:
            def __init__(self, **_kwargs):
                pass

            def solve_with_surface_refinement(self, mesh, settings, progress_callback=None):
                return SimpleNamespace(hotspot=None)

        return ThermalSolverEngine(mesher_factory=Mesher, solver_factory=Solver)

    def _enclosure(self):
        from models import EnclosureCFDSettings, EnclosureGeometrySettings

        return EnclosureCFDSettings(geometry=EnclosureGeometrySettings(
            width_mm=165.0, depth_mm=100.0, height_mm=50.0,
            wall_heat_transfer_w_m2k=5.0,
        ))

    def test_solves_at_ambient_plus_the_enclosure_rise(self):
        seen = {}
        outcome = self._engine(seen).solve(
            self._request(self._enclosure()), lambda _m: None, lambda *_: None, lambda: False,
        )
        # P02 Rev E, 2026-09-22: 45 C ambient + ~16.5 K from 4.9186 W in a
        # 165x100x50 mm, 5 W/m2K box (CFD-4's ~16 K measurement).
        self.assertAlmostEqual(seen["ambient_c"], 45.0 + 16.53, places=1)
        self.assertAlmostEqual(outcome.enclosure_air_rise_k, 16.53, places=1)
        self.assertTrue(outcome.enclosure_ambient_applied)

    def test_disabled_keeps_ambient_but_still_reports_the_rise(self):
        seen = {}
        outcome = self._engine(seen).solve(
            self._request(self._enclosure(), apply_it=False),
            lambda _m: None, lambda *_: None, lambda: False,
        )
        self.assertAlmostEqual(seen["ambient_c"], 45.0)
        self.assertAlmostEqual(outcome.enclosure_air_rise_k, 16.53, places=1)
        self.assertFalse(outcome.enclosure_ambient_applied)

    def test_no_enclosure_means_no_rise_at_all(self):
        seen = {}
        outcome = self._engine(seen).solve(
            self._request(None), lambda _m: None, lambda *_: None, lambda: False,
        )
        self.assertAlmostEqual(seen["ambient_c"], 45.0)
        self.assertIsNone(outcome.enclosure_air_rise_k)
        self.assertFalse(outcome.enclosure_ambient_applied)

    def test_coupled_run_says_why_no_rail_was_solved(self):
        # The P02 Rev E power tree came from a board scan and was never filled
        # in; the run only said it "requires a successful DC analysis".
        from application.thermal_controller import ThermalSolverEngine
        from models import PowerRail

        class NoRailSolved:
            def solve(self, request, emit, progress, cancelled):
                return {}

        request = SimpleNamespace(
            coupled=True, _dc_results=None,
            settings=SimpleNamespace(include_dc_copper_losses=True),
            dc_request=SimpleNamespace(rails=(PowerRail("/+5V_U4", 5.0), PowerRail("/+5V_RAIL", 5.0))),
        )
        with self.assertRaisesRegex(
            ValueError, r"None of the 2 rails has a source or a regulator output .*/\+5V_U4, /\+5V_RAIL",
        ):
            ThermalSolverEngine(dc_engine_factory=NoRailSolved).solve(
                request, lambda _message: None, lambda *_args: None, lambda: False,
            )


if __name__ == "__main__":
    unittest.main()
