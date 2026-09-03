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
    ACAnalysisSettings, ComponentRef, PowerRail, UnifiedLoad,
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
