"""Hybrid DC mesher: analytical track resistances + cut-cell zone conductances.

This module provides :class:`HybridMesher`, an alternative to the raster-based
``Mesher`` in ``mesh.py``.  It operates on offline-parsed
:class:`~ingest.board_reader.ParsedBoard` data (no live KiCad connection
required) and produces the same :class:`~mesh.Mesh` object consumed by
:class:`~solver.Solver`.

Key improvements over the legacy mesher
----------------------------------------
* **Track segments** — resistance is computed analytically as R = ρL/(w·t),
  exact to machine precision, instead of being approximated by a raster grid
  whose effective width equals the grid step.
* **Vias** — modelled by :func:`~ingest.track_resistance.via_resistance`, a
  thin-annulus barrel model, replacing the single-node snap used previously.
* **No ``safety_buffer``** — exact geometry, no polygon inflation.
* **Zone copper** — cut-cell: each lattice edge conducts through the copper
  fraction of the cell centred on it. A net's fills on a layer are meshed as
  one union, on a lattice aligned on multiples of the step and shared by
  every layer of the net.
* **Pad and via lands** — meshed as copper areas on the same lattice, so a
  track or pour spoke meets a pad wherever it touches it; a track is split
  where it runs across a land.

Node-coordinate convention
---------------------------
Segment endpoints are snapped to a 1 µm (0.001 mm) grid to allow shared
junction nodes when two segments meet at the same physical point.  Layer IDs
are taken from :attr:`~ingest.board_reader.CopperLayer.layer_id`; a synthetic
layer ID of -1 is used for vias when the board stackup is empty.
"""

from __future__ import annotations

import math
from typing import Callable, Optional

try:
    from .ingest.board_reader import ParsedBoard, Segment, Via, Zone, pad_copper_layers
    from .ingest.track_resistance import (
        RHO_COPPER,
        segment_resistance,
        via_resistance,
    )
except (ImportError, ValueError):
    from ingest.board_reader import ParsedBoard, Segment, Via, Zone, pad_copper_layers
    from ingest.track_resistance import (
        RHO_COPPER,
        segment_resistance,
        via_resistance,
    )

try:
    from .mesh import Mesh
    from .physical_constants import (
        DEFAULT_BOARD_THICKNESS_MM, DEFAULT_COPPER_THICKNESS_MM, PLATING_THICKNESS_FALLBACK_MM,
    )
except (ImportError, ValueError):
    from mesh import Mesh
    from physical_constants import (
        DEFAULT_BOARD_THICKNESS_MM, DEFAULT_COPPER_THICKNESS_MM, PLATING_THICKNESS_FALLBACK_MM,
    )

try:
    import numpy as np
    import shapely
    from shapely import Polygon as ShapelyPolygon
    from shapely import box as shapely_box, intersection as shapely_intersect, prepare as shapely_prepare
    _SHAPELY_OK = True
except ImportError:
    np = None  # type: ignore[assignment]
    _SHAPELY_OK = False

# Coordinate snap precision in mm (1 µm → 0.001 mm)
_SNAP = 0.001

# Barrel plating for a through-hole pad, mm. A via's plating is derived from
# its annular ring (size minus drill); a pad carries no equivalent pair in the
# parsed data, so the shared fallback -- the IPC-6012 Class 2 minimum -- is
# used. It is a floor, which keeps the pad's modelled resistance on the
# pessimistic side rather than flattering a rail with plating nobody promised.
PAD_PLATING_MM = PLATING_THICKNESS_FALLBACK_MM


def _barrel_section_mm2(drill_mm: float, plating_mm: float) -> float:
    """Wall section of a plated barrel whose finished hole is *drill_mm*.

    The same annulus :func:`~ingest.track_resistance.via_resistance` conducts
    through, so a density computed from it matches the resistance used.
    """
    return math.pi * (drill_mm * plating_mm + plating_mm * plating_mm)


def _pad_outline(pad):
    """A pad's copper outline in board coordinates, or ``None``.

    Round pads are circles and ovals stadiums; every other shape is taken as
    its bounding rectangle -- exact for "rect", a corner-sized overestimate for
    "roundrect", and the anchor size for "custom", whose primitives are not
    parsed. A pad drilled without plating carries no copper.
    """
    if not _SHAPELY_OK:
        return None
    width, height = (tuple(getattr(pad, "size_mm", ()) or ()) + (0.0, 0.0))[:2]
    if width <= 0.0 or height <= 0.0 or str(getattr(pad, "pad_type", "")) == "np_thru_hole":
        return None
    cx, cy = pad.position
    shape = str(getattr(pad, "shape", "") or "")
    if shape == "circle":
        return shapely.Point(cx, cy).buffer(width / 2.0, quad_segs=8)
    rad = math.radians(float(getattr(pad, "angle_deg", 0.0) or 0.0))
    cos_a, sin_a = math.cos(rad), math.sin(rad)

    def board_point(lx, ly):
        # KiCad's Y axis points down: a positive angle turns local +X to -Y.
        return (cx + lx * cos_a + ly * sin_a, cy - lx * sin_a + ly * cos_a)

    if shape == "oval" and abs(width - height) > 1e-9:
        half = abs(width - height) / 2.0
        ends = (
            [board_point(-half, 0.0), board_point(half, 0.0)] if width > height
            else [board_point(0.0, -half), board_point(0.0, half)]
        )
        return shapely.LineString(ends).buffer(min(width, height) / 2.0, quad_segs=8)
    if shape == "oval":
        return shapely.Point(cx, cy).buffer(width / 2.0, quad_segs=8)
    return ShapelyPolygon([
        board_point(-width / 2.0, -height / 2.0), board_point(width / 2.0, -height / 2.0),
        board_point(width / 2.0, height / 2.0), board_point(-width / 2.0, height / 2.0),
    ])


def _snap(v: float) -> float:
    """Round a coordinate to the nearest _SNAP mm to allow junction sharing."""
    return round(v / _SNAP) * _SNAP


def _layer_id_for_name(parsed_board: ParsedBoard, name: str) -> int:
    """Return the integer layer_id for a named copper layer, or -1 if unknown."""
    for cl in parsed_board.stackup.layers:
        if cl.name == name:
            return cl.layer_id
    return -1


def _thickness_for_layer(parsed_board: ParsedBoard, name: str) -> float:
    """Return copper thickness in mm for a named layer, or the shared default."""
    for cl in parsed_board.stackup.layers:
        if cl.name == name:
            return cl.thickness_mm if cl.thickness_mm > 0 else DEFAULT_COPPER_THICKNESS_MM
    return DEFAULT_COPPER_THICKNESS_MM


class HybridMesher:
    """Build a :class:`~mesh.Mesh` from a :class:`~ingest.board_reader.ParsedBoard`.

    Parameters
    ----------
    parsed_board:
        Board geometry obtained from :func:`~ingest.board_reader.read_board`.
    grid_step_mm:
        Cell size for zone cut-cell meshing (future use; currently zones are
        not meshed — see module docstring).
    log_callback:
        Optional callable receiving ``str`` log messages.
    rho:
        Copper resistivity in Ω·m (default: 1.72 × 10⁻⁸ at 20 °C).
    """

    DEFAULT_GRID_STEP_MM: float = 0.1
    MAX_ZONE_NODES: int = 400_000

    def __init__(
        self,
        parsed_board: ParsedBoard,
        grid_step_mm: float = DEFAULT_GRID_STEP_MM,
        log_callback: Optional[Callable[[str], None]] = None,
        rho: float = RHO_COPPER,
    ) -> None:
        self._board = parsed_board
        self._grid_step = grid_step_mm
        self._log_cb = log_callback
        self._rho = rho
        # The zone mesher may coarsen the grid to fit its node budget, so
        # tracks must attach against the step actually used, not the one asked
        # for. Seeded here so a board with no zones still has a sane value.
        self._effective_grid_step = grid_step_mm

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_mesh(self, net_name: str) -> Mesh:
        """Build and return a :class:`~mesh.Mesh` for *net_name*.

        Parameters
        ----------
        net_name:
            The PCB net name to mesh (e.g. ``"+5V_RAIL"``).

        Returns
        -------
        Mesh
            Ready for :class:`~solver.Solver`.
        """
        mesh = Mesh()
        # A net without pours must not inherit the step a previous net's pours
        # were coarsened to.
        self._effective_grid_step = self._grid_step
        node_registry: dict[tuple[float, float, int], int] = {}
        node_counter = [0]
        # Zone nodes indexed by their grid cell, so a track landing on a plane
        # can find the plane node instead of minting a private one. Populated
        # while the zones are meshed, consulted when tracks and vias are.
        zone_index: dict[tuple[int, int, int], int] = {}

        def get_or_create(x: float, y: float, layer_id: int) -> int:
            key = (_snap(x), _snap(y), layer_id)
            if key not in node_registry:
                nid = node_counter[0]
                node_counter[0] += 1
                node_registry[key] = nid
                mesh.nodes.append(nid)
                mesh.node_coords[nid] = (_snap(x), _snap(y), layer_id)
            return node_registry[key]

        def attach(x: float, y: float, layer_id: int) -> int:
            """Node for a track endpoint or via, joined to the plane it touches.

            Track endpoints snap to 1 um so two tracks meeting share a node.
            Zone nodes sit on the cut-cell grid, typically 0.1 mm. Those two
            lattices coincide only by accident, so every track was galvanically
            separate from every plane: one real rail split into 63,897
            connected components, and the loads on tracks could not be reached
            from a source on the pour.

            So a track endpoint first looks for a zone node in the surrounding
            grid cells on its own layer, and adopts the nearest one within half
            a cell. Only when the copper genuinely has no pour there does it
            create a node of its own.
            """
            if zone_index:
                step = self._effective_grid_step
                ix, iy = x / step, y / step
                best_id, best_distance = None, None
                for cx in (math.floor(ix), math.ceil(ix)):
                    for cy in (math.floor(iy), math.ceil(iy)):
                        found = zone_index.get((int(cx), int(cy), layer_id))
                        if found is None:
                            continue
                        zx, zy, _layer = mesh.node_coords[found]
                        distance = math.hypot(zx - x, zy - y)
                        if best_distance is None or distance < best_distance:
                            best_id, best_distance = found, distance
                # Half the cell *diagonal*, not half its side.
                #
                # Zone nodes sit on a square lattice of pitch `step`. A pad or
                # track endpoint falls at an arbitrary point inside a cell, and
                # the farthest such a point can be from the nearest lattice
                # node is at the cell centre: (step/2)*sqrt(2) ~ 0.707*step.
                # A threshold of 0.5*step therefore rejects legitimately
                # coincident copper in the corners of every cell -- roughly a
                # fifth of the cell area.
                #
                # It cost a real rail: J6 pad 3 on +3V3_MAIN sat 0.0559 mm from
                # the plane node it belongs to, against a 0.05 mm limit, and
                # missing by 6 um left the load unreachable from its source.
                if best_id is not None and best_distance <= 0.7072 * step:
                    return best_id
            return get_or_create(x, y, layer_id)

        def probe(x: float, y: float, layer_id: int):
            """Existing node at this point, or None -- never mints one.

            Pads use this before deciding to join. Creating a node for a pad
            that touches no modelled copper does not merely waste it: pads are
            also how loads and sources are located, and find_node_at picks the
            *nearest* node. A private node sitting exactly on the pad wins
            against the real network node a fraction of a millimetre away, so
            the anchor lands on an island and the rail becomes unsolvable.

            That regression was real -- meshing pads unconditionally moved
            +5V_RAIL's source Q3 onto three one-node components while the load
            sat in the 113,504-node network, turning a solvable rail into an
            unreachable one.
            """
            if zone_index:
                step = self._effective_grid_step
                ix, iy = x / step, y / step
                for cx in (math.floor(ix), math.ceil(ix)):
                    for cy in (math.floor(iy), math.ceil(iy)):
                        found = zone_index.get((int(cx), int(cy), layer_id))
                        if found is None:
                            continue
                        zx, zy, _layer = mesh.node_coords[found]
                        if math.hypot(zx - x, zy - y) <= 0.7072 * step:
                            return found
            return node_registry.get((_snap(x), _snap(y), layer_id))

        board = self._board

        # ------------------------------------------------------------------
        # 0. Zones first — they lay down the grid that tracks then attach to.
        #
        # Order matters here. Tracks can only adopt a plane node if the plane
        # nodes already exist, so meshing zones last left every track isolated.
        # ------------------------------------------------------------------
        lands = self._copper_lands(net_name, board)
        zone_count = self._mesh_zones(net_name, board, mesh, get_or_create, zone_index, lands)
        self._log(f"[HybridMesher] Net '{net_name}': {zone_count} zone and pad edges")
        land_trees = {
            layer: (shapely.STRtree(polygons), polygons)
            for layer, polygons in lands.items()
        }

        # ------------------------------------------------------------------
        # 1. Track segments — analytical resistance
        # ------------------------------------------------------------------
        seg_count = 0
        for seg in board.segments:
            if seg.net_name != net_name:
                continue
            x0, y0 = seg.start
            x1, y1 = seg.end
            if x0 == x1 and y0 == y1:
                # Zero-length degenerate segment — skip
                continue
            length_mm = math.hypot(x1 - x0, y1 - y0)
            if length_mm < 1e-9:
                continue
            thickness_mm = _thickness_for_layer(board, seg.layer)
            width_mm = seg.width_mm if seg.width_mm > 0 else 0.2
            try:
                R = segment_resistance(length_mm, width_mm, thickness_mm, self._rho)
            except ValueError:
                self._log(
                    f"Skipping degenerate segment on {seg.layer}: "
                    f"L={length_mm:.4f} W={width_mm:.4f} T={thickness_mm:.4f}"
                )
                continue
            layer_id = _layer_id_for_name(board, seg.layer)
            cs = width_mm * thickness_mm  # mm²
            # A track is joined to every pad or via land it runs across, not
            # only to those at its ends. On +3V3AON a B.Cu track from C5 runs
            # straight through the pads of C7 and C8 and stops beyond them;
            # meshed as one branch between its ends, it left both capacitors
            # on islands. The track is split where it crosses each land, and
            # the pieces' resistances add up to the whole track's.
            stations = [0.0, *self._land_crossings(seg, length_mm, land_trees), length_mm]
            added = False
            for start_mm, end_mm in zip(stations, stations[1:]):
                piece_mm = end_mm - start_mm
                if piece_mm <= _SNAP:
                    continue
                u = attach(x0 + (x1 - x0) * start_mm / length_mm,
                           y0 + (y1 - y0) * start_mm / length_mm, layer_id)
                v = attach(x0 + (x1 - x0) * end_mm / length_mm,
                           y0 + (y1 - y0) * end_mm / length_mm, layer_id)
                if u == v:
                    # Both ends adopted the same node: copper already joined.
                    continue
                mesh.add_edge_direct(
                    u, v, 1.0 / (R * piece_mm / length_mm),
                    kind="lateral",
                    cross_section_mm2=cs,
                    geometry_source=f"seg:{seg.layer}",
                )
                added = True
            if added:
                seg_count += 1

        self._log(f"[HybridMesher] Net '{net_name}': {seg_count} segment branches")

        # ------------------------------------------------------------------
        # 2. Vias — barrel analytical resistance
        # ------------------------------------------------------------------
        via_count = 0
        board_height_mm = board.stackup.total_thickness_mm
        if board_height_mm <= 0:
            board_height_mm = DEFAULT_BOARD_THICKNESS_MM

        for via in board.vias:
            if via.net_name != net_name:
                continue
            if len(via.layers) < 2:
                continue
            vx, vy = via.position
            drill_mm = via.drill_mm if via.drill_mm > 0 else 0.3
            # The board file does not record barrel plating. Half of size minus
            # drill is the annular ring -- the pad copper around the hole on
            # each layer -- and taking it for the barrel wall made a 0.6/0.3 mm
            # via's barrel 0.15 mm thick instead of about 0.02 mm, ten times too
            # conductive. The shared fallback is what the raster mesher uses.
            plating_mm = PLATING_THICKNESS_FALLBACK_MM

            top_layer = via.layers[0]
            bot_layer = via.layers[-1]
            lid_top = _layer_id_for_name(board, top_layer)
            lid_bot = _layer_id_for_name(board, bot_layer)
            if lid_top == lid_bot:
                continue  # single-layer via — unusual, skip

            try:
                R_via = via_resistance(board_height_mm, drill_mm, plating_mm, self._rho)
            except ValueError:
                continue

            # A through via touches every copper layer it passes, not just the
            # two it is named after. Modelling only the outer pair left the
            # rail's tracks on F.Cu/B.Cu unable to reach its plane on In2.Cu:
            # the barrel physically shorts them, the model did not, and the
            # loads were unreachable from the source.
            span = self._layers_spanned(board, top_layer, bot_layer)
            if len(span) < 2:
                continue
            # The barrel resistance is for the full board thickness; give each
            # inter-layer hop its share so the total across the stack is right.
            hops = len(span) - 1
            g = hops / R_via
            previous = attach(vx, vy, span[0][1])
            added = False
            for name, layer_id in span[1:]:
                node = attach(vx, vy, layer_id)
                if node != previous:
                    mesh.add_edge_direct(
                        previous, node, g,
                        kind="vertical",
                        cross_section_mm2=_barrel_section_mm2(drill_mm, plating_mm),
                        geometry_source=f"via:{top_layer}-{bot_layer}",
                    )
                    added = True
                previous = node
            if added:
                via_count += 1

        self._log(f"[HybridMesher] Net '{net_name}': {via_count} via branches")

        # ------------------------------------------------------------------
        # Pads.
        #
        # This mesher modelled segments, vias and zones, and never pads. A
        # through-hole pad is a vertical conductor exactly as a via is: it
        # shorts every copper layer it passes. Leaving it out meant a track
        # ending on one had no path to the plane beneath it, and the load on
        # that pad was unreachable from the source.
        #
        # That was not hypothetical. On the reference board it stranded loads
        # on four of seven rails, while the production rasterising mesher --
        # which does index pads -- reported no exclusions on the same copper.
        # +3V3_MAIN's load node sat at (106.610, 76.190), which is J6 pad 3,
        # `thru_hole` on `*.Cu`, 0.02 mm above a plane it could not reach.
        pad_count = 0
        for footprint in board.footprints:
            for pad in footprint.pads:
                if pad.net_name != net_name:
                    continue
                px, py = pad.position
                span = self._pad_layers(board, pad)
                if not span:
                    continue
                # Bind a pad only where it meets copper the mesh already has.
                # A pad floating free of every track and pour contributes no
                # conduction, and minting nodes for it actively harms the
                # result: those nodes then capture load and source lookups that
                # would otherwise have found the real network.
                if not any(probe(px, py, layer_id) is not None for _n, layer_id in span):
                    continue
                if len(span) < 2:
                    # A surface pad adds no vertical path; attaching it is what
                    # lets a load anchored on an SMD pad share the pour's node.
                    attach(px, py, span[0][1])
                    pad_count += 1
                    continue
                drill = float(pad.drill_mm or 0.0)
                if drill <= 0.0:
                    # No parsed hole means no defensible barrel resistance.
                    # Skipping is wrong too -- the pad really does conduct --
                    # so bind each layer without inventing a drill diameter,
                    # using the smallest hole KiCad will fabricate as a floor.
                    drill = 0.2
                try:
                    barrel = via_resistance(
                        board_height_mm, drill, PAD_PLATING_MM, self._rho,
                    )
                except ValueError:
                    continue
                hops = len(span) - 1
                conductance = hops / barrel
                previous = attach(px, py, span[0][1])
                added = False
                for _name, layer_id in span[1:]:
                    node = attach(px, py, layer_id)
                    if node != previous:
                        mesh.add_edge_direct(
                            previous, node, conductance,
                            kind="vertical",
                            cross_section_mm2=_barrel_section_mm2(drill, PAD_PLATING_MM),
                            geometry_source=f"pad:{footprint.reference}-{pad.number}",
                        )
                        added = True
                    previous = node
                if added:
                    pad_count += 1

        self._log(f"[HybridMesher] Net '{net_name}': {pad_count} pad connections")

        self._publish_grid(mesh)
        return mesh

    def _publish_grid(self, mesh: Mesh) -> None:
        """Fill the grid fields of the :class:`~mesh.Mesh` contract.

        Result readers report the step a solve ran at and draw density maps
        from ``node_map``. Pour nodes sit exactly on the lattice; a track or
        pad node takes the lattice slot it falls in when no pour node holds it,
        which places it within half a step for display only.
        """
        step = self._effective_grid_step
        mesh.grid_origin = (0.0, 0.0)
        mesh.grid_step = step
        mesh.requested_grid_step = self._grid_step
        mesh.adaptive_grid = step > self._grid_step * (1.0 + 1e-9)
        for node_id in sorted(mesh.node_coords):
            x, y, layer_id = mesh.node_coords[node_id]
            key = (int(round(x / step)), int(round(y / step)), layer_id)
            mesh.node_map.setdefault(key, node_id)

    @staticmethod
    def _land_crossings(seg: Segment, length_mm: float, land_trees: dict) -> list:
        """Distances along *seg* at which it runs across a land of its layer.

        One station per land crossed, at the middle of the stretch inside it,
        which lies within the land for every convex outline. Stations at the
        track's own ends are left to the ends.
        """
        entry = land_trees.get(seg.layer)
        if entry is None:
            return []
        tree, polygons = entry
        line = shapely.LineString([seg.start, seg.end])
        stations = set()
        for index in tree.query(line, predicate="intersects"):
            inside = line.intersection(polygons[int(index)])
            if inside.is_empty or inside.length <= 0.0:
                continue
            middle = inside.interpolate(0.5, normalized=True)
            station = line.project(middle)
            if _SNAP < station < length_mm - _SNAP:
                stations.add(round(station / _SNAP) * _SNAP)
        return sorted(stations)

    @staticmethod
    def _pad_layers(board: ParsedBoard, pad):
        """Copper layers a pad occupies, in stackup order.

        ``*.Cu`` is KiCad's shorthand for every copper layer and is what a
        through-hole pad carries; a surface pad names the single layer it sits
        on. Anything unrecognised yields nothing rather than a guess.
        """
        return [(cl.name, cl.layer_id) for cl in pad_copper_layers(board.stackup, pad)]

    @staticmethod
    def _layers_spanned(board: ParsedBoard, top_layer: str, bottom_layer: str):
        """Copper layers a via crosses, in stackup order, ends included.

        ``Via.layers`` names only the two ends. A through via drilled from
        F.Cu to B.Cu also passes every inner layer, and is electrically common
        with the net's copper on each of them -- which is how a track on an
        outer layer reaches a plane on an inner one.
        """
        order = [(cl.name, cl.layer_id) for cl in board.stackup.layers]
        names = [name for name, _ in order]
        if top_layer not in names or bottom_layer not in names:
            return []
        first, last = names.index(top_layer), names.index(bottom_layer)
        if first > last:
            first, last = last, first
        return order[first:last + 1]

    # ------------------------------------------------------------------
    # Zone cut-cell implementation
    # ------------------------------------------------------------------

    def _zone_polygon(self, zone: Zone):
        """Return a Shapely Polygon for the zone, or None if unavailable."""
        if not _SHAPELY_OK:
            return None
        pts = zone.filled_polygon or zone.outline_polygon
        if len(pts) < 3:
            return None
        try:
            poly = ShapelyPolygon(pts)
            if not poly.is_valid:
                poly = poly.buffer(0)  # fix self-intersections
            return poly if not poly.is_empty else None
        except Exception:
            return None

    def _copper_lands(self, net_name: str, board: ParsedBoard) -> dict:
        """Outlines of the net's pads and via lands, by copper layer name.

        Pads are copper areas. Modelling one as its centre point left every
        track and pour spoke touching it elsewhere unjoined: on the reference
        board the source pad of +3V3AON met a 5-node stub at its centre while
        the rail's network reached the same pad at its end, and every load was
        stranded. A pour's thermal spokes stop at the pad edge, so only the
        pad's own outline joins them; a pour set not to connect keeps its
        clearance gap and stays apart.

        A via's land is the same kind of copper, and a track may end anywhere
        on it: R9's feed on +3V3AON stops 0.14 mm from the centre of a 0.7 mm
        via, and meeting the via only at its centre stranded that load too.
        """
        lands: dict[str, list] = {}
        if not _SHAPELY_OK:
            return lands
        for footprint in board.footprints:
            for pad in footprint.pads:
                if pad.net_name != net_name:
                    continue
                outline = _pad_outline(pad)
                if outline is None:
                    continue
                for layer in pad_copper_layers(board.stackup, pad):
                    lands.setdefault(layer.name, []).append(outline)
        for via in board.vias:
            if via.net_name != net_name or via.size_mm <= 0.0 or len(via.layers) < 2:
                continue
            land = shapely.Point(*via.position).buffer(via.size_mm / 2.0, quad_segs=8)
            for name, _layer_id in self._layers_spanned(board, via.layers[0], via.layers[-1]):
                lands.setdefault(name, []).append(land)
        return lands

    def _mesh_zones(self, net_name, board, mesh, get_or_create, zone_index, lands=None) -> int:
        """Mesh every pour of *net_name*: one copper union per layer, one lattice.

        Zones were meshed one at a time, each on a lattice anchored at its own
        bounding box and coarsened against its own node budget. Once the reader
        returned every island and every layer of a zone -- it used to drop them
        -- that stopped being tenable: islands of one pour would sit on lattices
        offset from each other, tracks attach through an index keyed on one step
        and one origin, and two overlapping zones of the net would each conduct
        through the copper they share, counting it twice.

        So the net's copper on a layer is the union of its fills, the step is
        chosen once for the net from the area actually filled, and the lattice
        is aligned on multiples of that step for every layer.
        """
        if not _SHAPELY_OK or np is None:
            return 0
        polygons_by_layer: dict[str, list] = {}
        for zone in board.zones:
            if zone.net_name != net_name:
                continue
            poly = self._zone_polygon(zone)
            if poly is None:
                self._log(
                    f"[HybridMesher] Zone '{zone.net_name}' on {zone.layer}: "
                    "no polygon geometry available — zone skipped."
                )
                continue
            polygons_by_layer.setdefault(zone.layer, []).append(poly)

        pad_polygons = lands if lands is not None else self._copper_lands(net_name, board)
        if not polygons_by_layer and not pad_polygons:
            return 0

        copper_by_layer = {
            layer: shapely.union_all(polygons) if len(polygons) > 1 else polygons[0]
            for layer, polygons in polygons_by_layer.items()
        }
        # Pad copper already inside a pour is meshed with the pour; the rest is
        # meshed on the same lattice under its own label, so the conductances of
        # a cell split between the two add up to the union's.
        pad_copper_by_layer = {}
        for layer, polygons in pad_polygons.items():
            geometry = shapely.union_all(polygons)
            if layer in copper_by_layer:
                geometry = geometry.difference(copper_by_layer[layer])
            if not geometry.is_empty and geometry.area > 0.0:
                pad_copper_by_layer[layer] = geometry
        h = self._grid_step
        estimated_nodes = sum(
            geom.area for geom in (*copper_by_layer.values(), *pad_copper_by_layer.values())
        ) / (h * h)
        if estimated_nodes > self.MAX_ZONE_NODES:
            h = min(5.0, h * math.sqrt(estimated_nodes / self.MAX_ZONE_NODES) * 1.05)
            self._log(
                f"[HybridMesher] Net '{net_name}': zone grid degraded to {h:.3f} mm "
                f"(estimated {estimated_nodes:,.0f} > {self.MAX_ZONE_NODES:,} budget)"
            )
        # Tracks attach against the step actually used, not the requested one.
        self._effective_grid_step = h

        edges = 0
        for layer, geometry in copper_by_layer.items():
            edges += self._mesh_copper_cutcell(
                geometry, layer, board, mesh, get_or_create, zone_index, h,
            )
        for layer, geometry in pad_copper_by_layer.items():
            edges += self._mesh_copper_cutcell(
                geometry, layer, board, mesh, get_or_create, zone_index, h,
                label="pad-copper",
            )
        return edges

    def _mesh_copper_cutcell(
        self, geometry, layer: str, board: ParsedBoard, mesh: Mesh,
        get_or_create, zone_index, h: float, label: str = "zone",
    ) -> int:
        """Add cut-cell edges for one layer's copper. Returns the edge count.

        Each lattice edge conducts through the copper fraction of the cell
        centred on it. The fractions of a whole row are computed in one call on
        arrays of cells -- prepared predicates first, an intersection only for
        cells the copper crosses -- instead of one Shapely intersection per
        edge from Python, which is what made meshing the pours the dominant
        cost of a DC run.
        """
        thickness_mm = _thickness_for_layer(board, layer)
        layer_id = _layer_id_for_name(board, layer)
        # Conductivity in S/mm.
        #
        # rho is in ohm-metres, and 1 ohm-m is 1e3 ohm-mm, so the conversion is
        # a factor of 1e3 -- not the 1e6 this used, which is the factor for an
        # area. Every pour was therefore modelled a thousand times more
        # resistive than copper.
        #
        # Checkable against the handbook figure: one ounce of copper is 35 um
        # and has a sheet resistance of rho/t = 1.72e-8 / 35e-6 = 0.491 mohm per
        # square, so a square cell must conduct 2035 S. sigma * 0.035 gives
        # 2035 S here and gave 2.03 S before.
        #
        # It cost the advisor every number it produced: +5V_RAIL came out at
        # 6.2244 V of drop against the 0.0077 V the production mesher computes
        # for the same copper -- a drop larger than the rail's own supply.
        sigma = 1.0 / (self._rho * 1e3)

        # A lattice aligned on multiples of h, shared by every layer of the net
        # so that the index tracks attach through means the same cell anywhere.
        xmin, ymin, xmax, ymax = geometry.bounds
        xs = np.arange(math.floor(xmin / h), math.ceil(xmax / h) + 1) * h
        ys = np.arange(math.floor(ymin / h), math.ceil(ymax / h) + 1) * h
        if len(xs) < 2 or len(ys) < 2:
            return 0

        shapely_prepare(geometry)
        h2 = h * h  # cell area
        edges_added = 0

        def fractions(cells):
            """Copper fraction of each cell: 0 outside, 1 inside, clipped between."""
            fraction = np.zeros(len(cells))
            touching = shapely.intersects(geometry, cells)
            if not touching.any():
                return fraction
            inside = touching & shapely.contains(geometry, cells)
            fraction[inside] = 1.0
            crossing = touching & ~inside
            if crossing.any():
                fraction[crossing] = shapely.area(shapely_intersect(geometry, cells[crossing])) / h2
            return fraction

        def node_at(x: float, y: float) -> int:
            """Lattice node at a point, minted only when copper attaches to it.

            The grid spans the pour's *bounding box*, and a pour rarely fills
            its own bounding box. Minting a node per lattice point therefore
            put nodes where the net has no copper: in the empty corners of a
            non-rectangular pour, and inside every antipad hole. None of them
            can ever gain a branch, because a branch only exists where a cell
            overlaps copper, so each became a component of one node.

            On the reference board that is 900 of +3V3_MAIN's 902 connected
            components. Measured on the two shapes separately: a diamond pour
            on a 5 mm box at a 0.1 mm step yields 1,200 isolated nodes, and one
            0.6 mm antipad hole yields 25.

            They are not merely waste. They are indexed as plane nodes, so a
            pad or track endpoint over a hole in the pour adopts one and binds
            to copper that is not there -- ``probe`` reads the index to decide
            whether a pad meets copper at all.
            """
            nid = get_or_create(x, y, layer_id)
            if zone_index is not None:
                zone_index[(int(round(x / h)), int(round(y / h)), layer_id)] = nid
            return nid

        for iy, y in enumerate(ys):
            # Horizontal edges (x, y) -> (x+h, y); vertical edges (x, y) -> (x, y+h).
            horizontal = fractions(shapely_box(xs[:-1], y - h / 2, xs[1:], y + h / 2))
            vertical = (
                fractions(shapely_box(xs - h / 2, y, xs + h / 2, ys[iy + 1]))
                if iy + 1 < len(ys) else np.zeros(len(xs))
            )
            if not horizontal.any() and not vertical.any():
                continue
            for ix in np.flatnonzero((np.append(horizontal, 0.0) > 1e-9) | (vertical > 1e-9)):
                x = xs[ix]
                if ix + 1 < len(xs) and horizontal[ix] > 1e-9:
                    frac = float(horizontal[ix])
                    mesh.add_edge_direct(
                        node_at(x, y), node_at(xs[ix + 1], y), sigma * thickness_mm * frac,
                        kind="lateral",
                        cross_section_mm2=frac * h * thickness_mm,
                        geometry_source=f"{label}:{layer}:h",
                    )
                    edges_added += 1
                if vertical[ix] > 1e-9:
                    frac = float(vertical[ix])
                    mesh.add_edge_direct(
                        node_at(x, y), node_at(x, ys[iy + 1]), sigma * thickness_mm * frac,
                        kind="lateral",
                        cross_section_mm2=frac * h * thickness_mm,
                        geometry_source=f"{label}:{layer}:v",
                    )
                    edges_added += 1

        return edges_added

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log(self, msg: str) -> None:
        if self._log_cb is not None:
            self._log_cb(msg)
