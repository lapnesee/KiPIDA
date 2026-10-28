"""Benchmark the enclosure CFD against flows whose answer is known in closed form.

Run with ``python validation/cfd_benchmarks.py [case ...]``; with no argument
every case runs. Cases are deliberately slow: a pseudo-transient solver needs
O(ny^2) iterations to diffuse momentum across the channel, which is itself one
of the findings recorded in ``docs/validation-cfd.md``.

The reference case is a wide rectangular duct. Making it eight times wider than
it is tall means the mid-span profile approaches flow between parallel plates,
for which fully-developed laminar theory gives

    u(y) = 6 * u_mean * (y/H) * (1 - y/H)          =>   u_max / u_mean = 1.5

Two conditions must hold for that to be the right yardstick, and both are
checked rather than assumed: the flow must be laminar (Re well under 2300) and
the duct must be longer than the hydrodynamic entry length 0.05 * Re * Dh.
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cfd_mesh import CFDMesh, CFDMeshGenerator, CFDPatchCells  # noqa: E402
from cfd_model import CFDObstacle, EnclosureModel  # noqa: E402
from cfd_solver import EnclosureCFDSolver  # noqa: E402
from models import (  # noqa: E402
    CFDBoundaryPatch, CFDSolverSettings, EnclosureCFDSettings,
    EnclosureGeometrySettings, FluidProperties,
)

# A wide, flat duct: 8:1 span-to-height, so mid-span approximates parallel
# plates. Dimensions in metres.
LENGTH_M, HEIGHT_M, WIDTH_M = 0.100, 0.008, 0.064
INLET_M_S = 0.05


def duct_mesh(nx, ny, nz, length_m=LENGTH_M, height_m=HEIGHT_M, width_m=WIDTH_M):
    """An empty rectangular duct: every cell is fluid, no obstacles, no heat."""
    shape = (nx, ny, nz)
    return CFDMesh(
        shape=shape,
        spacing_m=(length_m / nx, height_m / ny, width_m / nz),
        dimensions_m=(length_m, height_m, width_m),
        fluid_mask=np.ones(shape, dtype=bool),
        solid_mask=np.zeros(shape, dtype=bool),
        solid_conductivity_w_mk=np.zeros(shape),
        heat_sources_w=np.zeros(shape),
        patch_cells=[],
    )


def duct_settings(velocity=INLET_M_S, iterations=3000, tolerance=1e-5):
    """Isothermal forced flow: buoyancy off, adiabatic walls, no heat source.

    This isolates the momentum and continuity machinery. Buoyancy and the
    energy equation are exercised by the enclosure tests instead.
    """
    settings = EnclosureCFDSettings(
        ambient_c=25.0,
        geometry=EnclosureGeometrySettings(wall_heat_transfer_w_m2k=0.0),
        fluid=FluidProperties(),
        solver=CFDSolverSettings(
            max_iterations=iterations,
            tolerance=tolerance,
            include_buoyancy=False,
        ),
    )
    settings.patches = [
        CFDBoundaryPatch(name="in", kind="INLET", face="XMIN",
                         center_u=0.5, center_v=0.5, size_u=1.0, size_v=1.0,
                         velocity_m_s=velocity),
        CFDBoundaryPatch(name="out", kind="OUTLET", face="XMAX",
                         center_u=0.5, center_v=0.5, size_u=1.0, size_v=1.0),
    ]
    return settings


def attach_patches(mesh, settings):
    """Map patches onto an ad-hoc mesh the way CFDMeshGenerator would."""
    generator = CFDMeshGenerator()
    mesh.patch_cells = [
        CFDPatchCells(patch=patch, cells=generator._patch_cells(patch, mesh.shape))
        for patch in settings.patches
    ]
    return mesh


def run(mesh, settings):
    result = EnclosureCFDSolver().solve(mesh, settings)
    fields = tuple(
        np.array(values).reshape(mesh.shape)
        for values in (result.velocity_u_m_s, result.velocity_v_m_s, result.velocity_w_m_s)
    )
    return result, fields


def parabola_shape_error(profile):
    """RMS deviation of a normalised profile from the parallel-plate parabola, %.

    ``u_max/u_mean`` alone is a poor yardstick on a coarse mesh, because the
    solver enforces no-slip by zeroing the *cell-centred* velocity in the wall
    cell. The effective wall therefore sits at the centre of cell 0, not at the
    domain face, so the open height is (ny-1)*dy rather than ny*dy and the
    section average is dragged down by two cells of dead fluid. On ny=10 that
    alone inflates u_max/u_mean from 1.500 to about 1.875 -- a first-order
    boundary-placement error, not a defect in the momentum scheme.

    Comparing shapes removes it. Both curves are normalised to unit peak and
    measured against u/u_max = 4*eta*(1-eta) on the effective open span, so what
    is left is how far the computed profile departs from a parabola.
    """
    values = np.asarray(profile, dtype=float)
    n = values.size
    peak = values.max()
    if n < 4 or peak <= 1e-12:
        return float("nan")
    # Cell centres measured from the effective wall at the centre of cell 0.
    eta = (np.arange(n) - 0.0) / (n - 1.0)
    reference = 4.0 * eta * (1.0 - eta)
    return float(np.sqrt(np.mean((values / peak - reference) ** 2))) * 100.0


def regime(velocity=INLET_M_S, height_m=HEIGHT_M, width_m=WIDTH_M, length_m=LENGTH_M):
    """Reynolds number and entry length -- is the Poiseuille answer applicable?"""
    props = FluidProperties()
    nu = props.dynamic_viscosity_pa_s / props.density_kg_m3
    hydraulic = 2.0 * height_m * width_m / (height_m + width_m)
    reynolds = velocity * hydraulic / nu
    entry = 0.05 * reynolds * hydraulic
    return reynolds, entry, hydraulic


# --------------------------------------------------------------------------
# Case 1 -- the velocity profile itself


def case_profile():
    reynolds, entry, hydraulic = regime()
    print(f"Re = {reynolds:.0f} (laminar below 2300), Dh = {hydraulic * 1e3:.2f} mm")
    print(f"entry length = {entry * 1e3:.1f} mm vs duct length {LENGTH_M * 1e3:.0f} mm "
          f"-- {'developed' if entry < 0.5 * LENGTH_M else 'NOT developed'} at the outlet")

    mesh = duct_mesh(30, 10, 8)
    settings = duct_settings()
    attach_patches(mesh, settings)
    result, (u, v, w) = run(mesh, settings)

    print(f"converged={result.converged} after {result.iterations} iterations")
    print(f"reported mass balance error = {result.mass_balance_error_pct:.4g} %")
    # "Did not converge" is only alarming if the residual is still large; report
    # how far short it fell rather than just the boolean.
    history = result.residuals
    print(f"final residuals: continuity={history.continuity[-1]:.3g}  "
          f"momentum={history.momentum[-1]:.3g}  energy={history.energy[-1]:.3g}  "
          f"(tolerance {settings.solver.tolerance:g})")
    stalled = history.momentum[len(history.momentum) // 2]
    print(f"momentum residual at half-run = {stalled:.3g} "
          f"-> {'still falling' if history.momentum[-1] < 0.5 * stalled else 'STALLED'}")
    print()

    mid_z = mesh.shape[2] // 2
    staircase = 1.5 * mesh.shape[1] / (mesh.shape[1] - 2.0)
    print(f"  discrete expectation for u_max/u_mean on ny={mesh.shape[1]}: {staircase:.3f}")
    print("  x/L    u_max/u_mean   shape error vs parabola   u_mean [m/s]")
    for fraction in (0.25, 0.5, 0.75, 0.95):
        i = min(mesh.shape[0] - 1, int(fraction * mesh.shape[0]))
        profile = u[i, :, mid_z]
        mean = profile.mean()
        ratio = profile.max() / mean if abs(mean) > 1e-12 else float("nan")
        print(f"  {fraction:4.2f}      {ratio:8.3f}          {parabola_shape_error(profile):8.2f} %"
              f"              {mean:.5f}")

    outlet = u[int(0.95 * mesh.shape[0]), :, mid_z]
    print()
    print(f"  no-slip at walls: u[0]={outlet[0]:.4g}  u[-1]={outlet[-1]:.4g}")
    span = max(abs(outlet).max(), 1e-12)
    print(f"  profile asymmetry: {np.abs(outlet - outlet[::-1]).max() / span * 100:.2f} %")


# --------------------------------------------------------------------------
# Case 2 -- does mass actually survive the duct?


def case_flux():
    """Streamwise volumetric flux must be identical in every cross-section.

    This is the test the built-in ``mass_balance_error_pct`` cannot fail: the
    solver overwrites the outlet velocity with inflow/outlet_area, so the
    reported figure is imposed rather than measured. Interior planes are not.
    """
    mesh = duct_mesh(30, 10, 8)
    settings = duct_settings()
    attach_patches(mesh, settings)
    result, (u, v, w) = run(mesh, settings)

    dy, dz = mesh.spacing_m[1], mesh.spacing_m[2]
    flux = [float(u[i, :, :].sum()) * dy * dz for i in range(mesh.shape[0])]

    # Reference against the flux the inlet boundary *imposes*, not against an
    # interior plane. Plane 1 is the worst possible yardstick: it sits one cell
    # downstream of a uniform inlet, where no-slip has just been switched on, so
    # it is the least converged plane in the duct. Measuring against it made the
    # error look like +23% when the physical deficit is the other sign.
    inlet_area = mesh.dimensions_m[1] * mesh.dimensions_m[2]
    imposed = INLET_M_S * inlet_area

    print(f"reported mass balance error = {result.mass_balance_error_pct:.4g} %")
    print(f"flux imposed by the inlet   = {imposed:.6e} m3/s")
    print()
    print("  plane    flux [m3/s]     ratio to the imposed inlet flux")
    for i in (0, 1, 5, 10, 15, 20, 25, mesh.shape[0] - 2):
        print(f"  {i:5d}   {flux[i]:.6e}      {flux[i] / imposed:6.3f}")
    settled = np.array(flux[mesh.shape[0] // 2:-1])
    print()
    print(f"  settled interior flux / imposed = {settled.mean() / imposed:.4f} "
          f"-> {(settled.mean() / imposed - 1.0) * 100:+.2f} % mass error")
    print(f"  plane-to-plane spread downstream = "
          f"{(settled.max() - settled.min()) / imposed * 100:.2f} %")


# --------------------------------------------------------------------------
# Case 3 -- interior continuity


def case_divergence():
    """A converged incompressible solution must have div(u) ~ 0 everywhere."""
    mesh = duct_mesh(30, 10, 8)
    settings = duct_settings()
    attach_patches(mesh, settings)
    result, (u, v, w) = run(mesh, settings)

    dx, dy, dz = mesh.spacing_m
    divergence = (
        (u[2:, 1:-1, 1:-1] - u[:-2, 1:-1, 1:-1]) / (2 * dx)
        + (v[1:-1, 2:, 1:-1] - v[1:-1, :-2, 1:-1]) / (2 * dy)
        + (w[1:-1, 1:-1, 2:] - w[1:-1, 1:-1, :-2]) / (2 * dz)
    )
    # Normalise against the only velocity-gradient scale in the problem, so the
    # number means "percent of a typical shear rate" rather than raw 1/s.
    scale = abs(u).max() / dy
    print(f"final continuity residual reported = {result.residuals.continuity[-1]:.4g}")
    print(f"cell-centred |div u| max  = {abs(divergence).max():.4g} 1/s "
          f"({abs(divergence).max() / scale * 100:.2f} % of u_max/dy)")
    print(f"cell-centred |div u| mean = {abs(divergence).mean():.4g} 1/s "
          f"({abs(divergence).mean() / scale * 100:.2f} % of u_max/dy)")


# --------------------------------------------------------------------------
# Case 4 -- mesh convergence


def case_mesh_convergence():
    """Refining the mesh must make the answer settle, not wander."""
    print("  ny   cells   iters  conv   u_max     u_max/u_mean  expected  shape err")
    for ny in (6, 10, 14, 20):
        mesh = duct_mesh(3 * ny, ny, 8)
        settings = duct_settings()
        attach_patches(mesh, settings)
        result, (u, _v, _w) = run(mesh, settings)
        profile = u[int(0.95 * mesh.shape[0]), :, mesh.shape[2] // 2]
        mean = profile.mean()
        ratio = profile.max() / mean if abs(mean) > 1e-12 else float("nan")
        print(f"  {ny:3d}  {mesh.cell_count:6d}  {result.iterations:5d}  "
              f"{str(result.converged):5s}  {profile.max():.5f}   {ratio:8.3f}  "
              f"{1.5 * ny / (ny - 2.0):8.3f}  {parabola_shape_error(profile):7.2f} %")


def case_residual_source():
    """Where does the continuity residual actually live?

    The floor near 1e-2 survived the inlet fix (0.0169 -> 0.0174), so the
    explanation that blamed the inlet discontinuity was wrong. The standing
    hypothesis is that _pressure_projection calls _apply_velocity_boundaries
    *after* correcting the velocity, re-zeroing wall cells the projection had
    just cleaned and re-introducing divergence in every cell adjacent to a wall.

    If that is right, the divergence must be concentrated in the one-cell shell
    against the walls and near-zero in the deep interior. If it is spread
    evenly, the hypothesis is dead and the cause is the scheme itself.
    """
    mesh = duct_mesh(30, 10, 8)
    settings = duct_settings()
    attach_patches(mesh, settings)
    result, (u, v, w) = run(mesh, settings)

    dx, dy, dz = mesh.spacing_m
    divergence = np.zeros(mesh.shape)
    divergence[1:-1, 1:-1, 1:-1] = (
        (u[2:, 1:-1, 1:-1] - u[:-2, 1:-1, 1:-1]) / (2 * dx)
        + (v[1:-1, 2:, 1:-1] - v[1:-1, :-2, 1:-1]) / (2 * dy)
        + (w[1:-1, 1:-1, 2:] - w[1:-1, 1:-1, :-2]) / (2 * dz)
    )
    magnitude = np.abs(divergence)

    # Shell = cells one step from any domain face; core = everything deeper.
    shell = np.zeros(mesh.shape, dtype=bool)
    shell[1, :, :] = shell[-2, :, :] = True
    shell[:, 1, :] = shell[:, -2, :] = True
    shell[:, :, 1] = shell[:, :, -2] = True
    core = np.zeros(mesh.shape, dtype=bool)
    core[2:-2, 2:-2, 2:-2] = True

    print(f"reported continuity residual = {result.residuals.continuity[-1]:.4g}")
    print(f"  wall-adjacent shell: mean |div| = {magnitude[shell].mean():.4g} 1/s "
          f"over {int(shell.sum())} cells")
    print(f"  deep interior core : mean |div| = {magnitude[core].mean():.4g} 1/s "
          f"over {int(core.sum())} cells")
    ratio = magnitude[shell].mean() / max(magnitude[core].mean(), 1e-30)
    print(f"  shell / core = {ratio:.1f}x")
    print()
    print("  hypothesis holds if the shell dominates by a wide margin;")
    print("  it is refuted if the two are comparable.")

    # Also report the share of total squared divergence sitting in the shell,
    # since that is what an RMS residual actually sums.
    total = float((magnitude ** 2).sum())
    if total > 0:
        print(f"  shell carries {float((magnitude[shell] ** 2).sum()) / total * 100:.1f}% "
              "of the total squared divergence")


# --------------------------------------------------------------------------
# Case 5 -- energy and buoyancy: the closed heated cavity (CFD-2)


def case_closed_cavity():
    """A sealed, heated, buoyant cavity -- the energy/buoyancy case.

    Every case above is isothermal forced flow with buoyancy off: nothing
    here ran the energy equation or buoyancy, and nothing asserted -- every
    case above only prints (docs/audit-2026-09-23.md triage). This closes
    both gaps, and its origin-invariance check is CFD-2's own regression:
    advecting the energy equation with a re-averaged, non-divergence-free
    cell velocity instead of the pressure projection's own face field was
    not conservative, and moving the temperature origin C -> K (the same
    physics) moved P02 Rev E's reported peak air temperature from 404.7 to
    1005.9 C (scratchpad/audit/cfd/p02_inspect.py). Film and Boussinesq beta
    are frozen to one ambient so this checks only the advection operator,
    the same reasoning tests.test_cfd_solver.py's unit-level regression uses.
    """
    settings = EnclosureCFDSettings(ambient_c=25.0)
    settings.geometry.width_mm = 30.0
    settings.geometry.depth_mm = 30.0
    settings.geometry.height_mm = 30.0
    settings.geometry.wall_heat_transfer_w_m2k = 8.0
    settings.solver.cell_size_mm = 6.0
    settings.solver.max_iterations = 400
    settings.solver.tolerance = 1e-6
    model = EnclosureModel(
        dimensions_mm=(30.0, 30.0, 30.0),
        obstacles=[CFDObstacle("HOT", (12, 12, 12, 18, 18, 18), 5.0, 0.5)],
    )
    mesh = CFDMeshGenerator().generate_mesh(model, settings)
    result = EnclosureCFDSolver().solve(mesh, settings)

    print(f"converged={result.converged} after {result.iterations} iterations")
    print(f"Tair_max={result.maximum_air_temperature_c:.2f} C  "
          f"Tsolid_max={result.maximum_solid_temperature_c:.2f} C  "
          f"energy balance error={result.energy_balance_error_pct:.3g}%")
    assert result.converged, "closed cavity did not converge"
    assert result.energy_balance_error_pct < 5.0, (
        f"energy balance error {result.energy_balance_error_pct:.3g}% exceeds 5%"
    )
    assert result.maximum_air_temperature_c > settings.ambient_c, "no heating reached the air"

    orig_film = EnclosureCFDSolver._film_coefficient
    orig_beta = EnclosureCFDSolver._boussinesq_beta
    ambient0 = settings.ambient_c
    EnclosureCFDSolver._film_coefficient = staticmethod(
        lambda dt, L, s, amb, e, facing="up", _o=orig_film, _a=ambient0:
        _o(dt, L, s, _a, e, facing)
    )
    EnclosureCFDSolver._boussinesq_beta = staticmethod(
        lambda ambient_c, _o=orig_beta, _a=ambient0: _o(_a)
    )
    try:
        mesh_k = CFDMeshGenerator().generate_mesh(model, settings)
        shifted = EnclosureCFDSettings(ambient_c=ambient0 + 273.15)
        shifted.geometry, shifted.solver = settings.geometry, settings.solver
        result_k = EnclosureCFDSolver().solve(mesh_k, shifted)
    finally:
        EnclosureCFDSolver._film_coefficient = staticmethod(orig_film)
        EnclosureCFDSolver._boussinesq_beta = staticmethod(orig_beta)

    difference = abs(
        result.maximum_air_temperature_c - (result_k.maximum_air_temperature_c - 273.15)
    )
    print(f"origin-invariance check: |T(C) - (T(K origin)-273.15)| = {difference:.3f} C")
    assert difference < 2.0, f"energy advection is not origin-invariant: {difference:.3f} C"


CASES = {
    "profile": case_profile,
    "residual": case_residual_source,
    "flux": case_flux,
    "divergence": case_divergence,
    "convergence": case_mesh_convergence,
    "cavity": case_closed_cavity,
}


def main(argv):
    selected = argv[1:] or list(CASES)
    for name in selected:
        if name not in CASES:
            raise SystemExit(f"unknown case {name!r}; choose from {', '.join(CASES)}")
        print("=" * 72)
        print(f"CASE: {name}")
        print("=" * 72)
        CASES[name]()
        print()


if __name__ == "__main__":
    main(sys.argv)
