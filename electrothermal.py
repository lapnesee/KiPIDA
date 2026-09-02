"""Iterative coupling between Ki-PIDA DC meshes and the 3D thermal model."""

import time

import numpy as np

try:
    from .models import ElectroThermalResult
    from .solver import Solver
    from .thermal_model import PowerLossEstimator
    from .thermal_solver import ThermalSolver
except (ImportError, ValueError):
    from models import ElectroThermalResult
    from solver import Solver
    from thermal_model import PowerLossEstimator
    from thermal_solver import ThermalSolver


class ElectroThermalSolver:
    def __init__(self, debug=False, log_callback=None, compute_settings=None, thermal_solver=None):
        self.debug = debug
        self.log_callback = log_callback
        self.compute_settings = compute_settings
        # A dialog-level solver retains the matrix and CUDA CSR workspace for
        # an unchanged thermal mesh across runs.  Falling back preserves the
        # public standalone solver behaviour.
        self.thermal_solver = thermal_solver

    def _log(self, message):
        if self.log_callback:
            self.log_callback(f"[ELECTRO-THERMAL] {message}")

    @staticmethod
    def _branch_temperature(thermal_mesh, temperatures, electrical_mesh, branch, ambient_c):
        coord_a = electrical_mesh.node_coords.get(branch.node_a)
        coord_b = electrical_mesh.node_coords.get(branch.node_b)
        if coord_a is None or coord_b is None:
            return float(ambient_c)
        x = (coord_a[0] + coord_b[0]) / 2.0
        y = (coord_a[1] + coord_b[1]) / 2.0
        layer = coord_a[2]
        node = thermal_mesh.nearest_node(x, y, layer)
        return float(temperatures.get(node, ambient_c)) if node is not None else float(ambient_c)

    @staticmethod
    def _thermal_node_index(thermal_mesh, node_id, identity_nodes, node_to_index):
        if node_id is None:
            return -1
        return int(node_id) if identity_nodes else int(node_to_index.get(node_id, -1))

    def _prepare_branch_thermal_indices(self, thermal_mesh, rail_contexts):
        """Map each electrical branch midpoint to a thermal cell once.

        Copper geometry does not move during electro-thermal convergence.  The
        legacy implementation performed this spatial lookup twice per branch
        and per iteration: once to scale resistance and once to inject Joule
        loss.  Caching the thermal-node index is exact for a fixed mesh.
        """
        nodes = thermal_mesh.nodes
        identity_nodes = bool(nodes and nodes[0] == 0 and nodes[-1] == len(nodes) - 1)
        node_to_index = None if identity_nodes else {node: index for index, node in enumerate(nodes)}
        mappings = {}
        started = time.perf_counter()
        branch_count = 0
        for rail_name, context in rail_contexts.items():
            electrical_mesh = context["mesh"]
            indices = np.full(len(electrical_mesh.branches), -1, dtype=np.int32)
            for index, branch in enumerate(electrical_mesh.branches):
                coord_a = electrical_mesh.node_coords.get(branch.node_a)
                coord_b = electrical_mesh.node_coords.get(branch.node_b)
                if coord_a is None or coord_b is None:
                    continue
                node = thermal_mesh.nearest_node(
                    (coord_a[0] + coord_b[0]) / 2.0,
                    (coord_a[1] + coord_b[1]) / 2.0,
                    coord_a[2],
                )
                indices[index] = self._thermal_node_index(
                    thermal_mesh, node, identity_nodes, node_to_index
                )
            mappings[rail_name] = indices
            branch_count += len(indices)
        self._log(
            f"Cached {branch_count:,} electrical-branch to thermal-cell mappings in "
            f"{time.perf_counter() - started:.3f} s."
        )
        return mappings, identity_nodes, node_to_index

    @staticmethod
    def _stage_power_by_component(stages):
        power = {}
        for stage in stages or []:
            for loss in stage.losses:
                if loss.ref_des:
                    power[loss.ref_des] = power.get(loss.ref_des, 0.0) + max(
                        0.0, float(loss.power_w)
                    )
        return power

    @staticmethod
    def _component_temperatures(thermal_mesh, temperatures, ambient_c,
                                identity_nodes, node_to_index):
        result = {}
        for ref_des, nodes in thermal_mesh.component_nodes.items():
            indices = [
                int(node) if identity_nodes else int(node_to_index.get(node, -1))
                for node in nodes
            ]
            indices = [index for index in indices if index >= 0]
            board_temperature = (
                float(np.mean(temperatures[indices])) if indices else float(ambient_c)
            )
            component = thermal_mesh.component_models.get(ref_des)
            junction_rise = 0.0
            if component is not None:
                junction_rise = max(0.0, float(component.power_w)) * max(
                    0.0, float(component.theta_jb_c_per_w)
                )
            result[ref_des] = board_temperature + junction_rise
        return result

    def solve(self, thermal_mesh, settings, rail_contexts, progress_callback=None,
              rails=None):
        if not rail_contexts:
            raise ValueError("Run a DC analysis before coupled electro-thermal analysis.")
        thermal_solver = self.thermal_solver or ThermalSolver(
            debug=self.debug,
            log_callback=self.log_callback,
            compute_settings=self.compute_settings,
        )
        dc_solver = Solver(
            debug=self.debug, log_callback=self.log_callback,
            compute_settings=self.compute_settings,
        )
        node_count = len(thermal_mesh.nodes)
        base_heat = dict(thermal_mesh.heat_sources_w)
        base_heat_vector = np.zeros(node_count, dtype=float)
        identity_nodes = bool(
            thermal_mesh.nodes and thermal_mesh.nodes[0] == 0 and
            thermal_mesh.nodes[-1] == node_count - 1
        )
        node_to_index = None if identity_nodes else {
            node: index for index, node in enumerate(thermal_mesh.nodes)
        }
        for node, power in base_heat.items():
            index = self._thermal_node_index(thermal_mesh, node, identity_nodes, node_to_index)
            if index >= 0:
                base_heat_vector[index] += float(power)
        # The mesh already contains the room-temperature stage estimate.  Strip
        # only that portion, retain loads/manual heat, then replace it from the
        # temperature-coupled model on every iteration.
        initial_stages = list(getattr(settings, "power_stage_reports", []) or [])
        initial_stage_power = self._stage_power_by_component(initial_stages)
        component_nonstage_power = {}
        if rails:
            for ref_des, stage_power in initial_stage_power.items():
                nodes = thermal_mesh.component_nodes.get(ref_des, [])
                component = thermal_mesh.component_models.get(ref_des)
                if not nodes or getattr(component, "model_source", "") == "user":
                    continue
                per_node = stage_power / len(nodes)
                for node in nodes:
                    index = self._thermal_node_index(
                        thermal_mesh, node, identity_nodes, node_to_index
                    )
                    if index >= 0:
                        base_heat_vector[index] -= per_node
                if component is not None:
                    component_nonstage_power[ref_des] = max(
                        0.0, float(component.power_w) - stage_power
                    )
        branch_mappings, _, _ = self._prepare_branch_thermal_indices(thermal_mesh, rail_contexts)
        previous_temperatures = np.full(node_count, float(settings.ambient_c), dtype=float)
        dc_results = {}
        converged = False
        thermal_result = None
        iterations = max(1, int(settings.coupled_iterations))

        for iteration in range(iterations):
            iteration_started = time.perf_counter()
            # Dense injection avoids millions of dictionary lookups on a fine
            # mesh.  ThermalSolver consumes this vector directly.
            heat_vector = base_heat_vector.copy()
            thermal_mesh.heat_sources_w = base_heat
            thermal_mesh.heat_vector_w = heat_vector
            dc_results = {}
            dynamic_stages = initial_stages
            if rails:
                component_temperatures = self._component_temperatures(
                    thermal_mesh, previous_temperatures, settings.ambient_c,
                    identity_nodes, node_to_index,
                )
                dynamic = PowerLossEstimator.estimate_details(
                    rails, component_temperatures_c=component_temperatures
                )
                dynamic_stages = dynamic.stages
                dynamic_power = self._stage_power_by_component(dynamic_stages)
                for ref_des, stage_power in dynamic_power.items():
                    nodes = thermal_mesh.component_nodes.get(ref_des, [])
                    component = thermal_mesh.component_models.get(ref_des)
                    if not nodes or getattr(component, "model_source", "") == "user":
                        continue
                    per_node = stage_power / len(nodes)
                    for node in nodes:
                        index = self._thermal_node_index(
                            thermal_mesh, node, identity_nodes, node_to_index
                        )
                        if index >= 0:
                            heat_vector[index] += per_node
                    if component is not None:
                        component.power_w = (
                            component_nonstage_power.get(ref_des, 0.0) + stage_power
                        )
            for rail_name, context in rail_contexts.items():
                electrical_mesh = context["mesh"]
                thermal_indices = branch_mappings[rail_name]
                branch_temperatures = np.full(
                    len(thermal_indices), float(settings.ambient_c), dtype=float
                )
                valid_indices = thermal_indices >= 0
                branch_temperatures[valid_indices] = previous_temperatures[
                    thermal_indices[valid_indices]
                ]
                scales = np.clip(
                    1.0 + float(settings.copper_temp_coefficient_per_c) *
                    (branch_temperatures - 20.0),
                    0.2, 3.0,
                )
                detailed = dc_solver.solve_detailed(
                    electrical_mesh,
                    context.get("sources", []),
                    context.get("loads", []),
                    branch_resistance_scales=scales,
                    initial_voltages=context.get("initial_voltages") if iteration == 0 else None,
                )
                dc_results[rail_name] = detailed
                losses = np.asarray(detailed.branch_losses_w, dtype=float)
                deposit = valid_indices & (losses > 0.0)
                if np.any(deposit):
                    np.add.at(heat_vector, thermal_indices[deposit], losses[deposit])

            dc_phase_seconds = time.perf_counter() - iteration_started
            thermal_result = thermal_solver.solve(
                thermal_mesh, settings.ambient_c, materialize_temperatures=False
            )
            current_temperatures = thermal_result.temperature_vector_c
            if current_temperatures is None:
                current_temperatures = np.fromiter(
                    (thermal_result.temperatures_c[node] for node in thermal_mesh.nodes),
                    dtype=float, count=node_count,
                )
            max_delta = float(np.max(np.abs(current_temperatures - previous_temperatures)))
            relaxation = max(0.05, min(1.0, float(settings.relaxation)))
            previous_temperatures += relaxation * (current_temperatures - previous_temperatures)
            self._log(
                f"Iteration {iteration + 1}/{iterations}: max delta {max_delta:.4g} C, "
                f"copper loss {sum(result.total_loss_w for result in dc_results.values()):.4g} W; "
                f"DC/map {dc_phase_seconds:.3f} s, total {time.perf_counter() - iteration_started:.3f} s."
            )
            if progress_callback:
                progress_callback(iteration + 1, iterations, f"delta={max_delta:.3g} C")
            if max_delta <= max(1e-6, float(settings.convergence_c)):
                converged = True
                break

        thermal_result.temperatures_c = {
            node: float(thermal_result.temperature_vector_c[index])
            for index, node in enumerate(thermal_mesh.nodes)
        }
        if rails:
            settings.power_stage_reports = dynamic_stages
        thermal_result.iterations = iteration + 1
        thermal_result.converged = converged
        return ElectroThermalResult(
            thermal=thermal_result,
            dc_results=dc_results,
            iterations=iteration + 1,
            converged=converged,
        )
