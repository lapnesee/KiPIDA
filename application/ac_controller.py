"""UI-independent orchestration for AC analysis and decoupling optimization."""

from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional, Tuple

from ac_model import ACModelBuilder
from ac_solver import ACSolver
from analysis_adapters import frequency_text
from decoupling_optimizer import DecouplingOptimizer
from application.background_controller import BackgroundAnalysisController
from application.dc_controller import capture_dc_board, live_layer_ids, saved_board_differences
from ingest.board_reader import read_board


class ACAnalysisCancelled(RuntimeError):
    pass


# The saved board last parsed, as (key, board), keyed on its path, size and
# modification time. A campaign prepares its AC profiles one after another on
# the UI thread, and each parsed P02 Rev E's file again: 0.9 s, three times.
_SAVED_BOARD = []


def _read_saved_board(path):
    """read_board(*path*), parsed again only when the file has changed."""
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    if _SAVED_BOARD and _SAVED_BOARD[0][0] == key:
        return _SAVED_BOARD[0][1]
    board = read_board(path)
    _SAVED_BOARD[:] = [(key, board)]
    return board


def saved_board_for_ac(board_path, live_board, live_stackup, net_names, emit=lambda _message: None):
    """``(parsed board, layer mapping, pad snapshot)`` for a hybrid AC network, or Nones.

    The hybrid mesh reads the saved board file, as the DC report's does. A
    file that cannot be read, whose copper layers do not all pair with the
    open board's, or whose vias and pads on the swept nets differ from the
    open board's is not the board on screen: the network is then built on
    the open board's raster mesh, and *emit* says why.
    """
    fallback = "the AC network uses the raster mesh of the open board"
    if not board_path:
        emit(f"The board file is not known: {fallback}.")
        return None, None, None
    name = Path(board_path).name
    try:
        board = _read_saved_board(Path(board_path))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        emit(f"{name} could not be read ({exc}): {fallback}.")
        return None, None, None
    mapping, _positional, unmatched = live_layer_ids(board.stackup, live_stackup)
    if unmatched:
        emit(f"{name} has copper layers the open board lacks ({', '.join(unmatched)}): {fallback}.")
        return None, None, None
    capture = SimpleNamespace(board=capture_dc_board(live_board), classification_geometry_by_rail=None)
    differences = [
        f"{net}: {difference}"
        for net in net_names
        for difference in saved_board_differences(board, net, capture, mapping)
    ]
    if any("live capture empty" in item for item in differences):
        # Not a different board: no board at all yet. Falling back to the
        # raster mesh of that empty capture only failed later with "No copper
        # geometry found" (P02 Rev E, 2026-09-29 16:51).
        raise ValueError(
            "KiCad IPC returned an empty board; wait for PCB Editor synchronisation and retry"
        )
    if differences:
        emit(f"The open board differs from {name} ({'; '.join(differences)}): {fallback}.")
        return None, None, None
    return board, mapping, capture.board


@dataclass(frozen=True)
class ACRunRequest:
    settings: Any
    network: Any
    compute_settings: Any = None
    debug: bool = False
    optimize: bool = False
    # What a hybrid network is built from -- parsed board, layer mapping,
    # stackup, pad snapshot, rail, all rails, grid. With ``network`` None the
    # engine builds it (build_ac_network), off the UI thread; _grid_check
    # builds it again at half the step (ac_grid_check). Private: kept out of
    # the campaign cache digest.
    _build: Any = None
    _grid_check: Any = None


# Time, not memory, bounds an AC sweep: on P02 Rev E's /VBUS_SYS the
# recycled LU took 1.6 s a point and 0.34 GB at 102,401 nodes, 5.1 s and
# 1.15 GB at 368,315. The limit was 100,000, which the hybrid mesh of that
# rail passed at the default 0.5 mm, dropping AC from a campaign.
AC_MAX_NETWORK_NODES = 400000
# Largest network the half-step check builds; past it the check is skipped.
AC_GRID_CHECK_MAX_NODES = AC_MAX_NETWORK_NODES


def build_ac_network(inputs, settings, emit=lambda _message: None):
    """The hybrid AC network of *settings*' rail, from a request's _build inputs.

    Meshing the saved board's rail and ground took 16 s of the UI thread
    per profile in the P02 Rev E campaign of 2026-09-29 14:19, before the
    run started; the inputs need nothing live, so the engine builds here.
    """
    network = ACModelBuilder(inputs.board, log_callback=emit).build(
        inputs.rail, settings, grid_size_mm=float(inputs.grid_mm), all_rails=inputs.all_rails,
        parsed_board=inputs.parsed_board, layer_mapping=inputs.layer_mapping,
        stackup=inputs.stackup,
    )
    emit(
        f"AC network ({network.mesh_engine} mesh) for {settings.rail_name}: "
        f"{network.node_count:,} nodes, effective grid {network.effective_grid_size_mm:g} mm, "
        f"{settings.frequency_points} frequency points."
    )
    if network.node_count > AC_MAX_NETWORK_NODES:
        raise ValueError(
            f"AC mesh contains {network.node_count:,} nodes, above the safe limit of "
            f"{AC_MAX_NETWORK_NODES:,}. Increase 'AC mesh resolution' above "
            f"{settings.mesh_resolution_mm:g} mm and retry."
        )
    return network


def ac_grid_check(request, result, solver, emit=lambda _message: None):
    """The worst impedance solved again on a network at half the grid step.

    The AC sweep had no error bar. On P02 Rev E's /VBUS_SYS (hybrid mesh)
    the worst |Z| read 0.248 ohm at 0.5 mm, 0.264 at 0.35 mm and 0.281 at
    0.25 mm: 13 % apart, the mesh not converged. Only the worst frequency
    is solved again -- one or two factorisations -- on a network rebuilt
    from the same saved board and pad snapshot, with the source the sweep
    resolved.
    """
    inputs = request._grid_check
    grid = float(inputs.grid_mm)
    settings = request.settings
    if not getattr(settings.source, "ref_des", "") and getattr(request.network, "source_ref_des", ""):
        settings = replace(settings, source=replace(settings.source, ref_des=request.network.source_ref_des))
    fine = ACModelBuilder(inputs.board).build(
        inputs.rail, settings, grid_size_mm=grid / 2.0, all_rails=inputs.all_rails,
        parsed_board=inputs.parsed_board, layer_mapping=inputs.layer_mapping,
        stackup=inputs.stackup,
    )
    if fine.node_count > AC_GRID_CHECK_MAX_NODES:
        return {"not_checked": f"{fine.node_count:,} nodes at {grid / 2.0:g} mm, over "
                               f"{AC_GRID_CHECK_MAX_NODES:,}"}
    # Where the verdict is decided: the least margin to the allowed
    # impedance, not the worst |Z|. P02 Rev E's /VBUS_SYS, 2026-09-30 00:05,
    # was checked at its 38.3 kHz source resonance, 0.2061 ohm against
    # 0.392 ohm allowed, and not at 100 MHz, where its margin is thinnest.
    critical = float(getattr(result, "critical_frequency_hz", 0.0) or 0.0) > 0.0
    frequency = float(result.critical_frequency_hz if critical else result.worst_frequency_hz)
    point = replace(
        settings, frequency_start_hz=frequency * 0.999, frequency_stop_hz=frequency,
        frequency_points=2,
    )
    checked = solver.solve_sweep_multiport(fine, point)
    value = abs(checked.impedance_ohm[-1])
    worst = float(result.critical_impedance_ohm if critical else result.worst_impedance_ohm)
    delta = abs(value - worst) / worst * 100.0 if worst > 0.0 else 0.0
    emit(
        f"[grid check] {settings.rail_name}: "
        f"{'least-margin' if critical else 'worst'} |Z| {worst * 1e3:.2f} mohm at {grid:g} mm, "
        f"{value * 1e3:.2f} mohm at {grid / 2.0:g} mm ({fine.node_count:,} nodes), "
        f"{delta:.1f} % apart at {frequency_text(frequency)}."
    )
    return {
        "grid_mm": grid, "check_grid_mm": grid / 2.0, "frequency_hz": frequency,
        "impedance_ohm": worst, "check_impedance_ohm": value, "delta_pct": delta,
        "least_margin": critical,
    }


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
            runs, skipped = [], list(request.skipped)
            for index, item in enumerate(request.requests, start=1):
                rail = getattr(item.settings, "rail_name", "")
                emit(f"AC profile {index}/{len(request.requests)}: {rail}.")
                # One profile failing to build or solve skips itself only.
                try:
                    if item.network is None and item._build is not None:
                        item = replace(item, network=build_ac_network(item._build, item.settings, emit))
                    runs.append((item, *self.solve(item, emit, progress, cancelled)))
                except ValueError as exc:
                    emit(f"AC profile '{rail}' not swept: {exc}")
                    skipped.append((rail, str(exc)))
            if not runs:
                raise ValueError(
                    "No AC profile could be swept: "
                    + "; ".join(f"{rail}: {reason}" for rail, reason in skipped)
                )
            return ACBatchOutcome(tuple(runs), tuple(skipped))

        if request.network is None and request._build is not None:
            request = replace(request, network=build_ac_network(request._build, request.settings, emit))

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
        if request._grid_check is not None:
            try:
                result.grid_check = ac_grid_check(request, result, solver, emit)
            except (ValueError, RuntimeError) as exc:
                result.grid_check = {"not_checked": str(exc)}
                emit(f"[grid check] {request.settings.rail_name}: not checked -- {exc}")
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
