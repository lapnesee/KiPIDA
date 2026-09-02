import threading
import unittest

from application.cfd_controller import (
    CFDAnalysisCancelled, CFDAnalysisController, CFDControllerCallbacks,
    CFDRunOutcome,
)


class FakeEngine:
    def solve(self, request, emit, progress, cancelled):
        emit("cfd worker started")
        progress(1, 1, "energy")
        if cancelled():
            raise CFDAnalysisCancelled("Enclosure CFD analysis cancelled.")
        return CFDRunOutcome("mesh", "result", (("3D", b"png"),), {})


class BlockingEngine:
    release = threading.Event()

    def solve(self, request, emit, progress, cancelled):
        self.release.wait(2.0)
        if cancelled():
            raise CFDAnalysisCancelled("Enclosure CFD analysis cancelled.")
        return FakeEngine().solve(request, emit, progress, cancelled)


class CFDControllerTests(unittest.TestCase):
    @staticmethod
    def callbacks(completed, errors, progress=None, logs=None):
        return CFDControllerCallbacks(
            on_progress=lambda *args: (progress if progress is not None else []).append(args),
            on_complete=completed.append,
            on_error=errors.append,
            on_log=lambda message: (logs if logs is not None else []).append(message),
        )

    def test_background_analysis_returns_mesh_result_and_plots(self):
        completed, errors, progress, logs = [], [], [], []
        controller = CFDAnalysisController(engine=FakeEngine())
        controller.start("request", self.callbacks(completed, errors, progress, logs))
        self.assertTrue(controller.wait(2.0))
        self.assertEqual(completed[0].mesh, "mesh")
        self.assertEqual(completed[0].plots, (("3D", b"png"),))
        self.assertEqual(progress, [(1, 1, "energy")])
        self.assertEqual(logs, ["cfd worker started"])
        self.assertFalse(errors)

    def test_concurrent_start_and_cancellation(self):
        BlockingEngine.release.clear()
        completed, errors = [], []
        controller = CFDAnalysisController(engine=BlockingEngine())
        controller.start("request", self.callbacks(completed, errors))
        with self.assertRaisesRegex(RuntimeError, "already running"):
            controller.start("other", self.callbacks([], []))
        self.assertTrue(controller.cancel())
        BlockingEngine.release.set()
        self.assertTrue(controller.wait(2.0))
        self.assertFalse(completed)
        self.assertIsInstance(errors[0], CFDAnalysisCancelled)

    def test_errors_cross_the_controller_boundary(self):
        class BrokenEngine:
            def solve(self, request, emit, progress, cancelled):
                raise ValueError("bad enclosure")

        completed, errors = [], []
        controller = CFDAnalysisController(engine=BrokenEngine())
        controller.start("request", self.callbacks(completed, errors))
        self.assertTrue(controller.wait(2.0))
        self.assertFalse(completed)
        self.assertEqual(str(errors[0]), "bad enclosure")


if __name__ == "__main__":
    unittest.main()
