import threading
import unittest

from application.background_controller import BackgroundAnalysisController


class Cancelled(RuntimeError):
    pass


class Callbacks:
    def __init__(self):
        self.progress = []
        self.logs = []
        self.completed = []
        self.errors = []
        self.on_progress = lambda *args: self.progress.append(args)
        self.on_log = self.logs.append
        self.on_complete = self.completed.append
        self.on_error = self.errors.append


class BackgroundControllerTests(unittest.TestCase):
    def controller(self, engine, dispatch=lambda callback, *args: callback(*args)):
        return BackgroundAnalysisController(
            engine,
            thread_name="KiPIDA-Test-Worker",
            busy_message="test already running",
            cancelled_error_factory=lambda: Cancelled("test cancelled"),
            dispatch=dispatch,
        )

    def test_success_uses_named_worker_and_dispatches_all_callbacks(self):
        class Engine:
            def solve(self, request, emit, progress, cancelled):
                emit(threading.current_thread().name)
                progress(1, 1, "done")
                return request.upper()

        dispatched = []

        def dispatch(callback, *args):
            dispatched.append((callback, args))
            callback(*args)

        callbacks = Callbacks()
        controller = self.controller(Engine(), dispatch)
        controller.start("result", callbacks)
        self.assertTrue(controller.wait(2.0))
        self.assertEqual(callbacks.completed, ["RESULT"])
        self.assertEqual(callbacks.logs, ["KiPIDA-Test-Worker"])
        self.assertEqual(callbacks.progress, [(1, 1, "done")])
        self.assertEqual(len(dispatched), 3)

    def test_idle_cancel_and_wait_are_noops(self):
        controller = self.controller(object())
        self.assertFalse(controller.cancel())
        self.assertTrue(controller.wait(0.0))

    def test_completion_after_cancel_becomes_typed_error(self):
        release = threading.Event()

        class Engine:
            def solve(self, request, emit, progress, cancelled):
                release.wait(2.0)
                return "late result"

        callbacks = Callbacks()
        controller = self.controller(Engine())
        controller.start(None, callbacks)
        self.assertTrue(controller.cancel())
        release.set()
        self.assertTrue(controller.wait(2.0))
        self.assertFalse(callbacks.completed)
        self.assertIsInstance(callbacks.errors[0], Cancelled)

    def test_engine_error_is_dispatched_without_rewrapping(self):
        expected = ValueError("engine failed")

        class Engine:
            def solve(self, request, emit, progress, cancelled):
                raise expected

        callbacks = Callbacks()
        controller = self.controller(Engine())
        controller.start(None, callbacks)
        self.assertTrue(controller.wait(2.0))
        self.assertIs(callbacks.errors[0], expected)


if __name__ == "__main__":
    unittest.main()
