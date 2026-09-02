"""Pure text presenters for analysis results; no wx or KiCad dependencies."""

from ac_model import format_capacitance
from power_loss import format_power_stage_report


def format_ac_report(result, optimization=None, settings=None):
    final = optimization.optimized if optimization else result
    target = float(getattr(final, "target_impedance_ohm", 0.0) or 0.0)
    worst = float(getattr(final, "worst_impedance_ohm", 0.0) or 0.0)
    frequencies = list(getattr(final, "frequencies_hz", ()) or ())
    impedance = list(getattr(final, "impedance_ohm", ()) or ())
    failures = [
        float(frequency) for frequency, value in zip(frequencies, impedance)
        if target > 0.0 and abs(complex(value)) > target
    ]
    capacitors = [
        item for item in (getattr(settings, "capacitors", ()) or ())
        if bool(getattr(item, "enabled", True))
    ]
    lines = [
        "AC Impedance Analysis Results",
        "=============================",
        f"Status: {'PASS' if final.meets_target else 'TARGET NOT MET'}",
        f"Worst |Z|: {final.worst_impedance_ohm:.6g} ohm",
        f"Worst frequency: {final.worst_frequency_hz:.6g} Hz",
        f"Target: {final.target_impedance_ohm:.6g} ohm",
        f"Worst/target ratio: {(worst / target if target > 0 else 0.0):.3g}x",
        f"Target exceedance: {len(failures)}/{len(frequencies)} point(s)"
        + (f", {failures[0]:.6g} to {failures[-1]:.6g} Hz" if failures else ""),
        f"AC mesh: {int(getattr(final, 'mesh_node_count', 0)):,} nodes; requested "
        f"{float(getattr(final, 'requested_grid_size_mm', 0.0)):g} mm; effective "
        f"{float(getattr(final, 'effective_grid_size_mm', 0.0)):g} mm",
        f"Compute backend: {final.compute_backend} ({final.compute_device})",
        f"Sparse solve time: {final.compute_solve_seconds:.4g} s "
        f"(transfer {final.compute_transfer_seconds:.4g} s)",
        f"Linear residual: {final.compute_relative_residual:.4g}; "
        f"CUDA structure cache hits: {final.compute_cache_hits}",
    ]
    if settings is not None:
        source = getattr(settings, "source", None)
        port = getattr(settings, "measurement_port", None)
        lines.extend([
            "",
            "Model inputs:",
            f"  Rail / return: {getattr(settings, 'rail_name', '')} / "
            f"{getattr(settings, 'ground_net_name', '')}",
            f"  Source: {getattr(source, 'ref_des', '')}; "
            f"R={float(getattr(source, 'resistance_ohm', 0.0)):.6g} ohm; "
            f"L={float(getattr(source, 'inductance_h', 0.0)):.6g} H",
            f"  Measurement component: {getattr(port, 'ref_des', '')}",
            f"  Enabled capacitors: {len(capacitors)}",
        ])
        lines.extend(
            f"    - {item.ref_des}: {format_capacitance(item.capacitance_f)}, "
            f"ESR={item.esr_ohm:.6g} ohm, ESL={item.esl_h:.6g} H "
            f"[{item.model_source}]"
            for item in capacitors
        )
    if optimization:
        lines.extend(["", "Decoupling recommendations:"])
        if optimization.recommendations:
            lines.extend(
                f"  - {item.action} {item.ref_des}: {format_capacitance(item.capacitance_f)}"
                for item in optimization.recommendations
            )
        else:
            lines.append("  - No capacitor changes recommended.")
    lines.extend([
        "",
        "Model note: AC mesh resolution is independent from the DC voltage-drop mesh. "
        "ESR/ESL and distributed inductance may be estimates; review before sign-off.",
    ])
    return "\n".join(lines)


def format_cfd_report(mesh, result):
    return "\n".join([
        "Phase 4 Enclosure CFD Results",
        "=============================",
        "Mode: steady incompressible laminar flow with Boussinesq buoyancy",
        f"Cells: {mesh.cell_count:,} ({mesh.shape[0]} x {mesh.shape[1]} x {mesh.shape[2]})",
        f"Iterations: {result.iterations} ({'converged' if result.converged else 'limit reached'})",
        f"Maximum velocity: {result.maximum_velocity_m_s:.6g} m/s",
        f"Maximum air temperature: {result.maximum_air_temperature_c:.3f} C",
        f"Maximum solid temperature: {result.maximum_solid_temperature_c:.3f} C",
        f"Mapped heat: {result.total_heat_w:.6g} W",
        f"Mass balance error: {result.mass_balance_error_pct:.4g}%",
        f"Energy balance error: {result.energy_balance_error_pct:.4g}%",
        f"Compute backend: {result.compute_backend} ({result.compute_device})",
        f"Last energy solve: {result.compute_solve_seconds:.4g} s, residual {result.compute_relative_residual:.4g}",
        "",
        "Model scope: structured volumetric CFD, boundary-patch fans/vents, and "
        "conjugate solid-air heat transfer. Fan blades, turbulence, radiation, "
        "and transient effects are outside this Phase 4 solver.",
    ])


def format_dc_report(system_results):
    lines = ["System Simulation Results:", "=========================="]
    for net, data in system_results.items():
        vmin, vmax, drop = data["stats"]
        lines.extend([f"Rail: {net}", f"  Range: {vmin:.4f} - {vmax:.4f} V", f"  Drop:  {drop:.4f} V", ""])
        actual_grid = data.get("grid_size_mm")
        requested_grid = data.get("requested_grid_size_mm", actual_grid)
        if actual_grid is not None:
            suffix = " (adapted for mesh safety)" if data.get("adaptive_grid") else ""
            lines.append(f"  DC grid: {actual_grid:.4g} mm{suffix}")
            if data.get("adaptive_grid"):
                lines.append(f"  Requested DC grid: {requested_grid:.4g} mm")
        detailed = data.get("detailed_result")
        if detailed is not None and not getattr(detailed, "valid", True):
            excluded = ", ".join(getattr(detailed, "excluded_load_references", []))
            refs = f" [{excluded}]" if excluded else ""
            lines.append(
                f"  Model status: INCOMPLETE — {getattr(detailed, 'excluded_load_node_count', 0)} "
                f"load node(s) excluded on source-free copper island(s){refs}"
            )
        density = data.get("current_density")
        if density is not None:
            lines.extend([
                f"  Maximum planar current density: {density.maximum_planar_a_per_mm2:.6g} A/mm²",
                f"  Planar current density P99.5: {density.percentile_99_5_a_per_mm2:.6g} A/mm²",
                f"  Maximum route current density: {density.maximum_track_a_per_mm2:.6g} A/mm²",
                f"  Maximum zone current density: {density.maximum_zone_a_per_mm2:.6g} A/mm²",
                f"  Maximum via current: {density.maximum_via_current_a:.6g} A",
                f"  Maximum via barrel current density: {density.maximum_via_a_per_mm2:.6g} A/mm²",
            ])
            hotspot = density.planar_hotspot
            if hotspot is not None:
                lines.append(
                    f"  Planar hotspot: {hotspot.copper_kind} on layer "
                    f"{getattr(hotspot, 'layer_name', hotspot.layer_id)} "
                    f"at ({hotspot.x_mm:.4f}, {hotspot.y_mm:.4f}) mm"
                )
            lines.append("  Current-density confidence: ESTIMATED")
            for warning in density.warnings:
                lines.append(f"  Current-density warning: {warning}")
        compute = data.get("compute_metadata")
        if compute is not None:
            lines.extend([
                f"  Backend: {compute.backend} ({compute.device}), method {compute.solver_method}, "
                f"status {'converged' if compute.converged else 'not converged'}",
                f"  Solve: {compute.solve_seconds:.4g} s, residual {compute.relative_residual:.3g}, "
                f"iterations {compute.iterations}",
                f"  Sparse path: {compute.matrix_assembly}; warm start "
                f"{'yes' if compute.warm_start_used else 'no'}; nominal initial guess "
                f"{'yes' if compute.initial_guess_used else 'no'}",
            ])
            if compute.fallback_reason:
                lines.append(f"  Fallback: {compute.fallback_reason}")
            lines.append("")
        if density is not None:
            lines.extend([
                "  Current-density scope: DC branch-current diagnostic; not an IPC ampacity certification.",
                "",
            ])
    return "\n".join(lines) + "\n"


def format_thermal_report(
    mesh, result, *, coupled=False, coupled_result=None, elapsed_seconds=None,
    color_map="inferno", color_scale_minimum_c=None,
    color_scale_minimum_mode="AUTO", color_scale_maximum_c=None,
    color_scale_maximum_mode="AUTO", show_internal_copper_layers=True,
    power_stage_reports=(),
):
    hotspot = result.hotspot
    lines = [
        "3D Thermal Analysis Results", "===========================",
        f"Mode: {'Coupled DC / thermal' if coupled else 'Thermal'}",
        f"Hotspot: {hotspot.temperature_c:.3f} C at ({hotspot.x_mm:.2f}, {hotspot.y_mm:.2f}, {hotspot.z_mm:.3f}) mm",
        f"Input heat: {result.total_input_power_w:.6g} W",
        f"Boundary heat: {result.total_boundary_power_w:.6g} W",
        f"Energy balance error: {result.energy_balance_error_pct:.4g}%",
        f"Effective h: {result.convection_coefficient_w_m2k:.4g} W/m2K",
        f"Iterations: {result.iterations} ({'converged' if result.converged else 'limit reached'})",
        f"Thermal grid: {mesh.grid_size_mm:.4g} mm" + (
            f" (requested {mesh.requested_grid_size_mm:.4g} mm; adapted)" if mesh.adaptive_grid else ""
        ),
        f"Thermal colors: {str(color_map).title()}",
        "Thermal colour minimum: " + (
            f"{float(color_scale_minimum_c):.3g} C ({str(color_scale_minimum_mode).lower()})"
            if color_scale_minimum_c is not None else "calculated minimum"
        ),
        "Thermal colour maximum: " + (
            f"{float(color_scale_maximum_c):.3g} C ({str(color_scale_maximum_mode).lower()})"
            if color_scale_maximum_c is not None else "calculated hotspot"
        ),
        "Internal copper maps: enabled" if show_internal_copper_layers else "Internal copper maps: disabled",
        f"Compute backend: {result.compute_backend} ({result.compute_device})",
        f"Sparse matrix path: {result.compute_matrix_assembly}",
        "CUDA warm start: device-resident previous thermal solution" if result.compute_warm_start_used
        else "CUDA warm start: unavailable (first solve or CPU backend)",
        f"CPU threads: {result.compute_cpu_threads}",
        f"Solve time: {result.compute_solve_seconds:.4g} s (transfer {result.compute_transfer_seconds:.4g} s)",
        f"Linear residual: {result.compute_relative_residual:.4g} ({result.compute_iterations} iteration(s))",
    ]
    if coupled:
        lines.extend(["", "Coupled DC rail solver status:"])
        dc_results = getattr(coupled_result, "dc_results", {}) if coupled_result else {}
        if not dc_results:
            lines.append("  - No coupled DC metadata available.")
        for rail_name, dc_result in dc_results.items():
            compute = getattr(dc_result, "compute_metadata", None)
            if compute is None:
                lines.append(f"  - {rail_name}: metadata unavailable")
                continue
            status = "converged" if compute.converged else "not converged"
            if not getattr(dc_result, "valid", True):
                excluded = ", ".join(getattr(dc_result, "excluded_load_references", []))
                refs = f"; {excluded}" if excluded else ""
                status += f", INCOMPLETE ({getattr(dc_result, 'excluded_load_node_count', 0)} load nodes excluded{refs})"
            lines.append(
                f"  - {rail_name}: {compute.backend}/{compute.solver_method}, {status}, "
                f"residual={compute.relative_residual:.3g}, iterations={compute.iterations}, "
                f"solve={compute.solve_seconds:.4g} s"
            )
            if compute.fallback_reason:
                lines.append(f"    fallback: {compute.fallback_reason}")
    if elapsed_seconds is not None:
        lines.append(f"Total elapsed time: {float(elapsed_seconds):.3f} s")
    lines.extend(["", "Power-stage loss accounting:"])
    if power_stage_reports:
        for stage in power_stage_reports:
            lines.extend(format_power_stage_report(stage))
    else:
        lines.append("  - No configured power stage.")
    lines.extend(["", "Component junction estimates:"])
    if result.component_results:
        for component in result.component_results:
            status = "OK" if component.margin_c >= 0 else "OVER LIMIT"
            lines.append(
                f"  - {component.ref_des}: Tj={component.junction_temperature_c:.2f} C, "
                f"P={component.power_w:.4g} W, margin={component.margin_c:.2f} C "
                f"[theta JB={component.theta_jb_c_per_w:.4g} C/W; {status}; "
                f"{component.model_source}; thermal={component.thermal_source}]"
            )
            if component.thermal_condition:
                lines.append(f"      thermal condition: {component.thermal_condition}")
    else:
        lines.append("  - No mapped component heat source.")
    lines.extend(["", "Model scope: steady-state 3D solid conduction with convective boundaries; this is not a volumetric CFD airflow solution."])
    if result.compute_fallback_reason:
        lines.append(f"Compute fallback: {result.compute_fallback_reason}")
    return "\n".join(lines)


def format_differential_report(results, stackup, target_tolerance_pct):
    lines = [
        "Differential Pair Impedance Results",
        "===================================",
        f"Stackup: {stackup.source} ({'trusted' if stackup.trustworthy else 'estimate only'})",
        "",
    ]
    if stackup.warnings:
        lines.append("Stackup warnings:")
        lines.extend(f"  - {warning}" for warning in stackup.warnings)
        lines.append("")
    for result in results:
        pair = result.pair
        lines.append(
            f"{pair.name}: {pair.positive_net} / {pair.negative_net} "
            f"[{pair.interface}; {pair.confidence}]"
        )
        lines.append(
            f"  Status: {result.status}; Zdiff={result.weighted_impedance_ohm:.3f} ohm; "
            f"target={pair.target_impedance_ohm:g} ohm; error={result.error_pct:+.2f}%"
        )
        tolerance_fraction = max(0.0, float(target_tolerance_pct)) / 100.0
        acceptance_low = pair.target_impedance_ohm * (1.0 - tolerance_fraction)
        acceptance_high = pair.target_impedance_ohm * (1.0 + tolerance_fraction)
        lines.append(
            f"  Target-centred acceptance band: {acceptance_low:.3f} .. "
            f"{acceptance_high:.3f} ohm (+/-{target_tolerance_pct:g}%)"
        )
        lines.append(
            f"  Observed section range: {result.minimum_impedance_ohm:.3f} .. "
            f"{result.maximum_impedance_ohm:.3f} ohm"
        )
        lines.append(
            f"  Length symmetry: {result.length_symmetry_status}; "
            f"{pair.positive_net}={result.positive_length_mm:.3f} mm, "
            f"{pair.negative_net}={result.negative_length_mm:.3f} mm; "
            f"dL={result.length_mismatch_mm:.3f} mm "
            f"({result.length_mismatch_pct:.3f}%); estimated skew="
            f"{result.estimated_skew_ps:.2f} ps / {result.skew_limit_ps:g} ps limit; "
            f"maximum dL={result.maximum_length_mismatch_mm:.3f} mm"
        )
        if result.shorter_net:
            lines.append(
                f"  Shorter net: {result.shorter_net}; remaining skew margin="
                f"{result.skew_margin_ps:.2f} ps."
            )
        lines.extend([
            "  Length basis: routed planar copper centreline; via-barrel delay is not included.",
            "  Status basis: length-weighted Zdiff plus routing/reference qualification; "
            "section min/max is diagnostic.",
            "  Diagnostic sections (not independent routing rules):",
        ])
        section_warnings = set()
        for section in result.sections:
            coplanar_detail = (
                f", GND gap={section.ground_clearance_mm:.3f} mm"
                if section.geometry_mode == "JLCPCB_COPLANAR" else ""
            )
            lines.append(
                f"  - {section.layer_name}: {section.topology}, "
                f"length={section.length_mm:.3f} mm, "
                f"w={section.width_mm:.3f} mm, gap={section.gap_mm:.3f} mm, "
                f"Zdiff={section.differential_impedance_ohm:.3f} ohm, "
                f"refs={section.reference_above or '-'} / {section.reference_below or '-'}, "
                f"coverage={section.reference_coverage_pct:.1f}%{coplanar_detail}"
            )
            if section.refinement_status not in {"2D_BASELINE", "NOT_APPLICABLE"}:
                lines.append(
                    f"    3-D refinement: {section.refinement_status}; "
                    f"2-D={section.two_d_impedance_ohm:.3f} ohm -> "
                    f"refined={section.three_d_impedance_ohm:.3f} ohm; "
                    f"reason={section.refinement_reason}"
                )
            section_warnings.update(section.warnings)
        section_warnings.update(result.warnings)
        lines.extend(f"  WARNING: {warning}" for warning in sorted(section_warnings))
        if result.recommendations:
            lines.append("  Routing decision:")
            for recommendation in result.recommendations:
                if recommendation.recommended_width_mm:
                    scope = (
                        f"{recommendation.layer_name or 'route'}, current "
                        f"w={recommendation.current_width_mm:.3f} mm / "
                        f"gap={recommendation.current_gap_mm:.3f} mm -> "
                    )
                    geometry = (
                        f"{scope}w={recommendation.recommended_width_mm:.3f} mm, "
                        f"gap={recommendation.recommended_gap_mm:.3f} mm"
                    )
                elif recommendation.action == "RESTORE_GND_REFERENCE":
                    geometry = "restore a continuous adjacent GND reference"
                elif recommendation.action == "REVIEW_DISCONTINUITIES":
                    geometry = "no safe global geometry; use the local sections below"
                elif recommendation.action == "ROUTE_PAIR":
                    geometry = "route both nets as a coupled same-layer pair"
                else:
                    geometry = "review stackup and fabrication limits"
                predicted = (
                    f"; predicted Zdiff={recommendation.predicted_impedance_ohm:.3f} ohm"
                    if recommendation.predicted_impedance_ohm > 0 else ""
                )
                ground_rule = (
                    f"GND gap target={recommendation.recommended_ground_clearance_mm:.3f} mm"
                    if recommendation.geometry_mode == "JLCPCB_COPLANAR"
                    and recommendation.recommended_width_mm > 0
                    else f"GND clearance >= {recommendation.recommended_ground_clearance_mm:.3f} mm"
                )
                lines.append(
                    f"  - {recommendation.action} [{recommendation.feasibility}; "
                    f"{recommendation.confidence}]: {geometry}{predicted}; {ground_rule}"
                )
                lines.extend(f"    NOTE: {note}" for note in recommendation.warnings)
        lines.append("")
    lines.extend([
        "Model scope: quasi-static coupled microstrip, stripline and grounded-coplanar estimates. ",
        "A bounded local 3-D quasi-static refinement is applied only to selected high-risk "
        "sections; it is not full-wave simulation.",
        "Vias and reference-plane transitions are reported as discontinuities; this is not "
        "a 3D full-wave solver.",
    ])
    return "\n".join(lines)


def format_emc_summary_lines(settings, result):
    counts = result.severity_counts
    lines = [
        "EMI / EMC Pre-compliance Results",
        "================================",
        f"Target: {settings.standard} ({settings.market})",
        f"Frequency band: {settings.frequency_start_hz / 1e6:g} .. "
        f"{settings.frequency_stop_hz / 1e6:g} MHz",
        f"Risk score: {result.risk_score}/100",
        "Risk scoring: severity weighted by evidence confidence; repeated rules capped.",
        f"Checks evaluated: {result.total_checks}",
        f"Findings: {len(result.findings)} — critical {counts.get('CRITICAL', 0)}, "
        f"high {counts.get('HIGH', 0)}, medium {counts.get('MEDIUM', 0)}, "
        f"low {counts.get('LOW', 0)}, info {counts.get('INFO', 0)}",
        f"Total elapsed time: {result.elapsed_seconds:.3f} s",
    ]
    penalties = getattr(result, "score_penalties_by_rule", {})
    lines.append(
        "Score deductions: " + ", ".join(
            f"{rule_id} -{value}" for rule_id, value in penalties.items()
        ) if penalties else "Score deductions: none."
    )
    lines.extend(["", "Configured emission sources", "---------------------------"])
    enabled_sources = [source for source in settings.sources if source.enabled]
    if not enabled_sources:
        lines.append("  - None; geometry-only analysis.")
    for source in enabled_sources:
        lines.append(
            f"  - {source.name}: {source.net_name}"
            f"{f' / {source.negative_net_name}' if source.negative_net_name else ''}, "
            f"{source.kind}, {source.frequency_hz / 1e6:g} MHz, "
            f"rise {source.rise_time_ns:g} ns swing {source.voltage_swing_v:g} V, "
            f"current {source.current_a:g} A "
            f"[{source.source}; confidence {source.parameter_confidence}]"
        )
        if source.parameter_notes:
            lines.append(f"      basis: {source.parameter_notes}")
    return lines


def format_emc_findings_lines(result):
    lines = ["", "Findings", "--------"]
    if not result.findings:
        lines.append("No finding was generated by the enabled deterministic checks.")
    for finding in result.findings:
        targets = []
        if finding.nets:
            targets.append("nets=" + ", ".join(finding.nets))
        if finding.components:
            targets.append("components=" + ", ".join(finding.components))
        lines.extend([
            f"[{finding.severity}] {finding.rule_id} — {finding.title} "
            f"(confidence {finding.confidence})",
            f"  {finding.description}",
        ])
        if targets:
            lines.append("  Evidence targets: " + "; ".join(targets))
        for evidence in finding.evidence:
            position = ""
            if evidence.x_mm is not None and evidence.y_mm is not None:
                position = f" at ({evidence.x_mm:.3f}, {evidence.y_mm:.3f}) mm"
                if evidence.layer_id is not None:
                    position += f" on layer {evidence.layer_id}"
            lines.append(f"  Evidence [{evidence.source}]{position}: {evidence.detail}")
        lines.append(f"  Recommendation: {finding.recommendation}")
    lines.extend(["", "Per-net risk scores", "-------------------"])
    if result.per_net_scores:
        for net, score in sorted(result.per_net_scores.items(), key=lambda item: (item[1], item[0])):
            lines.append(f"  - {net}: {score}/100")
    else:
        lines.append("  - No net-specific penalty.")
    lines.extend(["", "Pre-compliance test plan", "------------------------"])
    lines.extend(f"  - {item}" for item in result.test_plan)
    lines.extend(["", "Regulatory coverage", "-------------------"])
    lines.extend(f"  - {item}" for item in result.regulatory_coverage)
    lines.extend(["", "Model limitations", "-----------------"])
    lines.extend(f"  - {item}" for item in result.limitations)
    return lines


def format_emc_near_field_lines(result):
    field_result = getattr(result, "field_simulation", None)
    lines = ["", "Near-field simulation", "---------------------"]
    if field_result is None:
        lines.append("  - Disabled or unavailable for this run.")
        return lines
    mode = (
        f"selected envelope at {field_result.frequency_hz / 1e6:g} MHz"
        if field_result.frequency_hz > 0.0
        else "RSS ranking at each source's first in-band harmonic"
    )
    lines.extend([
        f"  - Observation plane: {field_result.probe_height_mm:g} mm above PCB; "
        f"grid requested {field_result.requested_grid_size_mm:g} mm, "
        f"effective {field_result.effective_grid_size_mm:g} mm.",
        f"  - Frequency mode: {mode}.",
        f"  - Model scope: {field_result.model_scope}; absolute values are not calibrated compliance levels.",
        f"  - Sources / trace elements: {field_result.source_count} / {field_result.segment_count}.",
        f"  - Maximum |E|: {field_result.maximum_e_v_m:.6g} V/m at "
        f"({field_result.maximum_e_position_mm[0]:.3f}, {field_result.maximum_e_position_mm[1]:.3f}) mm.",
        f"  - Maximum |H|: {field_result.maximum_h_a_m:.6g} A/m at "
        f"({field_result.maximum_h_position_mm[0]:.3f}, {field_result.maximum_h_position_mm[1]:.3f}) mm.",
        f"  - Backend / elapsed: {field_result.compute_backend}; {field_result.elapsed_seconds:.3f} s.",
    ])
    if field_result.source_contributions:
        lines.append("  - Dominant source contributions (individual source maxima):")
        ranked = sorted(
            field_result.source_contributions,
            key=lambda item: max(item.relative_e_pct, item.relative_h_pct), reverse=True,
        )
        for contribution in ranked:
            nets = " / ".join(contribution.net_names)
            lines.append(
                f"    * {contribution.source_name} [{nets}; {contribution.geometry_source}; "
                f"geometry confidence {contribution.geometry_confidence}]: "
                f"Emax={contribution.maximum_e_v_m:.5g} V/m "
                f"({contribution.relative_e_pct:.1f}% of combined peak), "
                f"Hmax={contribution.maximum_h_a_m:.5g} A/m "
                f"({contribution.relative_h_pct:.1f}% of combined peak), "
                f"f={contribution.analyzed_frequency_hz / 1e6:.6g} MHz "
                f"(harmonic {contribution.harmonic_number})."
            )
    if getattr(field_result, "inductor_contributions", None):
        lines.append("  - Switching-inductor magnetic contributions:")
        for contribution in field_result.inductor_contributions:
            attenuation = (
                f"{contribution.attenuation_applied_db:g} dB applied"
                if contribution.attenuation_applied_db is not None
                else "shield attenuation not quantified; 0 dB reduction applied"
            )
            lines.append(
                f"    * {contribution.ref_des} ({contribution.mpn or 'MPN unknown'}): "
                f"shield={contribution.shield_state}, model={contribution.model_level}, "
                f"ripple={contribution.ripple_current_pp_a:.5g} A p-p, "
                f"Ih={contribution.harmonic_current_peak_a:.5g} A peak at harmonic "
                f"{contribution.harmonic_number}, Hmax={contribution.maximum_h_a_m:.5g} A/m "
                f"at ({contribution.maximum_h_position_mm[0]:.3f}, "
                f"{contribution.maximum_h_position_mm[1]:.3f}) mm; {attenuation}; "
                f"parameter confidence {contribution.parameter_confidence}; "
                f"field-model confidence {contribution.model_confidence}."
            )
            lines.append(
                f"      refinement: {contribution.refinement_status}; "
                f"source: {contribution.parameter_reference or 'not documented'}"
            )
    lines.extend(f"  - Warning: {warning}" for warning in field_result.warnings)
    return lines


def format_emc_phase10_lines(settings, result):
    phase10 = getattr(result, "phase10_result", None)
    lines = ["", "Phase 10 multi-fidelity EMC", "---------------------------"]
    if phase10 is None:
        lines.append("  - Disabled or unavailable for this run.")
        return lines
    lines.extend([
        f"  - Status: {phase10.status}; elapsed {phase10.elapsed_seconds:.3f} s.",
        f"  - Artifacts: {phase10.output_directory}",
        "  - External tools:",
    ])
    for tool in phase10.tools:
        state = "READY" if tool.available else "MISSING"
        version = f"; {tool.version}" if tool.version else ""
        lines.append(f"    * {tool.name}: {state}; {tool.path or 'not found'}{version}; {tool.detail}")
    lines.append("  - Circuit excitations:")
    if not phase10.excitations:
        lines.append("    * None.")
    for excitation in phase10.excitations:
        lines.append(
            f"    * {excitation.source_name}: {excitation.status}; "
            f"Vpk={excitation.peak_voltage_v:.6g} V, Ipk={excitation.peak_current_a:.6g} A, "
            f"max dv/dt={excitation.maximum_dv_dt_v_s:.6g} V/s, "
            f"max di/dt={excitation.maximum_di_dt_a_s:.6g} A/s; {excitation.provenance}."
        )
        lines.extend(f"      note: {note}" for note in excitation.notes)
    lines.append("  - Remote Palace analyses:")
    if not getattr(phase10, "palace_runs", None):
        lines.append("    * None.")
    for palace in phase10.palace_runs:
        lines.append(
            f"    * {palace.problem_type}: {palace.status}; server={palace.server or 'not set'}; "
            f"MPI result code={palace.return_code}; elapsed={palace.elapsed_seconds:.3f} s."
        )
        lines.append(
            f"      validation={'PASS' if palace.dry_run_passed else 'NOT PASSED'}; "
            f"remote job={palace.remote_job_directory or 'not created'}; "
            f"local artifacts={palace.local_artifact_directory or 'none'}; "
            f"CSV files={len(palace.csv_files)}."
        )
        if palace.resolved_config_path:
            lines.append(f"      resolved config: {palace.resolved_config_path}")
        lines.extend(f"      warning: {warning}" for warning in palace.warnings)
    lines.append("  - Targeted full-wave regions:")
    if not phase10.regions:
        lines.append("    * No located high-risk finding selected.")
    for region in phase10.regions:
        lines.append(
            f"    * {region.name}: {region.status}; bounds={region.bounds_mm}; "
            f"estimated cells={region.estimated_cells:,}; findings="
            f"{', '.join(region.finding_ids) or 'none'}."
        )
        if region.port_net_name:
            lines.append(
                f"      port: mode={region.port_mode or 'LEGACY'}; count={region.port_count or 1}; "
                f"nets={', '.join(region.port_net_names) or region.port_net_name}; "
                f"Zleg={region.port_leg_impedance_ohm:g} ohm; "
                f"excitations={region.port_excitations or 'legacy'}; "
                f"reference layers={region.port_reference_layer_ids or 'fallback'}; "
                f"geometry={region.port_geometry_source}; confidence={region.port_confidence}."
            )
        if region.solver_iterations:
            convergence = (
                "YES" if region.solver_converged is True else
                "NO" if region.solver_converged is False else "UNKNOWN"
            )
            decay = (
                f"; measured energy decay={region.solver_energy_decay_db:.2f} dB"
                if region.solver_energy_decay_db is not None else ""
            )
            lines.append(
                f"      solver: iterations={region.solver_iterations}/"
                f"{settings.phase10.openems_max_timesteps}; actual cells="
                f"{region.solver_cells:,}; converged={convergence}{decay}."
            )
        if region.fields_extracted:
            lines.append(
                f"      latest uncalibrated field dump: Emax={region.maximum_e_v_m:.6g} V/m; "
                f"Hmax={region.maximum_h_a_m:.6g} A/m."
            )
        elif region.status.startswith("SOLVED"):
            lines.append("      fields: solver ran, but E/H maxima were not extracted.")
        lines.extend(f"      warning: {warning}" for warning in region.warnings)
    lines.append(
        "  - Virtual receiver: relative spectrum only; no regulatory margin is "
        "reported until calibrated far-field values exist."
    )
    return lines


def format_emc_spice_audit_lines(result):
    phase10 = getattr(result, "phase10_result", None)
    audits = getattr(phase10, "spice_model_audit", ()) if phase10 else ()
    lines = ["", "SPICE model coverage", "--------------------"]
    if not audits:
        lines.append("  - No component-specific SPICE model was required or audited.")
        return lines
    for audit in audits:
        lines.append(
            f"  - {audit.component_ref or '?'} {audit.mpn or '(MPN unknown)'} "
            f"[{audit.source_name}]: {audit.status}; model={audit.model_name or 'none'}; "
            f"used={'yes' if audit.used else 'no'}; fallback={audit.fallback}."
        )
        if audit.model_path:
            lines.append(f"    path: {audit.model_path}")
        if audit.catalog_status:
            lines.append(f"    catalog: {audit.catalog_status}")
        if audit.pin_mapping:
            lines.append(f"    pin mapping: {audit.pin_mapping}")
        if audit.wrapper_name:
            lines.append(f"    wrapper: {audit.wrapper_name}; {audit.wrapper_path or 'not written'}")
        lines.append(f"    ngspice compatibility: {audit.compatibility}")
        if audit.probe_log_path:
            lines.append(f"    compatibility probe: {audit.probe_log_path}")
        if audit.notes:
            lines.append(f"    note: {audit.notes}")
    missing_statuses = {"COMPONENT_NOT_IDENTIFIED", "LIBRARY_UNAVAILABLE", "MODEL_FILE_MISSING"}
    unusable_statuses = {
        "INCOMPATIBLE", "MAPPING_VERIFIED_PSPICE_ONLY",
        "MAPPING_VERIFIED_NGSPICE_PROBE_FAILED",
        "MAPPING_VERIFIED_NGSPICE_TRANSIENT_UNSTABLE",
    }
    missing = [item for item in audits if item.status in missing_statuses]
    unusable = [item for item in audits if item.status in unusable_statuses]
    lines.append(
        "  - Missing models: "
        + (", ".join(item.component_ref or item.source_name for item in missing) if missing else "none")
    )
    lines.append(
        "  - Present but unusable with the selected simulator: "
        + (", ".join(item.component_ref or item.source_name for item in unusable) if unusable else "none")
    )
    return lines


def format_emc_report(settings, result):
    lines = format_emc_summary_lines(settings, result)
    lines.extend(format_emc_near_field_lines(result))
    lines.extend(format_emc_phase10_lines(settings, result))
    lines.extend(format_emc_findings_lines(result))
    lines.extend(format_emc_spice_audit_lines(result))
    return "\n".join(lines)
