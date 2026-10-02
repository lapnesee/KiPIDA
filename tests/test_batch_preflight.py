"""The batch dialog says what each analysis will run on (review 2026-10-02)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


class BatchPreflightTests(unittest.TestCase):
    def test_each_analysis_says_what_it_runs_on_and_how_long_it_took(self):
        # P02 Rev E: 17 rails, 3 AC profiles, a CFD box on the default wall
        # coefficient; the AC took 336 s of the 575 s batch.
        try:
            from ui.main_dialog import KiPIDA_MainDialog
        except Exception:
            self.skipTest("the main dialog cannot be imported here")
        from analysis_contract import AnalysisResult
        from models import EMCSignalSource, EnclosureCFDSettings
        from result_history import ProjectResultsHistory

        with tempfile.TemporaryDirectory() as directory:
            history = ProjectResultsHistory(directory)
            history.save("AC", "AC Impedance", "report",
                         result=AnalysisResult("AC", "AC Impedance", elapsed_seconds=336.42).finish())
            dialog = SimpleNamespace(
                power_tree=SimpleNamespace(rails=[object()] * 17),
                ac_panel=SimpleNamespace(get_profiles=lambda: {"/VBUS_SYS": 1, "/+5V_RAIL": 2, "/+5V_U4": 3}),
                emc_panel=SimpleNamespace(settings=SimpleNamespace(sources=[
                    EMCSignalSource("U4 SW1", "/SYS_5V_SW1"), EMCSignalSource("U4 SW2", "/SYS_5V_SW2", enabled=False),
                ])),
                cfd_panel=SimpleNamespace(get_settings=EnclosureCFDSettings),
                results_workspace=SimpleNamespace(_history=history),
            )
            notes = KiPIDA_MainDialog._batch_preflight(dialog)
        self.assertEqual(notes["DC"], "17 rail(s)")
        self.assertEqual(notes["AC"], "3 AC profile(s) of 17 rail(s); about 6 min last time")
        self.assertEqual(notes["EMC"], "1 source(s)")
        self.assertEqual(notes["CFD"], "enclosure wall coefficient at its default")
        self.assertNotIn("THERMAL", notes)


if __name__ == "__main__":
    unittest.main()
