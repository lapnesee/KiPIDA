"""Sparse steady-state thermal solver with convective boundaries."""

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
    from .models import ComponentThermalResult, ThermalHotspot, ThermalResult
    from .compute_backend import SparseComputeBackend
    from .thermal_mesh import component_reference_nodes
except (ImportError, ValueError):
    from models import ComponentThermalResult, ThermalHotspot, ThermalResult
    from compute_backend import SparseComputeBackend
    from thermal_mesh import component_reference_nodes


class ThermalSolver:
    def __init__(self, debug=False, log_callback=None, compute_settings=None):
        self.debug = debug
        self.log_callback = log_callback
        self.compute_settings = compute_settings
        self.compute_backend = SparseComputeBackend(
            settings=self.compute_settings,
            log_callback=self.log_callback,
        )
        self._matrix_cache = {}
        if np is None or scipy is None:
            raise ImportError("NumPy and SciPy are required for 3D thermal analysis.")

    def _log(self, message):
        if self.log_callback:
            self.log_callback(f"[THERMAL SOLVER] {message}")

    # Surface refinement stops when every face's coefficient moves less than
    # this fraction between passes.  2% is far below the uncertainty of the
    # correlations themselves, so tightening it would buy precision the
    # physics cannot support.
    SURFACE_H_TOLERANCE = 0.02
    SURFACE_REFINEMENT_PASSES = 3

    def solve_with_surface_refinement(self, mesh, settings, progress_callback=None,
                                      materialize_temperatures=True):
        """Solve, then correct the surface coefficients against the solution.

        Natural convection and radiation both depend on how hot the surface
        ends up, which is not known when the mesh is built.  The mesh starts
        from a nominal rise; this re-evaluates each face's coefficient from the
        temperature actually obtained, rescales that face's boundaries, and
        re-solves, until the coefficients stop moving.

        Falls back to a single plain solve when the mesh carries no surface
        bookkeeping -- a mesh built by an older path, or one whose airflow mode
        is CUSTOM, where the user has fixed h and refining it would override
        them.
        """
        result = self.solve(
            mesh, ambient_c=settings.ambient_c,
            progress_callback=progress_callback,
            materialize_temperatures=materialize_temperatures,
        )
        base = dict(getattr(mesh, "surface_h_w_m2k", {}) or {})
        if not base or str(settings.airflow.mode or "").upper() == "CUSTOM":
            return result

        result.surface_refined = True
        for iteration in range(1, self.SURFACE_REFINEMENT_PASSES + 1):
            updated, worst_change = self._refine_surface_exchange(mesh, settings, result)
            if not updated:
                break
            self._log(
                f"Surface exchange pass {iteration}: coefficients moved "
                f"{worst_change * 100.0:.1f}% -- re-solving."
            )
            result = self.solve(
                mesh, ambient_c=settings.ambient_c,
                progress_callback=None,
                materialize_temperatures=materialize_temperatures,
            )
            result.surface_refined = True
            if worst_change <= self.SURFACE_H_TOLERANCE:
                break
        applied = ", ".join(
            f"{kind} {value:.1f}" for kind, value in sorted(mesh.surface_h_w_m2k.items())
        )
        self._log(
            f"Surface exchange settled at W/m2K: {applied} "
            f"({getattr(mesh, 'convection_basis', 'unspecified basis')})."
        )
        # TH-8: _refine_surface_exchange always evaluates in-range (even when
        # it finds nothing left to move, in which case the loop above never
        # re-solves after it), so this must be copied on unconditionally
        # rather than only on the result built by the next solve().
        result.surface_correlation_in_range = dict(
            getattr(mesh, "surface_correlation_in_range", {}) or {}
        )
        return result

    def _refine_surface_exchange(self, mesh, settings, result):
        """Recompute each face's coefficient from the solved temperatures.

        Returns ``(updated, worst_relative_change)``.  ``updated`` is False
        when every face already agrees with its solution to within tolerance,
        which is the signal to stop iterating.
        """
        try:
            from .thermal_mesh import ThermalMesher
            from . import surface_convection
        except (ImportError, ValueError):
            from thermal_mesh import ThermalMesher
            import surface_convection

        temperatures = getattr(result, "temperature_vector_c", None)
        if temperatures is None:
            temperatures = np.fromiter(
                (result.temperatures_c.get(node, settings.ambient_c) for node in mesh.nodes),
                dtype=float, count=len(mesh.nodes),
            )
        temperatures = np.asarray(temperatures, dtype=float).reshape(-1)
        node_index = {node: index for index, node in enumerate(mesh.nodes)}
        boundary_nodes, _ = mesh.boundaries.arrays()
        length_m = float(getattr(mesh, "characteristic_length_m", 0.0) or 0.0)
        if length_m <= 0.0:
            return False, 0.0

        ambient = float(settings.ambient_c)
        worst_change = 0.0
        updated = False
        # TH-8: natural_convection_in_range/forced_convection_in_range existed
        # but were never called anywhere, so a board whose Rayleigh number
        # sits outside the laminar horizontal-plate correlations' stated
        # range (P02 Rev E: Ra ~ 8.0e3, below the 1e4 lower bound) got the
        # correlation's number with no caveat that it was extrapolated.
        # Evaluated here, at the operating point the solve actually settles
        # on, rather than the mesh's nominal ambient + 30 K guess.
        in_range = {}
        for kind, current_h in list(mesh.surface_h_w_m2k.items()):
            mask = mesh.boundaries.kind_mask(kind)
            if not mask.any():
                continue
            indices = np.fromiter(
                (node_index.get(int(node), -1) for node in boundary_nodes[mask]),
                dtype=np.int64,
            )
            indices = indices[indices >= 0]
            if not indices.size:
                continue
            face_mean_c = float(np.mean(temperatures[indices]))
            delta_t = max(0.0, face_mean_c - ambient)
            film_c = ambient + delta_t / 2.0
            rayleigh = surface_convection.rayleigh_number(delta_t, length_m, film_c)
            kind_in_range = surface_convection.natural_convection_in_range(rayleigh)
            velocity = getattr(mesh, "air_velocity_m_s", None)
            if str(settings.airflow.mode or "").upper() == "FORCED" or velocity:
                reynolds = surface_convection.reynolds_number(
                    velocity if velocity else settings.airflow.velocity_m_s, length_m, film_c,
                )
                kind_in_range = kind_in_range and surface_convection.forced_convection_in_range(reynolds)
            in_range[kind] = kind_in_range
            convective = ThermalMesher.surface_coefficient(
                settings, kind, delta_t, length_m,
                air_velocity_m_s=getattr(mesh, "air_velocity_m_s", None),
            )
            radiative = 0.0
            if settings.include_radiation:
                radiative = surface_convection.radiation_h(
                    settings.emissivity, face_mean_c, ambient,
                )
            new_h = convective + radiative
            if new_h <= 0.0 or current_h <= 0.0:
                continue
            change = abs(new_h - current_h) / current_h
            worst_change = max(worst_change, change)
            if change <= self.SURFACE_H_TOLERANCE:
                continue
            mesh.boundaries.scale_kind(kind, new_h / current_h)
            mesh.surface_h_w_m2k[kind] = new_h
            updated = True

        mesh.surface_correlation_in_range = in_range
        if updated:
            # Boundary conductances are baked into the assembled matrix, so a
            # rescale invalidates it.  Dropping the cache is what makes the
            # next solve see the new coefficients.
            self._matrix_cache = {}
            mesh.convection_coefficient_w_m2k = mesh.surface_h_w_m2k.get(
                "top", mesh.convection_coefficient_w_m2k,
            )
        return updated, worst_change

    def solve(self, mesh, ambient_c=25.0, progress_callback=None,
              materialize_temperatures=True):
        if not mesh.nodes:
            raise ValueError("The thermal mesh is empty.")
        if not mesh.boundaries:
            raise ValueError("At least one exposed convective surface is required.")

        count = len(mesh.nodes)
        identity_nodes = (
            count > 0 and mesh.nodes[0] == 0 and mesh.nodes[-1] == count - 1
        )
        node_to_index = None if identity_nodes else {
            node: index for index, node in enumerate(mesh.nodes)
        }
        translate = (lambda node: int(node)) if identity_nodes else node_to_index.get

        cache_key = id(mesh)
        cached = self._matrix_cache.get(cache_key)
        if cached is not None and cached[0] is mesh and cached[3] == float(ambient_c):
            matrix = cached[1]
            rhs = cached[2].copy()
            self._log(
                f"Reusing cached {matrix.nnz:,}-entry thermal CSR matrix; "
                "CUDA keeps its CSR workspace resident in VRAM."
            )
        else:
            rhs = np.zeros(count, dtype=float)
            self._log(
                f"Assembling sparse thermal matrix from {len(mesh.branches):,} branches and "
                f"{len(mesh.boundaries):,} boundaries."
            )
            # Fine meshes store thermal connections in packed primitive arrays.
            # This avoids recreating millions of ``ThermalBranch`` objects and
            # then immediately converting them back to NumPy for COO/CSR.
            if hasattr(mesh.branches, "arrays"):
                branch_a, branch_b, conductance = mesh.branches.arrays()
                branch_a = branch_a.astype(np.int64, copy=False)
                branch_b = branch_b.astype(np.int64, copy=False)
            else:
                branch_a = np.fromiter(
                    (translate(branch.node_a) for branch in mesh.branches),
                    dtype=np.int64, count=len(mesh.branches),
                )
                branch_b = np.fromiter(
                    (translate(branch.node_b) for branch in mesh.branches),
                    dtype=np.int64, count=len(mesh.branches),
                )
                conductance = np.fromiter(
                    (float(branch.conductance_w_k) for branch in mesh.branches),
                    dtype=np.float64, count=len(mesh.branches),
                )
            if not identity_nodes:
                branch_a = np.fromiter((translate(node) for node in branch_a), dtype=np.int64)
                branch_b = np.fromiter((translate(node) for node in branch_b), dtype=np.int64)
            valid = (branch_a >= 0) & (branch_b >= 0) & (conductance > 0)
            branch_a, branch_b, conductance = branch_a[valid], branch_b[valid], conductance[valid]

            if hasattr(mesh.boundaries, "arrays"):
                boundary_index, boundary_g = mesh.boundaries.arrays()
                boundary_index = boundary_index.astype(np.int64, copy=False)
            else:
                boundary_index = np.fromiter(
                    (translate(boundary.node_id) for boundary in mesh.boundaries),
                    dtype=np.int64, count=len(mesh.boundaries),
                )
                boundary_g = np.fromiter(
                    (float(boundary.conductance_w_k) for boundary in mesh.boundaries),
                    dtype=np.float64, count=len(mesh.boundaries),
                )
            if not identity_nodes:
                boundary_index = np.fromiter(
                    (translate(node) for node in boundary_index), dtype=np.int64,
                )
            boundary_valid = (boundary_index >= 0) & (boundary_g > 0)
            boundary_index, boundary_g = boundary_index[boundary_valid], boundary_g[boundary_valid]
            np.add.at(rhs, boundary_index, boundary_g * float(ambient_c))

            rows = np.concatenate((branch_a, branch_b, branch_a, branch_b, boundary_index))
            cols = np.concatenate((branch_a, branch_b, branch_b, branch_a, boundary_index))
            values = np.concatenate((conductance, conductance, -conductance, -conductance, boundary_g))
            # Compact duplicates on the CPU once.  This is substantially less
            # data than transferring COO row/column vectors then converting on
            # the GPU, while retaining the exact same finite-volume matrix.
            matrix = scipy.sparse.coo_matrix((values, (rows, cols)), shape=(count, count)).tocsr()
            matrix.sum_duplicates()
            matrix.sort_indices()
            self._matrix_cache = {cache_key: (mesh, matrix, rhs.copy(), float(ambient_c))}
            del rows, cols, values, branch_a, branch_b, conductance, boundary_index, boundary_g

        heat_vector = getattr(mesh, "heat_vector_w", None)
        if heat_vector is not None:
            heat_vector = np.asarray(heat_vector, dtype=float).reshape(-1)
            if heat_vector.size != count:
                raise ValueError("Thermal heat vector does not match thermal mesh nodes.")
            rhs += heat_vector
            input_power = float(np.sum(heat_vector))
        else:
            for node, power in mesh.heat_sources_w.items():
                index = translate(node)
                if index is not None:
                    rhs[index] += float(power)
            input_power = float(sum(mesh.heat_sources_w.values()))

        if progress_callback:
            progress_callback(1, 3, "matrix")
        compute = self.compute_backend.solve(
            matrix, rhs, system_kind="SPD",
            cache_key=("thermal", id(mesh)), matrix_values_static=True,
        )
        temperatures = compute.values
        if progress_callback:
            progress_callback(2, 3, "solve")

        if np.any(~np.isfinite(temperatures)):
            raise ValueError("Thermal solution contains non-finite temperatures.")
        hotspot_index = int(np.argmax(temperatures))
        hotspot_node = mesh.nodes[hotspot_index]
        hotspot_x, hotspot_y, hotspot_z = mesh.node_coords[hotspot_node]
        hotspot = ThermalHotspot(
            node_id=hotspot_node,
            x_mm=float(hotspot_x),
            y_mm=float(hotspot_y),
            z_mm=float(hotspot_z),
            temperature_c=float(temperatures[hotspot_index]),
        )

        component_results = []
        for ref_des in mesh.component_nodes:
            component = mesh.component_models[ref_des]
            nodes, board_reference = component_reference_nodes(mesh, ref_des)
            valid_indices = np.fromiter(
                (translate(node) for node in nodes if translate(node) is not None),
                dtype=np.int64,
            )
            if valid_indices.size == 0:
                continue
            # TH-5: the mean under-part temperature can sit well below the
            # part's own hotspot -- P02 Rev E read Q10 Tj 110.64 C under a
            # 114.71 C board hotspot beneath Q10 itself. thetaJB already maps
            # the part's total dissipation to its hottest junction, so the
            # board-side term it adds to should be the hottest cell under the
            # part, not an average that a cooler pad drags down. A psiJB or
            # thetaJB is referred to the board 1 mm from the package instead
            # (component_reference_nodes): under the part, P02 Rev E's U15
            # added its psiJB to the peak of its own pads, which rose at every
            # grid refinement (a model of the board around it: 92.8, 94.7 and
            # 98.7 C at 0.4, 0.2 and 0.1 mm; 87.5, 87.8 and 89.1 C 1 mm out).
            board_temperature = float(np.max(temperatures[valid_indices]))
            junction = board_temperature + component.power_w * max(0.0, component.theta_jb_c_per_w)
            component_results.append(ComponentThermalResult(
                ref_des=ref_des,
                board_temperature_c=board_temperature,
                junction_temperature_c=junction,
                power_w=component.power_w,
                max_junction_c=component.max_junction_c,
                margin_c=component.max_junction_c - junction,
                model_source=component.model_source,
                theta_jb_c_per_w=component.theta_jb_c_per_w,
                thermal_source=getattr(component, "thermal_source", "estimate"),
                thermal_condition=getattr(component, "thermal_condition", ""),
                board_reference=board_reference,
                rated_current_a=float(getattr(component, "rated_current_a", 0.0) or 0.0),
                current_derating=list(getattr(component, "current_derating", None) or []),
                current_derating_source=str(getattr(component, "current_derating_source", "") or ""),
            ))
        component_results.sort(key=lambda item: item.junction_temperature_c, reverse=True)

        if hasattr(mesh.boundaries, "arrays"):
            boundary_ids, boundary_g = mesh.boundaries.arrays()
            if not identity_nodes:
                boundary_ids = np.fromiter(
                    (translate(node) for node in boundary_ids), dtype=np.int64,
                )
            boundary_power = float(np.sum(
                boundary_g * (temperatures[boundary_ids] - float(ambient_c))
            ))
        else:
            boundary_power = sum(
                boundary.conductance_w_k *
                (temperatures[translate(boundary.node_id)] - float(ambient_c))
                for boundary in mesh.boundaries
            )
        denominator = max(abs(input_power), 1e-12)
        balance_error = abs(boundary_power - input_power) / denominator * 100.0
        # TH-9: the check above only proves the linear solve is internally
        # consistent -- everything deposited eventually leaves through a
        # boundary -- not that everything the model asked for was deposited
        # in the first place. A component with no placement, or whose
        # window and nearest-node fallback both came up empty, or a
        # copper-loss point whose nearest cell was not found (an unplaced or
        # disabled component, or a copper branch without a cell,
        # electrothermal.py:241) is invisible to it: it just deposits less
        # heat, and a smaller total balances against itself just as cleanly.
        intended_power = float(getattr(mesh, "intended_input_power_w", 0.0) or 0.0)
        deposit_shortfall = max(0.0, intended_power - input_power)
        if progress_callback:
            progress_callback(3, 3, "results")
        shortfall_note = (
            f", {deposit_shortfall:.4g} W of {intended_power:.4g} W intended never reached a cell"
            if deposit_shortfall > 1e-9 else ""
        )
        self._log(
            f"Solved {count:,} nodes; hotspot {hotspot.temperature_c:.2f} C, "
            f"energy error {balance_error:.3g}%{shortfall_note}; backend {compute.metadata.backend}, "
            f"residual {compute.metadata.relative_residual:.3g}."
        )
        result_temperatures = ({
            node: float(temperatures[index]) for index, node in enumerate(mesh.nodes)
        } if materialize_temperatures else {})
        return ThermalResult(
            temperatures_c=result_temperatures,
            temperature_vector_c=temperatures,
            hotspot=hotspot,
            component_results=component_results,
            total_input_power_w=input_power,
            total_boundary_power_w=float(boundary_power),
            energy_balance_error_pct=float(balance_error),
            intended_input_power_w=intended_power,
            undeposited_power_w=float(deposit_shortfall),
            convection_coefficient_w_m2k=float(mesh.convection_coefficient_w_m2k),
            surface_h_w_m2k=dict(getattr(mesh, "surface_h_w_m2k", {}) or {}),
            surface_correlation_in_range=dict(
                getattr(mesh, "surface_correlation_in_range", {}) or {}
            ),
            convection_basis=str(getattr(mesh, "convection_basis", "") or ""),
            characteristic_length_m=float(
                getattr(mesh, "characteristic_length_m", 0.0) or 0.0
            ),
            compute_backend=compute.metadata.backend,
            compute_device=compute.metadata.device,
            compute_solve_seconds=compute.metadata.solve_seconds,
            compute_transfer_seconds=compute.metadata.transfer_seconds,
            compute_relative_residual=compute.metadata.relative_residual,
            compute_iterations=compute.metadata.iterations,
            compute_cpu_threads=compute.metadata.cpu_threads,
            compute_fallback_reason=compute.metadata.fallback_reason,
            compute_matrix_assembly=compute.metadata.matrix_assembly,
            compute_warm_start_used=compute.metadata.warm_start_used,
            requested_grid_size_mm=float(getattr(mesh, "requested_grid_size_mm", 0.0) or 0.0),
            effective_grid_size_mm=float(getattr(mesh, "grid_size_mm", 0.0) or 0.0),
            adaptive_grid=bool(getattr(mesh, "adaptive_grid", False)),
            mesh_node_count=len(getattr(mesh, "nodes", ()) or ()),
        )
