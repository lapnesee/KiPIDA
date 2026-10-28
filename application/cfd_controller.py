"""Background orchestration for enclosure CFD analysis."""

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Tuple

from application.dc_controller import DefaultDCSolverEngine
from application.thermal_controller import dc_copper_loss_points, describe_dc_source
from conjugate_heat_transfer import ConjugateHeatTransferSolver
from application.background_controller import BackgroundAnalysisController


class CFDAnalysisCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class CFDRunRequest:
    board_model: Any
    settings: Any
    compute_settings: Any = None
    debug: bool = False
    dc_request: Any = None
    plot_lock: Any = None
    # As on ThermalRunRequest: a DC result to take copper losses from, and
    # what it is, which the campaign cache digest leaves to the DC's run_id.
    _dc_results: Any = field(default=None, metadata={"digest": False})
    _dc_results_source: str = field(default="", metadata={"digest": False})


@dataclass(frozen=True)
class CFDRunOutcome:
    mesh: Any
    result: Any
    plots: Tuple[Tuple[str, Any], ...]
    system_results: Any = None
    # Where the copper-loss heat sources came from; empty when there were none.
    copper_loss_source: str = ""


@dataclass(frozen=True)
class CFDControllerCallbacks:
    on_progress: Callable[[int, int, str], None]
    on_complete: Callable[[CFDRunOutcome], None]
    on_error: Callable[[Exception], None]
    on_log: Callable[[str], None] = lambda _message: None


class CFDSolverEngine:
    def __init__(self, solver_factory=ConjugateHeatTransferSolver, dc_engine_factory=DefaultDCSolverEngine,
                 plotter_factory=None):
        self._solver_factory = solver_factory
        self._dc_engine_factory = dc_engine_factory
        self._plotter_factory = plotter_factory

    def solve(self, request, emit, progress, cancelled):
        def checked_progress(completed, total, detail):
            if cancelled():
                raise CFDAnalysisCancelled("Enclosure CFD analysis cancelled.")
            progress(completed, total, str(detail))

        system_results = {}
        copper_losses = []
        copper_loss_source = ""
        if request.settings.include_dc_copper_losses:
            handed_over = getattr(request, "_dc_results", None)
            if handed_over is not None:
                system_results = handed_over
                copper_loss_source = describe_dc_source(
                    system_results, request._dc_results_source or "a DC result handed to this run",
                )
                emit(f"Copper losses taken from {copper_loss_source}; the DC is not solved again.")
            else:
                if request.dc_request is None:
                    raise ValueError("The CFD run is missing its captured DC request.")
                emit("Running fresh DC analysis for enclosure copper-loss heat sources.")
                system_results = self._dc_engine_factory().solve(
                    request.dc_request, emit, checked_progress, cancelled,
                )
                copper_loss_source = describe_dc_source(system_results, "a DC solve run for this analysis")
            copper_losses = dc_copper_loss_points(system_results)
        board_model = replace(request.board_model, copper_losses=list(copper_losses))
        if not request.settings.use_phase3_heat_sources:
            board_model = replace(board_model, components=[], copper_losses=[])
        if cancelled():
            raise CFDAnalysisCancelled("Enclosure CFD analysis cancelled.")

        solver = self._solver_factory(
            debug=request.debug, log_callback=emit,
            compute_settings=request.compute_settings,
        )
        mesh, result = solver.solve(
            board_model, request.settings,
            progress_callback=checked_progress, cancel_callback=cancelled,
        )
        if cancelled():
            raise CFDAnalysisCancelled("Enclosure CFD analysis cancelled.")

        plotter_factory = self._plotter_factory
        if plotter_factory is None:
            from plotter import Plotter
            plotter_factory = Plotter
        plotter = plotter_factory(debug=request.debug)

        def render():
            return (
                ("CFD 3D", plotter.plot_cfd_3d(mesh, result, as_png=True)),
                ("Temperature XY", plotter.plot_cfd_slice(mesh, result, "TEMPERATURE", "XY", as_png=True)),
                ("Temperature XZ", plotter.plot_cfd_slice(mesh, result, "TEMPERATURE", "XZ", as_png=True)),
                ("Velocity XY", plotter.plot_cfd_slice(mesh, result, "VELOCITY", "XY", as_png=True)),
                ("Pressure XY", plotter.plot_cfd_slice(mesh, result, "PRESSURE", "XY", as_png=True)),
                ("Residuals", plotter.plot_cfd_residuals(result, as_png=True)),
            )

        if request.plot_lock is None:
            plots = render()
        else:
            with request.plot_lock:
                plots = render()
        if cancelled():
            raise CFDAnalysisCancelled("Enclosure CFD analysis cancelled.")
        return CFDRunOutcome(mesh, result, plots, system_results, copper_loss_source)


class CFDAnalysisController(BackgroundAnalysisController):
    def __init__(self, dispatch=lambda callback, *args: callback(*args), engine=None):
        super().__init__(
            engine or CFDSolverEngine(),
            thread_name="KiPIDA-Enclosure-CFD",
            busy_message="An enclosure CFD analysis is already running.",
            cancelled_error_factory=lambda: CFDAnalysisCancelled(
                "Enclosure CFD analysis cancelled."
            ),
            dispatch=dispatch,
        )
