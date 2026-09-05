"""Shared 2-D quasi-static Laplace field solver for transmission-line
cross-sections (non-uniform finite-volume grid, red-black SOR relaxation,
energy-based capacitance extraction).

Used by both the coplanar differential solver (differential_geometry.py)
and the edge-coupled microstrip/stripline solver for the same numerical
core, so both geometries share one validated implementation.
"""

import numpy as np


def piecewise_grid(boundaries, intervals):
    """Build a 1-D grid subdividing consecutive *boundaries* into *intervals*
    equal-count segments each. Returns a strictly increasing float array."""
    values = []
    for index, count in enumerate(intervals):
        section = np.linspace(boundaries[index], boundaries[index + 1], count + 1)
        values.extend(section[:-1])
    values.append(boundaries[-1])
    return np.asarray(values, dtype=np.float64)


def dielectric_map(y_values, signal_y, conductor_top_y, top_reference_y,
                    epsilon_below, epsilon_above,
                    include_solder_mask, solder_mask_thickness, solder_mask_epsilon):
    """Per-row relative permittivity for a stack with dielectric *below* the
    signal conductors, optionally a second dielectric/reference *above*
    (``top_reference_y`` not None), or a thin solder-mask layer capping an
    open top boundary."""
    epsilon = np.ones(len(y_values), dtype=np.float64)
    # The conductor-thickness band [signal_y, conductor_top_y] is not copper
    # everywhere: between and beside the traces it is substrate (or air, for
    # an unbacked open top). It must carry epsilon_below, not the default
    # vacuum, or the fringing field in the trace-to-trace gap -- the region
    # most influential for coupled-line capacitance -- is under-permittized.
    # Grid rows exactly at the trace faces are Dirichlet-fixed where a
    # conductor sits, so their nominal epsilon only matters for the free
    # (gap) nodes at that height.
    epsilon[y_values <= conductor_top_y] = epsilon_below
    if top_reference_y is not None:
        epsilon[y_values >= conductor_top_y] = epsilon_above
    elif include_solder_mask and solder_mask_thickness > 0.0:
        mask = (y_values >= conductor_top_y) & (
            y_values <= conductor_top_y + solder_mask_thickness
        )
        epsilon[mask] = solder_mask_epsilon
    return epsilon


def solve_potential_sor(x_values, y_values, epsilon_rows, fixed, fixed_values,
                         relaxation=1.82, max_iterations=3000, tolerance=2.0e-7):
    """Solve the non-uniform finite-volume Laplace grid with red-black SOR.

    ``fixed`` is a boolean (ny, nx) mask of Dirichlet nodes; ``fixed_values``
    holds their potential. Free nodes start at their ``fixed_values`` entry
    (usually 0) and are relaxed toward the finite-volume stencil average
    until the maximum update falls below *tolerance* or *max_iterations* is
    reached. The left/right domain edges use a zero-normal-field (mirror)
    boundary; the top/bottom edges do too unless a row is entirely fixed.
    """
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
    for _iteration in range(max_iterations):
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
        if maximum_delta < tolerance:
            break
    return potential


def energy_from_potential(potential, epsilon_rows, x_values, y_values):
    """Integrate 0.5*epsilon*|grad(potential)|^2 over the grid (per unit
    length in the invariant third dimension). Returns joules/(V^2*m) which,
    doubled, is the per-unit-length modal capacitance."""
    epsilon_0 = 8.8541878128e-12
    dx_edges, dy_edges = np.diff(x_values), np.diff(y_values)
    dx_control, dy_control = np.empty_like(x_values), np.empty_like(y_values)
    dx_control[0], dx_control[-1] = 0.5 * dx_edges[0], 0.5 * dx_edges[-1]
    dy_control[0], dy_control[-1] = 0.5 * dy_edges[0], 0.5 * dy_edges[-1]
    dx_control[1:-1] = 0.5 * (dx_edges[:-1] + dx_edges[1:])
    dy_control[1:-1] = 0.5 * (dy_edges[:-1] + dy_edges[1:])
    horizontal = 0.5 * epsilon_0 * np.sum(
        epsilon_rows[:, None] * np.diff(potential, axis=1) ** 2
        * dy_control[:, None] / dx_edges[None, :]
    )
    vertical_epsilon = 0.5 * (epsilon_rows[:-1] + epsilon_rows[1:])[:, None]
    vertical = 0.5 * epsilon_0 * np.sum(
        vertical_epsilon * np.diff(potential, axis=0) ** 2
        * dx_control[None, :] / dy_edges[:, None]
    )
    return float(horizontal + vertical)
