"""Labels outside the wx translation hooks are translated (review 2026-10-02)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
