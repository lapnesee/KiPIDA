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


if __name__ == "__main__":
    unittest.main()
