"""Pure-data enclosure model built from the Phase 3 thermal board model."""

from dataclasses import dataclass, field, replace
from typing import List, Tuple

try:
    from .models import EnclosureGeometrySettings
    from .physical_constants import DEFAULT_COPPER_THICKNESS_MM
except (ImportError, ValueError):
    from models import EnclosureGeometrySettings
    from physical_constants import DEFAULT_COPPER_THICKNESS_MM

# Air kept around a board whose enclosure was never entered: four of the
# default 5 mm CFD cells between each board edge and the wall.
UNSET_ENCLOSURE_MARGIN_MM = 20.0


def enclosure_air_rise_k(settings, heat_w: float) -> float:
    """Mean internal air temperature rise above ambient, in a sealed box.

    Q = h * A_walls * rise, so rise = Q / (h * A_walls): a lumped balance
    between the injected power and the whole enclosure's own film, not a
    local field value. It is the only enclosure-CFD quantity that does not
    need the flow field resolved to be defensible (docs/audit-2026-09-23.md
    CFD-4) -- unlike the solid/air maxima, which a millimetre mesh at these
    cell counts cannot be trusted for (CFD-003).

    On P02 Rev E (2026-09-22, ambient 45 C): 4.755 W into a 165 x 100 x 50 mm
    enclosure (A = 0.0595 m^2) at wall_heat_transfer_w_m2k = 5 -> 16.0 K,
    matching the solved wall-cell mean of 61.0 C - 45 C ambient
    (scratchpad/audit/cfd/p02_inspect.py). Both the CFD report and the 3D
    thermal analysis's internal ambient use this same function (see the
    integrator note in docs/audit-2026-09-23.md CFD-4).
    """
    geometry = settings.geometry
    width_m = float(geometry.width_mm) * 1e-3
    depth_m = float(geometry.depth_mm) * 1e-3
    height_m = float(geometry.height_mm) * 1e-3
    area_m2 = 2.0 * (width_m * depth_m + width_m * height_m + depth_m * height_m)
    wall_h = max(1e-9, float(geometry.wall_heat_transfer_w_m2k))
    return max(0.0, float(heat_w)) / (wall_h * max(area_m2, 1e-9))


def fit_unset_enclosure(settings, board_model, margin_mm=UNSET_ENCLOSURE_MARGIN_MM):
    """``(settings, note)`` with an enclosure that holds the board if none was entered.

    The enclosure is the user's to state. Until then its dataclass default,
    120 x 100 x 50 mm, stands in, and a longer board made the CFD refuse to
    run. Still at that default and too small, it grows to the board plus
    *margin_mm* on each side, and *note* says so; otherwise *settings* is
    returned as it is, with a None note.
    """
    geometry = settings.geometry
    if geometry != EnclosureGeometrySettings():
        return settings, None
    min_x, min_y, max_x, max_y = board_model.bounds_mm
    board_width, board_depth = max_x - min_x, max_y - min_y
    if board_width <= geometry.width_mm and board_depth <= geometry.depth_mm:
        return settings, None
    fitted = replace(
        geometry,
        width_mm=max(geometry.width_mm, float(round(board_width + 2.0 * margin_mm))),
        depth_mm=max(geometry.depth_mm, float(round(board_depth + 2.0 * margin_mm))),
    )
    note = (
        f"The enclosure was never set and the default {geometry.width_mm:g} x "
        f"{geometry.depth_mm:g} mm cannot hold the {board_width:.1f} x {board_depth:.1f} mm "
        f"board: using {fitted.width_mm:g} x {fitted.depth_mm:g} x {fitted.height_mm:g} mm, "
        f"the board plus {margin_mm:g} mm each side. Enter the real enclosure in Enclosure CFD."
    )
    return replace(settings, geometry=fitted), note


@dataclass
class CFDObstacle:
    name: str
    bounds_mm: Tuple[float, float, float, float, float, float]
    conductivity_w_mk: float
    heat_w: float = 0.0
    kind: str = "component"


@dataclass
class EnclosureModel:
    dimensions_mm: Tuple[float, float, float]
    obstacles: List[CFDObstacle] = field(default_factory=list)
    patches: list = field(default_factory=list)


class EnclosureModelBuilder:
    """Place an extracted PCB and compact component cuboids inside an enclosure."""

    def __init__(self, debug=False, log_callback=None):
        self.debug = debug
        self.log_callback = log_callback

    def _log(self, message):
        if self.log_callback:
            self.log_callback(f"[CFD MODEL] {message}")

    @staticmethod
    def _board_thickness_mm(stackup):
        copper = stackup.get("copper", {})
        substrate = stackup.get("substrate", [])
        thickness = sum(float(item.get("thickness_mm", DEFAULT_COPPER_THICKNESS_MM)) for item in copper.values())
        thickness += sum(float(item.get("thickness_mm", 0.0)) for item in substrate)
        return max(thickness, 1.0)

    @staticmethod
    def _oriented_bounds(orientation, origin, x, y, width, depth, height):
        ox, oy, oz = origin
        if orientation == "XZ":
            return (ox + x, oy, oz + y, ox + x + width, oy + height, oz + y + depth)
        if orientation == "YZ":
            return (ox, oy + x, oz + y, ox + height, oy + x + width, oz + y + depth)
        return (ox + x, oy + y, oz, ox + x + width, oy + y + depth, oz + height)

    def build(self, board_model, settings):
        width = float(settings.geometry.width_mm)
        depth = float(settings.geometry.depth_mm)
        height = float(settings.geometry.height_mm)
        if min(width, depth, height) <= 0:
            raise ValueError("Enclosure dimensions must be greater than zero.")

        min_x, min_y, max_x, max_y = board_model.bounds_mm
        board_width = max_x - min_x
        board_depth = max_y - min_y
        board_height = self._board_thickness_mm(board_model.stackup)
        orientation = str(settings.geometry.board_orientation or "XY").upper()
        if orientation not in {"XY", "XZ", "YZ"}:
            raise ValueError("Board orientation must be XY, XZ, or YZ.")

        if orientation == "XY":
            base = ((width - board_width) / 2.0, (depth - board_depth) / 2.0,
                    settings.geometry.board_offset_z_mm)
        elif orientation == "XZ":
            base = ((width - board_width) / 2.0, (depth - board_height) / 2.0,
                    (height - board_depth) / 2.0)
        else:
            base = ((width - board_height) / 2.0, (depth - board_width) / 2.0,
                    (height - board_depth) / 2.0)
        origin = (
            base[0] + settings.geometry.board_offset_x_mm,
            base[1] + settings.geometry.board_offset_y_mm,
            base[2],
        )

        board_heat = sum(max(0.0, loss.power_w) for loss in board_model.copper_losses)
        obstacles = [CFDObstacle(
            name="PCB",
            bounds_mm=self._oriented_bounds(
                orientation, origin, 0.0, 0.0, board_width, board_depth, board_height
            ),
            conductivity_w_mk=0.6,
            heat_w=board_heat,
            kind="board",
        )]
        components = {component.ref_des: component for component in board_model.components}
        for ref_des, placement in board_model.placements.items():
            component = components.get(ref_des)
            if component is None or not component.enabled:
                continue
            local_x = placement.x_mm - min_x - placement.width_mm / 2.0
            local_y = placement.y_mm - min_y - placement.depth_mm / 2.0
            component_height = max(0.2, float(component.height_mm))
            component_origin = list(origin)
            if placement.side == "BOTTOM":
                if orientation == "XY":
                    component_origin[2] -= component_height
                elif orientation == "XZ":
                    component_origin[1] -= component_height
                else:
                    component_origin[0] -= component_height
            else:
                if orientation == "XY":
                    component_origin[2] += board_height
                elif orientation == "XZ":
                    component_origin[1] += board_height
                else:
                    component_origin[0] += board_height
            obstacles.append(CFDObstacle(
                name=ref_des,
                bounds_mm=self._oriented_bounds(
                    orientation, tuple(component_origin), local_x, local_y,
                    placement.width_mm, placement.depth_mm, component_height,
                ),
                conductivity_w_mk=8.0,
                heat_w=max(0.0, float(component.power_w)),
                kind="component",
            ))

        for obstacle in obstacles:
            x0, y0, z0, x1, y1, z1 = obstacle.bounds_mm
            if x0 < 0 or y0 < 0 or z0 < 0 or x1 > width or y1 > depth or z1 > height:
                raise ValueError(
                    f"{obstacle.name} ({x1 - x0:.1f} x {y1 - y0:.1f} x {z1 - z0:.1f} mm) does not "
                    f"fit inside the configured {width:g} x {depth:g} x {height:g} mm enclosure."
                )
        self._log(
            f"Built {width:g} x {depth:g} x {height:g} mm enclosure with "
            f"{len(obstacles)} solid obstacles."
        )
        return EnclosureModel(
            dimensions_mm=(width, depth, height),
            obstacles=obstacles,
            patches=list(settings.patches),
        )
