"""Adapters from numerical-domain objects to the shared analysis contract."""

import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from analysis_contract import (
    AnalysisEvidence, AnalysisFinding, AnalysisMetric, AnalysisResult,
    AnalysisStatus, EvidenceConfidence, FindingSeverity,
    normalize_evidence_confidence,
)
from cfd_model import enclosure_air_rise_k
from physical_constants import (
    COPPER_RESISTIVITY_OHM_M, COPPER_RESISTIVITY_REFERENCE_C, DEFAULT_BOARD_THICKNESS_MM,
    PLATING_THICKNESS_FALLBACK_MM,
)

# The drill a thermal-via recommendation is sized for: 0.3 mm, the common
# small via (P02 Rev E's own vias are 0.3 and 0.4 mm).
THERMAL_VIA_DRILL_MM = 0.3
from raster_fidelity import DISCLOSE_BELOW_STEPS, track_limitation, width_in_steps


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
            # The campaign takes this as the EMC domain score rather than
            # re-scoring the findings on its own scale.
            "domain_score": int(getattr(domain_result, "risk_score", 0)),
            "total_checks": int(getattr(domain_result, "total_checks", 0)),
            "per_net_scores": dict(getattr(domain_result, "per_net_scores", {}) or {}),
        },
        findings=findings,
        metrics=[
            AnalysisMetric(
                "risk_score", "Relative risk score (uncalibrated)",
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


def _finding(rule_id, index, category, severity, title, description, recommendation="",
             confidence=EvidenceConfidence.ESTIMATED):
    """Build a finding, stating what its confidence actually rests on.

    This hardcoded DETERMINISTIC for every caller, so a report could print
    "J2_ESD_D: Estimate" carrying a DETERMINISTIC badge -- the deliverable
    contradicting itself on the project's central rule, that an estimate is
    never presented as a measurement.

    The default is now ESTIMATED, because most findings here are the verdict
    of a numerical model: a solved voltage drop, a swept impedance, a meshed
    temperature. DETERMINISTIC is passed explicitly by the callers whose
    finding is a fact about the run rather than a model output -- a solver
    that did not converge, or an island with no source, are observed, not
    estimated.
    """
    return AnalysisFinding(
        rule_id=rule_id,
        finding_id=f"{rule_id}:{index}",
        category=category,
        severity=severity,
        title=title,
        description=description,
        recommendation=recommendation,
        confidence=confidence,
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


def _raster_track_metrics(key_prefix: str, label_prefix: str, summary: Any, step_mm: float) -> list:
    """The narrowest track in grid steps and the oblique count, as metrics.

    The raster mesh's track error follows those two numbers (see
    raster_fidelity), so a result publishes them next to its step.
    """
    summary = dict(summary or {})
    if not summary.get("track_count") or step_mm <= 0.0:
        return []
    finest = float(summary.get("finest_width_mm", 0.0) or 0.0)
    return [
        AnalysisMetric(
            f"{key_prefix}narrowest_track_steps", f"{label_prefix} narrowest track in grid steps".strip(),
            width_in_steps(finest, step_mm), "steps",
            "WARN" if width_in_steps(finest, step_mm) < DISCLOSE_BELOW_STEPS else "INFO",
        ),
        AnalysisMetric(
            f"{key_prefix}oblique_tracks", f"{label_prefix} oblique tracks".strip(),
            int(summary.get("oblique_count", 0) or 0), status="INFO",
        ),
    ]


def _raster_track_limitations(summary: Any, step_mm: float, mesh_name: str) -> list:
    summary = dict(summary or {})
    if not summary.get("track_count"):
        return []
    text = track_limitation(
        float(summary.get("finest_width_mm", 0.0) or 0.0), step_mm,
        int(summary.get("oblique_count", 0) or 0), mesh_name=mesh_name,
    )
    return [text] if text else []


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


def _ac_source_is_default(settings: Any) -> bool:
    """True when the AC source still carries the untouched 10 mOhm + 1 nH
    default (models.ACSourceModel), which has no board evidence behind it.

    scratchpad/audit/ac/source_lumped.py measured this default hiding the
    source/bulk anti-resonance of P02 Rev E's 7-capacitor bank entirely
    (max|Z| 12 mOhm below 10 MHz), while a USB-C cable both reveals it and
    moves it close to the target (362 mOhm, 92% of 0.39226 ohm, at 21 kHz).
    """
    source = getattr(settings, "source", None)
    resistance_ohm = float(getattr(source, "resistance_ohm", 0.0) or 0.0)
    inductance_h = float(getattr(source, "inductance_h", 0.0) or 0.0)
    return math.isclose(resistance_ohm, 0.01, rel_tol=1e-6) and math.isclose(
        inductance_h, 1.0e-9, rel_tol=1e-6,
    )


def _ac_source_limitations(settings: Any) -> list:
    """State the source impedance as the dominant, and possibly unverified,
    assumption behind the sweep (AC-2)."""
    source = getattr(settings, "source", None)
    resistance_ohm = float(getattr(source, "resistance_ohm", 0.0) or 0.0)
    inductance_h = float(getattr(source, "inductance_h", 0.0) or 0.0)
    if _ac_source_is_default(settings):
        return [
            f"The source impedance ({resistance_ohm * 1e3:.3g} mOhm + "
            f"{inductance_h * 1e9:.3g} nH) is the untouched default with no board "
            "evidence behind it, and it dominates the swept impedance below a few "
            "MHz: a real USB-C cable or regulator output can move the source/bulk "
            "anti-resonance to within 92% of a typical target. Pick a source preset "
            "(USB-C cable/connector or regulator output) that matches what actually "
            "feeds this rail, or measure it; treat this sweep's low-frequency region "
            "as source-dependent and lower confidence until then."
        ]
    return [
        f"The source impedance ({resistance_ohm * 1e3:.3g} mOhm + "
        f"{inductance_h * 1e9:.3g} nH) dominates the swept impedance below a few "
        "MHz and remains a modelled R + jwL stamp: it has no explicit control-loop "
        "bandwidth roll-off, so a regulator's real output impedance may differ "
        "above its bandwidth."
    ]


def adapt_ac_result(
    domain_result: Any, optimization: Any = None, settings: Any = None,
) -> AnalysisResult:
    final = getattr(optimization, "optimized", None) or domain_result
    target = float(getattr(final, "target_impedance_ohm", 0.0) or 0.0)
    worst = float(getattr(final, "worst_impedance_ohm", 0.0) or 0.0)
    # An unverified default source hides the source/bulk anti-resonance
    # (AC-2), so a verdict that rests on it is worth less than one where the
    # source was actually characterised.
    source_default = _ac_source_is_default(settings)
    verdict_confidence = EvidenceConfidence.HEURISTIC if source_default else EvidenceConfidence.ESTIMATED
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
    elif (
        bool(getattr(final, "meets_target", False))
        and bool(getattr(final, "worst_at_sweep_edge", False))
        and float(getattr(final, "extrapolated_crossing_hz", 0.0) or 0.0) > 0.0
        and float(getattr(final, "extrapolated_crossing_hz", 0.0))
        < float(getattr(final, "quasi_static_limit_hz", 0.0) or float("inf"))
    ):
        # The swept points all pass, but the worst one sits on the closed
        # edge of the window and was still rising: linearly extrapolating
        # that rise crosses the target before the model's own quasi-static
        # limit, so the PASS is not established (AC-1).
        rail_name = str(getattr(settings, "rail_name", ""))
        crossing_hz = float(getattr(final, "extrapolated_crossing_hz", 0.0))
        limit_hz = float(getattr(final, "quasi_static_limit_hz", 0.0) or 0.0)
        stop_hz = float(getattr(final, "worst_frequency_hz", 0.0) or 0.0)
        finding = _finding(
            "AC-003", 1, "TARGET_IMPEDANCE", FindingSeverity.HIGH,
            "PASS is undetermined: the worst point is still rising at the sweep edge",
            f"Worst impedance {worst:.6g} ohm sits on the last swept point "
            f"({stop_hz:.6g} Hz) and was still rising there. Extrapolating that "
            f"slope linearly, the {target:.6g} ohm target would be crossed near "
            f"{crossing_hz / 1e6:.3g} MHz, inside the model's own quasi-static "
            f"domain (limit {limit_hz / 1e6:.3g} MHz): the PASS the swept points "
            "show is a lower bound, not a verdict.",
            "Extend the sweep's stop frequency toward the quasi-static limit and "
            "re-run before trusting a PASS.",
        )
        finding.confidence = verdict_confidence
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
        # The verdict of a lumped PDN model, which omits each capacitor's
        # mounting inductance and DC-bias derating -- both of which push the
        # real impedance above the swept one at high frequency. However exact
        # the target and the capacitor data, this is an estimate. It used to
        # read DETERMINISTIC whenever the target was explicit. HEURISTIC
        # instead of ESTIMATED when the source impedance is still the
        # untouched, unverified default (AC-2).
        finding.confidence = verdict_confidence
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
        ] + _raster_track_metrics(
            "", "AC", getattr(final, "track_summary", None),
            float(getattr(final, "effective_grid_size_mm", 0.0) or 0.0),
        ),
        provenance=[AnalysisEvidence(
            "PDN_MODEL",
            "The sweep uses the configured rail, source impedance, copper/via branches, and capacitor RLC models.",
            reference=str(getattr(final, "compute_backend", "") or "frequency-domain solver"),
        )],
        limitations=[
            "The AC result is an engineering PDN model; package, connector, and measurement-fixture parasitics require explicit models or measurement.",
            "MLCC DC-bias derating uses a single generic retention curve (ac_model.dc_bias_retention), not a per-part datasheet or case-size table (see AC-7), which can leave the real impedance above the swept one.",
            "A via barrel with a barrel of the other net within 5 mm carries half of the pair's two-wire loop inductance, mu0*h/pi * ln(s/r); one with no return barrel that close keeps a self-inductance estimate of about 1 nH per mm of span, which overstates a loop that returns through the planes. A capacitor's mounting inductance, its pad-to-via connection, is not modelled apart from its footprint's ESL.",
            "The AC mesh is intentionally independent from the finer DC voltage-drop mesh; refine it only after validating runtime and convergence.",
            (
                "The AC network is built on the hybrid mesh of the saved board file: tracks and via barrels carry their analytic resistance and pours a cut-cell lattice. A track's loop inductance is taken as mu0*h/2 per square like a pour's, which ignores its fringing field, and only pours carry cavity capacitance."
                if getattr(final, "mesh_engine", "raster") == "hybrid" else
                "The AC network is built on the raster mesh of the open board, the saved board file being unknown, unreadable or different from it (the run log says which)."
            ),
            "Plane-pair (cavity) capacitance is stamped as C = eps0*er*A/h between each rail cell and the return copper on the directly adjacent layer; a return reached through another copper layer is not given one.",
        ] + [
            # A dropped observation point narrows what the sweep actually
            # qualifies, so it belongs in the report rather than only in a log.
            f"Observation point {item.get('ref_des') or '(unnamed)'} was excluded "
            f"from the sweep: it {item.get('reason', 'could not be measured')}."
            for item in (getattr(final, "excluded_ports", None) or ())
        ] + _ac_source_limitations(settings) + _ac_validity_limitations(final) + _raster_track_limitations(
            getattr(final, "track_summary", None),
            float(getattr(final, "effective_grid_size_mm", 0.0) or 0.0), "AC mesh",
        ),
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


_STATUS_RANK = {"NO_DATA": 0, "PASS": 1, "WARN": 2, "FAIL": 3}


def adapt_ac_batch(items: Any, skipped: Any = ()) -> AnalysisResult:
    """One AC result over every profile a campaign swept.

    *items* holds ``(sweep result, optimization, settings)`` per rail and
    *skipped* ``(rail, reason)`` per configured profile that was not swept.
    Each rail is adapted on its own and the results joined: findings
    renumbered, metrics named after their rail, the worst status kept. A
    campaign used to sweep only the rail on screen (AC-10).
    """
    parts = [
        (str(getattr(settings, "rail_name", "") or f"rail {index}"),
         adapt_ac_result(result, optimization, settings))
        for index, (result, optimization, settings) in enumerate(items, start=1)
    ]
    findings, metrics, limitations, provenance = [], [], [], []
    numbering = defaultdict(int)
    for rail, part in parts:
        for finding in part.findings:
            numbering[finding.rule_id] += 1
            finding.finding_id = f"{finding.rule_id}:{numbering[finding.rule_id]}"
            findings.append(finding)
        for metric in part.metrics:
            metric.key = f"{rail}:{metric.key}"
            metric.label = f"{rail} {metric.label}"
            metrics.append(metric)
        limitations += [item for item in part.limitations if item not in limitations]
        provenance += [item for item in part.provenance if item not in provenance]
    skipped = list(skipped or ())
    if skipped:
        # A rail left out is a fact about the run, not a model verdict, so it
        # is DETERMINISTIC; LOW keeps it from drowning a real AC-001/AC-003
        # finding while still moving the score off 100.
        finding = _finding(
            "AC-004", 1, "TARGET_IMPEDANCE", FindingSeverity.LOW,
            "This campaign covers only part of the project's AC profiles",
            f"{len(parts)} of {len(parts) + len(skipped)} configured AC profile(s) "
            "were swept; not swept: "
            + "; ".join(f"'{rail}' ({reason})" for rail, reason in skipped)
            + ". Their PDN impedance is not scored or checked by this run.",
            "Fix what kept those profiles from being swept and re-run the campaign.",
        )
        finding.confidence = EvidenceConfidence.DETERMINISTIC
        finding.nets = [rail for rail, _reason in skipped if rail]
        findings.append(finding)
    statuses = [part.status for _rail, part in parts] + (
        [AnalysisStatus.WARN] if skipped else []
    )
    result = AnalysisResult(
        "AC", "AC Impedance",
        status=max(statuses, key=lambda status: _STATUS_RANK.get(status.value, 0))
        if statuses else AnalysisStatus.NO_DATA,
        configuration_snapshot={"rails": [part.configuration_snapshot for _rail, part in parts]},
        summary={"rails": {rail: part.summary for rail, part in parts}},
        findings=findings, metrics=metrics, provenance=provenance, limitations=limitations,
        compute_metadata={"rails": {rail: part.compute_metadata for rail, part in parts}},
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
            AnalysisMetric(f"pair_{index}_qualified", f"{prefix} length with a qualified reference", float(getattr(item, "qualified_length_pct", 0.0) or 0.0), "%", "INFO"),
        ])
        if status != "PASS":
            severity = FindingSeverity.HIGH if status == "FAIL" else FindingSeverity.MEDIUM
            # The pair's impedance against its target leads, and the geometry
            # the recommender solved for is the recommendation: on P02 Rev E
            # the finding gave the refinement's notes alone and a generic
            # "review width and gap", while 0.155 mm was computed for 90 ohm.
            impedance = float(getattr(item, "weighted_impedance_ohm", 0.0) or 0.0)
            target = float(getattr(pair, "target_impedance_ohm", 0.0) or 0.0)
            lead = (
                f"Zdiff {impedance:.1f} ohm against a {target:g} ohm target "
                f"({getattr(item, 'error_pct', 0.0):+.1f}%)."
                if impedance > 0.0 and target > 0.0 else ""
            )
            notes = "; ".join(getattr(item, "warnings", ()) or ())
            primary = next((
                rec for rec in (getattr(item, "recommendations", ()) or ())
                if getattr(rec, "action", "") == "SET_PRIMARY_ROUTE"
                and float(getattr(rec, "recommended_width_mm", 0.0) or 0.0) > 0.0
            ), None)
            finding = _finding(
                "SI-DIFF-001", index, "DIFFERENTIAL", severity,
                f"{prefix}: {status.replace('_', ' ').title()}",
                " ".join(part for part in (lead, notes) if part) or
                f"Impedance error is {getattr(item, 'error_pct', 0.0):+.2f}%.",
                (
                    f"Route the pair {primary.recommended_width_mm:.3f} mm wide with a "
                    f"{primary.recommended_gap_mm:.3f} mm gap on {primary.layer_name}: the model "
                    f"predicts {primary.predicted_impedance_ohm:.1f} ohm. Then check reference "
                    "continuity and length matching."
                ) if primary is not None else
                "Review stackup, reference continuity, width, gap, and length matching.",
            )
            # Each pair is its own defect: without its nets, the five failing
            # P02 Rev E pairs shared one identity, so fixing four of them
            # compared as "resolved 0" (COH-7).
            finding.nets = [
                net for net in (getattr(pair, "positive_net", ""), getattr(pair, "negative_net", ""))
                if net
            ]
            findings.append(finding)
    stackup_warnings = list(getattr(stackup, "warnings", ()) or ())
    for offset, warning in enumerate(stackup_warnings, start=len(findings) + 1):
        findings.append(_finding(
            "SI-STACKUP-001", offset, "STACKUP", FindingSeverity.MEDIUM,
            "Stackup confidence limitation", str(warning), "Import a fabrication stackup.",
        ))
    metrics.append(AnalysisMetric(
        "target_tolerance", "Impedance verdict tolerance", float(tolerance_pct), "+/- %", "INFO",
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
        limitations=[
            "Via and connector-launch discontinuities require 3-D or measured validation.",
            f"A pair passes within +/-{float(tolerance_pct):g} % of its target, the "
            "project's setting; interface standards differ (USB 2.0 allows +/-15 %).",
            "Traces are modelled with a rectangular cross-section; a trapezoidal etch "
            "(narrower top) typically raises Zdiff by 1-3 %.",
            f"A pair is judged on its routed length with a qualified reference plane; "
            f"below {90:g} % of the length it is reported as an estimate.",
        ],
    ).finish()


def _same_file(first: Any, second: Any) -> bool:
    if not first or not second:
        return False
    try:
        return Path(str(first)).resolve() == Path(str(second)).resolve()
    except (OSError, ValueError):
        return False


def attach_dc_remediations(result: Any, board_path: Any, rails: Any,
                           maximum_drop_pct: float, *, verify: bool = False,
                           log_callback=None, system_results: Any = None) -> int:
    """Give each voltage-drop finding a sized, quantified fix.

    The DC advisor ranks branches by R*I^2, sizes the widening the dominant
    segments need, and can re-simulate to check the prediction. It was written
    and tested and then never called, so every action in the consolidated
    report read "No structured remediation was computed". This is the missing
    call.

    ``verify=False`` by default: verification re-meshes and re-solves the net,
    which is worth seconds per rail and belongs to a deliberate request rather
    than to every DC run. Unverified remediations are marked as first-order
    estimates by the advisor itself.

    Returns the number of findings given a remediation. Never raises -- an
    advisor failure must not cost the user their DC result.
    """
    if not board_path or not rails:
        return 0
    try:
        from pathlib import Path

        from advisor.dc_advisor import build_dc_remediations, build_net_mesh
        from advisor.rail_sources import (
            map_pads_to_nodes, resolve_rail_source, solver_loads,
            unreachable_nodes,
        )
        from ingest.board_reader import read_board
    except ImportError:
        return 0

    def _log(message: str) -> None:
        if log_callback is not None:
            log_callback(f"[remediation] {message}")

    try:
        board = read_board(Path(board_path))
    except Exception as exc:
        _log(f"Board could not be read for remediation sizing: {exc}")
        return 0

    # The advisor re-meshes internally with these same parameters, and node ids
    # are stable for a given (board, net, grid) per dc_advisor's docstring, so
    # ids resolved here address the mesh it will solve.
    #
    # The rail is meshed alone. merge_meshes exists for a rail+return loop, but
    # build_dc_remediations takes flat source/load lists that carry no pairing
    # between a rail node and its return, so adding the ground net would attach
    # a floating island rather than a return path -- slower and noisier for no
    # gain in accuracy.
    grid_step_mm = 0.1
    rails_by_net = {str(getattr(rail, "net_name", "")): rail for rail in rails}
    attached = 0
    for finding in getattr(result, "findings", ()):
        if finding.rule_id != "DC-003":
            continue
        rail = next(
            (rails_by_net[net] for net in finding.nets if net in rails_by_net), None,
        )
        if rail is None:
            continue
        nominal = float(getattr(rail, "nominal_voltage", 0.0) or 0.0)
        if nominal <= 0.0:
            _log(
                f"{rail.net_name} has no nominal voltage, so there is no drop "
                "target to size against; skipped."
            )
            continue

        solved = (system_results or {}).get(rail.net_name) or {}
        if solved.get("engine") == "hybrid" and _same_file(solved.get("board_path"), board_path):
            # The DC result was solved on this very file with the hybrid mesh
            # the advisor builds, so its node ids address the advisor's mesh.
            # Sizing against the result's own sources, loads and step is what
            # makes the fix answer the drop DC-003 reported: every declared
            # source and regulator output, every load and regulator input,
            # rather than one resolved source and the declared loads alone.
            sources = list(solved.get("sources") or ())
            loads = list(solved.get("loads") or ())
            step = float(solved.get("requested_grid_size_mm") or solved.get("grid_size_mm") or grid_step_mm)
            detailed = solved.get("detailed_result")
            if not sources or not loads:
                _log(f"{rail.net_name} skipped: the DC result holds no source or no load to size against.")
                continue
            if detailed is not None and not getattr(detailed, "valid", True):
                _log(
                    f"{rail.net_name} skipped: {getattr(detailed, 'excluded_load_node_count', 0)} "
                    "load node(s) are not connected to a source in the solved copper, so any "
                    "drop computed would be fictitious."
                )
                continue
            reference_v = float(solved.get("source_voltage_v") or 0.0) or nominal
            _log(
                f"{rail.net_name} sized on the DC result's own mesh ({step:g} mm): "
                f"{len(sources)} source node(s), {len(loads)} load node(s)."
            )
            try:
                remediations = build_dc_remediations(
                    board, rail.net_name, "", sources=sources, loads=loads,
                    target_drop_v=reference_v * float(maximum_drop_pct) / 100.0,
                    grid_step_mm=step, verify=verify, log_callback=log_callback,
                )
            except Exception as exc:
                _log(f"Remediation sizing failed for {rail.net_name}: {exc}")
                continue
            if remediations:
                finding.remediations.extend(remediations)
                attached += 1
            continue

        source_ref, source_pads, rule = resolve_rail_source(board, rail, rails)
        if source_ref is None:
            _log(f"{rail.net_name} skipped: {rule}.")
            continue

        try:
            mesh = build_net_mesh(board, rail.net_name, "", grid_step_mm=grid_step_mm)
        except Exception as exc:
            _log(f"{rail.net_name} could not be meshed for sizing: {exc}")
            continue
        if not mesh.nodes:
            _log(f"{rail.net_name} has no meshable copper; skipped.")
            continue

        source_nodes, unlocated = map_pads_to_nodes(
            mesh, board, source_ref, source_pads,
            net_name=rail.net_name, grid_step_mm=grid_step_mm,
        )
        if not source_nodes:
            _log(
                f"{rail.net_name} skipped: source {source_ref} ({rule}) has no "
                "pad on the meshed copper"
                + (f"; unlocated pad(s) {', '.join(unlocated)}" if unlocated else "")
                + "."
            )
            continue
        _log(
            f"{rail.net_name} source resolved to {source_ref} via {rule} "
            f"({len(source_nodes)} node(s))."
        )

        loads = solver_loads(
            mesh, board, rail, grid_step_mm=grid_step_mm, log_callback=_log,
        )
        if not loads:
            _log(
                f"{rail.net_name} skipped: no load current could be placed on "
                "the mesh, so there is no drop to size against."
            )
            continue

        stranded = unreachable_nodes(
            mesh, source_nodes, [item["node_id"] for item in loads],
        )
        if stranded:
            # Without this the solver drops the unreachable loads, load_drop_v
            # finds no load voltage and returns 0.0 V, and the advisor reports
            # the rail as already within target -- a pass that never happened.
            _log(
                f"{rail.net_name} skipped: {len(stranded)} of {len(loads)} load "
                f"node(s) are not connected to source {source_ref} in the meshed "
                "copper, so any drop computed would be fictitious."
            )
            continue

        try:
            remediations = build_dc_remediations(
                board, rail.net_name, "",
                sources=[
                    {"node_id": node_id, "voltage": nominal}
                    for node_id in source_nodes
                ],
                loads=loads,
                target_drop_v=nominal * float(maximum_drop_pct) / 100.0,
                grid_step_mm=grid_step_mm,
                verify=verify, log_callback=log_callback,
            )
        except Exception as exc:
            _log(f"Remediation sizing failed for {rail.net_name}: {exc}")
            continue
        if remediations:
            finding.remediations.extend(remediations)
            attached += 1
        # Deliberately silent when the advisor declines. It logs its own reason
        # through this same callback -- "the dominant loss is not on track
        # segments", for instance -- and the generic line that used to follow
        # offered three alternatives, every one of them wrong in that case,
        # burying a precise answer under guesses.
    return attached


def _thermal_surface_metrics(domain_result: Any) -> list:
    """Publish the surface coefficients that produced the temperatures.

    A hotspot is only as trustworthy as the exchange coefficient behind it, and
    that coefficient was previously computed, stored and never shown. Reporting
    it lets a reader sanity-check the result against a handbook figure instead
    of taking the temperature on faith.
    """
    surfaces = dict(getattr(domain_result, "surface_h_w_m2k", {}) or {})
    if not surfaces:
        return []
    labels = {"top": "Top", "bottom": "Bottom", "edge": "Edge"}
    return [
        AnalysisMetric(
            f"surface_h_{kind}", f"{labels.get(kind, kind.title())} surface exchange",
            float(value), "W/m²K", "INFO",
        )
        for kind, value in sorted(surfaces.items())
    ]


def _thermal_surface_detail(domain_result: Any) -> str:
    """One line naming the coefficients used and where they came from.

    TH-2: the coupled path used to call solve() and never
    solve_with_surface_refinement, so this always claimed "refined against
    the solved surface temperature" while h stayed at the mesh's nominal
    ambient + 30 K guess (P02 Rev E published h 16.44 / 12.00 W/m2K, exactly
    that guess). The claim now follows ThermalResult.surface_refined, which
    is only True once ThermalSolver._refine_surface_exchange actually ran.
    """
    surfaces = dict(getattr(domain_result, "surface_h_w_m2k", {}) or {})
    basis = str(getattr(domain_result, "convection_basis", "") or "unspecified correlation")
    length_m = float(getattr(domain_result, "characteristic_length_m", 0.0) or 0.0)
    if not surfaces:
        return (
            f"Surface exchange follows {basis}; per-face coefficients were not recorded."
        )
    applied = ", ".join(
        f"{kind} {value:.1f} W/m²K" for kind, value in sorted(surfaces.items())
    )
    length_note = (
        f" Characteristic length A/P = {length_m * 1000.0:.1f} mm."
        if length_m > 0.0 else ""
    )
    provenance = (
        "Convection and radiation combined, refined against the solved surface temperature"
        if bool(getattr(domain_result, "surface_refined", False)) else
        "Convection and radiation combined at the mesh's nominal ambient + 30 K rise, "
        "not refined against the solved surface temperature"
    )
    return f"{provenance}: {applied}. Basis: {basis}.{length_note}"


def _thermal_correlation_range_limitations(domain_result: Any) -> list:
    """Say which faces' convection correlation is being extrapolated.

    TH-8: natural_convection_in_range/forced_convection_in_range existed in
    surface_convection.py but were never called. P02 Rev E's Ra ~ 8.0e3 sits
    below the 1e4 lower bound the laminar horizontal-plate correlations
    (Incropera) are stated for; the coefficient was published with no hint
    that it was extrapolated rather than looked up inside a validated range.
    """
    in_range = dict(getattr(domain_result, "surface_correlation_in_range", {}) or {})
    out_of_range = sorted(kind for kind, ok in in_range.items() if not ok)
    if not out_of_range:
        return []
    return [
        f"The natural/forced convection correlation is being applied outside its stated "
        f"Rayleigh/Reynolds range for the {', '.join(out_of_range)} face(s); the coefficient "
        "there is an extrapolation, not a value read from inside a validated range."
    ]


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


def _copper_loss_evidence(copper_loss_source: str) -> list:
    if not copper_loss_source:
        return []
    return [AnalysisEvidence(
        "COPPER_LOSSES",
        f"DC copper-loss heat sources come from {copper_loss_source}.",
        reference=copper_loss_source,
    )]


# A hotspot that moves by more than this between the grid and one twice as
# coarse is not converged in mesh: the solve's own resolution decides the
# figure more than a design change of that size would.
THERMAL_MESH_SENSITIVITY_LIMIT_C = 2.0


def _thermal_mesh_sensitivity(mesh_sensitivity: Any):
    """(metrics, findings, limitations) for a hotspot solved at two grid steps."""
    data = dict(mesh_sensitivity or {})
    if "delta_c" not in data:
        reason = data.get("not_verified_reason")
        if reason:
            # TH-3: a campaign always runs the coupled path, where the
            # frozen-heat coarse re-solve can fail for reasons a plain
            # thermal run never hits (a missing rail's DC result, for
            # example). Silence would look identical to the check never
            # having been requested; say plainly that it was not verified.
            return [], [], [
                f"Mesh sensitivity was not verified for this coupled result: {reason}"
            ]
        return [], [], []
    delta = float(data["delta_c"])
    exceeded = delta > THERMAL_MESH_SENSITIVITY_LIMIT_C
    metrics = [AnalysisMetric(
        "hotspot_mesh_sensitivity",
        f"Hotspot change at twice the grid step ({float(data.get('coarse_grid_mm', 0.0)):g} mm)",
        delta, "°C", "WARN" if exceeded else "INFO",
    )]
    if not exceeded:
        # Two solves agreeing is not convergence. Measured on a synthetic
        # board (validation/thermal_mesh_sensitivity.py) the hotspot read
        # 105.3, 101.3, 105.7 and 104.9 C at 0.5, 1, 2 and 4 mm: the 2 and
        # 4 mm solves agree within 0.8 C while 0.5 mm is 4 C from 1 mm.
        return metrics, [], [
            f"The hotspot moved {delta:.2f} C between {float(data.get('fine_grid_mm', 0.0)):g} and "
            f"{float(data.get('coarse_grid_mm', 0.0)):g} mm. Two steps agreeing does not prove the "
            "mesh converged: on a synthetic board the 2 and 4 mm solves agreed within 0.8 C "
            "while the 0.5 and 1 mm solves were 4 C apart (validation/thermal_mesh_sensitivity.py)."
        ]
    return metrics, [_finding(
        "TH-004", 1, "NUMERICS", FindingSeverity.MEDIUM,
        "Thermal result is not converged in mesh",
        f"The hotspot is {float(data.get('fine_hotspot_c', 0.0)):.2f} C at "
        f"{float(data.get('fine_grid_mm', 0.0)):g} mm and "
        f"{float(data.get('coarse_hotspot_c', 0.0)):.2f} C at "
        f"{float(data.get('coarse_grid_mm', 0.0)):g} mm, {delta:.2f} C apart, more than the "
        f"{THERMAL_MESH_SENSITIVITY_LIMIT_C:g} C the mesh may move a converged result.",
        f"Rerun with a finer grid until the check stays under {THERMAL_MESH_SENSITIVITY_LIMIT_C:g} C; "
        f"until then read temperatures as uncertain by at least {delta:.1f} C.",
        confidence=EvidenceConfidence.DETERMINISTIC,
    )], []


def _thermal_hotspot_component(hotspot: Any, mesh: Any):
    """(ref_des, distance_mm) of the component nearest the board hotspot.

    TH-1: the campaign report named neither the hottest junction nor the
    component at the hotspot -- on P02 Rev E the 114.71 C board hotspot sits
    under Q10 while the hottest *junction* is U14 (125.0 C, 25 K margin):
    two different components a reader could not otherwise tell apart.
    ``distance_mm`` is 0.0 when the hotspot node is literally one of the
    component's mapped mesh cells (mesh.component_nodes), i.e. "under" it;
    otherwise it is the distance to that component's nearest mapped centroid.
    """
    if hotspot is None or mesh is None:
        return None
    component_nodes = dict(getattr(mesh, "component_nodes", {}) or {})
    if not component_nodes:
        return None
    hotspot_node = getattr(hotspot, "node_id", None)
    for ref_des, nodes in component_nodes.items():
        if hotspot_node is not None and hotspot_node in nodes:
            return ref_des, 0.0
    node_coords = dict(getattr(mesh, "node_coords", {}) or {})
    hx = float(getattr(hotspot, "x_mm", 0.0))
    hy = float(getattr(hotspot, "y_mm", 0.0))
    best = None
    for ref_des, nodes in component_nodes.items():
        coords = [node_coords[node] for node in nodes if node in node_coords]
        if not coords:
            continue
        cx = sum(item[0] for item in coords) / len(coords)
        cy = sum(item[1] for item in coords) / len(coords)
        distance = math.hypot(hx - cx, hy - cy)
        if best is None or distance < best[1]:
            best = (ref_des, distance)
    return best


def _thermal_hotspot_location_evidence(hotspot: Any, mesh: Any) -> list:
    located = _thermal_hotspot_component(hotspot, mesh)
    if located is None:
        return []
    ref_des, distance_mm = located
    where = f"under {ref_des}" if distance_mm <= 1e-6 else f"{distance_mm:.1f} mm from {ref_des}"
    return [AnalysisEvidence(
        "HOTSPOT_LOCATION",
        f"The {float(getattr(hotspot, 'temperature_c', 0.0)):.2f} C hotspot at "
        f"({float(getattr(hotspot, 'x_mm', 0.0)):.2f}, {float(getattr(hotspot, 'y_mm', 0.0)):.2f}) "
        f"mm is {where}.",
        reference=ref_des,
        x_mm=float(getattr(hotspot, "x_mm", 0.0)), y_mm=float(getattr(hotspot, "y_mm", 0.0)),
    )]


def _thermal_junction_evidence(components: Any) -> list:
    """Name the hottest junctions instead of leaving them in a list nobody
    reading the report summary sees. ``components`` is already sorted
    hottest-first (thermal_solver.py); P02 Rev E's hottest is U14, 125.0 C,
    25 K margin."""
    top = list(components or [])[:3]
    if not top:
        return []
    named = "; ".join(
        f"{item.ref_des} {float(item.junction_temperature_c):.1f} C "
        f"(margin {float(item.margin_c):.1f} K, {item.thermal_source})"
        for item in top
    )
    return [AnalysisEvidence(
        "JUNCTION_TEMPERATURES",
        f"Hottest junction estimate(s): {named}.",
        reference=f"{len(components)} component(s) mapped",
    )]


def _thermal_fallback_stage_limitations(power_stage_reports: Any) -> list:
    """Say which stages are efficiency-closed fallbacks, and how much heat
    that places and where (TH-1, TH-6 context).

    power_loss.py:349-359 closes an incompletely modelled stage by forcing
    its total loss to the configured efficiency target and dumping the gap
    onto one component as a residual LossContribution (mechanism
    "unmodelled-conversion-residual"; provenance source "efficiency
    fallback" or "efficiency table residual"). On P02 Rev E that residual is
    1.365 W of 4.9186 W total board heat (28%), from U4's stage, landed on
    Q10 at confidence=low.
    """
    limitations = []
    for stage in power_stage_reports or []:
        stage_total = float(getattr(stage, "total_loss_w", 0.0) or 0.0)
        for loss in getattr(stage, "losses", ()) or ():
            provenance = dict(getattr(loss, "provenance", {}) or {})
            source = str(provenance.get("source", ""))
            if source not in ("efficiency fallback", "efficiency table residual"):
                continue
            power_w = float(getattr(loss, "power_w", 0.0) or 0.0)
            share_pct = power_w / stage_total * 100.0 if stage_total > 0 else 0.0
            basis = (
                "the configured efficiency fallback" if source == "efficiency fallback"
                else "the efficiency table"
            )
            limitations.append(
                f"{getattr(stage, 'name', '?')}: {power_w:.3g} W ({share_pct:.0f}% of this "
                f"stage's loss) is an unmodelled-conversion residual placed on "
                f"{getattr(loss, 'ref_des', '?')}, closed by {basis} "
                f"({getattr(stage, 'efficiency_provenance', '')}; "
                f"confidence={provenance.get('confidence', 'low')})."
            )
    return limitations


def _thermal_enclosure_metrics_evidence_limitations(
    ambient_c: Any, enclosure_air_rise_k: Any, enclosure_ambient_applied: bool,
):
    """COH-4: publish the enclosure's internal-air assumption, or say it was
    computed but not applied, instead of thermal silently ignoring the
    sealed box the CFD domain models (CFD-4: ~16 K mean air rise for P02
    Rev E's box; hotspot ~115 -> ~131 C, U14 margin ~25 -> ~9 K estimated).
    """
    if enclosure_air_rise_k is None:
        return [], [], []
    rise_k = float(enclosure_air_rise_k)
    internal_air_c = float(ambient_c or 0.0) + rise_k
    if not enclosure_ambient_applied:
        return [], [], [
            f"An enclosure is configured but its ambient-rise correction is disabled: "
            f"internal air would run {rise_k:.1f} K warmer than the configured ambient "
            f"({internal_air_c:.1f} C) if enabled (settings.apply_enclosure_ambient)."
        ]
    metrics = [AnalysisMetric(
        "internal_air_c", "Internal air (enclosure)", internal_air_c, "°C", "INFO",
    )]
    evidence = [AnalysisEvidence(
        "ENCLOSURE_AMBIENT",
        f"Solved at internal air {internal_air_c:.1f} C (ambient + {rise_k:.1f} K from the "
        "enclosure heat balance), not the bare configured ambient.",
        reference="enclosure heat balance: rise = Q / (h * A_walls)",
    )]
    return metrics, evidence, []


def _thermal_via_remediation(component: Any, ambient_c: Any):
    """Thermal vias for a part over its junction limit, or None."""
    if ambient_c is None:
        return None
    power = float(getattr(component, "power_w", 0.0) or 0.0)
    allowed_rise = (
        float(getattr(component, "max_junction_c", 0.0) or 0.0)
        - power * float(getattr(component, "theta_jb_c_per_w", 0.0) or 0.0)
        - float(ambient_c)
    )
    if power <= 0.0 or allowed_rise <= 0.0:
        return None
    try:
        from advisor.thermal_advisor import build_thermal_via_remediation
        return build_thermal_via_remediation(
            str(component.ref_des), power, allowed_rise,
            DEFAULT_BOARD_THICKNESS_MM, THERMAL_VIA_DRILL_MM, existing_via_count=None,
        )
    except (ImportError, ValueError):
        return None


def adapt_thermal_result(domain_result: Any, coupled: bool = False, elapsed_seconds: float = 0.0,
                         *, copper_loss_source: str = "", mesh_sensitivity: Any = None,
                         mesh: Any = None, power_stage_reports: Any = None,
                         ambient_c: Any = None, enclosure_air_rise_k: Any = None,
                         enclosure_ambient_applied: bool = False) -> AnalysisResult:
    sensitivity_metrics, sensitivity_findings, sensitivity_limitations = (
        _thermal_mesh_sensitivity(mesh_sensitivity)
    )
    findings = list(sensitivity_findings)
    components = list(getattr(domain_result, "component_results", ()) or ())
    for index, component in enumerate(components, start=1):
        if float(getattr(component, "margin_c", 0.0)) < 0:
            # The junction is the board temperature plus P x thetaJB, so the
            # verdict is only as good as thetaJB. A default one cannot carry a
            # CRITICAL: the same component with its datasheet value may pass.
            source = str(getattr(component, "thermal_source", "estimate") or "estimate").lower()
            stated = source != "estimate"
            theta = float(getattr(component, "theta_jb_c_per_w", 0.0) or 0.0)
            finding = _finding(
                "TH-001", index, "JUNCTION_TEMPERATURE",
                FindingSeverity.CRITICAL if stated else FindingSeverity.HIGH,
                f"{component.ref_des} exceeds its junction limit",
                f"Estimated Tj is {component.junction_temperature_c:.3f} C with "
                f"{component.margin_c:.3f} C margin"
                + (
                    f", from a thetaJB of {theta:g} C/W taken from {source}."
                    if stated else
                    f", from an assumed thetaJB of {theta:g} C/W; the datasheet value "
                    "decides whether the limit is really exceeded."
                ),
                "Reduce dissipation or improve the component thermal path."
                if stated else
                "Enter the component's datasheet thetaJB, then reduce dissipation or "
                "improve the thermal path if it still exceeds the limit.",
            )
            if "datasheet" in source:
                finding.confidence = EvidenceConfidence.DATASHEET_BACKED
            elif "measur" in source:
                finding.confidence = EvidenceConfidence.MEASURED
            finding.components = [str(component.ref_des)]
            # The thermal advisor sized thermal vias and was imported by no
            # production module, so no thermal action ever carried a
            # remediation (COH-1). Its 1-D sizing: the barrels that would
            # carry the part's heat through the board within the rise its
            # junction limit still allows above the ambient.
            effective_ambient = (
                None if ambient_c is None else float(ambient_c) + (
                    float(enclosure_air_rise_k or 0.0) if enclosure_ambient_applied else 0.0
                )
            )
            remediation = _thermal_via_remediation(component, effective_ambient)
            if remediation is not None:
                finding.remediations.append(remediation)
            findings.append(finding)
    if not bool(getattr(domain_result, "converged", True)):
        findings.append(_finding(
            "TH-002", 1, "NUMERICS", FindingSeverity.HIGH,
            "Thermal solver did not converge", "The iteration limit was reached.",
            "Review mesh resolution and convergence settings.",
            confidence=EvidenceConfidence.DETERMINISTIC,
        ))
    balance = float(getattr(domain_result, "energy_balance_error_pct", 0.0))
    if abs(balance) > 5.0:
        findings.append(_finding(
            "TH-003", 1, "ENERGY_BALANCE", FindingSeverity.MEDIUM,
            "Thermal energy balance is weak", f"Energy balance error is {balance:.3f}%.",
            "Refine the mesh or boundary conditions before relying on hotspots.",
        ))
    # The energy balance above is the linear solve's row-sum identity: it
    # compares deposited heat with what left, and cannot see heat that never
    # reached a cell (an unplaced component, a copper loss off the mesh). On
    # P02 Rev E nothing was lost (4.471 W + 0.4475 W = 4.9186 W checked by
    # hand), but nothing would have said so either (TH-9).
    undeposited = float(getattr(domain_result, "undeposited_power_w", 0.0) or 0.0)
    intended = float(getattr(domain_result, "intended_input_power_w", 0.0) or 0.0)
    if undeposited > max(1e-3, 0.001 * intended):
        findings.append(_finding(
            "TH-005", 1, "MODEL_INPUT", FindingSeverity.MEDIUM,
            "Heat left out of the thermal model",
            f"{undeposited:.3g} W of the {intended:.3g} W the model was given never reached a "
            "mesh cell: a component without a placement on the board, or a copper loss off "
            "the meshed copper. Temperatures are low by that much heat.",
            "Check that every heat-dissipating component is placed on the board and enabled.",
            confidence=EvidenceConfidence.DETERMINISTIC,
        ))
    hotspot = getattr(domain_result, "hotspot", None)
    status = AnalysisStatus.FAIL if any(item.severity == FindingSeverity.CRITICAL for item in findings) else (
        AnalysisStatus.WARN if findings else AnalysisStatus.PASS
    )
    enclosure_metrics, enclosure_evidence, enclosure_limitations = (
        _thermal_enclosure_metrics_evidence_limitations(
            ambient_c, enclosure_air_rise_k, enclosure_ambient_applied,
        )
    )
    return AnalysisResult(
        "THERMAL", "3D Thermal", status=status,
        elapsed_seconds=float(elapsed_seconds or 0.0),
        summary={"coupled": bool(coupled), "component_count": len(components)},
        findings=findings,
        metrics=[
            AnalysisMetric(
                "hotspot", "Hotspot",
                float(getattr(hotspot, "temperature_c", 0.0)), "°C",
                # TH-1 / COH-11: this used to copy the domain's PASS/WARN/FAIL
                # onto the metric with no threshold of its own -- "Hotspot
                # 114.71 C PASS" next to EMC flagging the same board over
                # 85 C. A bare temperature has no universal pass/fail line;
                # only a junction's margin does (TH-001 above).
                "INFO",
            ),
            AnalysisMetric(
                "input_heat", "Input heat",
                float(getattr(domain_result, "total_input_power_w", 0.0)), "W", "INFO",
            ),
            AnalysisMetric(
                "energy_balance", "Energy balance error", balance, "%",
                # Renamed from a bare "PASS": this measures only that the
                # linear solve converged, and would look just as good with a
                # coefficient wrong by a factor of ten. Calling it PASS invited
                # it to be read as physical accuracy.
                "NUMERICS_OK" if abs(balance) <= 5.0 else "WARN",
            ),
        ] + (
            [AnalysisMetric(
                "hottest_junction_margin", f"{components[0].ref_des} junction margin",
                float(components[0].margin_c), "K",
                "WARN" if float(components[0].margin_c) < 0 else "INFO",
            )] if components else []
        ) + _thermal_surface_metrics(domain_result) + sensitivity_metrics + enclosure_metrics,
        provenance=[
            AnalysisEvidence(
                "PCB_GEOMETRY",
                "The thermal mesh uses a detached snapshot of board copper, stackup, outline, and component geometry.",
                reference="KiCad IPC snapshot",
            ),
            AnalysisEvidence(
                "SURFACE_EXCHANGE",
                _thermal_surface_detail(domain_result),
                reference=str(getattr(domain_result, "convection_basis", "") or "surface correlation"),
            ),
        ] + _thermal_hotspot_location_evidence(hotspot, mesh) + _thermal_junction_evidence(components) + [
            AnalysisEvidence(
                "POWER_MODEL",
                "Heat sources use configured component losses and optional DC copper losses.",
                reference="coupled DC" if coupled else "thermal inputs",
            ),
        ] + _copper_loss_evidence(copper_loss_source) + enclosure_evidence,
        compute_metadata={
            "backend": getattr(domain_result, "compute_backend", ""),
            "device": getattr(domain_result, "compute_device", ""),
            "relative_residual": getattr(domain_result, "compute_relative_residual", 0.0),
        },
        limitations=[
            "Compact thermal models require datasheet and measurement validation.",
        ] + _thermal_grid_limitations(domain_result) + sensitivity_limitations
        + _thermal_fallback_stage_limitations(power_stage_reports) + enclosure_limitations
        + _thermal_correlation_range_limitations(domain_result),
    ).finish()


def _cfd_collapsed_obstacle_limitations(mesh: Any) -> list:
    # CFD-1, docs/audit-2026-09-23.md: an obstacle thinner than one cell on
    # every axis is still a single fallback cell -- a point mass with no
    # conduction path to its neighbours -- even after a thin board is snapped
    # to the full sheet of cells its footprint overlaps. Named rather than
    # silent, per the finding's own requirement.
    names = list(getattr(mesh, "collapsed_obstacles", None) or [])
    if not names:
        return []
    return [
        f"{len(names)} obstacle(s) smaller than one mesh cell on every axis are each "
        f"modelled as a single point cell with no conduction path to neighbouring cells: "
        f"{', '.join(names)}. Reduce the CFD cell size to resolve them.",
    ]


def adapt_cfd_result(mesh: Any, domain_result: Any, *, copper_loss_source: str = "",
                      settings: Any = None) -> AnalysisResult:
    findings = []
    mesh_quality_limitations = []
    if not bool(getattr(domain_result, "converged", False)):
        # Report which residual fell short and by how much. A bare "did not
        # converge" was useless here: continuity is floored around 1e-2 by the
        # inlet-cell discontinuity documented in docs/validation-cfd.md, so on a
        # run whose momentum and energy have both reached 1e-12 the flag says
        # nothing about solution quality. Grading by distance from tolerance
        # keeps HIGH meaningful for runs that genuinely stalled.
        history = getattr(domain_result, "residuals", None)
        tail = {
            name: float(getattr(history, name)[-1])
            for name in ("continuity", "momentum", "energy")
            if history is not None and getattr(history, name, None)
        }
        worst_name, worst = ("", 0.0)
        if tail:
            worst_name, worst = max(tail.items(), key=lambda item: item[1])
        detail = (
            "The iteration limit was reached. Final residuals: "
            + ", ".join(f"{name}={value:.3g}" for name, value in tail.items())
            if tail else "The iteration limit was reached."
        )
        findings.append(_finding(
            "CFD-001", 1, "NUMERICS",
            FindingSeverity.HIGH if worst > 0.1 else FindingSeverity.MEDIUM,
            f"CFD solver did not converge ({worst_name or 'residual'} limiting)"
            if worst_name else "CFD solver did not converge",
            detail,
            "Raise max_iterations, refine the mesh, or lower relaxation. "
            "Check the mass balance alongside this: a solve can miss the "
            "tolerance while still conserving mass to a fraction of a percent, "
            "which usually means the flow had not finished developing rather "
            "than that the solution is wrong.",
            confidence=EvidenceConfidence.DETERMINISTIC,
        ))
    # Resolution is a property of the mesh the user chose, not a defect this
    # run discovered -- CFD-1 and CFD-2 were the defects, and both are fixed.
    # It stays out of `findings` (and so out of the score and the action
    # list, docs/audit-2026-09-23.md CFD-4/COH-9) as a "calculation quality"
    # limitation instead: real information, never scored or ranked as if it
    # were a design problem. The benchmark found the momentum discretisation
    # accurate to 0.4% at 20 cells across a channel, 7% at 10, and at 6 cells
    # it produced no boundary layer at all -- a plug profile with
    # u_max/u_mean = 1.000. The threshold follows that measured error curve,
    # not a round number: 20 -> 0.4%, 14 -> 3.5%, 10 -> 7.1%, 6 -> none.
    across = min(getattr(mesh, "shape", ()) or (0,))
    mesh_inadequate = bool(across and across < 14)
    if mesh_inadequate:
        mesh_quality_limitations.append(
            f"Enclosure mesh resolution: the narrowest direction spans {across} cells. "
            "Measured profile error against laminar duct theory: 0.4% at 20 cells, 3.5% "
            "at 14, 7.1% at 10, and at 6 cells no boundary layer formed at all. Reduce "
            "the CFD cell size until the smallest dimension spans at least 14 cells, or "
            "read the velocity field as indicative only."
        )
        # This used to blame a specific mechanism -- an unresolved buoyant
        # plume carrying heat from the air to the wall -- for why solid and
        # air maxima are not usable at this mesh size. That diagnosis was
        # wrong: the real causes were CFD-1 (an obstacle collapsing to an
        # isolated cell with no conduction path) and CFD-2 (energy advected
        # non-conservatively), both fixed above. What is left is ordinary
        # mesh resolution error, the same one already stated for velocity,
        # and it is why the maxima below are marked unusable rather than
        # given a physical story they do not need.
        solid_c = float(getattr(domain_result, "maximum_solid_temperature_c", 0.0))
        air_c = float(getattr(domain_result, "maximum_air_temperature_c", 0.0))
        if solid_c > air_c > 0.0:
            emissivity = float(getattr(getattr(settings, "geometry", None), "emissivity", 0.0) or 0.0)
            film_terms = (
                "natural, forced and radiation correlations" if emissivity > 0.0
                else "natural and forced correlations (radiation is off: "
                     "geometry.emissivity is 0)"
            )
            mesh_quality_limitations.append(
                f"Solid and air maxima are not usable at this mesh size: the hottest "
                f"solid reads {solid_c:.1f} C against {air_c:.1f} C for the air, "
                f"{solid_c - air_c:.1f} C apart. A solid-air face does carry a surface "
                f"film (conduction through the solid half-cell in series with {film_terms}), "
                "so the component's own interface is modelled; the resolution error above "
                "is what makes the local values unreliable, not an unmodelled transport "
                "mechanism. Take component temperatures from the 3D thermal analysis, "
                "which applies a convective correlation and radiation at every exposed "
                "face. Use the mean internal air rise below, or the flow field, from "
                "this run."
            )
    # Boussinesq (buoyancy linear in temperature) is usually cited as valid to
    # beta*dT <~ 0.1; above that the force this solver computes is
    # progressively less trustworthy, and it is a fact about this run's own
    # solved field, not a design defect -- a limitation, not a finding.
    # See CFD-7, docs/audit-2026-09-23.md.
    boussinesq_max = float(getattr(domain_result, "boussinesq_beta_delta_t_max", 0.0))
    if boussinesq_max > 0.1:
        mesh_quality_limitations.append(
            f"Boussinesq buoyancy is out of its usual validity range: beta * max|T - "
            f"ambient| = {boussinesq_max:.2f} (usually cited as valid below ~0.1). "
            "The temperature field is likely developing enough real density change "
            "that the linear approximation under- or over-drives the flow."
        )
    mass_applicable = bool(getattr(domain_result, "mass_balance_applicable", True))
    for index, (key, label) in enumerate((("mass_balance_error_pct", "Mass"), ("energy_balance_error_pct", "Energy")), start=1):
        if key == "mass_balance_error_pct" and not mass_applicable:
            continue
        value = float(getattr(domain_result, key, 0.0))
        if abs(value) > 5.0:
            findings.append(_finding(
                "CFD-002", index, "CONSERVATION", FindingSeverity.MEDIUM,
                f"{label} balance is weak", f"{label} balance error is {value:.3f}%.",
                "Refine the mesh or boundary conditions.",
            ))
    overall_status = AnalysisStatus.WARN if findings else AnalysisStatus.PASS
    # The one number a sealed box's coarse mesh does not need resolving to
    # defend: Q / (h * A_walls). docs/audit-2026-09-23.md CFD-4 -- P02 Rev E
    # published solid 573.9 C / air 404.7 C as its metrics and never this,
    # even though it is the figure the thermal analysis's own ambient should
    # use (COH-4). Requires settings (the enclosure geometry); older callers
    # that do not pass it simply do not get the headline metrics.
    headline_metrics = []
    if settings is not None:
        try:
            air_rise_k = enclosure_air_rise_k(
                settings, float(getattr(domain_result, "total_heat_w", 0.0))
            )
            headline_metrics = [
                AnalysisMetric("mean_air_rise", "Mean internal air rise", air_rise_k, "K", "PASS"),
                AnalysisMetric(
                    "internal_ambient", "Internal ambient (ambient + mean air rise)",
                    float(getattr(settings, "ambient_c", 0.0)) + air_rise_k, "°C", "PASS",
                ),
            ]
        except (AttributeError, TypeError, ZeroDivisionError):
            headline_metrics = []
    # Hide/qualify, never silently trust: at this mesh size the maxima below
    # are the numbers the mesh-quality limitation above says not to use.
    maxima_status = "NOT_USABLE" if mesh_inadequate else overall_status.value
    return AnalysisResult(
        "CFD", "Enclosure CFD",
        status=overall_status,
        summary={"cell_count": int(getattr(mesh, "cell_count", 0)), "iterations": int(getattr(domain_result, "iterations", 0))},
        findings=findings,
        metrics=headline_metrics + [
            AnalysisMetric("maximum_velocity", "Maximum velocity", float(getattr(domain_result, "maximum_velocity_m_s", 0.0)), "m/s", overall_status.value),
            AnalysisMetric("board_free_stream_velocity", "Free-stream velocity over the board", float(getattr(domain_result, "board_free_stream_velocity_m_s", 0.0)), "m/s", overall_status.value),
            AnalysisMetric("maximum_air_temperature", "Maximum air temperature", float(getattr(domain_result, "maximum_air_temperature_c", 0.0)), "°C", maxima_status),
            AnalysisMetric("maximum_solid_temperature", "Maximum solid temperature", float(getattr(domain_result, "maximum_solid_temperature_c", 0.0)), "°C", maxima_status),
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
        ] + _copper_loss_evidence(copper_loss_source),
        compute_metadata={"backend": getattr(domain_result, "compute_backend", ""), "device": getattr(domain_result, "compute_device", "")},
        limitations=[
            "Laminar steady-state model; turbulence, fan blades, and transients are excluded. "
            + (
                "Radiation is included in the solid-air film (geometry.emissivity "
                f"= {float(getattr(getattr(settings, 'geometry', None), 'emissivity', 0.0) or 0.0):g}), "
                "but not between enclosure walls or to ambient."
                if float(getattr(getattr(settings, "geometry", None), "emissivity", 0.0) or 0.0) > 0.0
                else "Radiation is excluded (geometry.emissivity is 0)."
            ),
            "Momentum discretisation validated against laminar duct flow to 0.4% at 20 cells "
            "across the channel; at 6 cells it produced no boundary layer at all. "
            "See docs/validation-cfd.md.",
            "Solid-air film: the length scale is the mesh's own mean cell size, not each "
            "obstacle's A/P footprint; the forced-convection term uses the adjacent cell's "
            "local speed rather than the board's free-stream velocity (the value this same "
            "report uses for its own free-stream metric, for the opposite reason -- a "
            "near-wall cell under-predicts the coefficient there, the same physics this "
            "film term does not correct for); and natural convection is evaluated everywhere "
            "a solid-air face exists, including where the resolved buoyant flow already "
            "carries that heat, which can double-count it. See CFD-6, "
            "docs/audit-2026-09-23.md.",
            "Sealed enclosure: no flow crosses a boundary, so mass balance is "
            "not applicable rather than perfect."
            if not mass_applicable else
            f"Mass balance is measured before the outlet fix-up: "
            f"{float(getattr(domain_result, 'mass_balance_error_pct', 0.0)):.3g}% of the inflow "
            "did not reach the outlet.",
        ] + mesh_quality_limitations + _cfd_collapsed_obstacle_limitations(mesh),
    ).finish()


def adapt_dc_run(system_results: Any, maximum_drop_pct: float, *,
                 board_path: Any = None, rails: Any = None,
                 log_callback=None) -> AnalysisResult:
    """Turn one DC solve into the AnalysisResult everything downstream reads.

    ``adapt_dc_result`` builds the findings and ``attach_dc_remediations``
    sizes the fixes; a DC result that has had only the first done is missing
    every remediation, and the report says "No structured remediation was
    computed" for each action.  Two callers doing those two steps separately
    is how they come to disagree, so this is the one call both make: the
    dialog when it publishes a result tab, and CampaignEngine's DC adapter.

    Sizing does nothing without a board path and rails, and an advisor that
    cannot run costs the estimates rather than the result: a failure is caught
    here and recorded as a limitation, so the report says the fixes are
    missing instead of the campaign losing its DC section over them.
    """
    result = adapt_dc_result(system_results, maximum_drop_pct)
    _state_rail_discovery(result, rails)
    try:
        attached = attach_dc_remediations(
            result, board_path, rails, maximum_drop_pct, log_callback=log_callback,
            system_results=system_results,
        )
    except Exception as exc:
        message = (
            f"Corrective actions were not sized: the DC advisor failed with "
            f"{type(exc).__name__}: {exc}. The findings below stand; the fixes "
            "they would carry do not."
        )
        result.limitations.append(message)
        if log_callback is not None:
            log_callback(message)
        return result
    if attached and log_callback is not None:
        log_callback(f"Sized a copper fix for {attached} voltage-drop finding(s).")
    return result


def _state_rail_discovery(result: AnalysisResult, rails: Any) -> None:
    """Name the rails the run took from the board scan rather than from the project configuration.

    A configured rail is the designer's entry. A scanned one was declared by a
    power symbol, inferred from power pins, or guessed from its name -- and a
    scanned rail's voltage is always read off its name. The result says which
    (docs/plan-universalite.md, phase 5).
    """
    try:
        from discovery import DISCOVERY_LABELS
    except ImportError:  # pragma: no cover - package-relative import in the plugin
        from .discovery import DISCOVERY_LABELS
    scanned = [rail for rail in (rails or ()) if getattr(rail, "discovery", "") in DISCOVERY_LABELS]
    if not scanned:
        return
    result.provenance.append(AnalysisEvidence(
        "RAIL_DISCOVERY",
        "Rails from the board scan, not from the saved project configuration: "
        + "; ".join(f"{rail.net_name} ({DISCOVERY_LABELS[rail.discovery]})" for rail in scanned)
        + ". Their nominal voltages come from their names.",
    ))
    guessed = [rail.net_name for rail in scanned if rail.discovery == "name"]
    if guessed:
        result.limitations.append(
            "No saved file declares these nets as rails; they were guessed from their names: "
            + ", ".join(guessed) + "."
        )


# A worst-load drop that moves by more than this when the grid step is halved
# is not converged in mesh. On the P02 Rev E board +5V_U4 read 40.8 mV at 0.1
# mm and 49.7 mV at 0.05 mm, 22 % apart -- while 0.2 mm read 39.1 mV, which
# is why the DC check refines rather than coarsens.
DC_GRID_SENSITIVITY_LIMIT_PCT = 10.0


def _dc_grid_sensitivity(rail, index, check, maximum_drop_pct, source_voltage):
    """(metrics, findings) for a rail's worst-load drop solved again at half the grid step."""
    data = dict(check or {})
    if "delta_pct" not in data:
        return [], []
    delta = float(data["delta_pct"])
    grid, check_grid = float(data.get("grid_mm", 0.0)), float(data.get("check_grid_mm", 0.0))
    exceeded = delta > DC_GRID_SENSITIVITY_LIMIT_PCT
    metrics = [AnalysisMetric(
        f"rail_{index}_grid_sensitivity",
        f"{rail} worst-load drop change at half the grid step ({check_grid:g} mm)",
        delta, "%", "WARN" if exceeded else "INFO",
    )]
    if not exceeded:
        return metrics, []
    drop, check_drop = float(data.get("drop_v", 0.0)), float(data.get("check_drop_v", 0.0))
    budget_v = float(maximum_drop_pct) / 100.0 * float(source_voltage or 0.0)
    # A finer solve on the other side of the budget turns the verdict itself.
    flips = budget_v > 0.0 and (drop > budget_v) != (check_drop > budget_v)
    verdict = ""
    if flips:
        verdict = (
            f" At {check_grid:g} mm the drop "
            + ("exceeds" if check_drop > budget_v else "is within")
            + f" the {float(maximum_drop_pct):g}% budget the {grid:g} mm solve "
            + ("met." if check_drop > budget_v else "exceeded.")
        )
    return metrics, [_finding(
        "DC-005", index, "NUMERICS", FindingSeverity.HIGH if flips else FindingSeverity.MEDIUM,
        f"{rail} voltage drop is not converged in mesh",
        f"The worst load sees {drop * 1000.0:.1f} mV at {grid:g} mm and "
        f"{check_drop * 1000.0:.1f} mV at {check_grid:g} mm, {delta:.1f}% apart, more than the "
        f"{DC_GRID_SENSITIVITY_LIMIT_PCT:g}% a converged drop may move." + verdict,
        f"Rerun at {check_grid:g} mm or finer until the check stays under "
        f"{DC_GRID_SENSITIVITY_LIMIT_PCT:g}%; until then read the drop as uncertain by at least "
        f"{abs(check_drop - drop) * 1000.0:.1f} mV. Since DC-1, a track, via or pad joins the "
        "plane at every lattice node under its own footprint rather than the single nearest "
        "one, so a drop that still grows as the grid is refined usually runs through a neck "
        "of copper narrower than a few grid steps, not that meshing artifact.",
        confidence=EvidenceConfidence.DETERMINISTIC,
    )]


def adapt_dc_result(system_results: Any, maximum_drop_pct: float) -> AnalysisResult:
    findings = []
    metrics = []
    raster_limitations = []
    density_hotspots = {}
    grid_checked, grid_not_refined = [], []
    for index, (rail, data) in enumerate((system_results or {}).items(), start=1):
        first_finding = len(findings)
        vmin, vmax, drop = data.get("stats", (0.0, 0.0, 0.0))
        spread_pct = (float(drop) / float(vmax) * 100.0) if vmax else 0.0
        source_voltage = float(data.get("source_voltage_v", 0.0) or 0.0)
        if "load_drop_v" in data and source_voltage > 0.0:
            # The drop a load actually sees, from the rail's source to its
            # worst-placed load -- what the advisor sizes a fix against. The
            # node spread also counts copper no load draws through, and judged
            # alone it flagged rails whose loads were well within budget.
            drop_v = float(data["load_drop_v"])
            drop_pct = drop_v / source_voltage * 100.0
            detailed_result = data.get("detailed_result")
            # Loads on copper no source reaches were left out of the solve, so
            # a drop within budget says nothing about them: P02 Rev E's /+3V3AO,
            # every load excluded, read "0.0 % PASS".
            incomplete = detailed_result is not None and not getattr(detailed_result, "valid", True)
            # A rail that injects no current has no drop to judge: P02 Rev
            # E's /J2_LDO3V3, /+3V3AON and /J2_PP5V read "0.0 % PASS" (DC-4).
            injected = sum(
                abs(float(load.get("current", 0.0) or 0.0)) for load in data.get("loads") or ()
            ) if "loads" in data else None
            no_current = injected is not None and injected <= 0.0
            metrics.append(AnalysisMetric(
                f"rail_{index}_drop", f"{rail} voltage drop at the worst load", drop_pct, "%",
                "NO_DATA" if no_current else (
                    "WARN" if drop_pct > float(maximum_drop_pct)
                    else ("INCOMPLETE" if incomplete else "PASS")
                ),
            ))
            configured = [
                (str(reference), float(current)) for reference, current in data.get("configured_loads") or ()
                if float(current) > 0.0
            ]
            located = {
                str(load.get("ref_des")) for load in data.get("loads") or ()
                if load.get("ref_des") and float(load.get("current", 0.0) or 0.0) > 0.0
            }
            missing = [(reference, current) for reference, current in configured if reference not in located]
            if missing:
                missing_current = sum(current for _reference, current in missing)
                finding = _finding(
                    "DC-007", index, "MODEL_INPUT", FindingSeverity.MEDIUM,
                    f"{rail}: {len(missing)} configured load(s) were not placed on the copper",
                    ", ".join(f"{reference} ({current:.3g} A)" for reference, current in missing)
                    + f" -- {missing_current:.3g} A in total -- have no {rail} copper under "
                    "their pads, so the solve did not draw their current and the drop above "
                    "is low by that much.",
                    "Check the load's component reference and pad names in the power tree "
                    "against the board.",
                )
                finding.components = [reference for reference, _current in missing]
                findings.append(finding)
            metrics.append(AnalysisMetric(
                f"rail_{index}_spread", f"{rail} voltage spread across the net",
                spread_pct, "%", "INFO",
            ))
            drop_text = f"The worst-placed load sees {drop_pct:.3f}% ({drop_v * 1000.0:.1f} mV)"
        else:
            drop_pct = spread_pct
            metrics.append(AnalysisMetric(
                f"rail_{index}_drop", f"{rail} voltage drop", drop_pct, "%",
                "WARN" if drop_pct > float(maximum_drop_pct) else "PASS",
            ))
            drop_text = f"Calculated drop is {drop_pct:.3f}%"
        # DC-3: a pass element (fuse, eFuse, shunt, switch, net-tie) propagates
        # its input pad's real solved voltage to its output rail rather than
        # resetting to nominal (application/dc_controller.py's pass_voltages);
        # nominal_voltage_v is what the rail would read with no chain drop, so
        # the gap against source_voltage_v is the cumulative drop from the top
        # of the chain to this rail's own source. Rails with no pass-element
        # ancestor read the same value in both and raise nothing.
        nominal_voltage = float(data.get("nominal_voltage_v", 0.0) or 0.0)
        if nominal_voltage > 0.0 and source_voltage > 0.0:
            chain_drop_v = nominal_voltage - source_voltage
            if chain_drop_v > 1e-6:
                chain_drop_pct = chain_drop_v / nominal_voltage * 100.0
                metrics.append(AnalysisMetric(
                    f"rail_{index}_chain_drop",
                    f"{rail} cumulative drop from its chain's pass elements",
                    chain_drop_pct, "%",
                    "WARN" if chain_drop_pct > float(maximum_drop_pct) else "INFO",
                ))
                findings.append(_finding(
                    "DC-006", index, "VOLTAGE_DROP",
                    FindingSeverity.HIGH if chain_drop_pct > float(maximum_drop_pct)
                    else FindingSeverity.MEDIUM,
                    f"{rail} carries {chain_drop_v * 1000.0:.1f} mV of upstream chain drop",
                    f"{rail}'s source reads {source_voltage:.3f} V against a {nominal_voltage:.3f} V "
                    f"nominal, {chain_drop_v * 1000.0:.1f} mV ({chain_drop_pct:.2f}%) lost in the "
                    "fuses, eFuses, shunts, switches or net-ties between it and the regulator "
                    "actually setting the voltage -- copper this rail's own worst-load drop above "
                    "does not include.",
                    "Add the declared R_on of each pass element on the chain (sidecar "
                    "r_pass_ohm) if it is not already, and budget this drop alongside the "
                    "rail's own.",
                ))
        if data.get("engine") != "hybrid":
            metrics.extend(_raster_track_metrics(
                f"rail_{index}_", str(rail), data.get("track_summary"),
                float(data.get("grid_size_mm", 0.0) or 0.0),
            ))
            raster_limitations.extend(_raster_track_limitations(
                data.get("track_summary"), float(data.get("grid_size_mm", 0.0) or 0.0),
                f"{rail} raster DC mesh",
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
            # Not a fact about the copper: the raster mesh joins only
            # 4-neighbour grid nodes, and a track narrower than about 1.4 grid
            # steps on a diagonal carries no current in it at all
            # (validation/dc_raster_fidelity.py). An island can be the mesh's.
            step = float(data.get("grid_size_mm", 0.0) or 0.0)
            coarsened = bool(data.get("adaptive_grid", False))
            if data.get("engine") == "hybrid":
                # Tracks are analytic there, so no grid step opens one. The
                # mesh is the board as last saved, not as it is on screen.
                step_text = (
                    " The mesh was built from the saved board file with exact track"
                    " geometry, so no grid step disconnects a track; edits not yet"
                    " saved are not part of it."
                )
            else:
                step_text = (
                    f" The mesh ran at {step:g} mm"
                    + (f", coarsened from the requested {float(data.get('requested_grid_size_mm', 0.0) or 0.0):g} mm" if coarsened else "")
                    + "; tracks narrower than about 1.4 grid steps on a diagonal are not"
                    " connected at that step, so the island may be the mesh's rather than the board's."
                    if step > 0.0 else ""
                )
            excluded_refs = list(getattr(detailed, "excluded_load_references", ()) or ())
            excluded_current = float(getattr(detailed, "excluded_load_current_a", 0.0) or 0.0)
            total_current = float(getattr(detailed, "total_load_current_a", 0.0) or 0.0)
            # What is excluded is loads and their current, not nodes: P02 Rev
            # E's "81 load node(s)" were U14's input alone, 0.848 A of the
            # rail's 2.04 A (DC-7).
            if excluded_current > 0.0:
                share = (
                    f", {100.0 * excluded_current / total_current:.0f} % of the rail's "
                    f"{total_current:.3g} A" if total_current > 0.0 else ""
                )
                what = (
                    f"{', '.join(excluded_refs) or 'Loads'} ({excluded_current:.3g} A{share}) "
                    f"{'are' if len(excluded_refs) != 1 else 'is'} on copper no source reaches "
                    f"and were left out of the solve ({getattr(detailed, 'excluded_load_node_count', 0)} "
                    "mesh node(s))"
                )
            else:
                what = (
                    f"{getattr(detailed, 'excluded_load_node_count', 0)} load node(s) were excluded"
                    + (f" ({', '.join(excluded_refs)})" if excluded_refs else "")
                )
            dc001 = _finding(
                "DC-001", index, "CONNECTIVITY", FindingSeverity.HIGH,
                f"{rail} contains source-free loads",
                what + "." + step_text,
                # A finer grid cannot join what the hybrid mesh keeps apart:
                # on P02 Rev E each such island was an unrouted connection in
                # KiCad's DRC.
                "Check the copper between the source and these loads: KiCad's DRC lists the "
                "unconnected items. The hybrid mesh follows the saved copper exactly, so a finer "
                "grid will not join them."
                if data.get("engine") == "hybrid" else
                "Check the copper between the source and these loads in the editor. If it "
                "is continuous, rerun with a finer DC grid before editing the board.",
            )
            # The excluded parts, so the campaign can join this finding to
            # every other one about them (COH-5).
            dc001.components = excluded_refs
            findings.append(dc001)
        if compute is not None and not getattr(compute, "converged", True):
            findings.append(_finding(
                "DC-002", index, "NUMERICS", FindingSeverity.HIGH,
                f"{rail} solve did not converge", "The DC linear solve did not converge.",
                "Review connectivity, mesh resolution, and solver diagnostics.",
                confidence=EvidenceConfidence.DETERMINISTIC,
            ))
        differences = list(data.get("saved_board_differences") or ())
        if differences:
            findings.append(_finding(
                "DC-004", index, "MODEL_INPUT", FindingSeverity.MEDIUM,
                f"{rail} was solved on the saved board, which differs from the open one",
                "The DC mesh was built from the saved board file, and the open board no longer "
                "matches it: " + "; ".join(differences) + ". The figures above describe the "
                "saved copper.",
                "Save the board and rerun the DC analysis.",
                confidence=EvidenceConfidence.DETERMINISTIC,
            ))
        if drop_pct > float(maximum_drop_pct):
            findings.append(_finding(
                "DC-003", index, "VOLTAGE_DROP", FindingSeverity.HIGH,
                f"{rail} exceeds the voltage-drop target",
                f"{drop_text} versus {maximum_drop_pct:.3f}% allowed.",
                "Increase copper cross-section or reduce path/load resistance.",
            ))
        grid_check = data.get("grid_sensitivity") or {}
        if "delta_pct" in grid_check:
            check_metrics, check_findings = _dc_grid_sensitivity(
                rail, index, grid_check, maximum_drop_pct, source_voltage,
            )
            metrics.extend(check_metrics)
            findings.extend(check_findings)
            grid_checked.append(float(grid_check["delta_pct"]))
        elif "below_budget_share" in grid_check:
            grid_not_refined.append((str(rail), float(grid_check["below_budget_share"])))
        # Every DC finding is about its rail, and says so. The findings carried
        # no net: attach_dc_remediations finds a finding's rail by its nets, so
        # no voltage-drop finding the adapter produced was ever given a sized
        # fix, and the campaign could not group DC findings by net either.
        for finding in findings[first_finding:]:
            finding.nets = [str(rail)]
    hybrid_rails = [data for data in (system_results or {}).values() if data.get("engine") == "hybrid"]
    if hybrid_rails:
        geometry_evidence = AnalysisEvidence(
            "PCB_GEOMETRY",
            "The resistive mesh is built from the saved board file: analytic tracks, via and "
            "plated-hole barrels, cut-cell pours and pad lands. Sources and loads are held on "
            "every node inside their pads.",
            reference=Path(str(hybrid_rails[0].get("board_path") or "saved board file")).name,
        )
    else:
        geometry_evidence = AnalysisEvidence(
            "PCB_GEOMETRY",
            "The resistive mesh uses a detached snapshot of live-board copper, pads, vias, and zones.",
            reference="KiCad IPC snapshot",
        )
    return AnalysisResult(
        "DC", "DC Power",
        status=AnalysisStatus.NO_DATA if not system_results else (AnalysisStatus.WARN if findings else AnalysisStatus.PASS),
        summary={
            "rail_count": len(system_results or {}),
            "maximum_drop_pct": float(maximum_drop_pct),
            "current_density_hotspots": density_hotspots,
            "engine": hybrid_rails[0].get("engine") if hybrid_rails else (
                next(iter(system_results.values())).get("engine", "raster") if system_results else None
            ),
        },
        findings=findings, metrics=metrics,
        provenance=[
            geometry_evidence,
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
            f"Copper is modelled at {COPPER_RESISTIVITY_OHM_M:.3g} ohm-m, its resistivity at "
            f"{COPPER_RESISTIVITY_REFERENCE_C:g} C; the DC solve applies no temperature correction, "
            "which the coupled electro-thermal analysis does.",
            "Connector and contact resistances are included only when explicitly modeled.",
            # Both open since the 2026-09-13 audit and stated nowhere in the
            # drop's own limitations (audit-2026-09-23.md, DC section).
            "The drop is the supply side only: the ground return is not solved, so ground "
            "bounce between a load and its source is not included.",
            f"Via and plated-hole barrels use {PLATING_THICKNESS_FALLBACK_MM * 1000.0:g} um of "
            "plating, since the board file does not record it; a thinner finish raises their "
            "resistance in proportion.",
            "Current density is a DC mesh diagnostic, not an IPC ampacity certification; AC skin/proximity effects, contact details, and measured copper temperature are excluded.",
        ] + raster_limitations + _dc_grid_check_limitations(grid_checked, grid_not_refined),
    ).finish()


def _dc_grid_check_limitations(checked_deltas_pct, not_refined):
    """What the grid check covered, for a result whose rails it did not all refine."""
    limitations = []
    if checked_deltas_pct:
        limitations.append(
            f"The worst-load drop of {len(checked_deltas_pct)} rail(s) was solved again at half the "
            f"grid step and moved by at most {max(checked_deltas_pct):.1f}%."
        )
    if not_refined:
        share = max(item[1] for item in not_refined)
        names = [item[0] for item in not_refined]
        limitations.append(
            f"{len(names)} rail(s) were not solved at half the grid step, their drop being under "
            f"{share:.0%} of the budget, where no plausible grid error changes the verdict: "
            + ", ".join(names[:8]) + (", ..." if len(names) > 8 else "") + "."
        )
    return limitations
