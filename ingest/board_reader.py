"""Read KiCad PCB files (.kicad_pcb) offline.

Extracts stackup, footprints, pads, segments, vias, zones, and board outline
without any dependency on kipy or pcbnew.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .sexpr import parse, find, find_all, get_str


@dataclass
class CopperLayer:
    layer_id: int
    name: str
    layer_type: str       # "signal", "power", "mixed", "jumper"
    thickness_mm: float
    epsilon_r: Optional[float] = None
    loss_tangent: Optional[float] = None


@dataclass
class Stackup:
    layers: list[CopperLayer]
    total_thickness_mm: float
    copper_layer_count: int


@dataclass
class Pad:
    number: str
    net_name: str
    pintype: str
    position: tuple[float, float]  # absolute (x, y)


@dataclass
class Footprint:
    reference: str
    value: str
    lib_id: str
    position: tuple[float, float]
    rotation: float
    layer: str
    sch_path: str
    sch_sheet_file: str
    pads: list[Pad] = field(default_factory=list)


@dataclass
class Segment:
    net_name: str
    width_mm: float
    layer: str
    start: tuple[float, float]
    end: tuple[float, float]


@dataclass
class Via:
    net_name: str
    position: tuple[float, float]
    size_mm: float
    drill_mm: float
    layers: list[str]


@dataclass
class Zone:
    net_name: str
    layer: str
    # Filled copper polygon vertices [(x, y), ...], empty list if zones not filled
    filled_polygon: list[tuple[float, float]] = field(default_factory=list)
    # Design-rule outline polygon [(x, y), ...], used as fallback
    outline_polygon: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class BoardBounds:
    x_min: float
    y_min: float
    x_max: float
    y_max: float

    @property
    def width_mm(self) -> float:
        return self.x_max - self.x_min

    @property
    def height_mm(self) -> float:
        return self.y_max - self.y_min

    @property
    def area_mm2(self) -> float:
        return self.width_mm * self.height_mm


@dataclass
class ParsedBoard:
    pcb_path: Path
    stackup: Stackup
    bounds: BoardBounds
    footprints: list[Footprint]
    segments: list[Segment]
    vias: list[Via]
    zones: list[Zone]

    @property
    def all_net_names(self) -> set[str]:
        nets: set[str] = set()
        for fp in self.footprints:
            for p in fp.pads:
                if p.net_name:
                    nets.add(p.net_name)
        for s in self.segments:
            if s.net_name:
                nets.add(s.net_name)
        for v in self.vias:
            if v.net_name:
                nets.add(v.net_name)
        for z in self.zones:
            if z.net_name:
                nets.add(z.net_name)
        return nets


def read_board(pcb_path: Path) -> ParsedBoard:
    """Parse a .kicad_pcb file and return a ParsedBoard."""
    pcb_path = Path(pcb_path)
    text = pcb_path.read_text(encoding="utf-8")
    roots = parse(text)
    if not roots or not isinstance(roots[0], list):
        raise ValueError(f"Cannot parse {pcb_path}: no root node found")
    root = roots[0]

    # Build net-code → net-name map for legacy integer net references
    net_name_map = _build_net_name_map(root)

    stackup = _parse_stackup(root)
    bounds = _parse_bounds(root)
    footprints = _parse_footprints(root, net_name_map)
    segments = _parse_segments(root, net_name_map)
    vias = _parse_vias(root, net_name_map)
    zones = _parse_zones(root, net_name_map)

    return ParsedBoard(
        pcb_path=pcb_path,
        stackup=stackup,
        bounds=bounds,
        footprints=footprints,
        segments=segments,
        vias=vias,
        zones=zones,
    )


# ---------------------------------------------------------------------------
# Internal parsers
# ---------------------------------------------------------------------------

def _build_net_name_map(root: list) -> dict[str, str]:
    """Build {net_code_str: net_name} from top-level (net N "name") declarations."""
    result: dict[str, str] = {}
    for node in find_all(root, "net"):
        if len(node) >= 3:
            code = str(node[1])
            name = node[2]
            if isinstance(name, str):
                result[code] = name
    return result


def _resolve_net(net_val: str, net_map: dict[str, str]) -> str:
    """Resolve a net value that may be a name string or an integer code."""
    if not net_val:
        return ""
    # If it's a pure integer code, look up in map
    if net_val.lstrip("-").isdigit():
        return net_map.get(net_val, "")
    return net_val


def _parse_stackup(root: list) -> Stackup:
    setup = find(root, "setup")
    if not setup:
        return Stackup(layers=[], total_thickness_mm=0.0, copper_layer_count=0)

    stackup_node = find(setup, "stackup")
    copper_layers: list[CopperLayer] = []
    total_thickness = 0.0

    # Top-level layer declarations give us layer_id and type
    layer_id_map: dict[str, int] = {}  # name → id
    layer_type_map: dict[str, str] = {}  # name → signal/power
    for layer_decl in find_all(root, "layers"):
        for entry in layer_decl[1:]:
            if isinstance(entry, list) and len(entry) >= 3:
                try:
                    lid = int(entry[0])
                    lname = entry[1]
                    ltype = entry[2]
                    if isinstance(lname, str) and isinstance(ltype, str):
                        layer_id_map[lname] = lid
                        layer_type_map[lname] = ltype
                except (ValueError, IndexError):
                    pass

    # Stackup gives us thickness and dielectric properties
    if stackup_node:
        for layer_node in find_all(stackup_node, "layer"):
            if not layer_node or len(layer_node) < 2:
                continue
            lname = layer_node[1] if isinstance(layer_node[1], str) else ""
            ltype_node = find(layer_node, "type")
            ltype_str = ltype_node[1] if ltype_node and len(ltype_node) > 1 else ""

            thick_node = find(layer_node, "thickness")
            thick = float(thick_node[1]) if thick_node and len(thick_node) > 1 else 0.0
            total_thickness += thick

            eps_node = find(layer_node, "epsilon_r")
            eps = float(eps_node[1]) if eps_node and len(eps_node) > 1 else None
            loss_node = find(layer_node, "loss_tangent")
            loss = float(loss_node[1]) if loss_node and len(loss_node) > 1 else None

            if ltype_str == "copper":
                layer_id = layer_id_map.get(lname, -1)
                layer_type = layer_type_map.get(lname, "signal")
                copper_layers.append(CopperLayer(
                    layer_id=layer_id,
                    name=lname,
                    layer_type=layer_type,
                    thickness_mm=thick,
                    epsilon_r=eps,
                    loss_tangent=loss,
                ))

    # Fallback: parse (general (thickness T)) for total thickness
    if total_thickness == 0.0:
        general = find(root, "general")
        if general:
            thick_node = find(general, "thickness")
            if thick_node and len(thick_node) > 1:
                try:
                    total_thickness = float(thick_node[1])
                except ValueError:
                    pass

    return Stackup(
        layers=copper_layers,
        total_thickness_mm=total_thickness,
        copper_layer_count=len(copper_layers),
    )


def _parse_bounds(root: list) -> BoardBounds:
    """Extract board outline from Edge.Cuts shapes."""
    xs: list[float] = []
    ys: list[float] = []

    def _add_xy(node: list, tag: str) -> None:
        sub = find(node, tag)
        if sub and len(sub) >= 3:
            try:
                xs.append(float(sub[1]))
                ys.append(float(sub[2]))
            except (ValueError, IndexError):
                pass

    for shape_type in ("gr_rect", "gr_line", "gr_arc", "gr_poly"):
        for node in find_all(root, shape_type):
            layer_node = find(node, "layer")
            if not layer_node or len(layer_node) < 2:
                continue
            if layer_node[1] != "Edge.Cuts":
                continue
            _add_xy(node, "start")
            _add_xy(node, "end")
            _add_xy(node, "mid")

    if not xs or not ys:
        return BoardBounds(0.0, 0.0, 0.0, 0.0)

    return BoardBounds(
        x_min=min(xs), y_min=min(ys), x_max=max(xs), y_max=max(ys)
    )


def _parse_footprints(root: list, net_map: dict[str, str]) -> list[Footprint]:
    footprints: list[Footprint] = []
    for fp_node in find_all(root, "footprint"):
        if not fp_node or len(fp_node) < 2:
            continue
        lib_id = fp_node[1] if isinstance(fp_node[1], str) else ""

        at_node = find(fp_node, "at")
        if at_node and len(at_node) >= 3:
            try:
                px, py = float(at_node[1]), float(at_node[2])
                rotation = float(at_node[3]) if len(at_node) > 3 else 0.0
            except (ValueError, IndexError):
                px, py, rotation = 0.0, 0.0, 0.0
        else:
            px, py, rotation = 0.0, 0.0, 0.0

        layer_node = find(fp_node, "layer")
        layer = layer_node[1] if layer_node and len(layer_node) > 1 else ""

        props = _fp_properties(fp_node)
        reference = props.get("Reference", "")
        value = props.get("Value", "")

        path_node = find(fp_node, "path")
        sch_path = path_node[1] if path_node and len(path_node) > 1 else ""
        sheetfile_node = find(fp_node, "sheetfile")
        sch_sheet_file = sheetfile_node[1] if sheetfile_node and len(sheetfile_node) > 1 else ""

        pads: list[Pad] = []
        for pad_node in find_all(fp_node, "pad"):
            pad_num = pad_node[1] if len(pad_node) > 1 and isinstance(pad_node[1], str) else ""
            # pad net — may be (net "name") or (net N)
            net_node = find(pad_node, "net")
            if net_node and len(net_node) >= 2:
                raw_net = str(net_node[1])
                net_name = _resolve_net(raw_net, net_map)
            else:
                net_name = ""
            pintype_node = find(pad_node, "pintype")
            pintype = pintype_node[1] if pintype_node and len(pintype_node) > 1 else "passive"

            # Absolute pad position = fp position (pad offsets ignored for simplicity)
            pad_at = find(pad_node, "at")
            if pad_at and len(pad_at) >= 3:
                try:
                    pad_dx, pad_dy = float(pad_at[1]), float(pad_at[2])
                    # Rotate pad offset by footprint rotation
                    rad = math.radians(rotation)
                    abs_x = px + pad_dx * math.cos(rad) - pad_dy * math.sin(rad)
                    abs_y = py + pad_dx * math.sin(rad) + pad_dy * math.cos(rad)
                except (ValueError, IndexError):
                    abs_x, abs_y = px, py
            else:
                abs_x, abs_y = px, py

            pads.append(Pad(
                number=pad_num,
                net_name=net_name,
                pintype=pintype,
                position=(abs_x, abs_y),
            ))

        footprints.append(Footprint(
            reference=reference,
            value=value,
            lib_id=lib_id,
            position=(px, py),
            rotation=rotation,
            layer=layer,
            sch_path=sch_path,
            sch_sheet_file=sch_sheet_file,
            pads=pads,
        ))
    return footprints


def _fp_properties(fp_node: list) -> dict[str, str]:
    props: dict[str, str] = {}
    for prop in find_all(fp_node, "property"):
        if len(prop) >= 3 and isinstance(prop[1], str) and isinstance(prop[2], str):
            props[prop[1]] = prop[2]
    return props


def _parse_segments(root: list, net_map: dict[str, str]) -> list[Segment]:
    segments: list[Segment] = []
    for seg in find_all(root, "segment"):
        start_node = find(seg, "start")
        end_node = find(seg, "end")
        width_node = find(seg, "width")
        layer_node = find(seg, "layer")
        net_node = find(seg, "net")

        try:
            sx, sy = float(start_node[1]), float(start_node[2])
            ex, ey = float(end_node[1]), float(end_node[2])
        except (TypeError, ValueError, IndexError):
            continue

        width = 0.0
        if width_node and len(width_node) > 1:
            try:
                width = float(width_node[1])
            except ValueError:
                pass

        layer = layer_node[1] if layer_node and len(layer_node) > 1 else ""
        net_name = ""
        if net_node and len(net_node) > 1:
            net_name = _resolve_net(str(net_node[1]), net_map)

        segments.append(Segment(
            net_name=net_name,
            width_mm=width,
            layer=layer,
            start=(sx, sy),
            end=(ex, ey),
        ))
    return segments


def _parse_vias(root: list, net_map: dict[str, str]) -> list[Via]:
    vias: list[Via] = []
    for via_node in find_all(root, "via"):
        at_node = find(via_node, "at")
        if not at_node or len(at_node) < 3:
            continue
        try:
            vx, vy = float(at_node[1]), float(at_node[2])
        except (ValueError, IndexError):
            continue

        size_node = find(via_node, "size")
        size = float(size_node[1]) if size_node and len(size_node) > 1 else 0.0
        drill_node = find(via_node, "drill")
        drill = float(drill_node[1]) if drill_node and len(drill_node) > 1 else 0.0

        layers_node = find(via_node, "layers")
        via_layers: list[str] = []
        if layers_node:
            via_layers = [x for x in layers_node[1:] if isinstance(x, str)]

        net_node = find(via_node, "net")
        net_name = ""
        if net_node and len(net_node) > 1:
            net_name = _resolve_net(str(net_node[1]), net_map)

        vias.append(Via(
            net_name=net_name,
            position=(vx, vy),
            size_mm=size,
            drill_mm=drill,
            layers=via_layers,
        ))
    return vias


def _parse_polygon_pts(container: list) -> list[tuple[float, float]]:
    """Extract [(x, y), ...] from a (pts (xy X Y) ...) sub-tree."""
    pts_node = find(container, "pts")
    if not pts_node:
        return []
    coords: list[tuple[float, float]] = []
    for xy in find_all(pts_node, "xy"):
        try:
            coords.append((float(xy[1]), float(xy[2])))
        except (IndexError, ValueError):
            pass
    return coords


def _parse_zones(root: list, net_map: dict[str, str]) -> list[Zone]:
    zones: list[Zone] = []
    for zone_node in find_all(root, "zone"):
        net_name_node = find(zone_node, "net_name")
        if net_name_node and len(net_name_node) > 1:
            net_name = str(net_name_node[1])
        else:
            net_node = find(zone_node, "net")
            net_name = ""
            if net_node and len(net_node) > 1:
                net_name = _resolve_net(str(net_node[1]), net_map)

        layer_node = find(zone_node, "layer")
        layer = layer_node[1] if layer_node and len(layer_node) > 1 else ""

        # Design-rule outline polygon (always present in a zone definition)
        outline: list[tuple[float, float]] = []
        poly_node = find(zone_node, "polygon")
        if poly_node:
            outline = _parse_polygon_pts(poly_node)

        # Filled copper — KiCad writes (filled_polygon (layer "...") (pts ...))
        # after zone fill. A zone may contain multiple filled_polygon blocks
        # (one per island). We take the largest (most vertices) for the primary
        # cut-cell geometry; additional islands are ignored for now.
        filled: list[tuple[float, float]] = []
        for fp_node in find_all(zone_node, "filled_polygon"):
            fp_layer_node = find(fp_node, "layer")
            fp_layer = fp_layer_node[1] if fp_layer_node and len(fp_layer_node) > 1 else ""
            if fp_layer and fp_layer != layer:
                continue
            pts = _parse_polygon_pts(fp_node)
            if len(pts) > len(filled):
                filled = pts

        zones.append(Zone(
            net_name=net_name,
            layer=layer,
            filled_polygon=filled,
            outline_polygon=outline,
        ))
    return zones
