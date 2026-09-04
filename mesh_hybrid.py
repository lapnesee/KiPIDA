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
* **Zone copper** — requires polygon geometry not yet stored in
  :class:`~ingest.board_reader.Zone`.  A cut-cell implementation will be
  added in a future phase once ``Zone`` carries its filled polygon.  Zones
  are currently skipped with a warning; their copper contribution is absent
  from the mesh.

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
    from .ingest.board_reader import ParsedBoard, Segment, Via, Zone
    from .ingest.track_resistance import (
        RHO_COPPER,
        segment_resistance,
        via_resistance,
    )
except (ImportError, ValueError):
    from ingest.board_reader import ParsedBoard, Segment, Via, Zone
    from ingest.track_resistance import (
        RHO_COPPER,
        segment_resistance,
        via_resistance,
    )

try:
    from .mesh import Mesh
except (ImportError, ValueError):
    from mesh import Mesh

try:
    import numpy as np
    from shapely import Polygon as ShapelyPolygon
    from shapely import box as shapely_box, intersection as shapely_intersect, prepare as shapely_prepare
    _SHAPELY_OK = True
except ImportError:
    np = None  # type: ignore[assignment]
    _SHAPELY_OK = False

# Coordinate snap precision in mm (1 µm → 0.001 mm)
_SNAP = 0.001


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
    """Return copper thickness in mm for a named layer, or 0.035 mm as default."""
    for cl in parsed_board.stackup.layers:
        if cl.name == name:
            return cl.thickness_mm if cl.thickness_mm > 0 else 0.035
    return 0.035


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
                if best_id is not None and best_distance <= 0.5 * step:
                    return best_id
            return get_or_create(x, y, layer_id)

        board = self._board

        # ------------------------------------------------------------------
        # 0. Zones first — they lay down the grid that tracks then attach to.
        #
        # Order matters here. Tracks can only adopt a plane node if the plane
        # nodes already exist, so meshing zones last left every track isolated.
        # ------------------------------------------------------------------
        zone_count = 0
        for zone in board.zones:
            if zone.net_name != net_name:
                continue
            zone_count += self._mesh_zone_cutcell(
                zone, board, mesh, get_or_create, zone_index,
            )
        self._log(f"[HybridMesher] Net '{net_name}': {zone_count} zone edges")

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
            u = attach(x0, y0, layer_id)
            v = attach(x1, y1, layer_id)
            if u == v:
                # Both endpoints snapped to same node — treat as short
                continue
            g = 1.0 / R
            cs = width_mm * thickness_mm  # mm²
            mesh.add_edge_direct(
                u, v, g,
                kind="lateral",
                cross_section_mm2=cs,
                geometry_source=f"seg:{seg.layer}",
            )
            seg_count += 1

        self._log(f"[HybridMesher] Net '{net_name}': {seg_count} segment branches")

        # ------------------------------------------------------------------
        # 2. Vias — barrel analytical resistance
        # ------------------------------------------------------------------
        via_count = 0
        board_height_mm = board.stackup.total_thickness_mm
        if board_height_mm <= 0:
            board_height_mm = 1.6  # fallback

        for via in board.vias:
            if via.net_name != net_name:
                continue
            if len(via.layers) < 2:
                continue
            vx, vy = via.position
            drill_mm = via.drill_mm if via.drill_mm > 0 else 0.3
            size_mm = via.size_mm if via.size_mm > 0 else (drill_mm + 0.05)
            plating_mm = (size_mm - drill_mm) / 2.0
            if plating_mm <= 0:
                plating_mm = 0.025  # minimum 25 µm

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
                        cross_section_mm2=math.pi * drill_mm * plating_mm,
                        geometry_source=f"via:{top_layer}-{bot_layer}",
                    )
                    added = True
                previous = node
            if added:
                via_count += 1

        self._log(f"[HybridMesher] Net '{net_name}': {via_count} via branches")

        return mesh

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

    def _mesh_zone_cutcell(
        self,
        zone: Zone,
        board: ParsedBoard,
        mesh: Mesh,
        get_or_create,
        zone_index=None,
    ) -> int:
        """Add cut-cell edges for one zone. Returns edge count added."""
        poly = self._zone_polygon(zone)
        if poly is None:
            self._log(
                f"[HybridMesher] Zone '{zone.net_name}' on {zone.layer}: "
                "no polygon geometry available — zone skipped."
            )
            return 0

        if not _SHAPELY_OK or np is None:
            return 0

        thickness_mm = _thickness_for_layer(board, zone.layer)
        layer_id = _layer_id_for_name(board, zone.layer)
        sigma = 1.0 / self._rho / 1e6  # S/mm (convert Ω·m → Ω·mm, then invert)

        # Grid over zone bounding box
        xmin, ymin, xmax, ymax = poly.bounds
        h = self._grid_step

        # Budget check — degrade grid if needed
        nx_est = max(1, int((xmax - xmin) / h) + 1)
        ny_est = max(1, int((ymax - ymin) / h) + 1)
        estimated_nodes = nx_est * ny_est
        if estimated_nodes > self.MAX_ZONE_NODES:
            scale = math.sqrt(estimated_nodes / self.MAX_ZONE_NODES) * 1.05
            h = min(5.0, h * scale)
            self._log(
                f"[HybridMesher] Zone '{zone.net_name}': grid degraded to {h:.3f} mm "
                f"(estimated {estimated_nodes:,} > {self.MAX_ZONE_NODES:,} budget)"
            )
        # Tracks attach against the step actually used, not the requested one.
        self._effective_grid_step = h

        xs = np.arange(xmin, xmax + h * 0.5, h)
        ys = np.arange(ymin, ymax + h * 0.5, h)
        if len(xs) < 2 or len(ys) < 2:
            return 0

        # Prepare geometry for fast intersection tests
        shapely_prepare(poly)
        h2 = h * h  # cell area
        edges_added = 0

        for iy, y in enumerate(ys):
            for ix, x in enumerate(xs):
                u = get_or_create(x, y, layer_id)
                if zone_index is not None:
                    zone_index[(int(round(x / h)), int(round(y / h)), layer_id)] = u

                # Horizontal edge → (x+h, y)
                if ix + 1 < len(xs):
                    xr = xs[ix + 1]
                    cell_h = shapely_box(x, y - h / 2, xr, y + h / 2)
                    overlap = shapely_intersect(poly, cell_h)
                    frac = overlap.area / h2 if overlap and not overlap.is_empty else 0.0
                    if frac > 1e-9:
                        g = sigma * thickness_mm * frac
                        v = get_or_create(xr, y, layer_id)
                        mesh.add_edge_direct(
                            u, v, g, kind="lateral",
                            cross_section_mm2=frac * h * thickness_mm,
                            geometry_source=f"zone:{zone.layer}:h",
                        )
                        edges_added += 1

                # Vertical edge → (x, y+h)
                if iy + 1 < len(ys):
                    yt = ys[iy + 1]
                    cell_v = shapely_box(x - h / 2, y, x + h / 2, yt)
                    overlap = shapely_intersect(poly, cell_v)
                    frac = overlap.area / h2 if overlap and not overlap.is_empty else 0.0
                    if frac > 1e-9:
                        g = sigma * thickness_mm * frac
                        v = get_or_create(x, yt, layer_id)
                        mesh.add_edge_direct(
                            u, v, g, kind="lateral",
                            cross_section_mm2=frac * h * thickness_mm,
                            geometry_source=f"zone:{zone.layer}:v",
                        )
                        edges_added += 1

        return edges_added

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log(self, msg: str) -> None:
        if self._log_cb is not None:
            self._log_cb(msg)
