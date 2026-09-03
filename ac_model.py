"""Build frequency-domain PDN models from the existing Ki-PIDA mesh pipeline."""

from collections import defaultdict
from dataclasses import dataclass, field
import math
import re
from typing import Dict, List, Optional, Tuple

try:
    from .extractor import GeometryExtractor
    from .mesh import Mesher
    from .models import ACAnalysisSettings, CapacitorModel, MeshBranch, PowerRail
except (ImportError, ValueError):
    from extractor import GeometryExtractor
    from mesh import Mesher
    from models import ACAnalysisSettings, CapacitorModel, MeshBranch, PowerRail


@dataclass
class ACNodeConnection:
    rail_nodes: List[int] = field(default_factory=list)
    ground_nodes: List[int] = field(default_factory=list)


@dataclass
class ACNetwork:
    node_count: int
    branches: List[MeshBranch]
    source: ACNodeConnection
    measurement: ACNodeConnection
    capacitor_nodes: Dict[str, ACNodeConnection]
    node_coords: Dict[int, Tuple[float, float, int]] = field(default_factory=dict)
    requested_grid_size_mm: float = 0.0
    effective_grid_size_mm: float = 0.0
    # Every candidate observation point, keyed by reference designator.
    # ``measurement`` remains one of these and stays the single-port contract
    # the existing UI and result adapter consume.
    ports: Dict[str, ACNodeConnection] = field(default_factory=dict)
    source_ref_des: str = ""
    source_resolution: str = ""


def parse_capacitance(value) -> Optional[float]:
    """Parse common KiCad capacitor values such as 100n, 4u7 and 10uF."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None

    text = str(value).strip().replace("µ", "u").replace("μ", "u")
    # Match a complete capacitor value, optionally followed by a voltage
    # rating.  Searching inside arbitrary MPNs made IC values such as
    # TPS7A0233 look like plausible capacitances.
    match = re.fullmatch(
        r"(?i)(\d+(?:\.\d+)?)([pnum]?)(\d*)\s*f?"
        r"(?:\s*(?:[/,]\s*)?\d+(?:\.\d+)?\s*v)?",
        text,
    )
    if not match:
        return None

    major, prefix, fractional = match.groups()
    if fractional and "." not in major:
        major = f"{major}.{fractional}"

    scale = {"": 1.0, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3}
    try:
        capacitance = float(major) * scale[prefix.lower()]
    except (ValueError, KeyError):
        return None
    return capacitance if capacitance > 0 else None


def format_capacitance(value_f: float) -> str:
    for scale, suffix in ((1e-3, "mF"), (1e-6, "uF"), (1e-9, "nF"), (1e-12, "pF")):
        if value_f >= scale:
            return f"{value_f / scale:g}{suffix}"
    return f"{value_f:g}F"


class _PlanarNodeIndex:
    """Bucketed (x, y) index over mesh nodes for nearest-neighbour lookups.

    A ground plane mesh routinely holds tens of thousands of nodes, so the
    naive scan a port would otherwise do per rail node is O(N*M) and dominates
    model build time. Bucketing by a cell close to the grid pitch keeps roughly
    one node per bucket, so a lookup touches a handful of cells.
    """

    def __init__(self, node_coords: Dict[int, Tuple[float, float, int]], cell_mm: float = 0.0):
        self._buckets = defaultdict(list)
        cell = float(cell_mm or 0.0)
        if cell <= 0.0:
            cell = self._auto_cell(node_coords)
        self._cell = cell
        for node_id, coord in node_coords.items():
            x, y = float(coord[0]), float(coord[1])
            self._buckets[(int(math.floor(x / cell)), int(math.floor(y / cell)))].append(
                (x, y, int(node_id))
            )

    @staticmethod
    def _auto_cell(node_coords) -> float:
        """Pick a cell size giving roughly one node per bucket."""
        if not node_coords:
            return 1.0
        xs = [float(coord[0]) for coord in node_coords.values()]
        ys = [float(coord[1]) for coord in node_coords.values()]
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        if span <= 0.0:
            return 1.0
        return max(span / max(math.sqrt(len(node_coords)), 1.0), 1e-6)

    def nearest(self, x: float, y: float, count: int = 1, max_distance_mm=None):
        """Return up to *count* node ids closest to (x, y), nearest first."""
        if not self._buckets or count <= 0:
            return []
        cell = self._cell
        cx, cy = int(math.floor(x / cell)), int(math.floor(y / cell))
        found = []
        ring = 0
        # Expand ring by ring. A node in ring r+1 can still beat a corner hit
        # in ring r, so once enough candidates exist keep going until the ring's
        # guaranteed minimum distance exceeds the worst kept candidate.
        max_rings = max(len(self._buckets), 1)
        while ring <= max_rings:
            for bx in range(cx - ring, cx + ring + 1):
                for by in range(cy - ring, cy + ring + 1):
                    if ring and abs(bx - cx) != ring and abs(by - cy) != ring:
                        continue
                    for px, py, node_id in self._buckets.get((bx, by), ()):
                        distance = math.hypot(px - x, py - y)
                        if max_distance_mm is not None and distance > max_distance_mm:
                            continue
                        found.append((distance, node_id))
            if len(found) >= count:
                found.sort()
                # Anything beyond this ring is at least ring*cell away.
                if found[min(count, len(found)) - 1][0] <= ring * cell:
                    break
            ring += 1
        found.sort()
        return [node_id for _distance, node_id in found[:count]]


def nearest_ground_nodes(
    ground_mesh, rail_mesh, rail_nodes, *, max_distance_mm=None, per_rail_node=1,
):
    """Ground-side nodes for a port whose component carries no return pads.

    A switching regulator reaches its rail through an output inductor with no
    ground pad, so requiring the return on the same component makes every such
    rail unanalysable. The physical return is the plane under the injection
    point, so pick the ground node(s) closest in (x, y) to each rail node,
    across all layers.

    Returns ground-mesh node ids (NOT yet offset into the combined network).
    """
    ground_coords = getattr(ground_mesh, "node_coords", None) or {}
    rail_coords = getattr(rail_mesh, "node_coords", None) or {}
    if not ground_coords or not rail_nodes:
        return []

    index = _PlanarNodeIndex(ground_coords, getattr(ground_mesh, "grid_step", 0.0) or 0.0)
    resolved = []
    seen = set()
    for rail_node in rail_nodes:
        coord = rail_coords.get(int(rail_node))
        if coord is None:
            continue
        for node_id in index.nearest(
            float(coord[0]), float(coord[1]),
            count=max(1, int(per_rail_node)), max_distance_mm=max_distance_mm,
        ):
            if node_id not in seen:
                seen.add(node_id)
                resolved.append(node_id)
    return sorted(resolved)


class ACModelBuilder:
    """Adapter between KiCad board objects and the pure numerical AC solver."""

    def __init__(self, board, debug=False, log_callback=None):
        self.board = board
        self.debug = debug
        self.log_callback = log_callback

    def _log(self, message):
        if self.log_callback:
            self.log_callback(f"[AC MODEL] {message}")

    def _get_val(self, obj, attr_name, default=None):
        if obj is None:
            return default
        if hasattr(obj, attr_name):
            value = getattr(obj, attr_name)
            if value is not None and not callable(value):
                return value
        for method_name in (f"get_{attr_name}", attr_name):
            if hasattr(obj, method_name):
                try:
                    value = getattr(obj, method_name)()
                    if value is not None:
                        return value
                except Exception:
                    pass
        return default

    def _get_board_items(self, attr_name):
        value = self._get_val(self.board, attr_name, [])
        return value if value is not None else []

    def _get_footprint_reference(self, footprint):
        reference = self._get_val(footprint, "reference", self._get_val(footprint, "ref_des", ""))
        if reference:
            return str(reference)
        field = self._get_val(footprint, "reference_field")
        text = self._get_val(field, "text")
        return str(self._get_val(text, "value", "") or "")

    def _get_footprint_value(self, footprint):
        value = self._get_val(footprint, "value", "")
        if value:
            return str(value)
        field = self._get_val(footprint, "value_field")
        text = self._get_val(field, "text")
        return str(self._get_val(text, "value", "") or "")

    def _get_footprint_name(self, footprint):
        for attr in ("footprint_name", "library_link", "lib_id", "name"):
            value = self._get_val(footprint, attr, "")
            if value:
                return str(value)
        definition = self._get_val(footprint, "definition")
        return str(self._get_val(definition, "name", "") or "")

    def _get_pads(self, footprint):
        pads = self._get_val(footprint, "pads")
        if pads is None:
            definition = self._get_val(footprint, "definition")
            pads = self._get_val(definition, "pads", [])
        return pads or []

    def _pad_number(self, pad):
        return str(self._get_val(pad, "number", self._get_val(pad, "name", "")) or "")

    def _pad_net_name(self, pad):
        return str(self._get_val(self._get_val(pad, "net"), "name", "") or "")

    def _is_dnp(self, footprint):
        for attr in ("dnp", "do_not_populate", "exclude_from_bom", "not_in_bom"):
            if bool(self._get_val(footprint, attr, False)):
                return True
        attrs = str(self._get_val(footprint, "attributes", "")).lower()
        return "dnp" in attrs or "do_not_populate" in attrs

    def discover_ground_nets(self):
        names = set()
        for footprint in self._get_board_items("footprints"):
            for pad in self._get_pads(footprint):
                name = self._pad_net_name(pad)
                if re.search(r"(?i)(^|[/_+\-])(gnd|vss|ground)(\d*|$)", name) or name.upper() in {
                    "GND", "AGND", "DGND", "PGND", "VSS"
                }:
                    names.add(name)
        return sorted(names)

    def _default_esl(self, footprint_name):
        name = footprint_name.lower()
        for package, esl in (("0201", 0.25e-9), ("0402", 0.4e-9), ("0603", 0.6e-9),
                             ("0805", 0.8e-9), ("1206", 1.0e-9), ("1210", 1.2e-9)):
            if package in name:
                return esl
        return 0.8e-9

    def discover_capacitors(self, rail_name, ground_name):
        capacitors = []
        for footprint in self._get_board_items("footprints"):
            reference = self._get_footprint_reference(footprint)
            # KiCad reference designators are the authoritative component
            # class here.  Numeric fragments inside IC/connector values must
            # never create synthetic capacitor models.
            if not re.match(r"(?i)^C\d", reference):
                continue
            value_text = self._get_footprint_value(footprint)
            capacitance = parse_capacitance(value_text)

            rail_pads = []
            ground_pads = []
            for pad in self._get_pads(footprint):
                net_name = self._pad_net_name(pad)
                if net_name == rail_name:
                    rail_pads.append(self._pad_number(pad))
                elif net_name == ground_name:
                    ground_pads.append(self._pad_number(pad))

            if not rail_pads or not ground_pads:
                continue

            candidate = self._is_dnp(footprint) or capacitance is None
            capacitors.append(CapacitorModel(
                ref_des=reference,
                rail_pad_names=rail_pads,
                ground_pad_names=ground_pads,
                capacitance_f=capacitance or 100e-9,
                esr_ohm=0.01,
                esl_h=self._default_esl(self._get_footprint_name(footprint)),
                enabled=not candidate,
                candidate=candidate,
                model_source="value-and-package-estimate",
            ))

        capacitors.sort(key=lambda cap: cap.ref_des)
        return capacitors

    def _find_footprint(self, ref_des):
        for footprint in self._get_board_items("footprints"):
            if self._get_footprint_reference(footprint) == ref_des:
                return footprint
        return None

    def pad_names_for_net(self, ref_des, net_name):
        footprint = self._find_footprint(ref_des)
        if not footprint:
            return []
        return [self._pad_number(pad) for pad in self._get_pads(footprint)
                if self._pad_net_name(pad) == net_name]

    def find_pad_nodes(self, mesh, ref_des, pad_names, net_name):
        footprint = self._find_footprint(ref_des)
        if not footprint:
            return []

        wanted = set(str(name) for name in (pad_names or []))
        found = []
        for pad in self._get_pads(footprint):
            if self._pad_net_name(pad) != net_name:
                continue
            if wanted and self._pad_number(pad) not in wanted:
                continue

            position = self._get_val(pad, "position")
            if position is None:
                continue
            x = float(self._get_val(position, "x", 0.0) or 0.0)
            y = float(self._get_val(position, "y", 0.0) or 0.0)
            if abs(x) > 10000 or abs(y) > 10000:
                x /= 1e6
                y /= 1e6

            ix = int(round((x - mesh.grid_origin[0]) / mesh.grid_step))
            iy = int(round((y - mesh.grid_origin[1]) / mesh.grid_step))
            layers = self._get_val(pad, "layers") or []
            if not layers:
                padstack = self._get_val(pad, "padstack")
                layers = self._get_val(padstack, "layers", []) or []
            if not layers:
                layers = sorted({coords[2] for coords in mesh.node_coords.values()})

            pad_nodes = []
            for radius in range(0, 3):
                for dx in range(-radius, radius + 1):
                    for dy in range(-radius, radius + 1):
                        if radius and abs(dx) < radius and abs(dy) < radius:
                            continue
                        for layer in layers:
                            node_id = mesh.node_map.get((ix + dx, iy + dy, layer))
                            if node_id is not None:
                                pad_nodes.append(int(node_id))
                if pad_nodes:
                    break
            found.extend(pad_nodes)

        return sorted(set(found))

    def components_on_net(self, net_name):
        """Every (ref_des, pad_numbers) pair touching *net_name*, board order."""
        found = []
        for footprint in self._get_board_items("footprints"):
            reference = self._get_footprint_reference(footprint)
            if not reference:
                continue
            pads = [self._pad_number(pad) for pad in self._get_pads(footprint)
                    if self._pad_net_name(pad) == net_name]
            if pads:
                found.append((reference, pads))
        return found

    @staticmethod
    def _source_preference(ref_des):
        """Rank a fallback source candidate. Lower sorts first.

        An explicit heuristic, not a measurement: a rail is normally reached
        through its regulator's output inductor or ferrite, so L/FB references
        are the best guess for where energy enters the rail, then active parts,
        then anything else. Recorded here so the ordering is reviewable rather
        than buried in a sort key.
        """
        name = str(ref_des).upper()
        if name.startswith("FB") or name.startswith("L"):
            return 0
        if name.startswith("U"):
            return 1
        return 2

    def resolve_source(self, rail, settings, all_rails=None):
        """Pick the component injecting energy into the rail.

        Order: the explicit setting, a declared UnifiedSource, the output of a
        regulator feeding this rail, then a heuristic pick among components on
        the rail. Returns (ref_des, rail_pad_names, how) where *how* records
        which rule fired, for logging and for the failure message.
        """
        if settings.source.ref_des:
            return settings.source.ref_des, list(settings.source.rail_pad_names), "explicit"
        if rail is not None and rail.sources:
            source = rail.sources[0]
            return source.component_ref.ref_des, list(source.pad_names), "power-tree source"

        # PowerRail.child_regulators lists regulators this rail *feeds*, so the
        # one that produces this rail lives on some other rail's list.
        for candidate_rail in (all_rails or ([rail] if rail is not None else [])):
            for regulator in getattr(candidate_rail, "child_regulators", ()) or ():
                if getattr(regulator, "output_rail_name", "") == settings.rail_name:
                    return (
                        regulator.output_ref_des,
                        list(regulator.output_pad_names or []),
                        f"regulator '{getattr(regulator, 'name', '')}' output",
                    )

        candidates = self.components_on_net(settings.rail_name)
        if candidates:
            reference, pads = min(
                candidates, key=lambda item: (self._source_preference(item[0]), item[0]),
            )
            return reference, list(pads), "heuristic pick among rail components"
        return "", [], "unresolved"

    def resolve_ports(self, rail, settings):
        """Every candidate measurement port, as (ref_des, rail_pad_names).

        An explicit port wins. Otherwise every declared load is a port: the
        PDN is qualified by its worst observation point, and which point that
        is cannot be known before solving.
        """
        if settings.measurement_port.ref_des:
            return [(
                settings.measurement_port.ref_des,
                list(settings.measurement_port.rail_pad_names),
            )]
        ports = []
        for load in (getattr(rail, "loads", ()) or ()):
            ports.append((load.component_ref.ref_des, list(load.pad_names)))
        return ports

    def _connection(
        self, rail_mesh, ground_mesh, offset, ref_des,
        rail_pad_names, ground_pad_names, settings, label="port",
    ):
        """Map one port to rail and return nodes, falling back to the plane.

        Ground resolution order: the pads named on the component, then the
        nearest ground-mesh nodes under the rail pads. The second path is what
        makes a regulator output inductor -- which has no ground pad at all --
        usable as a port.
        """
        rail_nodes = self.find_pad_nodes(
            rail_mesh, ref_des, rail_pad_names, settings.rail_name,
        )
        ground_nodes = self.find_pad_nodes(
            ground_mesh, ref_des, ground_pad_names, settings.ground_net_name,
        )
        if not ground_nodes and rail_nodes:
            ground_nodes = nearest_ground_nodes(ground_mesh, rail_mesh, rail_nodes)
            if ground_nodes:
                self._log(
                    f"{label} '{ref_des}' has no {settings.ground_net_name} pad; "
                    f"using the {len(ground_nodes)} nearest return-plane node(s) "
                    "under its rail pads as the AC return."
                )
        return ACNodeConnection(
            rail_nodes=rail_nodes,
            ground_nodes=[node + offset for node in ground_nodes],
        )

    def _unmapped_message(self, label, ref_des, settings):
        """Explain a failed port mapping in terms the user can act on."""
        if not ref_des:
            return (
                f"No {label} could be resolved automatically for rail "
                f"'{settings.rail_name}'. Declare a source or a load on the rail in "
                "the Power Tree, or pick a component explicitly in the AC Impedance tab."
            )
        available = self.pad_names_for_net(ref_des, settings.rail_name)
        if not self._find_footprint(ref_des):
            return (
                f"The {label} component '{ref_des}' was not found on the board."
            )
        if not available:
            return (
                f"The {label} component '{ref_des}' has no pad on rail "
                f"'{settings.rail_name}'. Pick a component that connects to the rail."
            )
        return (
            f"The {label} pads of '{ref_des}' on '{settings.rail_name}' "
            f"(pads {', '.join(available)}) could not be matched to the rail mesh. "
            "Try a finer AC mesh resolution."
        )

    def _translate_mesh(self, mesh, offset):
        branches = [MeshBranch(
            node_a=branch.node_a + offset,
            node_b=branch.node_b + offset,
            resistance_ohm=branch.resistance_ohm,
            inductance_h=branch.inductance_h,
            kind=branch.kind,
            cross_section_mm2=branch.cross_section_mm2,
            geometry_source=branch.geometry_source,
        ) for branch in mesh.branches]
        coords = {node_id + offset: coord for node_id, coord in mesh.node_coords.items()}
        return branches, coords

    def build(self, rail: PowerRail, settings: ACAnalysisSettings, grid_size_mm=0.5,
              all_rails=None):
        extractor = GeometryExtractor(self.board, debug=self.debug, log_callback=self.log_callback)
        stackup = extractor.get_board_stackup()
        rail_geometry = extractor.get_net_geometry(settings.rail_name)
        ground_geometry = extractor.get_net_geometry(settings.ground_net_name)
        if not rail_geometry:
            raise ValueError(f"No copper geometry found for rail '{settings.rail_name}'.")
        if not ground_geometry:
            raise ValueError(f"No copper geometry found for return net '{settings.ground_net_name}'.")

        mesher = Mesher(self.board, debug=self.debug, log_callback=self.log_callback)
        rail_mesh = mesher.generate_mesh(settings.rail_name, rail_geometry, stackup, grid_size_mm)
        ground_mesh = mesher.generate_mesh(settings.ground_net_name, ground_geometry, stackup, grid_size_mm)
        if not rail_mesh.nodes or not ground_mesh.nodes:
            raise ValueError("The AC rail-to-ground mesh is empty.")

        offset = len(rail_mesh.nodes)
        rail_branches, rail_coords = self._translate_mesh(rail_mesh, 0)
        ground_branches, ground_coords = self._translate_mesh(ground_mesh, offset)

        source_ref, source_rail_pads, source_rule = self.resolve_source(
            rail, settings, all_rails=all_rails,
        )
        if source_rule != "explicit":
            self._log(f"AC source resolved to '{source_ref or '(none)'}' via {source_rule}.")
        source_ground_pads = settings.source.ground_pad_names or self.pad_names_for_net(
            source_ref, settings.ground_net_name
        )

        candidate_ports = self.resolve_ports(rail, settings)
        port_ref, port_rail_pads = candidate_ports[0] if candidate_ports else ("", [])
        port_ground_pads = settings.measurement_port.ground_pad_names or self.pad_names_for_net(
            port_ref, settings.ground_net_name
        )

        source = self._connection(
            rail_mesh, ground_mesh, offset, source_ref,
            source_rail_pads, source_ground_pads, settings, label="AC source",
        )
        measurement = self._connection(
            rail_mesh, ground_mesh, offset, port_ref,
            port_rail_pads, port_ground_pads, settings, label="measurement port",
        )
        if not source.rail_nodes:
            raise ValueError(self._unmapped_message("AC source", source_ref, settings))
        if not source.ground_nodes:
            raise ValueError(
                f"No return-net copper was found beneath the AC source pads on "
                f"'{settings.ground_net_name}'."
            )
        if not measurement.rail_nodes:
            raise ValueError(self._unmapped_message("measurement port", port_ref, settings))
        if not measurement.ground_nodes:
            raise ValueError(
                f"No return-net copper was found beneath the measurement port pads on "
                f"'{settings.ground_net_name}'."
            )

        capacitor_nodes = {}
        for capacitor in settings.capacitors:
            rail_nodes = self.find_pad_nodes(
                rail_mesh, capacitor.ref_des, capacitor.rail_pad_names, settings.rail_name
            )
            ground_nodes = [node + offset for node in self.find_pad_nodes(
                ground_mesh, capacitor.ref_des, capacitor.ground_pad_names, settings.ground_net_name
            )]
            if rail_nodes and ground_nodes:
                capacitor_nodes[capacitor.ref_des] = ACNodeConnection(rail_nodes, ground_nodes)
            else:
                self._log(f"Skipping {capacitor.ref_des}: pads could not be mapped to both meshes.")

        # Map every remaining candidate port so the solver can sweep them all.
        # The first is already mapped as `measurement`, which stays the
        # single-port contract the existing UI and adapter consume.
        ports = {}
        for candidate_ref, candidate_pads in candidate_ports:
            if candidate_ref in ports:
                continue
            connection = (
                measurement if candidate_ref == port_ref
                else self._connection(
                    rail_mesh, ground_mesh, offset, candidate_ref, candidate_pads,
                    self.pad_names_for_net(candidate_ref, settings.ground_net_name),
                    settings, label=f"port '{candidate_ref}'",
                )
            )
            if connection.rail_nodes and connection.ground_nodes:
                ports[candidate_ref] = connection
            else:
                self._log(
                    f"Skipping port '{candidate_ref}': pads could not be mapped to both meshes."
                )

        return ACNetwork(
            node_count=len(rail_mesh.nodes) + len(ground_mesh.nodes),
            branches=rail_branches + ground_branches,
            source=source,
            measurement=measurement,
            capacitor_nodes=capacitor_nodes,
            node_coords={**rail_coords, **ground_coords},
            requested_grid_size_mm=float(grid_size_mm),
            effective_grid_size_mm=max(
                float(rail_mesh.grid_step), float(ground_mesh.grid_step),
            ),
            ports=ports,
            source_ref_des=source_ref,
            source_resolution=source_rule,
        )
