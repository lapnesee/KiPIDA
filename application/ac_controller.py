"""UI-independent orchestration for AC analysis and decoupling optimization."""

from dataclasses import dataclass
from typing import Any, Callable, Optional

from ac_solver import ACSolver
from decoupling_optimizer import DecouplingOptimizer
from application.background_controller import BackgroundAnalysisController


class ACAnalysisCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ACRunRequest:
    settings: Any
    network: Any
    compute_settings: Any = None
    debug: bool = False
    optimize: bool = False


@dataclass(frozen=True)
class ACControllerCallbacks:
    on_progress: Callable[[int, int, Any], None]
    on_complete: Callable[[Any, Optional[Any]], None]
    on_error: Callable[[Exception], None]
    on_log: Callable[[str], None] = lambda _message: None


class ACSolverEngine:
    def __init__(self, solver_factory=ACSolver, optimizer_factory=DecouplingOptimizer):
        self._solver_factory = solver_factory
        self._optimizer_factory = optimizer_factory

    def solve(self, request, emit, progress, cancelled):
        def checked_progress(completed, total, detail):
            if cancelled():
                raise ACAnalysisCancelled("AC analysis cancelled.")
            progress(completed, total, detail)

        solver = self._solver_factory(
            debug=request.debug,
            log_callback=emit,
            compute_settings=request.compute_settings,
        )
        if request.optimize:
            optimizer = self._optimizer_factory(
                solver, debug=request.debug, log_callback=emit,
            )
            optimization = optimizer.optimize(
                request.network, request.settings, progress_callback=checked_progress,
            )
            if cancelled():
                raise ACAnalysisCancelled("AC analysis cancelled.")
            return optimization.baseline, optimization
        result = solver.solve_sweep(
            request.network, request.settings, progress_callback=checked_progress,
        )
        if cancelled():
            raise ACAnalysisCancelled("AC analysis cancelled.")
        return result, None


class ACAnalysisController(BackgroundAnalysisController):
    """Own one background AC job and marshal all callbacks through ``dispatch``."""

    def __init__(
        self,
        dispatch: Callable = lambda callback, *args: callback(*args),
        solver_factory: Callable = ACSolver,
        optimizer_factory: Callable = DecouplingOptimizer,
    ):
        engine = ACSolverEngine(solver_factory, optimizer_factory)
        super().__init__(
            engine,
            thread_name="KiPIDA-AC-Analysis",
            busy_message="An AC analysis is already running.",
            cancelled_error_factory=lambda: ACAnalysisCancelled("AC analysis cancelled."),
            dispatch=dispatch,
        )

    def _run(self, request, callbacks):
        """Unpack the AC engine's two-value outcome for the existing UI contract."""
        original_complete = callbacks.on_complete
        wrapped = ACControllerCallbacks(
            on_progress=callbacks.on_progress,
            on_complete=lambda outcome: original_complete(*outcome),
            on_error=callbacks.on_error,
            on_log=callbacks.on_log,
        )
        super()._run(request, wrapped)
