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
except (ImportError, ValueError):
    from models import ComponentThermalResult, ThermalHotspot, ThermalResult
    from compute_backend import SparseComputeBackend


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

    def solve(self, mesh, ambient_c=25.0, progress_callback=None):
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
        result_temperatures = {
            node: float(temperatures[index]) for index, node in enumerate(mesh.nodes)
        }
        hotspot_node = max(result_temperatures, key=result_temperatures.get)
        hotspot_x, hotspot_y, hotspot_z = mesh.node_coords[hotspot_node]
        hotspot = ThermalHotspot(
            node_id=hotspot_node,
            x_mm=float(hotspot_x),
            y_mm=float(hotspot_y),
            z_mm=float(hotspot_z),
            temperature_c=result_temperatures[hotspot_node],
        )

        component_results = []
        for ref_des, nodes in mesh.component_nodes.items():
            component = mesh.component_models[ref_des]
            valid = [result_temperatures[node] for node in nodes if node in result_temperatures]
            if not valid:
                continue
            board_temperature = float(sum(valid) / len(valid))
            junction = board_temperature + component.power_w * max(0.0, component.theta_jb_c_per_w)
            component_results.append(ComponentThermalResult(
                ref_des=ref_des,
                board_temperature_c=board_temperature,
                junction_temperature_c=junction,
                power_w=component.power_w,
                max_junction_c=component.max_junction_c,
                margin_c=component.max_junction_c - junction,
                model_source=component.model_source,
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
                (result_temperatures[boundary.node_id] - float(ambient_c))
                for boundary in mesh.boundaries
            )
        denominator = max(abs(input_power), 1e-12)
        balance_error = abs(boundary_power - input_power) / denominator * 100.0
        if progress_callback:
            progress_callback(3, 3, "results")
        self._log(
            f"Solved {count:,} nodes; hotspot {hotspot.temperature_c:.2f} C, "
            f"energy error {balance_error:.3g}%; backend {compute.metadata.backend}, "
            f"residual {compute.metadata.relative_residual:.3g}."
        )
        return ThermalResult(
            temperatures_c=result_temperatures,
            temperature_vector_c=temperatures,
            hotspot=hotspot,
            component_results=component_results,
            total_input_power_w=input_power,
            total_boundary_power_w=float(boundary_power),
            energy_balance_error_pct=float(balance_error),
            convection_coefficient_w_m2k=float(mesh.convection_coefficient_w_m2k),
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
        )
