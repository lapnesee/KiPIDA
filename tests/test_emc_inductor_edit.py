"""A switching inductor's operating point is edited on the EMI/EMC page (SW-004)."""

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
class InductorModelEditTests(unittest.TestCase):
    def test_the_current_sw004_names_is_corrected_on_the_emc_page(self):
        # P02 Rev E, 2026-10-01: SW-004 named L1's model at 4.36 A against
        # U4's 4.726 A in the power tree; the table showing it was read-only,
        # and the sidecar was edited by hand.
        from models import EMCInductorModel
        from ui.emc_analysis_panel import EMCInductorDialog, edited_inductor_model

        l1 = EMCInductorModel(
            ref_des="L1", mpn="XAL1010-472MED", inductance_h=4.7e-6, vin_v=20.0, vout_v=5.08,
            switching_frequency_hz=400e3, output_current_a=4.36, ripple_current_pp_a=2.016,
        )
        values = {"inductance_uh": "4.7", "vin_v": "20", "vout_v": "5.08", "frequency_khz": "400",
                  "current_a": "4,726", "ripple_a": "2.016", "attenuation_db": ""}
        edited = edited_inductor_model(l1, values)
        self.assertEqual((edited.output_current_a, edited.shielding_attenuation_db), (4.726, None))
        self.assertEqual((edited.mpn, edited.switching_frequency_hz), ("XAL1010-472MED", 400e3))
        with self.assertRaises(ValueError):
            edited_inductor_model(l1, dict(values, frequency_khz="0"))

        app = wx.App(False)
        dialog = EMCInductorDialog(None, l1)
        try:
            dialog.controls["current_a"].SetValue("4.726")
            self.assertEqual(dialog.get_model().output_current_a, 4.726)
        finally:
            dialog.Destroy()
            del app


if __name__ == "__main__":
    unittest.main()
