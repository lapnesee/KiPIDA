"""UI-independent orchestration for AC analysis and decoupling optimization."""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional, Tuple

from ac_solver import ACSolver
from decoupling_optimizer import DecouplingOptimizer
from application.background_controller import BackgroundAnalysisController
from application.dc_controller import capture_dc_board, live_layer_ids, saved_board_differences
from ingest.board_reader import read_board


class ACAnalysisCancelled(RuntimeError):
    pass


def saved_board_for_ac(board_path, live_board, live_stackup, net_names, emit=lambda _message: None):
    """``(parsed board, layer mapping)`` for a hybrid AC network, or ``(None, None)``.

    The hybrid mesh reads the saved board file, as the DC report's does. A
    file that cannot be read, whose copper layers do not all pair with the
    open board's, or whose vias and pads on the swept nets differ from the
    open board's is not the board on screen: the network is then built on
    the open board's raster mesh, and *emit* says why.
    """
    fallback = "the AC network uses the raster mesh of the open board"
    if not board_path:
        emit(f"The board file is not known: {fallback}.")
        return None, None
    name = Path(board_path).name
    try:
        board = read_board(Path(board_path))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        emit(f"{name} could not be read ({exc}): {fallback}.")
        return None, None
    mapping, _positional, unmatched = live_layer_ids(board.stackup, live_stackup)
    if unmatched:
        emit(f"{name} has copper layers the open board lacks ({', '.join(unmatched)}): {fallback}.")
        return None, None
    capture = SimpleNamespace(board=capture_dc_board(live_board), classification_geometry_by_rail=None)
    differences = [
        f"{net}: {difference}"
        for net in net_names
        for difference in saved_board_differences(board, net, capture, mapping)
    ]
    if differences:
        emit(f"The open board differs from {name} ({'; '.join(differences)}): {fallback}.")
        return None, None
    return board, mapping


@dataclass(frozen=True)
class ACRunRequest:
    settings: Any
    network: Any
    compute_settings: Any = None
    debug: bool = False
    optimize: bool = False


@dataclass(frozen=True)
class ACBatchRequest:
    """Every configured AC profile of a campaign, swept one after another.

    A campaign swept only the rail on screen: P02 Rev E has 3 AC profiles,
    and a campaign scored AC on one of them (AC-10).
    """
    requests: Tuple[ACRunRequest, ...]
    # (rail, reason) for each configured profile that could not be swept.
    skipped: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ACBatchOutcome:
    # (request, sweep result, optimization) per swept profile.
    runs: Tuple[Tuple[Any, Any, Any], ...]
    skipped: Tuple[Tuple[str, str], ...] = ()


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
        if isinstance(request, ACBatchRequest):
            runs = []
            for index, item in enumerate(request.requests, start=1):
                emit(
                    f"AC profile {index}/{len(request.requests)}: "
                    f"{getattr(item.settings, 'rail_name', '')}."
                )
                runs.append((item, *self.solve(item, emit, progress, cancelled)))
            return ACBatchOutcome(tuple(runs), request.skipped)

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
        # More than one candidate observation point means the rail's worst port
        # is not known before solving, so sweep them all rather than reporting
        # whichever one happened to be listed first.
        ports = getattr(request.network, "ports", None) or {}
        if len(ports) > 1 and hasattr(solver, "solve_sweep_multiport"):
            result = solver.solve_sweep_multiport(
                request.network, request.settings, progress_callback=checked_progress,
            )
        else:
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
