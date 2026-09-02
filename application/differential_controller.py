"""Background orchestration for differential-pair impedance analysis."""

from dataclasses import dataclass
from typing import Any, Callable, Tuple

from differential_impedance import DifferentialImpedanceSolver
from differential_recommender import DifferentialRecommendationEngine
from application.background_controller import BackgroundAnalysisController


class DifferentialAnalysisCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class DifferentialRunRequest:
    settings: Any
    pairs: Tuple[Any, ...]
    stackup: Any
    snapshot: Any
    debug: bool = False
    plot_lock: Any = None


@dataclass(frozen=True)
class DifferentialRunOutcome:
    results: Tuple[Any, ...]
    stackup: Any
    impedance_png: Any
    stackup_png: Any
    target_tolerance_pct: float


@dataclass(frozen=True)
class DifferentialControllerCallbacks:
    on_progress: Callable[[int, int, str], None]
    on_complete: Callable[[DifferentialRunOutcome], None]
    on_error: Callable[[Exception], None]
    on_log: Callable[[str], None] = lambda _message: None


class DifferentialSolverEngine:
    def __init__(
        self, solver_factory=DifferentialImpedanceSolver,
        recommendation_factory=DifferentialRecommendationEngine,
        plotter_factory=None,
    ):
        self._solver_factory = solver_factory
        self._recommendation_factory = recommendation_factory
        self._plotter_factory = plotter_factory

    def solve(self, request, emit, progress, cancelled):
        def checked_progress(completed, total, detail):
            if cancelled():
                raise DifferentialAnalysisCancelled("Differential analysis cancelled.")
            progress(completed, total, str(detail))

        solver = self._solver_factory(
            request.snapshot, request.stackup, request.settings,
            log_callback=emit,
        )
        results = solver.solve(list(request.pairs), progress_callback=checked_progress)
        if cancelled():
            raise DifferentialAnalysisCancelled("Differential analysis cancelled.")
        self._recommendation_factory(request.settings).recommend(results)
        plotter_factory = self._plotter_factory
        if plotter_factory is None:
            # Keep wx/matplotlib out of controller import time.  This also
            # preserves headless test discovery and makes rendering replaceable.
            from plotter import Plotter
            plotter_factory = Plotter
        plotter = plotter_factory(debug=request.debug)

        def render():
            return (
                plotter.plot_differential_impedance(
                    results, as_png=True,
                    target_tolerance_pct=request.settings.target_tolerance_pct,
                ),
                plotter.plot_stackup_profile(request.stackup, as_png=True),
            )

        if request.plot_lock is None:
            impedance_png, stackup_png = render()
        else:
            with request.plot_lock:
                impedance_png, stackup_png = render()
        if cancelled():
            raise DifferentialAnalysisCancelled("Differential analysis cancelled.")
        return DifferentialRunOutcome(
            results=tuple(results),
            stackup=request.stackup,
            impedance_png=impedance_png,
            stackup_png=stackup_png,
            target_tolerance_pct=float(request.settings.target_tolerance_pct),
        )


class DifferentialAnalysisController(BackgroundAnalysisController):
    def __init__(self, dispatch=lambda callback, *args: callback(*args), engine=None):
        super().__init__(
            engine or DifferentialSolverEngine(),
            thread_name="KiPIDA-Differential-Impedance",
            busy_message="A differential analysis is already running.",
            cancelled_error_factory=lambda: DifferentialAnalysisCancelled(
                "Differential analysis cancelled."
            ),
            dispatch=dispatch,
        )
