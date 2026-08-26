"""Finite-volume 3D thermal mesh for Ki-PIDA boards."""

from dataclasses import dataclass, field
import math
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

try:
    from shapely.geometry import Point
    from shapely.prepared import prep
except ImportError:
    Point = prep = None

try:
    from .models import ThermalAnalysisSettings
except (ImportError, ValueError):
    from models import ThermalAnalysisSettings


@dataclass
class ThermalLayerSpec:
    name: str
    thickness_mm: float
    layer_id: Optional[int]
    material: str


@dataclass
class ThermalBranch:
    node_a: int
    node_b: int
    conductance_w_k: float
    kind: str = "solid"


@dataclass
class ThermalBoundary:
    node_id: int
    conductance_w_k: float
    kind: str = "convection"


@dataclass
class ThermalMesh:
    nodes: List[int] = field(default_factory=list)
    node_coords: Dict[int, Tuple[float, float, float]] = field(default_factory=dict)
    node_layers: Dict[int, Optional[int]] = field(default_factory=dict)
    node_map: Dict[Tuple[int, int, int], int] = field(default_factory=dict)
    branches: List[ThermalBranch] = field(default_factory=list)
    boundaries: List[ThermalBoundary] = field(default_factory=list)
    heat_sources_w: Dict[int, float] = field(default_factory=dict)
    component_nodes: Dict[str, List[int]] = field(default_factory=dict)
    component_models: Dict[str, object] = field(default_factory=dict)
    grid_size_mm: float = 1.0
    bounds_mm: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    layer_specs: List[ThermalLayerSpec] = field(default_factory=list)
    convection_coefficient_w_m2k: float = 0.0
    requested_grid_size_mm: float = 1.0
    adaptive_grid: bool = False

    def add_heat(self, node_id, power_w):
        self.heat_sources_w[node_id] = self.heat_sources_w.get(node_id, 0.0) + float(power_w)

    def nearest_node(self, x_mm, y_mm, layer_id=None):
        """Return the closest cell without scanning the full 3D mesh in normal use."""
        if not self.nodes:
            return None
        min_x, min_y, _, _ = self.bounds_mm
        ix = int((float(x_mm) - min_x) / self.grid_size_mm)
        iy = int((float(y_mm) - min_y) / self.grid_size_mm)
        layer_indices = [
            index for index, spec in enumerate(self.layer_specs)
            if layer_id is None or spec.layer_id == layer_id
        ]
        if not layer_indices:
            layer_indices = list(range(len(self.layer_specs)))

        for radius in range(0, 5):
            candidates = []
            for iz in layer_indices:
                for offset_y in range(-radius, radius + 1):
                    for offset_x in range(-radius, radius + 1):
                        if radius and max(abs(offset_x), abs(offset_y)) != radius:
                            continue
                        node = self.node_map.get((ix + offset_x, iy + offset_y, iz))
                        if node is not None:
                            candidates.append(node)
            if candidates:
                return min(candidates, key=lambda node: (
                    (self.node_coords[node][0] - x_mm) ** 2 +
                    (self.node_coords[node][1] - y_mm) ** 2
                ))

        candidates = [
            node for node in self.nodes
            if layer_id is None or self.node_layers.get(node) == layer_id
        ] or list(self.nodes)
        return min(candidates, key=lambda node: (
            (self.node_coords[node][0] - x_mm) ** 2 +
            (self.node_coords[node][1] - y_mm) ** 2
        ))


def estimate_thermal_mesh_cost(context, grid_size_mm):
    """Return a conservative rectangular-board mesh and sparse-memory estimate."""
    context = context or {}
    width = max(0.0, float(context.get("width_mm", 0.0)))
    height = max(0.0, float(context.get("height_mm", 0.0)))
    layers = max(1, int(context.get("thermal_layers", 3)))
    grid = max(0.1, float(grid_size_mm))
    xy_cells = max(1, math.ceil(width / grid)) * max(1, math.ceil(height / grid))
    nodes = xy_cells * layers
    branches = int(xy_cells * (2 * layers + max(0, layers - 1)))
    nnz = nodes + 2 * branches
    cpu_bytes = nnz * 12 + nodes * 48 + branches * 32
    gpu_bytes = nnz * 12 + nodes * 8 * 8
    cuda_ok = bool(context.get("cuda_available", False))
    threshold = int(context.get("cuda_min_nodes", 100000))
    backend = "CUDA" if cuda_ok and nodes >= threshold else "CPU"
    return dict(nodes=nodes, branches=branches, cpu_bytes=cpu_bytes,
                gpu_bytes=gpu_bytes, backend=backend)


class ThermalMesher:
    COPPER_K = 385.0
    FR4_K_XY = 0.8
    FR4_K_Z = 0.3
    SIGMA = 5.670374419e-8
    CPU_NODE_LIMIT = 500000
    CUDA_NODE_LIMIT = 1250000

    def __init__(self, debug=False, log_callback=None, compute_settings=None):
        self.debug = debug
        self.log_callback = log_callback
        self.compute_settings = compute_settings

    def _log(self, message):
        if self.log_callback:
            self.log_callback(f"[THERMAL MESH] {message}")

    def _worker_count(self):
        settings = self.compute_settings
        if settings is not None and not settings.cpu_multithread:
            return 1
        configured = int(getattr(settings, "cpu_threads", 0) or 0) if settings else 0
        return max(1, configured or (os.cpu_count() or 1))

    def _node_limit(self):
        compute = self.compute_settings
        cuda_requested = bool(
            compute and getattr(compute, "cuda_enabled", False) and
            str(getattr(compute, "backend", "AUTO")).upper() != "CPU"
        )
        return (self.CUDA_NODE_LIMIT if cuda_requested else self.CPU_NODE_LIMIT), cuda_requested

    @staticmethod
    def _sample_layer(outline, copper, min_x, min_y, nx, ny, grid):
        prepared_outline = prep(outline)
        prepared_copper = prep(copper) if copper is not None and not copper.is_empty else None
        cells = []
        for iy in range(ny):
            y = min_y + (iy + 0.5) * grid
            for ix in range(nx):
                x = min_x + (ix + 0.5) * grid
                point = Point(x, y)
                if prepared_outline.covers(point):
                    cells.append((ix, iy, bool(prepared_copper and prepared_copper.covers(point))))
        return cells

    @staticmethod
    def convection_coefficient(settings: ThermalAnalysisSettings):
        airflow = settings.airflow
        mode = str(airflow.mode or "NATURAL").upper()
        if mode == "CUSTOM":
            return max(0.1, float(airflow.custom_h_w_m2k))
        if mode == "FORCED":
            return max(5.0, 5.7 + 3.8 * max(0.0, float(airflow.velocity_m_s)))
        return 5.0

    def _layer_specs(self, stackup):
        copper = stackup.get("copper", {})
        order = list(stackup.get("layer_order", [])) or sorted(copper)
        substrates = stackup.get("substrate", [])
        specs = []
        for index, layer_id in enumerate(order):
            info = copper.get(layer_id, {})
            specs.append(ThermalLayerSpec(
                name=info.get("name", str(layer_id)),
                thickness_mm=max(0.005, float(info.get("thickness_mm", 0.035))),
                layer_id=layer_id,
                material="copper-layer",
            ))
            if index >= len(order) - 1:
                continue
            next_id = order[index + 1]
            substrate = next((item for item in substrates
                              if item.get("between") == [layer_id, next_id]), None)
            if substrate is None:
                substrate = next((item for item in substrates
                                  if set(item.get("between", [])) == {layer_id, next_id}), None)
            thickness = float(substrate.get("thickness_mm", 0.0)) if substrate else 0.0
            if thickness <= 0:
                thickness = 1.53 / max(1, len(order) - 1)
            specs.append(ThermalLayerSpec(
                name=(substrate or {}).get("material", "FR4"),
                thickness_mm=thickness,
                layer_id=None,
                material="dielectric",
            ))
        if not specs:
            specs = [
                ThermalLayerSpec("F.Cu", 0.035, 0, "copper-layer"),
                ThermalLayerSpec("FR4", 1.53, None, "dielectric"),
                ThermalLayerSpec("B.Cu", 0.035, 31, "copper-layer"),
            ]
        return specs

    @staticmethod
    def _harmonic(k_a, k_b):
        return 2.0 * k_a * k_b / max(k_a + k_b, 1e-30)

    def generate_mesh(self, model, settings: ThermalAnalysisSettings, progress_callback=None):
        if Point is None or prep is None:
            raise ImportError("Shapely is required for thermal meshing.")
        requested_grid = max(0.1, float(settings.grid_size_mm))
        grid = requested_grid
        min_x, min_y, max_x, max_y = model.bounds_mm
        specs = self._layer_specs(model.stackup)
        nx = max(1, int(math.ceil((max_x - min_x) / grid)))
        ny = max(1, int(math.ceil((max_y - min_y) / grid)))
        projected_nodes = nx * ny * len(specs)
        requested_projected_nodes = projected_nodes
        node_limit, cuda_requested = self._node_limit()
        adaptive_grid = False
        if projected_nodes > node_limit:
            grid *= math.sqrt(projected_nodes / float(node_limit)) * 1.02
            nx = max(1, int(math.ceil((max_x - min_x) / grid)))
            ny = max(1, int(math.ceil((max_y - min_y) / grid)))
            projected_nodes = nx * ny * len(specs)
            adaptive_grid = True
            self._log(
                f"Requested {requested_grid:g} mm projects {requested_projected_nodes:,} nodes; "
                f"using {grid:.3g} mm for the {node_limit:,}-node "
                f"{'CUDA' if cuda_requested else 'CPU'} safety budget."
            )

        mesh = ThermalMesh(
            grid_size_mm=grid, requested_grid_size_mm=requested_grid,
            adaptive_grid=adaptive_grid, bounds_mm=model.bounds_mm, layer_specs=specs,
        )
        material = {}
        z_centers = []
        z_cursor = 0.0
        for spec in specs:
            z_centers.append(z_cursor + spec.thickness_mm / 2.0)
            z_cursor += spec.thickness_mm

        node_id = 0
        workers = min(self._worker_count(), len(specs))
        sample_args = [
            (model.outline, model.copper_by_layer.get(spec.layer_id), min_x, min_y, nx, ny, grid)
            for spec in specs
        ]
        if workers > 1 and nx * ny * len(specs) >= 10000:
            self._log(f"Sampling {len(specs)} thermal layers with {workers} CPU workers.")
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="KiPIDA-ThermalMesh") as pool:
                sampled_layers = list(pool.map(lambda args: self._sample_layer(*args), sample_args))
        else:
            sampled_layers = [self._sample_layer(*args) for args in sample_args]
        for iz, spec in enumerate(specs):
            for ix, iy, is_copper in sampled_layers[iz]:
                x = min_x + (ix + 0.5) * grid
                y = min_y + (iy + 0.5) * grid
                if spec.material == "copper-layer" and is_copper:
                    kx = ky = kz = self.COPPER_K
                else:
                    kx = ky = self.FR4_K_XY
                    kz = self.FR4_K_Z
                mesh.nodes.append(node_id)
                mesh.node_map[(ix, iy, iz)] = node_id
                mesh.node_coords[node_id] = (x, y, z_centers[iz])
                mesh.node_layers[node_id] = spec.layer_id
                material[node_id] = (kx, ky, kz, spec.thickness_mm)
                node_id += 1
            if progress_callback:
                progress_callback(iz + 1, len(specs), spec.name)

        dx = dy = grid * 1e-3
        for (ix, iy, iz), current in list(mesh.node_map.items()):
            kx, ky, kz, thickness_mm = material[current]
            dz = thickness_mm * 1e-3
            for delta, axis in (((1, 0, 0), "x"), ((0, 1, 0), "y")):
                neighbor = mesh.node_map.get((ix + delta[0], iy + delta[1], iz))
                if neighbor is None:
                    continue
                nkx, nky, _, _ = material[neighbor]
                conductivity = self._harmonic(kx, nkx) if axis == "x" else self._harmonic(ky, nky)
                area = (dy if axis == "x" else dx) * dz
                distance = dx if axis == "x" else dy
                mesh.branches.append(ThermalBranch(current, neighbor, conductivity * area / distance, "lateral"))
            upper = mesh.node_map.get((ix, iy, iz + 1))
            if upper is not None:
                _, _, upper_kz, upper_thickness_mm = material[upper]
                area = dx * dy
                resistance = (dz / 2.0) / kz + (upper_thickness_mm * 1e-3 / 2.0) / upper_kz
                mesh.branches.append(ThermalBranch(current, upper, area / max(resistance, 1e-30), "vertical"))

        board_thickness_m = max(z_cursor * 1e-3, 1e-6)
        for via in model.vias:
            ix = int((via.x_mm - min_x) / grid)
            iy = int((via.y_mm - min_y) / grid)
            bottom = mesh.node_map.get((ix, iy, 0))
            top = mesh.node_map.get((ix, iy, len(specs) - 1))
            if bottom is None or top is None or bottom == top:
                continue
            plating_mm = 0.025
            area_mm2 = math.pi * max(via.diameter_mm * plating_mm - plating_mm ** 2, 1e-6)
            conductance = self.COPPER_K * area_mm2 * 1e-6 / board_thickness_m
            mesh.branches.append(ThermalBranch(bottom, top, conductance, "via"))

        convective_h = self.convection_coefficient(settings)
        radiative_h = 0.0
        if settings.include_radiation:
            ambient_k = float(settings.ambient_c) + 273.15
            radiative_h = (
                4.0 * max(0.0, min(1.0, float(settings.emissivity))) *
                self.SIGMA * ambient_k ** 3
            )
        h = convective_h + radiative_h
        mesh.convection_coefficient_w_m2k = h
        area_xy = dx * dy
        angle_rad = math.radians(float(settings.airflow.direction_deg))
        flow_x, flow_y = math.cos(angle_rad), math.sin(angle_rad)
        projected_corners = [
            x * flow_x + y * flow_y
            for x in (min_x, max_x) for y in (min_y, max_y)
        ]
        flow_min, flow_max = min(projected_corners), max(projected_corners)
        flow_span = max(flow_max - flow_min, 1e-12)

        def surface_h(node):
            if str(settings.airflow.mode or "").upper() != "FORCED":
                return h
            x, y, _ = mesh.node_coords[node]
            stream_fraction = ((x * flow_x + y * flow_y) - flow_min) / flow_span
            stream_fraction = max(0.0, min(1.0, stream_fraction))
            # Compact flat-plate approximation: stronger transfer at the leading edge.
            return convective_h * (1.25 - 0.5 * stream_fraction) + radiative_h

        for (ix, iy, iz), current in mesh.node_map.items():
            _, _, _, thickness_mm = material[current]
            if iz == 0 and settings.airflow.expose_bottom:
                mesh.boundaries.append(ThermalBoundary(current, surface_h(current) * area_xy, "bottom"))
            if iz == len(specs) - 1 and settings.airflow.expose_top:
                mesh.boundaries.append(ThermalBoundary(current, surface_h(current) * area_xy, "top"))
            if settings.airflow.expose_edges:
                edge_area_x = dy * thickness_mm * 1e-3
                edge_area_y = dx * thickness_mm * 1e-3
                if mesh.node_map.get((ix - 1, iy, iz)) is None:
                    mesh.boundaries.append(ThermalBoundary(current, h * edge_area_x, "edge"))
                if mesh.node_map.get((ix + 1, iy, iz)) is None:
                    mesh.boundaries.append(ThermalBoundary(current, h * edge_area_x, "edge"))
                if mesh.node_map.get((ix, iy - 1, iz)) is None:
                    mesh.boundaries.append(ThermalBoundary(current, h * edge_area_y, "edge"))
                if mesh.node_map.get((ix, iy + 1, iz)) is None:
                    mesh.boundaries.append(ThermalBoundary(current, h * edge_area_y, "edge"))

        for component in model.components:
            if not component.enabled or component.power_w <= 0:
                continue
            placement = model.placements.get(component.ref_des)
            if placement is None:
                continue
            iz = 0 if placement.side == "BOTTOM" else len(specs) - 1
            nodes = []
            for (ix, iy, node_iz), candidate in mesh.node_map.items():
                if node_iz != iz:
                    continue
                x, y, _ = mesh.node_coords[candidate]
                if (abs(x - placement.x_mm) <= placement.width_mm / 2.0 and
                        abs(y - placement.y_mm) <= placement.depth_mm / 2.0):
                    nodes.append(candidate)
            if not nodes:
                nearest = mesh.nearest_node(placement.x_mm, placement.y_mm, specs[iz].layer_id)
                nodes = [nearest] if nearest is not None else []
            mesh.component_nodes[component.ref_des] = nodes
            mesh.component_models[component.ref_des] = component
            for candidate in nodes:
                mesh.add_heat(candidate, component.power_w / len(nodes))

        for loss in model.copper_losses:
            candidate = mesh.nearest_node(loss.x_mm, loss.y_mm, loss.layer_id)
            if candidate is not None and loss.power_w > 0:
                mesh.add_heat(candidate, loss.power_w)

        self._log(
            f"Generated {len(mesh.nodes):,} nodes, {len(mesh.branches):,} branches, "
            f"{sum(mesh.heat_sources_w.values()):.4g} W heat."
        )
        return mesh
