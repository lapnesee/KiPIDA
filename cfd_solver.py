"""Volumetric steady enclosure CFD with Boussinesq buoyancy and CHT energy."""

import math

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

    def __init__(self, debug=False, log_callback=None, compute_settings=None):
        self.debug = debug
        self.log_callback = log_callback
        self.compute_backend = SparseComputeBackend(compute_settings, log_callback)
        self._last_compute = None
        if np is None or scipy is None:
            raise ImportError("NumPy and SciPy are required for enclosure CFD.")

    def _log(self, message):
        if self.log_callback:
            self.log_callback(f"[CFD SOLVER] {message}")

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

    def _pressure_projection(self, mesh, velocity, pressure, density, dt, iterations):
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
    def _harmonic(a, b):
        return 2.0 * a * b / max(a + b, 1e-30)

    @staticmethod
    def _film_coefficient(delta_t_k, length_m, speed_m_s, ambient_c, emissivity):
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
        """
        try:
            from .import surface_convection
        except (ImportError, ValueError):
            import surface_convection

        natural = surface_convection.natural_convection_h(
            delta_t_k, length_m, facing="up", ambient_c=ambient_c,
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
        matrix = scipy.sparse.lil_matrix((count, count), dtype=float)
        rhs = mesh.heat_sources_w.reshape(-1).astype(float).copy()
        inlet_values = {}
        patch_lookup = {}
        for mapped in mesh.patch_cells:
            face = str(mapped.patch.face).upper()
            patch_lookup.update({(cell, face): mapped.patch for cell in mapped.cells})
            if str(mapped.patch.kind).upper() in {"INLET", "FAN"}:
                inlet_values.update({cell: float(mapped.patch.temperature_c) for cell in mapped.cells})
        wall_h = max(0.0, float(settings.geometry.wall_heat_transfer_w_m2k))
        ambient = float(settings.ambient_c)
        # Length scale for the plate correlations at a solid-air face. The mean
        # cell size is used because the mesh is all the geometry the solver has
        # here -- it does not carry each obstacle's own A/P. Since h varies only
        # as L^(-1/4), a length wrong by a factor of two moves the coefficient
        # by 19%, which is inside the correlation's own spread; a missing film
        # was wrong by a factor of forty.
        film_length_m = float(sum(mesh.spacing_m)) / 3.0
        emissivity = max(0.0, min(1.0, float(
            getattr(settings.geometry, "emissivity", 0.0) or 0.0
        )))
        directions = ((-1, 0, 0), (1, 0, 0), (0, -1, 0),
                      (0, 1, 0), (0, 0, -1), (0, 0, 1))
        for i in range(shape[0]):
            for j in range(shape[1]):
                for k in range(shape[2]):
                    cell = (i, j, k)
                    row = np.ravel_multi_index(cell, shape)
                    if cell in inlet_values:
                        matrix[row, row] = 1.0
                        rhs[row] = inlet_values[cell]
                        continue
                    diagonal = 0.0
                    for direction in directions:
                        ni, nj, nk = i + direction[0], j + direction[1], k + direction[2]
                        axis = 0 if direction[0] else (1 if direction[1] else 2)
                        area = areas[axis]
                        if not (0 <= ni < shape[0] and 0 <= nj < shape[1] and 0 <= nk < shape[2]):
                            face = {
                                (-1, 0, 0): "XMIN", (1, 0, 0): "XMAX",
                                (0, -1, 0): "YMIN", (0, 1, 0): "YMAX",
                                (0, 0, -1): "ZMIN", (0, 0, 1): "ZMAX",
                            }[direction]
                            patch = patch_lookup.get((cell, face))
                            kind = str(patch.kind).upper() if patch else "WALL"
                            if kind in {"OUTLET", "VENT"}:
                                _, inward_sign = self._normal(face)
                                outward = -inward_sign * velocity[axis][cell]
                                capacity = (props.density_kg_m3 * props.heat_capacity_j_kgk *
                                            area * abs(outward))
                                diagonal += capacity
                                if outward < 0.0:
                                    rhs[row] += capacity * float(patch.temperature_c)
                            else:
                                conductance = wall_h * area
                                diagonal += conductance
                                rhs[row] += conductance * ambient
                            continue
                        neighbor = (ni, nj, nk)
                        column = np.ravel_multi_index(neighbor, shape)
                        diffusion = self._harmonic(
                            float(conductivity[cell]), float(conductivity[neighbor])
                        ) * area / spacings[axis]
                        if fluid[cell] != fluid[neighbor]:
                            # A solid-air face. The path is: conduct from the
                            # solid cell centre to its surface, then cross the
                            # boundary layer.
                            #
                            #   R = (spacing/2)/(k_solid * A) + 1/(h * A)
                            #
                            # The air-side cell-to-cell conduction is dropped
                            # rather than kept in series, because it is not a
                            # model of the interface at all -- it is what the
                            # cell-centred scheme produces in the absence of
                            # one, and adding the film in series with it only
                            # made the solid hotter still (162.7 C -> 216.9 C
                            # on the reproduction case), since series can only
                            # lower a conductance.
                            #
                            # h is evaluated on the previous iterate, the same
                            # lagging the electro-thermal loop already uses.
                            solid = cell if not fluid[cell] else neighbor
                            air = neighbor if solid is cell else cell
                            delta_t = abs(float(previous[cell]) - float(previous[neighbor]))
                            speed = float(np.sqrt(sum(
                                component[air] ** 2 for component in velocity
                            )))
                            film = self._film_coefficient(
                                delta_t, film_length_m, speed, ambient, emissivity,
                            )
                            k_solid = max(float(conductivity[solid]), 1e-9)
                            resistance = (
                                0.5 * spacings[axis] / (k_solid * area)
                                + 1.0 / max(film * area, 1e-30)
                            )
                            diffusion = 1.0 / resistance
                        outward_sign = direction[axis]
                        face_velocity = 0.5 * (
                            velocity[axis][cell] + velocity[axis][neighbor]
                        ) if fluid[cell] and fluid[neighbor] else 0.0
                        flux = props.density_kg_m3 * props.heat_capacity_j_kgk * area * (
                            outward_sign * face_velocity
                        )
                        diagonal += diffusion + max(flux, 0.0)
                        matrix[row, column] -= diffusion + max(-flux, 0.0)
                    matrix[row, row] += max(diagonal, 1e-20)
        csr = matrix.tocsr()
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

    @staticmethod
    def _board_free_stream(mesh, speed):
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

        So this samples fluid cells at a stand-off of two cells from any solid,
        restricted to the slab the board occupies. It returns 0.0 rather than a
        fabricated fallback when the mesh is too coarse to offer such cells,
        which is the caller's signal to stay with natural convection.
        """
        names = getattr(mesh, "obstacle_names", None)
        if names is None:
            return 0.0, 0
        board = np.asarray(names) == "PCB"
        if not np.any(board):
            return 0.0, 0
        # Dilate the solid mask by two cells; anything still fluid afterwards is
        # at least two cells clear of every obstacle.
        near_solid = np.asarray(mesh.solid_mask, dtype=bool).copy()
        for _ in range(2):
            grown = near_solid.copy()
            for axis in range(3):
                grown |= EnclosureCFDSolver._shift(near_solid, axis, 1, False)
                grown |= EnclosureCFDSolver._shift(near_solid, axis, -1, False)
            near_solid = grown
        # Restrict to the slab spanned by the board, so a fan blowing through an
        # empty corner of a large enclosure cannot masquerade as flow over it.
        extent = np.argwhere(board)
        slab = np.zeros(mesh.shape, dtype=bool)
        lower = extent.min(axis=0)
        upper = extent.max(axis=0)
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
            dt = min(max(1e-6, float(controls.pseudo_time_step_s)), advective_dt, diffusive_dt)
            gradients = [self._gradient(component, mesh.spacing_m, fluid) for component in velocity]
            old_velocity = [component.copy() for component in velocity]
            body = [np.zeros(shape, dtype=float) for _ in range(3)]
            if controls.include_buoyancy:
                temperature_delta = temperature - float(settings.ambient_c)
                gravity = (controls.gravity_x_m_s2, controls.gravity_y_m_s2, controls.gravity_z_m_s2)
                for axis in range(3):
                    body[axis] = (-float(gravity[axis]) * props.thermal_expansion_per_k *
                                  temperature_delta)
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
            continuity = self._pressure_projection(
                mesh, velocity, pressure, density, dt, controls.pressure_iterations
            )
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
        free_stream, free_stream_cells = self._board_free_stream(mesh, speed)
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
