"""The batch report waits for the DC and THERMAL plots (review 2026-10-02)."""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


class BatchReportWaitTests(unittest.TestCase):
    def test_report_waits_for_plots_still_drawing(self):
        # P02 Rev E's 17:26 batch had CFD after THERMAL, 30 s in which the
        # thermal plots reached the history; a batch ending on THERMAL had none.
        try:
            from ui import main_dialog
        except Exception:
            self.skipTest("the main dialog cannot be imported here")
        dialog_class = main_dialog.KiPIDA_MainDialog
        written, logged = [], []
        dialog = SimpleNamespace(
            _closing=False, _dc_plot_thread=None, _thermal_plot_thread=object(),
            PLOT_WAIT_SECONDS=dialog_class.PLOT_WAIT_SECONDS, log=logged.append,
            _write_campaign_report=lambda campaign, open_browser: written.append(campaign),
        )
        dialog._plots_rendering = lambda: dialog_class._plots_rendering(dialog)
        dialog._write_batch_report = (
            lambda campaign, waited=0: dialog_class._write_batch_report(dialog, campaign, waited))
        with mock.patch.object(main_dialog.wx, "CallLater", create=True) as call_later:
            dialog._write_batch_report("campaign")
            self.assertEqual(written, [])
            self.assertEqual(logged, ["Batch report waits for the THERMAL plots."])
            dialog._thermal_plot_thread = None
            _delay, callback, *args = call_later.call_args.args
            callback(*args)
        self.assertEqual(written, ["campaign"])


if __name__ == "__main__":
    unittest.main()
