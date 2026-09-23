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
    EnclosureCFDSettings, LossContribution, PowerStageResult, ThermalHotspot,
    ThermalResult,
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
            # AC-2: a non-default source (here the USB-C cable preset) keeps
            # this test's confidence at ESTIMATED rather than the HEURISTIC
            # an unverified default source now gets.
            source=ACSourceModel(ref_des="U1", resistance_ohm=0.1, inductance_h=0.5e-6),
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

    def test_ac_adapter_warns_when_pass_is_only_a_lower_bound(self):
        # AC-1: P02 Rev E /VBUS_SYS sweep -- every swept point passes, but the
        # worst (0.2947 ohm at 100 MHz) sits on the closed sweep edge and was
        # still rising; extrapolated crossing (~133 MHz) falls inside the
        # quasi-static limit (~140 MHz), so the PASS must not stand alone.
        sweep = SimpleNamespace(
            meets_target=True, worst_impedance_ohm=0.2947, worst_frequency_hz=100e6,
            target_impedance_ohm=0.39226, compute_backend="CPU", compute_device="CPU",
            compute_solve_seconds=0.01, compute_relative_residual=1e-9,
            frequencies_hz=[1e3, 50e6, 100e6], impedance_ohm=[0.001 + 0j, 0.2 + 0j, 0.2947 + 0j],
            worst_at_sweep_edge=True, extrapolated_crossing_hz=133.1e6,
            quasi_static_limit_hz=140e6,
        )
        settings = ACAnalysisSettings(rail_name="/VBUS_SYS", ground_net_name="GND")
        result = adapt_ac_result(sweep, settings=settings)
        self.assertEqual(result.status, AnalysisStatus.WARN)
        self.assertEqual(result.findings[0].rule_id, "AC-003")

    def test_ac_adapter_states_the_via_mutual_inductance_gap(self):
        # AC-8: mesh.Mesher._calculate_vertical_l has no mutual term against
        # a nearby return via, overestimating a tight decoupling via pair's
        # loop inductance by about 2.6x (measured on a 1 mm-apart pair). A
        # full fix needs via-pair spacing this function cannot see, so it is
        # published as a limitation rather than silently corrected.
        sweep = SimpleNamespace(
            meets_target=True, worst_impedance_ohm=0.1, worst_frequency_hz=1e6,
            target_impedance_ohm=0.2, compute_backend="CPU", compute_device="CPU",
            compute_solve_seconds=0.01, compute_relative_residual=1e-9,
            frequencies_hz=[1e3, 1e6], impedance_ohm=[0.01 + 0j, 0.1 + 0j],
        )
        settings = ACAnalysisSettings(rail_name="+3V3", ground_net_name="GND")
        result = adapt_ac_result(sweep, settings=settings)
        self.assertTrue(any("AC-8" in item and "mutual" in item for item in result.limitations))

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

    def test_a_failing_pair_states_its_impedance_and_the_geometry_that_fixes_it(self):
        # P02 Rev E's /USB_DEVICE_D: 76.7 ohm for 90, and the recommender's
        # 0.155 mm was nowhere in the finding.
        pair_result = SimpleNamespace(
            pair=SimpleNamespace(name="/USB_DEVICE_D", target_impedance_ohm=90.0), status="FAIL",
            weighted_impedance_ohm=76.74, error_pct=-14.7, estimated_skew_ps=0.1,
            length_symmetry_status="PASS",
            warnings=["Targeted 3-D quasi-static refinement: 2-D impedance 14.6% from the target."],
            recommendations=[SimpleNamespace(
                action="SET_PRIMARY_ROUTE", recommended_width_mm=0.155, recommended_gap_mm=0.25,
                predicted_impedance_ohm=90.6, layer_name="B.Signal",
            )],
        )
        finding = adapt_differential_result([pair_result], None, 10.0).findings[0]
        self.assertTrue(finding.description.startswith("Zdiff 76.7 ohm against a 90 ohm target (-14.7%)."))
        self.assertIn("0.155 mm wide with a 0.250 mm gap on B.Signal", finding.recommendation)

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
        # TH-1 / COH-11: was "FAIL", copying the domain status onto the
        # metric with no threshold of its own -- the same "Hotspot ... PASS"
        # shape the audit flagged, just on the FAIL side. A bare temperature
        # has no universal pass/fail line; only a junction's margin does,
        # which is why U1's CRITICAL finding above still fires.
        self.assertEqual(result.metrics[0].status, "INFO")
        # Was "PASS". The energy balance only shows the linear solve converged
        # -- it would read just as well with a surface coefficient wrong by a
        # factor of ten -- so labelling it PASS invited it to be read as
        # physical accuracy. NUMERICS_OK says what it actually measures.
        self.assertEqual(result.metrics[2].status, "NUMERICS_OK")
        # TH-1: U1 exceeding its limit is named among the hottest junctions.
        self.assertEqual(result.metrics[3].key, "hottest_junction_margin")
        # Surface exchange and the hottest-junction summary are now stated
        # alongside geometry and power (no mesh here, so no HOTSPOT_LOCATION).
        self.assertEqual(len(result.provenance), 4)

    def test_hotspot_names_the_component_it_sits_under(self):
        # P02 Rev E, 2026-09-22 21:56: board hotspot 114.71 C at
        # (274.75, 86.56) mm sits under Q10 (274.53, 85.65); the hottest
        # *junction* is a different part, U14 (125.0 C, 25 K margin) -- the
        # report named neither before this fix.
        thermal = ThermalResult(
            hotspot=ThermalHotspot(node_id=7, x_mm=274.75, y_mm=86.56, z_mm=0.1, temperature_c=114.71),
            component_results=[SimpleNamespace(
                ref_des="U14", junction_temperature_c=125.0, margin_c=25.0,
                theta_jb_c_per_w=20.7, thermal_source="datasheet",
            )],
            total_input_power_w=4.9186, energy_balance_error_pct=0.1,
        )
        mesh = SimpleNamespace(
            component_nodes={"Q10": [7], "U14": [9]},
            node_coords={7: (274.75, 86.56, 0.1), 9: (100.0, 50.0, 0.1)},
        )
        result = adapt_thermal_result(thermal, mesh=mesh)
        location = next(item for item in result.provenance if item.source == "HOTSPOT_LOCATION")
        self.assertIn("under Q10", location.detail)
        junctions = next(item for item in result.provenance if item.source == "JUNCTION_TEMPERATURES")
        self.assertIn("U14 125.0 C (margin 25.0 K, datasheet)", junctions.detail)
        margin_metric = next(item for item in result.metrics if item.key == "hottest_junction_margin")
        self.assertEqual(margin_metric.value, 25.0)

    def test_efficiency_closed_stage_discloses_its_residual(self):
        # P02 Rev E: U4's stage is closed at a 93% efficiency fallback,
        # leaving a 1.365 W residual (28% of the stage's 4.9184 W) placed on
        # Q10 -- previously invisible in the report (TH-1, power_loss.py:349).
        stage = PowerStageResult(
            name="U4 buck", input_ref_des="U4", output_ref_des="L1",
            vin_v=12.0, vout_v=5.0, iin_a=1.0, iout_a=2.0,
            efficiency=0.93,
            efficiency_provenance="configured efficiency fallback (incomplete physical data)",
            losses=[
                LossContribution(
                    "Q10", "buck-mosfet-conduction", 3.5534,
                    {"source": "measured", "confidence": "high"},
                ),
                LossContribution(
                    "Q10", "unmodelled-conversion-residual", 1.365,
                    {"source": "efficiency fallback", "confidence": "low"},
                ),
            ],
        )
        thermal = ThermalResult(
            hotspot=ThermalHotspot(1, 0.0, 0.0, 0.1, 100.0),
            total_input_power_w=4.9186, energy_balance_error_pct=0.1,
        )
        result = adapt_thermal_result(thermal, power_stage_reports=[stage])
        limitation = next(
            item for item in result.limitations if "unmodelled-conversion residual" in item
        )
        self.assertIn("Q10", limitation)
        self.assertIn("28%", limitation)
        self.assertIn("the configured efficiency fallback", limitation)
        self.assertIn("confidence=low", limitation)

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

    def test_cfd_adapter_publishes_the_mean_air_rise_when_settings_are_given(self):
        # docs/audit-2026-09-23.md CFD-4: P02 Rev E's defensible headline
        # number, 4.755 W / (5 W/m2K * 0.0595 m2) = 16.0 K, was never
        # published; the report showed only the mesh-limited solid/air
        # maxima.
        mesh = SimpleNamespace(cell_count=1000)
        cfd = SimpleNamespace(
            converged=True, iterations=100, mass_balance_error_pct=0.0,
            energy_balance_error_pct=0.0, maximum_velocity_m_s=0.1,
            maximum_air_temperature_c=73.2, maximum_solid_temperature_c=90.0,
            total_heat_w=4.755, compute_backend="CPU", compute_device="CPU",
        )
        settings = EnclosureCFDSettings(ambient_c=45.0)
        settings.geometry.width_mm = 165.0
        settings.geometry.depth_mm = 100.0
        settings.geometry.height_mm = 50.0
        settings.geometry.wall_heat_transfer_w_m2k = 5.0

        result = adapt_cfd_result(mesh, cfd, settings=settings)

        metrics = {metric.key: metric for metric in result.metrics}
        self.assertAlmostEqual(metrics["mean_air_rise"].value, 16.0, places=1)
        self.assertAlmostEqual(metrics["internal_ambient"].value, 61.0, places=1)

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
