"""A converter's efficiency curve is entered in the regulator dialog (TH-008)."""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

try:
    import wx  # noqa: F401
    _WX = True
except ImportError:  # pragma: no cover
    _WX = False


@unittest.skipUnless(_WX, "wxPython is not installed")
class EfficiencyCurveTests(unittest.TestCase):
    def test_a_curve_entered_in_the_dialog_closes_the_estimate(self):
        # P02 Rev E, 2026-10-01: U4's 1.81 W rested on a 93 % estimate and
        # TH-008 asked for its curve, while the dialog took one efficiency,
        # which power_loss always reads as a fallback.
        from models import VoltageRegulator
        from power_loss import estimate_stage
        from ui.regulator_dialog import (
            apply_efficiency_curve, format_efficiency_points, parse_efficiency_points,
        )

        table = parse_efficiency_points("1   95,1\n3;94.2\n\n5 0.92")
        self.assertEqual(table, [
            {"iout_a": 1.0, "efficiency": 0.951},
            {"iout_a": 3.0, "efficiency": 0.942},
            {"iout_a": 5.0, "efficiency": 0.92},
        ])
        self.assertEqual(format_efficiency_points(table), "1 95.1\n3 94.2\n5 92")
        self.assertEqual(parse_efficiency_points("12 5 2 95.24")[0]["vin_v"], 12.0)
        with self.assertRaises(ValueError):
            parse_efficiency_points("1 95\n2 ninety")

        regulator = VoltageRegulator(
            "U4 buck-boost 5 V machine", "/VBUS_SYS", "U4", [], "/SYS_5V_PRE_SENSE", "L1", [],
            reg_type="SWITCHING", efficiency=0.93, loss_model={"kind": "buck"},
        )
        apply_efficiency_curve(regulator, {
            "efficiency_table": table, "efficiency_table_source": "datasheet",
            "efficiency_table_reference": "SLVSFC5 Figure 6-1",
        })
        self.assertEqual(regulator.loss_model["kind"], "buck")
        stage = estimate_stage(regulator, 20.0, 5.08, 4.726)
        self.assertTrue(stage.efficiency_provenance.startswith("interpolated efficiency table"))
        self.assertIn("SLVSFC5 Figure 6-1", stage.efficiency_provenance)

        apply_efficiency_curve(regulator, {"efficiency_table": []})
        self.assertNotIn("efficiency_table", regulator.loss_model)

    def test_the_dialog_shows_and_returns_a_saved_curve(self):
        # Its parameter grid was declared 2 x 2 with three rows already, which
        # wx asserts on once a fourth field is added.
        from types import SimpleNamespace
        from models import VoltageRegulator
        from ui.regulator_dialog import RegulatorDialog

        app = wx.App(False)
        pad = SimpleNamespace(number="1")
        discoverer = SimpleNamespace(get_components_on_net=lambda _net: {"U14": [pad], "L3": [pad]})
        regulator = VoltageRegulator(
            "U14 buck J2 source", "/VBUS_SYS", "U14", ["1"], "/J2_SRC_VBUS", "L3", ["1"],
            reg_type="SWITCHING", efficiency=0.92, loss_model={
                "efficiency_table": [{"vin_v": 12.0, "vout_v": 5.0, "iout_a": 3.0, "efficiency": 0.9419}],
                "efficiency_table_source": {"source": "datasheet", "reference": "SLUSEP7 Figure 7-1"},
            },
        )
        dialog = RegulatorDialog(None, "Edit Regulator", ["/VBUS_SYS", "/J2_SRC_VBUS"], discoverer,
                                 input_rail="/VBUS_SYS", output_rail="/J2_SRC_VBUS")
        try:
            dialog.prepopulate(regulator)
            value = dialog.GetValue()
            self.assertEqual(value["efficiency_table"], regulator.loss_model["efficiency_table"])
            self.assertEqual(value["efficiency_table_reference"], "SLUSEP7 Figure 7-1")
        finally:
            dialog.Destroy()
            del app


if __name__ == "__main__":
    unittest.main()
