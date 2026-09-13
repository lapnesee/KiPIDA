"""Thread-safe preparation and execution of DC power-integrity analyses.

KiCad IPC objects are captured by :func:`prepare_dc_request` on the caller
thread.  The controller worker only receives plain Python objects and Shapely
geometries, so it never calls the live KiCad board API.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any, Callable, Dict, Optional, Tuple

from advisor.dc_advisor import load_drop_v
from advisor.rail_sources import (
    NODE_MATCH_FLOOR_MM, NODE_MATCH_GRID_FACTOR, NodeIndex, map_pads_to_nodes,
)
from extractor import GeometryExtractor
from ingest.board_reader import read_board
from mesh import Mesher
from mesh_hybrid import HybridMesher
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
    # Per rail, the tracks as the raster error depends on them (count,
    # narrowest width, oblique count), so a raster result can state its error.
    track_summary_by_rail: Dict[str, Dict[str, Any]] = field(default_factory=dict)


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
    track_summary_by_rail = {}
    for rail in rails:
        started = time.perf_counter()
        geometry_by_rail[rail.net_name] = extractor.get_net_geometry(
            rail.net_name, merge=False,
        )
        classification_geometry_by_rail[rail.net_name] = {
            "track": extractor.get_track_geometry(rail.net_name),
            "zone": extractor.get_zone_geometry(rail.net_name),
        }
        track_summary_by_rail[rail.net_name] = extractor.get_track_summary(rail.net_name)
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
        track_summary_by_rail=track_summary_by_rail,
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


def _regulator_input_currents(rail, rails, rail_current):
    """Current each regulator fed by *rail* draws from it, and the rail's total.

    *rail_current* must already hold every downstream rail's total, which the
    solve order guarantees. It is computed for every rail whether or not the
    rail is then meshed: a rail skipped for want of copper still passes its
    regulators' demand upstream, where it used to drop out of the cascade.
    """
    rail_current[rail.net_name] = sum(load.total_current for load in rail.loads)
    demands = []
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
        demands.append((regulator, input_current))
    return demands


def _rail_injections(rail, rails, regulator_demands, locate, emit):
    """Sources and loads of one rail as solver injections, for either engine.

    Declared sources and the outputs of the regulators producing the rail hold
    it at its nominal voltage; declared loads and the inputs of the regulators
    it feeds draw current, split evenly over the nodes their pads were located
    on. *locate* maps ``(ref_des, pad_names)`` to node ids on the engine's mesh.
    """
    sources, loads = [], []
    for source in rail.sources:
        nodes = locate(source.component_ref.ref_des, source.pad_names)
        if not nodes: emit(f"  WARNING: Source {source.component_ref.ref_des} found NO mesh nodes!")
        sources.extend({'node_id': node, 'voltage': rail.nominal_voltage} for node in nodes)
    for parent in rails:
        for regulator in parent.child_regulators:
            if regulator.output_rail_name == rail.net_name:
                nodes = locate(regulator.output_ref_des, regulator.output_pad_names)
                sources.extend({'node_id': node, 'voltage': rail.nominal_voltage} for node in nodes)
    for load in rail.loads:
        nodes = locate(load.component_ref.ref_des, load.pad_names)
        if nodes:
            current = load.total_current / len(nodes)
            loads.extend({'node_id': node, 'current': current, 'ref_des': load.component_ref.ref_des} for node in nodes)
    for regulator, input_current in regulator_demands:
        nodes = locate(regulator.input_ref_des, regulator.input_pad_names)
        if not nodes:
            emit(f"  WARNING: Regulator {regulator.name} input found NO mesh nodes!")
            continue
        current = input_current / len(nodes)
        loads.extend({'node_id': node, 'current': current, 'ref_des': regulator.input_ref_des} for node in nodes)
    return sources, loads


def _load_point_drop(voltages, sources, loads):
    """``(worst drop seen by a load, rail voltage it is measured from)``.

    The drop DC-003 judges. The spread between the highest and lowest node of
    the net also counts copper no load draws through -- a stub, a pour corner
    -- and is not the quantity the DC advisor sizes a fix against; this is.
    """
    source_voltages = [voltages[item['node_id']] for item in sources if item.get('node_id') in voltages]
    if not source_voltages:
        return 0.0, 0.0
    return load_drop_v(voltages, sources, loads), max(source_voltages)


class DCSolverEngine:
    """DC solve on a raster mesh of the live board's captured copper."""

    engine_name = "raster"

    def __init__(self, mesher_factory=Mesher, solver_factory=Solver):
        self._mesher_factory = mesher_factory
        self._solver_factory = solver_factory

    # -- hooks an engine variant overrides ------------------------------------

    def _begin(self, request, emit):
        """Per-run state shared by every rail; the raster engine needs none."""
        return None

    def _build_mesh(self, rail, request, context, emit):
        """The rail's mesh, or ``None`` with the reason logged."""
        geometry = request.geometry_by_rail.get(rail.net_name, {})
        if not geometry:
            emit(f"  Skipping {rail.net_name}: No geometry.")
            return None
        mesher = self._mesher_factory(
            request.board, debug=request.debug, log_callback=emit,
            compute_settings=request.compute_settings,
        )
        return mesher.generate_mesh(
            rail.net_name, geometry, request.stackup,
            grid_size_mm=request.grid_size_mm,
        )

    def _locator(self, rail, mesh, request, context, emit):
        return lambda ref_des, pad_names: _mesh_nodes(
            mesh, request.board, ref_des, pad_names, request.debug, emit,
        )

    def _publish_mesh(self, mesh, context):
        """Bring the mesh to the live board's conventions before it is read."""

    def _rail_extras(self, rail, request, context):
        """Engine-specific fields added to the rail's result.

        A raster result carries the rail's track summary, from which the
        adapter states the error the raster mesh puts on its narrowest track.
        """
        summary = (getattr(request, "track_summary_by_rail", None) or {}).get(rail.net_name)
        return {'track_summary': dict(summary)} if summary else {}

    # -- shared solve loop ----------------------------------------------------

    def solve(self, request, emit, progress, cancelled):
        rails = list(request.rails)
        emit(f"--- Starting System Simulation ({len(rails)} rails) ---")
        sorted_rails = _sorted_rails(rails)
        emit("Rail solve order: " + " -> ".join(rail.net_name for rail in sorted_rails))
        context = self._begin(request, emit)
        results = {}
        rail_current = {rail.net_name: 0.0 for rail in rails}
        total = max(1, len(sorted_rails) * 3)
        completed = 0
        for rail in sorted_rails:
            if cancelled(): raise DCAnalysisCancelled("DC analysis cancelled.")
            emit(f"Processing Rail: {rail.net_name} (Sources: {len(rail.sources)}, Loads: {len(rail.loads)})")
            demands = _regulator_input_currents(rail, rails, rail_current)
            completed += 1
            progress(completed, total, f"{rail.net_name}: geometry")
            mesh = self._build_mesh(rail, request, context, emit)
            if mesh is None:
                completed += 2
                progress(completed, total, f"{rail.net_name}: skipped")
                continue
            completed += 1
            progress(completed, total, f"{rail.net_name}: mesh ({len(mesh.nodes):,} nodes)")
            if cancelled(): raise DCAnalysisCancelled("DC analysis cancelled.")
            if not mesh.nodes:
                emit(f"  Skipping {rail.net_name}: Mesh empty.")
                completed += 1
                progress(completed, total, f"{rail.net_name}: skipped")
                continue

            sources, loads = _rail_injections(
                rail, rails, demands,
                self._locator(rail, mesh, request, context, emit), emit,
            )
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
            self._publish_mesh(mesh, context)
            from application.dc_current_density import calculate_current_density
            current_density = calculate_current_density(
                mesh, detailed, request.stackup,
                request.classification_geometry_by_rail.get(rail.net_name, {}),
            )
            voltages = detailed.voltages
            if voltages:
                values = list(voltages.values())
                worst_load_drop, source_voltage = _load_point_drop(voltages, sources, loads)
                results[rail.net_name] = {
                    'mesh': mesh, 'results': voltages,
                    'stats': (min(values), max(values), max(values) - min(values)),
                    'load_drop_v': worst_load_drop,
                    'source_voltage_v': source_voltage,
                    'engine': self.engine_name,
                    'sources': sources, 'loads': loads, 'detailed_result': detailed,
                    'current_density': current_density,
                    'compute_metadata': solver.last_compute,
                    'grid_size_mm': mesh.grid_step,
                    'requested_grid_size_mm': mesh.requested_grid_step,
                    'adaptive_grid': mesh.adaptive_grid,
                    **self._rail_extras(rail, request, context),
                }
                emit(
                    f"  Solved {rail.net_name}: worst load drop {worst_load_drop:.4f} V, "
                    f"net spread {max(values) - min(values):.4f} V"
                )
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


def live_layer_ids(file_stackup, live_stackup):
    """Map the board file's copper layer ids onto the live board's.

    The two number layers differently -- the file by KiCad's internal ordinal,
    IPC by its own enum -- so only names can pair them. The live stackup
    carries the designer's name where one was given, the file both names.
    Layers no name pairs are paired by position when both stacks hold the same
    number of copper layers, since both list them top to bottom.

    Returns ``(mapping, paired_by_position, unmatched)`` where the last two are
    lists of file layer names.
    """
    copper = dict((live_stackup or {}).get("copper", {}) or {})
    by_name = {}
    for live_id, data in copper.items():
        name = str((data or {}).get("name", "") or "")
        if name:
            by_name.setdefault(name, live_id)
    file_layers = list(getattr(file_stackup, "layers", ()) or ())
    mapping = {}
    for layer in file_layers:
        live_id = by_name.get(layer.name)
        if live_id is None and getattr(layer, "user_name", ""):
            live_id = by_name.get(layer.user_name)
        if live_id is not None and live_id not in mapping.values():
            mapping[layer.layer_id] = live_id
    order = list((live_stackup or {}).get("layer_order") or copper.keys())
    positional = []
    if len(mapping) < len(file_layers) and len(order) == len(file_layers):
        used = set(mapping.values())
        for layer, live_id in zip(file_layers, order):
            if layer.layer_id not in mapping and live_id not in used:
                mapping[layer.layer_id] = live_id
                used.add(live_id)
                positional.append(layer.name)
    unmatched = [layer.name for layer in file_layers if layer.layer_id not in mapping]
    return mapping, positional, unmatched


def _relabel_layers(mesh, mapping):
    """Rewrite the mesh's layer ids through *mapping*, in place.

    A layer with no live counterpart gets a negative id, which no live layer
    uses, rather than keeping a file ordinal that may name another live layer.
    """
    def live(layer_id):
        if layer_id in mapping:
            return mapping[layer_id]
        return -1000 - int(layer_id) if int(layer_id) >= 0 else int(layer_id)

    mesh.node_coords = {
        node_id: (x, y, live(layer_id))
        for node_id, (x, y, layer_id) in mesh.node_coords.items()
    }
    mesh.node_map = {
        (ix, iy, live(layer_id)): node_id
        for (ix, iy, layer_id), node_id in mesh.node_map.items()
    }


def _snapshot_mm(value):
    # Live snapshots carry nanometres; the same heuristic as _mesh_nodes.
    value = float(value)
    return value / 1e6 if abs(value) > 10000 else value


def _position_key(x_mm, y_mm):
    return (round(x_mm, 3), round(y_mm, 3))


def _count_mismatch(live, saved, noun):
    only_live = sum((live - saved).values())
    only_saved = sum((saved - live).values())
    if not (only_live or only_saved):
        return None
    return f"{only_live} {noun}(s) only on the open board and {only_saved} only in the saved file"


# Copper that differs by less than this is taken as the same drawing, not an
# edit: arcs are approximated by chords on both sides, and polygon unions of
# the same shapes can differ in their last vertices.
DIVERGENCE_AREA_FRACTION = 0.02
DIVERGENCE_AREA_FLOOR_MM2 = 0.05


def saved_board_differences(board, net_name, request, layer_mapping):
    """How the open board's copper on *net_name* differs from the saved file.

    The hybrid engine solves the file. A board edited since it was saved is a
    different board, and a result that does not say so describes copper the
    user is no longer looking at. Compared: the net's vias and pads by position
    (from the live snapshot), and its track and pour copper per layer (from the
    live classification geometry) by the area of their symmetric difference.
    Whatever the request did not capture is not compared.
    """
    from collections import Counter

    differences = []
    snapshot = request.board
    if snapshot.vias or snapshot.footprints:
        live_vias = Counter(
            _position_key(_snapshot_mm(via.position.x), _snapshot_mm(via.position.y))
            for via in snapshot.vias if via.net.name == net_name
        )
        saved_vias = Counter(
            _position_key(*via.position) for via in board.vias if via.net_name == net_name
        )
        live_pads = Counter(
            (footprint.reference, str(pad.number), *_position_key(_snapshot_mm(pad.position.x), _snapshot_mm(pad.position.y)))
            for footprint in snapshot.footprints for pad in footprint.pads if pad.net.name == net_name
        )
        saved_pads = Counter(
            (footprint.reference, str(pad.number), *_position_key(*pad.position))
            for footprint in board.footprints for pad in footprint.pads if pad.net_name == net_name
        )
        for message in (
            _count_mismatch(live_vias, saved_vias, "via"),
            # A snapshot without footprints captured none, which says nothing
            # about the board's pads.
            _count_mismatch(live_pads, saved_pads, "pad") if snapshot.footprints else None,
        ):
            if message:
                differences.append(message)

    captured = (request.classification_geometry_by_rail or {}).get(net_name)
    if captured:
        import shapely

        file_to_live = {layer.name: layer_mapping.get(layer.layer_id) for layer in board.stackup.layers}
        names = {live_id: name for name, live_id in file_to_live.items() if live_id is not None}
        saved = {"track": {}, "zone": {}}
        for seg in board.segments:
            live_id = file_to_live.get(seg.layer)
            if seg.net_name == net_name and live_id is not None:
                width = seg.width_mm if seg.width_mm > 0 else 0.2
                shape = (shapely.LineString([seg.start, seg.end]) if seg.start != seg.end
                         else shapely.Point(seg.start)).buffer(width / 2.0)
                saved["track"].setdefault(live_id, []).append(shape)
        mesher = HybridMesher(board)
        for zone in board.zones:
            live_id = file_to_live.get(zone.layer)
            if zone.net_name == net_name and live_id is not None and zone.filled_polygon:
                polygon = mesher._zone_polygon(zone)
                if polygon is not None:
                    saved["zone"].setdefault(live_id, []).append(polygon)
        for kind in ("track", "zone"):
            live_layers = captured.get(kind) or {}
            saved_layers = {layer: shapely.union_all(shapes) for layer, shapes in saved[kind].items()}
            for layer_id in sorted(set(live_layers) | set(saved_layers), key=str):
                live = live_layers.get(layer_id)
                stored = saved_layers.get(layer_id)
                live_area = 0.0 if live is None else float(live.area)
                stored_area = 0.0 if stored is None else float(stored.area)
                if live is None or stored is None:
                    changed = max(live_area, stored_area)
                else:
                    changed = float(live.symmetric_difference(stored).area)
                reference = max(live_area, stored_area)
                if changed > max(DIVERGENCE_AREA_FRACTION * reference, DIVERGENCE_AREA_FLOOR_MM2):
                    differences.append(
                        f"{kind} copper on {names.get(layer_id, layer_id)} differs by "
                        f"{changed:.2f} mm² ({changed / reference:.0%} of it)"
                    )
    return differences


class BoardFileUnreadable(RuntimeError):
    """The saved board file could not be read; the hybrid engine cannot run."""


@dataclass(frozen=True)
class _HybridRunContext:
    board: Any
    layer_mapping: Dict[int, Any]


class HybridDCSolverEngine(DCSolverEngine):
    """DC solve on the saved board file: exact tracks, barrels, cut-cell pours.

    Tracks are analytic branches whatever their width or angle, where the
    raster mesh joins only 4-neighbour grid nodes and can open a narrow
    diagonal track altogether. It reads the ``.kicad_pcb`` the request was
    captured from, so it solves the board as last saved; the result contract,
    injections and solve order are the raster engine's.
    """

    engine_name = "hybrid"

    def __init__(self, mesher_factory=HybridMesher, solver_factory=Solver,
                 board_reader=read_board):
        super().__init__(mesher_factory=mesher_factory, solver_factory=solver_factory)
        self._board_reader = board_reader

    def _begin(self, request, emit):
        if not request.board_path:
            raise ValueError(
                "The hybrid DC engine reads the saved board file, and this run "
                "does not know where the board was loaded from."
            )
        started = time.perf_counter()
        try:
            board = self._board_reader(Path(request.board_path))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise BoardFileUnreadable(
                f"{Path(request.board_path).name} could not be read: {exc}"
            ) from exc
        mapping, positional, unmatched = live_layer_ids(board.stackup, request.stackup)
        emit(
            f"Read {Path(request.board_path).name} for the hybrid DC mesh in "
            f"{time.perf_counter() - started:.2f} s "
            f"({len(board.stackup.layers)} copper layers from its {board.stackup.source} table)."
        )
        if positional:
            emit(
                "  Copper layers paired with the live board by position, their names "
                "differing: " + ", ".join(positional) + "."
            )
        if unmatched:
            emit(
                "  WARNING: copper layers of the saved board with no live counterpart: "
                + ", ".join(unmatched) + ". Their current density cannot use the live stackup."
            )
        return _HybridRunContext(board, mapping)

    def _build_mesh(self, rail, request, context, emit):
        mesher = self._mesher_factory(
            context.board, grid_step_mm=request.grid_size_mm, log_callback=emit,
        )
        return mesher.build_mesh(rail.net_name)

    def _locator(self, rail, mesh, request, context, emit):
        tolerance = max(NODE_MATCH_GRID_FACTOR * mesh.grid_step, NODE_MATCH_FLOOR_MM)
        index = NodeIndex(mesh, tolerance)

        def locate(ref_des, pad_names):
            nodes, unlocated = map_pads_to_nodes(
                mesh, context.board, ref_des, pad_names, net_name=rail.net_name,
                tolerance_mm=tolerance, index=index,
            )
            if unlocated:
                emit(
                    f"  {ref_des} pad(s) {', '.join(unlocated)} have no {rail.net_name} "
                    f"copper within {tolerance:.2f} mm on their layers."
                )
            return nodes

        return locate

    def _publish_mesh(self, mesh, context):
        _relabel_layers(mesh, context.layer_mapping)

    def _rail_extras(self, rail, request, context):
        return {
            'board_path': str(request.board_path),
            'saved_board_differences': saved_board_differences(
                context.board, rail.net_name, request, context.layer_mapping,
            ),
        }


RASTER_FALLBACK_NOTICE = (
    "the DC analysis uses the raster mesh of the open board, whose track "
    "resistance is off by x0.75 to x2.1 depending on width, angle and grid "
    "alignment (validation/dc_raster_fidelity.py)."
)


class DefaultDCSolverEngine:
    """The DC engine every analysis gets: one engine, one copper.

    With the saved board file known and readable, the hybrid engine solves it,
    and the DC tab, the campaign, the thermal and CFD copper losses and the
    advisor's sizing all read the same mesh. Without one -- a board never
    saved, or a file that cannot be parsed -- the raster engine solves the open
    board, and the run log says which and why rather than leaving the reader
    to assume the more accurate one ran.
    """

    def __init__(self, hybrid=None, raster=None):
        self._hybrid = hybrid or HybridDCSolverEngine()
        self._raster = raster or DCSolverEngine()

    def solve(self, request, emit, progress, cancelled):
        path = getattr(request, "board_path", None)
        if not path or not Path(path).is_file():
            emit(
                ("No saved board file is known for this run: " if not path
                 else f"{Path(path).name} does not exist: ") + RASTER_FALLBACK_NOTICE
            )
            return self._raster.solve(request, emit, progress, cancelled)
        try:
            return self._hybrid.solve(request, emit, progress, cancelled)
        except BoardFileUnreadable as exc:
            emit(f"{exc}; " + RASTER_FALLBACK_NOTICE)
            return self._raster.solve(request, emit, progress, cancelled)


class DCAnalysisController(BackgroundAnalysisController):
    """Own one cancellable background DC job and marshal its callbacks."""

    def __init__(self, dispatch=lambda callback, *args: callback(*args), engine=None):
        super().__init__(
            engine or DefaultDCSolverEngine(),
            thread_name="KiPIDA-DC-Analysis",
            busy_message="A DC analysis is already running.",
            cancelled_error_factory=lambda: DCAnalysisCancelled("DC analysis cancelled."),
            dispatch=dispatch,
        )
