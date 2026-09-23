"""The PDN impedance target is computed from the rail, not typed in.

A fixed 0.05 ohm default decided pass/fail on every board without anyone
choosing it -- absurd by three orders of magnitude on a rail drawing 1.1 mA.
The target now follows from the rail's own voltage and configured load.
"""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from ac_model import derive_target_impedance, resolve_target_impedance
from models import (
    ACAnalysisSettings, ACSourceModel, ComponentRef, PowerRail, UnifiedLoad,
)


def _rail(voltage=5.0, currents=(3.8,)):
    rail = PowerRail(net_name="+5V_RAIL", nominal_voltage=voltage)
    for index, current in enumerate(currents, start=1):
        rail.loads.append(UnifiedLoad(
            component_ref=ComponentRef(ref_des=f"J{index}"), total_current=current,
        ))
    return rail


class DeriveTargetImpedanceTests(unittest.TestCase):
    def test_matches_the_formula_on_the_real_board_case(self):
        # p02_alimentation: +5V_RAIL, 5 V +/- 2 %, J6 drawing 3.8 A.
        value, provenance = derive_target_impedance(
            _rail(5.0, (3.8,)), ripple_fraction=0.02, transient_fraction=0.5,
        )
        expected = (5.0 * 0.02) / (3.8 * 0.5)
        self.assertAlmostEqual(value, expected, places=12)
        self.assertIn("5.00 V", provenance)
        self.assertIn("2.0 %", provenance)

    def test_loads_are_summed(self):
        value, _ = derive_target_impedance(_rail(3.3, (0.1, 0.2, 0.2)))
        expected = (3.3 * 0.02) / (0.5 * 0.5)
        self.assertAlmostEqual(value, expected, places=12)

    def test_a_rail_without_load_yields_no_target(self):
        value, reason = derive_target_impedance(_rail(3.3, ()))
        self.assertIsNone(value)
        self.assertIn("load current", reason)

    def test_a_rail_feeding_a_switching_converter_is_given_its_output_step(self):
        # P02 Rev E: /VBUS_SYS has no load of its own and feeds two bucks
        # (U4, U14). AC-5: the transient that matters for an AC target is the
        # converter's own chopped input current, which steps by roughly its
        # Iout at every switching edge -- not a fraction of the rail's
        # time-averaged input current (converter_input_current), which
        # understates it. Only the largest DIRECT converter's step is used
        # (here U4's downstream 4.5 A beats U14's 3.0 A), since asynchronous
        # converters do not step together.
        from models import VoltageRegulator

        def regulator(name, vin_rail, vout_rail, kind, efficiency=1.0):
            return VoltageRegulator(name, vin_rail, "U", ["1"], vout_rail, "U", ["2"],
                                    reg_type=kind, efficiency=efficiency)

        vbus = PowerRail(net_name="/VBUS_SYS", nominal_voltage=20.0)
        pre = PowerRail(net_name="/SYS_5V_PRE_SENSE", nominal_voltage=5.08)
        u4_out = PowerRail(net_name="/+5V_U4", nominal_voltage=5.08)
        rail5 = _rail(5.08, (4.0,))
        main33 = _rail(3.32, (0.5,))
        main33.net_name = "/+3V3_MAIN"
        j2 = _rail(5.2, (3.0,))
        j2.net_name = "/J2_SRC_VBUS"
        vbus.child_regulators += [regulator("U4", "/VBUS_SYS", "/SYS_5V_PRE_SENSE", "SWITCHING", 0.93),
                                  regulator("U14", "/VBUS_SYS", "/J2_SRC_VBUS", "SWITCHING", 0.92)]
        pre.child_regulators.append(regulator("R49", "/SYS_5V_PRE_SENSE", "/+5V_U4", "LINEAR"))
        u4_out.child_regulators.append(regulator("U15", "/+5V_U4", "+5V_RAIL", "LINEAR"))
        rail5.child_regulators.append(regulator("U5", "+5V_RAIL", "/+3V3_MAIN", "SWITCHING", 0.90))
        rails = [vbus, pre, u4_out, rail5, main33, j2]

        value, provenance = derive_target_impedance(vbus, all_rails=rails)
        # U4's own output step: rail5's 4.0 A load plus whatever U5 draws
        # downstream (main33's 0.5 A), all the way through U4's chain of
        # linear regulators -- 4.0 + 0.5 = 4.5 A, ahead of U14's 3.0 A.
        step_current = 4.0 + 0.5
        self.assertAlmostEqual(value, (20.0 * 0.02) / (step_current * 1.0), places=12)
        self.assertIn("chopped input current", provenance)
        self.assertIn("'U4'", provenance)
        self.assertIsNone(derive_target_impedance(vbus)[0])  # the tree is what supplies it

    def test_a_rail_feeding_only_linear_regulators_keeps_the_transient_fraction(self):
        # AC-5's step-current override only applies to SWITCHING converters;
        # a rail feeding a linear regulator still uses the old
        # transient-fraction heuristic on the time-averaged current.
        from models import VoltageRegulator

        source = PowerRail(net_name="+12V", nominal_voltage=12.0)
        sink = _rail(5.0, (2.0,))
        source.child_regulators.append(VoltageRegulator(
            "LDO1", "+12V", "U", ["1"], "+5V_RAIL", "U", ["2"], reg_type="LINEAR",
        ))
        value, provenance = derive_target_impedance(
            source, ripple_fraction=0.02, transient_fraction=0.5, all_rails=[source, sink],
        )
        self.assertAlmostEqual(value, (12.0 * 0.02) / (2.0 * 0.5), places=12)
        self.assertNotIn("chopped input current", provenance)

    def test_a_rail_without_voltage_yields_no_target(self):
        value, reason = derive_target_impedance(_rail(0.0, (1.0,)))
        self.assertIsNone(value)
        self.assertIn("nominal voltage", reason)


class ResolveTargetImpedanceTests(unittest.TestCase):
    def test_an_explicit_target_wins_over_derivation(self):
        settings = ACAnalysisSettings(rail_name="+5V_RAIL", target_impedance_ohm=0.2)
        value, provenance = resolve_target_impedance(_rail(), settings)
        self.assertAlmostEqual(value, 0.2)
        self.assertIn("as configured", provenance)

    def test_a_blank_target_is_derived(self):
        settings = ACAnalysisSettings(rail_name="+5V_RAIL", target_impedance_ohm=0.0)
        value, provenance = resolve_target_impedance(_rail(), settings)
        self.assertAlmostEqual(value, (5.0 * 0.02) / (3.8 * 0.5), places=12)
        self.assertIn("derived from", provenance)

    def test_an_underivable_target_reports_zero_and_says_why(self):
        settings = ACAnalysisSettings(rail_name="+3V3AON", target_impedance_ohm=0.0)
        value, provenance = resolve_target_impedance(_rail(3.3, ()), settings)
        self.assertEqual(value, 0.0)
        self.assertIn("not determinable", provenance)


class ConfigRoundTripTests(unittest.TestCase):
    def test_a_file_without_the_new_fields_still_loads(self):
        from config_manager import _dict_to_ac_settings

        settings = _dict_to_ac_settings({"rail_name": "+5V_RAIL"})
        self.assertEqual(settings.rail_name, "+5V_RAIL")
        self.assertEqual(settings.target_impedance_ohm, 0.0)
        self.assertAlmostEqual(settings.ripple_fraction, 0.02)
        self.assertAlmostEqual(settings.transient_fraction, 0.5)

    def test_the_new_fields_survive_a_round_trip(self):
        from config_manager import _ac_settings_to_dict, _dict_to_ac_settings

        original = ACAnalysisSettings(
            rail_name="+5V_RAIL", ripple_fraction=0.03, transient_fraction=0.4,
        )
        restored = _dict_to_ac_settings(_ac_settings_to_dict(original))
        self.assertAlmostEqual(restored.ripple_fraction, 0.03)
        self.assertAlmostEqual(restored.transient_fraction, 0.4)


class AdapterProvenanceTests(unittest.TestCase):
    def _result(self, target, meets):
        from models import ImpedanceSweepResult

        return ImpedanceSweepResult(
            frequencies_hz=[1e3, 1e6, 1e8],
            impedance_ohm=[0.01 + 0j, 0.05 + 0j, 0.36 + 0j],
            target_impedance_ohm=target,
            worst_frequency_hz=1e8, worst_impedance_ohm=0.36,
            meets_target=meets,
        )

    def test_a_derived_target_is_reported_as_estimated_with_its_origin(self):
        from analysis_adapters import adapt_ac_result
        from analysis_contract import EvidenceConfidence

        settings = ACAnalysisSettings(
            rail_name="+5V_RAIL", target_impedance_ohm=0.0526,
            target_impedance_provenance="0.0526 ohm derived from 5.00 V x 2.0 % ripple",
            # AC-2: a non-default source keeps this test about target
            # provenance, not about the separate default-source downgrade.
            source=ACSourceModel(resistance_ohm=0.1, inductance_h=0.5e-6),
        )
        result = adapt_ac_result(self._result(0.0526, False), settings=settings)
        finding = next(f for f in result.findings if f.rule_id == "AC-001")
        self.assertEqual(finding.confidence, EvidenceConfidence.ESTIMATED)
        self.assertIn("derived from", finding.description)

    def test_an_undeterminable_target_is_info_not_a_failure(self):
        from analysis_adapters import adapt_ac_result
        from analysis_contract import AnalysisStatus, FindingSeverity

        settings = ACAnalysisSettings(
            rail_name="+3V3AON", target_impedance_ohm=0.0,
            target_impedance_provenance="not determinable: the rail has no configured load current",
        )
        result = adapt_ac_result(self._result(0.0, False), settings=settings)
        finding = next(f for f in result.findings if f.rule_id == "AC-002")
        self.assertEqual(finding.severity, FindingSeverity.INFO)
        self.assertNotEqual(result.status, AnalysisStatus.FAIL)
        self.assertFalse(any(f.rule_id == "AC-001" for f in result.findings))


if __name__ == "__main__":
    unittest.main()
