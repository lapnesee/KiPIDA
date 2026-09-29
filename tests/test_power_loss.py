import os
import sys
import unittest

plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

from models import ComponentRef, PowerRail, UnifiedLoad, VoltageRegulator
from power_loss import estimate_stage, format_power_stage_report, interpolate_efficiency
from thermal_model import PowerLossEstimator, ThermalModelBuilder


def parameter(value):
    return {"value": value, "source": "test", "confidence": "high"}


class Point:
    def __init__(self, x, y):
        self.x, self.y = x, y


class Pad:
    def __init__(self, x, y):
        self.position = Point(x, y)


class Footprint:
    def __init__(self):
        self.pads = [Pad(10.0, 20.0), Pad(13.0, 24.0)]


class Board:
    footprints = []


class PowerLossTests(unittest.TestCase):
    def test_inductor_i2r_and_temperature_corrected_dcr(self):
        reg = VoltageRegulator(
            name="buck", input_rail_name="12V", input_ref_des="U1", input_pad_names=[],
            output_rail_name="5V", output_ref_des="L1", output_pad_names=[], reg_type="SWITCHING",
            loss_model={"kind": "buck", "inductors": [{
                "ref_des": "L1", "dcr_ohm": parameter(0.01), "reference_temperature_c": 25,
                "temperature_c": 125, "tempco_per_c": parameter(0.004),
            }]},
        )
        stage = estimate_stage(reg, 12.0, 5.0, 2.0)
        copper = next(loss for loss in stage.losses if loss.mechanism == "inductor-copper-i2r")
        self.assertAlmostEqual(copper.power_w, 2.0 * 2.0 * 0.01 * 1.4)

    def test_mosfet_i2r_accounts_for_parallel_paths_and_temperature(self):
        reg = VoltageRegulator(
            name="pass", input_rail_name="5V", input_ref_des="Q1", input_pad_names=[],
            output_rail_name="5V_SW", output_ref_des="Q1", output_pad_names=[], reg_type="LINEAR",
            loss_model={"kind": "mosfet", "mosfets": [{
                "ref_des": "Q1", "rds_on_ohm": parameter(0.01), "parallel_paths": 2,
                "temperature_multiplier_table": [
                    {"temperature_c": 25, "multiplier": 1.0},
                    {"temperature_c": 125, "multiplier": 1.5},
                ], "temperature_c": 125,
            }]},
        )
        stage = estimate_stage(reg, 5.0, 5.0, 2.0)
        self.assertAlmostEqual(stage.losses[0].power_w, 2.0 ** 2 * 0.01 * 1.5 / 2)

    def test_resistive_pass_stage_is_i2r_of_its_resistance(self):
        # The P02 Rev E current shunt R49, 7.5 mOhm, at the 4.4 A of the Main
        # 5 V path; it had to be declared as a MOSFET to get a loss at all.
        reg = VoltageRegulator(
            name="shunt", input_rail_name="PRE", input_ref_des="R49", input_pad_names=["1"],
            output_rail_name="5V", output_ref_des="R49", output_pad_names=["2"], reg_type="LINEAR",
            loss_model={"kind": "resistive", "resistors": [{
                "ref_des": "R49", "resistance_ohm": parameter(0.0075),
                "tempco_per_c": 0.0001, "temperature_c": 125,
            }]},
        )
        stage = estimate_stage(reg, 5.08, 5.08, 4.4)
        self.assertEqual([loss.mechanism for loss in stage.losses], ["resistive-i2r"])
        self.assertAlmostEqual(stage.total_loss_w, 4.4 * 4.4 * 0.0075 * 1.01)
        self.assertIn("R(T)=", "\n".join(format_power_stage_report(stage)))
        # COH-8: a series element carries the same current in as out (KCL);
        # its loss is a voltage drop, not extra input current. The general
        # Pin/Vin formula used to inflate this to Pin/Vin = (Pout + I^2R)/Vin
        # > Iout. On P02 Rev E, /SYS_5V_PRE_SENSE (this same R49 shunt) read
        # DC Iin=Iout=4.363 A while thermal's formula gave 4.447 A (+1.9%);
        # /VBUS_SYS 2.040 -> 2.084 A (+2.2%); /J1_VBUS_RAW 2.043 -> 2.103 A
        # (F1, +2.9%).
        self.assertEqual(stage.iin_a, stage.iout_a)

    def test_efficiency_table_interpolation_is_deterministic(self):
        table = [
            {"vin_v": 12, "vout_v": 5, "iout_a": 1, "efficiency": 0.90},
            {"vin_v": 12, "vout_v": 5, "iout_a": 3, "efficiency": 0.96},
        ]
        self.assertAlmostEqual(interpolate_efficiency(table, 12, 5, 2), 0.93)
        self.assertEqual(interpolate_efficiency(table, 12, 5, 2), interpolate_efficiency(table, 12, 5, 2))

    def test_efficiency_table_interpolates_vin_vout_and_iout(self):
        table = []
        for vin in (10.0, 14.0):
            for vout in (4.0, 6.0):
                for iout in (1.0, 3.0):
                    table.append({
                        "vin_v": vin, "vout_v": vout, "iout_a": iout,
                        "efficiency": 0.70 + vin / 100.0 + vout / 100.0 + iout / 100.0,
                    })
        self.assertAlmostEqual(interpolate_efficiency(table, 12, 5, 2), 0.89)

    def test_coupled_temperature_overrides_static_component_temperature(self):
        reg = VoltageRegulator(
            name="buck", input_rail_name="12V", input_ref_des="U1", input_pad_names=[],
            output_rail_name="5V", output_ref_des="L1", output_pad_names=[], reg_type="SWITCHING",
            loss_model={"kind": "buck", "inductors": [{
                "ref_des": "L1", "dcr_ohm": parameter(0.01),
                "reference_temperature_c": 25, "temperature_c": 25,
                "tempco_per_c": parameter(0.004),
            }]},
        )
        stage = estimate_stage(reg, 12.0, 5.0, 2.0, {"L1": 125.0})
        copper = next(loss for loss in stage.losses if loss.mechanism == "inductor-copper-i2r")
        self.assertAlmostEqual(copper.power_w, 2.0 * 2.0 * 0.01 * 1.4)
        self.assertEqual(copper.provenance["temperature_c"], 125.0)

    def test_buck_uses_separate_fet_and_quiescent_temperature_curves(self):
        reg = VoltageRegulator(
            name="buck", input_rail_name="12V", input_ref_des="U1", input_pad_names=[],
            output_rail_name="6V", output_ref_des="L1", output_pad_names=[], reg_type="SWITCHING",
            thermal_ref_des="U1", loss_model={
                "kind": "buck", "controller_ref_des": "U1",
                "high_side_rds_on_ohm": parameter(0.02),
                "low_side_rds_on_ohm": parameter(0.01),
                "high_side_temperature_multiplier_table": [
                    {"temperature_c": 25, "multiplier": 1.0},
                    {"temperature_c": 125, "multiplier": 2.0},
                ],
                "low_side_temperature_multiplier_table": [
                    {"temperature_c": 25, "multiplier": 1.0},
                    {"temperature_c": 125, "multiplier": 3.0},
                ],
                "quiescent_current_a": parameter(0.001),
                "quiescent_current_temperature_table": [
                    {"temperature_c": 25, "current_a": 0.001},
                    {"temperature_c": 125, "current_a": 0.002},
                ],
            },
        )
        stage = estimate_stage(reg, 12.0, 6.0, 2.0, {"U1": 125.0})
        hs = next(loss for loss in stage.losses if loss.mechanism == "buck-high-side-conduction")
        ls = next(loss for loss in stage.losses if loss.mechanism == "buck-low-side-conduction")
        iq = next(loss for loss in stage.losses if loss.mechanism == "buck-quiescent-input")
        self.assertAlmostEqual(hs.power_w, 4.0 * 0.5 * 0.02 * 2.0)
        self.assertAlmostEqual(ls.power_w, 4.0 * 0.5 * 0.01 * 3.0)
        self.assertAlmostEqual(iq.power_w, 12.0 * 0.002)

    def test_no_double_counting_and_power_balance(self):
        reg = VoltageRegulator(
            name="buck", input_rail_name="12V", input_ref_des="U4", input_pad_names=[],
            output_rail_name="5V", output_ref_des="L1", output_pad_names=[], reg_type="SWITCHING",
            efficiency=0.9, thermal_ref_des="U4",
            loss_model={"kind": "buck", "controller_ref_des": "U4", "fallback_efficiency": 0.9,
                        "high_side_rds_on_ohm": parameter(0.02), "low_side_rds_on_ohm": parameter(0.01),
                        "inductors": [{"ref_des": "L1", "dcr_ohm": parameter(0.03)}]},
        )
        stage = estimate_stage(reg, 12.0, 5.0, 2.0)
        self.assertAlmostEqual(stage.iin_a * stage.vin_v, stage.iout_a * stage.vout_v + stage.total_loss_w)
        self.assertAlmostEqual(stage.balance_relative_error_pct, 0.0)
        self.assertEqual(sum(loss.power_w for loss in stage.losses), stage.total_loss_w)
        self.assertEqual(len([loss for loss in stage.losses if loss.mechanism == "unmodelled-conversion-residual"]), 1)
        report = "\n".join(format_power_stage_report(stage))
        self.assertIn("buck-high-side-conduction", report)
        self.assertIn("confidence=high", report)
        self.assertIn("balance error=", report)

    def test_buck_mosfet_role_applies_duty_weighting(self):
        # TH-7: P02 Rev E's sidecar worked around this by pre-multiplying
        # RDS(on) by D (0.00066 ohm = 2.6 mOhm x 0.254 for Q10); a role lets
        # the datasheet value be entered directly instead.
        reg = VoltageRegulator(
            name="buck", input_rail_name="12V", input_ref_des="U4", input_pad_names=[],
            output_rail_name="6V", output_ref_des="L1", output_pad_names=[], reg_type="SWITCHING",
            loss_model={"kind": "buck", "mosfets": [
                {"ref_des": "Q10", "rds_on_ohm": parameter(0.0026), "role": "high"},
                {"ref_des": "Q11", "rds_on_ohm": parameter(0.0026), "role": "low"},
            ]},
        )
        stage = estimate_stage(reg, 12.0, 6.0, 2.0)  # duty = 6/12 = 0.5
        high = next(loss for loss in stage.losses if loss.ref_des == "Q10")
        low = next(loss for loss in stage.losses if loss.ref_des == "Q11")
        self.assertAlmostEqual(high.power_w, 2.0 ** 2 * 0.0026 * 0.5)
        self.assertAlmostEqual(low.power_w, 2.0 ** 2 * 0.0026 * 0.5)
        self.assertFalse(any("role" in warning for warning in stage.warnings))

    def test_buck_mosfet_without_a_role_keeps_full_current_and_warns(self):
        # A user entering the datasheet RDS(on) with no role gets the old
        # (full-inductor-RMS) behaviour, unchanged, but told about it: the
        # x1/D overstatement (x3.9 for Q10's 0.254 duty) must not be silent.
        reg = VoltageRegulator(
            name="buck", input_rail_name="12V", input_ref_des="U4", input_pad_names=[],
            output_rail_name="6V", output_ref_des="L1", output_pad_names=[], reg_type="SWITCHING",
            loss_model={"kind": "buck", "mosfets": [
                {"ref_des": "Q10", "rds_on_ohm": parameter(0.0026)},
            ]},
        )
        stage = estimate_stage(reg, 12.0, 6.0, 2.0)
        loss = next(loss for loss in stage.losses if loss.ref_des == "Q10")
        self.assertAlmostEqual(loss.power_w, 2.0 ** 2 * 0.0026)
        self.assertTrue(any("role" in warning for warning in stage.warnings))

    def test_a_switch_held_on_takes_the_full_current_without_a_warning(self):
        # P02 Rev E, 2026-09-29 18:51: U4's boost-leg high side conducts the
        # whole period in buck mode (0.151 W on its 7.6 mOhm), and the
        # warning told the user to declare "high", which would cut it by D.
        reg = VoltageRegulator(
            name="buck", input_rail_name="20V", input_ref_des="Q10", input_pad_names=[],
            output_rail_name="5V", output_ref_des="U4", output_pad_names=[], reg_type="SWITCHING",
            loss_model={"kind": "buck", "mosfets": [
                {"ref_des": "U4", "rds_on_ohm": parameter(0.0076), "role": "continuous"},
            ]},
        )
        stage = estimate_stage(reg, 20.0, 5.08, 4.42)
        loss = next(loss for loss in stage.losses if loss.ref_des == "U4")
        self.assertAlmostEqual(loss.power_w, 4.42 ** 2 * 0.0076)
        self.assertFalse(any("role" in warning for warning in stage.warnings))

    def test_discrete_mosfet_stage_ignores_role_and_warns_nothing(self):
        # kind="mosfet" is a pass device, not a switching duty cycle: role
        # must not change its behaviour or trigger the buck-only warning.
        reg = VoltageRegulator(
            name="pass", input_rail_name="5V", input_ref_des="Q1", input_pad_names=[],
            output_rail_name="5V_SW", output_ref_des="Q1", output_pad_names=[], reg_type="LINEAR",
            loss_model={"kind": "mosfet", "mosfets": [
                {"ref_des": "Q1", "rds_on_ohm": parameter(0.01)},
            ]},
        )
        stage = estimate_stage(reg, 5.0, 5.0, 2.0)
        self.assertAlmostEqual(stage.losses[0].power_w, 2.0 ** 2 * 0.01)
        self.assertEqual(stage.warnings, [])

    def test_residual_split_distributes_across_declared_components(self):
        # TH-6: P02 Rev E's residual bundles gate drive (dissipated in the
        # controller's driver), inductor core loss and dead-time diode
        # conduction, but all of it landed on one ref_des
        # (controller_ref_des) regardless. residual_split lets it name where
        # each share actually goes.
        reg = VoltageRegulator(
            name="buck", input_rail_name="12V", input_ref_des="U4", input_pad_names=[],
            output_rail_name="5V", output_ref_des="L1", output_pad_names=[], reg_type="SWITCHING",
            efficiency=0.9, thermal_ref_des="U4",
            loss_model={
                "kind": "buck", "controller_ref_des": "Q10", "fallback_efficiency": 0.9,
                "high_side_rds_on_ohm": parameter(0.02), "low_side_rds_on_ohm": parameter(0.01),
                "inductors": [{"ref_des": "L1", "dcr_ohm": parameter(0.03)}],
                "residual_split": {"Q10": 0.5, "U4": 0.3, "L1": 0.2},
            },
        )
        stage = estimate_stage(reg, 12.0, 5.0, 2.0)
        residual = {
            loss.ref_des: loss.power_w
            for loss in stage.losses if loss.mechanism == "unmodelled-conversion-residual"
        }
        self.assertEqual(set(residual), {"Q10", "U4", "L1"})
        total = sum(residual.values())
        self.assertAlmostEqual(residual["Q10"] / total, 0.5, places=6)
        self.assertAlmostEqual(residual["U4"] / total, 0.3, places=6)
        self.assertAlmostEqual(residual["L1"] / total, 0.2, places=6)
        self.assertAlmostEqual(stage.iin_a * stage.vin_v, stage.iout_a * stage.vout_v + stage.total_loss_w)

    def test_residual_target_grows_with_temperature_when_declared(self):
        # TH-6: a fixed efficiency target held this stage's total loss
        # constant no matter how hot its parts ran -- the residual just
        # shrank as the physical mechanisms grew, so "temperature
        # dependence" only moved heat between parts, never added any.
        # residual_temperature_multiplier_table lets the target scale too.
        def stage_at(temperature_c):
            reg = VoltageRegulator(
                name="buck", input_rail_name="12V", input_ref_des="U4", input_pad_names=[],
                output_rail_name="5V", output_ref_des="L1", output_pad_names=[], reg_type="SWITCHING",
                efficiency=0.9, thermal_ref_des="U4",
                loss_model={
                    "kind": "buck", "controller_ref_des": "Q10", "fallback_efficiency": 0.9,
                    "high_side_rds_on_ohm": parameter(0.02), "low_side_rds_on_ohm": parameter(0.01),
                    "inductors": [{"ref_des": "L1", "dcr_ohm": parameter(0.03)}],
                    "residual_temperature_multiplier_table": [
                        {"temperature_c": 25, "multiplier": 1.0},
                        {"temperature_c": 125, "multiplier": 1.5},
                    ],
                },
            )
            return estimate_stage(reg, 12.0, 5.0, 2.0, {"Q10": temperature_c})

        cold, hot = stage_at(25.0), stage_at(125.0)
        cold_residual = next(
            item for item in cold.losses if item.mechanism == "unmodelled-conversion-residual"
        ).power_w
        hot_residual = next(
            item for item in hot.losses if item.mechanism == "unmodelled-conversion-residual"
        ).power_w
        # Rds(on)/DCR are not temperature-corrected in this fixture (no
        # tempco given), so the entire increase must be the residual's.
        output_power = 5.0 * 2.0
        expected_target_delta = output_power * (1.0 / 0.9 - 1.0) * (1.5 - 1.0)
        self.assertAlmostEqual(hot_residual - cold_residual, expected_target_delta, places=6)
        self.assertAlmostEqual(hot.total_loss_w - cold.total_loss_w, expected_target_delta, places=6)

    def test_incomplete_datasheet_uses_explicit_fallback(self):
        reg = VoltageRegulator(
            name="legacy", input_rail_name="12V", input_ref_des="U1", input_pad_names=[],
            output_rail_name="5V", output_ref_des="L1", output_pad_names=[], reg_type="SWITCHING", efficiency=0.8,
        )
        stage = estimate_stage(reg, 12, 5, 1)
        self.assertTrue(any("fallback" in warning for warning in stage.warnings))
        self.assertEqual(stage.losses[0].mechanism, "unmodelled-conversion-residual")

    def test_efficiency_table_outside_validity_uses_explicit_fallback(self):
        reg = VoltageRegulator(
            name="buck", input_rail_name="12V", input_ref_des="U1", input_pad_names=[],
            output_rail_name="5V", output_ref_des="L1", output_pad_names=[], reg_type="SWITCHING",
            loss_model={
                "kind": "buck",
                "efficiency_table": [
                    {"vin_v": 12.0, "vout_v": 5.0, "iout_a": 1.0, "efficiency": 0.95},
                ],
                "efficiency_table_validity": {
                    "vin_min_v": 11.5, "vin_max_v": 12.5,
                    "vout_min_v": 4.8, "vout_max_v": 5.2,
                },
                "fallback_efficiency": parameter(0.80),
            },
        )
        stage = estimate_stage(reg, 9.0, 5.0, 1.0)
        self.assertAlmostEqual(stage.efficiency, 0.80)
        self.assertIn("fallback", stage.efficiency_provenance)
        self.assertTrue(any("fallback" in warning for warning in stage.warnings))

    def test_old_power_tree_remains_loadable_and_geometry_comes_from_footprint(self):
        rail_12 = PowerRail("12V", 12.0)
        rail_5 = PowerRail("5V", 5.0)
        rail_5.loads.append(UnifiedLoad(ComponentRef("J1"), 1.0, thermal_mode="EXTERNAL"))
        rail_12.child_regulators.append(VoltageRegulator(
            "old", "12V", "U1", [], "5V", "L1", [], reg_type="SWITCHING", efficiency=0.8,
        ))
        detail = PowerLossEstimator.estimate_details([rail_12, rail_5])
        self.assertAlmostEqual(next(item.power_w for item in detail.components if item.ref_des == "U1"), 1.25)
        width, depth = ThermalModelBuilder(Board())._footprint_size(Footprint(), 1.0, 1.0)
        self.assertEqual((width, depth), (3.5, 4.5))


if __name__ == "__main__":
    unittest.main()
