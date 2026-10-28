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

try:
    from .physical_constants import DEFAULT_COPPER_THICKNESS_MM
    from . import field_solver_2d
    from .field_solver_2d import (
        dielectric_map, energy_from_potential, etched_mask, piecewise_grid,
        solve_potential, undercut_mm,
    )
except (ImportError, ValueError):
    from physical_constants import DEFAULT_COPPER_THICKNESS_MM
    import field_solver_2d
    from field_solver_2d import (
        dielectric_map, energy_from_potential, etched_mask, piecewise_grid,
        solve_potential, undercut_mm,
    )


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


def _vertical_boundaries(signal_y, conductor_top_y, total_height, internal,
                          include_solder_mask, solder_mask_thickness_mm):
    """Y boundaries for the vertical grid, splitting off a sub-layer inside
    (and, unbacked, above) the copper-thickness band when a solder mask
    thinner than the copper coats the exposed substrate there. Matches the
    two mask regions field_solver_2d.dielectric_map() now models (SI-2)."""
    boundaries = [0.0, signal_y]
    mask_active = (not internal) and include_solder_mask and solder_mask_thickness_mm > 0.0
    if mask_active:
        band_mask_top = signal_y + solder_mask_thickness_mm
        if band_mask_top < conductor_top_y:
            boundaries.append(band_mask_top)
    boundaries.append(conductor_top_y)
    if mask_active:
        top_mask = conductor_top_y + solder_mask_thickness_mm
        if top_mask < total_height:
            boundaries.append(top_mask)
    boundaries.append(total_height)
    return boundaries


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
        return piecewise_grid(boundaries, intervals)

    @staticmethod
    def _dielectric_map(y_values, signal_y, conductor_top_y, top_reference_y,
                        epsilon_below, epsilon_above,
                        include_solder_mask, solder_mask_thickness, solder_mask_epsilon):
        return dielectric_map(
            y_values, signal_y, conductor_top_y, top_reference_y,
            epsilon_below, epsilon_above,
            include_solder_mask, solder_mask_thickness, solder_mask_epsilon,
        )

    @classmethod
    def _solve_potential(cls, x_values, y_values, epsilon_rows, fixed, fixed_values):
        """Solve the non-uniform finite-volume grid (direct sparse solve)."""
        return solve_potential(x_values, y_values, epsilon_rows, fixed, fixed_values)

    @classmethod
    def _energy(cls, potential, epsilon_rows, x_values, y_values):
        return energy_from_potential(potential, epsilon_rows, x_values, y_values)

    @classmethod
    def solve(cls, width_mm, pair_gap_mm, ground_gap_mm, height_below_mm,
              epsilon_below, copper_thickness_mm=DEFAULT_COPPER_THICKNESS_MM, height_above_mm=0.0,
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
            float(field_solver_2d.ETCH_FACTOR),
        )

    @classmethod
    @lru_cache(maxsize=256)
    def _solve_cached(cls, width_mm, pair_gap_mm, ground_gap_mm, height_below_mm,
                      epsilon_below, copper_thickness_mm, height_above_mm,
                      epsilon_above, solder_mask_thickness_mm, solder_mask_epsilon_r,
                      include_solder_mask, backing_plane, etch_factor):
        if min(width_mm, pair_gap_mm, ground_gap_mm, height_below_mm, copper_thickness_mm) <= 0.0:
            raise ValueError("Coplanar width, pair gap, ground gap and reference height must be positive.")
        if min(float(epsilon_below), float(epsilon_above)) < 1.0:
            raise ValueError("Relative permittivity must be at least 1.0.")
        internal = height_above_mm > 0.0
        vertical_scale = min(height_below_mm, height_above_mm if internal else height_below_mm)
        pair_half_width = pair_gap_mm / 2.0 + width_mm
        ground_edge = pair_half_width + ground_gap_mm
        # SI-2: the mirror (zero-normal-field) side boundary has to be far
        # enough from the ground trace that its image charge does not
        # perturb the near-field solution; it was independent of the gap
        # (max(4*h, 2*w), as little as 0.4 mm on P02) instead of scaling
        # with it.
        side_margin = max(10.0 * vertical_scale + ground_gap_mm, 2.0 * width_mm)
        half_span = ground_edge + side_margin
        signal_y, conductor_top_y = height_below_mm, height_below_mm + copper_thickness_mm
        if internal:
            total_height, top_reference_y = conductor_top_y + height_above_mm, conductor_top_y + height_above_mm
        else:
            # Same reasoning for the open top boundary: keep it far instead
            # of pinned to 4*h.
            total_height, top_reference_y = conductor_top_y + max(10.0 * height_below_mm, 2.0 * width_mm), None
        # SI-2: grid step <= h/20 (was h/7, -2.4% on Zdiff alone at P02's
        # geometry when compared against a doubled grid).
        target_step = min(width_mm, pair_gap_mm, ground_gap_mm, vertical_scale) / 20.0
        # 90 (was 56) so the far margin above stays resolved at close to
        # target_step instead of jumping straight to a coarse cell size at
        # the segment boundary once the margin fix widened it: on the P02
        # stripline-analogue Cohn check (w/s/b=0.15/0.15/0.5, er 4.2) that
        # discontinuity alone cost ~1 pp of the remaining error. Runtime
        # stays a fraction of a second per solve; lru_cache absorbs
        # repeats within one recommender sweep.
        def count(length, minimum=7, maximum=90):
            return max(minimum, min(maximum, int(math.ceil(length / target_step))))
        undercut = min(undercut_mm(copper_thickness_mm, etch_factor), 0.25 * min(width_mm, ground_gap_mm))
        if undercut > 0.0:
            # A column boundary where each etched edge's top lands, so the
            # trapezoid is resolved whatever the trace width.
            u, cu = undercut, count(undercut, 3)
            x_values = cls._piecewise_grid(
                (-half_span, -ground_edge - u, -ground_edge, -pair_half_width,
                 -pair_half_width + u, -pair_gap_mm / 2.0 - u, -pair_gap_mm / 2.0,
                 pair_gap_mm / 2.0, pair_gap_mm / 2.0 + u, pair_half_width - u,
                 pair_half_width, ground_edge, ground_edge + u, half_span),
                (count(side_margin - u, 14), cu, count(ground_gap_mm), cu,
                 count(width_mm - 2.0 * u), cu, count(pair_gap_mm, 10), cu,
                 count(width_mm - 2.0 * u), cu, count(ground_gap_mm), cu,
                 count(side_margin - u, 14)),
            )
        else:
            x_values = cls._piecewise_grid(
                (-half_span, -ground_edge, -pair_half_width, -pair_gap_mm / 2.0,
                 pair_gap_mm / 2.0, pair_half_width, ground_edge, half_span),
                (count(side_margin, 14), count(ground_gap_mm), count(width_mm),
                 count(pair_gap_mm, 10), count(width_mm), count(ground_gap_mm), count(side_margin, 14)),
            )
        y_boundaries = _vertical_boundaries(
            signal_y, conductor_top_y, total_height, internal,
            include_solder_mask and not internal, solder_mask_thickness_mm,
        )
        y_values = cls._piecewise_grid(
            y_boundaries, [count(b - a) for a, b in zip(y_boundaries[:-1], y_boundaries[1:])],
        )
        nx, ny = len(x_values), len(y_values)
        conductor_rows = (y_values >= signal_y - 1.0e-12) & (y_values <= conductor_top_y + 1.0e-12)
        fixed, values = np.zeros((ny, nx), dtype=bool), np.zeros((ny, nx), dtype=np.float64)
        # A conductor-backed CPW fixes the lower boundary to the adjacent
        # reference plane.  An unbacked CPW leaves it open (zero normal-field
        # boundary); the same-layer ground conductors remain the reference.
        fixed[0, :] = bool(backing_plane)
        if internal:
            fixed[-1, :] = True
        left_trace = etched_mask(x_values, y_values, -pair_half_width, -pair_gap_mm / 2.0,
                                 signal_y, conductor_top_y, undercut)
        right_trace = etched_mask(x_values, y_values, pair_gap_mm / 2.0, pair_half_width,
                                  signal_y, conductor_top_y, undercut)
        side_ground = (
            etched_mask(x_values, y_values, -np.inf, -ground_edge, signal_y, conductor_top_y, undercut)
            | etched_mask(x_values, y_values, ground_edge, np.inf, signal_y, conductor_top_y, undercut)
        )
        fixed[left_trace | right_trace | side_ground] = True
        values[left_trace] = -0.5
        values[right_trace] = 0.5
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


@dataclass(frozen=True)
class EdgeCoupledSolveResult:
    differential_impedance_ohm: float
    odd_mode_impedance_ohm: float
    effective_epsilon_r: float
    grid_columns: int
    grid_rows: int


class EdgeCoupledDifferentialSolver:
    """Quasi-static 2-D odd-mode solver for edge-coupled microstrip and
    stripline pairs.

    Unlike :class:`GroundedCoplanarDifferentialSolver`, there is no
    same-layer ground conductor: the reference is one plane below
    (microstrip) or two planes above and below (stripline). This replaces
    the IPC-D-317A closed-form estimates in ``differential_impedance.py``
    with an actual finite-volume field solve, sharing the Laplace/energy core
    in ``field_solver_2d.py``.
    """

    EPSILON_0 = 8.8541878128e-12
    LIGHT_SPEED_M_S = 299792458.0

    @classmethod
    def solve_microstrip(cls, width_mm, gap_mm, copper_thickness_mm, height_below_mm,
                          epsilon_below, include_solder_mask=True,
                          solder_mask_thickness_mm=0.02, solder_mask_epsilon_r=3.3):
        """Single reference plane below the pair; open dielectric above,
        optionally capped by a thin solder-mask layer."""
        return cls._solve_cached(
            *(round(float(value), 9) for value in (
                width_mm, gap_mm, copper_thickness_mm, height_below_mm, epsilon_below,
                0.0, epsilon_below, solder_mask_thickness_mm, solder_mask_epsilon_r,
            )), bool(include_solder_mask), float(field_solver_2d.ETCH_FACTOR),
        )

    @classmethod
    def solve_stripline(cls, width_mm, gap_mm, copper_thickness_mm,
                         height_above_mm, epsilon_above, height_below_mm, epsilon_below):
        """Two reference planes, above and below; may be asymmetric in
        spacing and/or dielectric."""
        return cls._solve_cached(
            *(round(float(value), 9) for value in (
                width_mm, gap_mm, copper_thickness_mm, height_below_mm, epsilon_below,
                height_above_mm, epsilon_above, 0.0, 3.3,
            )), False, float(field_solver_2d.ETCH_FACTOR),
        )

    @classmethod
    @lru_cache(maxsize=256)
    def _solve_cached(cls, width_mm, gap_mm, copper_thickness_mm, height_below_mm,
                      epsilon_below, height_above_mm, epsilon_above,
                      solder_mask_thickness_mm, solder_mask_epsilon_r, include_solder_mask,
                      etch_factor):
        return cls._solve_common(
            width_mm, gap_mm, copper_thickness_mm, height_below_mm,
            epsilon_below, height_above_mm, epsilon_above,
            solder_mask_thickness_mm, solder_mask_epsilon_r, include_solder_mask,
            left_trace_value=-0.5, etch_factor=etch_factor,
        )

    @classmethod
    @lru_cache(maxsize=256)
    def _solve_cached_even(cls, width_mm, gap_mm, copper_thickness_mm, height_below_mm,
                           epsilon_below, height_above_mm, epsilon_above,
                           solder_mask_thickness_mm, solder_mask_epsilon_r, include_solder_mask,
                           etch_factor):
        """Even (common) mode: both traces driven to +0.5 V instead of the
        odd-mode +/-0.5 V split used by :meth:`_solve_cached`. Same grid,
        dielectric map, and capacitance-ratio impedance extraction."""
        return cls._solve_common(
            width_mm, gap_mm, copper_thickness_mm, height_below_mm,
            epsilon_below, height_above_mm, epsilon_above,
            solder_mask_thickness_mm, solder_mask_epsilon_r, include_solder_mask,
            left_trace_value=0.5, etch_factor=etch_factor,
        )

    @classmethod
    def _solve_common(cls, width_mm, gap_mm, copper_thickness_mm, height_below_mm,
                      epsilon_below, height_above_mm, epsilon_above,
                      solder_mask_thickness_mm, solder_mask_epsilon_r, include_solder_mask,
                      left_trace_value, etch_factor=0.0):
        if min(width_mm, gap_mm, copper_thickness_mm, height_below_mm) <= 0.0:
            raise ValueError("Edge-coupled width, gap, thickness and reference height must be positive.")
        if min(float(epsilon_below), float(epsilon_above)) < 1.0:
            raise ValueError("Relative permittivity must be at least 1.0.")
        internal = height_above_mm > 0.0
        vertical_scale = min(height_below_mm, height_above_mm) if internal else height_below_mm
        pair_half_width = gap_mm / 2.0 + width_mm
        # SI-2: keep the mirror side boundary scaled to the gap, not pinned
        # to max(4*h, 2*w) (0.4 mm on P02's 0.0994 mm / 0.25 mm geometry).
        side_margin = max(10.0 * vertical_scale + gap_mm, 2.0 * width_mm)
        half_span = pair_half_width + side_margin
        signal_y, conductor_top_y = height_below_mm, height_below_mm + copper_thickness_mm
        if internal:
            total_height, top_reference_y = conductor_top_y + height_above_mm, conductor_top_y + height_above_mm
        else:
            total_height, top_reference_y = conductor_top_y + max(10.0 * height_below_mm, 2.0 * width_mm), None
        # SI-2: grid step <= h/20 (was h/7).
        target_step = min(width_mm, gap_mm, vertical_scale) / 20.0
        # 90 (was 56): see the matching comment in
        # GroundedCoplanarDifferentialSolver._solve_cached.
        def count(length, minimum=7, maximum=90):
            return max(minimum, min(maximum, int(math.ceil(length / target_step))))
        undercut = min(undercut_mm(copper_thickness_mm, etch_factor), 0.25 * width_mm)
        if undercut > 0.0:
            # A column boundary where each etched edge's top lands, so the
            # trapezoid is resolved whatever the trace width.
            u, cu = undercut, count(undercut, 3)
            x_values = piecewise_grid(
                (-half_span, -pair_half_width, -pair_half_width + u, -gap_mm / 2.0 - u,
                 -gap_mm / 2.0, gap_mm / 2.0, gap_mm / 2.0 + u, pair_half_width - u,
                 pair_half_width, half_span),
                (count(side_margin, 14), cu, count(width_mm - 2.0 * u), cu,
                 count(gap_mm, 10), cu, count(width_mm - 2.0 * u), cu, count(side_margin, 14)),
            )
        else:
            x_values = piecewise_grid(
                (-half_span, -pair_half_width, -gap_mm / 2.0, gap_mm / 2.0, pair_half_width, half_span),
                (count(side_margin, 14), count(width_mm), count(gap_mm, 10),
                 count(width_mm), count(side_margin, 14)),
            )
        y_boundaries = _vertical_boundaries(
            signal_y, conductor_top_y, total_height, internal,
            include_solder_mask, solder_mask_thickness_mm,
        )
        y_values = piecewise_grid(
            y_boundaries, [count(b - a) for a, b in zip(y_boundaries[:-1], y_boundaries[1:])],
        )
        nx, ny = len(x_values), len(y_values)
        conductor_rows = (y_values >= signal_y - 1.0e-12) & (y_values <= conductor_top_y + 1.0e-12)
        fixed, values = np.zeros((ny, nx), dtype=bool), np.zeros((ny, nx), dtype=np.float64)
        fixed[0, :] = True  # bottom reference plane is always present
        if internal:
            fixed[-1, :] = True  # stripline top reference plane
        left_trace = etched_mask(x_values, y_values, -pair_half_width, -gap_mm / 2.0,
                                 signal_y, conductor_top_y, undercut)
        right_trace = etched_mask(x_values, y_values, gap_mm / 2.0, pair_half_width,
                                  signal_y, conductor_top_y, undercut)
        fixed[left_trace | right_trace] = True
        values[left_trace] = left_trace_value
        values[right_trace] = 0.5
        epsilon_rows = dielectric_map(
            y_values, signal_y, conductor_top_y, top_reference_y,
            float(epsilon_below), float(epsilon_above), include_solder_mask and not internal,
            solder_mask_thickness_mm, solder_mask_epsilon_r,
        )
        potential = solve_potential(x_values, y_values, epsilon_rows, fixed, values)
        vacuum = solve_potential(x_values, y_values, np.ones_like(epsilon_rows), fixed, values)
        capacitance = 2.0 * energy_from_potential(potential, epsilon_rows, x_values, y_values)
        vacuum_capacitance = 2.0 * energy_from_potential(vacuum, np.ones_like(epsilon_rows), x_values, y_values)
        if min(capacitance, vacuum_capacitance) <= 0.0:
            raise ValueError("Edge-coupled field solution did not produce a valid modal capacitance.")
        effective_epsilon = capacitance / vacuum_capacitance
        differential_impedance = 1.0 / (cls.LIGHT_SPEED_M_S * math.sqrt(capacitance * vacuum_capacitance))
        return EdgeCoupledSolveResult(differential_impedance, 0.5 * differential_impedance,
                                      effective_epsilon, nx, ny)

    @classmethod
    def solve_microstrip_even(cls, width_mm, gap_mm, copper_thickness_mm, height_below_mm,
                              epsilon_below, include_solder_mask=True,
                              solder_mask_thickness_mm=0.02, solder_mask_epsilon_r=3.3):
        """Even (common) mode counterpart of :meth:`solve_microstrip` — both
        traces driven to the same potential, used to derive crosstalk
        coefficients alongside the odd-mode result."""
        return cls._solve_cached_even(
            *(round(float(value), 9) for value in (
                width_mm, gap_mm, copper_thickness_mm, height_below_mm, epsilon_below,
                0.0, epsilon_below, solder_mask_thickness_mm, solder_mask_epsilon_r,
            )), bool(include_solder_mask), float(field_solver_2d.ETCH_FACTOR),
        )

    @classmethod
    def solve_stripline_even(cls, width_mm, gap_mm, copper_thickness_mm,
                             height_above_mm, epsilon_above, height_below_mm, epsilon_below):
        """Even (common) mode counterpart of :meth:`solve_stripline`."""
        return cls._solve_cached_even(
            *(round(float(value), 9) for value in (
                width_mm, gap_mm, copper_thickness_mm, height_below_mm, epsilon_below,
                height_above_mm, epsilon_above, 0.0, 3.3,
            )), False, float(field_solver_2d.ETCH_FACTOR),
        )
