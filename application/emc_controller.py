"""Background orchestration for EMI/EMC pre-compliance analysis."""

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from emc_analyzer import EMCAnalyzer
from em_field_solver import EMNearFieldSolver
from emc_phase10 import EMCPhase10Pipeline, board_circuit_sources
from models import EMCFinding
from application.background_controller import BackgroundAnalysisController


class EMCAnalysisCancelled(RuntimeError):
    pass


# SPICE audits whose model Phase 10 could not run, beyond a missing file
# (PH-001) and an unstable transient (PH-002).
_UNUSABLE_SPICE_STATUSES = {
    "MAPPING_VERIFIED_PSPICE_ONLY", "AVAILABLE_PSPICE_ONLY", "MAPPING_VERIFIED_NGSPICE_PROBE_FAILED",
    "PSPICE_BENCH_FAILED", "MAPPING_VERIFIED_PSPICE_BENCH_FAILED",
    "INCOMPATIBLE", "LIBRARY_UNAVAILABLE", "COMPONENT_NOT_IDENTIFIED",
}
# Region outcomes where Phase 10's full-wave solve gave no usable field.
_FAILED_REGION_STATUSES = {
    "FAILED", "TIMEOUT", "VALIDATION_FAILED", "SKIPPED_CELL_LIMIT", "SKIPPED_TOOL_MISSING",
    "SOLVED_FIELDS_UNCALIBRATED_NOT_CONVERGED",
}


def _phase10_spice_findings(phase10_result):
    """EMCFindings for SPICE excitation models Phase 10 could not trust.

    Old audit item A: Phase 10 outcomes never became EMCFindings, so a
    missing or unstable model was visible only in phase10-result.json and
    the run log, not in the report the score and action list are built
    from. INFO severity, since Phase 10 runs after EMCAnalyzer._finalize()
    already fixed this run's score and severity counts, and a missing
    model is a data-availability gap in the excitation, not a new
    geometry/physics risk for the score to weigh (SEVERITY_WEIGHT gives
    INFO 0, so this cannot silently change a score already computed).
    """
    findings = []
    for audit in getattr(phase10_result, "spice_model_audit", None) or []:
        if audit.status == "MODEL_FILE_MISSING":
            findings.append(EMCFinding(
                "PH-001", "SWITCHING", "INFO",
                "Phase 10 SPICE model unavailable",
                f"{audit.component_ref} ({audit.mpn or 'MPN unknown'}), used by {audit.source_name}, "
                "has no resolvable SPICE model file"
                + (f": {audit.notes}" if audit.notes else "."),
                "Add the manufacturer's SPICE model to the library search path, or supply one manually.",
                "HIGH", [], [audit.component_ref],
            ))
        elif "UNSTABLE" in str(audit.status or "").upper():
            findings.append(EMCFinding(
                "PH-002", "SWITCHING", "INFO",
                "Phase 10 SPICE model is transient-unstable",
                f"{audit.component_ref} ({audit.mpn or 'MPN unknown'})'s model, used by {audit.source_name}, "
                f"did not produce a stable ngspice transient ({audit.status}).",
                "Verify the model's convergence settings or substitute a validated model before "
                "trusting its excitation.",
                "HIGH", [], [audit.component_ref],
            ))
        elif str(audit.status or "").upper() in _UNUSABLE_SPICE_STATUSES:
            findings.append(EMCFinding(
                "PH-003", "SWITCHING", "INFO",
                "Phase 10 SPICE model not usable",
                f"{audit.component_ref} ({audit.mpn or 'MPN unknown'})'s model, used by {audit.source_name}, "
                f"could not drive the excitation ({audit.status})"
                + (f": {audit.notes}" if audit.notes else "."),
                "Supply an ngspice-compatible model with a verified pin mapping.",
                "HIGH", [], [audit.component_ref] if audit.component_ref else [],
            ))
    circuits = getattr(phase10_result, "board_circuits", None) or []
    for circuit in circuits:
        if circuit.disagreements:
            findings.append(EMCFinding(
                "PH-005", "SWITCHING", "INFO",
                "Switching source disagrees with its board circuit",
                f"{circuit.component_ref} ({circuit.model_name}), run in the saved board's circuit from "
                f"{circuit.input_v:g} V into {circuit.load_a:g} A, contradicts {circuit.source_name}: "
                + "; ".join(circuit.disagreements) + ".",
                "Set the source to the circuit's values, or correct the circuit if the source "
                "states the intent.",
                "HIGH", [], [circuit.component_ref],
            ))
        elif circuit.status in {"NOT_SWITCHING", "NOT_STEADY"}:
            state = "did not switch" if circuit.status == "NOT_SWITCHING" else "had not settled"
            findings.append(EMCFinding(
                "PH-006", "SWITCHING", "INFO",
                "Converter not operating in its board circuit",
                f"{circuit.component_ref} ({circuit.model_name}), run in the saved board's circuit for "
                f"{circuit.source_name}, {state} after {circuit.simulated_s * 1e3:.3g} ms"
                + (f": {'; '.join(circuit.notes)}" if circuit.notes else "."),
                "Check the converter's enable, frequency and feedback pins on the board, then "
                "whether the model supports how they are wired.",
                "HIGH", [], [circuit.component_ref],
            ))
    failures = [
        (f"excitation {item.source_name}", str(item.status))
        for item in getattr(phase10_result, "excitations", None) or []
        if str(item.status).upper() == "FAILED"
    ] + [
        (f"board circuit {item.source_name}", str(item.status))
        for item in circuits if item.status in {"FAILED", "TIMEOUT"}
    ] + [
        (f"region {item.name}", str(item.status))
        for item in getattr(phase10_result, "regions", None) or []
        if str(item.status).upper() in _FAILED_REGION_STATUSES
    ]
    for what, status in failures:
        findings.append(EMCFinding(
            "PH-004", "SWITCHING", "INFO",
            f"Phase 10 {what} gave no usable result",
            f"The Phase 10 {what} ended {status}; the rule-based findings it was to check "
            "stand without its simulation.",
            "See the Phase 10 output directory for the solver log.",
            "HIGH", [], [],
        ))
    return findings


@dataclass(frozen=True)
class EMCRunRequest:
    settings: Any
    pairs: Tuple[Any, ...]
    snapshot: Any
    ac_results: Tuple[Any, ...] = ()
    differential_results: Optional[Dict[str, Any]] = None
    thermal_result: Any = None
    debug: bool = False
    board_file_path: Optional[str] = None
    plot_lock: Any = None


@dataclass(frozen=True)
class EMCRunOutcome:
    settings: Any
    result: Any
    risk_png: Any
    spectrum_png: Any
    field_e_png: Any = None
    field_h_png: Any = None


@dataclass(frozen=True)
class EMCControllerCallbacks:
    on_progress: Callable[[int, int, str], None]
    on_complete: Callable[[EMCRunOutcome], None]
    on_error: Callable[[Exception], None]
    on_log: Callable[[str], None] = lambda _message: None


class EMCSolverEngine:
    def __init__(
        self, analyzer_factory=EMCAnalyzer, field_solver_factory=EMNearFieldSolver,
        phase10_factory=EMCPhase10Pipeline, plotter_factory=None,
    ):
        self._analyzer_factory = analyzer_factory
        self._field_solver_factory = field_solver_factory
        self._phase10_factory = phase10_factory
        self._plotter_factory = plotter_factory

    def solve(self, request, emit, progress, cancelled):
        def checked_progress(completed, total, detail):
            if cancelled():
                raise EMCAnalysisCancelled("EMI/EMC analysis cancelled.")
            progress(completed, total, str(detail))

        # Phase 10's board circuits run first: the spectrum below takes each
        # converter's simulated frequency, swing and switched current. The
        # outcome carries these settings, which describe the run; the
        # configured ones stay as they are.
        settings, phase10 = request.settings, None
        if settings.phase10.enabled:
            try:
                phase10 = self._phase10_factory(
                    request.snapshot,
                    request.settings,
                    None,
                    board_file_path=request.board_file_path,
                    log_callback=emit,
                    cancellation_callback=cancelled,
                )
                circuits = phase10.simulate_circuits().board_circuits
                settings, revalued = board_circuit_sources(settings, circuits)
                for line in revalued:
                    emit(f"[PHASE 10] Spectrum source from its board circuit: {line}")
            except EMCAnalysisCancelled:
                raise
            except Exception as exc:
                if cancelled():
                    raise EMCAnalysisCancelled("EMI/EMC analysis cancelled.") from exc
                emit(f"[PHASE 10] Board circuits skipped before the analysis: {exc}")
        if cancelled():
            raise EMCAnalysisCancelled("EMI/EMC analysis cancelled.")

        analyzer = self._analyzer_factory(
            request.snapshot,
            settings,
            differential_pairs=list(request.pairs),
            differential_results=dict(request.differential_results or {}),
            ac_results=list(request.ac_results),
            thermal_result=request.thermal_result,
            log_callback=emit,
        )
        result = analyzer.analyze(progress_callback=checked_progress)
        if cancelled():
            raise EMCAnalysisCancelled("EMI/EMC analysis cancelled.")

        field_result = None
        if request.settings.field_simulation_enabled:
            try:
                field_solver = self._field_solver_factory(
                    request.snapshot, settings, log_callback=emit,
                )
                field_result = field_solver.solve(progress_callback=checked_progress)
                result.field_simulation = field_result
                result.elapsed_seconds += field_result.elapsed_seconds
                result.limitations.extend([
                    "Near-field E/H maps use quasi-static vector line-charge and Biot-Savart conductor elements; they are not a full-wave Maxwell solution.",
                    "Differential cancellation and continuous adjacent-GND return are approximated; finite-plane current spreading, dielectric boundaries, phase and enclosure scattering are not solved.",
                    "Inductor H fields use a one-turn package-area magnetic-dipole estimate driven by calculated ripple harmonics; hidden winding geometry is not reconstructed.",
                    "A shielded part receives no numerical field reduction unless a manufacturer curve or user measurement supplies an attenuation value.",
                ])
            except EMCAnalysisCancelled:
                raise
            except Exception as exc:
                warning = f"Near-field simulation skipped: {exc}"
                emit(f"[EM FIELD] {warning}")
                result.limitations.append(warning)
        if cancelled():
            raise EMCAnalysisCancelled("EMI/EMC analysis cancelled.")

        if request.settings.phase10.enabled:
            try:
                if phase10 is None:
                    phase10 = self._phase10_factory(
                        request.snapshot,
                        request.settings,
                        result,
                        board_file_path=request.board_file_path,
                        log_callback=emit,
                        cancellation_callback=cancelled,
                    )
                phase10.analysis_result = result
                phase10_result = phase10.run()
                result.phase10_result = phase10_result
                result.elapsed_seconds += phase10_result.elapsed_seconds
                result.limitations.extend(phase10_result.limitations)
                result.findings.extend(_phase10_spice_findings(phase10_result))
            except EMCAnalysisCancelled:
                raise
            except Exception as exc:
                if cancelled():
                    raise EMCAnalysisCancelled("EMI/EMC analysis cancelled.") from exc
                warning = f"Phase 10 skipped: {exc}"
                emit(f"[PHASE 10] {warning}")
                result.limitations.append(warning)
        if cancelled():
            raise EMCAnalysisCancelled("EMI/EMC analysis cancelled.")

        plotter_factory = self._plotter_factory
        if plotter_factory is None:
            from plotter import Plotter
            plotter_factory = Plotter
        plotter = plotter_factory(debug=request.debug)

        def render():
            risk = plotter.plot_emc_risk_map(
                request.snapshot, result, as_png=True, with_click_probe=True,
            )
            spectrum = plotter.plot_emc_spectrum(
                result, request.settings.frequency_start_hz,
                request.settings.frequency_stop_hz, as_png=True, with_click_probe=True,
            )
            field_e = plotter.plot_em_field(
                field_result, "E", as_png=True, with_hover_probe=True,
            ) if field_result is not None else None
            field_h = plotter.plot_em_field(
                field_result, "H", as_png=True, with_hover_probe=True,
            ) if field_result is not None else None
            return risk, spectrum, field_e, field_h

        if request.plot_lock is None:
            plots = render()
        else:
            with request.plot_lock:
                plots = render()
        if cancelled():
            raise EMCAnalysisCancelled("EMI/EMC analysis cancelled.")
        return EMCRunOutcome(settings, result, *plots)


class EMCAnalysisController(BackgroundAnalysisController):
    def __init__(self, dispatch=lambda callback, *args: callback(*args), engine=None):
        super().__init__(
            engine or EMCSolverEngine(),
            thread_name="KiPIDA-EMI-EMC",
            busy_message="An EMI/EMC analysis is already running.",
            cancelled_error_factory=lambda: EMCAnalysisCancelled(
                "EMI/EMC analysis cancelled."
            ),
            dispatch=dispatch,
        )
