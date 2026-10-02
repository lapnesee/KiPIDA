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

    def test_a_pass_the_finer_mesh_overturns_is_a_high_grid_finding(self):
        # P02 Rev E, 2026-09-29 21:59: /VBUS_SYS passed at 0.5 mm, 0.2294 ohm
        # against the 0.254 ohm allowed at 100 MHz; 0.2597 ohm at 0.25 mm.
        sweep = SimpleNamespace(
            meets_target=True, worst_impedance_ohm=0.2294, worst_frequency_hz=1e8,
            target_impedance_ohm=0.0888889, target_bandwidth_hz=35e6, compute_backend="CPU",
            frequencies_hz=[1e6, 1e8], impedance_ohm=[0.005 + 0j, 0.2294 + 0j],
            grid_check={
                "delta_pct": 13.2, "grid_mm": 0.5, "check_grid_mm": 0.25,
                "impedance_ohm": 0.2294, "check_impedance_ohm": 0.2597, "frequency_hz": 1e8,
            },
        )
        result = adapt_ac_result(sweep, settings=ACAnalysisSettings(rail_name="/VBUS_SYS"))

        finding = next(item for item in result.findings if item.rule_id == "AC-005")
        self.assertEqual(finding.severity, FindingSeverity.HIGH)
        self.assertEqual(finding.confidence, EvidenceConfidence.ESTIMATED)
        self.assertIn("0.254 ohm allowed there lies between", finding.description)
        # 0.2597 + (0.2597 - 0.2294) / (2 - 1)
        self.assertIn("it reads 0.29 ohm", finding.description)
        extrapolated = next(m for m in result.metrics if m.key == "grid_extrapolated_impedance")
        self.assertAlmostEqual(extrapolated.value, 0.29)

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

    def test_ac_batch_scores_every_profile(self):
        # AC-10: P02 Rev E has 3 AC profiles; a campaign swept only the rail
        # on screen and scored AC on it. The batch result carries every swept
        # rail, the worst verdict, and names a profile it could not sweep.
        from analysis_adapters import adapt_ac_batch

        def sweep(worst):
            return SimpleNamespace(
                meets_target=worst <= 0.2, worst_impedance_ohm=worst, worst_frequency_hz=1e6,
                target_impedance_ohm=0.2, compute_backend="CPU", compute_device="CPU",
                compute_solve_seconds=0.01, compute_relative_residual=1e-9,
                frequencies_hz=[1e3, 1e6], impedance_ohm=[0.01 + 0j, worst + 0j],
            )

        result = adapt_ac_batch(
            [(sweep(0.05), None, ACAnalysisSettings(rail_name="/+5V_RAIL")),
             (sweep(0.27), None, ACAnalysisSettings(rail_name="/VBUS_SYS"))],
            skipped=[("/+5V_U4", "no copper geometry")],
        )

        result.validate()
        self.assertEqual(result.status, AnalysisStatus.WARN)
        failing = next(f for f in result.findings if f.rule_id == "AC-001")
        self.assertIn("/VBUS_SYS", failing.nets)
        coverage = next(f for f in result.findings if f.rule_id == "AC-004")
        self.assertIn("2 of 3", coverage.description)
        self.assertEqual(set(result.summary["rails"]), {"/+5V_RAIL", "/VBUS_SYS"})

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

    def test_each_failing_pair_is_its_own_finding_and_the_tolerance_is_published(self):
        # P02 Rev E: five SI-DIFF-001 findings without nets shared one
        # identity, and the 10 % verdict tolerance appeared nowhere (COH-7,
        # COH-2).
        def failing(name, positive, negative):
            return SimpleNamespace(
                pair=SimpleNamespace(name=name, positive_net=positive, negative_net=negative,
                                     target_impedance_ohm=90.0),
                status="FAIL", weighted_impedance_ohm=76.7, error_pct=-14.7,
                estimated_skew_ps=0.1, length_symmetry_status="PASS", warnings=[],
            )
        stackup = SimpleNamespace(warnings=[], source="FAB", trustworthy=True)
        result = adapt_differential_result(
            [failing("A", "/A+", "/A-"), failing("B", "/B+", "/B-")], stackup, 10.0,
        )
        self.assertEqual([finding.nets for finding in result.findings], [["/A+", "/A-"], ["/B+", "/B-"]])
        tolerance = next(metric for metric in result.metrics if metric.key == "target_tolerance")
        self.assertEqual(tolerance.value, 10.0)

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

    def test_enclosure_rise_is_published_when_applied(self):
        # COH-4: P02 Rev E, sealed 165x100x50 mm box -> ~16.5 K rise on a
        # 45 C ambient (CFD-4's ~16 K measurement).
        thermal = ThermalResult(
            hotspot=ThermalHotspot(1, 0.0, 0.0, 0.1, 100.0),
            total_input_power_w=4.9186, energy_balance_error_pct=0.1,
        )
        result = adapt_thermal_result(
            thermal, ambient_c=45.0, enclosure_air_rise_k=16.53, enclosure_ambient_applied=True,
        )
        metric = next(item for item in result.metrics if item.key == "internal_air_c")
        self.assertAlmostEqual(metric.value, 61.53, places=1)
        evidence = next(item for item in result.provenance if item.source == "ENCLOSURE_AMBIENT")
        self.assertIn("61.5 C", evidence.detail)
        self.assertIn("16.5 K", evidence.detail)

    def test_enclosure_rise_is_a_limitation_when_disabled(self):
        thermal = ThermalResult(
            hotspot=ThermalHotspot(1, 0.0, 0.0, 0.1, 100.0),
            total_input_power_w=4.9186, energy_balance_error_pct=0.1,
        )
        result = adapt_thermal_result(
            thermal, ambient_c=45.0, enclosure_air_rise_k=16.53, enclosure_ambient_applied=False,
        )
        self.assertFalse(any(item.key == "internal_air_c" for item in result.metrics))
        limitation = next(item for item in result.limitations if "ambient-rise correction" in item)
        self.assertIn("16.5 K", limitation)

    def test_out_of_range_correlation_is_a_named_limitation(self):
        # TH-8: P02 Rev E's Ra ~ 8.0e3 sits below the 1e4 laminar-correlation
        # lower bound; the coefficient used to be published with no hint it
        # was extrapolated rather than looked up inside a validated range.
        thermal = ThermalResult(
            hotspot=ThermalHotspot(1, 0.0, 0.0, 0.1, 100.0),
            total_input_power_w=1.0, energy_balance_error_pct=0.1,
            surface_correlation_in_range={"top": False, "bottom": True, "edge": False},
        )
        result = adapt_thermal_result(thermal)
        limitation = next(item for item in result.limitations if "Rayleigh/Reynolds range" in item)
        self.assertIn("top", limitation)
        self.assertIn("edge", limitation)
        self.assertNotIn("bottom", limitation)

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
            # Solid cells are NaN in the air field.
            air_temperature_c=[60.0, float("nan"), 62.0, 61.0],
        )
        settings = EnclosureCFDSettings(ambient_c=45.0)
        settings.geometry.width_mm = 165.0
        settings.geometry.depth_mm = 100.0
        settings.geometry.height_mm = 50.0
        settings.geometry.wall_heat_transfer_w_m2k = 5.0

        result = adapt_cfd_result(mesh, cfd, settings=settings)

        metrics = {metric.key: metric for metric in result.metrics}
        # Through the walls, 16.0 K, and from the air to them (2026-10-02).
        self.assertAlmostEqual(metrics["mean_air_rise"].value, 30.9, places=1)
        self.assertAlmostEqual(metrics["internal_ambient"].value, 75.9, places=1)
        # The review of 2026-10-02: the cross-check compared that formula
        # with itself; the solved field's own mean is what it now reads.
        self.assertAlmostEqual(metrics["solved_mean_air"].value, 61.0)

    def test_cfd_adapter_flags_boussinesq_out_of_range_as_a_limitation(self):
        # docs/audit-2026-09-23.md CFD-7: beta * dT is usually cited as valid
        # below ~0.1; this is a fact about the solved field, not a design
        # defect, so it is a limitation rather than a scored finding.
        mesh = SimpleNamespace(cell_count=1000)
        cfd = SimpleNamespace(
            converged=True, iterations=100, mass_balance_error_pct=0.0,
            energy_balance_error_pct=0.0, maximum_velocity_m_s=0.1,
            maximum_air_temperature_c=40.0, maximum_solid_temperature_c=60.0,
            compute_backend="CPU", compute_device="CPU",
            boussinesq_beta_delta_t_max=0.42,
        )
        result = adapt_cfd_result(mesh, cfd)
        self.assertTrue(any("Boussinesq" in text for text in result.limitations))
        self.assertEqual({f.rule_id for f in result.findings}, set())

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

    def test_an_empty_live_capture_is_one_limitation_not_a_dc_004_per_rail(self):
        # P02 Rev E, 2026-09-29 16:50: 17 MEDIUM DC-004 "differs from the open
        # one", one per rail, from a capture KiCad had not sent yet.
        rail = {"stats": (5.0, 5.0, 0.0), "load_drop_v": 0.001, "source_voltage_v": 5.0,
                "saved_board_differences": ["live capture empty, retry after sync"]}
        result = adapt_dc_result({"/A": dict(rail), "/B": dict(rail)}, 5.0)
        self.assertNotIn("DC-004", {item.rule_id for item in result.findings})
        self.assertEqual(sum("empty live capture" in item for item in result.limitations), 1)

    def test_dc_006_reports_the_chain_drop_a_pass_element_hid(self):
        # DC-3. P02 Rev E's J2 chain (buck -> fuse -> eFuse -> shunt -> switch
        # -> J2) totals 56.1 mV that used to be invisible because every link
        # reset to nominal; this rail's source_voltage_v (now propagated,
        # dc_controller.py's pass_voltages) sits 56.1 mV below nominal_voltage_v.
        result = adapt_dc_result({"/J2_PD_HV": {
            "stats": (4.9439, 4.9439, 0.0), "load_drop_v": 0.0002,
            "source_voltage_v": 4.9439, "nominal_voltage_v": 5.0,
        }}, 5.0)
        finding = next(item for item in result.findings if item.rule_id == "DC-006")
        self.assertIn("56.1 mV", finding.description)
        # Under the budget, the chain is the breakdown of the verdict, not an
        # action of its own (P02 Rev E, 2026-09-29: 8 MEDIUM for <= 11.7 mV).
        self.assertEqual(finding.severity, FindingSeverity.INFO)
        total = next(item for item in result.metrics if item.key == "rail_1_drop_from_nominal")
        self.assertAlmostEqual(total.value, (0.0561 + 0.0002) / 5.0 * 100.0, places=6)
        metric = next(item for item in result.metrics if item.key == "rail_1_chain_drop")
        self.assertAlmostEqual(metric.value, 0.0561 / 5.0 * 100.0, places=6)

        # A rail with no pass-element ancestor reads the same value in both
        # fields and raises nothing.
        clean = adapt_dc_result({"+3V3": {
            "stats": (3.3, 3.3, 0.0), "load_drop_v": 0.0,
            "source_voltage_v": 3.3, "nominal_voltage_v": 3.3,
        }}, 5.0)
        self.assertNotIn("DC-006", {item.rule_id for item in clean.findings})

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


    def test_an_island_finding_states_the_load_and_current_left_out(self):
        # P02 Rev E /VBUS_SYS: "81 load node(s) were excluded" was one load,
        # U14's input, 0.848 A of the rail's 2.04 A (DC-7).
        detail = SimpleNamespace(
            valid=False, excluded_load_node_count=81, excluded_load_references=["U14"],
            excluded_load_current_a=0.848, total_load_current_a=2.04,
        )
        result = adapt_dc_result({"/VBUS_SYS": {
            "stats": (20.0, 20.0, 0.008), "detailed_result": detail, "engine": "hybrid",
            "load_drop_v": 0.008, "source_voltage_v": 20.0,
        }}, 5.0)
        island = next(item for item in result.findings if item.rule_id == "DC-001")
        self.assertIn("U14 (0.848 A, 42 % of the rail's 2.04 A)", island.description)
        self.assertEqual(island.components, ["U14"])

    def test_a_rail_without_current_is_no_data_and_a_lost_load_is_named(self):
        # P02 Rev E's /J2_LDO3V3 carried no load current and read "0.0 %
        # PASS"; a load whose pads found no copper vanished silently (DC-4).
        result = adapt_dc_result({
            "/J2_LDO3V3": {
                "stats": (3.3, 3.3, 0.0), "engine": "hybrid", "load_drop_v": 0.0,
                "source_voltage_v": 3.3, "loads": [], "configured_loads": [],
            },
            "/+5V_RAIL": {
                "stats": (5.0, 5.08, 0.015), "engine": "hybrid", "load_drop_v": 0.015,
                "source_voltage_v": 5.08,
                "loads": [{"node_id": 1, "current": 1.0, "ref_des": "J6"}],
                "configured_loads": [("J6", 1.0), ("U9", 0.25)],
            },
        }, 5.0)
        metrics = {metric.key: metric for metric in result.metrics}
        self.assertEqual(metrics["rail_1_drop"].status, "NO_DATA")
        lost = next(item for item in result.findings if item.rule_id == "DC-007")
        self.assertEqual(lost.components, ["U9"])
        self.assertIn("U9 (0.25 A)", lost.description)

    def test_pads_cut_off_from_the_ground_are_named_once(self):
        # P02 Rev E, 2026-09-30 13:46: C88's and R10's GND pads sat on F.Cu
        # pour pieces no via joins, and the solve only said "Ignoring
        # floating island" (276 and 329 nodes).
        ground = {"net": "GND", "combined_load_drop_v": 0.0, "stranded_pads": ["C88.2", "R10.2"]}
        rail = {
            "stats": (5.0, 5.08, 0.015), "engine": "hybrid", "load_drop_v": 0.015,
            "source_voltage_v": 5.08, "ground_return": ground,
        }
        result = adapt_dc_result({"/+5V_RAIL": dict(rail), "/+5V_U4": dict(rail)}, 5.0)
        stranded = [item for item in result.findings if item.rule_id == "DC-008"]
        self.assertEqual(len(stranded), 1)
        self.assertEqual(stranded[0].components, ["C88", "R10"])
        self.assertEqual(stranded[0].nets, ["GND"])

    def test_dc_states_the_missing_ground_return_and_the_assumed_plating(self):
        # Open since the 2026-09-13 audit and never said in the drop's own
        # limitations: the return path is not solved, and barrel plating is
        # the 20 um fallback.
        result = adapt_dc_result({"/+5V_RAIL": {
            "stats": (5.0, 5.08, 0.015), "engine": "hybrid", "load_drop_v": 0.015,
            "source_voltage_v": 5.08,
        }}, 5.0)
        self.assertTrue(any("ground return is not solved" in item for item in result.limitations))
        self.assertTrue(any("20 um of plating" in item for item in result.limitations))

    def test_a_junction_under_its_limit_by_less_than_20_k_is_flagged(self):
        # P02 Rev E, 2026-09-29 18:43: U14 at 126.06 C, 23.94 K under its
        # 150 C limit at the 45 C outside ambient; its sealed box adds about
        # 15 K, and a thermal PASS then rested on a ~9 K margin.
        from models import ComponentThermalResult
        u14 = ComponentThermalResult(
            "U14", board_temperature_c=115.9, junction_temperature_c=141.3, power_w=1.226,
            max_junction_c=150.0, margin_c=8.7, theta_jb_c_per_w=20.7, thermal_source="datasheet",
        )
        u16 = ComponentThermalResult(
            "U16", board_temperature_c=73.5, junction_temperature_c=74.2, power_w=0.061,
            max_junction_c=125.0, margin_c=50.8, theta_jb_c_per_w=11.2, thermal_source="datasheet",
        )

        result = adapt_thermal_result(ThermalResult(component_results=[u14, u16]), ambient_c=45.0)

        thin = [item for item in result.findings if item.rule_id == "TH-006"]
        self.assertEqual([item.components for item in thin], [["U14"]])
        self.assertEqual(thin[0].severity, FindingSeverity.MEDIUM)

    def test_a_thin_margin_the_mesh_can_erase_is_high(self):
        # P02 Rev E, 2026-09-30 13:53: U15 at 122.3 C, 2.7 K under 125 C,
        # while the hotspot moved 3.28 K between 0.2 and 0.4 mm. Moving as
        # much, U15 extrapolates to 125.6 C.
        from models import ComponentThermalResult
        u15 = ComponentThermalResult(
            "U15", board_temperature_c=118.3, junction_temperature_c=122.3, power_w=0.36,
            max_junction_c=125.0, margin_c=2.7, theta_jb_c_per_w=11.2, thermal_source="datasheet",
        )
        result = adapt_thermal_result(
            ThermalResult(component_results=[u15]), ambient_c=45.0,
            mesh_sensitivity={
                "fine_grid_mm": 0.2, "coarse_grid_mm": 0.4, "fine_hotspot_c": 118.27,
                "coarse_hotspot_c": 114.99, "delta_c": 3.28,
                "component_tj_c": {"U15": (122.3, 119.02)},
            },
        )
        thin = next(item for item in result.findings if item.rule_id == "TH-006")
        self.assertEqual(thin.severity, FindingSeverity.HIGH)
        self.assertIn("125.6 C, over its 125 C limit", thin.description)

    def test_a_fuse_margin_speaks_of_its_derating_not_a_junction(self):
        # P02 Rev E, 2026-10-01 00:21: F1 (Bourns SF-1206F, -20 to +105 C) at
        # 88.65 C read as a thin junction margin, to be closed with an
        # efficiency curve and a thetaJB it does not have.
        from models import ComponentThermalResult
        f1 = ComponentThermalResult(
            "F1", board_temperature_c=87.5, junction_temperature_c=88.65, power_w=0.05669,
            max_junction_c=105.0, margin_c=16.35, theta_jb_c_per_w=20.0, thermal_source="estimate",
        )
        result = adapt_thermal_result(ThermalResult(component_results=[f1]), ambient_c=45.0)
        thin = next(item for item in result.findings if item.rule_id == "TH-006")
        self.assertEqual(thin.title, "F1 temperature margin is thin")
        self.assertIn("under its 105 C operating limit", thin.description)
        self.assertIn("current derating", thin.recommendation)
        self.assertNotIn("Tj", thin.description)

    def test_a_fuse_its_derated_rating_covers_is_not_a_thin_margin(self):
        # P02 Rev E, 2026-10-01 00:58: F1 (SF-1206F500-2, 5 A) sat on 87.2 C
        # of board, 16.65 K under 105 C, carrying J1's 2.168 A. Bourns' thermal
        # derating curve keeps 90.5 % of its rating there.
        from models import ComponentThermalResult, LossContribution, PowerStageResult
        derating = [{"temperature_c": -19.8, "percent": 105.7}, {"temperature_c": 0.0, "percent": 103.0},
                    {"temperature_c": 99.9, "percent": 88.7}, {"temperature_c": 105.6, "percent": 87.7}]
        f1 = ComponentThermalResult(
            "F1", board_temperature_c=87.22, junction_temperature_c=88.35, power_w=0.05669,
            max_junction_c=105.0, margin_c=16.65, theta_jb_c_per_w=20.0, thermal_source="estimate",
            rated_current_a=5.0, current_derating=derating,
            current_derating_source="Bourns SF-1206F thermal derating curve",
        )
        stage = PowerStageResult("F1 J1 fuse", "F1", "F1", 20.0, 20.0, 2.168, 2.168, 0.999, "analytic",
                                 losses=[LossContribution("F1", "resistive-i2r", 0.0564)])
        result = adapt_thermal_result(
            ThermalResult(component_results=[f1]), ambient_c=45.0, power_stage_reports=[stage],
        )
        self.assertNotIn("TH-006", {item.rule_id for item in result.findings})
        held = next(item for item in result.findings if item.rule_id == "TH-007")
        self.assertEqual(held.severity, FindingSeverity.INFO)
        self.assertIn("keeps 90.5 % of its 5 A rating", held.description)
        self.assertIn("4.53 A; it carries 2.17 A, 48 % of that", held.description)

    def test_a_stage_closed_by_an_estimated_efficiency_is_data_to_complete(self):
        # P02 Rev E, 2026-10-01: U4's 1.81 W rests on a configured 93 %, an
        # engineering estimate, out of 4.82 W of board heat; U6's 0.0133 W
        # on another. Only a residual limitation said so.
        from models import LossContribution, PowerStageResult
        fallback = ("configured efficiency fallback (incomplete physical data); source=estimate; "
                    "confidence=low")
        u4 = PowerStageResult("U4 buck-boost 5 V machine", "Q10", "U4", 20.0, 5.08, 1.29, 4.726, 0.93,
                              fallback, losses=[LossContribution("Q10", "conduction", 1.521),
                                                LossContribution("L1", "residual", 0.289)])
        u6 = PowerStageResult("U6 +3V3AO module", "U6", "U6", 19.7, 3.32, 0.003, 0.016, 0.8,
                              fallback, losses=[LossContribution("U6", "residual", 0.0133)])
        result = adapt_thermal_result(
            ThermalResult(total_input_power_w=4.82), power_stage_reports=[u4, u6],
        )
        found = {item.components[0]: item for item in result.findings if item.rule_id == "TH-008"}
        self.assertEqual(found["U4"].severity, FindingSeverity.LOW)
        self.assertIn("loses 1.81 W, 38% of the 4.82 W", found["U4"].description)
        self.assertEqual(found["U6"].severity, FindingSeverity.INFO)
        # The 21:42 report gave U6's share as "0%".
        self.assertIn("loses 0.0133 W, 0.28% of the 4.82 W", found["U6"].description)

    def test_an_enclosure_wall_coefficient_left_at_its_default_is_data_to_complete(self):
        # P02 Rev E, 2026-10-01 22:29: the default 5 W/m2K put the air 16.2 K
        # above 45 C through the walls (4.82 W over 0.0595 m2), 31.3 K with
        # the air's film against them; only the first part follows the wall
        # coefficient.
        settings = EnclosureCFDSettings(ambient_c=45.0)
        settings.geometry.width_mm, settings.geometry.depth_mm = 165.0, 100.0
        found = adapt_thermal_result(
            ThermalResult(total_input_power_w=4.82), ambient_c=45.0, enclosure_air_rise_k=31.3,
            enclosure_ambient_applied=True, enclosure=settings,
        ).findings
        (wall,) = [item for item in found if item.rule_id == "TH-009"]
        self.assertEqual(wall.severity, FindingSeverity.LOW)
        self.assertIn("internal air runs 31.3 K above the ambient, 16.2 K of it through the walls",
                      wall.description)
        self.assertIn("at 4 or 6 W/m2K the rise would be 35.4 or 28.6 K.", wall.description)
        self.assertNotIn("size is the default", wall.description)
        # What TE-002 counts in the model's uncertainty: 2.7 K cooler at 6 W/m2K.
        from cfd_model import default_wall_air_uncertainty_k
        self.assertAlmostEqual(default_wall_air_uncertainty_k(settings, 16.2), 2.7)
        settings.geometry.wall_heat_transfer_w_m2k = 6.5
        self.assertEqual(default_wall_air_uncertainty_k(settings, 12.5), 0.0)
        self.assertFalse(any(item.rule_id == "TH-009" for item in adapt_thermal_result(
            ThermalResult(), enclosure_air_rise_k=12.5, enclosure_ambient_applied=True,
            enclosure=settings,
        ).findings))

    def test_the_least_junction_margin_is_named_when_not_the_hottest_parts(self):
        # P02 Rev E, 2026-10-01 19:12: U14 is the hottest junction (124.3 C of
        # 150, 25.7 K) but U4 keeps 22.4 K under 125 C. F1, judged by its
        # current derating, keeps 16.65 K under 105 C and is left out.
        from models import ComponentThermalResult
        u14 = ComponentThermalResult("U14", board_temperature_c=102.0, junction_temperature_c=124.33,
                                     power_w=1.08, max_junction_c=150.0, margin_c=25.67)
        u4 = ComponentThermalResult("U4", board_temperature_c=99.7, junction_temperature_c=102.6,
                                    power_w=0.228, max_junction_c=125.0, margin_c=22.4)
        f1 = ComponentThermalResult("F1", board_temperature_c=87.22, junction_temperature_c=88.35,
                                    power_w=0.057, max_junction_c=105.0, margin_c=16.65,
                                    rated_current_a=5.0,
                                    current_derating=[{"temperature_c": 99.9, "percent": 88.7}])
        result = adapt_thermal_result(ThermalResult(component_results=[u14, u4, f1]))
        least = next(item for item in result.metrics if item.key == "least_junction_margin")
        self.assertEqual((least.label, least.value), ("U4 junction margin, the least", 22.4))

    def test_a_junction_over_its_limit_gets_a_sized_thermal_via_remediation(self):
        # COH-1: advisor.thermal_advisor was imported by no production code,
        # so no thermal action ever carried a structured remediation.
        from models import ComponentThermalResult
        part = ComponentThermalResult(
            "Q10", board_temperature_c=150.0, junction_temperature_c=151.1, power_w=1.4,
            max_junction_c=150.0, margin_c=-1.1, theta_jb_c_per_w=0.8,
            thermal_source="datasheet",
        )
        result = adapt_thermal_result(
            ThermalResult(component_results=[part]), ambient_c=45.0,
        )
        finding = next(item for item in result.findings if item.rule_id == "TH-001")
        self.assertEqual(finding.components, ["Q10"])
        self.assertEqual(finding.remediations[0].action, "ADD_THERMAL_VIAS")
        self.assertIn("existing vias not counted", finding.remediations[0].predicted_gain)

if __name__ == "__main__":
    unittest.main()
