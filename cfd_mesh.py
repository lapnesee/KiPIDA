"""Structured volumetric mesh for enclosure airflow and conjugate heat transfer."""

from dataclasses import dataclass, field
import math

try:
    import numpy as np
except ImportError:
    np = None


@dataclass
class CFDPatchCells:
    patch: object
    cells: list = field(default_factory=list)


@dataclass
class CFDMesh:
    shape: tuple
    spacing_m: tuple
    dimensions_m: tuple
    fluid_mask: object
    solid_mask: object
    solid_conductivity_w_mk: object
    heat_sources_w: object
    patch_cells: list = field(default_factory=list)
    obstacle_names: object = None
    # Names of obstacles thinner than one cell on *every* axis. Each adds its
    # heat to the solid cell it sits on, named in collapsed_hosts, rather
    # than occupying a cell of its own. See CFD-1 in docs/audit-2026-09-23.md.
    collapsed_obstacles: list = field(default_factory=list)
    collapsed_hosts: dict = field(default_factory=dict)
    # Per solid cell, the plate length of its obstacle's faces, in metres:
    # A/P of its x-y footprint for a face normal to z, its z extent for a
    # face normal to x or y. None where the mesh predates them.
    film_length_horizontal_m: object = None
    film_length_vertical_m: object = None

    @property
    def cell_count(self):
        return int(self.shape[0] * self.shape[1] * self.shape[2])

    @property
    def cell_volume_m3(self):
        return self.spacing_m[0] * self.spacing_m[1] * self.spacing_m[2]

    def cell_center_mm(self, i, j, k):
        dx, dy, dz = self.spacing_m
        return ((i + 0.5) * dx * 1000.0,
                (j + 0.5) * dy * 1000.0,
                (k + 0.5) * dz * 1000.0)


class CFDMeshGenerator:
    def __init__(self, debug=False, log_callback=None):
        self.debug = debug
        self.log_callback = log_callback

    def _log(self, message):
        if self.log_callback:
            self.log_callback(f"[CFD MESH] {message}")

    @staticmethod
    def _patch_cells(patch, shape):
        nx, ny, nz = shape
        face = str(patch.face).upper()
        if face in {"XMIN", "XMAX"}:
            fixed = 0 if face == "XMIN" else nx - 1
            nu, nv = ny, nz
            make = lambda u, v: (fixed, u, v)
        elif face in {"YMIN", "YMAX"}:
            fixed = 0 if face == "YMIN" else ny - 1
            nu, nv = nx, nz
            make = lambda u, v: (u, fixed, v)
        elif face in {"ZMIN", "ZMAX"}:
            fixed = 0 if face == "ZMIN" else nz - 1
            nu, nv = nx, ny
            make = lambda u, v: (u, v, fixed)
        else:
            raise ValueError(f"Unsupported CFD patch face: {patch.face}")
        u0 = max(0, int(math.floor((patch.center_u - patch.size_u / 2.0) * nu)))
        u1 = min(nu, int(math.ceil((patch.center_u + patch.size_u / 2.0) * nu)))
        v0 = max(0, int(math.floor((patch.center_v - patch.size_v / 2.0) * nv)))
        v1 = min(nv, int(math.ceil((patch.center_v + patch.size_v / 2.0) * nv)))
        return [make(u, v) for u in range(u0, u1) for v in range(v0, v1)]

    def generate_mesh(self, model, settings):
        if np is None:
            raise ImportError("NumPy is required for enclosure CFD meshing.")
        requested = max(0.5, float(settings.solver.cell_size_mm))
        dims_mm = model.dimensions_mm
        shape = tuple(max(3, int(math.ceil(size / requested))) for size in dims_mm)
        count = shape[0] * shape[1] * shape[2]
        if count > int(settings.solver.max_cells):
            raise ValueError(
                f"CFD mesh would contain {count:,} cells. Increase the CFD cell size."
            )
        dims_m = tuple(value * 1e-3 for value in dims_mm)
        spacing = tuple(dims_m[index] / shape[index] for index in range(3))
        solid = np.zeros(shape, dtype=bool)
        conductivity = np.zeros(shape, dtype=float)
        heat = np.zeros(shape, dtype=float)
        names = np.empty(shape, dtype=object)
        names[:] = "AIR"
        length_horizontal = np.zeros(shape, dtype=float)
        length_vertical = np.zeros(shape, dtype=float)

        xs = (np.arange(shape[0]) + 0.5) * spacing[0] * 1000.0
        ys = (np.arange(shape[1]) + 0.5) * spacing[1] * 1000.0
        zs = (np.arange(shape[2]) + 0.5) * spacing[2] * 1000.0
        centers = (xs, ys, zs)
        collapsed = []
        deferred = []
        for obstacle in model.obstacles:
            bounds = ((obstacle.bounds_mm[0], obstacle.bounds_mm[3]),
                      (obstacle.bounds_mm[1], obstacle.bounds_mm[4]),
                      (obstacle.bounds_mm[2], obstacle.bounds_mm[5]))
            axis_indices = []
            fell_back = []
            for axis in range(3):
                lo, hi = bounds[axis]
                found = np.nonzero((centers[axis] >= lo) & (centers[axis] <= hi))[0]
                if found.size:
                    axis_indices.append(found)
                    fell_back.append(False)
                    continue
                # No cell centre falls inside this axis's span -- the
                # obstacle is thinner than one cell there. Snapping only
                # *this* axis to its nearest cell, instead of collapsing
                # every axis to the centroid's single cell, is what keeps a
                # thin board a conductive sheet across its whole footprint:
                # P02 Rev E's 125 x 45 x 1.29 mm board used to become one
                # 5 mm cell (CFD-1); here only z falls back, x and y keep
                # their full overlap.
                nearest = int(np.argmin(np.abs(centers[axis] - (lo + hi) / 2.0)))
                axis_indices.append(np.array([nearest]))
                fell_back.append(True)
            if all(fell_back):
                # Smaller than a cell on every axis: placed once every larger
                # obstacle, the board first, is in (below).
                deferred.append((obstacle, tuple(int(item[0]) for item in axis_indices)))
                continue
            mask = np.zeros(shape, dtype=bool)
            mask[np.ix_(*axis_indices)] = True
            # Snapped into a cell thicker than itself, a sheet would conduct
            # sideways as if it were a cell thick: P02 Rev E's 1.29 mm board
            # in a 5 mm cell, 3.9 times its own conductance. Its conductivity
            # is scaled by its thickness over the cell's, which keeps k * t.
            # An obstacle collapsed on every axis is no sheet and keeps its own.
            scale = 1.0
            for axis in range(3):
                if fell_back[axis]:
                    extent = (bounds[axis][1] - bounds[axis][0]) * 1e-3
                    scale *= min(1.0, max(extent, 0.0) / spacing[axis])
            solid |= mask
            conductivity[mask] = max(0.05, float(obstacle.conductivity_w_mk) * scale)
            names[mask] = obstacle.name
            # The obstacle's own extents, not the cells it was snapped to.
            width, depth, height = (max(hi - lo, 0.1) for lo, hi in bounds)
            length_horizontal[mask] = width * depth / (2.0 * (width + depth)) * 1e-3
            length_vertical[mask] = height * 1e-3
            cells = int(np.count_nonzero(mask))
            if obstacle.heat_w > 0 and cells:
                heat[mask] += float(obstacle.heat_w) / cells
        # An obstacle smaller than a cell on every axis became a whole cell of
        # its own conductivity at its centroid's cell. P02 Rev E's U15 (3 x 3 x
        # 1 mm) took a 5 mm board cell that way: renamed U15 and conducting
        # at its bulk 8 W/mK where the board sheet conducts k * t / cell.
        # It now adds its heat to the solid cell it sits on -- that cell if
        # solid, else the nearest solid cell -- and leaves the cell as it was.
        hosts = {}
        solid_cells = np.argwhere(solid)
        for obstacle, cell in deferred:
            collapsed.append(obstacle.name)
            if not solid[cell] and solid_cells.size:
                offsets = (solid_cells - np.asarray(cell)) * np.asarray(spacing)
                cell = tuple(int(value) for value in solid_cells[int(np.argmin((offsets ** 2).sum(axis=1)))])
            if not solid[cell]:
                # No solid anywhere to sit on: the old single-cell fallback.
                solid[cell] = True
                conductivity[cell] = max(0.05, float(obstacle.conductivity_w_mk))
                names[cell] = obstacle.name
            hosts[obstacle.name] = str(names[cell])
            if obstacle.heat_w > 0:
                heat[cell] += float(obstacle.heat_w)
        if collapsed:
            self._log(
                f"{len(collapsed)} obstacle(s) smaller than one cell on every axis add their "
                "heat to the solid cell they sit on: "
                + ", ".join(f"{name} into {hosts[name]}" for name in collapsed) + "."
            )

        fluid = ~solid
        patches = []
        occupied = set()
        for patch in model.patches:
            kind = str(patch.kind).upper()
            if kind not in {"WALL", "INLET", "OUTLET", "VENT", "FAN"}:
                raise ValueError(f"Unsupported CFD patch type: {patch.kind}")
            normalized = (patch.center_u, patch.center_v, patch.size_u, patch.size_v)
            if any(float(value) < 0.0 or float(value) > 1.0 for value in normalized):
                raise ValueError(
                    f"CFD boundary patch '{patch.name}' coordinates must be between 0 and 1."
                )
            if patch.size_u <= 0.0 or patch.size_v <= 0.0:
                raise ValueError(f"CFD boundary patch '{patch.name}' must have a positive size.")
            if kind in {"INLET", "FAN"} and patch.velocity_m_s <= 0.0:
                raise ValueError(
                    f"CFD boundary patch '{patch.name}' requires a positive velocity."
                )
            cells = [cell for cell in self._patch_cells(patch, shape) if fluid[cell]]
            if not cells:
                raise ValueError(
                    f"CFD boundary patch '{patch.name}' has no fluid cells on its face."
                )
            duplicate = occupied.intersection(cells)
            if duplicate:
                raise ValueError(f"CFD boundary patch '{patch.name}' overlaps another patch.")
            occupied.update(cells)
            patches.append(CFDPatchCells(patch=patch, cells=cells))
        inlet_count = sum(
            1 for mapped in patches if str(mapped.patch.kind).upper() in {"INLET", "FAN"}
        )
        outlet_count = sum(
            1 for mapped in patches if str(mapped.patch.kind).upper() in {"OUTLET", "VENT"}
        )
        if inlet_count and not outlet_count:
            raise ValueError("Forced enclosure flow requires at least one OUTLET or VENT patch.")
        mesh = CFDMesh(
            shape=shape,
            spacing_m=spacing,
            dimensions_m=dims_m,
            fluid_mask=fluid,
            solid_mask=solid,
            solid_conductivity_w_mk=conductivity,
            heat_sources_w=heat,
            patch_cells=patches,
            obstacle_names=names,
            collapsed_obstacles=collapsed,
            collapsed_hosts=hosts,
            film_length_horizontal_m=length_horizontal,
            film_length_vertical_m=length_vertical,
        )
        self._log(
            f"Generated {shape[0]} x {shape[1]} x {shape[2]} = {count:,} cells "
            f"({np.count_nonzero(fluid):,} fluid)."
        )
        return mesh
