import threading
import unittest

from application.differential_controller import (
    DifferentialAnalysisCancelled,
    DifferentialAnalysisController,
    DifferentialControllerCallbacks,
    DifferentialRunOutcome,
)


class FakeEngine:
    def solve(self, request, emit, progress, cancelled):
        emit("differential worker started")
        progress(1, 1, "PAIR")
        if cancelled():
            raise DifferentialAnalysisCancelled("Differential analysis cancelled.")
        return DifferentialRunOutcome(("result",), "stackup", b"z", b"s", 10.0)


class BlockingEngine:
    release = threading.Event()

    def solve(self, request, emit, progress, cancelled):
        self.release.wait(2.0)
        if cancelled():
            raise DifferentialAnalysisCancelled("Differential analysis cancelled.")
        return FakeEngine().solve(request, emit, progress, cancelled)


class DifferentialControllerTests(unittest.TestCase):
    @staticmethod
    def callbacks(completed, errors, progress=None, logs=None):
        return DifferentialControllerCallbacks(
            on_progress=lambda *args: (progress if progress is not None else []).append(args),
            on_complete=completed.append,
            on_error=errors.append,
            on_log=lambda message: (logs if logs is not None else []).append(message),
        )

    def test_background_analysis_reports_a_typed_outcome(self):
        completed, errors, progress, logs = [], [], [], []
        controller = DifferentialAnalysisController(engine=FakeEngine())
        controller.start("request", self.callbacks(completed, errors, progress, logs))
        self.assertTrue(controller.wait(2.0))
        self.assertEqual(completed[0].results, ("result",))
        self.assertEqual(progress, [(1, 1, "PAIR")])
        self.assertEqual(logs, ["differential worker started"])
        self.assertFalse(errors)

    def test_concurrent_start_and_cancellation(self):
        BlockingEngine.release.clear()
        completed, errors = [], []
        controller = DifferentialAnalysisController(engine=BlockingEngine())
        controller.start("request", self.callbacks(completed, errors))
        with self.assertRaisesRegex(RuntimeError, "already running"):
            controller.start("other", self.callbacks([], []))
        self.assertTrue(controller.cancel())
        BlockingEngine.release.set()
        self.assertTrue(controller.wait(2.0))
        self.assertFalse(completed)
        self.assertIsInstance(errors[0], DifferentialAnalysisCancelled)

    def test_errors_cross_the_controller_boundary(self):
        class BrokenEngine:
            def solve(self, request, emit, progress, cancelled):
                raise ValueError("bad pair")

        completed, errors = [], []
        controller = DifferentialAnalysisController(engine=BrokenEngine())
        controller.start("request", self.callbacks(completed, errors))
        self.assertTrue(controller.wait(2.0))
        self.assertFalse(completed)
        self.assertEqual(str(errors[0]), "bad pair")


if __name__ == "__main__":
    unittest.main()
