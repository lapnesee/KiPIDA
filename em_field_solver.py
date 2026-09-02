"""Quasi-static electric and magnetic near-field estimates for PCB traces.

This is an engineering pre-compliance model, not a full-wave Maxwell solver.
It treats routed source traces as short line-charge/current elements and
combines sources by root-sum-square because their relative phases are unknown.
"""

from dataclasses import dataclass
from itertools import combinations
import math
import time

import numpy as np

try:
    from shapely.geometry import LineString, Point
    from shapely.prepared import prep as prepare_geometry
except ImportError:  # pragma: no cover - field solve remains usable without Shapely
    LineString = Point = None
    prepare_geometry = None

try:
    from .inductor_em import triangular_harmonic_peak, TargetedInductorRefiner
    from .models import (
        EMFieldSimulationResult, EMFieldSourceContribution,
        EMInductorFieldContribution,
    )
    from .runtime_config import load_runtime_settings
except (ImportError, ValueError):
    from inductor_em import triangular_harmonic_peak, TargetedInductorRefiner
    from models import (
        EMFieldSimulationResult, EMFieldSourceContribution,
        EMInductorFieldContribution,
    )
    from runtime_config import load_runtime_settings


EPSILON_0 = 8.8541878128e-12
LINE_CAPACITANCE_F_M = 100.0e-12
ELECTRIC_CONSTANT = 1.0 / (4.0 * math.pi * EPSILON_0)
MAGNETIC_CONSTANT = 1.0 / (4.0 * math.pi)


@dataclass(frozen=True)
class _FieldElement:
    midpoint_m: tuple
    direction_length_m: tuple
    length_m: float
    polarity: float = 1.0


class EMNearFieldSolver:
    """Estimate E/H magnitude on a plane above the board.

    The electric model uses a nominal 100 pF/m trace capacitance to turn each
    configured source voltage into line charge.  The magnetic model integrates
    vector Biot-Savart contributions for the configured trace current.  A
    continuous adjacent GND zone is represented by an ideal image return and
    differential conductors use opposite polarity.  Finite-plane current
    spreading, phase, dielectric boundaries and enclosure scattering are not solved.
    """

    def __init__(self, snapshot, settings, runtime_settings=None, log_callback=None):
        self.snapshot = snapshot
        self.settings = settings
        self.runtime_settings = runtime_settings or load_runtime_settings()
        self.log_callback = log_callback

    def log(self, message):
        if self.log_callback:
            self.log_callback(message)

    def _layer_z_m(self):
        positions = {}
        cursor_mm = 0.0
        for layer in getattr(self.snapshot.stackup, "layers", []) or []:
            thickness_mm = max(0.0, float(getattr(layer, "thickness_mm", 0.0)))
            layer_id = getattr(layer, "layer_id", None)
            if str(getattr(layer, "kind", "")).upper() == "COPPER" and layer_id is not None:
                positions[int(layer_id)] = -(cursor_mm + thickness_mm / 2.0) * 1.0e-3
            cursor_mm += thickness_mm
        return positions

    @staticmethod
    def _source_factor(source, requested_frequency_hz):
        fundamental = max(0.0, float(source.frequency_hz))
        rise_s = max(1.0e-15, float(source.rise_time_ns) * 1.0e-9)
        if requested_frequency_hz <= 0.0:
            frequency = fundamental
            harmonic = 1
        elif fundamental > 0.0:
            frequency = float(requested_frequency_hz)
            harmonic = max(1, int(round(frequency / fundamental)))
        else:
            return 0.0
        edge_bandwidth = 0.35 / rise_s
        rolloff = 1.0 / math.sqrt(1.0 + (frequency / edge_bandwidth) ** 4)
        return rolloff / harmonic

    def _reference_planes(self, layer_z):
        planes = []
        if Point is None or prepare_geometry is None:
            return planes
        for net_name in getattr(self.settings, "reference_net_names", []) or []:
            for layer_id, geometry in self.snapshot.zones_by_net.get(net_name, {}).items():
                reference_z = layer_z.get(int(layer_id))
                if reference_z is not None and geometry is not None and not geometry.is_empty:
                    planes.append((int(layer_id), reference_z, prepare_geometry(geometry)))
        return planes

    @staticmethod
    def _reference_image_z(layer_id, midpoint_mm, layer_z, reference_planes):
        source_z = layer_z.get(int(layer_id))
        if source_z is None:
            return None
        candidates = []
        point = Point(*midpoint_mm) if Point is not None else None
        for reference_layer, reference_z, prepared_geometry in reference_planes:
            if reference_layer == int(layer_id) or point is None:
                continue
            if prepared_geometry.covers(point):
                candidates.append((abs(reference_z - source_z), reference_z))
        if not candidates:
            return None
        plane_z = min(candidates)[1]
        return 2.0 * plane_z - source_z

    def _fallback_pad_path(self, net_name):
        points = []
        for footprint in getattr(self.snapshot, "footprints", []) or []:
            for entry in getattr(footprint, "net_positions", ()) or ():
                if len(entry) >= 3 and entry[0] == net_name:
                    point = (float(entry[1]), float(entry[2]))
                    if point not in points:
                        points.append(point)
        zones = self.snapshot.zones_by_net.get(net_name, {})
        if len(points) < 2 or not zones or LineString is None:
            return None
        candidates = []
        for start, end in combinations(points, 2):
            line = LineString([start, end])
            if line.length <= 0.0:
                continue
            for layer_id, geometry in zones.items():
                if geometry is None or geometry.is_empty:
                    continue
                coverage = float(line.intersection(geometry).length) / float(line.length)
                candidates.append((coverage, float(line.length), start, end, int(layer_id)))
        if not candidates:
            return None
        coverage, _length, start, end, layer_id = max(
            candidates, key=lambda item: (item[0], item[1])
        )
        # Do not fabricate a radiating diagonal across disconnected pads or
        # separate copper islands merely because they share a net name.
        if coverage < 0.8:
            return None
        return start, end, layer_id, coverage

    def _elements_for_source(self, source, layer_z, reference_planes, step_mm):
        routes = []
        for net_name, polarity in (
            (source.net_name, 1.0),
            (getattr(source, "negative_net_name", ""), -1.0),
        ):
            if not net_name:
                continue
            for track in self.snapshot.tracks_by_net.get(net_name, []):
                routes.append((net_name, track.start, track.end, track.layer_id,
                               track.length_mm, polarity, "ROUTED_TRACKS"))
        geometry_source = "ROUTED_TRACKS"
        if not routes and str(source.kind).upper() == "SWITCHING":
            fallback = self._fallback_pad_path(source.net_name)
            if fallback is not None:
                start, end, layer_id, _coverage = fallback
                routes.append((source.net_name, start, end, layer_id,
                               math.dist(start, end), 1.0, "PAD_TO_PAD_ZONE_PATH"))
                geometry_source = "PAD_TO_PAD_ZONE_PATH"
        elements = []
        segments = []
        maximum_element_mm = max(0.25, min(2.0, step_mm, self.settings.field_probe_height_mm))
        for net_name, start, end, layer_id, route_length, polarity, route_source in routes:
            x0, y0 = start
            x1, y1 = end
            length_mm = max(float(route_length), math.hypot(x1 - x0, y1 - y0))
            if length_mm <= 0.0:
                continue
            segments.append((source.name, net_name, x0, y0, x1, y1, polarity, route_source))
            count = max(1, min(4096, int(math.ceil(length_mm / maximum_element_mm))))
            dx_m = (x1 - x0) * 1.0e-3 / count
            dy_m = (y1 - y0) * 1.0e-3 / count
            dz_m = 0.0
            z_m = layer_z.get(int(layer_id), 0.0)
            element_length_m = math.sqrt(dx_m * dx_m + dy_m * dy_m)
            for index in range(count):
                fraction = (index + 0.5) / count
                midpoint_mm = (
                    x0 + fraction * (x1 - x0),
                    y0 + fraction * (y1 - y0),
                )
                elements.append(_FieldElement(
                    (midpoint_mm[0] * 1.0e-3, midpoint_mm[1] * 1.0e-3, z_m),
                    (dx_m, dy_m, dz_m),
                    element_length_m,
                    polarity,
                ))
                image_z = self._reference_image_z(
                    layer_id, midpoint_mm, layer_z, reference_planes,
                )
                if image_z is not None:
                    elements.append(_FieldElement(
                        (midpoint_mm[0] * 1.0e-3, midpoint_mm[1] * 1.0e-3, image_z),
                        (dx_m, dy_m, dz_m), element_length_m, -polarity,
                    ))
        return elements, segments, geometry_source

    def _inductors_for_source(self, source):
        footprints = {
            item.reference.upper(): item
            for item in getattr(self.snapshot, "footprints", []) or []
        }
        matches = []
        for model in getattr(self.snapshot, "inductors", []) or []:
            if not model.enabled:
                continue
            linked = (
                (model.source_name and model.source_name == source.name)
                or (model.switching_net and model.switching_net == source.net_name)
            )
            footprint = footprints.get(model.ref_des.upper())
            if linked and footprint is not None:
                matches.append((model, footprint))
        return matches

    def _array_backend(self, cell_count):
        requested = str(getattr(self.runtime_settings, "backend", "AUTO") or "AUTO").upper()
        enabled = bool(getattr(self.runtime_settings, "cuda_enabled", False))
        threshold = int(getattr(self.runtime_settings, "cuda_min_nodes", 100000) or 100000)
        if enabled and requested != "CPU" and (requested == "CUDA" or cell_count >= threshold):
            try:
                import cupy as cp
                device = int(getattr(self.runtime_settings, "cuda_device", 0) or 0)
                cp.cuda.Device(device).use()
                return cp, f"CUDA_CUPY (GPU {device})"
            except Exception as exc:
                self.log(f"[EM FIELD] CUDA unavailable; using NumPy: {exc}")
        return np, "CPU_NUMPY"

    @staticmethod
    def _to_numpy(array, xp):
        return np.asarray(array) if xp is np else xp.asnumpy(array)

    def solve(self, progress_callback=None):
        started = time.perf_counter()
        height_mm = float(self.settings.field_probe_height_mm)
        step_mm = float(self.settings.field_grid_size_mm)
        if height_mm <= 0.0:
            raise ValueError("Near-field probe height must be positive.")
        if step_mm <= 0.0:
            raise ValueError("Near-field grid size must be positive.")
        min_x, min_y, max_x, max_y = map(float, self.snapshot.bounds_mm)
        if max_x <= min_x or max_y <= min_y:
            raise ValueError("The PCB outline is empty; near-field simulation cannot build a grid.")
        maximum_cells = max(100, int(self.settings.field_maximum_cells))
        requested_step_mm = step_mm

        def build_axes(candidate_step):
            return (
                np.arange(min_x, max_x + candidate_step * 0.5, candidate_step, dtype=np.float64),
                np.arange(min_y, max_y + candidate_step * 0.5, candidate_step, dtype=np.float64),
            )

        x_values, y_values = build_axes(step_mm)
        cell_count = int(x_values.size * y_values.size)
        warnings = []
        if cell_count > maximum_cells:
            requested_cells = cell_count
            # Account for inclusive end points, then iterate to guarantee the
            # configured memory ceiling even on very elongated boards.
            step_mm *= math.sqrt(cell_count / maximum_cells) * 1.002
            for _ in range(12):
                x_values, y_values = build_axes(step_mm)
                cell_count = int(x_values.size * y_values.size)
                if cell_count <= maximum_cells:
                    break
                step_mm *= math.sqrt(cell_count / maximum_cells) * 1.002
            if cell_count > maximum_cells:
                raise ValueError(
                    f"Near-field grid cannot be reduced below the {maximum_cells:,}-cell limit."
                )
            warning = (
                f"Requested {requested_step_mm:g} mm grid would contain {requested_cells:,} cells; "
                f"automatically coarsened to {step_mm:.4g} mm ({cell_count:,} cells, "
                f"limit {maximum_cells:,})."
            )
            warnings.append(warning)
            self.log(f"[EM FIELD] {warning}")

        enabled_sources = [source for source in self.settings.sources if source.enabled]
        layer_z = self._layer_z_m()
        reference_planes = self._reference_planes(layer_z)
        prepared = []
        frequency_hz = max(0.0, float(self.settings.field_frequency_hz))
        for source in enabled_sources:
            elements, segments, geometry_source = self._elements_for_source(
                source, layer_z, reference_planes, step_mm,
            )
            fundamental = max(0.0, float(source.frequency_hz))
            if frequency_hz > 0.0:
                analyzed_frequency = frequency_hz
            elif fundamental > 0.0:
                start = max(0.0, float(self.settings.frequency_start_hz))
                harmonic = max(1, int(math.ceil(start / fundamental)))
                analyzed_frequency = harmonic * fundamental
            else:
                analyzed_frequency = 0.0
            if (
                frequency_hz <= 0.0
                and analyzed_frequency > float(self.settings.frequency_stop_hz)
            ):
                warnings.append(
                    f"Source {source.name} has no fundamental/harmonic inside the selected "
                    "compliance band."
                )
                continue
            harmonic_number = (
                max(1, int(round(analyzed_frequency / fundamental)))
                if fundamental > 0.0 and analyzed_frequency > 0.0 else 1
            )
            factor = self._source_factor(source, analyzed_frequency)
            inductors = self._inductors_for_source(source)
            if not elements and not inductors:
                warnings.append(f"No routed segment found for source net {source.net_name}.")
                continue
            if factor <= 0.0:
                warnings.append(f"Source {source.name} has no usable frequency for the selected field map.")
                continue
            if geometry_source == "PAD_TO_PAD_ZONE_PATH":
                warnings.append(
                    f"Source {source.name} has no routed track segment; field geometry uses "
                    "a pad-to-pad path verified to remain at least 80% inside one copper zone."
                )
            if not elements and inductors:
                geometry_source = "INDUCTOR_ONLY"
                warnings.append(
                    f"Source {source.name} has no routed segment; only its configured "
                    "inductor magnetic model is evaluated."
                )
            prepared.append((
                source, factor, elements, segments, geometry_source,
                analyzed_frequency, harmonic_number, inductors,
            ))
        if not prepared:
            raise ValueError("No enabled EMI/EMC source has usable conductor or inductor geometry.")

        xp, backend = self._array_backend(cell_count)
        self.log(
            f"[EM FIELD] Solving {cell_count:,} observation cells at {height_mm:g} mm "
            f"with {sum(len(item[2]) for item in prepared):,} conductor/image elements on {backend}."
        )
        grid_x, grid_y = np.meshgrid(x_values * 1.0e-3, y_values * 1.0e-3)
        flat_x = grid_x.ravel()
        flat_y = grid_y.ravel()
        e_squared = xp.zeros(cell_count, dtype=xp.float64)
        h_squared = xp.zeros(cell_count, dtype=xp.float64)
        observation_z = height_mm * 1.0e-3
        cell_batch = 8192 if xp is np else 32768
        element_batch = 64

        contribution_data = []
        inductor_contribution_data = []
        all_source_segments = []
        for source_index, prepared_source in enumerate(prepared, start=1):
            (source, factor, elements, segments, geometry_source,
             analyzed_frequency, harmonic_number, source_inductors) = prepared_source
            source_e = xp.zeros(cell_count, dtype=xp.float64)
            source_h = xp.zeros(cell_count, dtype=xp.float64)
            voltage = max(0.0, float(source.voltage_swing_v)) * factor
            current = max(0.0, float(source.current_a)) * factor
            midpoint = np.asarray([item.midpoint_m for item in elements], dtype=np.float64)
            dl = np.asarray([item.direction_length_m for item in elements], dtype=np.float64)
            lengths = np.asarray([item.length_m for item in elements], dtype=np.float64)
            polarities = np.asarray([item.polarity for item in elements], dtype=np.float64)
            inductor_fields = [
                (model, footprint, xp.zeros(cell_count, dtype=xp.float64))
                for model, footprint in source_inductors
            ]
            for cell_start in range(0, cell_count, cell_batch):
                cell_stop = min(cell_count, cell_start + cell_batch)
                ox = xp.asarray(flat_x[cell_start:cell_stop])[:, None]
                oy = xp.asarray(flat_y[cell_start:cell_stop])[:, None]
                accumulated_e = xp.zeros((cell_stop - cell_start, 3), dtype=xp.float64)
                accumulated_h = xp.zeros((cell_stop - cell_start, 3), dtype=xp.float64)
                for element_start in range(0, len(elements), element_batch):
                    element_stop = min(len(elements), element_start + element_batch)
                    mid = xp.asarray(midpoint[element_start:element_stop])
                    line = xp.asarray(dl[element_start:element_stop])
                    length = xp.asarray(lengths[element_start:element_stop])
                    polarity = xp.asarray(polarities[element_start:element_stop])
                    rx = ox - mid[None, :, 0]
                    ry = oy - mid[None, :, 1]
                    rz = observation_z - mid[None, :, 2]
                    radius_squared = xp.maximum(rx * rx + ry * ry + rz * rz, 1.0e-18)
                    inverse_radius = 1.0 / xp.sqrt(radius_squared)
                    charge = LINE_CAPACITANCE_F_M * voltage * length[None, :] * polarity[None, :]
                    electric_scale = ELECTRIC_CONSTANT * charge * inverse_radius / radius_squared
                    accumulated_e[:, 0] += xp.sum(electric_scale * rx, axis=1)
                    accumulated_e[:, 1] += xp.sum(electric_scale * ry, axis=1)
                    accumulated_e[:, 2] += xp.sum(electric_scale * rz, axis=1)
                    cross_x = line[None, :, 1] * rz
                    cross_y = -line[None, :, 0] * rz
                    cross_z = line[None, :, 0] * ry - line[None, :, 1] * rx
                    magnetic_scale = (
                        MAGNETIC_CONSTANT * current * polarity[None, :]
                        * inverse_radius / radius_squared
                    )
                    accumulated_h[:, 0] += xp.sum(magnetic_scale * cross_x, axis=1)
                    accumulated_h[:, 1] += xp.sum(magnetic_scale * cross_y, axis=1)
                    accumulated_h[:, 2] += xp.sum(magnetic_scale * cross_z, axis=1)
                for model, footprint, field_storage in inductor_fields:
                    duty = (
                        model.vout_v / model.vin_v
                        if model.vin_v > 0.0 else 0.5
                    )
                    harmonic_current = triangular_harmonic_peak(
                        model.ripple_current_pp_a, harmonic_number, duty,
                    )
                    attenuation = (
                        10.0 ** (-float(model.shielding_attenuation_db) / 20.0)
                        if model.shielding_attenuation_db is not None else 1.0
                    )
                    # Transparent one-turn package-area equivalent.  It is an
                    # uncertainty estimate, not a reconstruction of the hidden winding.
                    area_m2 = max(0.0, model.width_mm * model.depth_mm) * 1.0e-6
                    moment = harmonic_current * area_m2 * attenuation
                    source_z = 0.5 * max(0.0, model.height_mm) * 1.0e-3
                    rx_i = ox[:, 0] - float(footprint.position[0]) * 1.0e-3
                    ry_i = oy[:, 0] - float(footprint.position[1]) * 1.0e-3
                    rz_i = observation_z - source_z
                    radius_sq = rx_i * rx_i + ry_i * ry_i + rz_i * rz_i
                    minimum_radius = max(
                        0.25 * min(model.width_mm or 1.0, model.depth_mm or 1.0) * 1.0e-3,
                        0.25e-3,
                    )
                    radius_sq = xp.maximum(radius_sq, minimum_radius * minimum_radius)
                    inverse_r = 1.0 / xp.sqrt(radius_sq)
                    inverse_r3 = inverse_r / radius_sq
                    inverse_r5 = inverse_r3 / radius_sq
                    scale = MAGNETIC_CONSTANT * moment
                    hx_i = scale * 3.0 * rz_i * rx_i * inverse_r5
                    hy_i = scale * 3.0 * rz_i * ry_i * inverse_r5
                    hz_i = scale * (3.0 * rz_i * rz_i * inverse_r5 - inverse_r3)
                    accumulated_h[:, 0] += hx_i
                    accumulated_h[:, 1] += hy_i
                    accumulated_h[:, 2] += hz_i
                    field_storage[cell_start:cell_stop] = xp.sqrt(
                        hx_i * hx_i + hy_i * hy_i + hz_i * hz_i
                    )
                source_e[cell_start:cell_stop] = xp.sqrt(xp.sum(accumulated_e * accumulated_e, axis=1))
                source_h[cell_start:cell_stop] = xp.sqrt(xp.sum(accumulated_h * accumulated_h, axis=1))
            e_squared += source_e * source_e
            h_squared += source_h * source_h
            source_e_np = self._to_numpy(source_e, xp)
            source_h_np = self._to_numpy(source_h, xp)
            source_e_index = int(np.argmax(source_e_np))
            source_h_index = int(np.argmax(source_h_np))
            e_y, e_x = np.unravel_index(source_e_index, (y_values.size, x_values.size))
            h_y, h_x = np.unravel_index(source_h_index, (y_values.size, x_values.size))
            contribution_data.append(EMFieldSourceContribution(
                source_name=source.name,
                net_names=tuple(filter(None, (source.net_name, getattr(source, "negative_net_name", "")))),
                geometry_source=geometry_source,
                geometry_confidence=(
                    "HIGH" if geometry_source == "ROUTED_TRACKS" else
                    "MEDIUM" if geometry_source == "PAD_TO_PAD_ZONE_PATH" else "LOW"
                ),
                maximum_e_v_m=float(source_e_np[source_e_index]),
                maximum_h_a_m=float(source_h_np[source_h_index]),
                maximum_e_position_mm=(float(x_values[e_x]), float(y_values[e_y])),
                maximum_h_position_mm=(float(x_values[h_x]), float(y_values[h_y])),
                analyzed_frequency_hz=analyzed_frequency,
                harmonic_number=harmonic_number,
            ))
            for model, _footprint, field_storage in inductor_fields:
                field_np = self._to_numpy(field_storage, xp)
                field_index = int(np.argmax(field_np))
                field_y, field_x = np.unravel_index(
                    field_index, (y_values.size, x_values.size),
                )
                duty = model.vout_v / model.vin_v if model.vin_v > 0.0 else 0.5
                inductor_contribution_data.append(EMInductorFieldContribution(
                    ref_des=model.ref_des,
                    mpn=model.mpn,
                    source_name=source.name,
                    model_level=model.model_level,
                    shield_state=model.shield_state,
                    attenuation_applied_db=model.shielding_attenuation_db,
                    harmonic_number=harmonic_number,
                    analyzed_frequency_hz=analyzed_frequency,
                    ripple_current_pp_a=model.ripple_current_pp_a,
                    harmonic_current_peak_a=triangular_harmonic_peak(
                        model.ripple_current_pp_a, harmonic_number, duty,
                    ),
                    maximum_h_a_m=float(field_np[field_index]),
                    maximum_h_position_mm=(float(x_values[field_x]), float(y_values[field_y])),
                    parameter_confidence=model.parameter_confidence,
                    model_confidence=(
                        "HIGH" if model.shielding_attenuation_db is not None
                        and model.ripple_current_pp_a > 0.0 else "LOW"
                    ),
                    parameter_reference=model.parameter_reference,
                    refinement_status=TargetedInductorRefiner.status(model),
                ))
            all_source_segments.extend(segments)
            if progress_callback:
                progress_callback(source_index, len(prepared), source.name)

        e_field = self._to_numpy(xp.sqrt(e_squared), xp).reshape(y_values.size, x_values.size)
        h_field = self._to_numpy(xp.sqrt(h_squared), xp).reshape(y_values.size, x_values.size)
        e_index = np.unravel_index(int(np.argmax(e_field)), e_field.shape)
        h_index = np.unravel_index(int(np.argmax(h_field)), h_field.shape)
        for item in contribution_data:
            item.relative_e_pct = 100.0 * item.maximum_e_v_m / max(float(e_field[e_index]), 1e-30)
            item.relative_h_pct = 100.0 * item.maximum_h_a_m / max(float(h_field[h_index]), 1e-30)
        result = EMFieldSimulationResult(
            x_coordinates_mm=x_values.tolist(),
            y_coordinates_mm=y_values.tolist(),
            electric_field_v_m=e_field.tolist(),
            magnetic_field_a_m=h_field.tolist(),
            probe_height_mm=height_mm,
            requested_grid_size_mm=requested_step_mm,
            effective_grid_size_mm=step_mm,
            frequency_hz=frequency_hz,
            frequency_mode=("SELECTED_ENVELOPE" if frequency_hz > 0.0
                            else "FIRST_IN_BAND_HARMONICS"),
            source_count=len(prepared),
            segment_count=sum(len(item[2]) for item in prepared),
            maximum_e_v_m=float(e_field[e_index]),
            maximum_h_a_m=float(h_field[h_index]),
            maximum_e_position_mm=(float(x_values[e_index[1]]), float(y_values[e_index[0]])),
            maximum_h_position_mm=(float(x_values[h_index[1]]), float(y_values[h_index[0]])),
            source_contributions=contribution_data,
            inductor_contributions=inductor_contribution_data,
            source_segments=all_source_segments,
            compute_backend=backend,
            elapsed_seconds=time.perf_counter() - started,
            warnings=warnings,
        )
        self.log(
            f"[EM FIELD] Complete in {result.elapsed_seconds:.3f} s: "
            f"Emax={result.maximum_e_v_m:.4g} V/m, Hmax={result.maximum_h_a_m:.4g} A/m."
        )
        return result
