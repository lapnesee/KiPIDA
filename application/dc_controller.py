"""Thread-safe preparation and execution of DC power-integrity analyses.

KiCad IPC objects are captured by :func:`prepare_dc_request` on the caller
thread.  The controller worker only receives plain Python objects and Shapely
geometries, so it never calls the live KiCad board API.
"""

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
import re
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from advisor.dc_advisor import load_drop_v
from advisor.rail_sources import (
    NODE_MATCH_FLOOR_MM, NODE_MATCH_GRID_FACTOR, NodeIndex, map_pads_to_nodes,
)
from extractor import GeometryExtractor, live_capture_has_no_copper
from ingest.board_reader import read_board
from mesh import Mesher
from mesh_hybrid import HybridMesher, relabel_layers as _relabel_layers, stranded_pads
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
    # Solve the rails whose drop can decide a verdict again at half the grid
    # step and record how far their worst-load drop moves (_grid_sensitivity).
    # Off for the DC solves a thermal or CFD run makes for its copper losses.
    grid_sensitivity_check: bool = False
    # The saved board as it was when the live board was captured. The hybrid
    # engine used to read the file when it ran: P02 Rev E was saved at 15:37
    # during a campaign captured at 15:29, and its DC domain and the coupled
    # thermal's DC solved two different boards.
    board_file_text: Optional[str] = field(default=None, repr=False)


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
        # A property this kipy version refuses: its getter is tried next.
        pass
    method = getattr(obj, f"get_{name}", None)
    if callable(method):
        try:
            value = method()
            if value is not None:
                return value
        except Exception:
            # Neither spelling answers: the default.
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
            # The attribute spelling below is tried next.
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
    grid_sensitivity_check=False,
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
        grid_sensitivity_check=bool(grid_sensitivity_check),
        board_file_text=_saved_board_text(board_path),
    )


def _saved_board_text(board_path):
    """The saved board file's content now, or None when it cannot be read."""
    if not board_path:
        return None
    try:
        return Path(board_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


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


# Share of the drop budget a rail's worst-load drop must reach for the grid
# check to refine it: a tenth, below which even a 50 % grid error leaves the
# drop within half its budget.
GRID_CHECK_BUDGET_SHARE = 0.10


def rails_without_supply(rails):
    """Names of the rails nothing drives: no declared source, no regulator output."""
    fed = {
        regulator.output_rail_name
        for rail in rails for regulator in (getattr(rail, "child_regulators", None) or ())
    }
    return [rail.net_name for rail in rails if not rail.sources and rail.net_name not in fed]


def unsolved_rails_reason(rails, shown=8):
    """Why a DC run over *rails* solved none of them, in terms the Power Tree can act on.

    A rail is solved only from a source or a regulator output: a power tree
    discovered from the board and never filled in has neither, and every rail
    is then skipped one log line at a time.
    """
    rails = list(rails)
    unsupplied = rails_without_supply(rails)
    names = ", ".join(unsupplied[:shown]) + (", ..." if len(unsupplied) > shown else "")
    if rails and len(unsupplied) == len(rails):
        return (
            f"None of the {len(rails)} rails has a source or a regulator output feeding it "
            f"({names}); declare them in the Power Tree."
        )
    if unsupplied:
        return (
            f"{len(unsupplied)} of the {len(rails)} rails have no source or regulator output "
            f"({names}); the others found no copper under their sources, see the DC log."
        )
    return (
        f"All {len(rails)} rails have a source, but none was found on copper; "
        "see the DC log for the sources and rails concerned."
    )


def _mesh_layers(mesh):
    """The copper layer ids the mesh holds nodes on, in the live board's numbering."""
    return sorted({key[2] for key in mesh.node_map})


def _mesh_nodes(mesh, board, ref_des, pad_names, debug=False, emit=lambda _message: None,
                layers=None):
    if debug:
        emit(f"  Mapping {ref_des} pads={pad_names}")
    footprint = next((item for item in board.footprints if item.reference == ref_des), None)
    if footprint is None:
        if debug: emit(f"  Warning: Footprint {ref_des} not found.")
        return []
    selected = footprint.pads if not pad_names else tuple(
        pad for pad in footprint.pads if pad.number in pad_names or pad.name in pad_names
    )
    # Every layer the mesh has, not a fixed id range: KiCad 10's IPC numbers
    # B.Cu 34, past the 0-31 of the old pcbnew numbering, and a bottom-side
    # pad was then only found through copper of another layer above it.
    if layers is None:
        layers = _mesh_layers(mesh)
    nodes = []
    for pad in selected:
        px, py = pad.position.x, pad.position.y
        if abs(px) > 10000 or abs(py) > 10000:
            px, py = px / 1e6, py / 1e6
        tx = int(round((px - mesh.grid_origin[0]) / mesh.grid_step))
        ty = int(round((py - mesh.grid_origin[1]) / mesh.grid_step))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for layer in layers:
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


def _rail_heading(rail, rails):
    """The log line opening a rail's solve, regulators counted.

    "Sources: 0, Loads: 0" headed 10 of P02 Rev E's 17 rails, /VBUS_SYS
    among them, which then reported a 4.8 mV drop: its current enters and
    leaves through regulators, which the line did not count.
    """
    fed_by = sum(
        1 for other in rails for regulator in getattr(other, "child_regulators", ())
        if regulator.output_rail_name == rail.net_name
    )
    return (
        f"Processing Rail: {rail.net_name} (Sources: {len(rail.sources)}, "
        f"Loads: {len(rail.loads)}, fed by {fed_by} regulator(s), "
        f"feeding {len(getattr(rail, 'child_regulators', ()))})"
    )


def _is_pass_element(regulator, by_name):
    """True for a fuse, eFuse, shunt, switch or net-tie, not a real converter.

    DC-3: these are declared as regulators (reg_type defaults to "LINEAR")
    because they connect two rails, but they do not regulate -- their input
    and output rails share the same nominal voltage. A true linear regulator
    (an LDO) is also "LINEAR" but drops its input to a lower, different
    nominal, and stays Dirichlet at that set point as intended.
    """
    if str(getattr(regulator, "reg_type", "LINEAR")) == "SWITCHING":
        return False
    output_rail = by_name.get(regulator.output_rail_name)
    input_rail = by_name.get(regulator.input_rail_name)
    if output_rail is None or input_rail is None:
        return False
    return abs(float(output_rail.nominal_voltage) - float(input_rail.nominal_voltage)) < 1e-9


def _rail_injections(rail, rails, regulator_demands, locate, emit, pass_voltages=None):
    """Sources, loads and downstream regulator input nodes of one rail.

    Declared sources hold the rail at its nominal voltage. The output of a
    regulator producing the rail does too, *unless* it is a pass element
    (DC-3, :func:`_is_pass_element`) and *pass_voltages* already carries a
    propagated voltage for this rail -- computed by the caller from the
    regulator's own input pad, once solved on its own (upstream) rail, minus
    its declared ``r_pass_ohm`` drop. Declared loads and the inputs of the
    regulators this rail feeds draw current, split evenly over the nodes
    their pads were located on. *locate* maps ``(ref_des, pad_names)`` to
    node ids on the engine's mesh.

    Returns ``(sources, loads, regulator_input_nodes)``, the last a
    ``{regulator.output_rail_name: [node_id, ...]}`` map of where each
    downstream regulator's input pad landed on *this* rail's mesh, so the
    caller can read its solved voltage back out and propagate it onward.
    """
    by_name = {item.net_name: item for item in rails}
    sources, loads = [], []
    for source in rail.sources:
        nodes = locate(source.component_ref.ref_des, source.pad_names)
        if not nodes: emit(f"  WARNING: Source {source.component_ref.ref_des} found NO mesh nodes!")
        sources.extend({'node_id': node, 'voltage': rail.nominal_voltage} for node in nodes)
    for parent in rails:
        for regulator in parent.child_regulators:
            if regulator.output_rail_name == rail.net_name:
                nodes = locate(regulator.output_ref_des, regulator.output_pad_names)
                voltage = rail.nominal_voltage
                if pass_voltages is not None and _is_pass_element(regulator, by_name):
                    propagated = pass_voltages.get(rail.net_name)
                    if propagated is not None:
                        voltage = propagated
                sources.extend({'node_id': node, 'voltage': voltage} for node in nodes)
    for load in rail.loads:
        nodes = locate(load.component_ref.ref_des, load.pad_names)
        if nodes:
            current = load.total_current / len(nodes)
            loads.extend({'node_id': node, 'current': current, 'ref_des': load.component_ref.ref_des} for node in nodes)
    regulator_input_nodes = {}
    for regulator, input_current in regulator_demands:
        nodes = locate(regulator.input_ref_des, regulator.input_pad_names)
        if not nodes:
            emit(f"  WARNING: Regulator {regulator.name} input found NO mesh nodes!")
            continue
        current = input_current / len(nodes)
        loads.extend({'node_id': node, 'current': current, 'ref_des': regulator.input_ref_des} for node in nodes)
        regulator_input_nodes[regulator.output_rail_name] = list(nodes)
    return sources, loads, regulator_input_nodes


def _propagated_pass_voltages(rail, rails, demands, regulator_input_nodes, voltages, pass_voltages):
    """Update *pass_voltages* in place with what this rail's solve found.

    For every pass-element regulator this rail feeds, the propagated source
    for its output rail is the mean solved voltage at its own input pad on
    *this* (upstream, already-solved) rail, minus its own ``r_pass_ohm``
    drop at the current it draws -- 0 V of extra drop when undeclared, which
    still beats resetting to nominal.
    """
    by_name = {item.net_name: item for item in rails}
    current_by_output_rail = {
        regulator.output_rail_name: input_current for regulator, input_current in demands
    }
    for regulator in rail.child_regulators:
        if not _is_pass_element(regulator, by_name):
            continue
        nodes = regulator_input_nodes.get(regulator.output_rail_name)
        if not nodes:
            continue
        seen = [voltages[node] for node in nodes if node in voltages]
        if not seen:
            continue
        input_voltage = sum(seen) / len(seen)
        current = current_by_output_rail.get(regulator.output_rail_name, 0.0)
        pass_voltages[regulator.output_rail_name] = (
            input_voltage - current * float(getattr(regulator, "r_pass_ohm", 0.0) or 0.0)
        )


_GROUND_NET_RE = re.compile(r"(?i)(^|[/_+\-])(gnd|vss|ground)(\d*|$)")


def main_ground_net(parsed_board):
    """The ground net most pads sit on, or None (P02 Rev E: GND)."""
    counts = Counter(
        pad.net_name
        for footprint in getattr(parsed_board, "footprints", ()) or ()
        for pad in footprint.pads
        if pad.net_name and _GROUND_NET_RE.search(pad.net_name)
    )
    return counts.most_common(1)[0][0] if counts else None


@dataclass
class GroundReturn:
    """The ground net solved with every load's current returning to its source.

    ``rise_by_ref_v`` is each part's ground potential above the top-level
    sources' ground pads, averaged over its own ground pads.
    """
    net_name: str
    mesh: Any
    sources: List[Dict[str, Any]]
    loads: List[Dict[str, Any]]
    detailed_result: Any
    rise_by_ref_v: Dict[str, float]
    unlocated: List[str] = field(default_factory=list)
    # "REF.pad" on ground copper cut off from the sources' (stranded_pads).
    stranded_pads: List[str] = field(default_factory=list)


def _ground_injections(rails, demands_by_rail, rail_current, locate):
    """Sources, loads and located parts of the ground return.

    The top-level sources' ground pads are the 0 V reference. A load returns
    its current into the ground at its own ground pads. A converter's ground
    pin draws Iout - Iin from the ground (Iin + Ig = Iout through the part),
    which a buck takes and a boost gives back; a pass element passes its
    current along the rail and none through a ground. Each current is split
    evenly over the part's located ground nodes, as the rail loads are.
    """
    by_name = {rail.net_name: rail for rail in rails}
    sources, loads, located, unlocated = [], [], {}, []

    def find(ref_des):
        if ref_des not in located:
            located[ref_des] = list(locate(ref_des, ()))
        return located[ref_des]

    def nodes_of(ref_des):
        nodes = find(ref_des)
        if not nodes and ref_des not in unlocated:
            unlocated.append(ref_des)
        return nodes

    for rail in rails:
        for source in rail.sources:
            sources.extend(
                {'node_id': node, 'voltage': 0.0}
                for node in nodes_of(source.component_ref.ref_des)
            )
        for load in rail.loads:
            nodes = nodes_of(load.component_ref.ref_des)
            current = float(load.total_current or 0.0)
            if nodes and current:
                loads.extend(
                    {'node_id': node, 'current': -current / len(nodes),
                     'ref_des': load.component_ref.ref_des}
                    for node in nodes
                )
        for regulator, input_current in demands_by_rail.get(rail.net_name, ()):
            if _is_pass_element(regulator, by_name):
                continue
            ground_draw = float(rail_current.get(regulator.output_rail_name, 0.0)) - float(input_current)
            if not ground_draw:
                continue
            # Either end may carry the ground pin. P02 Rev E's U4 takes its
            # input through Q10, which has none, and was reported "not
            # modelled" though U4's ground pads carried its return; D3, an
            # OR diode drawing nothing from ground, was reported too.
            nodes = find(regulator.input_ref_des) or find(regulator.output_ref_des)
            if not nodes:
                nodes_of(regulator.input_ref_des)
            if nodes:
                loads.extend(
                    {'node_id': node, 'current': ground_draw / len(nodes),
                     'ref_des': regulator.input_ref_des}
                    for node in nodes
                )
    return sources, loads, {ref: nodes for ref, nodes in located.items() if nodes}, unlocated


def _attach_ground_return(results, rails, ground):
    """Give each solved rail its load-point drop including the ground return.

    A load sees its rail minus its ground. Its rail's source regulates
    against its own ground pin, so the drop a load adds on the return is its
    ground rise less its source's: combined = supply drop + load rise -
    source rise, taken at the worst load node.
    """
    by_name = {rail.net_name: rail for rail in rails}
    rise = ground.rise_by_ref_v
    for name, data in results.items():
        rail = by_name.get(name)
        voltages = data.get('results') or {}
        source_refs = [source.component_ref.ref_des for source in getattr(rail, 'sources', ())] + [
            regulator.input_ref_des
            for parent in rails for regulator in parent.child_regulators
            if regulator.output_rail_name == name
        ]
        source_rises = [rise[ref] for ref in source_refs if ref in rise]
        source_rise = sum(source_rises) / len(source_rises) if source_rises else 0.0
        v_rail = float(data.get('source_voltage_v', 0.0) or 0.0)
        worst, worst_ref, load_rise = 0.0, "", {}
        for load in data.get('loads') or ():
            ref = load.get('ref_des')
            if load.get('node_id') not in voltages or not abs(float(load.get('current', 0.0))):
                continue
            ref_rise = rise.get(ref, 0.0) - source_rise
            load_rise[ref] = ref_rise
            combined = v_rail - voltages[load['node_id']] + ref_rise
            if combined > worst:
                worst, worst_ref = combined, ref
        data['ground_return'] = {
            'net': ground.net_name,
            'load_rise_v': load_rise,
            'source_ground_rise_v': source_rise,
            'combined_load_drop_v': worst,
            'combined_worst_ref': worst_ref,
            'unlocated': list(ground.unlocated),
            'stranded_pads': list(getattr(ground, 'stranded_pads', ()) or ()),
        }
        data['ground_solution'] = ground


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
        layers = _mesh_layers(mesh)
        return lambda ref_des, pad_names: _mesh_nodes(
            mesh, request.board, ref_des, pad_names, request.debug, emit, layers=layers,
        )

    def _publish_mesh(self, mesh, context):
        """Bring the mesh to the live board's conventions before it is read."""

    def _solve_ground_return(self, request, rails, results, demands_by_rail, rail_current,
                             context, emit):
        """The ground net's solve, or None: the raster capture holds no ground."""
        emit("Ground return not solved: the raster engine captures the rails' copper only.")
        return None

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
        # _sorted_rails is downstream-first (a regulator's own demand needs
        # its output rail's current already totalled), so this loop is what
        # _regulator_input_currents needs -- but DC-3's voltage propagation
        # needs the opposite: an upstream rail solved before the downstream
        # one whose pass-element source it feeds. Demands are computed here,
        # once, in the order current aggregation requires; the mesh/solve
        # loop below then runs the reverse of it -- upstream first -- reading
        # these demands back rather than recomputing them out of order.
        sorted_rails = _sorted_rails(rails)
        rail_current = {rail.net_name: 0.0 for rail in rails}
        demands_by_rail = {
            rail.net_name: _regulator_input_currents(rail, rails, rail_current)
            for rail in sorted_rails
        }
        solve_order = list(reversed(sorted_rails))
        emit("Rail solve order: " + " -> ".join(rail.net_name for rail in solve_order))
        context = self._begin(request, emit)
        results = {}
        # DC-3: a pass element's (fuse, eFuse, shunt, switch, net-tie) output
        # rail source, propagated from its own input pad once that rail is
        # solved -- {output_rail_name: volts}. Filled in solve_order, upstream
        # first, so it is always ready by the time a downstream rail needs it.
        pass_voltages = {}
        check_grid = bool(getattr(request, "grid_sensitivity_check", False))
        total = max(1, len(solve_order) * (4 if check_grid else 3))
        completed = 0
        for rail in solve_order:
            if cancelled(): raise DCAnalysisCancelled("DC analysis cancelled.")
            emit(_rail_heading(rail, rails))
            demands = demands_by_rail[rail.net_name]
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

            sources, loads, regulator_input_nodes = _rail_injections(
                rail, rails, demands,
                self._locator(rail, mesh, request, context, emit), emit,
                pass_voltages=pass_voltages,
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
            _propagated_pass_voltages(
                rail, rails, demands, regulator_input_nodes, detailed.voltages, pass_voltages,
            )
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
                    'stranded_pads': stranded_pads(mesh, [source['node_id'] for source in sources]),
                    'stats': (min(values), max(values), max(values) - min(values)),
                    'load_drop_v': worst_load_drop,
                    'source_voltage_v': source_voltage,
                    # DC-3: source_voltage_v is the propagated chain voltage
                    # when a pass element feeds this rail, and equals
                    # nominal_voltage_v otherwise -- the gap between the two
                    # is the cumulative drop from the top of the chain.
                    'nominal_voltage_v': float(rail.nominal_voltage or 0.0),
                    'engine': self.engine_name,
                    'sources': sources, 'loads': loads, 'detailed_result': detailed,
                    # What the rail was asked to deliver, per part, so the
                    # adapter can tell a load it never located from one that
                    # draws nothing (DC-4).
                    'configured_loads': [
                        (load.component_ref.ref_des, float(load.total_current or 0.0))
                        for load in rail.loads
                    ] + [
                        (regulator.input_ref_des, float(input_current or 0.0))
                        for regulator, input_current in demands
                    ],
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
        if rails and not results:
            emit(f"No rail was solved. {unsolved_rails_reason(rails)}")
        if results:
            if cancelled(): raise DCAnalysisCancelled("DC analysis cancelled.")
            ground = self._solve_ground_return(
                request, rails, results, demands_by_rail, rail_current, context, emit,
            )
            if ground is not None:
                _attach_ground_return(results, rails, ground)
        if check_grid and results:
            done = [completed]

            def advance(detail):
                done[0] += 1
                progress(min(done[0], total), total, detail)

            self._grid_sensitivity(
                request, rails, results, demands_by_rail, context, emit, advance, cancelled,
                pass_voltages,
            )
            progress(total, total, "grid check done")
        return results

    def _grid_sensitivity(self, request, rails, results, demands_by_rail, context, emit, advance,
                           cancelled, pass_voltages=None):
        """The worst-load drop again at half the grid step, on the rails where it counts.

        The drop had no error bar. Solved at twice the step instead, the P02
        Rev E +5V_U4 read 39.1 mV at 0.2 mm against 40.8 mV at 0.1 mm, while
        0.05 mm gave 49.7 mV: a coarser mesh agreeing proves nothing, so the
        check goes finer. That costs about four times the rail's solve, and is
        spent only on rails whose drop is at least GRID_CHECK_BUDGET_SHARE of
        the drop budget, below which no plausible grid error changes the
        verdict -- on every rail when the run carries no budget. Either way
        the rail's entry says so under ``grid_sensitivity``; the refined mesh
        and its solver stay out of the log.
        """
        by_name = {rail.net_name: rail for rail in rails}
        budget_pct = getattr(request, "maximum_drop_pct", None)
        below_budget = []

        def quiet(*_args):
            if cancelled():
                raise DCAnalysisCancelled("DC analysis cancelled.")

        for name, data in list(results.items()):
            if cancelled(): raise DCAnalysisCancelled("DC analysis cancelled.")
            rail = by_name.get(name)
            step = float(data.get("grid_size_mm", 0.0) or 0.0) or float(request.grid_size_mm)
            if rail is None or step <= 0.0:
                continue
            drop = float(data.get("load_drop_v", 0.0) or 0.0)
            source_v = float(data.get("source_voltage_v", 0.0) or 0.0) or float(rail.nominal_voltage or 0.0)
            if budget_pct is not None and source_v > 0.0 and (
                drop < GRID_CHECK_BUDGET_SHARE * float(budget_pct) / 100.0 * source_v
            ):
                data["grid_sensitivity"] = {"grid_mm": step, "below_budget_share": GRID_CHECK_BUDGET_SHARE}
                below_budget.append(name)
                continue
            advance(f"{name}: grid check at {step / 2.0:g} mm")
            fine_request = replace(request, grid_size_mm=step / 2.0)
            mesh = self._build_mesh(rail, fine_request, context, quiet)
            if mesh is None or not mesh.nodes or float(mesh.grid_step) >= step:
                emit(f"[grid check] {name}: no finer mesh than {step:g} mm could be built; not checked.")
                continue
            sources, loads, _regulator_input_nodes = _rail_injections(
                rail, rails, demands_by_rail.get(name, []),
                self._locator(rail, mesh, fine_request, context, quiet), quiet,
                pass_voltages=pass_voltages,
            )
            if not sources:
                continue
            solver = self._solver_factory(
                debug=False, log_callback=quiet, compute_settings=request.compute_settings,
            )
            detailed = solver.solve_detailed(mesh, sources, loads)
            if not detailed.voltages:
                continue
            fine_drop, _source_v = _load_point_drop(detailed.voltages, sources, loads)
            delta_pct = abs(fine_drop - drop) / drop * 100.0 if drop > 0.0 else 0.0
            data["grid_sensitivity"] = {
                "grid_mm": step, "check_grid_mm": float(mesh.grid_step),
                "drop_v": drop, "check_drop_v": float(fine_drop), "delta_pct": delta_pct,
            }
            emit(
                f"[grid check] {name}: worst load drop {drop * 1e3:.3f} mV at {step:g} mm, "
                f"{fine_drop * 1e3:.3f} mV at {float(mesh.grid_step):g} mm ({delta_pct:.1f} % apart)."
            )
        if below_budget:
            emit(
                f"[grid check] {len(below_budget)} rail(s) not refined, their drop under "
                f"{GRID_CHECK_BUDGET_SHARE:.0%} of the budget: "
                + ", ".join(below_budget[:8]) + (", ..." if len(below_budget) > 8 else "") + "."
            )


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


def _snapshot_mm(value):
    # Live snapshots carry nanometres; the same heuristic as _mesh_nodes.
    value = float(value)
    return value / 1e6 if abs(value) > 10000 else value


def _unmatched_positions(live, saved):
    """``(only live, only saved)`` counts of ``(key, x, y)`` items paired within tolerance.

    Items pair when their keys are equal and their positions lie within
    FOOTPRINT_POSITION_TOLERANCE_MM. Both sides used to be rounded to the
    micrometre and compared exactly, which split any coordinate on a half
    micrometre: P02 Rev E's D2 pads sit at x = 169.3075 mm, 169307500 nm live
    and 169.30749999999998 once the file reader has placed them, and ten
    untouched pads were reported moved (DC-004).
    """
    from collections import defaultdict

    pending = defaultdict(list)
    for key, x_mm, y_mm in saved:
        pending[key].append((x_mm, y_mm))
    only_live = 0
    for key, x_mm, y_mm in live:
        candidates = pending.get(key, [])
        match = next((
            index for index, (other_x, other_y) in enumerate(candidates)
            if abs(other_x - x_mm) <= FOOTPRINT_POSITION_TOLERANCE_MM
            and abs(other_y - y_mm) <= FOOTPRINT_POSITION_TOLERANCE_MM
        ), None)
        if match is None:
            only_live += 1
        else:
            candidates.pop(match)
    return only_live, sum(len(items) for items in pending.values())


def _count_mismatch(live, saved, noun):
    only_live, only_saved = _unmatched_positions(live, saved)
    if not (only_live or only_saved):
        return None
    return f"{only_live} {noun}(s) only on the open board and {only_saved} only in the saved file"


# Copper that differs by less than this is taken as the same drawing, not an
# edit: arcs are approximated by chords on both sides, and polygon unions of
# the same shapes can differ in their last vertices.
DIVERGENCE_AREA_FRACTION = 0.02
DIVERGENCE_AREA_FLOOR_MM2 = 0.05


def _live_capture_counts(request):
    """(track, via, zone, pad) counts of the whole live capture, every rail.

    ``request.classification_geometry_by_rail`` maps rail net -> kind
    ("track"/"zone") -> layer -> a Shapely geometry, which the IPC can hand
    back present but empty (area 0) while the editor is still synchronising;
    only non-empty geometry counts as captured.
    """
    snapshot = request.board
    track_count = 0
    zone_count = 0
    for kinds in (request.classification_geometry_by_rail or {}).values():
        for geometry in (kinds.get("track") or {}).values():
            if geometry is not None and not geometry.is_empty:
                track_count += 1
        for geometry in (kinds.get("zone") or {}).values():
            if geometry is not None and not geometry.is_empty:
                zone_count += 1
    pad_count = sum(len(footprint.pads) for footprint in snapshot.footprints)
    return track_count, len(snapshot.vias), zone_count, pad_count


def saved_board_differences(board, net_name, request, layer_mapping):
    """How the open board's copper on *net_name* differs from the saved file.

    The hybrid engine solves the file. A board edited since it was saved is a
    different board, and a result that does not say so describes copper the
    user is no longer looking at. Compared: the net's vias and pads by position
    (from the live snapshot), and its track and pour copper per layer (from the
    live classification geometry) by the area of their symmetric difference.
    Whatever the request did not capture is not compared.

    KiCad's IPC answers every collection empty while the PCB editor is still
    synchronising. On P02 Rev E a campaign started there (21:39,
    campaign-...-213933) compared that empty capture to the saved file and
    reported 17 rails "differs ... (100% of it)", one per rail, none of them
    a real edit. When the *whole* live capture -- every rail, not just this
    net -- has no via, pad, track or zone at all, that is not a diff to
    report; it is a capture that has not arrived yet.
    """
    if live_capture_has_no_copper(*_live_capture_counts(request)):
        return ["live capture empty, retry after sync"]
    differences = []
    snapshot = request.board
    if snapshot.vias or snapshot.footprints:
        live_vias = [
            (None, _snapshot_mm(via.position.x), _snapshot_mm(via.position.y))
            for via in snapshot.vias if via.net.name == net_name
        ]
        saved_vias = [
            (None, float(via.position[0]), float(via.position[1]))
            for via in board.vias if via.net_name == net_name
        ]
        live_pads = [
            ((footprint.reference, str(pad.number)), _snapshot_mm(pad.position.x), _snapshot_mm(pad.position.y))
            for footprint in snapshot.footprints for pad in footprint.pads if pad.net.name == net_name
        ]
        saved_pads = [
            ((footprint.reference, str(pad.number)), float(pad.position[0]), float(pad.position[1]))
            for footprint in board.footprints for pad in footprint.pads if pad.net_name == net_name
        ]
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


# Pads closer than this are the same pad: the file stores millimetres and the
# live board nanometres, and the offline reader rotates pad offsets in floating
# point, so exact equality would call an untouched rotated footprint moved.
FOOTPRINT_POSITION_TOLERANCE_MM = 0.01


def saved_footprint_differences(snapshot, board):
    """How the open board's footprints differ from the saved file's.

    For analyses that read the files rather than the live board: a footprint
    added, deleted or moved since the last save is a board the result does not
    describe. Footprints are paired by reference and compared by their pads'
    numbers and positions, which a move, a rotation or a flip all change. A
    snapshot that captured no footprint says nothing and yields nothing.
    """
    from collections import defaultdict

    live_footprints = tuple(getattr(snapshot, "footprints", ()) or ())
    if not live_footprints:
        return []
    live, saved = defaultdict(list), defaultdict(list)
    for footprint in live_footprints:
        live[footprint.reference].append(sorted(
            (str(pad.number), _snapshot_mm(pad.position.x), _snapshot_mm(pad.position.y))
            for pad in footprint.pads
        ))
    for footprint in board.footprints:
        saved[footprint.reference].append(sorted(
            (str(pad.number), float(pad.position[0]), float(pad.position[1])) for pad in footprint.pads
        ))

    only_live = sorted(ref for ref in live if len(live[ref]) > len(saved.get(ref, ())))
    only_saved = sorted(ref for ref in saved if len(saved[ref]) > len(live.get(ref, ())))
    moved = sorted(
        ref for ref in live
        if len(live[ref]) == len(saved.get(ref, ())) and not _same_placements(live[ref], saved[ref])
    )
    differences = []
    for references, text in (
        (only_live, "only on the open board"),
        (only_saved, "only in the saved file"),
        (moved, "placed differently"),
    ):
        if references:
            shown = ", ".join(references[:5]) + (", ..." if len(references) > 5 else "")
            differences.append(f"{len(references)} footprint(s) {text} ({shown})")
    return differences


def _same_placements(live_placements, saved_placements):
    remaining = list(saved_placements)
    for pads in live_placements:
        match = next((index for index, other in enumerate(remaining) if _same_pads(pads, other)), None)
        if match is None:
            return False
        remaining.pop(match)
    return True


def _same_pads(first, second):
    return len(first) == len(second) and all(
        a[0] == b[0]
        and abs(a[1] - b[1]) <= FOOTPRINT_POSITION_TOLERANCE_MM
        and abs(a[2] - b[2]) <= FOOTPRINT_POSITION_TOLERANCE_MM
        for a, b in zip(first, second)
    )


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
        text = getattr(request, "board_file_text", None)
        try:
            board = (
                self._board_reader(Path(request.board_path)) if text is None
                else self._board_reader(Path(request.board_path), text=text)
            )
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

    def _solve_ground_return(self, request, rails, results, demands_by_rail, rail_current,
                             context, emit):
        """Solve the main ground net with every load's current returning.

        The drop was the supply side alone: ground bounce between a load and
        its source was in no result (open since the 2026-09-13 audit).
        """
        net = main_ground_net(context.board)
        if net is None:
            emit("Ground return not solved: no ground net was found on the board.")
            return None
        started = time.perf_counter()
        ground_rail = SimpleNamespace(net_name=net, sources=(), loads=())
        mesh = self._build_mesh(ground_rail, request, context, emit)
        if mesh is None or not mesh.nodes:
            emit(f"Ground return not solved: {net} has no copper to mesh.")
            return None
        sources, loads, located, unlocated = _ground_injections(
            rails, demands_by_rail, rail_current,
            self._locator(ground_rail, mesh, request, context, emit),
        )
        if not sources:
            emit(f"Ground return not solved: no top-level source has a pad on {net}.")
            return None
        solver = self._solver_factory(
            debug=request.debug, log_callback=emit, compute_settings=request.compute_settings,
        )
        detailed = solver.solve_detailed(mesh, sources, loads)
        voltages = detailed.voltages
        if not voltages:
            emit(f"Ground return not solved: the {net} solve failed.")
            return None
        rise = {
            ref: sum(voltages.get(node, 0.0) for node in nodes) / len(nodes)
            for ref, nodes in located.items()
        }
        self._publish_mesh(mesh, context)
        worst_ref = max(rise, key=rise.get) if rise else ""
        emit(
            f"Ground return on {net}: {len(mesh.nodes):,} nodes, "
            f"{sum(-load['current'] for load in loads if load['current'] < 0):.3f} A returned, "
            f"largest rise {rise.get(worst_ref, 0.0) * 1e3:.3f} mV at {worst_ref or '-'}, "
            f"{time.perf_counter() - started:.1f} s."
        )
        if unlocated:
            emit(
                f"  No {net} pad located for: " + ", ".join(sorted(unlocated)[:12])
                + (", ..." if len(unlocated) > 12 else "") + "; their return is not modelled."
            )
        stranded = stranded_pads(mesh, [source['node_id'] for source in sources])
        if stranded:
            emit(f"  {net} pads on copper cut off from the rest of the net: {', '.join(stranded)}.")
        return GroundReturn(net, mesh, sources, loads, detailed, rise, sorted(unlocated), stranded)

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
