"""Quasi-static electric and magnetic near-field estimates for PCB traces.

This is an engineering pre-compliance model, not a full-wave Maxwell solver.
It treats routed source traces as short line-charge/current elements and
combines sources by root-sum-square because their relative phases are unknown.
"""

from dataclasses import dataclass
import math
import time

import numpy as np

try:
    from .models import EMFieldSimulationResult
    from .runtime_config import load_runtime_settings
except (ImportError, ValueError):
    from models import EMFieldSimulationResult
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


class EMNearFieldSolver:
    """Estimate E/H magnitude on a plane above the board.

    The electric model uses a nominal 100 pF/m trace capacitance to turn each
    configured source voltage into line charge.  The magnetic model integrates
    the Biot-Savart magnitude for the configured trace current.  Return paths,
    phase, dielectric boundaries and enclosure scattering are not solved.
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

    def _elements_for_source(self, source, layer_z, step_mm):
        tracks = self.snapshot.tracks_by_net.get(source.net_name, [])
        elements = []
        maximum_element_mm = max(0.25, min(2.0, step_mm, self.settings.field_probe_height_mm))
        for track in tracks:
            x0, y0 = track.start
            x1, y1 = track.end
            length_mm = max(float(track.length_mm), math.hypot(x1 - x0, y1 - y0))
            if length_mm <= 0.0:
                continue
            count = max(1, min(4096, int(math.ceil(length_mm / maximum_element_mm))))
            dx_m = (x1 - x0) * 1.0e-3 / count
            dy_m = (y1 - y0) * 1.0e-3 / count
            dz_m = 0.0
            z_m = layer_z.get(int(track.layer_id), 0.0)
            element_length_m = math.sqrt(dx_m * dx_m + dy_m * dy_m)
            for index in range(count):
                fraction = (index + 0.5) / count
                elements.append(_FieldElement(
                    ((x0 + fraction * (x1 - x0)) * 1.0e-3,
                     (y0 + fraction * (y1 - y0)) * 1.0e-3, z_m),
                    (dx_m, dy_m, dz_m),
                    element_length_m,
                ))
        return elements

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
        x_values = np.arange(min_x, max_x + step_mm * 0.5, step_mm, dtype=np.float64)
        y_values = np.arange(min_y, max_y + step_mm * 0.5, step_mm, dtype=np.float64)
        cell_count = int(x_values.size * y_values.size)
        maximum_cells = max(100, int(self.settings.field_maximum_cells))
        if cell_count > maximum_cells:
            required_step = math.sqrt((max_x - min_x) * (max_y - min_y) / maximum_cells)
            raise ValueError(
                f"Near-field grid would contain {cell_count:,} cells; increase the grid size "
                f"to at least {required_step:.3f} mm (limit {maximum_cells:,})."
            )

        enabled_sources = [source for source in self.settings.sources if source.enabled]
        layer_z = self._layer_z_m()
        prepared = []
        warnings = []
        frequency_hz = max(0.0, float(self.settings.field_frequency_hz))
        for source in enabled_sources:
            elements = self._elements_for_source(source, layer_z, step_mm)
            factor = self._source_factor(source, frequency_hz)
            if not elements:
                warnings.append(f"No routed segment found for source net {source.net_name}.")
                continue
            if factor <= 0.0:
                warnings.append(f"Source {source.name} has no usable frequency for the selected field map.")
                continue
            prepared.append((source, factor, elements))
        if not prepared:
            raise ValueError("No enabled EMI/EMC source has routed copper available for field simulation.")

        xp, backend = self._array_backend(cell_count)
        self.log(
            f"[EM FIELD] Solving {cell_count:,} observation cells at {height_mm:g} mm "
            f"with {sum(len(item[2]) for item in prepared):,} trace elements on {backend}."
        )
        grid_x, grid_y = np.meshgrid(x_values * 1.0e-3, y_values * 1.0e-3)
        flat_x = grid_x.ravel()
        flat_y = grid_y.ravel()
        e_squared = xp.zeros(cell_count, dtype=xp.float64)
        h_squared = xp.zeros(cell_count, dtype=xp.float64)
        observation_z = height_mm * 1.0e-3
        cell_batch = 8192 if xp is np else 32768
        element_batch = 64

        for source_index, (source, factor, elements) in enumerate(prepared, start=1):
            source_e = xp.zeros(cell_count, dtype=xp.float64)
            source_h = xp.zeros(cell_count, dtype=xp.float64)
            voltage = max(0.0, float(source.voltage_swing_v)) * factor
            current = max(0.0, float(source.current_a)) * factor
            midpoint = np.asarray([item.midpoint_m for item in elements], dtype=np.float64)
            dl = np.asarray([item.direction_length_m for item in elements], dtype=np.float64)
            lengths = np.asarray([item.length_m for item in elements], dtype=np.float64)
            for cell_start in range(0, cell_count, cell_batch):
                cell_stop = min(cell_count, cell_start + cell_batch)
                ox = xp.asarray(flat_x[cell_start:cell_stop])[:, None]
                oy = xp.asarray(flat_y[cell_start:cell_stop])[:, None]
                accumulated_e = xp.zeros(cell_stop - cell_start, dtype=xp.float64)
                accumulated_h = xp.zeros(cell_stop - cell_start, dtype=xp.float64)
                for element_start in range(0, len(elements), element_batch):
                    element_stop = min(len(elements), element_start + element_batch)
                    mid = xp.asarray(midpoint[element_start:element_stop])
                    line = xp.asarray(dl[element_start:element_stop])
                    length = xp.asarray(lengths[element_start:element_stop])
                    rx = ox - mid[None, :, 0]
                    ry = oy - mid[None, :, 1]
                    rz = observation_z - mid[None, :, 2]
                    radius_squared = xp.maximum(rx * rx + ry * ry + rz * rz, 1.0e-18)
                    inverse_radius = 1.0 / xp.sqrt(radius_squared)
                    charge = LINE_CAPACITANCE_F_M * voltage * length[None, :]
                    accumulated_e += xp.sum(
                        ELECTRIC_CONSTANT * charge / radius_squared, axis=1,
                    )
                    cross_x = line[None, :, 1] * rz
                    cross_y = -line[None, :, 0] * rz
                    cross_z = line[None, :, 0] * ry - line[None, :, 1] * rx
                    cross_magnitude = xp.sqrt(cross_x * cross_x + cross_y * cross_y + cross_z * cross_z)
                    accumulated_h += xp.sum(
                        MAGNETIC_CONSTANT * current * cross_magnitude * inverse_radius / radius_squared,
                        axis=1,
                    )
                source_e[cell_start:cell_stop] = accumulated_e
                source_h[cell_start:cell_stop] = accumulated_h
            e_squared += source_e * source_e
            h_squared += source_h * source_h
            if progress_callback:
                progress_callback(source_index, len(prepared), source.name)

        e_field = self._to_numpy(xp.sqrt(e_squared), xp).reshape(y_values.size, x_values.size)
        h_field = self._to_numpy(xp.sqrt(h_squared), xp).reshape(y_values.size, x_values.size)
        e_index = np.unravel_index(int(np.argmax(e_field)), e_field.shape)
        h_index = np.unravel_index(int(np.argmax(h_field)), h_field.shape)
        result = EMFieldSimulationResult(
            x_coordinates_mm=x_values.tolist(),
            y_coordinates_mm=y_values.tolist(),
            electric_field_v_m=e_field.tolist(),
            magnetic_field_a_m=h_field.tolist(),
            probe_height_mm=height_mm,
            frequency_hz=frequency_hz,
            frequency_mode="SELECTED_ENVELOPE" if frequency_hz > 0.0 else "SOURCE_FUNDAMENTALS",
            source_count=len(prepared),
            segment_count=sum(len(item[2]) for item in prepared),
            maximum_e_v_m=float(e_field[e_index]),
            maximum_h_a_m=float(h_field[h_index]),
            maximum_e_position_mm=(float(x_values[e_index[1]]), float(y_values[e_index[0]])),
            maximum_h_position_mm=(float(x_values[h_index[1]]), float(y_values[h_index[0]])),
            compute_backend=backend,
            elapsed_seconds=time.perf_counter() - started,
            warnings=warnings,
        )
        self.log(
            f"[EM FIELD] Complete in {result.elapsed_seconds:.3f} s: "
            f"Emax={result.maximum_e_v_m:.4g} V/m, Hmax={result.maximum_h_a_m:.4g} A/m."
        )
        return result
