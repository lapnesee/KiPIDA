"""Sparse frequency-domain solver for Ki-PIDA power distribution networks."""

import time
import warnings

try:
    import numpy as np
    import scipy.sparse
    import scipy.sparse.linalg
except ImportError:
    np = None
    scipy = None

try:
    from .models import ACAnalysisSettings, ImpedanceSweepResult
    from .compute_backend import SparseComputeBackend
except (ImportError, ValueError):
    from models import ACAnalysisSettings, ImpedanceSweepResult
    from compute_backend import SparseComputeBackend


class ACSolver:
    def __init__(self, debug=False, log_callback=None, compute_settings=None):
        self.debug = debug
        self.log_callback = log_callback
        self.compute_backend = SparseComputeBackend(compute_settings, log_callback)
        if np is None or scipy is None:
            raise ImportError("NumPy and SciPy are required for AC analysis.")

    def _log(self, message):
        if self.log_callback:
            self.log_callback(f"[AC SOLVER] {message}")

    @staticmethod
    def capacitor_impedance(capacitor, frequency_hz):
        omega = 2.0 * np.pi * frequency_hz
        capacitance = max(float(capacitor.capacitance_f), 1e-18)
        return complex(capacitor.esr_ohm, omega * capacitor.esl_h - 1.0 / (omega * capacitance))

    @staticmethod
    def _stamp_branch(matrix, node_a, node_b, admittance):
        matrix[node_a, node_a] += admittance
        matrix[node_b, node_b] += admittance
        matrix[node_a, node_b] -= admittance
        matrix[node_b, node_a] -= admittance

    def _stamp_group_branch(self, matrix, connection, admittance):
        if not connection.rail_nodes or not connection.ground_nodes:
            return
        pairs = len(connection.rail_nodes) * len(connection.ground_nodes)
        distributed = admittance / pairs
        for rail_node in connection.rail_nodes:
            for ground_node in connection.ground_nodes:
                self._stamp_branch(matrix, rail_node, ground_node, distributed)

    @staticmethod
    def _topology_anchors(network, capacitors):
        """Find floating islands and verify that the measurement reaches the source."""
        adjacency = [set() for _ in range(network.node_count)]

        def connect(node_a, node_b):
            if not (0 <= node_a < network.node_count and 0 <= node_b < network.node_count):
                raise ValueError("The AC network contains an invalid node reference.")
            adjacency[node_a].add(node_b)
            adjacency[node_b].add(node_a)

        for branch in network.branches:
            connect(branch.node_a, branch.node_b)
        for rail_node in network.source.rail_nodes:
            for ground_node in network.source.ground_nodes:
                connect(rail_node, ground_node)
        for capacitor in capacitors:
            if not capacitor.enabled or capacitor.capacitance_f <= 0:
                continue
            connection = network.capacitor_nodes.get(capacitor.ref_des)
            if connection is None:
                continue
            for rail_node in connection.rail_nodes:
                for ground_node in connection.ground_nodes:
                    connect(rail_node, ground_node)

        components = []
        component_by_node = {}
        remaining = set(range(network.node_count))
        while remaining:
            start = next(iter(remaining))
            stack = [start]
            component = []
            remaining.remove(start)
            while stack:
                node = stack.pop()
                component_by_node[node] = len(components)
                component.append(node)
                for neighbor in adjacency[node]:
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        stack.append(neighbor)
            components.append(component)

        source_anchor = network.source.ground_nodes[0]
        source_component = component_by_node[source_anchor]
        measurement_nodes = network.measurement.rail_nodes + network.measurement.ground_nodes
        if any(node < 0 or node >= network.node_count for node in measurement_nodes):
            raise ValueError("The measurement port contains an invalid node reference.")
        if any(component_by_node[node] != source_component for node in measurement_nodes):
            raise ValueError("The measurement port is not electrically connected to the AC source.")

        anchors = [source_anchor]
        anchors.extend(component[0] for index, component in enumerate(components)
                       if index != source_component)
        return anchors

    def _assemble(self, network, settings, capacitors, frequency):
        """Stamp the network at one frequency. Identical for every port."""
        omega = 2.0 * np.pi * frequency
        matrix = scipy.sparse.lil_matrix(
            (network.node_count, network.node_count), dtype=np.complex128,
        )
        for branch in network.branches:
            resistance = max(float(branch.resistance_ohm), 1e-15)
            impedance = complex(resistance, omega * max(0.0, float(branch.inductance_h)))
            self._stamp_branch(matrix, branch.node_a, branch.node_b, 1.0 / impedance)

        source_impedance = complex(
            max(float(settings.source.resistance_ohm), 1e-9),
            omega * max(0.0, float(settings.source.inductance_h)),
        )
        self._stamp_group_branch(matrix, network.source, 1.0 / source_impedance)

        for capacitor in capacitors:
            if not capacitor.enabled or capacitor.capacitance_f <= 0:
                continue
            connection = network.capacitor_nodes.get(capacitor.ref_des)
            if connection is None:
                continue
            impedance = self.capacitor_impedance(capacitor, frequency)
            self._stamp_group_branch(matrix, connection, 1.0 / impedance)
        return matrix

    @staticmethod
    def _injection(network, connection):
        """Unit current into the rail side of a port and out of its return."""
        current = np.zeros(network.node_count, dtype=np.complex128)
        for node_id in connection.rail_nodes:
            current[node_id] += 1.0 / len(connection.rail_nodes)
        for node_id in connection.ground_nodes:
            current[node_id] -= 1.0 / len(connection.ground_nodes)
        return current

    @staticmethod
    def _port_voltage(connection, voltage):
        rail_voltage = np.mean([voltage[node] for node in connection.rail_nodes])
        ground_voltage = np.mean([voltage[node] for node in connection.ground_nodes])
        return complex(rail_voltage - ground_voltage)

    @staticmethod
    def _summarize(frequencies, impedances, settings, network, **compute):
        magnitudes = np.abs(np.asarray(impedances))
        worst_index = int(np.argmax(magnitudes))
        target = max(0.0, float(settings.target_impedance_ohm))
        return ImpedanceSweepResult(
            frequencies_hz=[float(value) for value in frequencies],
            impedance_ohm=list(impedances),
            target_impedance_ohm=target,
            worst_frequency_hz=float(frequencies[worst_index]),
            worst_impedance_ohm=float(magnitudes[worst_index]),
            meets_target=bool(target > 0 and np.all(magnitudes <= target)),
            mesh_node_count=int(network.node_count),
            requested_grid_size_mm=float(network.requested_grid_size_mm),
            effective_grid_size_mm=float(network.effective_grid_size_mm),
            **compute,
        )

    def solve_sweep_multiport(
        self, network, settings: ACAnalysisSettings, ports=None,
        capacitors=None, progress_callback=None,
    ):
        """Sweep every observation point and return the worst case.

        A PDN is qualified by its worst port, and which port that is cannot be
        known before solving -- so sweeping them is the alternative to making
        the user try combinations by hand.

        At one frequency the matrix is the same for every port; only the
        right-hand side changes. It is therefore factorised once per frequency
        and applied to each port, rather than re-solving the system N times.

        The returned result is the worst port's sweep, carrying every port's
        sweep in ``per_port_results``, so single-port consumers are unaffected.
        """
        ports = dict(ports if ports is not None else (getattr(network, "ports", None) or {}))
        if not ports:
            ports = {"": network.measurement}

        # One port that is the network's own measurement is the historical
        # case: delegate so its numbers and compute metadata stay identical.
        if len(ports) == 1:
            ref_des, connection = next(iter(ports.items()))
            if (connection.rail_nodes == network.measurement.rail_nodes
                    and connection.ground_nodes == network.measurement.ground_nodes):
                result = self.solve_sweep(
                    network, settings, capacitors, progress_callback,
                )
                result.per_port_results = {ref_des: result}
                result.worst_port_ref_des = ref_des
                return result

        if settings.frequency_points < 2:
            raise ValueError("At least two frequency points are required.")
        capacitors = list(settings.capacitors if capacitors is None else capacitors)
        anchors = self._topology_anchors(network, capacitors)

        usable = {
            ref: connection for ref, connection in ports.items()
            if connection.rail_nodes and connection.ground_nodes
        }
        for ref in sorted(set(ports) - set(usable)):
            self._log(f"Port '{ref}' is not connected to both meshes; skipped.")
        if not usable:
            raise ValueError("No measurement port is connected to the AC network.")

        frequencies = np.logspace(
            np.log10(settings.frequency_start_hz),
            np.log10(settings.frequency_stop_hz),
            int(settings.frequency_points),
        )
        per_port = {ref: [] for ref in usable}
        solve_seconds = 0.0

        for index, frequency in enumerate(frequencies):
            matrix = self._assemble(network, settings, capacitors, frequency)
            injections = {ref: self._injection(network, connection)
                          for ref, connection in usable.items()}
            for anchor in anchors:
                matrix.rows[anchor] = [anchor]
                matrix.data[anchor] = [1.0 + 0.0j]
                for current in injections.values():
                    current[anchor] = 0.0

            started = time.perf_counter()
            try:
                factorized = scipy.sparse.linalg.splu(matrix.tocsc())
            except Exception as exc:
                raise ValueError(f"AC solve failed at {frequency:g} Hz: {exc}") from exc
            for ref, connection in usable.items():
                voltage = factorized.solve(injections[ref])
                if not np.all(np.isfinite(voltage)):
                    raise ValueError(f"AC solve produced non-finite values at {frequency:g} Hz.")
                per_port[ref].append(self._port_voltage(connection, voltage))
            solve_seconds += time.perf_counter() - started

            if progress_callback:
                progress_callback(index + 1, len(frequencies), float(frequency))

        results = {
            ref: self._summarize(
                frequencies, impedances, settings, network,
                compute_backend="CPU_SCIPY", compute_device="CPU",
                compute_solve_seconds=solve_seconds / max(len(usable), 1),
            )
            for ref, impedances in per_port.items()
        }
        worst_ref = max(results, key=lambda ref: results[ref].worst_impedance_ohm)
        worst = results[worst_ref]
        worst.per_port_results = results
        worst.worst_port_ref_des = worst_ref
        if self.debug:
            self._log(
                f"Swept {len(usable)} port(s); worst is '{worst_ref}' at "
                f"{worst.worst_impedance_ohm:.6g} ohm."
            )
        return worst

    def solve_sweep(self, network, settings: ACAnalysisSettings, capacitors=None, progress_callback=None):
        if settings.frequency_start_hz <= 0:
            raise ValueError("Start frequency must be greater than zero.")
        if settings.frequency_stop_hz <= settings.frequency_start_hz:
            raise ValueError("Stop frequency must be greater than start frequency.")
        if settings.frequency_points < 2:
            raise ValueError("At least two frequency points are required.")
        if not network.measurement.rail_nodes or not network.measurement.ground_nodes:
            raise ValueError("Measurement port is not connected to the AC network.")

        capacitors = list(settings.capacitors if capacitors is None else capacitors)
        if not network.source.rail_nodes or not network.source.ground_nodes:
            raise ValueError("AC source is not connected to the network.")
        anchors = self._topology_anchors(network, capacitors)
        frequencies = np.logspace(
            np.log10(settings.frequency_start_hz),
            np.log10(settings.frequency_stop_hz),
            int(settings.frequency_points),
        )
        impedances = []
        compute_samples = []

        for index, frequency in enumerate(frequencies):
            matrix = self._assemble(network, settings, capacitors, frequency)
            current = self._injection(network, network.measurement)

            # Fix one return node to zero volts to remove the arbitrary common-mode potential.
            for anchor in anchors:
                matrix.rows[anchor] = [anchor]
                matrix.data[anchor] = [1.0 + 0.0j]
                current[anchor] = 0.0

            with warnings.catch_warnings():
                warnings.simplefilter("error", scipy.sparse.linalg.MatrixRankWarning)
                try:
                    solved = self.compute_backend.solve(
                        matrix.tocsr(), current, system_kind="GENERAL",
                        cache_key=("ac", id(network)), matrix_values_static=False,
                    )
                    voltage = solved.values
                    compute_samples.append(solved.metadata)
                    if (
                        solved.metadata.fallback_reason and
                        self.compute_backend.settings.backend == "AUTO"
                    ):
                        # A general complex AC matrix that exhausts the CUDA
                        # iteration budget is unlikely to improve at the next
                        # adjacent frequency.  Avoid paying that full failed
                        # trial at every sweep point; CPU direct solve remains
                        # the deterministic fallback for the rest of this run.
                        self.compute_backend.settings.backend = "CPU"
                        self._log(
                            "CUDA did not converge for this AC network; continuing "
                            "the remaining frequency points on CPU."
                        )
                except Exception as exc:
                    raise ValueError(f"AC solve failed at {frequency:g} Hz: {exc}") from exc

            rail_voltage = np.mean([voltage[node] for node in network.measurement.rail_nodes])
            ground_voltage = np.mean([voltage[node] for node in network.measurement.ground_nodes])
            impedances.append(complex(rail_voltage - ground_voltage))

            if progress_callback:
                progress_callback(index + 1, len(frequencies), float(frequency))

        magnitudes = np.abs(np.asarray(impedances))
        worst_index = int(np.argmax(magnitudes))
        target = max(0.0, float(settings.target_impedance_ohm))
        meets_target = bool(target > 0 and np.all(magnitudes <= target))
        if self.debug:
            self._log(
                f"Solved {len(frequencies)} points; worst |Z|={magnitudes[worst_index]:.6g} ohm "
                f"at {frequencies[worst_index]:.6g} Hz."
            )

        return ImpedanceSweepResult(
            frequencies_hz=[float(value) for value in frequencies],
            impedance_ohm=impedances,
            target_impedance_ohm=target,
            worst_frequency_hz=float(frequencies[worst_index]),
            worst_impedance_ohm=float(magnitudes[worst_index]),
            meets_target=meets_target,
            compute_backend=compute_samples[-1].backend if compute_samples else "CPU",
            compute_device=compute_samples[-1].device if compute_samples else "CPU",
            compute_solve_seconds=sum(item.solve_seconds for item in compute_samples),
            compute_transfer_seconds=sum(item.transfer_seconds for item in compute_samples),
            compute_relative_residual=max(
                (item.relative_residual for item in compute_samples), default=0.0
            ),
            compute_iterations=sum(item.iterations for item in compute_samples),
            compute_cache_hits=sum(bool(item.cache_hit) for item in compute_samples),
            mesh_node_count=int(network.node_count),
            requested_grid_size_mm=float(network.requested_grid_size_mm),
            effective_grid_size_mm=float(network.effective_grid_size_mm),
        )
