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
            for cell in mapped.cells:
                neighbor = self._inside_neighbor(cell, face, mesh.shape)
                if kind in {"INLET", "FAN"}:
                    for index, component in enumerate(velocity):
                        component[cell] = inward_sign * patch.velocity_m_s if index == axis else 0.0
                    pressure[cell] = pressure[neighbor]
                elif kind in {"OUTLET", "VENT"}:
                    for component in velocity:
                        component[cell] = component[neighbor]
                    pressure[cell] = patch.pressure_pa
                elif kind == "WALL":
                    for component in velocity:
                        component[cell] = 0.0

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
        for _ in range(max(1, int(iterations))):
            total = np.zeros_like(pressure)
            denominator = np.zeros_like(pressure)
            for axis, coefficient in enumerate(coefficients):
                plus = self._shift(pressure, axis, -1)
                minus = self._shift(pressure, axis, 1)
                plus_fluid = self._shift(fluid, axis, -1, False)
                minus_fluid = self._shift(fluid, axis, 1, False)
                total += coefficient * np.where(plus_fluid, plus, 0.0)
                total += coefficient * np.where(minus_fluid, minus, 0.0)
                denominator += coefficient * plus_fluid
                denominator += coefficient * minus_fluid
            candidate = (total - rhs) / np.maximum(denominator, 1e-30)
            pressure[fluid] = candidate[fluid]
            pressure[~fluid] = 0.0
            if not outlet_cells:
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
        return float(np.sqrt(np.mean(corrected[fluid] ** 2))) if np.any(fluid) else 0.0

    @staticmethod
    def _harmonic(a, b):
        return 2.0 * a * b / max(a + b, 1e-30)

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
            return 0.0
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
            f"Vmax={np.max(speed):.4g} m/s, mass error={mass_error:.3g}%."
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
            mass_balance_error_pct=float(mass_error),
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
