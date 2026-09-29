"""Shared 2-D quasi-static Laplace field solver for transmission-line
cross-sections (non-uniform finite-volume grid, direct sparse solve,
energy-based capacitance extraction).

Used by both the coplanar differential solver (differential_geometry.py)
and the edge-coupled microstrip/stripline solver for the same numerical
core, so both geometries share one validated implementation.
"""

import numpy as np
import scipy.sparse
import scipy.sparse.linalg


def piecewise_grid(boundaries, intervals):
    """Build a 1-D grid subdividing consecutive *boundaries* into *intervals*
    equal-count segments each. Returns a strictly increasing float array."""
    values = []
    for index, count in enumerate(intervals):
        section = np.linspace(boundaries[index], boundaries[index + 1], count + 1)
        values.extend(section[:-1])
    values.append(boundaries[-1])
    return np.asarray(values, dtype=np.float64)


# Etch factor of a trace edge: the copper thickness over the undercut of
# one side. 2.75 makes the top of a 35 um trace 25.4 um (1 mil) narrower
# than its base, the usual allowance for 1 oz copper; 0 draws the trace
# rectangular.
ETCH_FACTOR = 2.75


def undercut_mm(copper_thickness_mm, etch_factor):
    """How far each edge of a trace's top sits inside its base, in mm."""
    return copper_thickness_mm / etch_factor if etch_factor > 0.0 else 0.0


def etched_mask(x_values, y_values, left, right, bottom_y, top_y, undercut):
    """The grid cells of a trace whose base spans *left* to *right*.

    Etching takes copper from the top of a trace's edges: its cross-section
    is a trapezoid, each edge moving in by *undercut* from base to top. It
    was drawn as a rectangle (a limitation the report stated).
    """
    rows = (y_values >= bottom_y - 1.0e-12) & (y_values <= top_y + 1.0e-12)
    rise = np.clip((y_values - bottom_y) / max(top_y - bottom_y, 1.0e-12), 0.0, 1.0)
    inset = (rise * undercut)[:, None]
    columns = (
        (x_values[None, :] >= left + inset - 1.0e-12)
        & (x_values[None, :] <= right - inset + 1.0e-12)
    )
    return columns & rows[:, None]


def dielectric_map(y_values, signal_y, conductor_top_y, top_reference_y,
                    epsilon_below, epsilon_above,
                    include_solder_mask, solder_mask_thickness, solder_mask_epsilon):
    """Per-row relative permittivity for a stack with dielectric *below* the
    signal conductors, optionally a second dielectric/reference *above*
    (``top_reference_y`` not None), or a thin solder-mask layer capping an
    open top boundary.

    Grid rows exactly at the trace faces are Dirichlet-fixed where a
    conductor sits, so their nominal epsilon only matters for the free
    (gap) nodes at that height -- the fringing field between and beside the
    traces, which dominates coupled-line capacitance.
    """
    epsilon = np.ones(len(y_values), dtype=np.float64)
    epsilon[y_values < signal_y] = epsilon_below
    if top_reference_y is not None:
        # Buried/internal conductor (stripline): resin flows around the
        # trace during lamination, so the copper-thickness band is the same
        # laminate as above and below, not air.
        epsilon[(y_values >= signal_y) & (y_values < conductor_top_y)] = epsilon_below
        epsilon[y_values >= conductor_top_y] = epsilon_above
        return epsilon
    # Exposed conductor (microstrip, or a coplanar pair on the surface
    # layer): the copper-thickness band [signal_y, conductor_top_y] is air
    # between and beside the traces, not the substrate below. Filling it
    # with epsilon_below (as a buried conductor's band correctly is) was
    # SI-2 in audit-2026-09-23.md: on P02 Rev E (0.200/0.250 mm, er 4.1,
    # 0.01 mm mask) it read Zdiff 6.2% low with no mask modelled, 3.1% low
    # with the real mask -- part of the tool's 76.86-ohm-vs-85.4-ohm gap.
    # A solder mask coats whatever surface is exposed at its own local
    # height: on the substrate beside the trace, that is up to
    # solder_mask_thickness above signal_y (this block); on top of the
    # trace, that is up to solder_mask_thickness above conductor_top_y
    # (the block below).
    if include_solder_mask and solder_mask_thickness > 0.0:
        gap_mask = (y_values >= signal_y) & (
            y_values <= min(conductor_top_y, signal_y + solder_mask_thickness)
        )
        epsilon[gap_mask] = solder_mask_epsilon
        mask_above = (y_values >= conductor_top_y) & (
            y_values <= conductor_top_y + solder_mask_thickness
        )
        epsilon[mask_above] = solder_mask_epsilon
    return epsilon


def solve_potential(x_values, y_values, epsilon_rows, fixed, fixed_values):
    """Solve the non-uniform finite-volume Laplace grid directly.

    ``fixed`` is a boolean (ny, nx) mask of Dirichlet nodes; ``fixed_values``
    holds their potential. The left/right domain edges use a zero-normal-field
    (mirror) boundary; the top/bottom edges do too unless a row holds a fixed
    node, in which case that row's free nodes keep their ``fixed_values``
    entry.

    This is the exact fixed point the red-black SOR it replaces converged to,
    solved as one sparse system. The SOR needed up to 20,000 sweeps once the
    grid was refined to h/20 with the wider mirror margins (SI-2): 3-13 s per
    cross-section, and the suite went from 31 s to 488 s. A sparse LU of the
    same stencil is exact to round-off in a fraction of that.
    """
    ny, nx = fixed.shape
    index = np.arange(ny * nx).reshape(ny, nx)
    rows, cols, vals = [], [], []
    rhs = np.zeros(ny * nx, dtype=np.float64)

    def couple(mask, own, other, weight):
        rows.append(index[own][mask])
        cols.append(index[other][mask])
        vals.append(np.broadcast_to(weight, mask.shape)[mask])

    handled = np.array(fixed, copy=True)
    rhs[fixed.ravel()] = fixed_values[fixed]
    identity = handled.copy()

    def mirror(mask, own, other):
        # V(own) - V(other) = 0 for the masked nodes not already handled.
        take = mask & ~handled[own]
        full = np.zeros_like(handled)
        full[own] = take
        identity[full] = True
        couple(take, own, other, -1.0)
        handled[full] = True

    row_edges = []
    if not np.any(fixed[0, :]):
        mirror(np.ones(nx, dtype=bool), (0, slice(None)), (1, slice(None)))
    else:
        row_edges.append(0)
    if not np.any(fixed[-1, :]):
        mirror(np.ones(nx, dtype=bool), (ny - 1, slice(None)), (ny - 2, slice(None)))
    else:
        row_edges.append(ny - 1)
    mirror(np.ones(ny, dtype=bool), (slice(None), 0), (slice(None), 1))
    mirror(np.ones(ny, dtype=bool), (slice(None), nx - 1), (slice(None), nx - 2))
    for row in row_edges:
        # A partly fixed edge row: its free nodes are never relaxed.
        free = ~handled[row, :]
        identity[row, free] = True
        rhs[index[row, free]] = fixed_values[row, free]
        handled[row, free] = True

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
    interior = ~handled[1:-1, 1:-1]
    centre = (slice(1, -1), slice(1, -1))
    couple(interior, centre, centre, horizontal_left + horizontal_right + downward + upward)
    couple(interior, centre, (slice(1, -1), slice(None, -2)), -horizontal_left)
    couple(interior, centre, (slice(1, -1), slice(2, None)), -horizontal_right)
    couple(interior, centre, (slice(None, -2), slice(1, -1)), -downward)
    couple(interior, centre, (slice(2, None), slice(1, -1)), -upward)
    diagonal = identity.ravel()
    rows.append(index.ravel()[diagonal])
    cols.append(index.ravel()[diagonal])
    vals.append(np.ones(int(diagonal.sum())))
    matrix = scipy.sparse.csr_matrix(
        (np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
        shape=(ny * nx, ny * nx),
    )
    potential = scipy.sparse.linalg.spsolve(matrix.tocsc(), rhs)
    return potential.reshape(ny, nx)


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
