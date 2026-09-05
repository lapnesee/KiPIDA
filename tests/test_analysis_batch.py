"""The batch runner sequences analyses without overlapping them.

Each analysis runs on its own background thread, so starting the next one
before the previous finishes would have two solvers competing for the same
GPU, the same geometry snapshot and the same result workspace. The batch waits
for every controller to go idle instead.

Exercised against the unbound methods with a stub, rather than a constructed
wx.Dialog: the logic touches only the queue, the controllers' is_running and
log, and building the real dialog needs a live KiCad board.
"""

import os
import sys
import unittest
import unittest.mock

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


class _Controller:
    def __init__(self, running=False):
        self.is_running = running


class _Stub:
    """Enough of the dialog for the batch methods to run."""

    def __init__(self, queue):
        from ui.main_dialog import KiPIDA_MainDialog

        self.BATCH_ANALYSES = KiPIDA_MainDialog.BATCH_ANALYSES
        self.BATCH_POLL_MS = KiPIDA_MainDialog.BATCH_POLL_MS
        self._closing = False
        self._batch_queue = list(queue)
        self._batch_total = len(queue)
        self._batch_done = 0
        self.messages = []
        self.started = []
        self.status = []
        self.controllers = [_Controller() for _ in range(6)]

    def log(self, message):
        self.messages.append(message)

    def _set_interaction_status(self, message):
        self.status.append(message)

    def _batch_controllers(self):
        return tuple(self.controllers)

    def _batch_busy(self):
        from ui.main_dialog import KiPIDA_MainDialog

        return KiPIDA_MainDialog._batch_busy(self)

    def _batch_wait(self):
        from ui.main_dialog import KiPIDA_MainDialog

        return KiPIDA_MainDialog._batch_wait(self)

    def _batch_step(self):
        from ui.main_dialog import KiPIDA_MainDialog

        return KiPIDA_MainDialog._batch_step(self)

    # The handlers the step method dispatches to.
    def on_run(self, _event):
        self.started.append("dc")

    def on_run_ac(self, _event):
        self.started.append("ac")

    def on_run_differential(self, _event):
        self.started.append("differential")

    def on_run_coupled_thermal(self, _event):
        self.started.append("thermal")

    def on_run_emc(self, _event):
        self.started.append("emc")

    def on_run_cfd(self, _event):
        self.started.append("cfd")


class BatchSequencing(unittest.TestCase):
    def setUp(self):
        try:
            from ui.main_dialog import KiPIDA_MainDialog
        except Exception as exc:  # pragma: no cover - depends on import order
            self.skipTest(f"ui.main_dialog unavailable: {exc}")
        self.step = KiPIDA_MainDialog._batch_step
        self.wait = KiPIDA_MainDialog._batch_wait
        self.busy = KiPIDA_MainDialog._batch_busy

    def test_it_starts_the_first_analysis_and_stops_there(self):
        import wx

        stub = _Stub(["dc", "ac"])
        with unittest.mock.patch.object(wx, "CallLater"):
            self.step(stub)

        # One started, the other still queued: nothing runs concurrently.
        self.assertEqual(stub.started, ["dc"])
        self.assertEqual(stub._batch_queue, ["ac"])

    def test_a_busy_controller_defers_the_next_analysis(self):
        import wx

        stub = _Stub(["ac"])
        stub.controllers[0].is_running = True
        with unittest.mock.patch.object(wx, "CallLater") as later:
            self.wait(stub)

        self.assertEqual(stub.started, [])
        self.assertEqual(stub._batch_queue, ["ac"])
        later.assert_called_once()

    def test_an_idle_bench_advances_the_queue(self):
        import wx

        stub = _Stub(["ac"])
        with unittest.mock.patch.object(wx, "CallLater"):
            self.wait(stub)

        self.assertEqual(stub.started, ["ac"])
        self.assertEqual(stub._batch_queue, [])

    def test_a_handler_that_raises_does_not_stop_the_batch(self):
        # One analysis failing must cost its own result, not the five after it.
        import wx

        stub = _Stub(["dc", "ac"])
        stub.on_run = lambda _event: (_ for _ in ()).throw(ValueError("no board"))
        with unittest.mock.patch.object(wx, "CallLater") as later:
            self.step(stub)

        self.assertTrue(any("failed to start" in m for m in stub.messages))
        later.assert_called_once()
        self.assertEqual(stub._batch_queue, ["ac"])

    def test_closing_the_dialog_abandons_the_queue(self):
        import wx

        stub = _Stub(["dc", "ac"])
        stub._closing = True
        with unittest.mock.patch.object(wx, "CallLater"):
            self.step(stub)

        self.assertEqual(stub.started, [])
        self.assertEqual(stub._batch_queue, [])

    def test_dc_runs_before_the_analyses_that_consume_it(self):
        # Thermal and CFD read DC copper losses; EMC reads AC and differential
        # results. The declared order is the dependency order, not a guess.
        from ui.main_dialog import KiPIDA_MainDialog

        keys = [key for key, _label in KiPIDA_MainDialog.BATCH_ANALYSES]
        self.assertLess(keys.index("dc"), keys.index("thermal"))
        self.assertLess(keys.index("dc"), keys.index("cfd"))
        self.assertLess(keys.index("ac"), keys.index("emc"))
        self.assertLess(keys.index("differential"), keys.index("emc"))


if __name__ == "__main__":
    unittest.main()
