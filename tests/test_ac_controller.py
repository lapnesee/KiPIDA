import threading
import time
from types import SimpleNamespace
import unittest

from application.ac_controller import (
    ACAnalysisCancelled, ACAnalysisController, ACControllerCallbacks, ACRunRequest,
)


class FakeSolver:
    def __init__(self, debug=False, log_callback=None, compute_settings=None):
        self.log_callback = log_callback

    def solve_sweep(self, network, settings, progress_callback=None):
        if self.log_callback:
            self.log_callback("solver started")
        if progress_callback:
            progress_callback(1, 2, 1000.0)
            progress_callback(2, 2, 2000.0)
        return "sweep-result"


class FakeOptimizer:
    def __init__(self, solver, debug=False, log_callback=None):
        self.log_callback = log_callback

    def optimize(self, network, settings, progress_callback=None):
        if progress_callback:
            progress_callback(1, 1, "C1")
        return SimpleNamespace(baseline="baseline", optimized="optimized")


class BlockingSolver(FakeSolver):
    release = threading.Event()

    def solve_sweep(self, network, settings, progress_callback=None):
        self.release.wait(2.0)
        if progress_callback:
            progress_callback(1, 1, "done")
        return "done"


class ACAnalysisControllerTests(unittest.TestCase):
    def callbacks(self, completed, errors, progress=None, logs=None):
        return ACControllerCallbacks(
            on_progress=lambda *args: (progress if progress is not None else []).append(args),
            on_complete=lambda result, optimization: completed.append((result, optimization)),
            on_error=errors.append,
            on_log=lambda message: (logs if logs is not None else []).append(message),
        )

    def test_analysis_runs_off_caller_thread_and_reports_progress(self):
        completed, errors, progress, logs = [], [], [], []
        callback_threads = []
        caller_thread = threading.get_ident()
        callbacks = ACControllerCallbacks(
            on_progress=lambda *args: (progress.append(args), callback_threads.append(threading.get_ident())),
            on_complete=lambda *args: (completed.append(args), callback_threads.append(threading.get_ident())),
            on_error=errors.append,
            on_log=logs.append,
        )
        controller = ACAnalysisController(solver_factory=FakeSolver)
        controller.start(ACRunRequest("settings", "network"), callbacks)
        self.assertTrue(controller.wait(2.0))
        self.assertEqual(completed, [("sweep-result", None)])
        self.assertEqual(len(progress), 2)
        self.assertEqual(logs, ["solver started"])
        self.assertFalse(errors)
        self.assertTrue(all(thread_id != caller_thread for thread_id in callback_threads))

    def test_optimizer_returns_baseline_and_optimization(self):
        completed, errors = [], []
        controller = ACAnalysisController(
            solver_factory=FakeSolver, optimizer_factory=FakeOptimizer,
        )
        controller.start(
            ACRunRequest("settings", "network", optimize=True),
            self.callbacks(completed, errors),
        )
        self.assertTrue(controller.wait(2.0))
        self.assertEqual(completed[0][0], "baseline")
        self.assertEqual(completed[0][1].optimized, "optimized")
        self.assertFalse(errors)

    def test_concurrent_start_is_rejected(self):
        BlockingSolver.release.clear()
        controller = ACAnalysisController(solver_factory=BlockingSolver)
        completed, errors = [], []
        controller.start(ACRunRequest("settings", "network"), self.callbacks(completed, errors))
        with self.assertRaisesRegex(RuntimeError, "already running"):
            controller.start(ACRunRequest("settings", "network"), self.callbacks([], []))
        BlockingSolver.release.set()
        self.assertTrue(controller.wait(2.0))

    def test_cancel_is_reported_as_a_typed_error(self):
        BlockingSolver.release.clear()
        controller = ACAnalysisController(solver_factory=BlockingSolver)
        completed, errors = [], []
        controller.start(ACRunRequest("settings", "network"), self.callbacks(completed, errors))
        self.assertTrue(controller.cancel())
        BlockingSolver.release.set()
        self.assertTrue(controller.wait(2.0))
        self.assertFalse(completed)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ACAnalysisCancelled)

    def test_solver_errors_cross_the_controller_boundary(self):
        class BrokenSolver(FakeSolver):
            def solve_sweep(self, network, settings, progress_callback=None):
                raise ValueError("bad network")

        completed, errors = [], []
        controller = ACAnalysisController(solver_factory=BrokenSolver)
        controller.start(ACRunRequest("settings", "network"), self.callbacks(completed, errors))
        self.assertTrue(controller.wait(2.0))
        self.assertFalse(completed)
        self.assertEqual(str(errors[0]), "bad network")


if __name__ == "__main__":
    unittest.main()
