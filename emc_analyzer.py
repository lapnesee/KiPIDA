"""Deterministic EMI/EMC pre-compliance analysis for live KiCad boards.

The analyzer intentionally reports risks and traceable geometric evidence.  It
does not claim regulatory compliance and does not replace full-wave simulation
or accredited measurements.
"""

from collections import defaultdict
from dataclasses import dataclass, field, replace
import math
from pathlib import Path
import re
import time

try:
    from shapely.geometry import LineString, Point, box
    from shapely.ops import unary_union
    from shapely.strtree import STRtree
except ImportError:  # pragma: no cover - Ki-PIDA normally ships Shapely
    LineString = Point = box = STRtree = None
    unary_union = None

try:
    from .differential_length import protocol_skew_limit_ps
    from .extractor import GeometryExtractor, live_capture_has_no_copper, to_mm
    from .inductor_em import resolve_inductor_models, trapezoid_envelope_db, TargetedInductorRefiner
    from .power_tree import converter_output_for, converter_outputs
    from .models import (
        EMCAnalysisResult, EMCEvidence, EMCFinding, EMCFrequencyRisk,
        EMCProbePoint, EMCSignalSource,
    )
except (ImportError, ValueError):
    from differential_length import protocol_skew_limit_ps
    from extractor import GeometryExtractor, live_capture_has_no_copper, to_mm
    from inductor_em import resolve_inductor_models, trapezoid_envelope_db, TargetedInductorRefiner
    from power_tree import converter_output_for, converter_outputs
    from models import (
        EMCAnalysisResult, EMCEvidence, EMCFinding, EMCFrequencyRisk,
        EMCProbePoint, EMCSignalSource,
    )

try:
    from .physical_constants import DEFAULT_COPPER_THICKNESS_MM
except (ImportError, ValueError):
    from physical_constants import DEFAULT_COPPER_THICKNESS_MM

try:
    from .crosstalk_2d import (
        SymmetricCoupledLineSolver, far_end_crosstalk_ratio,
        near_end_crosstalk_coefficient, near_end_crosstalk_ratio,
        saturation_length_m,
    )
except (ImportError, ValueError):
    try:
        from crosstalk_2d import (
            SymmetricCoupledLineSolver, far_end_crosstalk_ratio,
            near_end_crosstalk_coefficient, near_end_crosstalk_ratio,
            saturation_length_m,
        )
    except ImportError:  # pragma: no cover - NumPy absent
        SymmetricCoupledLineSolver = None
        far_end_crosstalk_ratio = near_end_crosstalk_coefficient = None
        near_end_crosstalk_ratio = saturation_length_m = None


SEVERITY_WEIGHT = {"CRITICAL": 25, "HIGH": 12, "MEDIUM": 6, "LOW": 2, "INFO": 0}

# Where each numeric threshold comes from, quoted in its finding. None of
# them is a value a standard prescribes: each is this tool's reading of the
# layout guidance named, and says so (audit-2026-09-23.md EMC-7).
THRESHOLD_BASIS = {
    "DE-001": (
        "Thresholds 5 mm (MEDIUM) and 8 mm (HIGH) are this tool's heuristic for the "
        "'at the supply pin' placement that capacitor and IC makers recommend; not a "
        "standard value."
    ),
    "ES-001": (
        "The 10 mm threshold is this tool's heuristic for the 'as close to the connector "
        "as possible' placement of TI SLVA680 (ESD layout guide); not a standard value."
    ),
    "SW-002": (
        "Thresholds 25 mm2 (MEDIUM) and 100 mm2 (HIGH) are this tool's heuristic for the "
        "'minimise switch-node area' guidance of TI SLVA959; not a standard value."
    ),
    "SW-001": (
        "Severity follows this tool's heuristic: HIGH for a harmonic order up to 10 or "
        "within 20 dB of the envelope, MEDIUM up to order 30 or a 0.25 V*A drive index."
    ),
    "SW-003": (
        "Thresholds 10 mm (MEDIUM) and 20 mm (HIGH) on the loop's widest gap are this "
        "tool's heuristic for the 'as close together as possible' placement of TI SLVA959 "
        "and ADI AN-136; not a standard value."
    ),
}
CONFIDENCE_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.65, "LOW": 0.25}

# Near-end coupling, as a fraction of the aggressor swing, above which XT-001
# escalates.  A widely used board-level budget allows a victim to see a few
# percent of an aggressor edge before it threatens a logic threshold: 5 % of a
# 3.3 V swing is ~165 mV, comparable to the noise margin of common LVCMOS
# receivers, and 10 % is ~330 mV, which most of them cannot absorb.  These are
# design budgets rather than a standard, so they are stated here and repeated
# in the finding rather than hidden in a comparison.
NEXT_RATIO_HIGH = 0.10
NEXT_RATIO_MEDIUM = 0.05
# A sensitive victim gets one severity step earlier, on a tenth of the budget.
NEXT_RATIO_SENSITIVE = 0.01
# Beyond this edge-to-edge gap the 2-D solver's fixed side margin
# (max(4h, 2w), independent of the gap) stops bounding the domain properly and
# the extracted impedances drift; see docs/audit-si-pdn.md.  Coupling is
# negligible there anyway, so the rule falls back rather than quoting a number
# from outside the solver's trustworthy range.
COUPLING_SOLVER_MAX_GAP_MM = 2.0
SENSITIVE_VICTIM_PATTERN = re.compile(r"ADC|ANALOG|SENSE", re.I)
SWITCH_TOKEN_PATTERN = re.compile(r"(^|[_/])(PH|LX)([_/]|$)", re.I)
CONTEXTUAL_SW_PATTERN = re.compile(
    r"(^|[^A-Z0-9])(?:U\d+|BUCK\d*|REG\d*|CONVERTER|SWITCHER)[_-]SW(?:[^A-Z0-9]|$)", re.I,
)
# An SW token alone ("/SYS_5V_SW1", "/J2_SRC_SW") names switched DC rails
# such as VBUS_PD_SW as often as switch nodes; switch_node_evidence decides.
SW_NAME_PATTERN = re.compile(r"(^|[_/])SW\d*([_/]|$)", re.I)
_INDUCTOR_REF_RE = re.compile(r"^L\d", re.I)
_FERRITE_REF_RE = re.compile(r"^FB\d", re.I)
_IC_REF_RE = re.compile(r"^(U|IC)\d", re.I)
# The dielectric a ceramic capacitor's value names, and the highest
# temperature it is rated for: an EIA class II code's digit sets it (X5R
# 85 C, X6S 105 C, X7R 125 C, X8R 150 C); C0G/NP0 (class I) is rated to
# 125 C. P02 Rev E's values carry the code ("22u 35V X5R").
_DIELECTRIC_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[XYZ]([4-9])[A-Z]|C0G|COG|NP0|NPO)(?![A-Za-z0-9])", re.I,
)
_CLASS_II_RATED_C = {"4": 65.0, "5": 85.0, "6": 105.0, "7": 125.0, "8": 150.0, "9": 200.0}
_CLASS_I_RATED_C = 125.0
# An entered converter current further than this from the power tree's own
# sum, or an entered frequency further than CONVERTER_FREQUENCY_TOLERANCE from
# the converter's loss model, is a copy that was not updated (SW-004).
CONVERTER_CURRENT_TOLERANCE = 0.05
CONVERTER_FREQUENCY_TOLERANCE = 0.10
# Findings the EMC risk score leaves out. TE-002 judges a capacitor against
# its dielectric's temperature rating, a reliability limit hosted here
# because this analysis reads the parts' values; on P02 Rev E it took 24 of
# the score's 65 points, though no emission or immunity risk changed.
UNSCORED_RULES = frozenset(("TE-002",))
# TE-001 names the capacitors, inductors and ferrites above this: the X5R
# limit, past which ceramics lose capacitance and ferrites impedance fast
# enough to matter to a filter.
HOT_PASSIVE_C = 85.0


def dielectric_rating_c(value):
    """``(code, rated maximum C)`` for the dielectric *value* names, or None."""
    match = _DIELECTRIC_RE.search(str(value or ""))
    if match is None:
        return None
    code = match.group(0).upper()
    return code, _CLASS_II_RATED_C[match.group(1)] if match.group(1) else _CLASS_I_RATED_C
# A part that clamps a line to ground: a diode's annotation, or a value that
# names a TVS or ESD part.
_PROTECTION_REF_RE = re.compile(r"^(D\d|TVS|ESD)", re.I)
_ESD_PROTECTOR_VALUE_RE = re.compile(
    r"(?:USBLC|PESD|TPD\d|ESD|TVS|TRANSIL|SMBJ|SMAJ|SMCJ|SRV05|PRTR|ECMF|EMIF|RCLAMP)", re.I,
)
# A wire-to-board connector: its cable goes to another board or a panel part,
# and what it carries is not taken for a port the user plugs into.
_HARNESS_CONNECTOR_RE = re.compile(
    r"PICO.?CLASP|PICO.?BLADE|PICO.?LOCK|MICRO.?LOCK|CLIK.?MATE|(?<![A-Z])JST(?![A-Z])|(?<![A-Z])DF\d{2}(?!\d)|"
    r"WIRE.?TO.?BOARD|HARNESS",
    re.I,
)


def is_unconnected_pin_net(name):
    """KiCad's name for the net of a single pin connected to nothing."""
    return str(name or "").startswith("unconnected-")


def is_switching_node_name(name):
    """True for converter switch nodes, not generic switched DC rail suffixes."""
    if is_unconnected_pin_net(name):
        # unconnected-(U6-SW-Pad5): a module's internal SW pin, left open.
        return False
    return bool(SWITCH_TOKEN_PATTERN.search(name) or CONTEXTUAL_SW_PATTERN.search(name))


def hot_loop_components(snapshot, source_name, net_name):
    """``{reference: (x, y)}`` for the footprints in a switching hot loop.

    Every footprint with a pad on the switch node (SW/PH/LX) is part of it:
    an integrated switcher's IC and the inductor, or an external-FET
    controller's high-side FET, low-side FET (or diode) and the inductor,
    since all of them have a pin on that exact net (EMC-4, P02 Rev E: U4's
    /SYS_5V_SW1 hot loop is Q10, Q11 and L1, all three on that net). The
    linked inductor's own footprint is added even when it is captured
    without that specific pad net, resolved the same way
    em_field_solver.EMNearFieldSolver._inductors_for_source does.
    """
    seen = {}
    for footprint in snapshot.footprints:
        if net_name in footprint.nets:
            seen[footprint.reference] = footprint.position
    for model in getattr(snapshot, "inductors", []) or []:
        if model.source_name == source_name or model.switching_net == net_name:
            for footprint in snapshot.footprints:
                if footprint.reference.upper() == model.ref_des.upper():
                    seen[footprint.reference] = footprint.position
    return seen


_SLOW_BUS_TOKENS = frozenset(("SDA", "SCL", "I2C", "SMBCLK", "SMBDAT", "SMBALERT", "WP"))


def _drives_only_a_slow_bus(footprint, excluded_nets):
    """True when every signal net of *footprint* belongs to an I2C/SMBus port."""
    signals = [
        net for net in footprint.nets
        if net and net not in excluded_nets and not is_unconnected_pin_net(net)
    ]
    return bool(signals) and all(
        _SLOW_BUS_TOKENS & set(re.split(r"[^A-Z0-9]+", net.upper())) for net in signals
    )


def buck_hot_loop(snapshot, net_name, ground_nets, power_nets, is_capacitor=None):
    """The switched loop of a buck leg: input capacitor, high side, low side.

    The loop that carries the chopped current runs from the input
    capacitor's supply pad through the high-side switch to the switch node,
    through the low-side switch to ground, and back to the capacitor's
    ground pad; the inductor carries a smooth current and is not in it.
    The high side is the part on the switch node with a pad on a supply
    rail, the low side the one with a pad on ground; an integrated switcher
    is both. The input capacitor is the one bridging that supply and ground
    closest to the two switches' pads.

    Returns ``{"high", "low", "capacitor", "vin_mm", "ground_mm",
    "switch_mm", "span_mm"}``, or None when the pads' nets do not tell the
    three apart.
    """
    grounds = set(ground_nets or ())
    supplies = set(power_nets or ()) - grounds
    is_capacitor = is_capacitor or (lambda item: item.reference.upper().startswith("C"))
    on_node = [item for item in snapshot.footprints if net_name in item.nets]
    highs = [item for item in on_node if set(item.nets) & supplies]
    lows = [item for item in on_node if set(item.nets) & grounds]
    discrete_highs = [item for item in highs if item not in lows]
    discrete_lows = [item for item in lows if item not in highs]
    if discrete_highs and discrete_lows:
        high, low = min(
            ((a, b) for a in discrete_highs for b in discrete_lows),
            key=lambda pair: math.dist(pair[0].position, pair[1].position),
        )
    else:
        both = [item for item in highs if item in lows]
        if not both:
            return None
        high = low = both[0]

    def pads(item, nets):
        return [(x, y) for net, x, y in item.net_positions if net in nets]

    supply = set(high.nets) & supplies
    high_supply, low_ground = pads(high, supply), pads(low, grounds)
    if not high_supply or not low_ground:
        return None
    best = None
    for item in snapshot.footprints:
        if item is high or item is low or not is_capacitor(item):
            continue
        cap_supply, cap_ground = pads(item, supply), pads(item, grounds)
        if not cap_supply or not cap_ground:
            continue
        vin = min(math.dist(a, b) for a in high_supply for b in cap_supply)
        ground = min(math.dist(a, b) for a in low_ground for b in cap_ground)
        if best is None or vin + ground < best[0]:
            best = (vin + ground, item, vin, ground)
    if best is None:
        return None
    _total, capacitor, vin, ground = best
    switch = math.dist(high.position, low.position)
    return {
        "high": high.reference, "low": low.reference, "capacitor": capacitor.reference,
        "vin_mm": vin, "ground_mm": ground, "switch_mm": switch,
        "span_mm": max(vin, ground, switch),
    }


def switch_node_evidence(footprints, rail_nets=()):
    """``{net: references}`` for SW-named nets that join an inductor to an IC.

    A converter's switch node runs from the converter to its inductor, which
    tells /J2_SRC_SW from a switched rail named the same way. Rails and
    unconnected pins are never switch nodes. The references are every part on
    the net, ICs first, so discovery finds the converter's frequency on
    whichever part the power tree or a Fsw field gives it to.
    """
    rail_nets = set(rail_nets or ())
    parts = defaultdict(set)
    for footprint in footprints or ():
        reference = str(getattr(footprint, "reference", "") or "")
        for pad in getattr(footprint, "pads", ()) or ():
            name = getattr(pad, "net_name", None)
            if name is None:
                name = getattr(getattr(pad, "net", None), "name", "")
            name = str(name or "")
            if (reference and SW_NAME_PATTERN.search(name) and name not in rail_nets
                    and not is_unconnected_pin_net(name)):
                parts[name].add(reference)
    evidence = {}
    for net, references in parts.items():
        ics = sorted(reference for reference in references if _IC_REF_RE.match(reference))
        if ics and any(_INDUCTOR_REF_RE.match(reference) for reference in references):
            evidence[net] = tuple(ics) + tuple(sorted(references - set(ics)))
    return evidence


# kiapi.board.types.PadType.PT_PTH: kipy's Pad.pad_type for a plated hole.
PAD_TYPE_PTH = 1


@dataclass(frozen=True)
class EMCTrack:
    net_name: str
    start: tuple
    end: tuple
    width_mm: float
    layer_id: int
    length_mm: float


@dataclass(frozen=True)
class EMCVia:
    net_name: str
    position: tuple
    layer_ids: tuple = ()
    # Half the land's larger side, 0.0 when not captured (vias).
    land_radius_mm: float = 0.0


@dataclass(frozen=True)
class EMCFootprint:
    reference: str
    value: str
    position: tuple
    nets: tuple = ()
    net_positions: tuple = ()


@dataclass
class EMCGeometrySnapshot:
    """Worker-safe snapshot; no KiCad IPC object crosses the GUI boundary."""
    bounds_mm: tuple
    stackup: object
    tracks: list = field(default_factory=list)
    vias: list = field(default_factory=list)
    footprints: list = field(default_factory=list)
    zones_by_net: dict = field(default_factory=dict)
    power_nets: set = field(default_factory=set)
    # {net: nominal V} of the power tree's rails, which Phase 10's board
    # circuits take their input and output voltages from.
    rail_voltages: dict = field(default_factory=dict)
    ignored_offboard_items: int = 0
    ignored_offboard_nets: tuple = ()
    ignored_offboard_counts: dict = field(default_factory=dict)
    inductors: list = field(default_factory=list)
    # Plated through-hole pads, as vias spanning every copper layer: a pin's
    # barrel joins the pour pieces it passes as a via's does (GP-003).
    through_pads: list = field(default_factory=list)
    # {reference: power_tree.ConverterOutput}: each switching converter's
    # output current and frequency in the power tree, under every reference
    # it goes by (SW-004, Phase 10's board-circuit load).
    converter_outputs: dict = field(default_factory=dict)
    # {reference: {"capacitor", "powered", "connector"}} from the project's
    # saved files (ingest.declarations.component_classes); empty without them.
    component_classes: dict = field(default_factory=dict)

    @property
    def tracks_by_net(self):
        grouped = defaultdict(list)
        for track in self.tracks:
            grouped[track.net_name].append(track)
        return dict(grouped)

    @classmethod
    def capture(cls, board, settings, rails=None, differential_pairs=None,
                board_file_path=None, log_callback=None, component_classes=None):
        if board is None:
            raise RuntimeError(
                "No live KiCad PCB is connected; EMI/EMC geometry capture was cancelled."
            )
        extractor = GeometryExtractor(board, log_callback=log_callback, board_path=board_file_path)

        def value(obj, name, default=None):
            return extractor._get_val(obj, name, default)

        def items(name):
            return extractor._get_board_items(name) or []

        def point(obj):
            if obj is None:
                return None
            return (to_mm(value(obj, "x", 0.0)), to_mm(value(obj, "y", 0.0)))

        def net_name(obj):
            return str(value(value(obj, "net"), "name", "") or "")

        def pad_land_radius(pad):
            try:
                xmin, ymin, xmax, ymax = extractor.pad_polygon(pad).bounds
            except Exception:
                return 0.0
            return max(xmax - xmin, ymax - ymin) / 2.0

        def field_text(obj, direct_name, field_name=None):
            """Read both scalar and kipy protobuf text-field representations."""
            candidates = [value(obj, direct_name)]
            if field_name:
                candidates.extend([
                    value(obj, field_name),
                    value(value(obj, "definition"), field_name),
                ])
            for candidate in candidates:
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
                text_object = value(candidate, "text")
                nested = value(text_object, "value", value(candidate, "value"))
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
            return ""

        def board_footprint_values(path):
            """Fallback to saved KiCad properties when IPC omits footprint values."""
            if not path:
                return {}
            path = Path(path)
            if path.suffix.lower() != ".kicad_pcb" or not path.is_file():
                return {}
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return {}
            values, reference, footprint_value = {}, "", ""

            def commit():
                if reference and footprint_value:
                    values[reference] = footprint_value

            property_pattern = re.compile(
                r'^\(property\s+"(Reference|Value)"\s+"((?:\\.|[^"\\])*)"'
            )
            active = False
            for line in lines:
                stripped = line.lstrip()
                if stripped.startswith("(footprint "):
                    if active:
                        commit()
                    active, reference, footprint_value = True, "", ""
                    continue
                if not active:
                    continue
                match = property_pattern.match(stripped)
                if not match:
                    continue
                text = match.group(2).replace(r'\"', '"').replace(r'\\', '\\')
                if match.group(1) == "Reference":
                    reference = text
                else:
                    footprint_value = text
            if active:
                commit()
            return values

        bounds = tuple(extractor.get_board_bounds(
            margin_mm=0.0, board_file_path=board_file_path,
        ))
        board_shape = box(*bounds) if box is not None else None

        def inside(position):
            return (bounds[0] <= position[0] <= bounds[2]
                    and bounds[1] <= position[1] <= bounds[3])

        ignored_count = 0
        ignored_counts = defaultdict(int)
        ignored_nets = set()
        tracks = []
        for item in items("tracks"):
            start, end = point(value(item, "start")), point(value(item, "end"))
            name = net_name(item)
            width = to_mm(value(item, "width", 0.0))
            if not name or start is None or end is None or width <= 0:
                continue
            if (max(start[0], end[0]) < bounds[0] or min(start[0], end[0]) > bounds[2]
                    or max(start[1], end[1]) < bounds[1] or min(start[1], end[1]) > bounds[3]):
                ignored_count += 1
                ignored_counts["tracks"] += 1
                ignored_nets.add(name)
                continue
            tracks.append(EMCTrack(
                name, start, end, width, int(value(item, "layer", -1)),
                math.dist(start, end),
            ))

        vias = []
        via_items = list(items("vias"))
        # Some adapters expose vias in the track collection.
        if not via_items:
            via_items = [item for item in items("tracks") if value(item, "diameter") is not None]
        for item in via_items:
            position = point(value(item, "position", value(item, "start")))
            name = net_name(item)
            raw_layers = value(item, "layers", value(item, "layer_ids", [])) or []
            if not raw_layers:
                padstack = value(item, "padstack", value(item, "pad_stack"))
                drill = value(padstack, "drill")
                start_layer = value(drill, "start_layer")
                end_layer = value(drill, "end_layer")
                if start_layer is not None and end_layer is not None:
                    raw_layers = [start_layer, end_layer]
                else:
                    raw_layers = value(padstack, "layers", []) or []
            try:
                layers = tuple(dict.fromkeys(int(layer) for layer in raw_layers if int(layer) >= 3))
            except TypeError:
                layers = ()
            if name and position is not None:
                if not inside(position):
                    ignored_count += 1
                    ignored_counts["vias"] += 1
                    ignored_nets.add(name)
                    continue
                vias.append(EMCVia(name, position, layers))

        footprints, through_pads = [], []
        saved_footprint_values = board_footprint_values(board_file_path)
        all_nets = {track.net_name for track in tracks} | {via.net_name for via in vias}
        for item in items("footprints"):
            reference = field_text(item, "reference") or field_text(item, "ref_des")
            if not reference:
                reference = field_text(item, "reference", "reference_field")
            footprint_value = (
                saved_footprint_values.get(reference)
                or field_text(item, "value", "value_field")
            )
            footprint_position = point(value(item, "position"))
            pads = value(item, "pads")
            if pads is None:
                pads = value(value(item, "definition"), "pads", [])
            pad_points, pad_nets, pad_net_positions = [], set(), []
            for pad in pads or []:
                pad_position = point(value(pad, "position"))
                if pad_position is not None:
                    pad_points.append(pad_position)
                name = net_name(pad)
                if name:
                    pad_nets.add(name)
                    all_nets.add(name)
                    if pad_position is not None:
                        pad_net_positions.append((name, pad_position[0], pad_position[1]))
                        if value(pad, "pad_type") in (PAD_TYPE_PTH, "thru_hole"):
                            through_pads.append(EMCVia(name, pad_position, (), pad_land_radius(pad)))
            if footprint_position is None and pad_points:
                footprint_position = (
                    sum(item[0] for item in pad_points) / len(pad_points),
                    sum(item[1] for item in pad_points) / len(pad_points),
                )
            if reference and footprint_position is not None:
                if not inside(footprint_position):
                    ignored_count += 1
                    ignored_counts["footprints"] += 1
                    ignored_nets.update(pad_nets)
                    continue
                footprints.append(EMCFootprint(
                    reference, footprint_value,
                    footprint_position, tuple(sorted(pad_nets)), tuple(pad_net_positions),
                ))

        power_nets = {rail.net_name for rail in (rails or [])}
        requested_zones = set(settings.reference_net_names) | power_nets
        requested_zones.update(
            source.net_name for source in settings.sources
            if source.enabled and source.net_name
        )
        requested_zones.update(name for name in all_nets if is_switching_node_name(name))
        zones = {name: extractor.get_zone_geometry(name) for name in requested_zones}
        if board_shape is not None:
            zones = {
                name: {
                    layer: geometry.intersection(board_shape)
                    for layer, geometry in data.items()
                    if geometry is not None and not geometry.is_empty
                }
                for name, data in zones.items()
            }
        zones = {name: data for name, data in zones.items() if data}
        if not footprints and (tracks or zones):
            # A partial capture: copper arrived, no footprint did. P02 Rev E,
            # 2026-09-29 16:50: the switch node's copper was measured (SW-002)
            # while every converter read COMPONENT_NOT_IDENTIFIED and no
            # decoupling, ESD or connector rule had a part to check -- scored
            # 87/100 against 25 for the full board an hour earlier.
            raise ValueError(
                "KiCad IPC returned copper but no footprint, a capture taken while the "
                "PCB Editor was still synchronising; the component rules would read a "
                "board without parts. Wait for synchronisation and retry."
            )
        inductors = resolve_inductor_models(
            settings.inductor_models, footprints, rails or [], settings.sources,
        )
        return cls(
            bounds_mm=bounds,
            stackup=extractor.get_stackup_profile(),
            tracks=tracks,
            vias=vias,
            footprints=footprints,
            through_pads=through_pads,
            zones_by_net=zones,
            power_nets=power_nets,
            rail_voltages={
                str(rail.net_name): float(rail.nominal_voltage)
                for rail in (rails or []) if float(getattr(rail, "nominal_voltage", 0.0) or 0.0) > 0.0
            },
            ignored_offboard_items=ignored_count,
            ignored_offboard_nets=tuple(sorted(ignored_nets)),
            ignored_offboard_counts=dict(sorted(ignored_counts.items())),
            inductors=inductors,
            converter_outputs=converter_outputs(rails or []),
            component_classes=dict(component_classes or {}),
        )


def supply_voltages_by_net(footprints, rails):
    """``{net: (voltage, provenance)}`` from the configured rails powering its parts.

    A discovered source with no converter loss model used to take a flat
    3.3 V swing whatever the board. The components connected to the source net
    are powered by configured rails; the highest of those rails is the swing a
    logic output on that net can reach, and it is the conservative choice when
    a part carries more than one supply. Nets that are rails themselves, and
    nets whose parts touch no configured rail, get nothing.
    """
    voltages = {
        str(rail.net_name): float(getattr(rail, "nominal_voltage", 0.0) or 0.0)
        for rail in (rails or ())
        if float(getattr(rail, "nominal_voltage", 0.0) or 0.0) > 0.0
    }
    candidates = {}
    for footprint in footprints or ():
        reference = str(getattr(footprint, "reference", "") or "")
        nets = set()
        for pad in getattr(footprint, "pads", ()) or ():
            name = getattr(pad, "net_name", None)
            if name is None:
                name = getattr(getattr(pad, "net", None), "name", "")
            if name:
                nets.add(str(name))
        supplies = sorted(((voltages[net], net) for net in nets if net in voltages), reverse=True)
        if not supplies:
            continue
        voltage, rail_name = supplies[0]
        for net in nets - set(voltages):
            best = candidates.get(net)
            if best is None or voltage > best[0]:
                candidates[net] = (voltage, rail_name, {reference})
            elif voltage == best[0] and rail_name == best[1]:
                best[2].add(reference)
    return {
        net: (
            voltage,
            f"voltage from {rail_name} ({voltage:g} V), the highest configured rail "
            f"powering {', '.join(sorted(refs))} on this net",
        )
        for net, (voltage, rail_name, refs) in candidates.items()
    }


# A frequency written on a part: "25MHz", "32.768 kHz", "ABM8-25.000MHZ-B2".
_FREQUENCY_RE = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*([kKmMgG]?)\s*[hH][zZ]")
# On a timing part or in a frequency field the unit is often left out: "12M", "32.768k".
_BARE_FREQUENCY_RE = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*([kKmMgG])(?![\w])")
_FREQUENCY_FIELD_RE = re.compile(r"^(frequency|freq|fsw|f_sw|switching[ _]frequency)$", re.I)
_TIMING_REF_RE = re.compile(r"^(Y|X|XTAL|OSC)\d", re.I)
_TIMING_PART_RE = re.compile(r"crystal|oscillator|xtal|resonator", re.I)
_FREQUENCY_MULTIPLIER = {"": 1.0, "k": 1e3, "m": 1e6, "g": 1e9}
_STANDARD_PAIR_FUNDAMENTALS = {
    "USB_HS": (240e6, "240 MHz maximum-toggle fundamental of the 480 Mbit/s USB 2.0 high-speed rate."),
    "USB_FS": (6e6, "6 MHz maximum-toggle fundamental of the 12 Mbit/s USB full-speed rate."),
}
_UNKNOWN_FREQUENCY_NOTE = (
    "Frequency unknown: nothing in the project states it, so no harmonic or field "
    "estimate uses this source until it is entered."
)


def parse_frequency_hz(text, bare_unit=False):
    """The first frequency written in *text*, in hertz, or None."""
    match = _FREQUENCY_RE.search(text or "")
    if match is None and bare_unit:
        match = _BARE_FREQUENCY_RE.search(text or "")
    if match is None:
        return None
    value = float(match.group(1).replace(",", "."))
    return value * _FREQUENCY_MULTIPLIER[match.group(2).lower()] or None


def declared_frequencies(components, rail_nets=()):
    """What a project's parts say about frequencies: ``(by_net, by_part)``.

    ``by_net`` maps each signal net of a crystal, oscillator or resonator to
    the frequency its value or its Frequency field states; ``by_part`` maps a
    reference to a Frequency/Fsw field on any part (a converter's switching
    frequency). Each entry is ``(hertz, where it was read)``. Rails and
    unconnected nets are left out, so an oscillator's supply is not a clock.
    """
    by_net, by_part = {}, {}
    rail_nets = set(rail_nets or ())
    for component in components:
        reference = str(getattr(component, "reference", "") or "")
        field_hz = None
        for key, text in (getattr(component, "extra_fields", None) or {}).items():
            if _FREQUENCY_FIELD_RE.match(str(key).strip()):
                field_hz = parse_frequency_hz(str(text), bare_unit=True)
                if field_hz:
                    by_part[reference.upper()] = (field_hz, f"the {key} field of {reference} ({text})")
                    break
        value = str(getattr(component, "value", "") or "")
        timing = (
            _TIMING_REF_RE.match(reference)
            or _TIMING_PART_RE.search(str(getattr(component, "lib_id", "") or ""))
            or _TIMING_PART_RE.search(value)
        )
        if not timing:
            continue
        if field_hz:
            declared = by_part[reference.upper()]
        else:
            hz = parse_frequency_hz(value, bare_unit=True)
            if not hz:
                continue
            declared = (hz, f"the value of {reference} ({value})")
        for net in set((getattr(component, "nets_by_pad", None) or {}).values()):
            if net and net not in rail_nets and not net.startswith("unconnected-"):
                by_net.setdefault(net, declared)
    return by_net, by_part


class EMCSourceDiscoverer:
    """Name/evidence based source discovery with manual-setting preservation.

    A net's name says a source may be there; it does not say at what
    frequency. Frequencies come from the power tree's converter loss models,
    from what the project's parts state (``declared_frequencies``) or from
    the user. Otherwise a source's frequency is 0 -- unknown -- where it used
    to be 25 MHz for a clock, 500 kHz for a switching node, 100 MHz for a fast
    interface and 240 or 125 MHz for a differential pair, figures nothing on
    the board had stated (docs/plan-universalite.md, 5.2).
    """
    CLOCK_PATTERN = re.compile(r"(^|[_/])(CLK|CLOCK|MCLK|BCLK|SCLK|XTAL)([_/]|$)", re.I)
    FAST_PATTERN = re.compile(r"USB|HDMI|PCIE|ETH|RMII|RGMII|MIPI|LVDS", re.I)

    @classmethod
    def is_switching_node(cls, name):
        """Reject switched DC rails such as VBUS_PD_SW while retaining real PH/LX/SW nodes."""
        return is_switching_node_name(name)

    @staticmethod
    def _usb_levels(name):
        if "USB" in name.upper():
            # USB 2.0 high-speed nominal differential swing is about 400 mV.
            return 0.4, 0.008
        return 3.3, 0.1

    @staticmethod
    def _rail_swing(name, supply_voltages, default_notes):
        """(voltage, notes) for a source with no converter loss model.

        The configured rail powering the parts on the net when there is one,
        said in the notes; otherwise the flat default, also said. The current
        is never derived here, so the notes keep calling it assumed.
        """
        supplied = (supply_voltages or {}).get(name)
        if supplied is None:
            return 3.3, default_notes
        voltage, provenance = supplied
        return float(voltage), (
            f"Swing {provenance}; the 0.1 A current and the remaining parameters are "
            "assumed defaults to confirm."
        )

    @staticmethod
    def _declared(net_frequencies, net):
        """(frequency, note, confidence) for a net a part states a frequency for, or unknown."""
        declared = (net_frequencies or {}).get(net)
        if declared is None:
            return 0.0, _UNKNOWN_FREQUENCY_NOTE, "LOW"
        return float(declared[0]), f"Frequency from {declared[1]}.", "MEDIUM"

    @classmethod
    def discover(cls, net_names, existing=None, differential_pairs=None,
                 switching_frequencies=None, supply_voltages=None,
                 net_frequencies=None, part_frequencies=None, switch_nodes=None):
        """Sources on *net_names*, keeping the user's manual ones.

        *switch_nodes* is ``switch_node_evidence`` for the board: SW-named nets
        that join an inductor to an IC, with the parts on them.
        """
        retained = {source.net_name: source for source in (existing or []) if source.source == "manual"}
        switching_frequencies = switching_frequencies or {}
        part_frequencies = part_frequencies or {}
        switch_nodes = switch_nodes or {}
        enabled_pairs = [
            pair for pair in (differential_pairs or [])
            if getattr(pair, "enabled", True)
        ]
        paired_nets = {
            net_name for pair in enabled_pairs
            for net_name in (pair.positive_net, pair.negative_net)
        }
        discovered = []
        for name in sorted(set(net_names)):
            if name in retained or is_unconnected_pin_net(name):
                continue
            if cls.CLOCK_PATTERN.search(name):
                frequency, frequency_note, confidence = cls._declared(net_frequencies, name)
                voltage, notes = cls._rail_swing(
                    name, supply_voltages, "Name-based clock defaults; confirm all electrical parameters.",
                )
                discovered.append(EMCSignalSource(
                    name, name, "CLOCK", frequency, 2.0, source="auto",
                    voltage_swing_v=voltage, parameter_confidence=confidence,
                    parameter_notes=f"{frequency_note} {notes}",
                ))
            elif cls.is_switching_node(name) or name in switch_nodes:
                ref_match = re.search(
                    r"(^|[^A-Z0-9])(U\d+)[_-]SW(?:[^A-Z0-9]|$)", name, re.I,
                )
                candidates = ([ref_match.group(2).upper()] if ref_match else []) + [
                    reference.upper() for reference in switch_nodes.get(name, ())
                ]
                ref_des = next(
                    (reference for reference in candidates if reference in switching_frequencies),
                    next((reference for reference in candidates if reference in part_frequencies),
                         candidates[0] if candidates else ""),
                )
                parameters = switching_frequencies.get(ref_des)
                confidence = "LOW"
                if isinstance(parameters, dict):
                    frequency = float(parameters.get("frequency_hz", 0.0))
                    voltage = float(parameters.get("voltage_swing_v", 3.3))
                    current = float(parameters.get("current_a", 0.1))
                    origin, confidence = "power-tree", "MEDIUM"
                    notes = (
                        "Frequency from converter loss model; voltage from input rail; "
                        "current from downstream load; 10 ns rise time remains an editable estimate."
                    )
                else:
                    current, origin = 0.1, "auto"
                    if parameters is not None:
                        frequency, frequency_note = float(parameters), "Switching frequency supplied by the caller."
                    elif ref_des in part_frequencies:
                        declared = part_frequencies[ref_des]
                        frequency, frequency_note = float(declared[0]), f"Frequency from {declared[1]}."
                        confidence = "MEDIUM"
                    else:
                        frequency, frequency_note = 0.0, _UNKNOWN_FREQUENCY_NOTE
                    voltage, notes = cls._rail_swing(
                        name, supply_voltages,
                        "Name-based switching-source defaults; confirm all electrical parameters.",
                    )
                    notes = f"{frequency_note} {notes}"
                if name in switch_nodes:
                    parts = switch_nodes[name]
                    inductors = ", ".join(part for part in parts if _INDUCTOR_REF_RE.match(part))
                    ics = ", ".join(part for part in parts if _IC_REF_RE.match(part))
                    notes = f"Switch node: the net joins {inductors} to {ics}. {notes}"
                discovered.append(EMCSignalSource(
                    name, name, "SWITCHING", frequency, 10.0, source=origin,
                    voltage_swing_v=voltage, current_a=current,
                    parameter_confidence=confidence,
                    parameter_notes=notes,
                ))
            elif cls.FAST_PATTERN.search(name) and name not in paired_nets:
                frequency, frequency_note, _confidence = cls._declared(net_frequencies, name)
                voltage, current = cls._usb_levels(name)
                notes = "Interface-name defaults; confirm activity rate and edge time."
                if "USB" not in name.upper():
                    # USB levels are set by the standard, not by a supply.
                    voltage, notes = cls._rail_swing(name, supply_voltages, notes)
                discovered.append(EMCSignalSource(
                    name, name, "DIGITAL", frequency, 1.0, source="auto",
                    voltage_swing_v=voltage, current_a=current,
                    parameter_confidence="LOW",
                    parameter_notes=f"{frequency_note} {notes}",
                ))
        # A timing part the project declares is a clock source whatever its nets
        # are called: crystal nets are named /XI, Net-(U10-XTAL1_IN) and the
        # like, which the name patterns miss. One source per part, on its first
        # signal net; skipped when a named source already covers one of them.
        covered = set(retained) | {source.net_name for source in discovered} | paired_nets
        by_part = defaultdict(list)
        for net, (hz, where) in (net_frequencies or {}).items():
            by_part[(hz, where)].append(net)
        for (hz, where), nets in sorted(by_part.items(), key=lambda item: item[0][1]):
            if covered.intersection(nets):
                continue
            net = sorted(nets)[0]
            voltage, notes = cls._rail_swing(
                net, supply_voltages, "Electrical parameters are defaults; confirm them.",
            )
            discovered.append(EMCSignalSource(
                f"Clock ({where})", net, "CLOCK", float(hz), 2.0, source="auto",
                voltage_swing_v=voltage, parameter_confidence="MEDIUM",
                parameter_notes=f"Frequency from {where}, on nets {', '.join(sorted(nets))}. {notes}",
            ))
        for pair in enabled_pairs:
            if pair.positive_net in retained or pair.negative_net in retained:
                continue
            interface = str(pair.interface or "").upper()
            if interface in _STANDARD_PAIR_FUNDAMENTALS:
                # A pair whose interface names its speed: the standard fixes
                # the rate. USB 2.0 HS is 480 Mbit/s NRZI, and a maximum-
                # transition pattern alternates every bit, so its square-wave
                # fundamental is 240 MHz; 480 MHz is the bit rate.
                frequency, frequency_note = _STANDARD_PAIR_FUNDAMENTALS[interface]
                confidence = "MEDIUM"
            else:
                # "USB" alone does not say high, full or low speed.
                frequency, frequency_note, confidence = cls._declared(net_frequencies, pair.positive_net)
            voltage, current = cls._usb_levels(pair.name + " " + pair.interface)
            discovered.append(EMCSignalSource(
                pair.name, pair.positive_net, "DIFFERENTIAL", frequency,
                0.8, source="differential-scan",
                voltage_swing_v=voltage, current_a=current,
                negative_net_name=pair.negative_net,
                parameter_confidence=confidence,
                parameter_notes=(
                    f"Differential nets confirmed by pair scan. {frequency_note} "
                    "Electrical levels, edge time and activity remain editable defaults."
                ),
            ))
        # A saved source keeps the frequency a fresh scan cannot supply. P02
        # Rev E saved its /USB_J2_MUX_D pair at 240 MHz; rescanned, the pair's
        # "USB" interface does not say which speed, the value was dropped, and
        # EE-003 reported the frequency unknown.
        saved = {
            (source.net_name, source.kind.upper()): source for source in (existing or [])
            if source.source != "manual" and float(source.frequency_hz or 0.0) > 0.0
        }
        for index, source in enumerate(discovered):
            previous = saved.get((source.net_name, source.kind.upper()))
            if previous is not None and float(source.frequency_hz or 0.0) <= 0.0:
                discovered[index] = replace(
                    source, frequency_hz=float(previous.frequency_hz),
                    parameter_confidence=previous.parameter_confidence,
                    parameter_notes=previous.parameter_notes,
                )
        return list(retained.values()) + discovered


class EMCAnalyzer:
    """Rule engine for explainable PCB-level EMI/EMC risks."""

    def __init__(self, snapshot, settings, differential_pairs=None,
                 differential_results=None, ac_results=None, thermal_result=None,
                 log_callback=None):
        self.snapshot = snapshot
        self.settings = settings
        self.differential_pairs = list(differential_pairs or [])
        self.differential_results = dict(differential_results or {})
        self.ac_results = list(ac_results or [])
        self.thermal_result = thermal_result
        self.log_callback = log_callback
        self.result = EMCAnalysisResult()
        self._tracks = snapshot.tracks_by_net
        self._categories = {item.upper() for item in settings.enabled_categories}
        self._copper_layers = [
            layer for layer in snapshot.stackup.layers if layer.kind.upper() == "COPPER"
        ]
        self._outer_layers = {
            self._copper_layers[0].layer_id, self._copper_layers[-1].layer_id
        } if self._copper_layers else set()
        self._ground_zones = self._merge_ground_zones()
        self._ground_plane_layers = self._identify_ground_plane_layers()
        self._reference_ground_zones = {
            layer: geometry for layer, geometry in self._ground_zones.items()
            if layer in self._ground_plane_layers
        }
        self._source_by_net = {
            source.net_name: source for source in settings.sources if source.enabled
        }
        self._fast_nets = {
            name for name, source in self._source_by_net.items()
            if source.kind.upper() != "SWITCHING"
        }
        for pair in self.differential_pairs:
            if getattr(pair, "enabled", True):
                self._fast_nets.update((pair.positive_net, pair.negative_net))
        # A source with no frequency of its own that names its own return
        # conductor is a DC feed and its return: its current comes back
        # through that conductor, not through the plane under it, so neither
        # the plane-gap check (GP-001) nor the ground-via check (RP-001)
        # applies, and the via RP-001 asks for would join an isolated
        # supply's primary to its secondary. poe_bt's PoE centre taps
        # (POE_CT12 back on POE_CT36, POE_CT45 on POE_CT78) drew both. A
        # source with edges of its own keeps both: a pair's common mode
        # still needs the plane.
        self._dc_feed_nets = set()
        for name, source in self._source_by_net.items():
            negative = str(source.negative_net_name or "").strip()
            if negative and float(source.frequency_hz or 0.0) <= 0.0:
                self._dc_feed_nets.update((name, negative))
        self._checks = 0

    def log(self, message):
        if self.log_callback:
            self.log_callback(message)

    def _enabled(self, category):
        return category in self._categories

    def _check(self, count=1):
        self._checks += count

    def _add(self, rule_id, category, severity, title, description, recommendation,
             confidence="MEDIUM", nets=None, components=None, location=None, detail=""):
        evidence = []
        if location is not None:
            x, y, layer = location
            evidence.append(EMCEvidence(
                "PCB_GEOMETRY", detail or description, float(x), float(y),
                int(layer) if layer is not None else None,
            ))
        finding = EMCFinding(
            rule_id, category, severity, title, description, recommendation,
            confidence, list(nets or []), list(components or []), evidence,
        )
        self.result.findings.append(finding)
        if location is not None and severity in {"CRITICAL", "HIGH", "MEDIUM"}:
            self.result.probe_points.append(EMCProbePoint(
                float(location[0]), float(location[1]), title, rule_id,
            ))

    def _merge_ground_zones(self):
        by_layer = defaultdict(list)
        for name in self.settings.reference_net_names:
            for layer, geometry in self.snapshot.zones_by_net.get(name, {}).items():
                if geometry is not None and not geometry.is_empty:
                    by_layer[int(layer)].append(geometry)
        if unary_union is None:
            return {layer: shapes[0] for layer, shapes in by_layer.items() if shapes}
        return {layer: unary_union(shapes) for layer, shapes in by_layer.items() if shapes}

    # How far from a via's centre a pour piece is taken to touch it: the
    # via's land, with a thermal relief's spokes, is no wider than this.
    GROUND_VIA_REACH_MM = 0.5

    def _ground_islands(self):
        """``{layer: [piece]}``: ground pour pieces joined to their net's main ground by nothing.

        Each reference net is its own ground: an isolated supply's primary
        and secondary grounds are two, and neither is an island of the
        other. poe_bt declares GND and the PoE side's /POE_MOD_VN; counted
        as one ground, the larger /POE_MOD_VN pour made every GND piece of
        In1 and In2 an island, ten a layer.
        """
        islands = defaultdict(list)
        for net in dict.fromkeys(self.settings.reference_net_names):
            for layer, parts in self._net_islands(net).items():
                islands[layer].extend(parts)
        return dict(islands)

    def _net_islands(self, net):
        """``{layer: [piece]}``: *net*'s pour pieces joined to its main body by nothing.

        A pour splits into pieces wherever tracks and pads cut it, and the
        small ring a fill leaves around a via is a piece of its own. A ground
        via or plated through-hole pad joins what it lands on, on every layer
        it passes; a ground track joins the pieces, tracks and holes it runs
        over. Counted bare, a plane's via rings and a two-layer board's pour
        pieces tied by their pins and back-side tracks read as fragmented.
        The main ground is the joined group of greatest pour area; every
        piece outside it is an island.
        """
        pieces = []
        for layer, geometry in (self.snapshot.zones_by_net.get(net) or {}).items():
            if geometry is None:
                continue
            for part in getattr(geometry, "geoms", [geometry]):
                if not part.is_empty:
                    pieces.append((int(layer), part))
        if not pieces:
            return {}
        tracks = [
            (track.layer_id, LineString([track.start, track.end]).buffer(max(track.width_mm, 0.0) / 2.0))
            for track in self._tracks.get(net, ())
        ]
        order = [int(layer.layer_id) for layer in self._copper_layers if layer.layer_id is not None]
        holes = []
        for joint in [*self.snapshot.vias, *self.snapshot.through_pads]:
            if joint.net_name != net:
                continue
            ends = [order.index(layer) for layer in joint.layer_ids if layer in order]
            spanned = order[min(ends):max(ends) + 1] if len(ends) >= 2 else order
            reach = max(self.GROUND_VIA_REACH_MM, float(joint.land_radius_mm) + 0.05)
            holes.append((spanned, Point(joint.position).buffer(reach)))

        # Union-find over pieces, then tracks, then holes.
        parent = list(range(len(pieces) + len(tracks) + len(holes)))

        def root(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def join(first, others):
            for other in others:
                parent[root(other)] = root(first)

        # Per layer, the copper a hole or track can meet there.
        copper = defaultdict(list)
        for index, (layer, part) in enumerate(pieces):
            copper[layer].append((index, part))
        for offset, (layer, body) in enumerate(tracks):
            copper[layer].append((len(pieces) + offset, body))
        trees = {
            layer: (STRtree([geometry for _index, geometry in items]), [index for index, _g in items])
            for layer, items in copper.items()
        }

        def touched(layer, geometry):
            if layer not in trees:
                return []
            tree, indices = trees[layer]
            return [indices[int(found)] for found in tree.query(geometry, predicate="intersects")]

        for offset, (layer, body) in enumerate(tracks):
            join(len(pieces) + offset, touched(layer, body))
        for offset, (spanned, land) in enumerate(holes):
            join(len(pieces) + len(tracks) + offset, [
                index for layer in spanned for index in touched(layer, land)
            ])
        area = defaultdict(float)
        for index, (_layer, part) in enumerate(pieces):
            area[root(index)] += float(part.area)
        if not area:
            return {}
        main = max(area, key=area.get)
        islands = defaultdict(list)
        for index, (layer, part) in enumerate(pieces):
            if root(index) != main:
                islands[layer].append(part)
        return dict(islands)

    def _identify_ground_plane_layers(self):
        """Distinguish dedicated reference planes from incidental GND pours."""
        aliases = {name.upper() for name in self.settings.reference_net_names}
        explicit = {
            int(layer.layer_id) for layer in self._copper_layers
            if layer.layer_id is not None and any(
                re.search(rf"(^|[^A-Z0-9]){re.escape(alias)}([^A-Z0-9]|$)", layer.name.upper())
                for alias in aliases
            )
        }
        explicit &= set(self._ground_zones)
        if explicit:
            return explicit
        bounds = self.snapshot.bounds_mm
        board_area = max((bounds[2] - bounds[0]) * (bounds[3] - bounds[1]), 1e-9)
        substantial = {
            layer for layer, geometry in self._ground_zones.items()
            if float(geometry.area) / board_area >= 0.60
        }
        if substantial:
            return substantial
        if not self._ground_zones:
            return set()
        # With no layer-role metadata, use only the largest pour as the
        # tentative reference plane instead of treating every GND island as one.
        return {max(self._ground_zones, key=lambda layer: float(self._ground_zones[layer].area))}

    def _layer_positions(self):
        positions, cursor = {}, 0.0
        for layer in self.snapshot.stackup.layers:
            thickness = max(float(layer.thickness_mm), 0.0)
            if layer.kind.upper() == "COPPER" and layer.layer_id is not None:
                positions[int(layer.layer_id)] = cursor + thickness / 2.0
            cursor += thickness
        return positions

    def _nearest_ground_layer(self, layer_id):
        positions = self._layer_positions()
        if layer_id not in positions or not self._reference_ground_zones:
            return None, None
        candidates = [(abs(positions[layer_id] - positions[item]), item)
                      for item in self._reference_ground_zones if item in positions and item != layer_id]
        if not candidates:
            return None, None
        distance, reference = min(candidates)
        return reference, distance

    def _copper_thickness(self, layer_id):
        for layer in self._copper_layers:
            if layer.layer_id is not None and int(layer.layer_id) == int(layer_id):
                return max(float(layer.thickness_mm), 1.0e-4)
        return DEFAULT_COPPER_THICKNESS_MM

    def _dielectric_between(self, layer_a, layer_b):
        """Thickness-weighted epsilon_r of the dielectric separating two layers.

        A pair separated by prepreg and core sees neither value alone, so the
        substrate the field actually crosses is averaged by thickness.  Falls
        back to 4.4 (FR-4) when the stackup carries no permittivity, which is
        the same assumption the rest of the analyzer already makes.
        """
        positions = self._layer_positions()
        if layer_a not in positions or layer_b not in positions:
            return 4.4
        low, high = sorted((positions[layer_a], positions[layer_b]))
        cursor, weighted, total = 0.0, 0.0, 0.0
        for layer in self.snapshot.stackup.layers:
            thickness = max(float(layer.thickness_mm), 0.0)
            centre = cursor + thickness / 2.0
            cursor += thickness
            if layer.kind.upper() != "DIELECTRIC" or not (low < centre < high):
                continue
            epsilon = float(getattr(layer, "epsilon_r", 0.0) or 0.0)
            if epsilon >= 1.0:
                weighted += epsilon * thickness
                total += thickness
        return weighted / total if total > 0.0 else 4.4

    def _references_around(self, layer_id):
        """Nearest reference plane above and below a layer, by stackup position."""
        positions = self._layer_positions()
        if layer_id not in positions:
            return (None, None), (None, None)
        here = positions[layer_id]
        above = below = (None, None)
        for reference in self._reference_ground_zones:
            if reference not in positions or reference == layer_id:
                continue
            offset = positions[reference] - here
            if offset > 0 and (above[1] is None or offset < above[1]):
                above = (reference, offset)
            elif offset < 0 and (below[1] is None or -offset < below[1]):
                below = (reference, -offset)
        return above, below

    def _coupled_modes(self, track, gap_mm, width_mm):
        """Solve the even/odd modes of this trace pair, or explain why not.

        Returns ``(modes, topology, note)``; ``modes`` is None when the
        cross-section cannot be solved, in which case ``note`` says why and the
        caller must fall back to the geometric rule rather than invent a number.
        """
        if SymmetricCoupledLineSolver is None:
            return None, "", "the 2-D solver is unavailable in this runtime"
        if gap_mm <= 0.0:
            return None, "", "the traces overlap once their widths are removed"
        if gap_mm > COUPLING_SOLVER_MAX_GAP_MM:
            return None, "", (
                f"the {gap_mm:.3f} mm gap is beyond the {COUPLING_SOLVER_MAX_GAP_MM:g} mm "
                "range where the 2-D solver is trustworthy"
            )
        layer_id = int(track.layer_id)
        thickness = self._copper_thickness(layer_id)
        above, below = self._references_around(layer_id)
        try:
            if above[0] is not None and below[0] is not None:
                return (
                    SymmetricCoupledLineSolver.solve_stripline_pair(
                        width_mm, gap_mm, thickness,
                        above[1], self._dielectric_between(layer_id, above[0]),
                        below[1], self._dielectric_between(layer_id, below[0]),
                    ),
                    "stripline",
                    "",
                )
            reference, height = (below if below[0] is not None else above)
            if reference is None or not height:
                return None, "", "no reference plane was found for this layer"
            return (
                SymmetricCoupledLineSolver.solve_microstrip_pair(
                    width_mm, gap_mm, thickness, height,
                    self._dielectric_between(layer_id, reference),
                ),
                "microstrip",
                "",
            )
        except (ValueError, ZeroDivisionError, ArithmeticError) as exc:
            return None, "", f"the 2-D solve failed ({exc})"

    def _pair_reference_coverage(self, pair, tracks):
        """Measure adjacent-plane and coplanar return coverage point by point."""
        if Point is None or not tracks:
            return 0.0, 0.0, 0.0, []
        pair_result = self.differential_results.get(pair.signature)
        coplanar_reach = {}
        if pair_result is not None:
            for section in getattr(pair_result, "sections", []) or []:
                if str(section.topology).startswith("COPLANAR") and section.ground_clearance_mm > 0.0:
                    reach = section.ground_clearance_mm + 0.5 * section.width_mm + 0.03
                    coplanar_reach[section.layer_id] = max(
                        coplanar_reach.get(section.layer_id, 0.0), reach,
                    )
        adjacent_weight = coplanar_weight = combined_weight = total_weight = 0.0
        uncovered_intervals = []
        for track in tracks:
            reference, reference_distance = self._nearest_ground_layer(track.layer_id)
            adjacent_zone = self._reference_ground_zones.get(reference)
            same_layer_zone = self._ground_zones.get(track.layer_id)
            reach = coplanar_reach.get(track.layer_id, 0.0)
            if reach <= 0.0 and same_layer_zone is not None:
                # Remain autonomous when no differential analysis has been run
                # in the current KiCad session.  A lateral GND edge within two
                # reference heights (or 1.5 trace widths) is close enough to
                # provide a credible coplanar return path for this rule.
                edge_reach = max(
                    0.15,
                    2.0 * float(reference_distance or 0.0),
                    1.5 * float(track.width_mm),
                )
                reach = 0.5 * float(track.width_mm) + edge_reach + 0.02
            samples = max(3, min(81, int(math.ceil(track.length_mm / 0.25)) + 1))
            sample_weight = track.length_mm / samples
            uncovered_start = None
            for index in range(samples):
                fraction = (index + 0.5) / samples
                point = Point(
                    track.start[0] + fraction * (track.end[0] - track.start[0]),
                    track.start[1] + fraction * (track.end[1] - track.start[1]),
                )
                adjacent = bool(adjacent_zone is not None and adjacent_zone.covers(point))
                coplanar = bool(
                    reach > 0.0 and same_layer_zone is not None
                    and same_layer_zone.distance(point) <= reach
                )
                adjacent_weight += sample_weight if adjacent else 0.0
                coplanar_weight += sample_weight if coplanar else 0.0
                combined_weight += sample_weight if adjacent or coplanar else 0.0
                total_weight += sample_weight
                uncovered = not (adjacent or coplanar)
                if uncovered and uncovered_start is None:
                    uncovered_start = index
                if uncovered_start is not None and (not uncovered or index == samples - 1):
                    stop = index if not uncovered else index + 1
                    start_fraction = uncovered_start / samples
                    stop_fraction = stop / samples
                    midpoint_fraction = 0.5 * (start_fraction + stop_fraction)
                    uncovered_intervals.append({
                        "net_name": track.net_name,
                        "layer_id": track.layer_id,
                        "length_mm": track.length_mm * (stop_fraction - start_fraction),
                        "position": (
                            track.start[0] + midpoint_fraction * (track.end[0] - track.start[0]),
                            track.start[1] + midpoint_fraction * (track.end[1] - track.start[1]),
                        ),
                    })
                    uncovered_start = None
        denominator = max(total_weight, 1e-12)
        return (
            100.0 * adjacent_weight / denominator,
            100.0 * coplanar_weight / denominator,
            100.0 * combined_weight / denominator,
            sorted(uncovered_intervals, key=lambda item: item["length_mm"], reverse=True),
        )

    def _ground_rules(self):
        if not self._enabled("GROUND"):
            return
        self._check(4)
        # Only claim "no ground plane" once zone capture demonstrably worked
        # for *some* net; an IPC answer with zero zones anywhere (still
        # syncing) says nothing about whether a ground plane exists.
        if len(self._copper_layers) >= 4 and self.snapshot.zones_by_net and not self._ground_zones:
            self._add("GP-002", "GROUND", "CRITICAL", "No ground plane detected",
                      "The multilayer board has no filled zone on a configured ground net.",
                      "Add a continuous ground plane and verify the configured ground-net aliases.", "HIGH")
            return
        bounds = self.snapshot.bounds_mm
        board_area = max((bounds[2] - bounds[0]) * (bounds[3] - bounds[1]), 1e-9)
        islands = self._ground_islands()
        for layer in sorted(self._ground_plane_layers):
            geometry = self._ground_zones[layer]
            if len(islands.get(layer, ())) >= 3:
                self._add("GP-003", "GROUND", "HIGH", "Fragmented ground plane",
                          f"Ground copper on layer {layer} contains {len(islands[layer])} islands "
                          "that no via or track joins to the rest of the ground.",
                          "Remove isolated islands or join them with ground copper and stitching vias.",
                          "HIGH", location=(*geometry.centroid.coords[0], layer))
            fill = 100.0 * float(geometry.area) / board_area
            if fill < 60.0:
                self._add("GP-004", "GROUND", "MEDIUM", "Low ground-plane coverage",
                          f"Ground fill on layer {layer} covers about {fill:.1f}% of the board envelope.",
                          "Increase continuous ground coverage, prioritising regions below fast signals.",
                          "MEDIUM", location=(*geometry.centroid.coords[0], layer))
        present_domains = [name for name in self.settings.reference_net_names
                           if self.snapshot.zones_by_net.get(name)]
        if len(present_domains) > 1:
            self._add("GP-005", "GROUND", "MEDIUM", "Multiple ground domains",
                      "Filled copper exists on multiple configured grounds: " + ", ".join(present_domains) + ".",
                      "Verify intentional single-point connections and prevent signals crossing domain boundaries.",
                      "MEDIUM", nets=present_domains)

    def _stackup_rules(self):
        if not self._enabled("STACKUP"):
            return
        self._check(3)
        if not self._copper_layers:
            return
        plane_layers = set(self._ground_zones)
        # A signal layer carries signal tracks. One with pours alone is a
        # plane -- P02 Rev E's PWR.L3 and PWR.L4 hold its rails -- and has no
        # signal to couple or to reference; so is one whose only tracks are
        # rails or grounds: the 2026-09-29 board routes /J2_VBUS_FUSED on
        # both, 11.4 and 46.4 mm, and SU-001 came back as the first action.
        non_signal = set(self.snapshot.power_nets) | set(self.settings.reference_net_names)
        routed_layers = {
            track.layer_id for track in self.snapshot.tracks if track.net_name not in non_signal
        }
        signal_layers = [layer for layer in self._copper_layers
                         if layer.layer_id not in plane_layers and layer.layer_id in routed_layers]
        signal_ids = {layer.layer_id for layer in signal_layers}
        for first, second in zip(self._copper_layers, self._copper_layers[1:]):
            if first.layer_id in signal_ids and second.layer_id in signal_ids:
                self._add("SU-001", "STACKUP", "HIGH", "Adjacent signal layers",
                          f"{first.name} and {second.name} have no detected reference plane between them.",
                          "Use an adjacent solid ground plane or route the layers in orthogonal directions.",
                          "MEDIUM")
        for layer in signal_layers:
            reference, distance = self._nearest_ground_layer(layer.layer_id)
            if distance is not None and distance > 0.3:
                self._add("SU-002", "STACKUP", "LOW", "Signal layer far from reference",
                          f"{layer.name} is approximately {distance:.3f} mm from the nearest detected ground plane.",
                          "Reduce signal-to-reference spacing in the fabrication stackup.", "HIGH")

    @staticmethod
    def _track_midpoint(track):
        return ((track.start[0] + track.end[0]) / 2.0,
                (track.start[1] + track.end[1]) / 2.0)

    def _classes(self, item):
        """The project files' classes for a footprint, or None when they do not classify it."""
        return (getattr(self.snapshot, "component_classes", None) or {}).get(item.reference)

    def _is_connector(self, item):
        # The user's connector prefixes, or a Connector symbol or footprint library.
        return (
            any(item.reference.upper().startswith(prefix.upper())
                for prefix in self.settings.external_connector_prefixes)
            or "connector" in (self._classes(item) or ())
        )

    def _is_capacitor(self, item):
        # A capacitor symbol or footprint says so; a connector symbol says not,
        # whatever its reference; otherwise the C annotation prefix.
        classes = self._classes(item) or set()
        if "capacitor" in classes:
            return True
        return "connector" not in classes and item.reference.upper().startswith("C")

    def _needs_decoupling(self, item):
        """A part with a power_in pin, or a U/IC part; never a capacitor or a connector.

        The pin type adds parts the prefix misses (a module annotated M1) but
        does not replace it: vendor symbols often type their supply pins
        passive, and on the reference board main 96 of 114 U parts have no
        power_in pin at all.
        """
        if self._is_connector(item) or self._is_capacitor(item):
            # A connector's VBUS pin is often typed power_in, and connectors
            # from private libraries carry no Connector library name.
            return False
        if _PROTECTION_REF_RE.match(item.reference) or _ESD_PROTECTOR_VALUE_RE.search(item.value or ""):
            # A diode or TVS clamps its line and draws no supply current. P02
            # Rev E's TVS2200 on VBUS_PD types its ground pin power_in, and
            # DE-001 (HIGH) asked for the capacitor 24 mm away to move closer.
            return False
        return "powered" in (self._classes(item) or ()) or item.reference.upper().startswith(("U", "IC"))

    def _signal_rules(self):
        source_by_net = self._source_by_net
        connectors = [item for item in self.snapshot.footprints if self._is_connector(item)]
        bounds = self.snapshot.bounds_mm
        reference_names = set(self.settings.reference_net_names)
        # A pair's return path is DP-004's to judge, both lines together and
        # with the coplanar ground. GP-001 measured one line of it again, a
        # level higher: P02 Rev E's USB_HOST_D+ drew a HIGH GP-001 at 78.7 %
        # coverage and the pair a MEDIUM DP-004 at 89.6 % for one 2.3 mm gap.
        pair_nets = {
            net for pair in self.differential_pairs if getattr(pair, "enabled", True)
            for net in (pair.positive_net, pair.negative_net)
        } if self._enabled("DIFFERENTIAL") else set()
        if self.snapshot.ignored_offboard_items:
            nets = list(self.snapshot.ignored_offboard_nets)
            counts = self.snapshot.ignored_offboard_counts
            detail = ", ".join(
                f"{count} {kind}" for kind, count in counts.items() if count
            ) or f"{self.snapshot.ignored_offboard_items} item(s)"
            routed = counts.get("tracks", 0) + counts.get("vias", 0)
            recommendation = (
                "Move or remove the off-board tracks/vias and rerun KiCad DRC."
                if routed else
                "Review these off-board footprints; no routed electrical net was excluded."
            )
            self._add(
                "GE-001", "GEOMETRY", "INFO", "Off-board geometry excluded",
                f"Excluded {detail} entirely outside Edge.Cuts across {len(nets)} net(s).",
                recommendation,
                "HIGH", nets=nets,
            )
        for net, tracks in self._tracks.items():
            if net in reference_names or net in self.snapshot.power_nets or not tracks:
                continue
            source = source_by_net.get(net)
            total_length = sum(item.length_mm for item in tracks)
            if (net in self._fast_nets and net not in pair_nets and net not in self._dc_feed_nets
                    and self._enabled("GROUND")
                    and LineString is not None and self._ground_zones):
                self._check()
                covered_length = 0.0
                worst_uncovered, worst_track = 0.0, None
                for track in tracks:
                    reference, _ = self._nearest_ground_layer(track.layer_id)
                    zone = self._reference_ground_zones.get(reference)
                    if zone is None or track.length_mm <= 1e-9:
                        intersection_length = 0.0
                    else:
                        line = LineString([track.start, track.end])
                        intersection_length = min(
                            track.length_mm, float(line.intersection(zone).length),
                        )
                    covered_length += intersection_length
                    uncovered = max(0.0, track.length_mm - intersection_length)
                    if uncovered > worst_uncovered:
                        worst_uncovered, worst_track = uncovered, track
                route_coverage = 100.0 * covered_length / max(total_length, 1e-9)
                uncovered_total = max(0.0, total_length - covered_length)
                # Ignore tiny pad escapes and polygon-boundary numerical slivers.
                meaningful_gap = max(1.0, 0.05 * total_length)
                if (worst_track is not None and route_coverage < 95.0
                        and uncovered_total >= meaningful_gap):
                    severity = "CRITICAL" if source and route_coverage < 50.0 else "HIGH"
                    x, y = self._track_midpoint(worst_track)
                    self._add("GP-001", "GROUND", severity, "Signal crosses a reference-plane void",
                              f"{net} has {route_coverage:.1f}% projected ground coverage over "
                              f"{total_length:.1f} mm ({uncovered_total:.1f} mm uncovered).",
                              "Reroute over continuous ground or restore the reference plane below the segment.",
                              "HIGH", [net], location=(x, y, worst_track.layer_id))
            if self._enabled("BOARD_EDGE"):
                self._check()
                edge_track, edge_distance = None, float("inf")
                for track in tracks:
                    if track.layer_id not in self._outer_layers:
                        continue
                    for point_value in (track.start, track.end):
                        distance = min(point_value[0] - bounds[0], bounds[2] - point_value[0],
                                       point_value[1] - bounds[1], bounds[3] - point_value[1])
                        if distance < edge_distance:
                            edge_track, edge_distance = track, distance
                _, height = self._nearest_ground_layer(edge_track.layer_id) if edge_track else (None, None)
                threshold = max(float(height or 0.2), 0.1)
                if edge_track and edge_distance < 0.0:
                    x, y = self._track_midpoint(edge_track)
                    self._add("BE-001", "BOARD_EDGE", "HIGH",
                              "Routed copper lies outside the board outline",
                              f"{net} extends {-edge_distance:.3f} mm beyond the Edge.Cuts envelope.",
                              "Move or remove the off-board route and rerun KiCad DRC before EMC analysis.",
                              "HIGH", [net], location=(x, y, edge_track.layer_id))
                elif edge_track and net in self._fast_nets and edge_distance < threshold:
                    x, y = self._track_midpoint(edge_track)
                    self._add("BE-001", "BOARD_EDGE", "HIGH" if source else "MEDIUM",
                              "Signal routed near board edge",
                              f"{net} approaches the board edge to {edge_distance:.3f} mm (reference height {threshold:.3f} mm).",
                              "Move the route inward or add a grounded edge guard with stitching vias.",
                              "MEDIUM", [net], location=(x, y, edge_track.layer_id))
            if source and source.kind == "CLOCK" and self._enabled("CLOCK"):
                self._check(3)
                outer_length = sum(item.length_mm for item in tracks if item.layer_id in self._outer_layers)
                representative = max(tracks, key=lambda item: item.length_mm)
                x, y = self._track_midpoint(representative)
                if len(self._copper_layers) > 2 and outer_length > 0.5 * total_length:
                    self._add("CK-001", "CLOCK", "MEDIUM", "Clock routed mostly on outer copper",
                              f"{net} has {100.0 * outer_length / max(total_length, 1e-9):.1f}% of its route on outer layers.",
                              "Prefer stripline routing between reference planes where practical.",
                              "HIGH", [net], location=(x, y, representative.layer_id))
                if total_length > 100.0:
                    self._add("CK-002", "CLOCK", "MEDIUM", "Long clock route",
                              f"{net} is approximately {total_length:.1f} mm long.",
                              "Shorten the route and keep it referenced to uninterrupted ground.",
                              "HIGH", [net], location=(x, y, representative.layer_id))
                if connectors:
                    nearest = min(math.dist((x, y), item.position) for item in connectors)
                    if nearest < 10.0:
                        self._add("CK-003", "CLOCK", "MEDIUM", "Clock close to connector",
                                  f"{net} passes about {nearest:.1f} mm from an external connector.",
                                  "Increase separation to reduce coupling onto attached cables.",
                                  "MEDIUM", [net], location=(x, y, representative.layer_id))

    def _return_path_rules(self):
        if not self._enabled("RETURN_PATH"):
            return
        ground_vias = [via for via in self.snapshot.vias
                       if via.net_name in self.settings.reference_net_names]
        grouped = defaultdict(list)
        routed_layers = {
            net: {track.layer_id for track in tracks}
            for net, tracks in self._tracks.items()
        }
        for via in self.snapshot.vias:
            if (via.net_name in self.settings.reference_net_names
                    or via.net_name in self.snapshot.power_nets
                    or via.net_name in self._dc_feed_nets
                    or via.net_name not in self._fast_nets):
                continue
            # A via is an EMC reference transition only when this routed net is
            # demonstrably present on at least two copper layers.
            if len(routed_layers.get(via.net_name, set())) < 2 or len(via.layer_ids) < 2:
                continue
            self._check()
            reference_distances = [self._nearest_ground_layer(layer)[1] for layer in via.layer_ids]
            height = max([item for item in reference_distances if item is not None] or [0.5])
            threshold = max(2.0 * height, 1.0)
            nearest = min((math.dist(via.position, item.position) for item in ground_vias), default=float("inf"))
            if nearest > threshold:
                grouped[via.net_name].append((via, nearest, threshold))
        for net, offenders in grouped.items():
            via, nearest, threshold = max(offenders, key=lambda item: item[1])
            severity = "HIGH" if net in {item.net_name for item in self.settings.sources} else "MEDIUM"
            self._add("RP-001", "RETURN_PATH", severity, "Layer transition lacks a nearby ground via",
                      f"{len(offenders)} transition(s) on {net} have no ground via within {threshold:.2f} mm; worst distance is {nearest:.2f} mm.",
                      "Place a ground stitching via beside each signal transition.",
                      "HIGH", [net], location=(*via.position, via.layer_ids[0] if via.layer_ids else None))

    def _component_rules(self):
        capacitors = [item for item in self.snapshot.footprints if self._is_capacitor(item)]
        protection_value = re.compile(
            r"(?:USBLC|PESD|TPD\d|ESD|TVS|TRANSIL|SMBJ|SMAJ|SMCJ|SRV05|PRTR|CMF|CMC|COMMON.?MODE|FERRITE)",
            re.I,
        )

        def is_filter(item):
            return bool(
                "protection" in (self._classes(item) or ())
                or re.match(r"^(FB|FL|TVS|CMC)", item.reference, re.I)
                or protection_value.search(item.value)
            )

        def is_esd_protector(item):
            # The schematic's symbol (KiCad's Power_Protection library or a
            # D_TVS) recognises the part first; the reference/value pattern
            # still covers parts drawn from private libraries (previous
            # audits' item B: TVS/ESD recognised by a regex on the value
            # alone).
            return bool(
                "protection" in (self._classes(item) or ())
                or re.match(r"^(TVS|ESD)", item.reference, re.I)
                or _ESD_PROTECTOR_VALUE_RE.search(item.value)
            )

        filters = [item for item in self.snapshot.footprints if is_filter(item)]
        if self._enabled("DECOUPLING"):
            for ic in [item for item in self.snapshot.footprints if self._needs_decoupling(item)]:
                power_management = bool(re.search(
                    r"(?:LTC|LT\d|TPS|LM\d|REGULATOR|BUCK|BOOST|POWER.?PATH|PMIC)",
                    ic.value, re.I,
                ))
                supply_nets = (
                    set(ic.nets) & set(self.snapshot.power_nets)
                ) - set(self.settings.reference_net_names)
                if not supply_nets:
                    # Without a known supply rail, proximity alone is not
                    # electrical evidence of a decoupling relationship.
                    continue
                self._check()
                candidates = []
                for cap in capacitors:
                    shared = supply_nets & set(cap.nets)
                    if not shared:
                        continue
                    distances = []
                    for rail in shared:
                        ic_points = [(x, y) for net, x, y in ic.net_positions if net == rail]
                        cap_points = [(x, y) for net, x, y in cap.net_positions if net == rail]
                        if ic_points and cap_points:
                            distances.extend(math.dist(a, b) for a in ic_points for b in cap_points)
                    distance = min(distances, default=math.dist(ic.position, cap.position))
                    candidates.append((distance, cap, sorted(shared)))
                nearest = min(candidates, key=lambda item: item[0], default=None)
                if nearest is None:
                    self._add("DE-002", "DECOUPLING", "HIGH", "No nearby decoupling capacitor",
                              f"No capacitor sharing a known supply rail with {ic.reference} was found.",
                              "Verify every supply pin and place its high-frequency capacitor at the pin/via pair.",
                              "LOW", components=[ic.reference], location=(*ic.position, None))
                elif nearest[0] > 5.0:
                    # An IC whose only signals are an I2C/SMBus port switches
                    # kHz-to-MHz open-drain edges from a few mA: a distant
                    # bypass capacitor costs it little. P02 Rev E's EEPROM
                    # U18 (C13 at 22.9 mm) ranked the campaign's first HIGH
                    # action on the same bar as a processor.
                    slow_bus = _drives_only_a_slow_bus(
                        ic, supply_nets | set(self.settings.reference_net_names),
                    )
                    severity = (
                        "MEDIUM" if power_management or slow_bus else
                        "HIGH" if nearest[0] > 8.0 else "MEDIUM"
                    )
                    title = (
                        "Distant rail-bypass candidate" if power_management else
                        "Distant decoupling capacitor"
                    )
                    recommendation = (
                        "Identify the relevant VIN/VOUT pin from the datasheet, then verify its "
                        "specified local bypass capacitor and pad-to-via loop."
                        if power_management else
                        "Move the high-frequency capacitor closer and minimise pad-to-via inductance."
                    )
                    self._add("DE-001", "DECOUPLING", severity, title,
                              f"The nearest capacitor sharing {', '.join(nearest[2])} with "
                              f"{ic.reference} is {nearest[1].reference} at {nearest[0]:.2f} mm "
                              f"(pad to pad). {THRESHOLD_BASIS['DE-001']}"
                              + (f" {ic.reference} only drives an I2C/SMBus port, whose supply "
                                 "current steps are small and slow, so this stays MEDIUM at any "
                                 "distance." if slow_bus and not power_management else ""),
                              recommendation,
                              "LOW" if power_management else "MEDIUM",
                              components=[ic.reference, nearest[1].reference], location=(*ic.position, None))
        if any(self._enabled(category) for category in ("IO", "ESD", "SHIELDING")):
            for connector in [item for item in self.snapshot.footprints if self._is_connector(item)]:
                self._check()
                likely_external = bool(re.search(r"USB|HDMI|RJ|ETH|CAN|EXT", connector.value, re.I))
                if likely_external and _HARNESS_CONNECTOR_RE.search(connector.value):
                    # P02 Rev E's J8, a Pico-Clasp header labelled "USB HOST",
                    # drew ES-001, IO-001 and SH-001 (two HIGH) for a cable to
                    # the main board, while the port the user plugs into, J2,
                    # has its ESD filter. One note says what was assumed.
                    if self._enabled("IO"):
                        self._add("IO-002", "IO", "LOW", "Interface on a cable connector",
                                  f"{connector.reference} ({connector.value}) carries an external-type "
                                  "interface on a wire-to-board connector, taken as internal.",
                                  "If its cable leaves the enclosure, protect the lines where it does, "
                                  "at the panel connector or next to this one.",
                                  "LOW", components=[connector.reference],
                                  location=(*connector.position, None))
                    continue
                connector_signals = (
                    set(connector.nets)
                    - set(self.settings.reference_net_names)
                    - set(self.snapshot.power_nets)
                )
                connected_filters = [
                    item for item in filters if connector_signals & set(item.nets)
                ]
                nearest = min(
                    (math.dist(connector.position, item.position) for item in connected_filters),
                    default=float("inf"),
                )
                if self._enabled("IO") and likely_external and nearest > 25.0:
                    self._add("IO-001", "IO", "HIGH", "External connector lacks nearby EMC protection",
                              f"No ferrite, common-mode choke, TVS or filter was detected within 25 mm of {connector.reference}.",
                              "Place ESD protection and any interface filtering next to the connector entry point.",
                              "LOW", components=[connector.reference], location=(*connector.position, None))
                if self._enabled("ESD") and likely_external:
                    protectors = [item for item in connected_filters if is_esd_protector(item)]
                    def pin_distance(item):
                        # Pad to pad on the lines the part protects, not
                        # footprint centre to centre: P02 Rev E's J1 -> U2
                        # read 17.3 mm centre to centre, 15.5 mm pin to pin
                        # (audit-2026-09-23.md EMC-7).
                        shared = connector_signals & set(item.nets)
                        ours = [(x, y) for net, x, y in connector.net_positions if net in shared]
                        theirs = [(x, y) for net, x, y in item.net_positions if net in shared]
                        if ours and theirs:
                            return min(math.dist(a, b) for a in ours for b in theirs)
                        return math.dist(connector.position, item.position)

                    protector = min(protectors, key=pin_distance, default=None)
                    protector_distance = pin_distance(protector) if protector else float("inf")
                    if protector is None:
                        self._add("ES-001", "ESD", "HIGH", "ESD path is missing or too long",
                                  f"No TVS/diode protection was detected on the lines of {connector.reference}.",
                                  "Place the TVS at the connector and route the discharge path directly to chassis/ground.",
                                  "LOW", components=[connector.reference], location=(*connector.position, None))
                    elif protector_distance > 10.0:
                        # Protected, but far: P02 Rev E's J1 has its USB lines
                        # through U2, an ECMF02 filter with ESD, 17 mm away.
                        self._add("ES-001", "ESD", "MEDIUM", "ESD path is missing or too long",
                                  f"{protector.reference} ({protector.value}) protects lines of "
                                  f"{connector.reference} but sits {protector_distance:.1f} mm from it, "
                                  f"pin to pin. {THRESHOLD_BASIS['ES-001']}",
                                  "Move the protection next to the connector, or keep the line to it short "
                                  "and clear of other nets.",
                                  "MEDIUM", components=[connector.reference, protector.reference],
                                  location=(*connector.position, None))
                    else:
                        nearby_ground_vias = sum(
                            math.dist(protector.position, via.position) <= 3.0
                            for via in self.snapshot.vias
                            if via.net_name in self.settings.reference_net_names
                        )
                        if nearby_ground_vias < 2:
                            self._add("ES-002", "ESD", "MEDIUM", "TVS has insufficient nearby ground vias",
                                      f"Only {nearby_ground_vias} ground via(s) were found within 3 mm of {protector.reference}.",
                                      "Use a short, wide discharge path and at least two nearby ground/chassis vias.",
                                      "MEDIUM", components=[connector.reference, protector.reference],
                                      location=(*protector.position, None))
                if self._enabled("SHIELDING") and likely_external:
                    shield_nets = [net for net in connector.nets if re.search(r"SHIELD|CHASSIS|EARTH", net, re.I)]
                    if not shield_nets and re.search(r"USB|HDMI|RJ|ETH", connector.value, re.I):
                        self._add("SH-001", "SHIELDING", "MEDIUM", "Connector shield return not identified",
                                  f"{connector.reference} exposes an external high-speed interface but no SHIELD/CHASSIS net was detected.",
                                  "Verify the connector shell connection and provide a short high-frequency chassis return.",
                                  "LOW", components=[connector.reference], location=(*connector.position, None))

    def _stitching_rules(self):
        if not self._enabled("STITCHING"):
            return
        self._check()
        ground_vias = [via for via in self.snapshot.vias
                       if via.net_name in self.settings.reference_net_names]
        if len(ground_vias) < 2 or self.settings.frequency_stop_hz <= 0:
            return
        epsilon = max((layer.epsilon_r for layer in self.snapshot.stackup.layers
                       if layer.kind.upper() == "DIELECTRIC"), default=4.4)
        required = 299792458.0 / (math.sqrt(epsilon) * self.settings.frequency_stop_hz) * 1000.0 / 20.0
        nearest = []
        for index, via in enumerate(ground_vias):
            nearest.append(min(math.dist(via.position, other.position)
                               for other_index, other in enumerate(ground_vias) if other_index != index))
        average = sum(nearest) / len(nearest)
        if average > 2.0 * required:
            worst = ground_vias[nearest.index(max(nearest))]
            self._add("VS-001", "STITCHING", "MEDIUM", "Ground-via stitching is sparse",
                      f"Average nearest ground-via spacing is {average:.2f} mm; lambda/20 at the configured upper frequency is {required:.2f} mm.",
                      "Add ground stitching around board edges, connectors and reference transitions.",
                      "MEDIUM", location=(*worst.position, None))

    def _copper_area_mm2(self, net_name, tracks):
        """Copper area of a net: the union of its tracks and pours per layer.

        Summing track length x width and pour areas counted a track lying on
        its own pour twice: P02 Rev E's /J2_SRC_SW read 41.8 mm2 for 31.8 in
        union (+31 %), /SYS_5V_SW1 112.9 for 108.8 (audit EMC-7).
        """
        if LineString is None or unary_union is None:
            area = sum(track.length_mm * track.width_mm for track in tracks)
            for geometry in self.snapshot.zones_by_net.get(net_name, {}).values():
                area += float(geometry.area)
            return area
        by_layer = defaultdict(list)
        for track in tracks:
            if track.width_mm > 0.0 and track.start != track.end:
                by_layer[track.layer_id].append(
                    LineString([track.start, track.end]).buffer(track.width_mm / 2.0)
                )
        for layer_id, geometry in self.snapshot.zones_by_net.get(net_name, {}).items():
            if geometry is not None and not geometry.is_empty:
                by_layer[int(layer_id)].append(geometry)
        return float(sum(unary_union(parts).area for parts in by_layer.values()))

    def _hot_loop_components(self, source):
        return hot_loop_components(self.snapshot, source.name, source.net_name)

    def _switching_rules(self):
        if not self._enabled("SWITCHING"):
            return
        for source in [item for item in self.settings.sources if item.enabled and item.kind == "SWITCHING"]:
            tracks = self._tracks.get(source.net_name, [])
            hot_loop = self._hot_loop_components(source)
            source_location = None
            if tracks:
                representative = max(tracks, key=lambda item: item.length_mm * item.width_mm)
                source_location = (*self._track_midpoint(representative), representative.layer_id)
            else:
                geometries = [
                    (layer_id, geometry)
                    for layer_id, geometry in self.snapshot.zones_by_net.get(source.net_name, {}).items()
                    if geometry is not None and not geometry.is_empty
                ]
                if geometries:
                    layer_id, geometry = max(geometries, key=lambda item: float(item[1].area))
                    point = geometry.representative_point()
                    source_location = (float(point.x), float(point.y), int(layer_id))
                else:
                    pads = [
                        (float(x), float(y))
                        for footprint in self.snapshot.footprints
                        for net, x, y in footprint.net_positions if net == source.net_name
                    ]
                    if pads:
                        source_location = (*pads[0], None)
            self._check(2)
            area = self._copper_area_mm2(source.net_name, tracks)
            if area > 25.0:
                severity = "HIGH" if area > 100.0 else "MEDIUM"
                # An IC with 2+ other components on its own switch node is an
                # external-FET controller (P02 Rev E: U4 drives Q10/Q11 to
                # L1 on /SYS_5V_SW1) -- "IC-to-inductor" describes an
                # integrated switcher and is the wrong recommendation there.
                non_inductor_refs = len(hot_loop) - sum(
                    1 for model in getattr(self.snapshot, "inductors", []) or []
                    if (model.source_name == source.name or model.switching_net == source.net_name)
                    and model.ref_des in hot_loop
                )
                recommendation = (
                    "Restrict the FET-to-FET (or FET-to-diode) and FET-to-inductor copper to the "
                    "shortest path, and keep the input capacitor's return current on the same loop."
                    if non_inductor_refs >= 2 else
                    "Restrict SW/PH/LX copper to the shortest IC-to-inductor connection."
                )
                self._add("SW-002", "SWITCHING", severity, "Large switching-node copper area",
                          f"{source.net_name} occupies approximately {area:.1f} mm2 of copper. "
                          f"{THRESHOLD_BASIS['SW-002']}",
                          recommendation,
                          "MEDIUM", [source.net_name], location=source_location)
            # The loop the chopped current actually runs: input capacitor, high
            # side, low side. Every part on the switch node used to count, so
            # P02 Rev E's /SYS_5V_SW1 finding listed U4, L1, the bootstrap
            # capacitor C43 and R57, and none of the input capacitors -- while
            # C37's ground pad sits 18.2 mm from Q11's.
            loop = buck_hot_loop(
                self.snapshot, source.net_name, self.settings.reference_net_names,
                self.snapshot.power_nets, self._is_capacitor,
            )
            if loop is not None:
                self._check()
                span_mm = loop["span_mm"]
                severity = "HIGH" if span_mm > 20.0 else "MEDIUM" if span_mm > 10.0 else None
                if severity is not None:
                    parts = [loop["capacitor"], loop["high"]] + (
                        [loop["low"]] if loop["low"] != loop["high"] else []
                    )
                    footprint_positions = {
                        item.reference: item.position for item in self.snapshot.footprints
                    }
                    points = [footprint_positions[ref] for ref in parts if ref in footprint_positions]
                    midpoint = (
                        sum(point[0] for point in points) / len(points),
                        sum(point[1] for point in points) / len(points),
                    ) if points else None
                    detail = (
                        f"{loop['capacitor']}'s supply pad is {loop['vin_mm']:.1f} mm from "
                        f"{loop['high']}'s and its ground pad {loop['ground_mm']:.1f} mm from "
                        f"{loop['low']}'s"
                        + (f"; {loop['high']} and {loop['low']} are {loop['switch_mm']:.1f} mm apart"
                           if loop["low"] != loop["high"] else "")
                    )
                    self._add(
                        "SW-003", "SWITCHING", severity, "Switching hot loop is spread out",
                        f"On {source.net_name}'s hot loop, {' -> '.join(parts)}: {detail}. "
                        f"{THRESHOLD_BASIS.get('SW-003', '')}".strip(),
                        "Move the input capacitor, high-side and low-side switches as close "
                        "together as the footprints allow, the capacitor's ground pad next to the "
                        "low side's, to minimise loop area and di/dt radiation.",
                        "MEDIUM", [source.net_name], components=parts,
                        location=(*midpoint, None) if midpoint else None,
                    )
            elif len(hot_loop) >= 2:
                refs = sorted(hot_loop)
                far_a, far_b, span_mm = max(
                    ((a, b, math.dist(hot_loop[a], hot_loop[b]))
                     for i, a in enumerate(refs) for b in refs[i + 1:]),
                    key=lambda item: item[2],
                )
                self._check()
                # TI SLVA959 ("Optimizing the Layout of a Synchronous Buck
                # Converter") and ADI AN-136 both call for the high-side
                # FET/diode, low-side FET and input capacitor to sit as
                # close together as the footprints allow, citing millimetre-
                # scale spacing at these switching currents and frequencies;
                # 10 mm/20 mm are this tool's own read of that guidance, not
                # a number either app note states outright. P02 Rev E: Q10
                # and Q11 (U4's high-/low-side FETs) sit 20.1 mm apart with
                # nothing flagging it before this rule.
                if span_mm > 20.0:
                    severity = "HIGH"
                elif span_mm > 10.0:
                    severity = "MEDIUM"
                else:
                    severity = None
                if severity is not None:
                    midpoint = ((hot_loop[far_a][0] + hot_loop[far_b][0]) / 2.0,
                                (hot_loop[far_a][1] + hot_loop[far_b][1]) / 2.0)
                    self._add(
                        "SW-003", "SWITCHING", severity, "Switching hot loop is spread out",
                        f"{far_a} and {far_b}, on {source.net_name}'s hot loop, are {span_mm:.1f} mm apart.",
                        "Move the high-side FET (or diode), low-side FET/rectifier, input "
                        "capacitor and inductor as close together as the footprints allow to "
                        "minimise loop area and di/dt radiation.",
                        "MEDIUM", [source.net_name], components=list(hot_loop),
                        location=(*midpoint, None),
                    )
            if source.frequency_hz > 0:
                first = max(1, int(math.ceil(self.settings.frequency_start_hz / source.frequency_hz)))
                harmonic = first * source.frequency_hz
                if harmonic <= self.settings.frequency_stop_hz:
                    rise_time_s = max(float(source.rise_time_ns), 1e-6) * 1e-9
                    # Paul's trapezoid bound (EMC-3): shared with EE-002 and
                    # the near-field solver so the same source no longer
                    # gets three disagreeing envelopes (P02 Rev E audit,
                    # 2026-09-23: this used to knee at 1/(2*pi*tr), -6.6 dB
                    # for U4's 75th harmonic where Paul's bound gives -37.5).
                    relative_db = trapezoid_envelope_db(first, source.frequency_hz, rise_time_s)
                    relative = 10.0 ** (relative_db / 20.0)
                    drive_index = abs(float(source.voltage_swing_v) * float(source.current_a)) * relative
                    if first <= 10 or relative_db > -20.0:
                        severity = "HIGH"
                    elif first <= 30 or drive_index >= 0.25:
                        severity = "MEDIUM"
                    else:
                        severity = "LOW"
                    self._add("SW-001", "SWITCHING", severity,
                              "Switching harmonics enter the emissions band",
                              f"Harmonic {first} of {source.name} is {harmonic / 1e6:.3f} MHz; "
                              f"the configured edge model estimates {relative_db:.1f} dB relative "
                              f"to its low-frequency square-wave envelope (drive index {drive_index:.3g} V*A). "
                              f"{THRESHOLD_BASIS['SW-001']} Source parameters: {source.parameter_notes}",
                              "Minimise hot-loop area and verify this band with a near-field probe.",
                              # A stated frequency places the harmonic; its level
                              # is still an uncalibrated edge model (+/-10-20 dB),
                              # however the source was entered or simulated.
                              "MEDIUM" if source.source in ("manual", "power-tree", "board-circuit") else "LOW",
                              [source.net_name], location=source_location)

    def _inductor_rules(self):
        """Qualify magnetic models without inventing shielding performance."""
        if not self._enabled("SWITCHING"):
            return
        footprints = {item.reference.upper(): item for item in self.snapshot.footprints}
        for model in getattr(self.snapshot, "inductors", []) or []:
            self._check(2)
            footprint = footprints.get(model.ref_des.upper())
            location = (*footprint.position, None) if footprint else None
            peak_current = model.output_current_a + 0.5 * model.ripple_current_pp_a
            if model.isat_a > 0.0 and peak_current > model.isat_a:
                ratio = peak_current / model.isat_a
                self._add(
                    "IN-004", "SWITCHING", "HIGH" if ratio > 1.1 else "MEDIUM",
                    "Inductor saturation-current margin exceeded",
                    f"{model.ref_des} reaches an estimated {peak_current:.3g} A peak versus "
                    f"{model.isat_a:.3g} A typical Isat.",
                    "Increase inductance/current rating or validate the worst-case ripple and bias.",
                    model.parameter_confidence, components=[model.ref_des], location=location,
                )
            if (str(model.shield_state).upper() == "SHIELDED"
                    and model.shielding_attenuation_db is None):
                self._add(
                    "IN-001", "SWITCHING", "INFO",
                    "Inductor shielding is identified but not numerically calibrated",
                    f"{model.ref_des} ({model.mpn or 'MPN unknown'}) is magnetically shielded; "
                    "no manufacturer stray-field/attenuation curve is available, so no numerical "
                    "shield reduction is applied.",
                    "Keep the unattenuated geometric estimate as an uncertainty bound and validate "
                    "locally with an H-field probe.",
                    "HIGH" if model.parameter_source == "datasheet" else "MEDIUM",
                    components=[model.ref_des], location=location,
                )

    def _differential_rules(self):
        if not self._enabled("DIFFERENTIAL"):
            return
        positions = self._layer_positions()
        for pair in self.differential_pairs:
            if not getattr(pair, "enabled", True):
                continue
            self._check(3)
            positive, negative = self._tracks.get(pair.positive_net, []), self._tracks.get(pair.negative_net, [])
            len_p = sum(item.length_mm for item in positive)
            len_n = sum(item.length_mm for item in negative)
            epsilon = max((layer.epsilon_r for layer in self.snapshot.stackup.layers
                           if layer.kind.upper() == "DIELECTRIC"), default=4.4)
            skew_ps = abs(len_p - len_n) * math.sqrt(epsilon) / 0.299792458
            interface = pair.interface.upper()
            limit = protocol_skew_limit_ps(interface)
            representative = (positive or negative or [None])[0]
            location = (*self._track_midpoint(representative), representative.layer_id) if representative else None
            if skew_ps > 0.5 * limit:
                self._add("DP-001", "DIFFERENTIAL", "HIGH" if skew_ps > limit else "MEDIUM",
                          "Differential-pair skew", f"{pair.name} has about {skew_ps:.1f} ps skew (limit {limit:g} ps).",
                          "Length-match the pair while preserving constant spacing and reference.",
                          "HIGH", [pair.positive_net, pair.negative_net], location=location)
            layers = {item.layer_id for item in positive + negative}
            if len(layers) > 1:
                self._add("DP-003", "DIFFERENTIAL", "HIGH", "Differential pair changes reference layer",
                          f"{pair.name} uses {len(layers)} copper layers.",
                          "Add symmetric transitions and adjacent ground vias at every layer change.",
                          "HIGH", [pair.positive_net, pair.negative_net], location=location)
            pair_tracks = positive + negative
            outer = sum(item.length_mm for item in pair_tracks if item.layer_id in self._outer_layers)
            total = len_p + len_n
            if len(self._copper_layers) > 2 and outer > 0.5 * total and total > 0.0:
                adjacent_coverage, coplanar_coverage, coverage, uncovered = (
                    self._pair_reference_coverage(pair, pair_tracks)
                )
                if coverage < 95.0:
                    worst = uncovered[0] if uncovered else None
                    finding_location = (
                        (*worst["position"], worst["layer_id"])
                        if worst is not None else location
                    )
                    longest_gap = (
                        f" Longest uncovered interval is about {worst['length_mm']:.2f} mm "
                        f"on {worst['net_name']} near "
                        f"({worst['position'][0]:.2f}, {worst['position'][1]:.2f}) mm."
                        if worst is not None else ""
                    )
                    worst_gap = float(worst["length_mm"]) if worst is not None else 0.0
                    # A sub-millimetre polygon/pad escape is not equivalent to a long
                    # plane split. Retain it as a local, actionable warning without
                    # allowing percentage coverage on a short route to inflate severity.
                    if coverage < 50.0 or worst_gap >= 5.0:
                        severity = "HIGH"
                    elif coverage < 70.0 or worst_gap >= 1.0:
                        severity = "MEDIUM"
                    else:
                        severity = "LOW"
                    self._add("DP-004", "DIFFERENTIAL", severity,
                              "Localized differential return-reference interruptions",
                              f"{pair.name} is {100.0 * outer / total:.1f}% on outer layers "
                              f"with {coverage:.1f}% combined return coverage "
                              f"(adjacent plane {adjacent_coverage:.1f}%, "
                              f"coplanar GND {coplanar_coverage:.1f}%).{longest_gap}",
                              "Inspect and stitch/restore GND only at the reported coordinates; "
                              "do not reroute an otherwise referenced pair solely from percentage coverage.",
                              "HIGH", [pair.positive_net, pair.negative_net],
                              location=finding_location)
                    finding = self.result.findings[-1]
                    for interval in uncovered[:3]:
                        finding.evidence.append(EMCEvidence(
                            "PCB_GEOMETRY",
                            f"Uncovered return interval about {interval['length_mm']:.2f} mm "
                            f"on {interval['net_name']}.",
                            interval["position"][0], interval["position"][1],
                            interval["layer_id"],
                        ))

    def _crosstalk_rules(self):
        if not self._enabled("CROSSTALK") or LineString is None:
            return
        aggressors = {item.net_name for item in self.settings.sources if item.enabled}
        cell_size = 10.0
        spatial = defaultdict(list)
        for tracks in self._tracks.values():
            for track in tracks:
                min_x, max_x = sorted((track.start[0], track.end[0]))
                min_y, max_y = sorted((track.start[1], track.end[1]))
                for cell_x in range(math.floor(min_x / cell_size), math.floor(max_x / cell_size) + 1):
                    for cell_y in range(math.floor(min_y / cell_size), math.floor(max_y / cell_size) + 1):
                        spatial[(track.layer_id, cell_x, cell_y)].append(track)
        reported = set()
        for aggressor in aggressors:
            for first in self._tracks.get(aggressor, []):
                _, height = self._nearest_ground_layer(first.layer_id)
                threshold = 3.0 * max(float(height or 0.2), 0.1)
                line_a = LineString([first.start, first.end])
                min_x = min(first.start[0], first.end[0]) - threshold
                max_x = max(first.start[0], first.end[0]) + threshold
                min_y = min(first.start[1], first.end[1]) - threshold
                max_y = max(first.start[1], first.end[1]) + threshold
                candidates = {}
                for cell_x in range(math.floor(min_x / cell_size), math.floor(max_x / cell_size) + 1):
                    for cell_y in range(math.floor(min_y / cell_size), math.floor(max_y / cell_size) + 1):
                        for track in spatial.get((first.layer_id, cell_x, cell_y), []):
                            candidates[id(track)] = track
                for second in candidates.values():
                    victim = second.net_name
                    if victim == aggressor or victim in self.settings.reference_net_names:
                        continue
                    key = tuple(sorted((aggressor, victim)))
                    if key in reported:
                        continue
                    self._check()
                    dx1, dy1 = first.end[0] - first.start[0], first.end[1] - first.start[1]
                    dx2, dy2 = second.end[0] - second.start[0], second.end[1] - second.start[1]
                    denom = max(first.length_mm * second.length_mm, 1e-12)
                    parallel = abs((dx1 * dx2 + dy1 * dy2) / denom) > 0.94
                    distance = line_a.distance(LineString([second.start, second.end]))
                    if parallel and min(first.length_mm, second.length_mm) >= 5.0 and distance < threshold:
                        x, y = self._track_midpoint(first)
                        self._report_crosstalk(
                            aggressor, victim, first, second, distance, threshold,
                            (x, y, first.layer_id),
                        )
                        reported.add(key)

    def _report_crosstalk(self, aggressor, victim, first, second, distance, threshold, location):
        """Emit XT-001, quantifying the coupling when the cross-section allows.

        The rule used to compare a centreline distance with a 3H threshold and
        stop there, which says the traces are close but not how much they
        couple.  ``crosstalk_2d`` turns the same geometry into an even/odd mode
        solve and a near-end coupling coefficient, so the finding can carry a
        percentage of the aggressor swing instead of a spacing.

        When the cross-section cannot be solved the geometric rule still fires,
        because a proximity warning is better than silence -- but it is
        labelled HEURISTIC and says which path produced it, so a reader never
        mistakes a spacing check for a coupling calculation.
        """
        sensitive = bool(SENSITIVE_VICTIM_PATTERN.search(victim))
        width_mm = 0.5 * (float(first.width_mm) + float(second.width_mm))
        # LineString.distance measures centreline to centreline; the solver
        # wants the copper-edge gap.
        gap_mm = distance - width_mm
        coupled_length_mm = min(float(first.length_mm), float(second.length_mm))
        modes, topology, reason = self._coupled_modes(first, gap_mm, width_mm)

        if modes is None:
            severity = "HIGH" if sensitive else "MEDIUM"
            self._add(
                "XT-001", "CROSSTALK", severity, "Long parallel trace coupling",
                f"{aggressor} and {victim} run in parallel within {distance:.3f} mm; "
                f"3H target is {threshold:.3f} mm. Coupling was not quantified because "
                f"{reason}; this is a spacing check, not a coupling calculation.",
                "Increase spacing to at least 3H, change layer, or insert a grounded guard.",
                "LOW", [aggressor, victim], location=location,
            )
            return

        kb_sat = near_end_crosstalk_coefficient(modes)
        source = self._source_by_net.get(aggressor)
        rise_time_s = max(float(getattr(source, "rise_time_ns", 0.0) or 0.0), 0.0) * 1e-9
        coupled_length_m = coupled_length_mm / 1000.0

        # Kb_sat is the saturated worst case and needs no rise time, so it is
        # always reported.  The length-corrected NEXT additionally needs the
        # aggressor edge, whose provenance is stated rather than assumed.
        detail = (
            f"Saturated near-end coupling Kb={abs(kb_sat) * 100:.2f} % of the aggressor "
            f"swing, from a {topology} even/odd mode solve "
            f"(Z0e={modes.even_mode_impedance_ohm:.1f} ohm, "
            f"Z0o={modes.odd_mode_impedance_ohm:.1f} ohm) over {coupled_length_mm:.2f} mm "
            f"at a {gap_mm:.3f} mm edge gap."
        )
        next_ratio = abs(kb_sat)
        if rise_time_s > 0.0:
            next_ratio = abs(near_end_crosstalk_ratio(modes, coupled_length_m, rise_time_s))
            fext_ratio = far_end_crosstalk_ratio(modes, coupled_length_m, rise_time_s)
            provenance = (
                "measured from the configured source"
                if str(getattr(source, "source", "auto")).lower() != "auto"
                else "an engineering default, not a datasheet value"
            )
            detail += (
                f" With a {rise_time_s * 1e9:.2f} ns aggressor edge ({provenance}), "
                f"NEXT={next_ratio * 100:.2f} % and FEXT={fext_ratio * 100:+.2f} % "
                f"of the swing; saturation length is "
                f"{saturation_length_m(modes, rise_time_s) * 1000.0:.2f} mm."
            )
        else:
            detail += " No aggressor rise time is configured, so only the saturated worst case is given."

        limit_high = NEXT_RATIO_SENSITIVE if sensitive else NEXT_RATIO_HIGH
        limit_medium = NEXT_RATIO_SENSITIVE / 2.0 if sensitive else NEXT_RATIO_MEDIUM
        if next_ratio >= limit_high:
            severity = "HIGH"
        elif next_ratio >= limit_medium:
            severity = "MEDIUM"
        else:
            severity = "LOW"

        self._add(
            "XT-001", "CROSSTALK", severity, "Quantified parallel trace coupling",
            f"{aggressor} couples {next_ratio * 100:.2f} % into {victim}. {detail} "
            f"Escalation budget: {limit_high * 100:g} % HIGH / {limit_medium * 100:g} % MEDIUM"
            f"{' (sensitive victim)' if sensitive else ''}.",
            "Increase spacing, change layer, or insert a grounded guard trace; "
            "the coupling falls roughly with the square of the gap-to-height ratio.",
            "MEDIUM", [aggressor, victim], location=location, detail=detail,
        )

    def _pdn_rules(self):
        if not self._enabled("PDN"):
            return
        for name, result in self.ac_results:
            self._check()
            target = float(getattr(result, "target_impedance_ohm", 0.0) or 0.0)
            worst = float(getattr(result, "worst_impedance_ohm", 0.0) or 0.0)
            frequency = float(getattr(result, "worst_frequency_hz", 0.0) or 0.0)
            if target <= 0:
                continue
            # Stated at the point of least margin, against what is allowed
            # there (ac_model.target_mask). P02 Rev E, 2026-09-30 00:09,
            # read "/VBUS_SYS: worst impedance 0.2061 ohm is within the
            # 0.08889 ohm target": 0.392 ohm is allowed at its 38.3 kHz.
            allowed = float(getattr(result, "critical_allowed_ohm", 0.0) or 0.0)
            if allowed > 0.0:
                worst = float(result.critical_impedance_ohm)
                frequency = float(result.critical_frequency_hz)
            else:
                allowed = target
            if not bool(getattr(result, "meets_target", worst <= target)):
                # INFO, not HIGH: the AC domain already judges the sweep this
                # restates (AC-001), and HIGH counted it twice -- P02 Rev E's
                # three failing rails, 2026-09-29 16:26, took the EMC score
                # from 49 to 25 and made three top actions of AC-001's copy.
                # TE-001 was demoted for the thermal hotspot the same way.
                self._add("PD-001", "PDN", "INFO", "PDN impedance exceeds target",
                          f"{name}: {worst:.4g} ohm at {frequency / 1e6:.3f} MHz exceeds the "
                          f"{allowed:.4g} ohm allowed there.",
                          "Optimise capacitor values/placement and rerun the AC impedance analysis.",
                          "HIGH", [name])
            else:
                self._add("PD-002", "PDN", "INFO", "PDN remains within target",
                          f"{name}: {worst:.4g} ohm at {frequency / 1e6:.3f} MHz, its least margin, "
                          f"is within the {allowed:.4g} ohm allowed there.",
                          "Retain the analysed capacitor population and verify on the assembled board.",
                          "HIGH", [name])

    def _emission_rules(self):
        if not self._enabled("EMISSIONS"):
            return
        self._check(2)
        bounds = self.snapshot.bounds_mm
        length = max((bounds[2] - bounds[0]) / 1000.0, 1e-6)
        width = max((bounds[3] - bounds[1]) / 1000.0, 1e-6)
        epsilon = max((layer.epsilon_r for layer in self.snapshot.stackup.layers
                       if layer.kind.upper() == "DIELECTRIC"), default=4.4)
        c = 299792458.0
        resonances = []
        for m, n in ((1, 0), (0, 1), (1, 1), (2, 0), (0, 2)):
            frequency = c / (2.0 * math.sqrt(epsilon)) * math.sqrt((m / length) ** 2 + (n / width) ** 2)
            if self.settings.frequency_start_hz <= frequency <= 2.0 * self.settings.frequency_stop_hz:
                resonances.append(frequency)
        self.result.cavity_resonances_hz = sorted(resonances)
        if resonances:
            self._add("EE-001", "EMISSIONS", "INFO", "Board cavity resonances estimated",
                      "First rectangular-board modes: " + ", ".join(f"{item / 1e6:.1f} MHz" for item in sorted(resonances)[:3]) + ".",
                      "Use these frequencies to prioritise near-field and chamber measurements.", "MEDIUM")
        for source in [item for item in self.settings.sources if item.enabled and item.frequency_hz > 0]:
            rise_s = max(source.rise_time_ns * 1e-9, 1e-12)
            stop = min(self.settings.frequency_stop_hz, 0.5 / rise_s)
            harmonic = max(1, int(math.ceil(self.settings.frequency_start_hz / source.frequency_hz)))
            while harmonic * source.frequency_hz <= stop and len(self.result.frequency_risks) < 500:
                frequency = harmonic * source.frequency_hz
                # Paul's trapezoid bound (EMC-3), shared with SW-001 and the
                # near-field solver.
                level = trapezoid_envelope_db(harmonic, source.frequency_hz, rise_s)
                self.result.frequency_risks.append(EMCFrequencyRisk(frequency, level, source.name))
                harmonic += 1
        if self.result.frequency_risks:
            self._add("EE-002", "EMISSIONS", "INFO", "Source harmonic envelopes estimated",
                      f"Generated {len(self.result.frequency_risks)} relative harmonic markers for lab prioritisation.",
                      "Treat amplitudes as relative ranking only; measure absolute field strength.",
                      "HIGH" if all(item.source == "manual" for item in self.settings.sources if item.enabled) else "LOW")
        unknown = [item for item in self.settings.sources if item.enabled and item.frequency_hz <= 0.0]
        if unknown:
            # What an unknown frequency costs, said once, instead of harmonics
            # computed on a frequency the project never stated.
            names = ", ".join(item.name for item in unknown[:10]) + (", ..." if len(unknown) > 10 else "")
            self._add("EE-003", "EMISSIONS", "INFO", "Source frequency unknown",
                      f"{len(unknown)} source(s) have no frequency stated by the project or entered: {names}. "
                      "Their harmonic markers, switching-band check (SW-001) and near-field "
                      "contributions are not computed.",
                      "Enter each source's frequency, or state it on the board: a crystal or "
                      "oscillator value, or a Frequency/Fsw field on the part.",
                      "HIGH", sorted({item.net_name for item in unknown}))

    def _power_tree_rules(self):
        """SW-004: an entered switching source or inductor model that
        disagrees with what the power tree says its converter delivers.

        P02 Rev E's U4 SW1 source and L1's model were entered at 4.36 A; the
        tree's loads give U4 4.726 A, which the AC target already used.
        """
        outputs = dict(getattr(self.snapshot, "converter_outputs", None) or {})
        if not outputs or not self._enabled("SWITCHING"):
            return
        entered = defaultdict(list)
        for source in self.settings.sources:
            if not source.enabled or str(source.kind).upper() != "SWITCHING":
                continue
            if source.source == "board-circuit":
                # Revalued from its simulated board circuit, which already
                # draws the tree's current: the inductor peak it carries is
                # not an output current. On P02 Rev E's 16:51 run U14's 3.87 A
                # and U5's 1.26 A peaks were reported against 3 A and 1 A.
                continue
            output = converter_output_for(source.name, outputs)
            if output is not None:
                self._check()
                entered[output].append((source.name, float(source.current_a or 0.0),
                                        float(source.frequency_hz or 0.0), source.net_name, ""))
        for model in getattr(self.snapshot, "inductors", ()) or ():
            output = (converter_output_for(getattr(model, "source_name", ""), outputs)
                      or outputs.get(str(model.ref_des).upper()))
            if output is not None:
                self._check()
                entered[output].append((f"{model.ref_des}'s magnetic model",
                                        float(model.output_current_a or 0.0),
                                        float(model.switching_frequency_hz or 0.0),
                                        model.switching_net, model.ref_des))
        for output, items in entered.items():
            strays, stray_items = [], []
            for item in items:
                label, current, frequency, _net, _ref = item
                parts = []
                if (current > 0.0 and output.current_a > 0.0
                        and abs(current - output.current_a) > CONVERTER_CURRENT_TOLERANCE * output.current_a):
                    parts.append(f"{current:.3g} A")
                if (frequency > 0.0 and output.frequency_hz > 0.0 and abs(frequency - output.frequency_hz)
                        > CONVERTER_FREQUENCY_TOLERANCE * output.frequency_hz):
                    parts.append(f"{frequency / 1e3:.0f} kHz")
                if parts:
                    strays.append(f"{label} at {' and '.join(parts)}")
                    stray_items.append(item)
            if not strays:
                continue
            tree = f"{output.current_a:.3g} A"
            if output.frequency_hz > 0.0:
                tree += f" at {output.frequency_hz / 1e3:.0f} kHz"
            reference = output.name.split(" ", 1)[0]
            self._add("SW-004", "SWITCHING", "LOW", "Entered converter values differ from the power tree",
                      f"{'; '.join(strays)} entered; the power tree gives {output.name} {tree} of "
                      f"output on {output.output_rail}. The spectrum and the inductor fields use the "
                      "entered values; Phase 10's board circuit draws the tree's current.",
                      "Enter the power tree's values on the EMI/EMC page (Edit Selected for a "
                      "source, Edit Selected Model for an inductor), or correct the tree's loads if "
                      "the entered ones are right.",
                      "HIGH",
                      nets=sorted({net for *_rest, net, _ref in stray_items if net}),
                      components=[reference] + sorted({ref for *_rest, ref in stray_items if ref}))

    def _thermal_rules(self):
        if not self._enabled("THERMAL") or self.thermal_result is None:
            return
        hotspot = getattr(self.thermal_result, "hotspot", None)
        at_hotspot = (hotspot.x_mm, hotspot.y_mm, None) if hotspot is not None else None
        temperatures = dict(getattr(self.thermal_result, "placement_temperatures_c", None) or {})
        if not temperatures:
            # A thermal result without the board temperature under each
            # footprint: only its hotspot can be said.
            if hotspot is not None and hotspot.temperature_c > HOT_PASSIVE_C:
                self._check()
                # INFO, not MEDIUM: the thermal domain already judges the
                # hotspot, and taking 4 points off the EMC score for it counted
                # it twice (P02 Rev E, 114.7 C; audit-2026-09-23.md EMC-9).
                self._add("TE-001", "THERMAL", "INFO", "High temperature may alter EMC components",
                          f"The latest thermal hotspot is {hotspot.temperature_c:.1f} C.",
                          "Verify MLCC DC-bias/SRF and ferrite impedance at the local operating "
                          "temperature.",
                          "MEDIUM", location=at_hotspot)
            return
        self._check()
        parts = {item.reference: item for item in self.snapshot.footprints}
        # The parts that filter: the hotspot alone said nothing of them. On P02
        # Rev E it sat on Q10's drain pad, 4.4 mm from C37 (22 uF X5R).
        hot = sorted(
            ((reference, temperature) for reference, temperature in temperatures.items()
             if temperature > HOT_PASSIVE_C and reference in parts and (
                 self._is_capacitor(parts[reference])
                 or _INDUCTOR_REF_RE.match(reference) or _FERRITE_REF_RE.match(reference))),
            key=lambda item: (-item[1], item[0]),
        )
        if hot:
            named = ", ".join(f"{reference} {temperature:.1f} C" for reference, temperature in hot[:12])
            self._add("TE-001", "THERMAL", "INFO", "High temperature may alter EMC components",
                      f"{len(hot)} capacitor(s), inductor(s) or ferrite(s) sit above "
                      f"{HOT_PASSIVE_C:g} C on the latest thermal result: {named}"
                      + (", ..." if len(hot) > 12 else "") + ".",
                      "Verify their capacitance under DC bias, their SRF and the ferrites' "
                      "impedance at these temperatures.",
                      "MEDIUM", components=[reference for reference, _temperature in hot],
                      location=at_hotspot)
        # One finding per dielectric: P02 Rev E's 2026-10-01 16:51 run had
        # thirteen X5R capacitors over 85 C, one cause (X5R chosen in the hot
        # power section), which made thirteen actions to fix.
        over = defaultdict(list)
        for reference, temperature in temperatures.items():
            part = parts.get(reference)
            if part is None or not self._is_capacitor(part):
                continue
            rating = dielectric_rating_c(part.value)
            if rating is not None and temperature > rating[1]:
                over[rating].append((temperature - rating[1], reference, part, temperature))
        # How far the board under each part moved at twice the thermal grid
        # step. P02 Rev E's 21:42 hotspot moved 4.02 K while C49 sat 0.7 K
        # over its 85 C, and TH-004 said no part's margin depended on the mesh.
        sensitivity = getattr(self.thermal_result, "mesh_sensitivity", None) or {}
        moves = {
            str(reference): abs(float(fine) - float(coarse))
            for reference, (fine, coarse) in (sensitivity.get("placement_c") or {}).items()
        }
        # And how much cooler the whole board could be because the ambient
        # rests on a default: P02 Rev E's air, 16.2 K over 45 C on the
        # default 5 W/m2K wall coefficient, is 2.7 K cooler at 6 (TH-009).
        ambient_k = float(getattr(self.thermal_result, "ambient_uncertainty_k", 0.0) or 0.0)
        grids = (f"{float(sensitivity.get('fine_grid_mm', 0.0)):g} and "
                 f"{float(sensitivity.get('coarse_grid_mm', 0.0)):g} mm thermal grids")
        parts_of_it = (
            ([f"the move of the board under each between the {grids}"] if moves else [])
            + ([f"the {ambient_k:.1f} K the internal air would drop at 1 W/m2K more on the "
                "enclosure's default wall coefficient (TH-009)"] if ambient_k > 0.0 else [])
        )
        for (code, rated_c), items in sorted(over.items()):
            items.sort(key=lambda item: (-item[0], item[1]))
            known = bool(parts_of_it) and (not moves or all(item[1] in moves for item in items))
            uncertainty = {
                reference: moves.get(reference, 0.0) + ambient_k
                for _excess, reference, _part, _t in items
            }
            firm = [item for item in items if not known or item[0] > uncertainty[item[1]]]
            within = [item for item in items if known and item[0] <= uncertainty[item[1]]]
            if firm:
                named = "; ".join(
                    f"{reference} ({part.value}) {temperature:.1f} C, +{excess:.1f} K"
                    for excess, reference, part, temperature in firm
                )
                note = ""
                if known:
                    # Said, not left silent: P02 Rev E's 22:29 report could
                    # not tell a firm excess from a check never made.
                    largest, where = max((uncertainty[item[1]], item[1]) for item in firm)
                    note = (f" Each excess is larger than the model's uncertainty under the "
                            f"capacitor, {largest:.1f} K at most ({where}): "
                            + " plus ".join(parts_of_it) + ".")
                self._add("TE-002", "THERMAL", "HIGH",
                          "Capacitors above their dielectric's rated temperature",
                          f"{len(firm)} {code} capacitor(s), rated to {rated_c:g} C, exceed it on the "
                          f"latest thermal result: {named}. Beyond it the datasheet no longer "
                          f"specifies their capacitance or their life.{note}",
                          "Use a dielectric rated higher (X7R, 125 C) in the same value and voltage, "
                          "or move the capacitors away from the heat; check their capacitance at "
                          "temperature and DC bias.",
                          "MEDIUM", components=[item[1] for item in firm],
                          location=(*firm[0][2].position, None))
            if within:
                # P02 Rev E's 13 X5R capacitors were all "fix before
                # manufacture", C49's 0.7 K as much as C37's 21.8 K.
                named = "; ".join(
                    f"{reference} ({part.value}) {temperature:.1f} C, +{excess:.1f} K against "
                    f"{uncertainty[reference]:.1f} K"
                    for excess, reference, part, temperature in within
                )
                self._add("TE-002", "THERMAL", "MEDIUM",
                          "Capacitors over their dielectric's rated temperature by less than the "
                          "model's uncertainty",
                          f"{len(within)} {code} capacitor(s), rated to {rated_c:g} C, exceed it by "
                          f"no more than the model's uncertainty under them: {named}. It adds "
                          + " and ".join(parts_of_it) + ". Whether they exceed their rating rests "
                          "on the model as much as on the board.",
                          "Enter the inputs the uncertainty rests on and rerun the thermal analysis "
                          "at a finer grid; if the excess holds, use a dielectric rated higher (X7R, "
                          "125 C).",
                          "MEDIUM", components=[item[1] for item in within],
                          location=(*within[0][2].position, None))

    def _finalize(self, started):
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
        self.result.findings.sort(key=lambda item: (order.get(item.severity, 9), item.rule_id, item.title))
        counts = {name: 0 for name in order}
        by_rule = defaultdict(list)
        for finding in self.result.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
            by_rule[finding.rule_id].append(finding)
        penalty = 0
        penalties_by_rule = {}
        cap = max(1, int(self.settings.maximum_findings_per_rule_for_score))
        for rule_id, findings in by_rule.items():
            if rule_id in UNSCORED_RULES:
                continue
            rule_penalty = sum(
                int(round(SEVERITY_WEIGHT.get(item.severity, 0)
                          * CONFIDENCE_WEIGHT.get(item.confidence, 0.5)))
                for item in findings[:cap]
            )
            if rule_penalty:
                penalties_by_rule[rule_id] = rule_penalty
                penalty += rule_penalty
        self.result.score_penalties_by_rule = dict(sorted(
            penalties_by_rule.items(), key=lambda item: (-item[1], item[0])
        ))
        self.result.risk_score = max(0, 100 - penalty)
        self.result.total_checks = self._checks
        self.result.severity_counts = counts
        per_net_penalty = defaultdict(int)
        for finding in self.result.findings:
            for net in finding.nets:
                per_net_penalty[net] += int(round(
                    SEVERITY_WEIGHT.get(finding.severity, 0)
                    * CONFIDENCE_WEIGHT.get(finding.confidence, 0.5)
                ))
        self.result.per_net_scores = {net: max(0, 100 - value)
                                      for net, value in sorted(per_net_penalty.items())}
        priority = [item for item in self.result.findings if item.severity in {"CRITICAL", "HIGH"}]
        self.result.test_plan = [
            f"Near-field scan around ({point.x_mm:.2f}, {point.y_mm:.2f}) mm: {point.reason}"
            for point in self.result.probe_points[:12]
        ]
        if self.result.frequency_risks:
            freqs = sorted({round(item.frequency_hz / 1e6, 3) for item in self.result.frequency_risks})[:12]
            self.result.test_plan.append("Prioritise harmonic checks near: " + ", ".join(map(str, freqs)) + " MHz.")
        if not self.result.test_plan:
            self.result.test_plan.append("Perform a baseline conducted and radiated pre-scan across the configured band.")
        self.result.regulatory_coverage = [
            # No limit line is evaluated: the standard is a reference, not a
            # target this analysis checks against (EMC-8).
            f"Reference standard (not evaluated): {self.settings.standard} ({self.settings.market}).",
            "PCB geometry, return paths, PDN reuse and relative source harmonics are covered.",
            "Absolute emissions, immunity, enclosure seams and cable routing require physical tests.",
        ]
        classes_from_schematic = bool(getattr(self.snapshot, "component_classes", None))
        # A source whose current nobody stated runs on the 0.1 A default; the
        # H field and drive index of those sources scale with that guess
        # (previous audits: the default was assumed, never said).
        assumed_current = [
            item.name for item in self.settings.sources
            if item.enabled and item.source not in ("manual", "power-tree")
            and abs(float(item.current_a) - 0.1) < 1e-12
        ]
        self.result.limitations = [
            "Risk analyzer only; it cannot certify FCC/CISPR/MIL compliance.",
            "Relative emission estimates typically carry at least +/-10 to 20 dB uncertainty.",
            # The two statements this replaces were stale: component classes
            # come from the schematic's symbols when it is available, and an
            # unknown source frequency is excluded (EE-003), not defaulted.
            (
                "Decoupling, protection and connector roles come from the schematic's symbols."
                if classes_from_schematic else
                "No schematic data was available: decoupling, protection and connector roles "
                "are inferred from footprint names and values, with low confidence."
            ),
            "Sources without a known frequency are excluded from harmonic and near-field "
            "estimates (EE-003) rather than given a default.",
            "The score out of 100 is a relative, uncalibrated risk ranking weighted by "
            "confidence; it is not a compliance margin.",
            "Near-field maps are an uncalibrated quasi-static ranking of where fields "
            "concentrate; their V/m and A/m are not comparable to an emission limit.",
        ] + ([
            f"{len(assumed_current)} source(s) use an assumed 0.1 A current, which their "
            "magnetic field and drive index scale with: "
            + ", ".join(assumed_current[:10]) + (", ..." if len(assumed_current) > 10 else "")
            + ". Enter the real current to replace it."
        ] if assumed_current else [])
        self.result.elapsed_seconds = time.perf_counter() - started
        self.log(f"EMI/EMC analysis completed: {len(self.result.findings)} findings, score {self.result.risk_score}/100.")
        return self.result

    def analyze(self, progress_callback=None):
        started = time.perf_counter()
        if live_capture_has_no_copper(
            len(self.snapshot.tracks), len(self.snapshot.vias),
            sum(len(layers) for layers in self.snapshot.zones_by_net.values()),
            sum(len(footprint.nets) for footprint in self.snapshot.footprints),
        ):
            # KiCad's IPC answers every collection empty while the PCB editor
            # is still synchronising. On P02 Rev E a campaign run at 21:39
            # (report campaign-...-213933) hit exactly this: 62/100 with 33
            # checks instead of 1569, the only geometric finding being GP-002
            # CRITICAL "No ground plane detected" -- false, since no zone was
            # captured at all, not just the ground ones. Refusing here, like
            # thermal_model.ThermalModelBuilder.build does for the same IPC
            # condition (1bf2be7), keeps that false finding and score out of
            # the report instead of computing them from nothing.
            raise ValueError(
                "KiCad IPC returned no copper geometry on any layer (no tracks, vias, "
                "filled zones or net-connected pads), so every EMI/EMC check would read "
                "an empty board rather than the real one. Wait for PCB Editor "
                "synchronisation and retry."
            )
        feeds = sorted(name for name in self._source_by_net if name in self._dc_feed_nets)
        if feeds:
            self.log(
                f"EMC: {', '.join(feeds)} declared as DC feeds returning on their own conductor; "
                "the plane-gap (GP-001) and ground-via (RP-001) checks skip them."
            )
        stages = [
            ("Ground planes", self._ground_rules),
            ("Stackup", self._stackup_rules),
            ("Signals and edges", self._signal_rules),
            ("Return paths", self._return_path_rules),
            ("Via stitching", self._stitching_rules),
            ("Components and I/O", self._component_rules),
            ("Switching sources", self._switching_rules),
            ("Inductor magnetic models", self._inductor_rules),
            ("Power tree", self._power_tree_rules),
            ("Differential pairs", self._differential_rules),
            ("Crosstalk", self._crosstalk_rules),
            ("PDN", self._pdn_rules),
            ("Emission estimates", self._emission_rules),
            ("Thermal interaction", self._thermal_rules),
        ]
        for index, (name, callback) in enumerate(stages, start=1):
            callback()
            if progress_callback:
                progress_callback(index, len(stages), name)
        return self._finalize(started)
