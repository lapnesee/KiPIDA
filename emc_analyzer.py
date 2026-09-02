"""Deterministic EMI/EMC pre-compliance analysis for live KiCad boards.

The analyzer intentionally reports risks and traceable geometric evidence.  It
does not claim regulatory compliance and does not replace full-wave simulation
or accredited measurements.
"""

from collections import defaultdict
from dataclasses import dataclass, field
import math
from pathlib import Path
import re
import time

try:
    from shapely.geometry import LineString, Point, box
    from shapely.ops import unary_union
except ImportError:  # pragma: no cover - Ki-PIDA normally ships Shapely
    LineString = Point = box = None
    unary_union = None

try:
    from .differential_length import protocol_skew_limit_ps
    from .extractor import GeometryExtractor, to_mm
    from .inductor_em import resolve_inductor_models, TargetedInductorRefiner
    from .models import (
        EMCAnalysisResult, EMCEvidence, EMCFinding, EMCFrequencyRisk,
        EMCProbePoint, EMCSignalSource,
    )
except (ImportError, ValueError):
    from differential_length import protocol_skew_limit_ps
    from extractor import GeometryExtractor, to_mm
    from inductor_em import resolve_inductor_models, TargetedInductorRefiner
    from models import (
        EMCAnalysisResult, EMCEvidence, EMCFinding, EMCFrequencyRisk,
        EMCProbePoint, EMCSignalSource,
    )


SEVERITY_WEIGHT = {"CRITICAL": 25, "HIGH": 12, "MEDIUM": 6, "LOW": 2, "INFO": 0}
CONFIDENCE_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.65, "LOW": 0.25}
SWITCH_TOKEN_PATTERN = re.compile(r"(^|[_/])(PH|LX)([_/]|$)", re.I)
CONTEXTUAL_SW_PATTERN = re.compile(
    r"(^|[^A-Z0-9])(?:U\d+|BUCK\d*|REG\d*|CONVERTER|SWITCHER)[_-]SW(?:[^A-Z0-9]|$)", re.I,
)


def is_switching_node_name(name):
    """True for converter switch nodes, not generic switched DC rail suffixes."""
    return bool(SWITCH_TOKEN_PATTERN.search(name) or CONTEXTUAL_SW_PATTERN.search(name))


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
    diameter_mm: float = 0.0
    drill_mm: float = 0.0


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
    ignored_offboard_items: int = 0
    ignored_offboard_nets: tuple = ()
    ignored_offboard_counts: dict = field(default_factory=dict)
    inductors: list = field(default_factory=list)

    @property
    def tracks_by_net(self):
        grouped = defaultdict(list)
        for track in self.tracks:
            grouped[track.net_name].append(track)
        return dict(grouped)

    @classmethod
    def capture(cls, board, settings, rails=None, differential_pairs=None,
                board_file_path=None, log_callback=None):
        if board is None:
            raise RuntimeError(
                "No live KiCad PCB is connected; EMI/EMC geometry capture was cancelled."
            )
        extractor = GeometryExtractor(board, log_callback=log_callback)

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

        def board_via_dimensions(path):
            """Read saved via size/drill as a fallback for incomplete IPC objects."""
            if not path:
                return {}
            path = Path(path)
            if path.suffix.lower() != ".kicad_pcb" or not path.is_file():
                return {}
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                return {}
            dimensions, block, depth = {}, [], 0
            for line in lines:
                stripped = line.lstrip()
                if not block and stripped.startswith("(via"):
                    block, depth = [stripped], stripped.count("(") - stripped.count(")")
                    continue
                if not block:
                    continue
                block.append(stripped)
                depth += stripped.count("(") - stripped.count(")")
                if depth > 0:
                    continue
                text = " ".join(block)
                at = re.search(r"\(at\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)", text)
                size = re.search(r"\(size\s+([-+0-9.eE]+)", text)
                drill = re.search(r"\(drill\s+([-+0-9.eE]+)", text)
                if at and size and drill:
                    key = (round(float(at.group(1)), 6), round(float(at.group(2)), 6))
                    dimensions[key] = (float(size.group(1)), float(drill.group(1)))
                block, depth = [], 0
            return dimensions

        def dimension_mm(candidate):
            if candidate is None:
                return 0.0
            if isinstance(candidate, (int, float)):
                return max(to_mm(candidate), 0.0)
            for name in ("diameter", "size", "value"):
                nested = value(candidate, name)
                if isinstance(nested, (int, float)):
                    return max(to_mm(nested), 0.0)
            axes = [value(candidate, axis) for axis in ("x", "y")]
            axes = [to_mm(item) for item in axes if isinstance(item, (int, float)) and item > 0]
            return min(axes) if axes else 0.0

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
        saved_via_dimensions = board_via_dimensions(board_file_path)
        via_items = list(items("vias"))
        # Some adapters expose vias in the track collection.
        if not via_items:
            via_items = [item for item in items("tracks") if value(item, "diameter") is not None]
        for item in via_items:
            position = point(value(item, "position", value(item, "start")))
            name = net_name(item)
            raw_layers = value(item, "layers", value(item, "layer_ids", [])) or []
            padstack = value(item, "padstack", value(item, "pad_stack"))
            drill = value(padstack, "drill")
            if not raw_layers:
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
            diameter_mm = dimension_mm(
                value(item, "diameter", value(item, "size", value(padstack, "diameter")))
            )
            drill_mm = dimension_mm(
                value(item, "drill", value(item, "drill_size", drill))
            )
            if position is not None and (diameter_mm <= 0.0 or drill_mm <= 0.0):
                saved = saved_via_dimensions.get(
                    (round(position[0], 6), round(position[1], 6))
                )
                if saved:
                    diameter_mm = diameter_mm or saved[0]
                    drill_mm = drill_mm or saved[1]
            if name and position is not None:
                if not inside(position):
                    ignored_count += 1
                    ignored_counts["vias"] += 1
                    ignored_nets.add(name)
                    continue
                vias.append(EMCVia(name, position, layers, diameter_mm, drill_mm))

        footprints = []
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
        inductors = resolve_inductor_models(
            settings.inductor_models, footprints, rails or [], settings.sources,
        )
        return cls(
            bounds_mm=bounds,
            stackup=extractor.get_stackup_profile(),
            tracks=tracks,
            vias=vias,
            footprints=footprints,
            zones_by_net=zones,
            power_nets=power_nets,
            ignored_offboard_items=ignored_count,
            ignored_offboard_nets=tuple(sorted(ignored_nets)),
            ignored_offboard_counts=dict(sorted(ignored_counts.items())),
            inductors=inductors,
        )


class EMCSourceDiscoverer:
    """Name/evidence based source discovery with manual-setting preservation."""
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

    @classmethod
    def discover(cls, net_names, existing=None, differential_pairs=None,
                 switching_frequencies=None):
        retained = {source.net_name: source for source in (existing or []) if source.source == "manual"}
        switching_frequencies = switching_frequencies or {}
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
            if name in retained:
                continue
            if cls.CLOCK_PATTERN.search(name):
                discovered.append(EMCSignalSource(name, name, "CLOCK", 25e6, 2.0, source="auto"))
            elif cls.is_switching_node(name):
                ref_match = re.search(
                    r"(^|[^A-Z0-9])(U\d+)[_-]SW(?:[^A-Z0-9]|$)", name, re.I,
                )
                ref_des = ref_match.group(2).upper() if ref_match else ""
                parameters = switching_frequencies.get(ref_des, 500e3)
                if isinstance(parameters, dict):
                    frequency = float(parameters.get("frequency_hz", 500e3))
                    voltage = float(parameters.get("voltage_swing_v", 3.3))
                    current = float(parameters.get("current_a", 0.1))
                    origin = "power-tree"
                else:
                    frequency = float(parameters)
                    voltage, current, origin = 3.3, 0.1, "auto"
                discovered.append(EMCSignalSource(
                    name, name, "SWITCHING", frequency, 10.0, source=origin,
                    voltage_swing_v=voltage, current_a=current,
                    parameter_confidence="MEDIUM" if origin == "power-tree" else "LOW",
                    parameter_notes=(
                        "Frequency from converter loss model; voltage from input rail; "
                        "current from downstream load; 10 ns rise time remains an editable estimate."
                        if origin == "power-tree" else
                        "Name-based switching-source defaults; confirm all electrical parameters."
                    ),
                ))
            elif cls.FAST_PATTERN.search(name) and name not in paired_nets:
                voltage, current = cls._usb_levels(name)
                discovered.append(EMCSignalSource(
                    name, name, "DIGITAL", 100e6, 1.0, source="auto",
                    voltage_swing_v=voltage, current_a=current,
                    parameter_confidence="LOW",
                    parameter_notes="Interface-name defaults; confirm activity rate and edge time.",
                ))
        for pair in enabled_pairs:
            if pair.positive_net in retained or pair.negative_net in retained:
                continue
            # USB 2.0 HS is 480 Mbit/s NRZI.  A maximum-transition pattern
            # alternates state every bit, so its square-wave fundamental is
            # 240 MHz; 480 MHz is the bit rate, not the fundamental.
            default_frequency = 240e6 if "USB" in pair.interface.upper() else 125e6
            voltage, current = cls._usb_levels(pair.name + " " + pair.interface)
            discovered.append(EMCSignalSource(
                pair.name, pair.positive_net, "DIFFERENTIAL", default_frequency,
                0.8, source="differential-scan",
                voltage_swing_v=voltage, current_a=current,
                negative_net_name=pair.negative_net,
                parameter_confidence="MEDIUM",
                parameter_notes=(
                    "Differential nets confirmed by pair scan; USB HS uses a 240 MHz "
                    "maximum-toggle fundamental derived from the 480 Mbit/s NRZI rate. "
                    "Electrical levels, edge time and activity remain editable defaults."
                ),
            ))
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
        if len(self._copper_layers) >= 4 and not self._ground_zones:
            self._add("GP-002", "GROUND", "CRITICAL", "No ground plane detected",
                      "The multilayer board has no filled zone on a configured ground net.",
                      "Add a continuous ground plane and verify the configured ground-net aliases.", "HIGH")
            return
        bounds = self.snapshot.bounds_mm
        board_area = max((bounds[2] - bounds[0]) * (bounds[3] - bounds[1]), 1e-9)
        for layer in sorted(self._ground_plane_layers):
            geometry = self._ground_zones[layer]
            parts = list(getattr(geometry, "geoms", [geometry]))
            if len(parts) > 3:
                self._add("GP-003", "GROUND", "HIGH", "Fragmented ground plane",
                          f"Ground copper on layer {layer} contains {len(parts)} disconnected islands.",
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
        for first, second in zip(self._copper_layers, self._copper_layers[1:]):
            if first.layer_id not in plane_layers and second.layer_id not in plane_layers:
                self._add("SU-001", "STACKUP", "HIGH", "Adjacent signal layers",
                          f"{first.name} and {second.name} have no detected reference plane between them.",
                          "Use an adjacent solid ground plane or route the layers in orthogonal directions.",
                          "MEDIUM")
        for layer in self._copper_layers:
            if layer.layer_id in plane_layers:
                continue
            reference, distance = self._nearest_ground_layer(layer.layer_id)
            if distance is not None and distance > 0.3:
                self._add("SU-002", "STACKUP", "LOW", "Signal layer far from reference",
                          f"{layer.name} is approximately {distance:.3f} mm from the nearest detected ground plane.",
                          "Reduce signal-to-reference spacing in the fabrication stackup.", "HIGH")

    @staticmethod
    def _track_midpoint(track):
        return ((track.start[0] + track.end[0]) / 2.0,
                (track.start[1] + track.end[1]) / 2.0)

    def _signal_rules(self):
        source_by_net = self._source_by_net
        connectors = [item for item in self.snapshot.footprints
                      if any(item.reference.upper().startswith(prefix.upper())
                             for prefix in self.settings.external_connector_prefixes)]
        bounds = self.snapshot.bounds_mm
        reference_names = set(self.settings.reference_net_names)
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
            if (net in self._fast_nets and self._enabled("GROUND")
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
        capacitors = [item for item in self.snapshot.footprints if item.reference.upper().startswith("C")]
        protection_value = re.compile(
            r"(?:USBLC|PESD|TPD\d|ESD|TVS|TRANSIL|SMBJ|SMAJ|SMCJ|SRV05|PRTR|CMF|CMC|COMMON.?MODE|FERRITE)",
            re.I,
        )

        def is_filter(item):
            return bool(
                re.match(r"^(FB|FL|TVS|CMC)", item.reference, re.I)
                or protection_value.search(item.value)
            )

        def is_esd_protector(item):
            return bool(
                re.match(r"^(TVS|ESD)", item.reference, re.I)
                or re.search(r"(?:USBLC|PESD|TPD\d|ESD|TVS|TRANSIL|SMBJ|SMAJ|SMCJ|SRV05|PRTR)", item.value, re.I)
            )

        filters = [item for item in self.snapshot.footprints if is_filter(item)]
        if self._enabled("DECOUPLING"):
            for ic in [item for item in self.snapshot.footprints if item.reference.upper().startswith(("U", "IC"))]:
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
                    self._add("DC-002", "DECOUPLING", "HIGH", "No nearby decoupling capacitor",
                              f"No capacitor sharing a known supply rail with {ic.reference} was found.",
                              "Verify every supply pin and place its high-frequency capacitor at the pin/via pair.",
                              "LOW", components=[ic.reference], location=(*ic.position, None))
                elif nearest[0] > 5.0:
                    severity = (
                        "MEDIUM" if power_management else
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
                    self._add("DC-001", "DECOUPLING", severity, title,
                              f"The nearest capacitor sharing {', '.join(nearest[2])} with "
                              f"{ic.reference} is {nearest[1].reference} at {nearest[0]:.2f} mm.",
                              recommendation,
                              "LOW" if power_management else "MEDIUM",
                              components=[ic.reference, nearest[1].reference], location=(*ic.position, None))
        if any(self._enabled(category) for category in ("IO", "ESD", "SHIELDING")):
            for connector in [item for item in self.snapshot.footprints
                              if any(item.reference.upper().startswith(prefix.upper())
                                     for prefix in self.settings.external_connector_prefixes)]:
                self._check()
                likely_external = bool(re.search(r"USB|HDMI|RJ|ETH|CAN|EXT", connector.value, re.I))
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
                    protector = min(
                        protectors, key=lambda item: math.dist(connector.position, item.position),
                        default=None,
                    )
                    protector_distance = (
                        math.dist(connector.position, protector.position) if protector else float("inf")
                    )
                    if protector_distance > 10.0:
                        self._add("ES-001", "ESD", "HIGH", "ESD path is missing or too long",
                                  f"No TVS/diode protection was detected within 10 mm of {connector.reference}.",
                                  "Place the TVS at the connector and route the discharge path directly to chassis/ground.",
                                  "LOW", components=[connector.reference], location=(*connector.position, None))
                    elif protector is not None:
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

    def _switching_rules(self):
        if not self._enabled("SWITCHING"):
            return
        for source in [item for item in self.settings.sources if item.enabled and item.kind == "SWITCHING"]:
            tracks = self._tracks.get(source.net_name, [])
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
            area = sum(track.length_mm * track.width_mm for track in tracks)
            for geometry in self.snapshot.zones_by_net.get(source.net_name, {}).values():
                area += float(geometry.area)
            if area > 25.0:
                severity = "HIGH" if area > 100.0 else "MEDIUM"
                self._add("SW-002", "SWITCHING", severity, "Large switching-node copper area",
                          f"{source.net_name} occupies approximately {area:.1f} mm2 of copper.",
                          "Restrict SW/PH/LX copper to the shortest IC-to-inductor connection.",
                          "MEDIUM", [source.net_name], location=source_location)
            if source.frequency_hz > 0:
                first = max(1, int(math.ceil(self.settings.frequency_start_hz / source.frequency_hz)))
                harmonic = first * source.frequency_hz
                if harmonic <= self.settings.frequency_stop_hz:
                    rise_time_s = max(float(source.rise_time_ns), 1e-6) * 1e-9
                    rolloff = 1.0 / math.sqrt(1.0 + (2.0 * math.pi * harmonic * rise_time_s) ** 2)
                    relative = rolloff / first
                    relative_db = 20.0 * math.log10(max(relative, 1e-12))
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
                              f"to its low-frequency square-wave envelope (drive index {drive_index:.3g} V*A).",
                              "Minimise hot-loop area and verify this band with a near-field probe.",
                              "HIGH" if source.source == "manual" else
                              "MEDIUM" if source.source == "power-tree" else "LOW",
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
                        severity = "HIGH" if re.search(r"ADC|ANALOG|SENSE", victim, re.I) else "MEDIUM"
                        self._add("XT-001", "CROSSTALK", severity, "Long parallel trace coupling",
                                  f"{aggressor} and {victim} run in parallel within {distance:.3f} mm; 3H target is {threshold:.3f} mm.",
                                  "Increase spacing to at least 3H, change layer, or insert a grounded guard.",
                                  "MEDIUM", [aggressor, victim], location=(x, y, first.layer_id))
                        reported.add(key)

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
            if worst > target:
                self._add("PD-001", "PDN", "HIGH", "PDN impedance exceeds target",
                          f"{name}: {worst:.4g} ohm at {frequency / 1e6:.3f} MHz exceeds {target:.4g} ohm.",
                          "Optimise capacitor values/placement and rerun the AC impedance analysis.",
                          "HIGH", [name])
            else:
                self._add("PD-002", "PDN", "INFO", "PDN remains within target",
                          f"{name}: worst impedance {worst:.4g} ohm is within the {target:.4g} ohm target.",
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
                level = -20.0 * math.log10(max(harmonic, 1))
                if frequency > 1.0 / (math.pi * rise_s):
                    level -= 20.0 * math.log10(frequency * math.pi * rise_s)
                self.result.frequency_risks.append(EMCFrequencyRisk(frequency, level, source.name))
                harmonic += 1
        if self.result.frequency_risks:
            self._add("EE-002", "EMISSIONS", "INFO", "Source harmonic envelopes estimated",
                      f"Generated {len(self.result.frequency_risks)} relative harmonic markers for lab prioritisation.",
                      "Treat amplitudes as relative ranking only; measure absolute field strength.",
                      "HIGH" if all(item.source == "manual" for item in self.settings.sources if item.enabled) else "LOW")

    def _thermal_rules(self):
        if not self._enabled("THERMAL") or self.thermal_result is None:
            return
        hotspot = getattr(self.thermal_result, "hotspot", None)
        if hotspot is not None and hotspot.temperature_c > 85.0:
            self._check()
            self._add("TH-001", "THERMAL", "MEDIUM", "High temperature may alter EMC components",
                      f"The latest thermal hotspot is {hotspot.temperature_c:.1f} C.",
                      "Verify MLCC DC-bias/SRF and ferrite impedance at the local operating temperature.",
                      "MEDIUM", location=(hotspot.x_mm, hotspot.y_mm, None))

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
            f"Target profile: {self.settings.standard} ({self.settings.market}).",
            "PCB geometry, return paths, PDN reuse and relative source harmonics are covered.",
            "Absolute emissions, immunity, enclosure seams and cable routing require physical tests.",
        ]
        self.result.limitations = [
            "Risk analyzer only; it cannot certify FCC/CISPR/MIL compliance.",
            "Relative emission estimates typically carry at least +/-10 to 20 dB uncertainty.",
            "Footprint-name decoupling and connector checks have low confidence without schematic pin metadata.",
            "Automatically detected source frequencies are editable defaults until confirmed by the designer.",
            "Risk-score penalties are confidence-weighted; low-confidence heuristics cannot dominate the score.",
        ]
        self.result.elapsed_seconds = time.perf_counter() - started
        self.log(f"EMI/EMC analysis completed: {len(self.result.findings)} findings, score {self.result.risk_score}/100.")
        return self.result

    def analyze(self, progress_callback=None):
        started = time.perf_counter()
        stages = [
            ("Ground planes", self._ground_rules),
            ("Stackup", self._stackup_rules),
            ("Signals and edges", self._signal_rules),
            ("Return paths", self._return_path_rules),
            ("Via stitching", self._stitching_rules),
            ("Components and I/O", self._component_rules),
            ("Switching sources", self._switching_rules),
            ("Inductor magnetic models", self._inductor_rules),
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
