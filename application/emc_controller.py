"""Background orchestration for EMI/EMC pre-compliance analysis."""

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from emc_analyzer import EMCAnalyzer
from em_field_solver import EMNearFieldSolver
from emc_phase10 import EMCPhase10Pipeline
from application.background_controller import BackgroundAnalysisController


class EMCAnalysisCancelled(RuntimeError):
    pass


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

        analyzer = self._analyzer_factory(
            request.snapshot,
            request.settings,
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
                    request.snapshot, request.settings, log_callback=emit,
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
                phase10_result = self._phase10_factory(
                    request.snapshot,
                    request.settings,
                    result,
                    board_file_path=request.board_file_path,
                    log_callback=emit,
                    cancellation_callback=cancelled,
                ).run()
                result.phase10_result = phase10_result
                result.elapsed_seconds += phase10_result.elapsed_seconds
                result.limitations.extend(phase10_result.limitations)
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
        return EMCRunOutcome(request.settings, result, *plots)


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
