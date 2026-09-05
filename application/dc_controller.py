"""Thread-safe preparation and execution of DC power-integrity analyses.

KiCad IPC objects are captured by :func:`prepare_dc_request` on the caller
thread.  The controller worker only receives plain Python objects and Shapely
geometries, so it never calls the live KiCad board API.
"""

from copy import deepcopy
from dataclasses import dataclass
import time
from typing import Any, Callable, Dict, Optional, Tuple

from extractor import GeometryExtractor
from mesh import Mesher
from solver import Solver
from application.background_controller import BackgroundAnalysisController


class DCAnalysisCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class DCPointSnapshot:
    x: float
    y: float


@dataclass(frozen=True)
class DCNetSnapshot:
    name: str


@dataclass(frozen=True)
class DCPadSnapshot:
    number: str
    name: str
    position: DCPointSnapshot
    net: DCNetSnapshot
    pad_type: Any = None
    type: str = ""
    drill_size: Optional[DCPointSnapshot] = None
    layers: Tuple[int, ...] = ()


@dataclass(frozen=True)
class DCFootprintSnapshot:
    reference: str
    pads: Tuple[DCPadSnapshot, ...]


@dataclass(frozen=True)
class DCViaSnapshot:
    net: DCNetSnapshot
    position: DCPointSnapshot
    start: DCPointSnapshot
    width: float
    drill_size: Optional[DCPointSnapshot] = None
    layers: Tuple[int, ...] = ()
    layer_pair: Tuple[int, ...] = ()


@dataclass(frozen=True)
class DCBoardSnapshot:
    footprints: Tuple[DCFootprintSnapshot, ...]
    vias: Tuple[DCViaSnapshot, ...]


@dataclass(frozen=True)
class DCRunRequest:
    rails: Tuple[Any, ...]
    geometry_by_rail: Dict[str, Dict[int, Any]]
    classification_geometry_by_rail: Dict[str, Dict[str, Dict[int, Any]]]
    stackup: Dict[str, Any]
    board: DCBoardSnapshot
    grid_size_mm: float
    compute_settings: Any = None
    debug: bool = False
    # The voltage-drop budget the results are judged against.  The solve does
    # not use it -- adapting the results into findings does -- but it belongs
    # to the run, not to whoever reads it afterwards.  Optional because a DC
    # solve is perfectly valid without a verdict; adapters that need it demand
    # it rather than substituting a number nobody chose.
    maximum_drop_pct: Optional[float] = None
    # The .kicad_pcb this request was captured from.  The solve reads the
    # snapshot rather than the file, but sizing a copper fix needs the offline
    # board, and the run knows where it came from where a later reader does not.
    board_path: Optional[str] = None


@dataclass(frozen=True)
class DCControllerCallbacks:
    on_progress: Callable[[int, int, str], None]
    on_complete: Callable[[Dict[str, Any]], None]
    on_error: Callable[[Exception], None]
    on_log: Callable[[str], None] = lambda _message: None


def _value(obj, name, default=None):
    if obj is None:
        return default
    try:
        value = getattr(obj, name)
        if value is not None:
            return value
    except Exception:
        pass
    method = getattr(obj, f"get_{name}", None)
    if callable(method):
        try:
            value = method()
            if value is not None:
                return value
        except Exception:
            pass
    return default


def _items(board, name):
    method = getattr(board, f"get_{name}", None)
    if callable(method):
        try:
            result = method()
            if result is not None:
                return list(result)
        except Exception:
            pass
    result = getattr(board, name, ())
    get_all = getattr(result, "get_all", None)
    if callable(get_all):
        result = get_all()
    try:
        return list(result or ())
    except TypeError:
        return []


def _point(obj) -> DCPointSnapshot:
    return DCPointSnapshot(float(_value(obj, "x", 0.0)), float(_value(obj, "y", 0.0)))


def _layers(obj) -> Tuple[int, ...]:
    result = []
    for layer in obj or ():
        try:
            result.append(int(layer))
        except (TypeError, ValueError):
            result.append(layer)
    return tuple(result)


def _net_name(item) -> str:
    return str(_value(_value(item, "net"), "name", "") or "")


def _integer_if_possible(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _drill_snapshot(item):
    drill = _value(item, "drill_size", _value(item, "drill"))
    if drill is None:
        drill = _value(_value(item, "padstack"), "drill")
    if drill is None:
        return None
    if isinstance(drill, (int, float)):
        value = float(drill)
        return DCPointSnapshot(value, value)
    size = _value(drill, "size", drill)
    diameter = _value(size, "diameter", _value(drill, "diameter"))
    if diameter is not None:
        # kipy returns DrillProperties.diameter as a Vector2, because a hole
        # may be a milled slot with different X and Y dimensions. A snapshot
        # already carries both axes, so keep them rather than collapsing.
        dx = _value(diameter, "x")
        if dx is not None:
            dy = _value(diameter, "y", dx)
            return DCPointSnapshot(float(dx), float(dy if dy is not None else dx))
        value = float(diameter)
        return DCPointSnapshot(value, value)
    x = _value(size, "x")
    y = _value(size, "y", x)
    if x is None:
        return None
    return DCPointSnapshot(float(x), float(y if y is not None else x))


def capture_dc_board(board) -> DCBoardSnapshot:
    """Detach the footprint, pad, and via fields consumed by the DC worker."""
    footprints = []
    for footprint in _items(board, "footprints"):
        reference = str(_value(footprint, "reference", _value(footprint, "ref_des", "")) or "")
        if not reference:
            field = _value(footprint, "reference_field")
            reference = str(_value(_value(field, "text"), "value", "") or "")
        pads = _value(footprint, "pads")
        if pads is None:
            pads = _value(_value(footprint, "definition"), "pads", ())
        pad_snapshots = []
        for pad in pads or ():
            position = _value(pad, "position")
            if position is None:
                continue
            number = str(_value(pad, "number", _value(pad, "name", "")) or "")
            drill = _value(pad, "drill_size")
            pad_layers = _value(pad, "layers")
            if not pad_layers:
                pad_layers = _value(_value(pad, "padstack"), "layers", ())
            pad_snapshots.append(DCPadSnapshot(
                number=number,
                name=str(_value(pad, "name", number) or number),
                position=_point(position),
                net=DCNetSnapshot(_net_name(pad)),
                pad_type=_integer_if_possible(_value(pad, "pad_type")),
                type=str(_value(pad, "type", "") or ""),
                drill_size=_point(drill) if drill is not None else None,
                layers=_layers(pad_layers),
            ))
        footprints.append(DCFootprintSnapshot(reference, tuple(pad_snapshots)))

    vias = []
    for via in _items(board, "vias"):
        position = _value(via, "start", _value(via, "position"))
        if position is None:
            continue
        via_layers = _value(via, "layers")
        if not via_layers:
            via_layers = _value(_value(via, "padstack"), "layers", ())
        vias.append(DCViaSnapshot(
            net=DCNetSnapshot(_net_name(via)),
            position=_point(position),
            start=_point(position),
            width=float(_value(via, "width", 0.6e6) or 0.6e6),
            drill_size=_drill_snapshot(via),
            layers=_layers(via_layers),
            layer_pair=_layers(_value(via, "layer_pair", ())),
        ))
    return DCBoardSnapshot(tuple(footprints), tuple(vias))


def prepare_dc_request(
    board, rails, grid_size_mm, compute_settings=None, debug=False,
    log_callback=None, board_path=None, maximum_drop_pct=None,
) -> DCRunRequest:
    """Capture every live-board dependency before starting a worker thread."""
    emit = log_callback or (lambda _message: None)
    extractor = GeometryExtractor(
        board, debug=debug, log_callback=emit, board_path=board_path,
    )
    stackup = deepcopy(extractor.get_board_stackup())
    geometry_by_rail = {}
    classification_geometry_by_rail = {}
    for rail in rails:
        started = time.perf_counter()
        geometry_by_rail[rail.net_name] = extractor.get_net_geometry(
            rail.net_name, merge=False,
        )
        classification_geometry_by_rail[rail.net_name] = {
            "track": extractor.get_track_geometry(rail.net_name),
            "zone": extractor.get_zone_geometry(rail.net_name),
        }
        emit(
            f"Captured {rail.net_name} copper geometry on the UI thread in "
            f"{time.perf_counter() - started:.3f} s."
        )
    return DCRunRequest(
        rails=tuple(deepcopy(list(rails))),
        geometry_by_rail=geometry_by_rail,
        classification_geometry_by_rail=classification_geometry_by_rail,
        stackup=stackup,
        board=capture_dc_board(board),
        grid_size_mm=float(grid_size_mm),
        compute_settings=deepcopy(compute_settings),
        debug=bool(debug),
        maximum_drop_pct=(
            None if maximum_drop_pct is None else float(maximum_drop_pct)
        ),
        board_path=(None if board_path is None else str(board_path)),
    )


def _sorted_rails(rails):
    graph = {rail.net_name: [] for rail in rails}
    for rail in rails:
        for regulator in rail.child_regulators:
            if regulator.output_rail_name in graph:
                graph[regulator.output_rail_name].append(rail.net_name)
    visited, active, result = set(), set(), []

    def visit(name):
        if name in active:
            raise ValueError(f"Cycle detected in power rail dependencies involving '{name}'")
        if name in visited:
            return
        visited.add(name)
        active.add(name)
        for dependency in graph.get(name, ()): visit(dependency)
        active.remove(name)
        result.append(name)

    for rail in rails: visit(rail.net_name)
    by_name = {rail.net_name: rail for rail in rails}
    return [by_name[name] for name in reversed(result)]


def _mesh_nodes(mesh, board, ref_des, pad_names, debug=False, emit=lambda _message: None):
    if debug:
        emit(f"  Mapping {ref_des} pads={pad_names}")
    footprint = next((item for item in board.footprints if item.reference == ref_des), None)
    if footprint is None:
        if debug: emit(f"  Warning: Footprint {ref_des} not found.")
        return []
    selected = footprint.pads if not pad_names else tuple(
        pad for pad in footprint.pads if pad.number in pad_names or pad.name in pad_names
    )
    nodes = []
    for pad in selected:
        px, py = pad.position.x, pad.position.y
        if abs(px) > 10000 or abs(py) > 10000:
            px, py = px / 1e6, py / 1e6
        tx = int(round((px - mesh.grid_origin[0]) / mesh.grid_step))
        ty = int(round((py - mesh.grid_origin[1]) / mesh.grid_step))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for layer in range(32):
                    node = mesh.node_map.get((tx + dx, ty + dy, layer))
                    if node is not None:
                        nodes.append(node)
    return list(set(nodes))


class DCSolverEngine:
    def __init__(self, mesher_factory=Mesher, solver_factory=Solver):
        self._mesher_factory = mesher_factory
        self._solver_factory = solver_factory

    def solve(self, request, emit, progress, cancelled):
        rails = list(request.rails)
        emit(f"--- Starting System Simulation ({len(rails)} rails) ---")
        sorted_rails = _sorted_rails(rails)
        emit("Rail solve order: " + " -> ".join(rail.net_name for rail in sorted_rails))
        results = {}
        rail_current = {rail.net_name: 0.0 for rail in rails}
        total = max(1, len(sorted_rails) * 3)
        completed = 0
        for rail in sorted_rails:
            if cancelled(): raise DCAnalysisCancelled("DC analysis cancelled.")
            emit(f"Processing Rail: {rail.net_name} (Sources: {len(rail.sources)}, Loads: {len(rail.loads)})")
            rail_current[rail.net_name] = sum(load.total_current for load in rail.loads)
            geometry = request.geometry_by_rail.get(rail.net_name, {})
            completed += 1
            progress(completed, total, f"{rail.net_name}: geometry")
            if not geometry:
                emit(f"  Skipping {rail.net_name}: No geometry.")
                completed += 2
                progress(completed, total, f"{rail.net_name}: skipped")
                continue
            mesher = self._mesher_factory(
                request.board, debug=request.debug, log_callback=emit,
                compute_settings=request.compute_settings,
            )
            mesh = mesher.generate_mesh(
                rail.net_name, geometry, request.stackup,
                grid_size_mm=request.grid_size_mm,
            )
            completed += 1
            progress(completed, total, f"{rail.net_name}: mesh ({len(mesh.nodes):,} nodes)")
            if cancelled(): raise DCAnalysisCancelled("DC analysis cancelled.")
            if not mesh.nodes:
                emit(f"  Skipping {rail.net_name}: Mesh empty.")
                completed += 1
                progress(completed, total, f"{rail.net_name}: skipped")
                continue

            sources, loads = [], []
            for source in rail.sources:
                nodes = _mesh_nodes(mesh, request.board, source.component_ref.ref_des, source.pad_names, request.debug, emit)
                if not nodes: emit(f"  WARNING: Source {source.component_ref.ref_des} found NO mesh nodes!")
                sources.extend({'node_id': node, 'voltage': rail.nominal_voltage} for node in nodes)
            for parent in rails:
                for regulator in parent.child_regulators:
                    if regulator.output_rail_name == rail.net_name:
                        nodes = _mesh_nodes(mesh, request.board, regulator.output_ref_des, regulator.output_pad_names, request.debug, emit)
                        sources.extend({'node_id': node, 'voltage': rail.nominal_voltage} for node in nodes)
            for load in rail.loads:
                nodes = _mesh_nodes(mesh, request.board, load.component_ref.ref_des, load.pad_names, request.debug, emit)
                if nodes:
                    current = load.total_current / len(nodes)
                    loads.extend({'node_id': node, 'current': current, 'ref_des': load.component_ref.ref_des} for node in nodes)
            for regulator in rail.child_regulators:
                output_current = rail_current.get(regulator.output_rail_name, 0.0)
                if not output_current:
                    continue
                output_rail = next((item for item in rails if item.net_name == regulator.output_rail_name), None)
                if regulator.reg_type == "SWITCHING":
                    output_power = output_current * (output_rail.nominal_voltage if output_rail else 0.0)
                    input_power = output_power / regulator.efficiency if regulator.efficiency > 0 else output_power
                    input_current = input_power / rail.nominal_voltage if rail.nominal_voltage > 0 else 0.0
                else:
                    input_current = output_current
                rail_current[rail.net_name] += input_current
                nodes = _mesh_nodes(mesh, request.board, regulator.input_ref_des, regulator.input_pad_names, request.debug, emit)
                if not nodes:
                    emit(f"  WARNING: Regulator {regulator.name} input found NO mesh nodes!")
                    continue
                current = input_current / len(nodes)
                loads.extend({'node_id': node, 'current': current, 'ref_des': regulator.input_ref_des} for node in nodes)
            if not sources:
                emit(f"  Warning: No sources for {rail.net_name}. Skipping solve.")
                completed += 1
                progress(completed, total, f"{rail.net_name}: skipped")
                continue
            if cancelled(): raise DCAnalysisCancelled("DC analysis cancelled.")
            solver = self._solver_factory(
                debug=request.debug, log_callback=emit,
                compute_settings=request.compute_settings,
            )
            detailed = solver.solve_detailed(mesh, sources, loads)
            from application.dc_current_density import calculate_current_density
            current_density = calculate_current_density(
                mesh, detailed, request.stackup,
                request.classification_geometry_by_rail.get(rail.net_name, {}),
            )
            voltages = detailed.voltages
            if voltages:
                values = list(voltages.values())
                results[rail.net_name] = {
                    'mesh': mesh, 'results': voltages,
                    'stats': (min(values), max(values), max(values) - min(values)),
                    'sources': sources, 'loads': loads, 'detailed_result': detailed,
                    'current_density': current_density,
                    'compute_metadata': solver.last_compute,
                    'grid_size_mm': mesh.grid_step,
                    'requested_grid_size_mm': mesh.requested_grid_step,
                    'adaptive_grid': mesh.adaptive_grid,
                }
                emit(f"  Solved {rail.net_name}: Drop {max(values) - min(values):.4f} V")
                if not getattr(detailed, "valid", True):
                    excluded = ", ".join(getattr(detailed, "excluded_load_references", ()))
                    references = f" [{excluded}]" if excluded else ""
                    emit(
                        f"  {rail.net_name} model status: INCOMPLETE; "
                        f"{getattr(detailed, 'excluded_load_node_count', 0)} load node(s) "
                        f"excluded on copper island(s) without a voltage source{references}."
                    )
                compute = solver.last_compute
                if compute is not None:
                    emit(
                        f"  {rail.net_name} compute: {compute.backend}/{compute.solver_method}, "
                        f"converged={'yes' if compute.converged else 'no'}, "
                        f"residual={compute.relative_residual:.3g}, "
                        f"iterations={compute.iterations}, solve={compute.solve_seconds:.3f} s."
                    )
            completed += 1
            progress(completed, total, f"{rail.net_name}: solved")
        return results


class DCAnalysisController(BackgroundAnalysisController):
    """Own one cancellable background DC job and marshal its callbacks."""

    def __init__(self, dispatch=lambda callback, *args: callback(*args), engine=None):
        super().__init__(
            engine or DCSolverEngine(),
            thread_name="KiPIDA-DC-Analysis",
            busy_message="A DC analysis is already running.",
            cancelled_error_factory=lambda: DCAnalysisCancelled("DC analysis cancelled."),
            dispatch=dispatch,
        )
