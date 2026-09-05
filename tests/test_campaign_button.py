"""The consolidated-report button must reach a written HTML file.

Scope: the wiring only -- that a session's published results can be collected
and turned into a report on disk. The aggregation itself is covered by
tests/test_campaign.py and the rendering by tests/test_report_html.py.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

# NOTE ON SKIPPING IN A FULL-SUITE RUN
# tests/test_plotter.py installs a stub module under sys.modules['wx'] at
# import time when the key is still absent. unittest discovery imports every
# test module before running any test, so by the time these tests execute the
# stub has usually won the race and they skip -- even on a runner with
# wxPython installed. They do run under `python -m unittest
# tests.test_campaign_button`.
#
# Importing the real wx at module scope here would win that race, but it also
# hands test_plotter the real wx.Bitmap/wx.Image instead of its lambdas, and
# its plot calls then fail with "wx.App object must be created first" -- its
# assertions still pass, but against the error path rather than the one they
# were written for. Weakening an existing test to make these run is the wrong
# trade, so the skip stands.


class CampaignButtonWiringTests(unittest.TestCase):
    def setUp(self):
        # Another test in the suite stubs sys.modules['wx'], so a bare
        # ImportError guard is not enough: wx imports but lacks the widgets
        # the UI needs. tests/test_structure.py uses the same hasattr check.
        try:
            import wx
        except ImportError:
            self.skipTest("wxPython not available")
        if not all(hasattr(wx, name) for name in ("Panel", "PopupWindow", "App")):
            self.skipTest("A complete wxPython runtime is not available")

    def test_button_is_registered_and_handled(self):
        from ui.dialog_action_bar import DialogActionBar
        from ui.main_dialog import KiPIDA_MainDialog

        keys = [key for key, _label in DialogActionBar.BUTTON_SPECS]
        self.assertIn("campaign", keys)
        # A button with no handler raises a KeyError at bar construction, so the
        # handler must exist on the dialog for the wiring to hold together.
        self.assertTrue(hasattr(KiPIDA_MainDialog, "on_build_campaign_report"))

    def test_session_results_collects_published_results(self):
        import wx
        from analysis_contract import AnalysisResult, AnalysisStatus
        from ui.results_workspace import ResultsWorkspace

        app = wx.App(False)
        try:
            frame = wx.Frame(None)
            with tempfile.TemporaryDirectory() as tmp:
                workspace = ResultsWorkspace(frame, history_directory=Path(tmp))
                dc = AnalysisResult("DC", "DC Power", status=AnalysisStatus.PASS)
                emc = AnalysisResult("EMC", "EMI/EMC", status=AnalysisStatus.WARN)
                workspace.publish("DC", "dc report", result=dc)
                workspace.publish("EMC", "emc report", result=emc)

                collected = workspace.session_results()
                self.assertEqual(
                    [r.analysis_type for r in collected], ["DC", "EMC"],
                )
            frame.Destroy()
        finally:
            app.Destroy()

    def test_collected_results_produce_a_report_on_disk(self):
        # The end the button exists for: results in, HTML file out.
        from analysis_contract import (
            AnalysisFinding, AnalysisResult, AnalysisStatus, EvidenceConfidence,
            FindingSeverity,
        )
        from campaign import CampaignResult
        from report.html_report import write_campaign_html

        dc = AnalysisResult("DC", "DC Power", status=AnalysisStatus.WARN)
        dc.findings.append(AnalysisFinding(
            rule_id="DC-003", category="VOLTAGE_DROP",
            severity=FindingSeverity.HIGH, title="Drop on +5V_RAIL",
            description="synthetic", confidence=EvidenceConfidence.DETERMINISTIC,
            nets=["+5V_RAIL"],
        ))
        campaign = CampaignResult(
            project_name="p02_alimentation", results=[dc],
        ).recompute()

        with tempfile.TemporaryDirectory() as tmp:
            path = write_campaign_html(campaign, Path(tmp) / "campaign.html")
            self.assertTrue(path.is_file())
            html = path.read_text(encoding="utf-8")
            self.assertIn("p02_alimentation", html)
            self.assertIn("+5V_RAIL", html)


if __name__ == "__main__":
    unittest.main()
