"""Labels outside the wx translation hooks are translated (review 2026-10-02)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


class UiTranslationTests(unittest.TestCase):
    def setUp(self):
        try:
            import wx
        except ImportError:
            self.skipTest("wxPython not available")
        if not all(hasattr(wx, name) for name in ("Panel", "PopupWindow", "App")):
            self.skipTest("A complete wxPython runtime is not available")

    def test_a_translated_filter_keeps_its_canonical_name(self):
        # The navigation, the batch dialog and the results filters stayed in
        # English under the French interface; the findings filter matched the
        # text it showed, so translating that text would have broken it.
        import wx
        import i18n
        from ui.results_workspace import ResultsWorkspace

        i18n.configure("fr")
        app = wx.App(False)
        try:
            frame = wx.Frame(None)
            with tempfile.TemporaryDirectory() as directory:
                workspace = ResultsWorkspace(frame, history_directory=Path(directory))
                page = workspace.page_for("DC")
                page.finding_filter.SetSelection(1)
                self.assertEqual(page.finding_filter.GetString(1), "Critiques / élevées")
                self.assertEqual(page._selected_severity_filter(), "Critical / High")
                self.assertEqual(workspace.history_filter.GetString(0), "Dernier par analyse")
            frame.Destroy()
        finally:
            i18n.configure("en")
            app.Destroy()


class LogTests(unittest.TestCase):
    def test_the_log_shows_french_and_keeps_english_on_disk(self):
        # The log stayed English under the French interface, was written
        # nowhere, and scrolled to the end on every line of a batch.
        try:
            import wx  # noqa: F401
            from ui.main_dialog import KiPIDA_MainDialog
        except Exception:
            self.skipTest("the main dialog cannot be imported here")
        import i18n

        class Control:
            def __init__(self):
                self.text, self.shown = "", 0
                self.insertion = 0

            def GetInsertionPoint(self):
                return self.insertion

            def GetLastPosition(self):
                return len(self.text)

            def AppendText(self, value):
                self.text += value

            def ShowPosition(self, _position):
                self.shown += 1

        with tempfile.TemporaryDirectory() as directory:
            dialog = SimpleNamespace(log_ctrl=Control(), _closing=False,
                                     _results_history_directory=lambda: Path(directory),
                                     LOG_FILE_LIMIT_BYTES=5_000_000)
            dialog._write_log_file = lambda line: KiPIDA_MainDialog._write_log_file(dialog, line)
            i18n.configure("fr")
            try:
                KiPIDA_MainDialog.log(dialog, "DC: --- Starting System Simulation (17 rails) ---")
                dialog.log_ctrl.insertion = 0  # the reader scrolled up
                KiPIDA_MainDialog.log(dialog, "Saved Thermique 3D result history entry.")
            finally:
                i18n.configure("en")
            self.assertIn("DC: --- Démarrage de la simulation système (17 rails) ---",
                          dialog.log_ctrl.text)
            self.assertEqual(dialog.log_ctrl.shown, 1)
            on_disk = Path(directory, "ki-pida.log").read_text(encoding="utf-8")
        self.assertIn("DC: --- Starting System Simulation (17 rails) ---", on_disk)
        self.assertIn("Saved Thermique 3D result history entry.", on_disk)


if __name__ == "__main__":
    unittest.main()
