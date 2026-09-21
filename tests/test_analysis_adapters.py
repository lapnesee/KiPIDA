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
        # HIGH is exact geometry judged against a hard-coded threshold: an
        # estimate resting on good data, no longer labelled a fact.
        self.assertEqual(result.findings[0].confidence, EvidenceConfidence.ESTIMATED)
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
            # A CRITICAL needs a stated thetaJB; a default one yields HIGH
            # (tests/test_confidence_calibration.py).
            component_results=[SimpleNamespace(
                ref_des="U1", junction_temperature_c=140.0, margin_c=-15.0,
                theta_jb_c_per_w=8.5, thermal_source="datasheet",
            )],
            total_input_power_w=2.0, energy_balance_error_pct=0.5,
        )
        result = adapt_thermal_result(thermal)
        self.assertEqual(result.status, AnalysisStatus.FAIL)
        self.assertEqual(result.findings[0].severity, FindingSeverity.CRITICAL)
        self.assertEqual(result.metrics[0].status, "FAIL")
        # Was "PASS". The energy balance only shows the linear solve converged
        # -- it would read just as well with a surface coefficient wrong by a
        # factor of ten -- so labelling it PASS invited it to be read as
        # physical accuracy. NUMERICS_OK says what it actually measures.
        self.assertEqual(result.metrics[2].status, "NUMERICS_OK")
        # Surface exchange is now stated alongside geometry and power.
        self.assertEqual(len(result.provenance), 3)

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
        density = SimpleNamespace(
            maximum_planar_a_per_mm2=57.0, percentile_99_5_a_per_mm2=50.0,
            maximum_track_a_per_mm2=57.0, maximum_zone_a_per_mm2=20.0,
            maximum_via_current_a=0.2, maximum_via_a_per_mm2=25.0,
        )
        result = adapt_dc_result({"+3V3": {
            "stats": (3.0, 3.3, 0.3), "detailed_result": detail,
            "compute_metadata": compute, "current_density": density,
        }}, 5.0)
        self.assertEqual(result.status, AnalysisStatus.WARN)
        self.assertEqual({item.rule_id for item in result.findings}, {"DC-001", "DC-003"})
        self.assertEqual(result.metrics[0].status, "WARN")
        self.assertEqual(len(result.metrics), 7)
        self.assertEqual(result.metrics[1].unit, "A/mm²")
        self.assertEqual(len(result.provenance), 3)
        self.assertTrue(any("IPC ampacity" in item for item in result.limitations))
        payload = result.to_json()
        self.assertIn("current_density_hotspots", payload)
        self.assertNotIn("planar_samples", payload)

    def test_dc_003_judges_the_worst_load_not_the_net_spread(self):
        # 9% between the net's highest and lowest nodes -- a pour corner no
        # load draws through -- while the worst load sees 100 mV of 3.3 V.
        within = adapt_dc_result({"+3V3": {
            "stats": (3.0, 3.3, 0.3), "load_drop_v": 0.1, "source_voltage_v": 3.3,
        }}, 5.0)
        self.assertNotIn("DC-003", {item.rule_id for item in within.findings})
        metrics = {metric.key: metric for metric in within.metrics}
        self.assertAlmostEqual(metrics["rail_1_drop"].value, 0.1 / 3.3 * 100.0)
        self.assertEqual(metrics["rail_1_drop"].status, "PASS")
        self.assertAlmostEqual(metrics["rail_1_spread"].value, 0.3 / 3.3 * 100.0)

        beyond = adapt_dc_result({"+3V3": {
            "stats": (3.0, 3.3, 0.3), "load_drop_v": 0.25, "source_voltage_v": 3.3,
        }}, 5.0)
        finding = next(item for item in beyond.findings if item.rule_id == "DC-003")
        self.assertIn("250.0 mV", finding.description)

    def test_every_dc_finding_names_its_rail(self):
        # The advisor finds a voltage-drop finding's rail by its nets. Without
        # them no finding from this adapter was ever given a sized fix.
        detail = SimpleNamespace(valid=False, excluded_load_node_count=1)
        result = adapt_dc_result({
            "+5V": {"stats": (4.0, 5.0, 1.0), "detailed_result": detail},
            "+3V3": {"stats": (3.0, 3.3, 0.3)},
        }, 1.0)
        self.assertEqual(
            sorted((item.rule_id, tuple(item.nets)) for item in result.findings),
            [("DC-001", ("+5V",)), ("DC-003", ("+3V3",)), ("DC-003", ("+5V",))],
        )

    def test_a_drop_that_moves_at_half_the_grid_step_raises_dc_005(self):
        # P02 Rev E +5V_U4 at 4.4 A: 40.8 mV at 0.1 mm, 49.7 mV at 0.05 mm.
        rail = {
            "stats": (5.03, 5.08, 0.05), "load_drop_v": 0.0408, "source_voltage_v": 5.08,
            "grid_sensitivity": {
                "grid_mm": 0.1, "check_grid_mm": 0.05, "drop_v": 0.0408,
                "check_drop_v": 0.0497, "delta_pct": 21.8,
            },
        }
        result = adapt_dc_result({"+5V_U4": rail}, 5.0)
        finding = next(item for item in result.findings if item.rule_id == "DC-005")
        self.assertEqual(finding.severity, FindingSeverity.MEDIUM)
        self.assertIn("49.7 mV at 0.05 mm", finding.description)
        self.assertEqual(finding.nets, ["+5V_U4"])
        # At a 0.9% budget (45.7 mV) the finer solve turns the verdict.
        flipped = adapt_dc_result({"+5V_U4": rail}, 0.9)
        finding = next(item for item in flipped.findings if item.rule_id == "DC-005")
        self.assertEqual(finding.severity, FindingSeverity.HIGH)
        self.assertIn("exceeds the 0.9% budget", finding.description)

    def test_a_hybrid_island_is_not_blamed_on_the_grid_step(self):
        detail = SimpleNamespace(valid=False, excluded_load_node_count=1)
        result = adapt_dc_result({"+5V": {
            "stats": (4.9, 5.0, 0.1), "detailed_result": detail, "engine": "hybrid",
            "grid_size_mm": 0.1, "load_drop_v": 0.05, "source_voltage_v": 5.0,
        }}, 5.0)
        island = next(item for item in result.findings if item.rule_id == "DC-001")
        self.assertNotIn("1.4 grid steps", island.description)
        self.assertIn("saved board file", island.description)
        self.assertNotIn("finer DC grid", island.recommendation)

    def test_a_drop_with_loads_left_out_is_not_a_pass(self):
        # P02 Rev E's /+3V3AO had every load on copper its source does not
        # reach, and its drop metric read "0.0 % PASS".
        detail = SimpleNamespace(valid=False, excluded_load_node_count=150,
                                 excluded_load_references=("U13", "U7"))
        result = adapt_dc_result({"/+3V3AO": {
            "stats": (3.32, 3.32, 0.0), "detailed_result": detail, "engine": "hybrid",
            "load_drop_v": 0.0, "source_voltage_v": 3.32,
        }}, 5.0)
        metrics = {metric.key: metric for metric in result.metrics}
        self.assertEqual(metrics["rail_1_drop"].status, "INCOMPLETE")
        island = next(item for item in result.findings if item.rule_id == "DC-001")
        self.assertIn("(U13, U7)", island.description)


if __name__ == "__main__":
    unittest.main()
