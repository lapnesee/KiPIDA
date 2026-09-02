import unittest
from types import SimpleNamespace

from analysis_adapters import (
    adapt_ac_result, adapt_cfd_result, adapt_dc_result, adapt_differential_result,
    adapt_emc_result, adapt_thermal_result,
)
from analysis_contract import AnalysisStatus, EvidenceConfidence, FindingSeverity
from models import (
    ACAnalysisSettings, ACMeasurementPort, ACSourceModel, CapacitorModel,
    EMCAnalysisResult, EMCAnalysisSettings, EMCEvidence, EMCFinding,
    ThermalHotspot, ThermalResult,
)


class AnalysisAdapterTests(unittest.TestCase):
    def test_emc_adapter_preserves_actionable_evidence(self):
        domain = EMCAnalysisResult(
            findings=[EMCFinding(
                rule_id="RET-001", category="RETURN_PATH", severity="HIGH",
                title="Missing return via", description="No nearby return via.",
                recommendation="Add a stitching via.", confidence="HIGH",
                nets=["USB_D+"], components=["U1"],
                evidence=[EMCEvidence("PCB_GEOMETRY", "Nearest via is 8 mm away", 10.0, 12.0, 0)],
            )],
            risk_score=72, total_checks=44, elapsed_seconds=1.25,
            limitations=["Cable common-mode current is not measured."],
        )
        result = adapt_emc_result(EMCAnalysisSettings(), domain)
        self.assertEqual(result.status, AnalysisStatus.WARN)
        self.assertEqual(result.findings[0].severity, FindingSeverity.HIGH)
        self.assertEqual(result.findings[0].confidence, EvidenceConfidence.DETERMINISTIC)
        self.assertEqual(result.findings[0].evidence[0].x_mm, 10.0)
        self.assertEqual(result.summary["risk_score"], 72)
        self.assertEqual(result.metrics[0].unit, "/100")
        self.assertEqual(result.metrics[0].status, "WARN")
        self.assertTrue(result.provenance)

    def test_emc_adapter_passes_when_only_informational_findings_exist(self):
        domain = EMCAnalysisResult(findings=[EMCFinding(
            "IN-001", "INDUCTOR", "INFO", "Shield status", "Documented", "",
        )])
        self.assertEqual(
            adapt_emc_result(EMCAnalysisSettings(), domain).status,
            AnalysisStatus.PASS,
        )

    def test_ac_adapter_uses_target_as_verdict(self):
        sweep = SimpleNamespace(
            meets_target=False, worst_impedance_ohm=0.12, worst_frequency_hz=1e6,
            target_impedance_ohm=0.05, compute_backend="CPU", compute_device="CPU",
            compute_solve_seconds=0.01, compute_relative_residual=1e-9,
            frequencies_hz=[1e3, 1e6], impedance_ohm=[0.01 + 0j, 0.12 + 0.01j],
        )
        settings = ACAnalysisSettings(
            rail_name="+3V3", ground_net_name="GND",
            source=ACSourceModel(ref_des="U1", resistance_ohm=0.01, inductance_h=1e-9),
            measurement_port=ACMeasurementPort(ref_des="U2"),
            capacitors=[CapacitorModel("C1", capacitance_f=1e-6)],
        )
        result = adapt_ac_result(sweep, settings=settings)
        self.assertEqual(result.status, AnalysisStatus.WARN)
        self.assertEqual(result.findings[0].rule_id, "AC-001")
        self.assertEqual(result.findings[0].confidence, EvidenceConfidence.ESTIMATED)
        self.assertEqual(result.findings[0].components, ["C1"])
        self.assertEqual(result.metrics[0].status, "WARN")
        self.assertTrue(result.provenance)
        self.assertEqual(result.configuration_snapshot["rail_name"], "+3V3")
        self.assertEqual(result.configuration_snapshot["source"]["reference"], "U1")
        self.assertEqual(result.summary["target_exceeding_point_count"], 1)
        self.assertEqual(len(result.summary["sweep"]), 2)
        self.assertAlmostEqual(result.summary["sweep"][1]["real_ohm"], 0.12)

    def test_differential_adapter_aggregates_pair_status(self):
        pair = SimpleNamespace(name="USB", target_impedance_ohm=90.0)
        pair_result = SimpleNamespace(
            pair=pair, status="FAIL", weighted_impedance_ohm=110.0,
            estimated_skew_ps=20.0, length_symmetry_status="PASS",
            error_pct=22.2, warnings=["Reference gap"],
        )
        stackup = SimpleNamespace(warnings=[], source="FAB", trustworthy=True)
        result = adapt_differential_result([pair_result], stackup, 10.0)
        self.assertEqual(result.status, AnalysisStatus.FAIL)
        self.assertEqual(result.metrics[0].unit, "ohm")

    def test_thermal_adapter_reports_over_limit_component(self):
        thermal = ThermalResult(
            hotspot=ThermalHotspot(1, 2.0, 3.0, 0.1, 130.0),
            component_results=[SimpleNamespace(
                ref_des="U1", junction_temperature_c=140.0, margin_c=-15.0,
            )],
            total_input_power_w=2.0, energy_balance_error_pct=0.5,
        )
        result = adapt_thermal_result(thermal)
        self.assertEqual(result.status, AnalysisStatus.FAIL)
        self.assertEqual(result.findings[0].severity, FindingSeverity.CRITICAL)
        self.assertEqual(result.metrics[0].status, "FAIL")
        self.assertEqual(result.metrics[2].status, "PASS")
        self.assertEqual(len(result.provenance), 2)

    def test_cfd_adapter_reports_convergence_and_balance(self):
        mesh = SimpleNamespace(cell_count=1000)
        cfd = SimpleNamespace(
            converged=False, iterations=100, mass_balance_error_pct=8.0,
            energy_balance_error_pct=1.0, maximum_velocity_m_s=1.0,
            maximum_air_temperature_c=40.0, maximum_solid_temperature_c=60.0,
            compute_backend="CPU", compute_device="CPU",
        )
        result = adapt_cfd_result(mesh, cfd)
        self.assertEqual(result.status, AnalysisStatus.WARN)
        self.assertEqual(len(result.findings), 2)
        self.assertTrue(all(metric.status == "WARN" for metric in result.metrics))
        self.assertEqual(len(result.provenance), 2)

    def test_dc_adapter_combines_connectivity_and_drop_rules(self):
        detail = SimpleNamespace(valid=False, excluded_load_node_count=2)
        compute = SimpleNamespace(converged=True)
        result = adapt_dc_result({"+3V3": {
            "stats": (3.0, 3.3, 0.3), "detailed_result": detail,
            "compute_metadata": compute,
        }}, 5.0)
        self.assertEqual(result.status, AnalysisStatus.WARN)
        self.assertEqual({item.rule_id for item in result.findings}, {"DC-001", "DC-003"})
        self.assertEqual(result.metrics[0].status, "WARN")
        self.assertEqual(len(result.provenance), 2)


if __name__ == "__main__":
    unittest.main()
