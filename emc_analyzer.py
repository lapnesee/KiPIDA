"""Deterministic EMI/EMC pre-compliance analysis for live KiCad boards.

The analyzer intentionally reports risks and traceable geometric evidence.  It
does not claim regulatory compliance and does not replace full-wave simulation
or accredited measurements.
"""

from collections import defaultdict
from dataclasses import dataclass, field
import math
import re
import time

try:
    from shapely.geometry import LineString, Point
    from shapely.ops import unary_union
except ImportError:  # pragma: no cover - Ki-PIDA normally ships Shapely
    LineString = Point = None
    unary_union = None

try:
    from .extractor import GeometryExtractor, to_mm
    from .models import (
        EMCAnalysisResult, EMCEvidence, EMCFinding, EMCFrequencyRisk,
        EMCProbePoint, EMCSignalSource,
    )
except (ImportError, ValueError):
    from extractor import GeometryExtractor, to_mm
    from models import (
        EMCAnalysisResult, EMCEvidence, EMCFinding, EMCFrequencyRisk,
        EMCProbePoint, EMCSignalSource,
    )


SEVERITY_WEIGHT = {"CRITICAL": 25, "HIGH": 12, "MEDIUM": 6, "LOW": 2, "INFO": 0}
PROTOCOL_SKEW_PS = {"USB": 25.0, "USB_HS": 25.0, "USB_SS": 5.0,
                    "ETHERNET": 50.0, "HDMI": 20.0, "PCIE": 5.0}


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


@dataclass(frozen=True)
class EMCFootprint:
    reference: str
    value: str
    position: tuple
    nets: tuple = ()


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

    @property
    def tracks_by_net(self):
        grouped = defaultdict(list)
        for track in self.tracks:
            grouped[track.net_name].append(track)
        return dict(grouped)

    @classmethod
    def capture(cls, board, settings, rails=None, differential_pairs=None,
                board_file_path=None, log_callback=None):
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

        tracks = []
        for item in items("tracks"):
            start, end = point(value(item, "start")), point(value(item, "end"))
            name = net_name(item)
            width = to_mm(value(item, "width", 0.0))
            if not name or start is None or end is None or width <= 0:
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
            try:
                layers = tuple(int(layer) for layer in raw_layers)
            except TypeError:
                layers = ()
            if name and position is not None:
                vias.append(EMCVia(name, position, layers))

        footprints = []
        all_nets = {track.net_name for track in tracks} | {via.net_name for via in vias}
        for item in items("footprints"):
            reference = str(value(item, "reference", value(item, "ref_des", "")) or "")
            if not reference:
                field_value = value(value(value(item, "reference_field"), "text"), "value", "")
                reference = str(field_value or "")
            footprint_position = point(value(item, "position"))
            pads = value(item, "pads")
            if pads is None:
                pads = value(value(item, "definition"), "pads", [])
            pad_points, pad_nets = [], set()
            for pad in pads or []:
                pad_position = point(value(pad, "position"))
                if pad_position is not None:
                    pad_points.append(pad_position)
                name = net_name(pad)
                if name:
                    pad_nets.add(name)
                    all_nets.add(name)
            if footprint_position is None and pad_points:
                footprint_position = (
                    sum(item[0] for item in pad_points) / len(pad_points),
                    sum(item[1] for item in pad_points) / len(pad_points),
                )
            if reference and footprint_position is not None:
                footprints.append(EMCFootprint(
                    reference, str(value(item, "value", "") or ""),
                    footprint_position, tuple(sorted(pad_nets)),
                ))

        power_nets = {rail.net_name for rail in (rails or [])}
        requested_zones = set(settings.reference_net_names) | power_nets
        requested_zones.update(
            source.net_name for source in settings.sources
            if source.enabled and source.net_name
        )
        requested_zones.update(
            name for name in all_nets if re.search(r"(^|[_/])(SW|PH|LX)([_/]|$)", name, re.I)
        )
        zones = {name: extractor.get_zone_geometry(name) for name in requested_zones}
        zones = {name: data for name, data in zones.items() if data}
        bounds = extractor.get_board_bounds(margin_mm=0.0, board_file_path=board_file_path)
        return cls(
            tuple(bounds), extractor.get_stackup_profile(), tracks, vias,
            footprints, zones, power_nets,
        )


class EMCSourceDiscoverer:
    """Name/evidence based source discovery with manual-setting preservation."""
    CLOCK_PATTERN = re.compile(r"(^|[_/])(CLK|CLOCK|MCLK|BCLK|SCLK|XTAL)([_/]|$)", re.I)
    SWITCH_PATTERN = re.compile(r"(^|[_/])(SW|PH|LX)([_/]|$)", re.I)
    FAST_PATTERN = re.compile(r"USB|HDMI|PCIE|ETH|RMII|RGMII|MIPI|LVDS", re.I)

    @classmethod
    def discover(cls, net_names, existing=None, differential_pairs=None):
        retained = {source.net_name: source for source in (existing or []) if source.source == "manual"}
        discovered = []
        for name in sorted(set(net_names)):
            if name in retained:
                continue
            if cls.CLOCK_PATTERN.search(name):
                discovered.append(EMCSignalSource(name, name, "CLOCK", 25e6, 2.0, source="auto"))
            elif cls.SWITCH_PATTERN.search(name):
                discovered.append(EMCSignalSource(name, name, "SWITCHING", 500e3, 10.0, source="auto"))
            elif cls.FAST_PATTERN.search(name):
                discovered.append(EMCSignalSource(name, name, "DIGITAL", 100e6, 1.0, source="auto"))
        for pair in differential_pairs or []:
            if not getattr(pair, "enabled", True):
                continue
            if pair.positive_net in retained or any(item.net_name == pair.positive_net for item in discovered):
                continue
            default_frequency = 480e6 if "USB" in pair.interface.upper() else 125e6
            discovered.append(EMCSignalSource(
                pair.name, pair.positive_net, "DIFFERENTIAL", default_frequency,
                0.8, source="differential-scan",
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
        if layer_id not in positions or not self._ground_zones:
            return None, None
        candidates = [(abs(positions[layer_id] - positions[item]), item)
                      for item in self._ground_zones if item in positions and item != layer_id]
        if not candidates:
            return None, None
        distance, reference = min(candidates)
        return reference, distance

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
        for layer, geometry in self._ground_zones.items():
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
        source_by_net = {source.net_name: source for source in self.settings.sources if source.enabled}
        connectors = [item for item in self.snapshot.footprints
                      if any(item.reference.upper().startswith(prefix.upper())
                             for prefix in self.settings.external_connector_prefixes)]
        bounds = self.snapshot.bounds_mm
        reference_names = set(self.settings.reference_net_names)
        for net, tracks in self._tracks.items():
            if net in reference_names or net in self.snapshot.power_nets or not tracks:
                continue
            source = source_by_net.get(net)
            total_length = sum(item.length_mm for item in tracks)
            worst_coverage, worst_track = 100.0, None
            if self._enabled("GROUND") and LineString is not None and self._ground_zones:
                self._check()
                for track in tracks:
                    reference, _ = self._nearest_ground_layer(track.layer_id)
                    zone = self._ground_zones.get(reference)
                    if zone is None or track.length_mm <= 1e-9:
                        coverage = 0.0
                    else:
                        line = LineString([track.start, track.end])
                        coverage = 100.0 * line.intersection(zone).length / track.length_mm
                    if coverage < worst_coverage:
                        worst_coverage, worst_track = coverage, track
                if worst_track is not None and worst_coverage < 95.0:
                    severity = "CRITICAL" if source else "HIGH"
                    x, y = self._track_midpoint(worst_track)
                    self._add("GP-001", "GROUND", severity, "Signal crosses a reference-plane void",
                              f"{net} has only {worst_coverage:.1f}% projected ground coverage on its worst segment.",
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
                if edge_track and edge_distance < threshold:
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
        for via in self.snapshot.vias:
            if via.net_name in self.settings.reference_net_names:
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
        filters = [item for item in self.snapshot.footprints
                   if re.match(r"^(FB|FL|TVS|D|CMC|L)", item.reference, re.I)]
        if self._enabled("DECOUPLING"):
            for ic in [item for item in self.snapshot.footprints if item.reference.upper().startswith(("U", "IC"))]:
                self._check()
                nearest = min(
                    ((math.dist(ic.position, cap.position), cap) for cap in capacitors),
                    key=lambda item: item[0], default=None,
                )
                if nearest is None or nearest[0] > 10.0:
                    self._add("DC-002", "DECOUPLING", "HIGH", "No nearby decoupling capacitor",
                              f"No capacitor footprint was found within 10 mm of {ic.reference}.",
                              "Verify every supply pin and place its high-frequency capacitor at the pin/via pair.",
                              "LOW", components=[ic.reference], location=(*ic.position, None))
                elif nearest[0] > 5.0:
                    severity = "HIGH" if nearest[0] > 8.0 else "MEDIUM"
                    self._add("DC-001", "DECOUPLING", severity, "Distant decoupling capacitor",
                              f"The nearest capacitor to {ic.reference} is {nearest[1].reference} at {nearest[0]:.2f} mm.",
                              "Move the high-frequency capacitor closer and minimise pad-to-via inductance.",
                              "LOW", components=[ic.reference, nearest[1].reference], location=(*ic.position, None))
        if any(self._enabled(category) for category in ("IO", "ESD", "SHIELDING")):
            for connector in [item for item in self.snapshot.footprints
                              if any(item.reference.upper().startswith(prefix.upper())
                                     for prefix in self.settings.external_connector_prefixes)]:
                self._check()
                likely_external = bool(re.search(r"USB|HDMI|RJ|ETH|CAN|EXT", connector.value, re.I))
                nearest = min((math.dist(connector.position, item.position) for item in filters), default=float("inf"))
                if self._enabled("IO") and likely_external and nearest > 25.0:
                    self._add("IO-001", "IO", "HIGH", "External connector lacks nearby EMC protection",
                              f"No ferrite, common-mode choke, TVS or filter was detected within 25 mm of {connector.reference}.",
                              "Place ESD protection and any interface filtering next to the connector entry point.",
                              "LOW", components=[connector.reference], location=(*connector.position, None))
                if self._enabled("ESD") and likely_external:
                    protectors = [item for item in filters if re.match(r"^(TVS|D)", item.reference, re.I)]
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
            self._check(2)
            area = sum(track.length_mm * track.width_mm for track in tracks)
            for geometry in self.snapshot.zones_by_net.get(source.net_name, {}).values():
                area += float(geometry.area)
            if area > 25.0:
                severity = "HIGH" if area > 100.0 else "MEDIUM"
                if tracks:
                    track = max(tracks, key=lambda item: item.length_mm * item.width_mm)
                    location = (*self._track_midpoint(track), track.layer_id)
                else:
                    location = None
                self._add("SW-002", "SWITCHING", severity, "Large switching-node copper area",
                          f"{source.net_name} occupies approximately {area:.1f} mm2 of copper.",
                          "Restrict SW/PH/LX copper to the shortest IC-to-inductor connection.",
                          "MEDIUM", [source.net_name], location=location)
            if source.frequency_hz > 0:
                first = max(1, int(math.ceil(self.settings.frequency_start_hz / source.frequency_hz)))
                harmonic = first * source.frequency_hz
                if harmonic <= self.settings.frequency_stop_hz:
                    self._add("SW-001", "SWITCHING", "HIGH" if first <= 20 else "MEDIUM",
                              "Switching harmonics enter the emissions band",
                              f"Harmonic {first} of {source.name} is {harmonic / 1e6:.3f} MHz.",
                              "Minimise hot-loop area and verify this band with a near-field probe.",
                              "HIGH" if source.source == "manual" else "LOW", [source.net_name])

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
            limit = next((value for name, value in PROTOCOL_SKEW_PS.items() if name in interface), 50.0)
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
            outer = sum(item.length_mm for item in positive + negative if item.layer_id in self._outer_layers)
            total = len_p + len_n
            if len(self._copper_layers) > 2 and outer > 0.5 * total:
                self._add("DP-004", "DIFFERENTIAL", "MEDIUM", "Differential pair mostly on outer layers",
                          f"{pair.name} has {100.0 * outer / max(total, 1e-9):.1f}% outer-layer routing.",
                          "Prefer a well-referenced stripline layer when stackup and fabrication permit it.",
                          "HIGH", [pair.positive_net, pair.negative_net], location=location)

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
        cap = max(1, int(self.settings.maximum_findings_per_rule_for_score))
        for findings in by_rule.values():
            penalty += sum(SEVERITY_WEIGHT.get(item.severity, 0) for item in findings[:cap])
        self.result.risk_score = max(0, 100 - penalty)
        self.result.total_checks = self._checks
        self.result.severity_counts = counts
        per_net_penalty = defaultdict(int)
        for finding in self.result.findings:
            for net in finding.nets:
                per_net_penalty[net] += SEVERITY_WEIGHT.get(finding.severity, 0)
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
