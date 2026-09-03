
import sys

try:
    from .models import DCSolveResult
    from .compute_backend import SparseComputeBackend
except (ImportError, ValueError):
    from models import DCSolveResult
    from compute_backend import SparseComputeBackend

# Try imports, handle if missing (though they should be in KiCad 9)
try:
    import numpy as np
    import scipy
    import scipy.sparse
    import scipy.sparse.linalg
except ImportError:
    np = None
    scipy = None

try:
    import pypardiso
except ImportError:
    pypardiso = None

class Solver:
    def __init__(self, debug=False, log_callback=None, compute_settings=None):
        self.debug = debug
        self.log_callback = log_callback
        self.compute_backend = SparseComputeBackend(compute_settings, log_callback)
        self.last_compute = None
        self._topology_cache = {}
        self._node_index_cache = {}
        self._branch_array_cache = {}
        self._last_solve_state = None
        self.last_diagnostics = {}
        if np is None or scipy is None:
            raise ImportError("NumPy and SciPy are required for Solver backend.")

    def _log(self, msg):
        if self.log_callback:
            self.log_callback(f"[SOLVER] {msg}")

    def _node_index(self, mesh):
        """Cache immutable node-index data for repeated coupled solves."""
        nodes_object = getattr(mesh, 'nodes', [])
        key = (id(mesh), id(nodes_object), len(nodes_object))
        cached = self._node_index_cache.get(key)
        if cached is None:
            nodes = list(nodes_object)
            cached = (nodes, {node_id: index for index, node_id in enumerate(nodes)})
            self._node_index_cache[key] = cached
        return cached

    def _branch_arrays(self, mesh, id_to_idx):
        """Return fixed branch topology/resistance arrays for a mesh."""
        branches = getattr(mesh, 'branches', [])
        key = (id(mesh), id(branches), len(branches), len(id_to_idx))
        cached = self._branch_array_cache.get(key)
        if cached is None:
            branch_a = np.fromiter(
                (id_to_idx.get(branch.node_a, -1) for branch in branches),
                dtype=np.int64, count=len(branches),
            )
            branch_b = np.fromiter(
                (id_to_idx.get(branch.node_b, -1) for branch in branches),
                dtype=np.int64, count=len(branches),
            )
            base_resistance = np.fromiter(
                (float(branch.resistance_ohm) for branch in branches),
                dtype=float, count=len(branches),
            )
            valid = (branch_a >= 0) & (branch_b >= 0)
            valid_a = branch_a[valid]
            valid_b = branch_b[valid]
            rows = np.concatenate((valid_a, valid_b, valid_a, valid_b))
            cols = np.concatenate((valid_a, valid_b, valid_b, valid_a))
            cached = (branch_a, branch_b, base_resistance, valid, rows, cols)
            self._branch_array_cache[key] = cached
        return cached

    def solve(
        self, mesh, sources, loads, branch_resistance_scales=None,
        initial_voltages=None, use_precond=False,
    ):
        """
        Solves the DC circuit Mesh: [G][V] = [I]

        Args:
            mesh: Mesh object with .nodes (list) and .edges (list of u,v,g)
            sources: list of dicts { 'node_id': int, 'voltage': float } (Dirichlet)
            use_precond: If True, use an ILU(0) preconditioned CG solver instead of
                the default direct/iterative backend.  Disabled by default to preserve
                existing behaviour.
            loads: list of dicts { 'node_id': int, 'current': float } (Neumann/Source)
            
        Returns:
            dict: { node_id: voltage_float }
        """
        if not mesh.nodes:
            self._log("Mesh has no nodes. Returning empty result.")
            return {}
            
        # 1. Map Node IDs to Matrix Indices (0..N-1)
        nodes, id_to_idx = self._node_index(mesh)
        N = len(nodes)
        branch_arrays = None
        
        # 2. Build Matrix G
        if branch_resistance_scales is not None and getattr(mesh, 'branches', None):
            scales = np.asarray(branch_resistance_scales, dtype=float).reshape(-1)
            if len(scales) != len(mesh.branches):
                raise ValueError("One resistance scale is required for every mesh branch.")
            # Resistance changes during electro-thermal coupling, but branch
            # endpoints never do.  COO assembly retains the same Laplacian as
            # the legacy LIL inserts while removing millions of Python sparse
            # writes from the repeated DC path.
            branch_arrays = self._branch_arrays(mesh, id_to_idx)
            branch_a, branch_b, base_resistance, valid, rows, cols = branch_arrays
            conductance = 1.0 / np.maximum(
                base_resistance * np.maximum(scales, 1.0e-9), 1.0e-15
            )
            conductance = conductance[valid]
            values = np.concatenate((conductance, conductance, -conductance, -conductance))
            G = scipy.sparse.coo_matrix((values, (rows, cols)), shape=(N, N))
        elif hasattr(mesh, 'G_coo_data') and len(mesh.G_coo_data) > 0:
            if self.debug:
                self._log(f"Using pre-computed sparse matrix ({len(mesh.G_coo_data)} entries).")
            # We have raw node IDs in G_coo_row/col, need to map to indices 0..N-1
            # Optimally, Mesher should produce 0..N indices, but it deals with arbitrary node IDs.
            # If node IDs are 0..N-1 sequential (which they are in Mesher implementation), we can skip mapping?
            # Let's verify: Mesher.generate_mesh starts node_counter=0 and increments.
            # So if mesh.nodes is sorted 0..N-1, id_to_idx is identity.
            
            # Check if mapping is needed (heuristic: if max node id < N, probably okay, but safer to map)
            # Vectorized mapping using numpy is fast.
            
            # Convert to numpy arrays if not already
            row_ids = np.array(mesh.G_coo_row)
            col_ids = np.array(mesh.G_coo_col)
            data = np.array(mesh.G_coo_data)
            
            # Check if simple identity mapping checks out
            # Mesher logic guarantees 0...N-1 sequentially.
            # But let's be robust:
            # We can use a fast lookup array if max(nodes) isn't huge.
            G = scipy.sparse.coo_matrix((data, (row_ids, col_ids)), shape=(N, N))
            
        else:
            if self.debug:
                self._log("Using legacy edge iteration for matrix build.")
            # Legacy Path (Slow)
            G = scipy.sparse.lil_matrix((N, N))
            for u_id, v_id, g in mesh.edges:
                if u_id not in id_to_idx or v_id not in id_to_idx:
                    continue
                
                u = id_to_idx[u_id]
                v = id_to_idx[v_id]
                
                G[u, u] += g
                G[v, v] += g
                G[u, v] -= g
                G[v, u] -= g
            
        # Determine electrical connectivity before applying Dirichlet rows.
        # Replacing a source row with an identity row changes the matrix graph
        # and can create artificial one-node islands in diagnostics.  It also
        # leaves genuinely floating copper islands in a singular system.
        valid_node_mask = np.ones(N, dtype=bool)
        floating_representatives = []
        excluded_load_nodes = set()
        topology_key = (
            id(mesh), N,
            tuple(sorted(str(source.get('node_id')) for source in sources)),
            tuple(sorted(str(load.get('node_id')) for load in loads if abs(float(load.get('current', 0.0))) > 0.0)),
        )
        cached_topology = self._topology_cache.get(topology_key)
        if cached_topology is not None:
            valid_node_mask, floating_representatives, excluded_load_nodes = (
                cached_topology[0].copy(), list(cached_topology[1]), set(cached_topology[2])
            )
        else:
            try:
                from scipy.sparse.csgraph import connected_components

                connectivity_matrix = G.tocsr()
                n_components, component_labels = connected_components(
                    csgraph=connectivity_matrix,
                    directed=False,
                    return_labels=True,
                )
                source_components = set()
                for source in sources:
                    idx = id_to_idx.get(source.get('node_id'))
                    if idx is not None:
                        source_components.add(int(component_labels[idx]))

                valid_node_mask = np.array(
                    [int(label) in source_components for label in component_labels],
                    dtype=bool,
                )
                if n_components > 1:
                    self._log(f"Detected {n_components} copper islands before source constraints.")

                for component in range(n_components):
                    if component in source_components:
                        continue
                    component_indices = np.flatnonzero(component_labels == component)
                    if len(component_indices) == 0:
                        continue
                    floating_representatives.append(int(component_indices[0]))
                    load_nodes = {
                        load.get('node_id') for load in loads
                        if load.get('node_id') in id_to_idx
                        and int(component_labels[id_to_idx[load.get('node_id')]]) == component
                        and abs(float(load.get('current', 0.0))) > 0.0
                    }
                    if load_nodes:
                        excluded_load_nodes.update(load_nodes)
                        self._log(
                            f"ERROR: Island #{component} ({len(component_indices)} nodes) has "
                            f"{len(load_nodes)} load node(s) but no voltage source; "
                            "its loads and voltages are excluded."
                        )
                    else:
                        self._log(
                            f"Ignoring floating island #{component} ({len(component_indices)} nodes): "
                            "no voltage source or load."
                        )
                self._topology_cache[topology_key] = (
                    valid_node_mask.copy(), tuple(floating_representatives),
                    tuple(excluded_load_nodes),
                )
            except Exception as e:
                self._log(f"Connectivity diagnostic failed: {e}")

        # Initialize Vector I
        I = np.zeros(N)

        # 4. Apply Loads (Current Sources)
        for load in loads:
            nid = load.get('node_id')
            current = load.get('current', 0.0)
            if nid in id_to_idx and valid_node_mask[id_to_idx[nid]]:
                idx = id_to_idx[nid]
                I[idx] -= current
                
        # 5. Collect voltage sources (Dirichlet BCs).  They are applied after
        # connectivity analysis so the original copper graph remains visible.
        constrained_values = {}
        for source in sources:
            nid = source.get('node_id')
            voltage = source.get('voltage', 0.0)
            if nid in id_to_idx:
                idx = id_to_idx[nid]
                value = float(voltage)
                previous = constrained_values.get(idx)
                if previous is not None and not np.isclose(previous, value, rtol=0.0, atol=1.0e-12):
                    raise ValueError(
                        f"Conflicting source voltages at node {nid}: {previous:g} V and {value:g} V."
                    )
                constrained_values[idx] = value

        # Anchor one node in each excluded component so the full sparse matrix
        # remains nonsingular.  These reference values are never returned and
        # therefore cannot contaminate voltage-drop statistics or plots.
        for idx in floating_representatives:
            constrained_values.setdefault(idx, 0.0)
                
        # 6. Solve System
        # Eliminate Dirichlet rows *and columns* while correcting the RHS.
        # Row-only replacement makes the Laplacian non-symmetric and forces
        # slow/fragile BiCGSTAB on CUDA.  Symmetric elimination preserves the
        # exact voltages, yields an SPD matrix and unlocks robust GPU CG.
        G_base = G.tocsr()
        G_base.sum_duplicates()
        G_base.sort_indices()
        if constrained_values:
            constrained_idx = np.fromiter(constrained_values.keys(), dtype=np.int64)
            constrained_voltage = np.fromiter(
                (constrained_values[index] for index in constrained_values),
                dtype=float,
            )
            I -= np.asarray(G_base[:, constrained_idx].dot(constrained_voltage)).reshape(-1)
            free = np.ones(N, dtype=bool)
            free[constrained_idx] = False
            coo = G_base.tocoo(copy=False)
            keep = free[coo.row] & free[coo.col]
            rows = np.concatenate((coo.row[keep], constrained_idx))
            cols = np.concatenate((coo.col[keep], constrained_idx))
            data = np.concatenate((coo.data[keep], np.ones(len(constrained_idx), dtype=float)))
            G_csr = scipy.sparse.coo_matrix((data, (rows, cols)), shape=(N, N)).tocsr()
            I[constrained_idx] = constrained_voltage
        else:
            G_csr = G_base
        G_csr.sort_indices()
        
        try:
            static_values = branch_resistance_scales is None
            # A power rail normally sits close to its source voltage.  Starting
            # CG from that uniform field removes much of the slow near-null
            # error mode on the first CUDA solve.  Later coupled solves replace
            # it with the still-better device-resident previous solution.
            source_voltages = [
                float(source.get('voltage', 0.0)) for source in sources
                if source.get('node_id') in id_to_idx
            ]
            initial_guess = None
            if initial_voltages:
                initial_guess = np.full(
                    N, float(np.median(source_voltages)) if source_voltages else 0.0,
                    dtype=float,
                )
                for node_id, value in initial_voltages.items():
                    index = id_to_idx.get(node_id)
                    if index is not None:
                        initial_guess[index] = float(value)
                for idx, value in constrained_values.items():
                    initial_guess[idx] = value
            elif source_voltages:
                initial_guess = np.full(N, float(np.median(source_voltages)), dtype=float)
                for idx, value in constrained_values.items():
                    initial_guess[idx] = value

            V_solution = None
            if use_precond:
                # ILU(0)-preconditioned conjugate gradient — O(N log N) vs O(N^1.5)
                # for unpreconditioned CG on 2-D Laplacians.
                try:
                    ilu = scipy.sparse.linalg.spilu(
                        G_csr, drop_tol=1e-4, fill_factor=10
                    )
                    M = scipy.sparse.linalg.LinearOperator(G_csr.shape, ilu.solve)
                    x0 = initial_guess if initial_guess is not None else np.zeros(N)
                    x, info = scipy.sparse.linalg.cg(
                        G_csr, I, x0=x0, M=M, rtol=1e-8, maxiter=5 * N
                    )
                    if info == 0:
                        V_solution = x
                        self.last_compute = {"solver": "ILU-CG", "converged": True}
                    else:
                        self._log(
                            f"ILU-CG did not converge (info={info}); "
                            "falling back to default backend."
                        )
                except Exception as precond_exc:
                    self._log(
                        f"ILU preconditioner failed ({precond_exc}); "
                        "falling back to default backend."
                    )

            if V_solution is None:
                solved = self.compute_backend.solve(
                    G_csr, I, system_kind="SPD",
                    cache_key=("dc", id(mesh)), matrix_values_static=static_values,
                    initial_guess=initial_guess,
                )
                self.last_compute = solved.metadata
                V_solution = solved.values

            if np.any(np.isnan(V_solution)):
                self._log("Warning: Solution contains NaN values.")
        except Exception as e:
            self._log(f"Solver Exception: {e}")
            if self.compute_backend.settings.backend == "CUDA":
                raise
            return {}
            
        # 7. Map results back
        results = {}
        for i, v_val in enumerate(V_solution):
            if not valid_node_mask[i]:
                continue
            nid = nodes[i]
            results[nid] = float(v_val)

        if branch_arrays is None and getattr(mesh, 'branches', None):
            branch_arrays = self._branch_arrays(mesh, id_to_idx)
        self._last_solve_state = {
            "mesh_id": id(mesh),
            "values": np.asarray(V_solution, dtype=float),
            "valid_node_mask": valid_node_mask,
            "branch_arrays": branch_arrays,
        }
        self.last_diagnostics = {
            "excluded_load_node_count": len(excluded_load_nodes),
            "excluded_load_references": sorted({
                str(load.get("ref_des")) for load in loads
                if load.get("node_id") in excluded_load_nodes and load.get("ref_des")
            }),
            "floating_island_count": len(floating_representatives),
        }
        return results

    def solve_detailed(
        self, mesh, sources, loads, branch_resistance_scales=None,
        initial_voltages=None,
    ):
        """Solve DC and retain branch currents/losses for thermal coupling."""
        voltages = self.solve(
            mesh,
            sources,
            loads,
            branch_resistance_scales=branch_resistance_scales,
            initial_voltages=initial_voltages,
        )
        scales = (
            np.asarray(branch_resistance_scales, dtype=float).reshape(-1)
            if branch_resistance_scales is not None
            else np.ones(len(getattr(mesh, 'branches', [])), dtype=float)
        )
        if len(scales) != len(getattr(mesh, 'branches', [])):
            raise ValueError("One resistance scale is required for every mesh branch.")
        state = self._last_solve_state
        branch_count = len(getattr(mesh, 'branches', []))
        if state is not None and state["mesh_id"] == id(mesh) and state["branch_arrays"] is not None:
            branch_a, branch_b, base_resistance, valid_endpoints, _, _ = state["branch_arrays"]
            resistance = np.maximum(base_resistance * np.maximum(scales, 1e-9), 1e-15)
            active = valid_endpoints.copy()
            active[valid_endpoints] &= (
                state["valid_node_mask"][branch_a[valid_endpoints]] &
                state["valid_node_mask"][branch_b[valid_endpoints]]
            )
            voltage_delta = np.zeros(branch_count, dtype=float)
            voltage_delta[active] = (
                state["values"][branch_a[active]] - state["values"][branch_b[active]]
            )
            currents_array = voltage_delta / resistance
            losses_array = currents_array * currents_array * resistance
            currents = currents_array.tolist()
            losses = losses_array.tolist()
            total_loss = float(np.sum(losses_array, dtype=float))
        else:
            currents = []
            losses = []
            for branch, scale in zip(getattr(mesh, 'branches', []), scales):
                resistance = max(branch.resistance_ohm * max(float(scale), 1e-9), 1e-15)
                voltage_delta = voltages.get(branch.node_a, 0.0) - voltages.get(branch.node_b, 0.0)
                current = voltage_delta / resistance
                currents.append(float(current))
                losses.append(float(current * current * resistance))
            total_loss = float(sum(losses))
        excluded_count = int(self.last_diagnostics.get("excluded_load_node_count", 0))
        excluded_references = list(self.last_diagnostics.get("excluded_load_references", []))
        warnings = []
        if excluded_count:
            reference_suffix = (
                f" ({', '.join(excluded_references)})" if excluded_references else ""
            )
            warnings.append(
                f"{excluded_count} load node(s){reference_suffix} excluded because their copper "
                "island has no voltage source."
            )
        return DCSolveResult(
            voltages=voltages,
            branch_currents_a=currents,
            branch_losses_w=losses,
            total_loss_w=total_loss,
            compute_metadata=self.last_compute,
            valid=excluded_count == 0,
            excluded_load_node_count=excluded_count,
            excluded_load_references=excluded_references,
            warnings=warnings,
        )

