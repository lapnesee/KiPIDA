import threading
from types import SimpleNamespace
import unittest

from application.dc_controller import (
    DCAnalysisCancelled,
    DCAnalysisController,
    DCControllerCallbacks,
    DCPointSnapshot,
    capture_dc_board,
)


class FakeEngine:
    def solve(self, request, emit, progress, cancelled):
        emit("worker started")
        progress(1, 1, "done")
        if cancelled():
            raise DCAnalysisCancelled("DC analysis cancelled.")
        return {"VCC": "result"}


class BlockingEngine:
    release = threading.Event()

    def solve(self, request, emit, progress, cancelled):
        self.release.wait(2.0)
        if cancelled():
            raise DCAnalysisCancelled("DC analysis cancelled.")
        return {}


class DCControllerTests(unittest.TestCase):
    @staticmethod
    def callbacks(completed, errors, progress=None, logs=None):
        return DCControllerCallbacks(
            on_progress=lambda *args: (progress if progress is not None else []).append(args),
            on_complete=completed.append,
            on_error=errors.append,
            on_log=lambda message: (logs if logs is not None else []).append(message),
        )

    def test_analysis_runs_off_caller_thread_and_reports_progress(self):
        completed, errors, progress, logs, callback_threads = [], [], [], [], []
        caller = threading.get_ident()
        callbacks = DCControllerCallbacks(
            on_progress=lambda *args: (progress.append(args), callback_threads.append(threading.get_ident())),
            on_complete=lambda result: (completed.append(result), callback_threads.append(threading.get_ident())),
            on_error=errors.append,
            on_log=logs.append,
        )
        controller = DCAnalysisController(engine=FakeEngine())
        controller.start("request", callbacks)
        self.assertTrue(controller.wait(2.0))
        self.assertEqual(completed, [{"VCC": "result"}])
        self.assertEqual(progress, [(1, 1, "done")])
        self.assertEqual(logs, ["worker started"])
        self.assertFalse(errors)
        self.assertTrue(all(thread_id != caller for thread_id in callback_threads))

    def test_concurrent_start_is_rejected_and_cancel_is_typed(self):
        BlockingEngine.release.clear()
        controller = DCAnalysisController(engine=BlockingEngine())
        completed, errors = [], []
        controller.start("request", self.callbacks(completed, errors))
        with self.assertRaisesRegex(RuntimeError, "already running"):
            controller.start("other", self.callbacks([], []))
        self.assertTrue(controller.cancel())
        BlockingEngine.release.set()
        self.assertTrue(controller.wait(2.0))
        self.assertFalse(completed)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], DCAnalysisCancelled)

    def test_capture_detaches_only_plain_board_data(self):
        point = SimpleNamespace(x=12_000_000, y=5_000_000)
        net = SimpleNamespace(name="VCC")
        pad = SimpleNamespace(
            number="1", name="1", position=point, net=net, pad_type=1,
            type="THROUGH_HOLE", drill_size=SimpleNamespace(x=300_000, y=300_000),
            layers=[0, 31],
        )
        footprint = SimpleNamespace(reference="J1", pads=[pad])
        via = SimpleNamespace(
            start=point, net=net, width=600_000,
            drill_size=SimpleNamespace(x=300_000, y=300_000), layers=[0, 31],
        )
        snapshot = capture_dc_board(SimpleNamespace(footprints=[footprint], vias=[via]))
        self.assertEqual(snapshot.footprints[0].reference, "J1")
        self.assertEqual(snapshot.footprints[0].pads[0].net.name, "VCC")
        self.assertEqual(snapshot.vias[0].layers, (0, 31))
        self.assertEqual(snapshot.vias[0].position, DCPointSnapshot(12_000_000, 5_000_000))
        self.assertEqual(snapshot.vias[0].drill_size, DCPointSnapshot(300_000, 300_000))

    def test_engine_errors_cross_the_controller_boundary(self):
        class BrokenEngine:
            def solve(self, request, emit, progress, cancelled):
                raise ValueError("bad snapshot")

        completed, errors = [], []
        controller = DCAnalysisController(engine=BrokenEngine())
        controller.start("request", self.callbacks(completed, errors))
        self.assertTrue(controller.wait(2.0))
        self.assertFalse(completed)
        self.assertEqual(str(errors[0]), "bad snapshot")

    def test_capture_recovers_via_drill_from_padstack(self):
        point = SimpleNamespace(x=1_000_000, y=2_000_000)
        via = SimpleNamespace(
            start=point, net=SimpleNamespace(name="VCC"), width=600_000,
            layers=[0, 31],
            padstack=SimpleNamespace(drill=SimpleNamespace(diameter=320_000)),
        )
        snapshot = capture_dc_board(SimpleNamespace(footprints=[], vias=[via]))
        self.assertEqual(snapshot.vias[0].drill_size, DCPointSnapshot(320_000, 320_000))


if __name__ == "__main__":
    unittest.main()
