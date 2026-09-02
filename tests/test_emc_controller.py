import threading
import unittest

from application.emc_controller import (
    EMCAnalysisCancelled, EMCAnalysisController, EMCControllerCallbacks,
    EMCRunOutcome,
)


class FakeEngine:
    def solve(self, request, emit, progress, cancelled):
        emit("emc worker started")
        progress(1, 1, "rules")
        if cancelled():
            raise EMCAnalysisCancelled("EMI/EMC analysis cancelled.")
        return EMCRunOutcome("settings", "result", b"risk", b"spectrum")


class BlockingEngine:
    release = threading.Event()

    def solve(self, request, emit, progress, cancelled):
        self.release.wait(2.0)
        if cancelled():
            raise EMCAnalysisCancelled("EMI/EMC analysis cancelled.")
        return FakeEngine().solve(request, emit, progress, cancelled)


class EMCControllerTests(unittest.TestCase):
    @staticmethod
    def callbacks(completed, errors, progress=None, logs=None):
        return EMCControllerCallbacks(
            on_progress=lambda *args: (progress if progress is not None else []).append(args),
            on_complete=completed.append,
            on_error=errors.append,
            on_log=lambda message: (logs if logs is not None else []).append(message),
        )

    def test_background_analysis_returns_all_artifacts(self):
        completed, errors, progress, logs = [], [], [], []
        controller = EMCAnalysisController(engine=FakeEngine())
        controller.start("request", self.callbacks(completed, errors, progress, logs))
        self.assertTrue(controller.wait(2.0))
        self.assertEqual(completed[0].risk_png, b"risk")
        self.assertEqual(completed[0].spectrum_png, b"spectrum")
        self.assertEqual(progress, [(1, 1, "rules")])
        self.assertEqual(logs, ["emc worker started"])
        self.assertFalse(errors)

    def test_concurrent_start_and_cancellation(self):
        BlockingEngine.release.clear()
        completed, errors = [], []
        controller = EMCAnalysisController(engine=BlockingEngine())
        controller.start("request", self.callbacks(completed, errors))
        with self.assertRaisesRegex(RuntimeError, "already running"):
            controller.start("other", self.callbacks([], []))
        self.assertTrue(controller.cancel())
        BlockingEngine.release.set()
        self.assertTrue(controller.wait(2.0))
        self.assertFalse(completed)
        self.assertIsInstance(errors[0], EMCAnalysisCancelled)

    def test_errors_cross_the_controller_boundary(self):
        class BrokenEngine:
            def solve(self, request, emit, progress, cancelled):
                raise ValueError("bad emc snapshot")

        completed, errors = [], []
        controller = EMCAnalysisController(engine=BrokenEngine())
        controller.start("request", self.callbacks(completed, errors))
        self.assertTrue(controller.wait(2.0))
        self.assertFalse(completed)
        self.assertEqual(str(errors[0]), "bad emc snapshot")


if __name__ == "__main__":
    unittest.main()
