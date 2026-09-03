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

        def get_or_create(x: float, y: float, layer_id: int) -> int:
            key = (_snap(x), _snap(y), layer_id)
            if key not in node_registry:
                nid = node_counter[0]
                node_counter[0] += 1
                node_registry[key] = nid
                mesh.nodes.append(nid)
                mesh.node_coords[nid] = (_snap(x), _snap(y), layer_id)
            return node_registry[key]

        board = self._board

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
            u = get_or_create(x0, y0, layer_id)
            v = get_or_create(x1, y1, layer_id)
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

            u = get_or_create(vx, vy, lid_top)
            v = get_or_create(vx, vy, lid_bot)
            if u == v:
                continue
            g = 1.0 / R_via
            mesh.add_edge_direct(
                u, v, g,
                kind="vertical",
                cross_section_mm2=math.pi * drill_mm * plating_mm,
                geometry_source=f"via:{top_layer}-{bot_layer}",
            )
            via_count += 1

        self._log(f"[HybridMesher] Net '{net_name}': {via_count} via branches")

        # ------------------------------------------------------------------
        # 3. Zones — cut-cell (pending polygon geometry in ParsedBoard.Zone)
        # ------------------------------------------------------------------
        zone_layers = set(z.layer for z in board.zones if z.net_name == net_name)
        if zone_layers:
            self._log(
                f"[HybridMesher] Net '{net_name}': zone copper on {sorted(zone_layers)} "
                "not yet meshed — Zone.polygon not available in ParsedBoard. "
                "Add polygon geometry to Zone to enable cut-cell."
            )

        return mesh

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log(self, msg: str) -> None:
        if self._log_cb is not None:
            self._log_cb(msg)
