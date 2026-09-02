"""Differential transmission-line geometry definitions and CPWG solver.

The existing Ki-PIDA differential model uses fast closed-form microstrip and
stripline estimates. Grounded coplanar differential pairs need an additional
same-layer ground gap, so this module provides the shared UI identifiers and a
small two-dimensional electrostatic field solver for that geometry.
"""

from dataclasses import dataclass
from functools import lru_cache
import math

import numpy as np


GEOMETRY_AUTO = "AUTO"
GEOMETRY_JLCPCB_NON_COPLANAR = "JLCPCB_NON_COPLANAR"
GEOMETRY_JLCPCB_COPLANAR = "JLCPCB_COPLANAR"
GEOMETRY_MICROSTRIP = "MICROSTRIP"
GEOMETRY_STRIPLINE = "STRIPLINE"

GEOMETRY_CHOICES = (
    (GEOMETRY_AUTO, "Automatic from layer/reference planes"),
    (GEOMETRY_JLCPCB_NON_COPLANAR, "JLCPCB - Differential pair (non-coplanar)"),
    (GEOMETRY_JLCPCB_COPLANAR, "JLCPCB - Coplanar differential pair"),
    (GEOMETRY_MICROSTRIP, "Edge-coupled differential microstrip"),
    (GEOMETRY_STRIPLINE, "Edge-coupled differential stripline"),
)
GEOMETRY_LABELS = dict(GEOMETRY_CHOICES)
SUPPORTED_GEOMETRIES = frozenset(GEOMETRY_LABELS)
COPLANAR_GEOMETRIES = frozenset({GEOMETRY_JLCPCB_COPLANAR})


def normalize_geometry(value):
    value = str(value or GEOMETRY_AUTO).strip().upper()
    return value if value in SUPPORTED_GEOMETRIES else GEOMETRY_AUTO


def geometry_label(value):
    return GEOMETRY_LABELS.get(normalize_geometry(value), GEOMETRY_LABELS[GEOMETRY_AUTO])


@dataclass(frozen=True)
class CoplanarSolveResult:
    differential_impedance_ohm: float
    odd_mode_impedance_ohm: float
    effective_epsilon_r: float
    grid_columns: int
    grid_rows: int


class GroundedCoplanarDifferentialSolver:
    """Quasi-static 2-D odd-mode solver for a grounded coplanar pair."""

    EPSILON_0 = 8.8541878128e-12
    LIGHT_SPEED_M_S = 299792458.0

    @staticmethod
    def _piecewise_grid(boundaries, intervals):
        values = []
        for index, count in enumerate(intervals):
            section = np.linspace(boundaries[index], boundaries[index + 1], count + 1)
            values.extend(section[:-1])
        values.append(boundaries[-1])
        return np.asarray(values, dtype=np.float64)

    @staticmethod
    def _dielectric_map(y_values, signal_y, conductor_top_y, top_reference_y,
                        epsilon_below, epsilon_above,
                        include_solder_mask, solder_mask_thickness, solder_mask_epsilon):
        epsilon = np.ones(len(y_values), dtype=np.float64)
        epsilon[y_values < signal_y] = epsilon_below
        if top_reference_y is not None:
            epsilon[y_values > conductor_top_y] = epsilon_above
        elif include_solder_mask and solder_mask_thickness > 0.0:
            mask = (y_values >= conductor_top_y) & (
                y_values <= conductor_top_y + solder_mask_thickness
            )
            epsilon[mask] = solder_mask_epsilon
        return epsilon

    @classmethod
    def _solve_potential(cls, x_values, y_values, epsilon_rows, fixed, fixed_values):
        """Solve the non-uniform finite-volume grid with red-black SOR."""
        ny, nx = fixed.shape
        potential = np.array(fixed_values, copy=True)
        yy, xx = np.indices((ny - 2, nx - 2))
        free = ~fixed[1:-1, 1:-1]
        red = free & ((xx + yy) % 2 == 0)
        black = free & ~red
        dx_left = x_values[1:-1] - x_values[:-2]
        dx_right = x_values[2:] - x_values[1:-1]
        dx_control = 0.5 * (dx_left + dx_right)
        dy_down = y_values[1:-1] - y_values[:-2]
        dy_up = y_values[2:] - y_values[1:-1]
        dy_control = 0.5 * (dy_down + dy_up)
        horizontal_left = epsilon_rows[1:-1, None] * dy_control[:, None] / dx_left[None, :]
        horizontal_right = epsilon_rows[1:-1, None] * dy_control[:, None] / dx_right[None, :]
        downward = (0.5 * (epsilon_rows[1:-1] + epsilon_rows[:-2])[:, None]
                    * dx_control[None, :] / dy_down[:, None])
        upward = (0.5 * (epsilon_rows[1:-1] + epsilon_rows[2:])[:, None]
                  * dx_control[None, :] / dy_up[:, None])
        denominator = horizontal_left + horizontal_right + downward + upward
        relaxation = 1.82
        for _iteration in range(3000):
            maximum_delta = 0.0
            for color in (red, black):
                center = potential[1:-1, 1:-1]
                target = (horizontal_left * potential[1:-1, :-2]
                          + horizontal_right * potential[1:-1, 2:]
                          + downward * potential[:-2, 1:-1]
                          + upward * potential[2:, 1:-1]) / denominator
                delta = relaxation * (target - center)
                if np.any(color):
                    maximum_delta = max(maximum_delta, float(np.max(np.abs(delta[color]))))
                    center[color] += delta[color]
            potential[:, 0] = potential[:, 1]
            potential[:, -1] = potential[:, -2]
            if not np.any(fixed[0, :]):
                potential[0, :] = potential[1, :]
            if not np.any(fixed[-1, :]):
                potential[-1, :] = potential[-2, :]
            potential[fixed] = fixed_values[fixed]
            if maximum_delta < 2.0e-7:
                break
        return potential

    @classmethod
    def _energy(cls, potential, epsilon_rows, x_values, y_values):
        dx_edges, dy_edges = np.diff(x_values), np.diff(y_values)
        dx_control, dy_control = np.empty_like(x_values), np.empty_like(y_values)
        dx_control[0], dx_control[-1] = 0.5 * dx_edges[0], 0.5 * dx_edges[-1]
        dy_control[0], dy_control[-1] = 0.5 * dy_edges[0], 0.5 * dy_edges[-1]
        dx_control[1:-1] = 0.5 * (dx_edges[:-1] + dx_edges[1:])
        dy_control[1:-1] = 0.5 * (dy_edges[:-1] + dy_edges[1:])
        horizontal = 0.5 * cls.EPSILON_0 * np.sum(
            epsilon_rows[:, None] * np.diff(potential, axis=1) ** 2
            * dy_control[:, None] / dx_edges[None, :]
        )
        vertical_epsilon = 0.5 * (epsilon_rows[:-1] + epsilon_rows[1:])[:, None]
        vertical = 0.5 * cls.EPSILON_0 * np.sum(
            vertical_epsilon * np.diff(potential, axis=0) ** 2
            * dx_control[None, :] / dy_edges[:, None]
        )
        return float(horizontal + vertical)

    @classmethod
    def solve(cls, width_mm, pair_gap_mm, ground_gap_mm, height_below_mm,
              epsilon_below, copper_thickness_mm=0.035, height_above_mm=0.0,
              epsilon_above=None, include_solder_mask=True,
              solder_mask_thickness_mm=0.02, solder_mask_epsilon_r=3.3,
              backing_plane=True):
        return cls._solve_cached(
            *(round(float(value), 9) for value in (
                width_mm, pair_gap_mm, ground_gap_mm, height_below_mm, epsilon_below,
                copper_thickness_mm, height_above_mm,
                epsilon_above if epsilon_above is not None else epsilon_below,
                solder_mask_thickness_mm, solder_mask_epsilon_r,
            )), bool(include_solder_mask), bool(backing_plane),
        )

    @classmethod
    @lru_cache(maxsize=256)
    def _solve_cached(cls, width_mm, pair_gap_mm, ground_gap_mm, height_below_mm,
                      epsilon_below, copper_thickness_mm, height_above_mm,
                      epsilon_above, solder_mask_thickness_mm, solder_mask_epsilon_r,
                      include_solder_mask, backing_plane):
        if min(width_mm, pair_gap_mm, ground_gap_mm, height_below_mm, copper_thickness_mm) <= 0.0:
            raise ValueError("Coplanar width, pair gap, ground gap and reference height must be positive.")
        if min(float(epsilon_below), float(epsilon_above)) < 1.0:
            raise ValueError("Relative permittivity must be at least 1.0.")
        internal = height_above_mm > 0.0
        vertical_scale = min(height_below_mm, height_above_mm if internal else height_below_mm)
        pair_half_width = pair_gap_mm / 2.0 + width_mm
        ground_edge = pair_half_width + ground_gap_mm
        side_margin = max(4.0 * vertical_scale, 2.0 * width_mm)
        half_span = ground_edge + side_margin
        signal_y, conductor_top_y = height_below_mm, height_below_mm + copper_thickness_mm
        if internal:
            total_height, top_reference_y = conductor_top_y + height_above_mm, conductor_top_y + height_above_mm
        else:
            total_height, top_reference_y = conductor_top_y + max(4.0 * height_below_mm, 2.0 * width_mm), None
        target_step = min(width_mm, pair_gap_mm, ground_gap_mm, vertical_scale) / 7.0
        def count(length, minimum=7, maximum=56):
            return max(minimum, min(maximum, int(math.ceil(length / target_step))))
        x_values = cls._piecewise_grid(
            (-half_span, -ground_edge, -pair_half_width, -pair_gap_mm / 2.0,
             pair_gap_mm / 2.0, pair_half_width, ground_edge, half_span),
            (count(side_margin, 14), count(ground_gap_mm), count(width_mm),
             count(pair_gap_mm, 10), count(width_mm), count(ground_gap_mm), count(side_margin, 14)),
        )
        if internal:
            y_values = cls._piecewise_grid((0.0, signal_y, conductor_top_y, total_height), (20, 3, 20))
        elif include_solder_mask and 0.0 < solder_mask_thickness_mm < total_height - conductor_top_y:
            y_values = cls._piecewise_grid((0.0, signal_y, conductor_top_y, conductor_top_y + solder_mask_thickness_mm, total_height), (20, 3, 3, 18))
        else:
            y_values = cls._piecewise_grid((0.0, signal_y, conductor_top_y, total_height), (20, 3, 22))
        nx, ny = len(x_values), len(y_values)
        conductor_rows = (y_values >= signal_y - 1.0e-12) & (y_values <= conductor_top_y + 1.0e-12)
        fixed, values = np.zeros((ny, nx), dtype=bool), np.zeros((ny, nx), dtype=np.float64)
        # A conductor-backed CPW fixes the lower boundary to the adjacent
        # reference plane.  An unbacked CPW leaves it open (zero normal-field
        # boundary); the same-layer ground conductors remain the reference.
        fixed[0, :] = bool(backing_plane)
        if internal:
            fixed[-1, :] = True
        left_trace = (x_values >= -pair_half_width) & (x_values <= -pair_gap_mm / 2.0)
        right_trace = (x_values >= pair_gap_mm / 2.0) & (x_values <= pair_half_width)
        side_ground = (x_values <= -ground_edge) | (x_values >= ground_edge)
        fixed[np.ix_(conductor_rows, left_trace | right_trace | side_ground)] = True
        values[np.ix_(conductor_rows, left_trace)] = -0.5
        values[np.ix_(conductor_rows, right_trace)] = 0.5
        epsilon_rows = cls._dielectric_map(
            y_values, signal_y, conductor_top_y, top_reference_y,
            float(epsilon_below), float(epsilon_above), include_solder_mask and not internal,
            solder_mask_thickness_mm, solder_mask_epsilon_r,
        )
        potential = cls._solve_potential(x_values, y_values, epsilon_rows, fixed, values)
        vacuum = cls._solve_potential(x_values, y_values, np.ones_like(epsilon_rows), fixed, values)
        capacitance = 2.0 * cls._energy(potential, epsilon_rows, x_values, y_values)
        vacuum_capacitance = 2.0 * cls._energy(vacuum, np.ones_like(epsilon_rows), x_values, y_values)
        if min(capacitance, vacuum_capacitance) <= 0.0:
            raise ValueError("Coplanar field solution did not produce a valid modal capacitance.")
        effective_epsilon = capacitance / vacuum_capacitance
        differential_impedance = 1.0 / (cls.LIGHT_SPEED_M_S * math.sqrt(capacitance * vacuum_capacitance))
        return CoplanarSolveResult(differential_impedance, 0.5 * differential_impedance,
                                   effective_epsilon, nx, ny)
