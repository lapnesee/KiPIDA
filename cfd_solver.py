"""Volumetric steady enclosure CFD with Boussinesq buoyancy and CHT energy."""

import math
from dataclasses import replace as _replace

try:
    import numpy as np
    import scipy.sparse
    import scipy.sparse.linalg
except ImportError:
    np = None
    scipy = None

try:
    import pypardiso
except ImportError:
    pypardiso = None

try:
    from .models import CFDResidualHistory, EnclosureCFDResult
    from .compute_backend import SparseComputeBackend
except (ImportError, ValueError):
    from models import CFDResidualHistory, EnclosureCFDResult
    from compute_backend import SparseComputeBackend


class EnclosureCFDSolver:
    """Pseudo-transient projection solver on a structured cell-centred grid."""

    # AUTO routes to CUDA past this many nodes, below the runtime-wide
    # cuda_min_nodes default (100,000) but far above a typical enclosure
    # mesh. CFD's per-iteration work is many small/medium GENERAL solves
    # (the pressure Poisson system and the energy equation), not the few
    # large ones CUDA amortises its transfer and kernel-launch overhead
    # against. Measured on P02 Rev E with the user's saved runtime.json
    # (backend AUTO, cuda_min_nodes lowered to 1,000 for other domains):
    # 6,600 cells went to CUDA at 324 ms/iteration against 17 ms/iteration
    # on CPU (docs/audit-2026-09-23.md CFD-3, scratchpad p02_usercfg.py).
    CFD_CUDA_MIN_NODES = 100_000

    @staticmethod
    def _cfd_compute_settings(compute_settings, log_callback):
        """Raise AUTO's CUDA threshold for this solve; leave CPU/CUDA alone.

        A user-forced backend (CPU or CUDA) is respected unchanged -- this
        only guards the AUTO heuristic, and only when the configured
        threshold is lower than CFD needs. The caller's settings object is
        never mutated, so a request shared with another domain (DC, thermal)
        keeps its own threshold for their solves.
        """
        if compute_settings is None:
            return compute_settings
        backend = str(getattr(compute_settings, "backend", "AUTO") or "AUTO").upper()
        current = int(getattr(compute_settings, "cuda_min_nodes", 0) or 0)
        if backend != "AUTO" or current >= EnclosureCFDSolver.CFD_CUDA_MIN_NODES:
            return compute_settings
        if log_callback:
            log_callback(
                f"[CFD SOLVER] AUTO backend: using a {EnclosureCFDSolver.CFD_CUDA_MIN_NODES:,}-cell "
                f"CUDA threshold for this solve (configured cuda_min_nodes={current:,} is "
                "tuned for other domains' larger, less frequent solves; CFD's many small "
                "per-iteration solves measured 324 ms/it on CUDA against 17 ms/it on CPU "
                "at 6,600 cells)."
            )
        return _replace(compute_settings, cuda_min_nodes=EnclosureCFDSolver.CFD_CUDA_MIN_NODES)

    def __init__(self, debug=False, log_callback=None, compute_settings=None):
        self.debug = debug
        self.log_callback = log_callback
        compute_settings = self._cfd_compute_settings(compute_settings, log_callback)
        self.compute_backend = SparseComputeBackend(compute_settings, log_callback)
        self._last_compute = None
        # The mass-conservative face velocities the last _pressure_projection
        # call produced, for _solve_energy to advect with. See CFD-2 in
        # docs/audit-2026-09-23.md: set only inside _pressure_projection.
        self._face_velocity = None
        if np is None or scipy is None:
            raise ImportError("NumPy and SciPy are required for enclosure CFD.")

    def _log(self, message):
        if self.log_callback:
            self.log_callback(f"[CFD SOLVER] {message}")

    @staticmethod
    def _boussinesq_beta(ambient_c):
        """Thermal expansion coefficient of air for the Boussinesq force, 1/K.

        beta = 1/T for an ideal gas at absolute temperature T. The solver
        used to take it from FluidProperties.thermal_expansion_per_k, a
        constant (default 0.00335 = 1/298.15, i.e. 25 C) applied unchanged
        at any ambient -- wrong by 6% at P02 Rev E's 45 C ambient and worse
        further out (CFD-7, docs/audit-2026-09-23.md).
        """
        return 1.0 / (float(ambient_c) + 273.15)

    @staticmethod
    def _shift(array, axis, amount, fill=None):
        shifted = np.roll(array, amount, axis=axis)
        selector = [slice(None)] * array.ndim
        selector[axis] = 0 if amount > 0 else -1
        if fill is None:
            source = [slice(None)] * array.ndim
            source[axis] = 0 if amount > 0 else -1
            shifted[tuple(selector)] = array[tuple(source)]
        else:
            shifted[tuple(selector)] = fill
        return shifted

    def _gradient(self, field, spacing, fluid):
        gradients = []
        for axis, delta in enumerate(spacing):
            plus = self._shift(field, axis, -1)
            minus = self._shift(field, axis, 1)
            value = (plus - minus) / (2.0 * delta)
            value[~fluid] = 0.0
            gradients.append(value)
        return gradients

    def _laplacian_velocity(self, field, spacing, fluid):
        result = np.zeros_like(field)
        for axis, delta in enumerate(spacing):
            plus = self._shift(field, axis, -1, 0.0)
            minus = self._shift(field, axis, 1, 0.0)
            plus_fluid = self._shift(fluid, axis, -1, False)
            minus_fluid = self._shift(fluid, axis, 1, False)
            plus = np.where(plus_fluid, plus, 0.0)
            minus = np.where(minus_fluid, minus, 0.0)
            result += (plus - 2.0 * field + minus) / (delta * delta)
        result[~fluid] = 0.0
        return result

    @staticmethod
    def _normal(face):
        return {
            "XMIN": (0, 1.0), "XMAX": (0, -1.0),
            "YMIN": (1, 1.0), "YMAX": (1, -1.0),
            "ZMIN": (2, 1.0), "ZMAX": (2, -1.0),
        }[str(face).upper()]

    @staticmethod
    def _inside_neighbor(cell, face, shape):
        i, j, k = cell
        if face == "XMIN": i += 1
        elif face == "XMAX": i -= 1
        elif face == "YMIN": j += 1
        elif face == "YMAX": j -= 1
        elif face == "ZMIN": k += 1
        else: k -= 1
        return (min(shape[0] - 1, max(0, i)),
                min(shape[1] - 1, max(0, j)),
                min(shape[2] - 1, max(0, k)))

    def _inlet_weights(self, mesh, mapped):
        """Per-cell shape factors for an inlet, normalised to preserve flux.

        A uniform plug across the whole patch is discontinuous wherever the
        patch meets a wall: the wall cell must be zero by no-slip, the inlet
        cell beside it carries the full velocity. The pressure solver cannot
        reconcile that, and on the validation duct it lost 5.2% of the mass in
        the first cell (docs/validation-cfd.md, finding 2). Fixing it took the
        duct from -1.62% to -0.25%, and the first interior plane from 0.770 to
        0.997 of the imposed flux.

        It did *not* move the continuity residual floor (0.0169 -> 0.0174),
        which had been attributed to this same discontinuity. That attribution
        was wrong; the floor has another cause.

        Only the cells that genuinely must vanish are zeroed -- those sitting on
        a transverse domain wall or against a solid -- and the rest are scaled
        up so the volumetric flow the user asked for is unchanged. In effect the
        inlet becomes plug flow across the *open* area rather than across the
        nominal area.

        A parabolic taper was tried first and rejected on measurement: on a
        three-cell-wide patch it zeroes both edges and forces the entire flow
        through the middle cell, which took the smoke case from 10% mass error
        to 15%. Coarse patches are the normal case for this tool, so the gentler
        rule wins.

        A patch floating in the middle of a face touches nothing and keeps a
        flat profile, exactly as before.
        """
        cells = list(mapped.cells)
        if not cells:
            return {}
        axis, _sign = self._normal(str(mapped.patch.face).upper())
        transverse = [a for a in range(3) if a != axis]
        solid = np.asarray(mesh.solid_mask, dtype=bool)

        def must_vanish(cell):
            for a in transverse:
                if cell[a] == 0 or cell[a] == mesh.shape[a] - 1:
                    return True
                for step in (-1, 1):
                    neighbour = list(cell)
                    neighbour[a] += step
                    if 0 <= neighbour[a] < mesh.shape[a] and solid[tuple(neighbour)]:
                        return True
            return False

        shapes = [0.0 if must_vanish(cell) else 1.0 for cell in cells]
        mean = sum(shapes) / len(shapes)
        if mean <= 1e-12:
            # Every cell touches something; refusing to blank the whole inlet is
            # better than silently delivering no flow at all.
            return {cell: 1.0 for cell in cells}
        return {cell: shape / mean for cell, shape in zip(cells, shapes)}

    def _apply_velocity_boundaries(self, mesh, velocity, pressure):
        fluid = mesh.fluid_mask
        for component in velocity:
            component[~fluid] = 0.0
            component[0, :, :] = 0.0
            component[-1, :, :] = 0.0
            component[:, 0, :] = 0.0
            component[:, -1, :] = 0.0
            component[:, :, 0] = 0.0
            component[:, :, -1] = 0.0
        for mapped in mesh.patch_cells:
            patch = mapped.patch
            kind = str(patch.kind).upper()
            face = str(patch.face).upper()
            axis, inward_sign = self._normal(face)
            weights = None
            if kind in {"INLET", "FAN"}:
                cache = getattr(self, "_inlet_weight_cache", None)
                if cache is None:
                    cache = self._inlet_weight_cache = {}
                key = id(mapped)
                if key not in cache:
                    cache[key] = self._inlet_weights(mesh, mapped)
                weights = cache[key]
            for cell in mapped.cells:
                neighbor = self._inside_neighbor(cell, face, mesh.shape)
                if kind in {"INLET", "FAN"}:
                    speed = inward_sign * patch.velocity_m_s * weights.get(cell, 1.0)
                    for index, component in enumerate(velocity):
                        component[cell] = speed if index == axis else 0.0
                    pressure[cell] = pressure[neighbor]
                elif kind in {"OUTLET", "VENT"}:
                    for component in velocity:
                        component[cell] = component[neighbor]
                    pressure[cell] = patch.pressure_pa
                elif kind == "WALL":
                    for component in velocity:
                        component[cell] = 0.0

    def _pressure_system(self, mesh, outlet_cells, reference):
        """Assemble the pressure Poisson operator once for the whole solve.

        The projection previously cleaned divergence with hand-rolled Jacobi
        sweeps. Jacobi damps low-frequency error at a rate that scales with the
        square of the grid size, and it is precisely that long-wavelength
        component which enforces global mass balance, so the sweep count bought
        accuracy linearly and never reached it: 30/60/240/960 sweeps gave
        -8.2/-5.2/-1.6/-0.6% mass error on the validation duct.

        The operator depends only on geometry -- the fluid mask and the cell
        spacing -- while the right-hand side changes every iteration with the
        divergence and the pseudo time step. So it is assembled once and handed
        to SparseComputeBackend, which the project already owns and which keeps
        its CUDA workspace resident across solves with a stable cache key.

        Rows are made identity for three kinds of cell: solid cells, which carry
        no pressure; outlet cells, which are Dirichlet at the patch pressure;
        and, when there is no outlet at all, one reference cell to pin the
        otherwise-singular pure-Neumann system.

        That row surgery breaks symmetry -- the row is cleared but the
        corresponding column is not -- so the system is solved as GENERAL rather
        than SPD, the same choice _solve_energy makes for the same reason.
        """
        shape = mesh.shape
        fluid = np.asarray(mesh.fluid_mask, dtype=bool)
        count = mesh.cell_count
        index = np.arange(count).reshape(shape)

        fixed = ~fluid
        for cell, _value in outlet_cells:
            fixed[cell] = True
        if not outlet_cells and reference is not None:
            fixed[reference] = True

        rows, columns, values = [], [], []
        diagonal = np.zeros(shape, dtype=float)
        movable = fluid & ~fixed
        for axis, spacing in enumerate(mesh.spacing_m):
            coefficient = 1.0 / (spacing * spacing)
            for amount in (-1, 1):
                neighbour_fluid = self._shift(fluid, axis, amount, False)
                neighbour_index = self._shift(index, axis, amount, 0)
                # A neighbour outside the domain is not fluid, so _shift's
                # False fill already excludes it: the operator gets a natural
                # zero-gradient wall without a special case.
                connected = movable & neighbour_fluid
                diagonal += coefficient * connected
                rows.append(index[connected])
                columns.append(neighbour_index[connected])
                values.append(np.full(int(connected.sum()), -coefficient))

        # An isolated fluid cell has no neighbour to balance against; giving it
        # a unit row keeps the matrix non-singular instead of dividing by zero.
        isolated = movable & (diagonal <= 0.0)
        diagonal[isolated] = 1.0
        diagonal[fixed] = 1.0
        rows.append(index.reshape(-1))
        columns.append(index.reshape(-1))
        values.append(diagonal.reshape(-1))

        matrix = scipy.sparse.csr_matrix(
            (np.concatenate(values),
             (np.concatenate(rows), np.concatenate(columns))),
            shape=(count, count), dtype=float,
        )
        matrix.sort_indices()
        return matrix, fixed, isolated

    def _pressure_projection(self, mesh, velocity, pressure, density, dt):
        fluid = mesh.fluid_mask
        shape = mesh.shape
        faces = []
        for axis, component in enumerate(velocity):
            face_shape = list(shape)
            face_shape[axis] += 1
            face_velocity = np.zeros(face_shape, dtype=float)
            lower_face = [slice(None)] * 3
            upper_face = [slice(None)] * 3
            lower_cell = [slice(None)] * 3
            upper_cell = [slice(None)] * 3
            lower_face[axis] = slice(1, shape[axis])
            upper_face[axis] = slice(1, shape[axis])
            lower_cell[axis] = slice(0, shape[axis] - 1)
            upper_cell[axis] = slice(1, shape[axis])
            adjacent = fluid[tuple(lower_cell)] & fluid[tuple(upper_cell)]
            face_velocity[tuple(lower_face)] = np.where(
                adjacent,
                0.5 * (component[tuple(lower_cell)] + component[tuple(upper_cell)]),
                0.0,
            )
            first_face = [slice(None)] * 3
            last_face = [slice(None)] * 3
            first_cell = [slice(None)] * 3
            last_cell = [slice(None)] * 3
            first_face[axis] = 0
            last_face[axis] = shape[axis]
            first_cell[axis] = 0
            last_cell[axis] = shape[axis] - 1
            face_velocity[tuple(first_face)] = np.where(
                fluid[tuple(first_cell)], component[tuple(first_cell)], 0.0
            )
            face_velocity[tuple(last_face)] = np.where(
                fluid[tuple(last_cell)], component[tuple(last_cell)], 0.0
            )
            faces.append(face_velocity)

        divergence = np.zeros(shape, dtype=float)
        for axis, spacing in enumerate(mesh.spacing_m):
            lower = [slice(None)] * 3
            upper = [slice(None)] * 3
            lower[axis] = slice(0, shape[axis])
            upper[axis] = slice(1, shape[axis] + 1)
            divergence += (faces[axis][tuple(upper)] - faces[axis][tuple(lower)]) / spacing
        divergence[~fluid] = 0.0
        rhs = density / max(dt, 1e-12) * divergence
        coefficients = tuple(1.0 / spacing ** 2 for spacing in mesh.spacing_m)
        reference = tuple(np.argwhere(fluid)[0])
        outlet_cells = []
        for mapped in mesh.patch_cells:
            if str(mapped.patch.kind).upper() in {"OUTLET", "VENT"}:
                outlet_cells.extend((cell, mapped.patch.pressure_pa) for cell in mapped.cells)
        cached = getattr(self, "_poisson", None)
        if cached is None:
            cached = self._poisson = self._pressure_system(mesh, outlet_cells, reference)
        matrix, fixed, isolated = cached

        # Only the right-hand side moves between iterations: the operator is
        # geometry, the load is the divergence to be cleaned.
        load = -rhs.copy()
        load[fixed] = 0.0
        load[isolated] = 0.0
        for cell, value in outlet_cells:
            load[cell] = value
        solution = self.compute_backend.solve(
            matrix, load.reshape(-1), system_kind="GENERAL",
            cache_key=self._poisson_cache_key, matrix_values_static=True,
        )
        pressure[:] = solution.values.reshape(mesh.shape)
        pressure[~fluid] = 0.0
        if not outlet_cells and reference is not None:
            pressure[reference] = 0.0
        for cell, value in outlet_cells:
            pressure[cell] = value

        for axis in range(3):
            lower = [slice(None)] * 3
            upper = [slice(None)] * 3
            face_slice = [slice(None)] * 3
            lower[axis] = slice(0, shape[axis] - 1)
            upper[axis] = slice(1, shape[axis])
            face_slice[axis] = slice(1, shape[axis])
            adjacent = fluid[tuple(lower)] & fluid[tuple(upper)]
            correction = dt / density * (
                pressure[tuple(upper)] - pressure[tuple(lower)]
            ) / mesh.spacing_m[axis]
            faces[axis][tuple(face_slice)] -= np.where(adjacent, correction, 0.0)
            cell_lower = [slice(None)] * 3
            cell_upper = [slice(None)] * 3
            cell_lower[axis] = slice(0, shape[axis])
            cell_upper[axis] = slice(1, shape[axis] + 1)
            velocity[axis][fluid] = (
                0.5 * (faces[axis][tuple(cell_lower)] + faces[axis][tuple(cell_upper)])
            )[fluid]
        self._apply_velocity_boundaries(mesh, velocity, pressure)

        incoming = 0.0
        outlets = []
        areas = (mesh.spacing_m[1] * mesh.spacing_m[2],
                 mesh.spacing_m[0] * mesh.spacing_m[2],
                 mesh.spacing_m[0] * mesh.spacing_m[1])
        for mapped in mesh.patch_cells:
            kind = str(mapped.patch.kind).upper()
            axis, inward_sign = self._normal(str(mapped.patch.face).upper())
            if kind in {"INLET", "FAN"}:
                incoming += sum(
                    max(0.0, inward_sign * velocity[axis][cell]) * areas[axis]
                    for cell in mapped.cells
                )
            elif kind in {"OUTLET", "VENT"}:
                outlets.extend((cell, axis, inward_sign, areas[axis]) for cell in mapped.cells)
        outlet_area = sum(item[3] for item in outlets)
        # Record what the projection actually delivered to the outlet *before*
        # the fix-up below overwrites it. Comparing the inflow against the
        # imposed outflow is a tautology -- it made mass_balance_error_pct read
        # 4e-14% on a duct that was losing 5.2% of its mass. This is the honest
        # measurement: how far the solver was off before being corrected.
        self._natural_outflow = sum(
            max(0.0, -inward_sign * velocity[axis][cell]) * area
            for cell, axis, inward_sign, area in outlets
        )
        self._imposed_inflow = incoming
        if incoming > 0.0 and outlet_area > 0.0:
            outward_velocity = incoming / outlet_area
            for cell, axis, inward_sign, _ in outlets:
                velocity[axis][cell] = -inward_sign * outward_velocity
                face_cell = list(cell)
                if inward_sign < 0.0:
                    face_cell[axis] += 1
                faces[axis][tuple(face_cell)] = velocity[axis][cell]

        # Hand the divergence-free faces to _solve_energy. Re-averaging the
        # *cell* velocities there -- the previous scheme -- discards the
        # projection: on P02 Rev E it left Sum|rho cp div(V) T| = 21.5 W of
        # spurious source/sink for 4.75 W injected, and moving the °C origin
        # to K (same physics, frozen velocity) moved the reported peak air
        # temperature from 404.7 to 1005.9 C -- a conservative scheme cannot
        # do that. Storing the faces this projection actually solved for
        # removes the re-averaging step instead of tuning it.
        self._face_velocity = faces

        corrected = np.zeros(shape, dtype=float)
        for axis, face_velocity in enumerate(faces):
            lower = [slice(None)] * 3
            upper = [slice(None)] * 3
            lower[axis] = slice(0, shape[axis])
            upper[axis] = slice(1, shape[axis] + 1)
            corrected += (face_velocity[tuple(upper)] - face_velocity[tuple(lower)]) / mesh.spacing_m[axis]
        corrected[~fluid] = 0.0
        # Measure only where the projection has degrees of freedom.
        #
        # _apply_velocity_boundaries prescribes the velocity in the six outer
        # layers and in every patch cell. The pressure field cannot move those
        # values, so whatever divergence they carry is a property of the
        # boundary condition, not a convergence failure -- averaging it into
        # the residual reports the solver as stuck on cells it does not own.
        #
        # This was measured before it was believed: on the validation duct the
        # one-cell shell against the walls holds 99.9% of the total squared
        # divergence, and its mean is 9.5x the deep interior's.
        free = fluid & self._unprescribed_mask(mesh)
        if not np.any(free):
            free = fluid
        return float(np.sqrt(np.mean(corrected[free] ** 2))) if np.any(free) else 0.0

    def _unprescribed_mask(self, mesh):
        """Cells whose velocity the pressure projection is free to change."""
        cached = getattr(self, "_free_cells", None)
        if cached is not None:
            return cached
        free = np.ones(mesh.shape, dtype=bool)
        free[0, :, :] = free[-1, :, :] = False
        free[:, 0, :] = free[:, -1, :] = False
        free[:, :, 0] = free[:, :, -1] = False
        for mapped in mesh.patch_cells:
            for cell in mapped.cells:
                free[cell] = False
        self._free_cells = free
        return free

    @staticmethod
    def _film_coefficient(delta_t_k, length_m, speed_m_s, ambient_c, emissivity, facing="up"):
        """Surface film at a solid-air face, W/m^2K.

        Without this the only path off a hot component is conduction into still
        air and advection between cells, and neither can carry the heat at
        millimetre resolution: one 5 mm face passes 3.6e-3 W/K by advection and
        2.6e-4 W/K by conduction, so shedding 1.45 W would need 402 K. The
        reference board duly reported a 339 C solid against 72.6 C from the 3D
        thermal analysis of the same board at the same power.

        The convective boundary layer is thinner than a cell and always will be
        at these mesh sizes, so it is modelled rather than resolved -- which is
        what the 3D thermal solver already does, through the same correlations.
        Reusing surface_convection keeps the two analyses answering with one
        physics instead of two.

        ``facing`` is "up" for a solid whose exposed face is beneath the air
        (natural_convection_h's plume case, coefficient 0.54) and anything
        else -- "down" for a solid above its air, or a vertical side face --
        for the 0.27 case (see CFD-6, docs/audit-2026-09-23.md: every call
        used to pass "up" regardless of the face's actual orientation, which
        over-estimated a downward-facing bottom face by about 2x, 0.54 vs
        0.27).
        """
        try:
            from .import surface_convection
        except (ImportError, ValueError):
            import surface_convection

        natural = surface_convection.natural_convection_h(
            delta_t_k, length_m, facing=facing, ambient_c=ambient_c,
        )
        forced = surface_convection.forced_convection_h(
            speed_m_s, length_m, ambient_c=ambient_c, delta_t_k=delta_t_k,
        )
        film = surface_convection.combined_h(natural, forced)
        if emissivity > 0.0:
            film += surface_convection.radiation_h(
                emissivity, ambient_c + delta_t_k, ambient_c,
            )
        return film

    def _solve_energy(self, mesh, settings, velocity, previous):
        shape = mesh.shape
        count = mesh.cell_count
        dx, dy, dz = mesh.spacing_m
        spacings = (dx, dy, dz)
        areas = (dy * dz, dx * dz, dx * dy)
        fluid = mesh.fluid_mask
        props = settings.fluid
        conductivity = np.where(
            fluid, props.conductivity_w_mk, mesh.solid_conductivity_w_mk
        )
        rhs = mesh.heat_sources_w.astype(float).copy()
        # Patches as whole-grid masks. A later patch on the same cell and face
        # overrides an earlier one, as the per-cell lookup it replaces did.
        inlet = np.zeros(shape, dtype=bool)
        inlet_temperature = np.zeros(shape, dtype=float)
        face_patch = {}
        for mapped in mesh.patch_cells:
            face = str(mapped.patch.face).upper()
            kind = str(mapped.patch.kind).upper()
            outlet, outlet_temperature = face_patch.setdefault(
                face, (np.zeros(shape, dtype=bool), np.zeros(shape, dtype=float))
            )
            for cell in mapped.cells:
                outlet[cell] = kind in {"OUTLET", "VENT"}
                outlet_temperature[cell] = float(mapped.patch.temperature_c)
                if kind in {"INLET", "FAN"}:
                    inlet[cell] = True
                    inlet_temperature[cell] = float(mapped.patch.temperature_c)
        wall_h = max(0.0, float(settings.geometry.wall_heat_transfer_w_m2k))
        ambient = float(settings.ambient_c)
        # Length scale for the plate correlations at a solid-air face. The mean
        # cell size is used because the mesh is all the geometry the solver has
        # here -- it does not carry each obstacle's own A/P (surface_convection.
        # characteristic_length_m), which the 3D thermal solver uses instead
        # (CFD-6, docs/audit-2026-09-23.md). A length wrong by a factor of two
        # moves natural h (~L^-1/4) by 19% and forced h (~L^-1/2) by 41%; both
        # are inside the correlations' own spread, and a missing film was wrong
        # by a factor of forty. Stated as a limitation below rather than
        # plumbed through, since the mesh has no per-obstacle footprint to give
        # it.
        film_length_m = float(sum(mesh.spacing_m)) / 3.0
        emissivity = max(0.0, min(1.0, float(
            getattr(settings.geometry, "emissivity", 0.0) or 0.0
        )))
        capacity_density = props.density_kg_m3 * props.heat_capacity_j_kgk
        index = np.arange(count).reshape(shape)
        diagonal = np.zeros(shape, dtype=float)
        rows, columns, values = [], [], []
        # The operator is assembled face by face with whole-array operations.
        # It used to be one Python pass over every cell and each of its six
        # faces into a LIL matrix: 0.67 s per energy solve on a 6,600-cell
        # enclosure, called every fifth iteration, which made the energy
        # equation nine tenths of the CFD's run time.
        for axis in range(3):
            area = areas[axis]
            lower = [slice(None)] * 3
            upper = [slice(None)] * 3
            lower[axis] = slice(0, shape[axis] - 1)
            upper[axis] = slice(1, shape[axis])
            lower, upper = tuple(lower), tuple(upper)
            fluid_lower, fluid_upper = fluid[lower], fluid[upper]
            k_lower, k_upper = conductivity[lower], conductivity[upper]
            diffusion = (
                2.0 * k_lower * k_upper / np.maximum(k_lower + k_upper, 1e-30)
                * area / spacings[axis]
            )
            # A solid-air face. The path is: conduct from the solid cell centre
            # to its surface, then cross the boundary layer.
            #
            #   R = (spacing/2)/(k_solid * A) + 1/(h * A)
            #
            # The air-side cell-to-cell conduction is dropped rather than kept
            # in series, because it is not a model of the interface at all --
            # it is what the cell-centred scheme produces in the absence of
            # one, and adding the film in series with it only made the solid
            # hotter still (162.7 C -> 216.9 C on the reproduction case), since
            # series can only lower a conductance.
            #
            # h is evaluated on the previous iterate, the same lagging the
            # electro-thermal loop already uses. The correlations are scalar,
            # so they run per interface face -- a small share of all faces.
            interface = fluid_lower != fluid_upper
            if np.any(interface):
                speed = np.sqrt(sum(component ** 2 for component in velocity))
                delta_t = np.abs(previous[lower] - previous[upper])[interface]
                air_speed = np.where(fluid_lower, speed[lower], speed[upper])[interface]
                k_solid = np.maximum(np.where(fluid_lower, k_upper, k_lower)[interface], 1e-9)
                # Which way the solid's exposed face points. Only the two
                # z-axis cases differ: a solid below its air (fluid on the
                # upper side) has a top face driving a plume, "up"; a solid
                # above its air (fluid below) has a bottom face trapping the
                # boundary layer, "down". A side face (x or y axis) is
                # vertical, the same 0.27 coefficient as "down". See CFD-6.
                if axis == 2:
                    # fluid_lower True means the solid is the *upper* cell:
                    # its exposed face is its underside, facing down into the
                    # air beneath it. fluid_lower False puts the solid below
                    # its air, exposed face up, driving the plume.
                    facing = np.where(fluid_lower[interface], "down", "up")
                else:
                    facing = np.full(int(interface.sum()), "down")
                film = np.fromiter(
                    (self._film_coefficient(float(dt), film_length_m, float(v),
                                            ambient, emissivity, str(f))
                     for dt, v, f in zip(delta_t, air_speed, facing)),
                    dtype=float, count=delta_t.size,
                )
                resistance = (
                    0.5 * spacings[axis] / (k_solid * area)
                    + 1.0 / np.maximum(film * area, 1e-30)
                )
                diffusion[interface] = 1.0 / resistance
            # Advect with the face velocity the pressure projection actually
            # solved for, not a re-average of the cell values on either side.
            # The two agree only where the field is already divergence-free;
            # everywhere else the re-average reintroduces the divergence the
            # projection just removed, and this face is exactly where
            # div(V) is evaluated for the energy equation. See CFD-2.
            projected = self._face_velocity
            if projected is not None and projected[axis].shape == tuple(
                s + (1 if a == axis else 0) for a, s in enumerate(shape)
            ):
                face_velocity = np.where(
                    fluid_lower & fluid_upper, projected[axis][upper], 0.0,
                )
            else:
                face_velocity = np.where(
                    fluid_lower & fluid_upper,
                    0.5 * (velocity[axis][lower] + velocity[axis][upper]), 0.0,
                )
            # Flux leaving the lower cell through this face; the upper cell
            # sees the same face with the opposite sign.
            flux = capacity_density * area * face_velocity
            diagonal[lower] += diffusion + np.maximum(flux, 0.0)
            diagonal[upper] += diffusion + np.maximum(-flux, 0.0)
            for row_cells, column_cells, coupling in (
                (lower, upper, diffusion + np.maximum(-flux, 0.0)),
                (upper, lower, diffusion + np.maximum(flux, 0.0)),
            ):
                keep = ~inlet[row_cells]
                rows.append(index[row_cells][keep])
                columns.append(index[column_cells][keep])
                values.append(-coupling[keep])
            for face, side in ((("XMIN", "YMIN", "ZMIN")[axis], 0),
                               (("XMAX", "YMAX", "ZMAX")[axis], shape[axis] - 1)):
                boundary = [slice(None)] * 3
                boundary[axis] = side
                boundary = tuple(boundary)
                outlet, outlet_temperature = face_patch.get(
                    face, (np.zeros(shape, dtype=bool), np.zeros(shape, dtype=float))
                )
                outlet = outlet[boundary]
                _, inward_sign = self._normal(face)
                outward = -inward_sign * velocity[axis][boundary]
                capacity = capacity_density * area * np.abs(outward)
                wall = wall_h * area
                diagonal[boundary] += np.where(outlet, capacity, wall)
                rhs[boundary] += np.where(
                    outlet,
                    np.where(outward < 0.0, capacity * outlet_temperature[boundary], 0.0),
                    wall * ambient,
                )
        diagonal = np.maximum(diagonal, 1e-20)
        diagonal[inlet] = 1.0
        rhs[inlet] = inlet_temperature[inlet]
        rows.append(index.reshape(-1))
        columns.append(index.reshape(-1))
        values.append(diagonal.reshape(-1))
        csr = scipy.sparse.csr_matrix(
            (np.concatenate(values), (np.concatenate(rows), np.concatenate(columns))),
            shape=(count, count), dtype=float,
        )
        csr.sort_indices()
        rhs = rhs.reshape(-1)
        compute = self.compute_backend.solve(csr, rhs, system_kind="GENERAL")
        self._last_compute = compute.metadata
        temperatures = compute.values.reshape(shape)
        if np.any(~np.isfinite(temperatures)):
            raise ValueError("CFD energy solution contains non-finite temperatures.")
        residual = float(np.max(np.abs(temperatures - previous)))
        return temperatures, residual

    def _flow_balance(self, mesh, velocity):
        """Percentage of the inflow the solver failed to carry to the outlet.

        This compares the imposed inflow against the outflow the pressure
        projection *produced*, captured before the outlet fix-up overwrites it.
        The previous version compared the inflow against the imposed outflow,
        which are the same number by construction; it reported 4e-14% on a duct
        that was losing 5.2% of its mass. See docs/validation-cfd.md.

        Falls back to the patch-based comparison only when no projection has run
        yet, so the value is never fabricated.
        """
        incoming = float(getattr(self, "_imposed_inflow", 0.0))
        outgoing = getattr(self, "_natural_outflow", None)
        if outgoing is None:
            areas = (mesh.spacing_m[1] * mesh.spacing_m[2],
                     mesh.spacing_m[0] * mesh.spacing_m[2],
                     mesh.spacing_m[0] * mesh.spacing_m[1])
            incoming = outgoing = 0.0
            for mapped in mesh.patch_cells:
                kind = str(mapped.patch.kind).upper()
                if kind not in {"INLET", "FAN", "OUTLET", "VENT"}:
                    continue
                axis, inward_sign = self._normal(str(mapped.patch.face).upper())
                for cell in mapped.cells:
                    inward_velocity = inward_sign * velocity[axis][cell]
                    if inward_velocity >= 0:
                        incoming += inward_velocity * areas[axis]
                    else:
                        outgoing += -inward_velocity * areas[axis]
        outgoing = float(outgoing)
        if incoming + outgoing <= 1e-15:
            # A sealed enclosure has no through-flow, so there is nothing to
            # conserve across a boundary. Returning 0.0 here would print
            # "mass error=0%" and read as a conservation check that passed --
            # the same false reassurance as the tautology this replaced, just
            # arrived at differently. None means "not applicable".
            return None
        return abs(incoming - outgoing) / max(incoming, outgoing, 1e-15) * 100.0

    # Board orientation -> the mesh axis running through its thickness (its
    # normal). Matches EnclosureModelBuilder._oriented_bounds: XY keeps the
    # board flat, thin in z; XZ and YZ stand it on edge, thin in y or x.
    _ORIENTATION_NORMAL_AXIS = {"XY": 2, "XZ": 1, "YZ": 0}

    @staticmethod
    def _board_free_stream(mesh, speed, board_orientation="XY"):
        """Mean air speed approaching the board, in m/s, with its cell count.

        The flat-plate correlation Nu = 0.664 Re^0.5 Pr^(1/3) takes the
        *free-stream* velocity: it derives the boundary layer analytically, so
        feeding it a near-wall speed would count the same physics twice and
        under-predict the coefficient.

        That happens to align with what a coarse enclosure mesh can be trusted
        for. The validation duct showed the bulk speed converging to 0.4% of the
        analytic answer once resolved, while the wall-adjacent layer is exactly
        where a 5 mm cell is least reliable -- at six cells across a channel the
        solver produced no boundary layer at all.

        So this samples fluid cells at a stand-off of two cells from any solid
        and from the enclosure walls, over the board's footprint. It returns
        0.0 rather than a fabricated fallback when the mesh is too coarse to
        offer such cells, which is the caller's signal to stay with natural
        convection.
        """
        names = getattr(mesh, "obstacle_names", None)
        if names is None:
            return 0.0, 0
        board = np.asarray(names) == "PCB"
        if not np.any(board):
            return 0.0, 0
        # Dilate the solid mask by two cells, the walls included (the True
        # fill); anything still fluid afterwards is at least two cells clear of
        # every obstacle and of the no-slip enclosure.
        near_solid = np.asarray(mesh.solid_mask, dtype=bool).copy()
        for _ in range(2):
            grown = near_solid.copy()
            for axis in range(3):
                grown |= EnclosureCFDSolver._shift(near_solid, axis, 1, True)
                grown |= EnclosureCFDSolver._shift(near_solid, axis, -1, True)
            near_solid = grown
        # Restrict to the board's footprint, so a fan blowing through an empty
        # corner of a large enclosure cannot masquerade as flow over it. Along
        # the board's normal, the slab runs the full enclosure: the air to
        # sample is above and below the board. It used to stop at the board's
        # own thickness, one layer of cells that is solid wherever the board
        # is, so no mesh could ever offer a sample: P02 Rev E's 33 x 20 x 10
        # enclosure returned none at 21:58.
        #
        # The normal used to be guessed from the populated mask's own extent
        # (argmin of upper - lower): whichever axis looked thinnest. A board
        # snapped to one cell on every axis (CFD-1) has extent (0, 0, 0), so
        # argmin trivially picked axis 0 regardless of which way the board
        # actually faces -- on P02 that sampled a line running through the
        # board's own plane, not the air above and below it: 7 cells at
        # 0.0117 m/s (docs/audit-2026-09-23.md CFD-5, commit 15666c8). The
        # orientation the board was actually built with is not a guess.
        normal = EnclosureCFDSolver._ORIENTATION_NORMAL_AXIS.get(
            str(board_orientation or "XY").upper(), 2
        )
        extent = np.argwhere(board)
        slab = np.zeros(mesh.shape, dtype=bool)
        lower = extent.min(axis=0)
        upper = extent.max(axis=0)
        lower[normal], upper[normal] = 0, mesh.shape[normal] - 1
        slab[lower[0]:upper[0] + 1, lower[1]:upper[1] + 1, lower[2]:upper[2] + 1] = True
        sample = mesh.fluid_mask & ~near_solid & slab
        if not np.any(sample):
            return 0.0, 0
        return float(np.mean(speed[sample])), int(np.count_nonzero(sample))

    def _energy_balance(self, mesh, settings, temperature, velocity):
        ambient = float(settings.ambient_c)
        wall_h = max(0.0, float(settings.geometry.wall_heat_transfer_w_m2k))
        dx, dy, dz = mesh.spacing_m
        areas = (dy * dz, dx * dz, dx * dy)
        open_cells = set()
        for mapped in mesh.patch_cells:
            if str(mapped.patch.kind).upper() in {"INLET", "FAN", "OUTLET", "VENT"}:
                open_cells.update(
                    (cell, str(mapped.patch.face).upper()) for cell in mapped.cells
                )
        loss = 0.0
        for face, axis, selector in (
            ("XMIN", 0, (0, slice(None), slice(None))),
            ("XMAX", 0, (-1, slice(None), slice(None))),
            ("YMIN", 1, (slice(None), 0, slice(None))),
            ("YMAX", 1, (slice(None), -1, slice(None))),
            ("ZMIN", 2, (slice(None), slice(None), 0)),
            ("ZMAX", 2, (slice(None), slice(None), -1)),
        ):
            for local in np.argwhere(np.ones(temperature[selector].shape, dtype=bool)):
                if axis == 0:
                    cell = (0 if face == "XMIN" else mesh.shape[0] - 1,
                            int(local[0]), int(local[1]))
                elif axis == 1:
                    cell = (int(local[0]), 0 if face == "YMIN" else mesh.shape[1] - 1,
                            int(local[1]))
                else:
                    cell = (int(local[0]), int(local[1]),
                            0 if face == "ZMIN" else mesh.shape[2] - 1)
                if (cell, face) not in open_cells:
                    loss += wall_h * areas[axis] * (temperature[cell] - ambient)
        props = settings.fluid
        for mapped in mesh.patch_cells:
            kind = str(mapped.patch.kind).upper()
            if kind not in {"INLET", "FAN", "OUTLET", "VENT"}:
                continue
            axis, inward_sign = self._normal(str(mapped.patch.face).upper())
            for cell in mapped.cells:
                outward = -inward_sign * velocity[axis][cell]
                loss += (props.density_kg_m3 * props.heat_capacity_j_kgk * areas[axis] *
                         outward * (temperature[cell] - ambient))
        heat = float(np.sum(mesh.heat_sources_w))
        if heat <= 1e-15:
            return 0.0
        return abs(float(loss) - heat) / heat * 100.0

    def solve(self, mesh, settings, progress_callback=None, cancel_callback=None):
        shape = mesh.shape
        fluid = mesh.fluid_mask
        if not np.any(fluid):
            raise ValueError("The enclosure CFD mesh has no fluid cells.")
        pressure = np.zeros(shape, dtype=float)
        velocity = [np.zeros(shape, dtype=float) for _ in range(3)]
        temperature = np.full(shape, float(settings.ambient_c), dtype=float)
        self._apply_velocity_boundaries(mesh, velocity, pressure)
        props = settings.fluid
        density = max(float(props.density_kg_m3), 1e-9)
        viscosity = max(float(props.dynamic_viscosity_pa_s), 1e-12) / density
        controls = settings.solver
        relaxation = max(0.01, min(1.0, float(controls.relaxation)))
        # The Poisson operator is geometry, so it is built on first use and
        # reused for every iteration of this solve. Dropping it here rather than
        # in __init__ keeps a reused solver instance from carrying a stale
        # operator onto a different mesh.
        self._poisson = None
        self._free_cells = None
        self._face_velocity = None
        self._poisson_cache_key = f"cfd-poisson-{id(mesh)}-{mesh.shape}"
        residuals = CFDResidualHistory()
        converged = False
        max_iterations = max(1, int(controls.max_iterations))
        energy_residual = math.inf
        # Velocity scale used to make the residuals dimensionless. A forced case
        # has one from the start; a purely buoyant case has to grow its own, so
        # fall back to the evolving maximum speed.
        inlet_speed = max(
            [float(mapped.patch.velocity_m_s) for mapped in mesh.patch_cells
             if str(mapped.patch.kind).upper() in {"INLET", "FAN"}] or [0.0]
        )

        for iteration in range(max_iterations):
            if cancel_callback and cancel_callback():
                raise RuntimeError("Enclosure CFD analysis cancelled.")
            speed = np.sqrt(sum(component ** 2 for component in velocity))
            max_speed = float(np.max(speed[fluid])) if np.any(fluid) else 0.0
            advective_dt = 0.35 * min(mesh.spacing_m) / max(max_speed, 1e-3)
            diffusive_dt = 0.12 * min(mesh.spacing_m) ** 2 / max(viscosity, 1e-12)
            # Explicit CENTRED advection has a third, independent stability
            # bound: it oscillates above a cell Peclet number of 2, which
            # combined with the diffusive time-step limit gives
            # dt <= 2*nu/u^2 (Peclet = u*dx/nu <= 2 <=> dt_diff <= 2*nu/u^2
            # at dt_diff = dx^2/nu). Neither advective_dt (a CFL bound) nor
            # diffusive_dt (viscous-only) enforces it. relaxation scales the
            # velocity update actually applied, so it divides the bound
            # rather than multiplying dt. At P02 Rev E's 0.15 m/s this is
            # 1.4 ms against the previous effective (relaxation-scaled) step
            # of 5.3 ms, 3.8x over (CFD-7, docs/audit-2026-09-23.md).
            centred_advection_dt = (
                2.0 * viscosity / max(max_speed, 1e-3) ** 2 / max(relaxation, 1e-6)
            )
            dt = min(
                max(1e-6, float(controls.pseudo_time_step_s)),
                advective_dt, diffusive_dt, centred_advection_dt,
            )
            gradients = [self._gradient(component, mesh.spacing_m, fluid) for component in velocity]
            old_velocity = [component.copy() for component in velocity]
            body = [np.zeros(shape, dtype=float) for _ in range(3)]
            if controls.include_buoyancy:
                temperature_delta = temperature - float(settings.ambient_c)
                gravity = (controls.gravity_x_m_s2, controls.gravity_y_m_s2, controls.gravity_z_m_s2)
                # Boussinesq's beta is 1/T_ambient (ideal gas, absolute
                # temperature), not a fitted constant: the configured
                # thermal_expansion_per_k (default 0.00335 = 1/298.15, i.e.
                # 25 C) was used unchanged at any ambient, wrong by 6% at
                # P02 Rev E's 45 C ambient and worse further out (CFD-7).
                beta = self._boussinesq_beta(settings.ambient_c)
                for axis in range(3):
                    body[axis] = -float(gravity[axis]) * beta * temperature_delta
                    body[axis][~fluid] = 0.0
            for axis in range(3):
                advection = sum(velocity[d] * gradients[axis][d] for d in range(3))
                candidate = velocity[axis] + dt * (
                    -advection + viscosity * self._laplacian_velocity(
                        velocity[axis], mesh.spacing_m, fluid
                    ) + body[axis]
                )
                velocity[axis][fluid] = (
                    (1.0 - relaxation) * velocity[axis][fluid] +
                    relaxation * candidate[fluid]
                )
            self._apply_velocity_boundaries(mesh, velocity, pressure)
            continuity = self._pressure_projection(mesh, velocity, pressure, density, dt)
            momentum = max(float(np.max(np.abs(velocity[a] - old_velocity[a]))) for a in range(3))
            if iteration == 0 or (iteration + 1) % 5 == 0:
                new_temperature, energy_residual = self._solve_energy(
                    mesh, settings, velocity, temperature
                )
                temperature = (1.0 - relaxation) * temperature + relaxation * new_temperature
            # Normalise before comparing. The raw quantities carry three
            # different units -- continuity is a divergence in 1/s, momentum a
            # velocity change in m/s, energy a temperature change in K -- so
            # maxing them together and testing against one dimensionless
            # tolerance was meaningless. Continuity sits around 2.4 1/s on the
            # validation duct, so the test could never pass and `converged` was
            # structurally always False, which in turn made adapt_cfd_result
            # raise a HIGH numerics finding on every single run.
            # energy_residual carries over between energy solves, so normalise
            # into separate names -- rescaling it in place would compound the
            # division on every iteration that skips the energy step.
            reference_speed = max(max_speed, inlet_speed, 1e-9)
            reference_delta_t = max(
                float(np.max(np.abs(temperature - float(settings.ambient_c)))), 1.0
            )
            continuity_norm = continuity * min(mesh.spacing_m) / reference_speed
            momentum_norm = momentum / reference_speed
            energy_norm = float(energy_residual) / reference_delta_t
            residuals.continuity.append(continuity_norm)
            residuals.momentum.append(momentum_norm)
            residuals.energy.append(energy_norm)
            combined = max(continuity_norm, momentum_norm, energy_norm)
            if progress_callback and (iteration == 0 or (iteration + 1) % 5 == 0):
                progress_callback(iteration + 1, max_iterations, f"residual={combined:.3g}")
            if iteration >= 5 and combined <= max(1e-10, float(controls.tolerance)):
                converged = True
                break

        if (iteration + 1) % 5 != 0:
            temperature, energy_residual = self._solve_energy(mesh, settings, velocity, temperature)
        speed = np.sqrt(sum(component ** 2 for component in velocity))
        mass_error = self._flow_balance(mesh, velocity)
        energy_error = self._energy_balance(mesh, settings, temperature, velocity)
        solid_values = temperature[mesh.solid_mask]
        air_values = temperature[fluid]
        total_heat = float(np.sum(mesh.heat_sources_w))
        free_stream, free_stream_cells = self._board_free_stream(
            mesh, speed, getattr(settings.geometry, "board_orientation", "XY")
        )
        beta = self._boussinesq_beta(settings.ambient_c)
        boussinesq_max = (
            float(beta * np.max(np.abs(temperature[fluid] - float(settings.ambient_c))))
            if np.any(fluid) else 0.0
        )
        self._log(
            f"Solved {mesh.cell_count:,} cells in {iteration + 1} iterations; "
            f"Vmax={np.max(speed):.4g} m/s, "
            + ("mass balance not applicable (sealed enclosure, no through-flow)."
               if mass_error is None else f"mass error={mass_error:.3g}%.")
        )
        return EnclosureCFDResult(
            pressure_pa=pressure.reshape(-1).tolist(),
            velocity_u_m_s=velocity[0].reshape(-1).tolist(),
            velocity_v_m_s=velocity[1].reshape(-1).tolist(),
            velocity_w_m_s=velocity[2].reshape(-1).tolist(),
            air_temperature_c=np.where(fluid, temperature, np.nan).reshape(-1).tolist(),
            solid_temperature_c=np.where(mesh.solid_mask, temperature, np.nan).reshape(-1).tolist(),
            residuals=residuals,
            iterations=iteration + 1,
            converged=converged,
            mass_balance_error_pct=0.0 if mass_error is None else float(mass_error),
            mass_balance_applicable=mass_error is not None,
            energy_balance_error_pct=float(energy_error),
            boussinesq_beta_delta_t_max=boussinesq_max,
            maximum_velocity_m_s=float(np.max(speed[fluid])) if np.any(fluid) else 0.0,
            board_free_stream_velocity_m_s=free_stream,
            board_free_stream_cells=free_stream_cells,
            maximum_air_temperature_c=float(np.max(air_values)) if air_values.size else settings.ambient_c,
            maximum_solid_temperature_c=float(np.max(solid_values)) if solid_values.size else settings.ambient_c,
            total_heat_w=total_heat,
            compute_backend=self._last_compute.backend if self._last_compute else "CPU",
            compute_device=self._last_compute.device if self._last_compute else "CPU",
            compute_solve_seconds=self._last_compute.solve_seconds if self._last_compute else 0.0,
            compute_relative_residual=self._last_compute.relative_residual if self._last_compute else 0.0,
            compute_fallback_reason=self._last_compute.fallback_reason if self._last_compute else "",
        )
