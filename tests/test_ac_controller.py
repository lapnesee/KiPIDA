import os
import sys
import threading
import time
from types import SimpleNamespace
import unittest

plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

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


class SavedBoardCacheTests(unittest.TestCase):
    def test_the_saved_board_is_parsed_again_only_once_it_changes(self):
        # P02 Rev E, 2026-09-29 18:23: three AC profiles parsed the same file
        # in turn on the UI thread, 0.9 s each.
        import tempfile
        from pathlib import Path
        from unittest import mock
        from application import ac_controller

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "board.kicad_pcb"
            path.write_text("(kicad_pcb)")
            with mock.patch.object(ac_controller, "read_board", side_effect=lambda p: object()) as read:
                first = ac_controller._read_saved_board(path)
                self.assertIs(ac_controller._read_saved_board(path), first)
                path.write_text("(kicad_pcb (version 2))")
                self.assertIsNot(ac_controller._read_saved_board(path), first)
        self.assertEqual(read.call_count, 2)


class DeferredNetworkTests(unittest.TestCase):
    def test_a_profile_whose_network_fails_to_build_skips_only_itself(self):
        # The hybrid networks are now meshed by the engine, off the UI thread
        # (16 s per profile there on P02 Rev E); a profile over the node limit
        # must still skip itself only, as it did when built in the dialog.
        from unittest import mock
        import application.ac_controller as controller

        def build(inputs, settings, emit):
            if settings.rail_name == "/VBUS_SYS":
                raise ValueError("AC mesh contains 402,000 nodes")
            return "network"

        requests = tuple(
            ACRunRequest(settings=SimpleNamespace(rail_name=name), network=None, _build=object())
            for name in ("/VBUS_SYS", "/+5V_RAIL")
        )
        engine = controller.ACSolverEngine(solver_factory=FakeSolver)
        with mock.patch.object(controller, "build_ac_network", build):
            outcome = engine.solve(controller.ACBatchRequest(requests), lambda _m: None,
                                   lambda *_a: None, lambda: False)

        self.assertEqual([item.settings.rail_name for item, *_ in outcome.runs], ["/+5V_RAIL"])
        self.assertEqual(outcome.runs[0][0].network, "network")
        self.assertEqual(outcome.skipped, (("/VBUS_SYS", "AC mesh contains 402,000 nodes"),))


if __name__ == "__main__":
    unittest.main()
