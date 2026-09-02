from types import SimpleNamespace
import unittest

from models import ACAnalysisSettings, ACMeasurementPort, ACSourceModel, CapacitorModel

from application.report_presenters import (
    format_ac_report,
    format_cfd_report,
    format_dc_report,
    format_differential_report,
    format_emc_findings_lines,
    format_emc_near_field_lines,
    format_emc_phase10_lines,
    format_emc_report,
    format_emc_spice_audit_lines,
    format_emc_summary_lines,
    format_thermal_report,
)


class ReportPresenterTests(unittest.TestCase):
    def test_ac_report_uses_optimized_result_and_recommendations(self):
        baseline = SimpleNamespace()
        final = SimpleNamespace(
            meets_target=True, worst_impedance_ohm=0.04, worst_frequency_hz=1e6,
            target_impedance_ohm=0.05, compute_backend="CPU", compute_device="host",
            compute_solve_seconds=0.1, compute_transfer_seconds=0.0,
            compute_relative_residual=1e-8, compute_cache_hits=2,
            frequencies_hz=[1e3, 1e6], impedance_ohm=[0.01 + 0j, 0.04 + 0j],
        )
        recommendation = SimpleNamespace(action="ADD", ref_des="C1", capacitance_f=1e-6)
        optimization = SimpleNamespace(optimized=final, recommendations=[recommendation])
        settings = ACAnalysisSettings(
            rail_name="+3V3", source=ACSourceModel(ref_des="U1"),
            measurement_port=ACMeasurementPort(ref_des="U2"),
            capacitors=[CapacitorModel("C1", capacitance_f=1e-6)],
        )
        report = format_ac_report(baseline, optimization, settings)
        self.assertIn("Status: PASS", report)
        self.assertIn("ADD C1", report)
        self.assertIn("1uF", report)
        self.assertIn("Worst/target ratio", report)
        self.assertIn("Rail / return: +3V3 / GND", report)
        self.assertIn("C1: 1uF", report)

    def test_dc_report_preserves_model_and_compute_provenance(self):
        detailed = SimpleNamespace(
            valid=False, excluded_load_references=["U2"], excluded_load_node_count=3,
        )
        compute = SimpleNamespace(
            backend="CPU", device="host", solver_method="CG", converged=True,
            solve_seconds=0.2, relative_residual=1e-7, iterations=8,
            matrix_assembly="CSR", warm_start_used=True, initial_guess_used=False,
            fallback_reason="",
        )
        report = format_dc_report({"VCC": {
            "stats": (3.1, 3.3, 0.2), "grid_size_mm": 0.2,
            "requested_grid_size_mm": 0.1, "adaptive_grid": True,
            "detailed_result": detailed, "compute_metadata": compute,
        }})
        self.assertIn("Rail: VCC", report)
        self.assertIn("adapted for mesh safety", report)
        self.assertIn("INCOMPLETE", report)
        self.assertIn("U2", report)
        self.assertIn("method CG", report)

    def test_cfd_report_contains_conservation_and_scope(self):
        mesh = SimpleNamespace(cell_count=24, shape=(2, 3, 4))
        result = SimpleNamespace(
            iterations=12, converged=False, maximum_velocity_m_s=0.4,
            maximum_air_temperature_c=32.0, maximum_solid_temperature_c=45.0,
            total_heat_w=3.0, mass_balance_error_pct=0.2,
            energy_balance_error_pct=0.4, compute_backend="CPU",
            compute_device="host", compute_solve_seconds=0.3,
            compute_relative_residual=2e-5,
        )
        report = format_cfd_report(mesh, result)
        self.assertIn("Cells: 24 (2 x 3 x 4)", report)
        self.assertIn("limit reached", report)
        self.assertIn("Mass balance error", report)
        self.assertIn("turbulence", report)

    def test_thermal_report_contains_compute_provenance_and_model_scope(self):
        mesh = SimpleNamespace(
            grid_size_mm=0.25, requested_grid_size_mm=0.1, adaptive_grid=True,
        )
        component = SimpleNamespace(
            ref_des="U1", junction_temperature_c=82.0, power_w=1.5,
            margin_c=18.0, theta_jb_c_per_w=12.0, model_source="datasheet",
            thermal_source="board", thermal_condition="natural convection",
        )
        result = SimpleNamespace(
            hotspot=SimpleNamespace(temperature_c=70.0, x_mm=1.0, y_mm=2.0, z_mm=0.8),
            total_input_power_w=2.0, total_boundary_power_w=1.99,
            energy_balance_error_pct=0.5, convection_coefficient_w_m2k=8.0,
            iterations=20, converged=True, compute_backend="CPU", compute_device="host",
            compute_matrix_assembly="CSR", compute_warm_start_used=False,
            compute_cpu_threads=4, compute_solve_seconds=0.4,
            compute_transfer_seconds=0.0, compute_relative_residual=1e-7,
            compute_iterations=9, component_results=[component],
            compute_fallback_reason="",
        )
        report = format_thermal_report(
            mesh, result, elapsed_seconds=0.7, color_map="inferno",
            show_internal_copper_layers=False,
        )
        self.assertIn("Hotspot: 70.000 C", report)
        self.assertIn("requested 0.1 mm; adapted", report)
        self.assertIn("Compute backend: CPU (host)", report)
        self.assertIn("U1: Tj=82.00 C", report)
        self.assertIn("not a volumetric CFD airflow solution", report)

    def test_differential_report_keeps_target_band_and_routing_evidence(self):
        stackup = SimpleNamespace(source="board", trustworthy=True, warnings=[])
        pair = SimpleNamespace(
            name="USB", positive_net="USB_D+", negative_net="USB_D-",
            interface="USB2", confidence="HIGH", target_impedance_ohm=90.0,
        )
        section = SimpleNamespace(
            ground_clearance_mm=0.2, geometry_mode="JLCPCB_COPLANAR",
            layer_name="F.Cu", topology="microstrip", length_mm=10.0,
            width_mm=0.15, gap_mm=0.15, differential_impedance_ohm=91.0,
            reference_above="", reference_below="GND", reference_coverage_pct=98.0,
            refinement_status="2D_BASELINE", two_d_impedance_ohm=91.0,
            three_d_impedance_ohm=91.0, refinement_reason="", warnings=["local neckdown"],
        )
        recommendation = SimpleNamespace(
            recommended_width_mm=0.16, layer_name="F.Cu", current_width_mm=0.15,
            current_gap_mm=0.15, recommended_gap_mm=0.14, action="ADJUST_GEOMETRY",
            predicted_impedance_ohm=90.0, geometry_mode="JLCPCB_COPLANAR",
            recommended_ground_clearance_mm=0.2, feasibility="FEASIBLE",
            confidence="HIGH", warnings=[],
        )
        result = SimpleNamespace(
            pair=pair, status="PASS", weighted_impedance_ohm=90.5, error_pct=0.56,
            minimum_impedance_ohm=89.0, maximum_impedance_ohm=92.0,
            length_symmetry_status="PASS", positive_length_mm=10.0,
            negative_length_mm=9.9, length_mismatch_mm=0.1,
            length_mismatch_pct=1.0, estimated_skew_ps=0.6, skew_limit_ps=5.0,
            maximum_length_mismatch_mm=0.8, shorter_net="USB_D-", skew_margin_ps=4.4,
            sections=[section], warnings=[], recommendations=[recommendation],
        )
        report = format_differential_report([result], stackup, 10.0)
        self.assertIn("81.000 .. 99.000 ohm", report)
        self.assertIn("USB_D+=10.000 mm", report)
        self.assertIn("WARNING: local neckdown", report)
        self.assertIn("ADJUST_GEOMETRY [FEASIBLE; HIGH]", report)
        self.assertIn("not full-wave simulation", report)

    def test_emc_sections_keep_risk_provenance_and_empty_states(self):
        settings = SimpleNamespace(
            standard="CISPR 32", market="EU", frequency_start_hz=30e6,
            frequency_stop_hz=1e9, sources=[],
        )
        result = SimpleNamespace(
            severity_counts={"HIGH": 1}, risk_score=73, total_checks=44,
            findings=[], elapsed_seconds=1.25,
            score_penalties_by_rule={"EMC-01": 7}, per_net_scores={},
            test_plan=["Scan 30 MHz to 1 GHz"],
            regulatory_coverage=["Radiated emissions"],
            limitations=["Pre-compliance model"],
        )
        summary = "\n".join(format_emc_summary_lines(settings, result))
        details = "\n".join(format_emc_findings_lines(result))
        self.assertIn("Target: CISPR 32 (EU)", summary)
        self.assertIn("EMC-01 -7", summary)
        self.assertIn("geometry-only analysis", summary)
        self.assertIn("No finding was generated", details)
        self.assertIn("No net-specific penalty", details)
        self.assertIn("Scan 30 MHz to 1 GHz", details)
        result.field_simulation = None
        result.phase10_result = None
        near_field = "\n".join(format_emc_near_field_lines(result))
        self.assertIn("Disabled or unavailable", near_field)
        phase10 = "\n".join(format_emc_phase10_lines(settings, result))
        spice = "\n".join(format_emc_spice_audit_lines(result))
        report = format_emc_report(settings, result)
        self.assertIn("Phase 10 multi-fidelity EMC", phase10)
        self.assertIn("No component-specific SPICE model", spice)
        self.assertIn("EMI / EMC Pre-compliance Results", report)
        self.assertIn("Model limitations", report)


if __name__ == "__main__":
    unittest.main()
