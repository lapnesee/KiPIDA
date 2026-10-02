"""An input left at a dialog's default is not reported as entered (review 2026-10-02)."""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

try:
    import wx
    _WX = True
except ImportError:  # pragma: no cover
    _WX = False


@unittest.skipUnless(_WX, "wxPython is not installed")
class DefaultInputTests(unittest.TestCase):
    def setUp(self):
        self.app = wx.App(False)

    def tearDown(self):
        del self.app

    def test_a_manual_source_left_at_the_defaults_is_low_confidence(self):
        # Every manual source read HIGH, even one left at 25 MHz, 2 ns, 3.3 V
        # and 0.1 A, and the AC target quoted its edge "(high confidence)".
        from ui.emc_analysis_panel import EMCSourceDialog

        dialog = EMCSourceDialog(None)
        try:
            dialog.net.SetValue("/MCU_CLK")
            dialog.frequency.SetValue("24")
            source = dialog.get_source()
            self.assertEqual(source.parameter_confidence, "LOW")
            self.assertEqual(source.parameter_notes,
                             "Defaults kept: rise time 2 ns, swing 3.3 V, current 0.1 A.")
            for control, value in ((dialog.rise, "1.5"), (dialog.voltage, "5"),
                                   (dialog.current, "0.02")):
                control.SetValue(value)
            self.assertEqual(dialog.get_source().parameter_confidence, "HIGH")
        finally:
            dialog.Destroy()

    def test_a_load_has_no_current_until_one_is_entered(self):
        # A load took 1.0 A unless changed, and "0,5" became 0 A silently.
        from ui.component_selector import ComponentSelectorDialog, parse_current

        dialog = ComponentSelectorDialog(None, "Select Component", "/+5V_RAIL", {"J6": []})
        try:
            dialog.set_mode("LOAD")
            self.assertEqual(dialog.txt_val.GetValue(), "")
            dialog.lst_comps.SetSelection(0)
            dialog.txt_val.SetValue("0,5")
            self.assertEqual(dialog.GetSelection()[1], 0.5)
        finally:
            dialog.Destroy()
        for text in ("", "abc", "0"):
            with self.assertRaises(ValueError):
                parse_current(text)


if __name__ == "__main__":
    unittest.main()
