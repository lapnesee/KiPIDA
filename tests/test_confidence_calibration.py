"""Confidence says what a finding rests on, not how serious it sounds.

DETERMINISTIC is for a fact about the run -- a solve that did not converge, a
mesh coarser than a stated limit. The audit of 2026-09-13 found it on three
findings that are model verdicts: an EMC rule graded HIGH became DETERMINISTIC
through the legacy mapping although its verdict is a hard-coded threshold;
AC-001 became DETERMINISTIC whenever the target was explicit, on a PDN model
that omits capacitor mounting inductance; DC-001 stated a copper island as a
fact although the raster mesh opens narrow diagonal tracks by itself. And
TH-001 was CRITICAL on a junction temperature computed from a default thetaJB.
"""

import unittest
from types import SimpleNamespace

from analysis_adapters import adapt_ac_result, adapt_dc_result, adapt_thermal_result
from analysis_contract import EvidenceConfidence, FindingSeverity, normalize_evidence_confidence
from models import (
    ACAnalysisSettings, ACMeasurementPort, ACSourceModel, CapacitorModel,
    ThermalHotspot, ThermalResult,
)


class ConfidenceCalibrationTests(unittest.TestCase):
    def test_a_high_legacy_label_is_not_a_fact(self):
        self.assertEqual(normalize_evidence_confidence("HIGH"), EvidenceConfidence.ESTIMATED)

    def test_ac_001_is_an_estimate_even_with_an_explicit_target(self):
        sweep = SimpleNamespace(
            meets_target=False, worst_impedance_ohm=0.12, worst_frequency_hz=50e6,
            target_impedance_ohm=0.05, frequencies_hz=[1e6, 50e6],
            impedance_ohm=[0.01 + 0j, 0.12 + 0j],
        )
        settings = ACAnalysisSettings(
            rail_name="+3V3", ground_net_name="GND",
            # AC-2: a non-default source keeps this test about the target's
            # own confidence, not about the separate default-source downgrade.
            source=ACSourceModel(ref_des="U1", resistance_ohm=0.1, inductance_h=0.5e-6),
            measurement_port=ACMeasurementPort(ref_des="U2"),
            capacitors=[CapacitorModel("C1", capacitance_f=1e-6, model_source="datasheet")],
        )
        settings.target_impedance_provenance = "set explicitly"
        result = adapt_ac_result(sweep, settings=settings)
        self.assertEqual(result.findings[0].confidence, EvidenceConfidence.ESTIMATED)
        self.assertTrue(any("mounting inductance" in item for item in result.limitations))

    def test_ac_001_drops_to_heuristic_on_the_untouched_default_source(self):
        # AC-2: scratchpad/audit/ac/source_lumped.py measured the default 10
        # mOhm + 1 nH source hiding the source/bulk anti-resonance of P02 Rev
        # E's 7-capacitor bank entirely (max|Z| 12 mOhm below 10 MHz), while a
        # USB-C cable moves it to 92% of a typical target. A verdict that
        # still rests on the untouched default is worth less than an
        # estimate, so it must not read ESTIMATED.
        sweep = SimpleNamespace(
            meets_target=False, worst_impedance_ohm=0.12, worst_frequency_hz=50e6,
            target_impedance_ohm=0.05, frequencies_hz=[1e6, 50e6],
            impedance_ohm=[0.01 + 0j, 0.12 + 0j],
        )
        settings = ACAnalysisSettings(
            rail_name="+3V3", ground_net_name="GND",
            source=ACSourceModel(ref_des="U1"),  # untouched default (0.01 ohm, 1 nH)
            measurement_port=ACMeasurementPort(ref_des="U2"),
        )
        result = adapt_ac_result(sweep, settings=settings)
        finding = next(f for f in result.findings if f.rule_id == "AC-001")
        self.assertEqual(finding.confidence, EvidenceConfidence.HEURISTIC)
        self.assertTrue(any("no board evidence" in item for item in result.limitations))

    def test_a_pass_on_the_default_source_names_it_as_data_to_complete(self):
        # P02 Rev E, 2026-10-01 21:42: /+5V_RAIL (source U15) passed on the
        # untouched default and only the appendix said so.
        sweep = SimpleNamespace(
            meets_target=True, worst_impedance_ohm=0.0149, worst_frequency_hz=1e3,
            target_impedance_ohm=0.043, frequencies_hz=[1e3, 1e6], impedance_ohm=[0.0149 + 0j, 0.01 + 0j],
        )
        settings = ACAnalysisSettings(
            rail_name="/+5V_RAIL", ground_net_name="GND", source=ACSourceModel(ref_des="U15"),
            measurement_port=ACMeasurementPort(ref_des="J6"),
        )
        result = adapt_ac_result(sweep, settings=settings)
        self.assertEqual(result.status.value, "PASS")
        (finding,) = [f for f in result.findings if f.rule_id == "AC-006"]
        self.assertEqual((finding.severity.value, finding.components, finding.nets),
                         ("INFO", ["U15"], ["/+5V_RAIL"]))
        # It names the page and the fields, not "the rail's AC profile".
        self.assertTrue(finding.recommendation.startswith(
            "On the AC Impedance page, pick the Source model"))

    def test_dc_001_names_the_mesh_as_a_possible_cause(self):
        result = adapt_dc_result({"+3V3": {
            "stats": (3.2, 3.3, 0.1),
            "detailed_result": SimpleNamespace(valid=False, excluded_load_node_count=3),
            "compute_metadata": SimpleNamespace(converged=True),
            "grid_size_mm": 0.25, "requested_grid_size_mm": 0.1, "adaptive_grid": True,
        }}, 5.0)
        island = next(item for item in result.findings if item.rule_id == "DC-001")
        self.assertEqual(island.confidence, EvidenceConfidence.ESTIMATED)
        self.assertIn("0.25 mm", island.description)
        self.assertIn("grid", island.recommendation.lower())

    def test_th_001_is_critical_only_on_a_stated_theta(self):
        def run(source):
            thermal = ThermalResult(
                hotspot=ThermalHotspot(1, 2.0, 3.0, 0.1, 130.0),
                component_results=[SimpleNamespace(
                    ref_des="U1", junction_temperature_c=140.0, margin_c=-15.0,
                    theta_jb_c_per_w=20.0, thermal_source=source,
                )],
            )
            return adapt_thermal_result(thermal).findings[0]

        assumed = run("estimate")
        self.assertEqual(assumed.severity, FindingSeverity.HIGH)
        self.assertEqual(assumed.confidence, EvidenceConfidence.ESTIMATED)
        self.assertIn("20", assumed.description)
        stated = run("datasheet")
        self.assertEqual(stated.severity, FindingSeverity.CRITICAL)
        self.assertEqual(stated.confidence, EvidenceConfidence.DATASHEET_BACKED)


if __name__ == "__main__":
    unittest.main()
