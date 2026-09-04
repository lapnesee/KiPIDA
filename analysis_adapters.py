"""Adapters from numerical-domain objects to the shared analysis contract."""

import math
from typing import Any

from analysis_contract import (
    AnalysisEvidence, AnalysisFinding, AnalysisMetric, AnalysisResult,
    AnalysisStatus, EvidenceConfidence, FindingSeverity,
    normalize_evidence_confidence,
)


def _severity(value: Any) -> FindingSeverity:
    name = str(value or "INFO").upper()
    aliases = {"ERROR": "CRITICAL", "WARNING": "MEDIUM"}
    return FindingSeverity(aliases.get(name, name))


def _confidence(value: Any) -> EvidenceConfidence:
    """Map legacy certainty levels to an explicit evidence basis.

    This is intentionally conservative.  Existing HIGH confidence is only
    treated as deterministic where an analyzer supplied concrete evidence;
    the caller can refine the basis when datasheet or measurement provenance
    becomes available.
    """
    return normalize_evidence_confidence(value)


def adapt_emc_result(settings: Any, domain_result: Any) -> AnalysisResult:
    findings = []
    for index, item in enumerate(getattr(domain_result, "findings", ()) or (), start=1):
        evidence = [
            AnalysisEvidence(
                source=str(getattr(entry, "source", "analyzer")),
                detail=str(getattr(entry, "detail", "")),
                x_mm=getattr(entry, "x_mm", None),
                y_mm=getattr(entry, "y_mm", None),
                layer=str(getattr(entry, "layer_id", "") or ""),
            )
            for entry in (getattr(item, "evidence", ()) or ())
        ]
        findings.append(AnalysisFinding(
            rule_id=str(getattr(item, "rule_id", "EMC-UNKNOWN")),
            finding_id=f"{getattr(item, 'rule_id', 'EMC-UNKNOWN')}:{index}",
            category=str(getattr(item, "category", "EMC")),
            severity=_severity(getattr(item, "severity", "INFO")),
            title=str(getattr(item, "title", "EMC finding")),
            description=str(getattr(item, "description", "")),
            recommendation=str(getattr(item, "recommendation", "")),
            confidence=_confidence(getattr(item, "confidence", "LOW")),
            nets=list(getattr(item, "nets", ()) or ()),
            components=list(getattr(item, "components", ()) or ()),
            evidence=evidence,
        ))

    significant = any(
        finding.severity in {FindingSeverity.CRITICAL, FindingSeverity.HIGH, FindingSeverity.MEDIUM}
        for finding in findings
    )
    phase10 = getattr(domain_result, "phase10_result", None)
    field_result = getattr(domain_result, "field_simulation", None)
    overall_status = AnalysisStatus.WARN if significant else AnalysisStatus.PASS
    result = AnalysisResult(
        analysis_type="EMC",
        title="EMI / EMC",
        status=overall_status,
        elapsed_seconds=float(getattr(domain_result, "elapsed_seconds", 0.0) or 0.0),
        configuration_snapshot={
            "standard": getattr(settings, "standard", ""),
            "market": getattr(settings, "market", ""),
            "frequency_start_hz": getattr(settings, "frequency_start_hz", 0.0),
            "frequency_stop_hz": getattr(settings, "frequency_stop_hz", 0.0),
            "enabled_categories": list(getattr(settings, "enabled_categories", ()) or ()),
        },
        summary={
            "risk_score": int(getattr(domain_result, "risk_score", 0)),
            "total_checks": int(getattr(domain_result, "total_checks", 0)),
            "per_net_scores": dict(getattr(domain_result, "per_net_scores", {}) or {}),
        },
        findings=findings,
        metrics=[
            AnalysisMetric(
                "risk_score", "Risk score",
                int(getattr(domain_result, "risk_score", 0)), "/100",
                overall_status.value,
            ),
            AnalysisMetric(
                "checks", "Checks executed",
                int(getattr(domain_result, "total_checks", 0)), status="INFO",
            ),
        ],
        provenance=[
            AnalysisEvidence(
                "PCB_GEOMETRY",
                "Rules and field estimates use a detached snapshot of the live KiCad board.",
                reference="KiCad IPC snapshot",
            ),
            AnalysisEvidence(
                "ENGINEERING_MODEL",
                "Risk scoring combines configured sources, deterministic geometry checks, and explicit approximations.",
                reference=str(getattr(field_result, "compute_backend", "") or "rule engine"),
            ),
        ],
        limitations=list(getattr(domain_result, "limitations", ()) or ()),
        compute_metadata={
            "near_field_backend": getattr(field_result, "compute_backend", "") if field_result else "",
            "phase10_status": getattr(phase10, "status", "NOT_RUN") if phase10 else "NOT_RUN",
            "phase10_output_directory": getattr(phase10, "output_directory", "") if phase10 else "",
            "palace_remote_status": (
                phase10.palace_runs[-1].status
                if phase10 and getattr(phase10, "palace_runs", None) else "NOT_RUN"
            ),
            "palace_remote_server": (
                phase10.palace_runs[-1].server
                if phase10 and getattr(phase10, "palace_runs", None) else ""
            ),
        },
    )
    return result.finish()


def _finding(rule_id, index, category, severity, title, description, recommendation=""):
    return AnalysisFinding(
        rule_id=rule_id,
        finding_id=f"{rule_id}:{index}",
        category=category,
        severity=severity,
        title=title,
        description=description,
        recommendation=recommendation,
        confidence=EvidenceConfidence.DETERMINISTIC,
    )


def _ac_capacitor_snapshot(settings: Any):
    capacitors = []
    for capacitor in (getattr(settings, "capacitors", ()) or ()):
        if not bool(getattr(capacitor, "enabled", True)):
            continue
        capacitors.append({
            "reference": str(getattr(capacitor, "ref_des", "")),
            "capacitance_f": float(getattr(capacitor, "capacitance_f", 0.0)),
            "esr_ohm": float(getattr(capacitor, "esr_ohm", 0.0)),
            "esl_h": float(getattr(capacitor, "esl_h", 0.0)),
            "model_source": str(getattr(capacitor, "model_source", "estimated")),
            "rail_pad_names": list(getattr(capacitor, "rail_pad_names", ()) or ()),
            "ground_pad_names": list(getattr(capacitor, "ground_pad_names", ()) or ()),
        })
    return capacitors


def _ac_sweep_snapshot(final: Any):
    frequencies = list(getattr(final, "frequencies_hz", ()) or ())
    impedance = list(getattr(final, "impedance_ohm", ()) or ())
    points = []
    for frequency, value in zip(frequencies, impedance):
        value = complex(value)
        points.append({
            "frequency_hz": float(frequency),
            "real_ohm": float(value.real),
            "imag_ohm": float(value.imag),
            "magnitude_ohm": float(abs(value)),
            "phase_deg": float(math.degrees(math.atan2(value.imag, value.real))),
        })
    return points


def _ac_validity_limitations(final: Any) -> list:
    """Say where the swept numbers stop deserving equal confidence.

    Two distinct caveats, both invisible in the metrics alone: a worst case
    pinned to the last swept point is a lower bound rather than a maximum,
    and points above the quasi-static limit come from a lumped model that no
    longer represents the structure. Neither invalidates the sweep; both
    change how it should be read.
    """
    notes = []
    if bool(getattr(final, "worst_at_sweep_edge", False)):
        stop_hz = 0.0
        frequencies = getattr(final, "frequencies_hz", ()) or ()
        if frequencies:
            stop_hz = float(frequencies[-1])
        notes.append(
            f"The worst impedance falls on the last swept point ({stop_hz:.4g} Hz): "
            "the impedance was still rising when the window closed, so this is a "
            "lower bound on the maximum. Extend the stop frequency to locate it."
        )
    accuracy = str(getattr(final, "gpu_accuracy_check", "") or "")
    if accuracy.startswith("failed"):
        # The sweep still produced trustworthy numbers -- it moved to the CPU
        # reference -- but the reader should know the GPU was rejected here.
        notes.append(f"GPU accuracy check {accuracy}.")
    limit_hz = float(getattr(final, "quasi_static_limit_hz", 0.0) or 0.0)
    beyond = int(getattr(final, "points_beyond_quasi_static", 0) or 0)
    if limit_hz > 0.0 and beyond > 0:
        notes.append(
            f"{beyond} swept point(s) lie above the quasi-static validity limit of "
            f"{limit_hz / 1e6:.1f} MHz, where the lumped mesh omits plane-cavity "
            "resonances and distributed propagation. Treat those points as "
            "indicative only."
        )
    return notes


def adapt_ac_result(domain_result: Any, optimization: Any = None, settings: Any = None) -> AnalysisResult:
    final = getattr(optimization, "optimized", None) or domain_result
    target = float(getattr(final, "target_impedance_ohm", 0.0) or 0.0)
    worst = float(getattr(final, "worst_impedance_ohm", 0.0) or 0.0)
    sweep = _ac_sweep_snapshot(final)
    failing = [point for point in sweep if target > 0.0 and point["magnitude_ohm"] > target]
    first_failure = failing[0]["frequency_hz"] if failing else 0.0
    last_failure = failing[-1]["frequency_hz"] if failing else 0.0
    exceedance_ratio = worst / target if target > 0.0 else 0.0
    capacitors = _ac_capacitor_snapshot(settings)
    estimated_components = [
        item["reference"] for item in capacitors
        if "estimat" in item["model_source"].lower()
    ]
    target_provenance = str(getattr(settings, "target_impedance_provenance", "") or "")
    target_is_derived = "derived from" in target_provenance
    findings = []
    if target <= 0.0:
        # Nothing to fail against. A rail with no configured load is not a
        # defective rail, so this stays INFO and out of the verdict rather
        # than inventing a budget to judge it by.
        rail_name = str(getattr(settings, "rail_name", ""))
        reason = target_provenance or "no rail voltage or load current is configured"
        finding = _finding(
            "AC-002", 1, "TARGET_IMPEDANCE", FindingSeverity.INFO,
            "Target impedance could not be determined",
            f"The sweep ran but no impedance target applies: {reason}. "
            f"Worst impedance is {worst:.6g} ohm at "
            f"{getattr(final, 'worst_frequency_hz', 0.0):.6g} Hz.",
            "Configure the rail's load current, or set an explicit target, to "
            "qualify this sweep.",
        )
        finding.confidence = EvidenceConfidence.DETERMINISTIC
        finding.nets = [item for item in (rail_name,) if item]
        findings.append(finding)
    elif not bool(getattr(final, "meets_target", False)):
        rail_name = str(getattr(settings, "rail_name", ""))
        ground_name = str(getattr(settings, "ground_net_name", ""))
        band = (
            f" The target is exceeded from {first_failure:.6g} to {last_failure:.6g} Hz."
            if failing else ""
        )
        at_upper_edge = bool(sweep and failing and failing[-1] is sweep[-1])
        recommendation = (
            "Validate capacitor ESL and connection inductance, then reduce the high-frequency "
            "decoupling loop area or add a suitably modelled local capacitor."
            if at_upper_edge else
            "Review the rail model, anti-resonance peaks, and decoupling placement."
        )
        # Where the target came from decides what the verdict is worth, so it
        # is stated with the number rather than left for the reader to assume.
        origin = f" Target {target_provenance}." if target_provenance else ""
        finding = _finding(
            "AC-001", 1, "TARGET_IMPEDANCE", FindingSeverity.HIGH,
            "Target impedance is not met",
            f"Worst impedance is {worst:.6g} ohm at "
            f"{getattr(final, 'worst_frequency_hz', 0.0):.6g} Hz "
            f"({exceedance_ratio:.3g}x the {target:.6g} ohm target).{band}{origin}",
            recommendation,
        )
        # A derived target rests on an assumed ripple and transient fraction,
        # so the finding is an estimate however exact the sweep itself was.
        finding.confidence = (
            EvidenceConfidence.ESTIMATED
            if (estimated_components or target_is_derived)
            else EvidenceConfidence.DETERMINISTIC
        )
        finding.nets = [item for item in (rail_name, ground_name) if item]
        finding.components = [item["reference"] for item in capacitors if item["reference"]]
        finding.evidence = [AnalysisEvidence(
            "AC_SWEEP",
            f"{len(sweep)} solved frequency points; {len(failing)} exceed the configured target.",
            reference=str(getattr(final, "compute_backend", "") or "frequency-domain solver"),
        )]
        findings.append(finding)
    recommendations = list(getattr(optimization, "recommendations", ()) or ())
    overall_status = AnalysisStatus.PASS if not findings else AnalysisStatus.WARN
    result = AnalysisResult(
        "AC", "AC Impedance",
        status=overall_status,
        configuration_snapshot={
            "rail_name": str(getattr(settings, "rail_name", "")),
            "ground_net_name": str(getattr(settings, "ground_net_name", "")),
            "frequency_start_hz": float(getattr(settings, "frequency_start_hz", 0.0) or 0.0),
            "frequency_stop_hz": float(getattr(settings, "frequency_stop_hz", 0.0) or 0.0),
            "frequency_points": int(getattr(settings, "frequency_points", len(sweep)) or len(sweep)),
            "mesh_resolution_mm": float(getattr(settings, "mesh_resolution_mm", 0.0) or 0.0),
            "target_impedance_ohm": target,
            "target_impedance_provenance": target_provenance,
            "source": {
                "reference": str(getattr(getattr(settings, "source", None), "ref_des", "")),
                "resistance_ohm": float(getattr(getattr(settings, "source", None), "resistance_ohm", 0.0) or 0.0),
                "inductance_h": float(getattr(getattr(settings, "source", None), "inductance_h", 0.0) or 0.0),
            },
            "measurement_port": {
                "reference": str(getattr(getattr(settings, "measurement_port", None), "ref_des", "")),
            },
            "capacitors": capacitors,
        },
        summary={
            "optimized": optimization is not None,
            "recommendation_count": len(recommendations),
            "enabled_capacitor_count": len(capacitors),
            "estimated_capacitor_models": estimated_components,
            "target_exceedance_ratio": exceedance_ratio,
            "target_exceeding_point_count": len(failing),
            "first_target_exceedance_hz": first_failure,
            "last_target_exceedance_hz": last_failure,
            "sweep": sweep,
        },
        findings=findings,
        metrics=[
            AnalysisMetric(
                "worst_impedance", "Worst impedance",
                float(getattr(final, "worst_impedance_ohm", 0.0)), "ohm",
                overall_status.value,
            ),
            AnalysisMetric(
                "worst_frequency", "Worst frequency",
                float(getattr(final, "worst_frequency_hz", 0.0)), "Hz", "INFO",
            ),
            AnalysisMetric(
                "target_impedance", "Target impedance",
                float(getattr(final, "target_impedance_ohm", 0.0)), "ohm", "TARGET",
            ),
            AnalysisMetric(
                "mesh_nodes", "AC mesh nodes",
                int(getattr(final, "mesh_node_count", 0)), status="INFO",
            ),
            AnalysisMetric(
                "mesh_resolution", "Effective AC mesh resolution",
                float(getattr(final, "effective_grid_size_mm", 0.0)), "mm", "INFO",
            ),
        ],
        provenance=[AnalysisEvidence(
            "PDN_MODEL",
            "The sweep uses the configured rail, source impedance, copper/via branches, and capacitor RLC models.",
            reference=str(getattr(final, "compute_backend", "") or "frequency-domain solver"),
        )],
        limitations=[
            "The AC result is an engineering PDN model; package, connector, and measurement-fixture parasitics require explicit models or measurement.",
            "The AC mesh is intentionally independent from the finer DC voltage-drop mesh; refine it only after validating runtime and convergence.",
        ] + [
            # A dropped observation point narrows what the sweep actually
            # qualifies, so it belongs in the report rather than only in a log.
            f"Observation point {item.get('ref_des') or '(unnamed)'} was excluded "
            f"from the sweep: it {item.get('reason', 'could not be measured')}."
            for item in (getattr(final, "excluded_ports", None) or ())
        ] + _ac_validity_limitations(final),
        compute_metadata={
            "backend": getattr(final, "compute_backend", ""),
            "device": getattr(final, "compute_device", ""),
            "solve_seconds": getattr(final, "compute_solve_seconds", 0.0),
            "relative_residual": getattr(final, "compute_relative_residual", 0.0),
            "mesh_node_count": getattr(final, "mesh_node_count", 0),
            "requested_grid_size_mm": getattr(final, "requested_grid_size_mm", 0.0),
            "effective_grid_size_mm": getattr(final, "effective_grid_size_mm", 0.0),
            # Which arithmetic produced these impedances, and whether it was
            # audited against a direct factorisation. Empty when no GPU ran.
            "gpu_accuracy_check": getattr(final, "gpu_accuracy_check", ""),
        },
    )
    return result.finish()


def adapt_differential_result(results: Any, stackup: Any, tolerance_pct: float) -> AnalysisResult:
    findings = []
    metrics = []
    statuses = []
    for index, item in enumerate(results or (), start=1):
        pair = item.pair
        status = str(getattr(item, "status", "NO_DATA")).upper()
        statuses.append(status)
        prefix = str(getattr(pair, "name", f"Pair {index}"))
        metrics.extend([
            AnalysisMetric(f"pair_{index}_zdiff", f"{prefix} Zdiff", float(getattr(item, "weighted_impedance_ohm", 0.0)), "ohm", status),
            AnalysisMetric(f"pair_{index}_skew", f"{prefix} skew", float(getattr(item, "estimated_skew_ps", 0.0)), "ps", str(getattr(item, "length_symmetry_status", "NO_DATA"))),
        ])
        if status != "PASS":
            severity = FindingSeverity.HIGH if status == "FAIL" else FindingSeverity.MEDIUM
            findings.append(_finding(
                "SI-DIFF-001", index, "DIFFERENTIAL", severity,
                f"{prefix}: {status.replace('_', ' ').title()}",
                "; ".join(getattr(item, "warnings", ()) or ()) or
                f"Impedance error is {getattr(item, 'error_pct', 0.0):+.2f}%.",
                "Review stackup, reference continuity, width, gap, and length matching.",
            ))
    stackup_warnings = list(getattr(stackup, "warnings", ()) or ())
    for offset, warning in enumerate(stackup_warnings, start=len(findings) + 1):
        findings.append(_finding(
            "SI-STACKUP-001", offset, "STACKUP", FindingSeverity.MEDIUM,
            "Stackup confidence limitation", str(warning), "Import a fabrication stackup.",
        ))
    if not results:
        overall = AnalysisStatus.NO_DATA
    elif any(status == "FAIL" for status in statuses):
        overall = AnalysisStatus.FAIL
    elif findings:
        overall = AnalysisStatus.WARN
    else:
        overall = AnalysisStatus.PASS
    return AnalysisResult(
        "DIFFERENTIAL", "Differential Pairs", status=overall,
        summary={"pair_count": len(results or ()), "target_tolerance_pct": float(tolerance_pct)},
        findings=findings, metrics=metrics,
        provenance=[AnalysisEvidence(
            "stackup", str(getattr(stackup, "source", "unknown")),
            reference="trusted" if getattr(stackup, "trustworthy", False) else "estimate",
        )],
        limitations=["Via and connector-launch discontinuities require 3-D or measured validation."],
    ).finish()


def _thermal_grid_limitations(domain_result: Any) -> list:
    """Say so when the mesh was coarsened below the requested resolution.

    The mesher silently rescales the grid to fit its node budget. Until now
    that appeared only in the run log, so a report read later showed a hotspot
    computed at 0.089 mm to someone who had asked for 0.05 mm, with nothing
    marking the difference. Mesh resolution moved this board's hotspot by
    7.4 C between 0.5 and 0.1 mm, so it is not a detail.
    """
    if not bool(getattr(domain_result, "adaptive_grid", False)):
        return []
    requested = float(getattr(domain_result, "requested_grid_size_mm", 0.0) or 0.0)
    effective = float(getattr(domain_result, "effective_grid_size_mm", 0.0) or 0.0)
    if requested <= 0.0 or effective <= 0.0 or effective <= requested:
        return []
    nodes = int(getattr(domain_result, "mesh_node_count", 0) or 0)
    return [
        f"The thermal mesh was coarsened from the requested {requested:g} mm to "
        f"{effective:g} mm ({nodes:,} nodes) to fit the node budget. Temperatures "
        "are those of the coarser mesh; raise the memory ceiling in Runtime "
        "settings to honour the requested resolution."
    ]


def adapt_thermal_result(domain_result: Any, coupled: bool = False, elapsed_seconds: float = 0.0) -> AnalysisResult:
    findings = []
    components = list(getattr(domain_result, "component_results", ()) or ())
    for index, component in enumerate(components, start=1):
        if float(getattr(component, "margin_c", 0.0)) < 0:
            findings.append(_finding(
                "TH-001", index, "JUNCTION_TEMPERATURE", FindingSeverity.CRITICAL,
                f"{component.ref_des} exceeds its junction limit",
                f"Estimated Tj is {component.junction_temperature_c:.3f} C with "
                f"{component.margin_c:.3f} C margin.",
                "Reduce dissipation or improve the component thermal path.",
            ))
    if not bool(getattr(domain_result, "converged", True)):
        findings.append(_finding(
            "TH-002", 1, "NUMERICS", FindingSeverity.HIGH,
            "Thermal solver did not converge", "The iteration limit was reached.",
            "Review mesh resolution and convergence settings.",
        ))
    balance = float(getattr(domain_result, "energy_balance_error_pct", 0.0))
    if abs(balance) > 5.0:
        findings.append(_finding(
            "TH-003", 1, "ENERGY_BALANCE", FindingSeverity.MEDIUM,
            "Thermal energy balance is weak", f"Energy balance error is {balance:.3f}%.",
            "Refine the mesh or boundary conditions before relying on hotspots.",
        ))
    hotspot = getattr(domain_result, "hotspot", None)
    status = AnalysisStatus.FAIL if any(item.severity == FindingSeverity.CRITICAL for item in findings) else (
        AnalysisStatus.WARN if findings else AnalysisStatus.PASS
    )
    return AnalysisResult(
        "THERMAL", "3D Thermal", status=status,
        elapsed_seconds=float(elapsed_seconds or 0.0),
        summary={"coupled": bool(coupled), "component_count": len(components)},
        findings=findings,
        metrics=[
            AnalysisMetric(
                "hotspot", "Hotspot",
                float(getattr(hotspot, "temperature_c", 0.0)), "°C", status.value,
            ),
            AnalysisMetric(
                "input_heat", "Input heat",
                float(getattr(domain_result, "total_input_power_w", 0.0)), "W", "INFO",
            ),
            AnalysisMetric(
                "energy_balance", "Energy balance error", balance, "%",
                "PASS" if abs(balance) <= 5.0 else "WARN",
            ),
        ],
        provenance=[
            AnalysisEvidence(
                "PCB_GEOMETRY",
                "The thermal mesh uses a detached snapshot of board copper, stackup, outline, and component geometry.",
                reference="KiCad IPC snapshot",
            ),
            AnalysisEvidence(
                "POWER_MODEL",
                "Heat sources use configured component losses and optional DC copper losses.",
                reference="coupled DC" if coupled else "thermal inputs",
            ),
        ],
        compute_metadata={
            "backend": getattr(domain_result, "compute_backend", ""),
            "device": getattr(domain_result, "compute_device", ""),
            "relative_residual": getattr(domain_result, "compute_relative_residual", 0.0),
        },
        limitations=[
            "Compact thermal models require datasheet and measurement validation.",
        ] + _thermal_grid_limitations(domain_result),
    ).finish()


def adapt_cfd_result(mesh: Any, domain_result: Any) -> AnalysisResult:
    findings = []
    if not bool(getattr(domain_result, "converged", False)):
        findings.append(_finding(
            "CFD-001", 1, "NUMERICS", FindingSeverity.HIGH,
            "CFD solver did not converge", "The iteration limit was reached.",
            "Review the mesh, relaxation, and boundary conditions.",
        ))
    for index, (key, label) in enumerate((("mass_balance_error_pct", "Mass"), ("energy_balance_error_pct", "Energy")), start=1):
        value = float(getattr(domain_result, key, 0.0))
        if abs(value) > 5.0:
            findings.append(_finding(
                "CFD-002", index, "CONSERVATION", FindingSeverity.MEDIUM,
                f"{label} balance is weak", f"{label} balance error is {value:.3f}%.",
                "Refine the mesh or boundary conditions.",
            ))
    overall_status = AnalysisStatus.WARN if findings else AnalysisStatus.PASS
    return AnalysisResult(
        "CFD", "Enclosure CFD",
        status=overall_status,
        summary={"cell_count": int(getattr(mesh, "cell_count", 0)), "iterations": int(getattr(domain_result, "iterations", 0))},
        findings=findings,
        metrics=[
            AnalysisMetric("maximum_velocity", "Maximum velocity", float(getattr(domain_result, "maximum_velocity_m_s", 0.0)), "m/s", overall_status.value),
            AnalysisMetric("maximum_air_temperature", "Maximum air temperature", float(getattr(domain_result, "maximum_air_temperature_c", 0.0)), "°C", overall_status.value),
            AnalysisMetric("maximum_solid_temperature", "Maximum solid temperature", float(getattr(domain_result, "maximum_solid_temperature_c", 0.0)), "°C", overall_status.value),
        ],
        provenance=[
            AnalysisEvidence(
                "ENCLOSURE_MODEL",
                "The mesh uses the configured enclosure, PCB placement, and boundary patches.",
                reference="structured CFD mesh",
            ),
            AnalysisEvidence(
                "POWER_MODEL",
                "Solid heat sources use configured component and optional DC copper losses.",
                reference="thermal inputs",
            ),
        ],
        compute_metadata={"backend": getattr(domain_result, "compute_backend", ""), "device": getattr(domain_result, "compute_device", "")},
        limitations=["Laminar steady-state model; turbulence, fan blades, radiation, and transients are excluded."],
    ).finish()


def adapt_dc_result(system_results: Any, maximum_drop_pct: float) -> AnalysisResult:
    findings = []
    metrics = []
    density_hotspots = {}
    for index, (rail, data) in enumerate((system_results or {}).items(), start=1):
        vmin, vmax, drop = data.get("stats", (0.0, 0.0, 0.0))
        drop_pct = (float(drop) / float(vmax) * 100.0) if vmax else 0.0
        metrics.append(AnalysisMetric(
            f"rail_{index}_drop", f"{rail} voltage drop", drop_pct, "%",
            "WARN" if drop_pct > float(maximum_drop_pct) else "PASS",
        ))
        density = data.get("current_density")
        if density is not None:
            metrics.extend([
                AnalysisMetric(
                    f"rail_{index}_maximum_planar_current_density",
                    f"{rail} maximum planar current density",
                    float(density.maximum_planar_a_per_mm2), "A/mm²", "ESTIMATED",
                ),
                AnalysisMetric(
                    f"rail_{index}_planar_current_density_p99_5",
                    f"{rail} planar current density P99.5",
                    float(density.percentile_99_5_a_per_mm2), "A/mm²", "ESTIMATED",
                ),
                AnalysisMetric(
                    f"rail_{index}_maximum_track_current_density",
                    f"{rail} maximum route current density",
                    float(density.maximum_track_a_per_mm2), "A/mm²", "ESTIMATED",
                ),
                AnalysisMetric(
                    f"rail_{index}_maximum_zone_current_density",
                    f"{rail} maximum zone current density",
                    float(density.maximum_zone_a_per_mm2), "A/mm²", "ESTIMATED",
                ),
                AnalysisMetric(
                    f"rail_{index}_maximum_via_current",
                    f"{rail} maximum via current",
                    float(density.maximum_via_current_a), "A", "ESTIMATED",
                ),
                AnalysisMetric(
                    f"rail_{index}_maximum_via_current_density",
                    f"{rail} maximum via barrel current density",
                    float(density.maximum_via_a_per_mm2), "A/mm²", "ESTIMATED",
                ),
            ])
            hotspot = getattr(density, "planar_hotspot", None)
            if hotspot is not None:
                density_hotspots[str(rail)] = {
                    "density_a_per_mm2": float(hotspot.density_a_per_mm2),
                    "x_mm": float(hotspot.x_mm), "y_mm": float(hotspot.y_mm),
                    "layer_id": int(hotspot.layer_id),
                    "layer_name": str(getattr(hotspot, "layer_name", hotspot.layer_id)),
                    "copper_kind": str(hotspot.copper_kind),
                }
        detailed = data.get("detailed_result")
        compute = data.get("compute_metadata")
        if detailed is not None and not getattr(detailed, "valid", True):
            findings.append(_finding(
                "DC-001", index, "CONNECTIVITY", FindingSeverity.HIGH,
                f"{rail} contains source-free loads",
                f"{getattr(detailed, 'excluded_load_node_count', 0)} load node(s) were excluded.",
                "Repair copper connectivity before using this rail result.",
            ))
        if compute is not None and not getattr(compute, "converged", True):
            findings.append(_finding(
                "DC-002", index, "NUMERICS", FindingSeverity.HIGH,
                f"{rail} solve did not converge", "The DC linear solve did not converge.",
                "Review connectivity, mesh resolution, and solver diagnostics.",
            ))
        if drop_pct > float(maximum_drop_pct):
            findings.append(_finding(
                "DC-003", index, "VOLTAGE_DROP", FindingSeverity.HIGH,
                f"{rail} exceeds the voltage-drop target",
                f"Calculated drop is {drop_pct:.3f}% versus {maximum_drop_pct:.3f}% allowed.",
                "Increase copper cross-section or reduce path/load resistance.",
            ))
    return AnalysisResult(
        "DC", "DC Power",
        status=AnalysisStatus.NO_DATA if not system_results else (AnalysisStatus.WARN if findings else AnalysisStatus.PASS),
        summary={
            "rail_count": len(system_results or {}),
            "maximum_drop_pct": float(maximum_drop_pct),
            "current_density_hotspots": density_hotspots,
        },
        findings=findings, metrics=metrics,
        provenance=[
            AnalysisEvidence(
                "PCB_GEOMETRY",
                "The resistive mesh uses a detached snapshot of live-board copper, pads, vias, and zones.",
                reference="KiCad IPC snapshot",
            ),
            AnalysisEvidence(
                "LOAD_MODEL",
                "Rail sources, loads, regulator relationships, and explicit parasitics come from the saved project configuration.",
                reference="project configuration",
            ),
            AnalysisEvidence(
                "DC_SOLVER",
                "Current-density metrics use solved branch currents directly; no voltage-gradient reconstruction is used.",
                reference="DCSolveResult.branch_currents_a",
            ),
        ],
        limitations=[
            "Connector and contact resistances are included only when explicitly modeled.",
            "Current density is a DC mesh diagnostic, not an IPC ampacity certification; AC skin/proximity effects, contact details, and measured copper temperature are excluded.",
        ],
    ).finish()
