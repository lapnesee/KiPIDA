"""Sparse frequency-domain solver for Ki-PIDA power distribution networks."""

from dataclasses import replace
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
    from .compute_backend import ComputeMetadata, SparseComputeBackend
except (ImportError, ValueError):
    from models import ACAnalysisSettings, ImpedanceSweepResult
    from compute_backend import ComputeMetadata, SparseComputeBackend


# Relative disagreement between the GPU and CPU solutions of the same system
# above which the GPU result is not trusted for this network. The sweep is
# compared against a target expressed in milliohms, so agreement well below
# the part-per-million level is what "as accurate as CPU" has to mean here.
GPU_ACCURACY_TOLERANCE = 1.0e-6

# The CPU sweep: SciPy's SuperLU factorisation, reused as a GMRES
# preconditioner across neighbouring frequencies.
RECYCLED_LU_BACKEND = "CPU_SCIPY_LU_GMRES"
# GMRES iterations past which a factorisation carried over from an earlier
# frequency is replaced at the next point. Each iteration costs a pair of
# triangular solves; a dozen of them approach the price of a fresh
# factorisation spread over the points it then serves.
LU_REFRESH_ITERATIONS = 12
# Krylov space per GMRES restart, and the restarts allowed before a carried
# factorisation is abandoned for the current point.
GMRES_RESTART = 20
GMRES_MAX_RESTARTS = 3


class _NetworkStamps:
    """The network's nodal admittance matrix, stamped at any frequency.

    Stamping element by element into a LIL matrix took 2.8 s per frequency
    on a 94,000-node network: a Python loop over 187,000 branches, repeated
    at every point of the sweep. The topology does not depend on frequency,
    so it is laid out once as index arrays and each point only evaluates
    the admittances, which takes NumPy milliseconds.

    An anchored node is pinned to zero volts by an identity row *and*
    column. Its voltage is zero, so dropping the column changes no solution,
    and it keeps the matrix complex symmetric, which the factorisation in
    :class:`_RecycledLUSolver` relies on.
    """

    def __init__(self, network, lumped_elements, anchors):
        branches = network.branches
        count = len(branches)
        self.node_count = int(network.node_count)
        end_a = [np.fromiter((branch.node_a for branch in branches), dtype=np.int64, count=count)]
        end_b = [np.fromiter((branch.node_b for branch in branches), dtype=np.int64, count=count)]
        self.resistance = np.maximum(np.fromiter(
            (float(branch.resistance_ohm) for branch in branches), dtype=float, count=count,
        ), 1e-15)
        self.inductance = np.maximum(np.fromiter(
            (float(branch.inductance_h) for branch in branches), dtype=float, count=count,
        ), 0.0)

        # Plane-pair (cavity) capacitance: one jw*C admittance per rail/return
        # node pair the mesh found overlapping (mesh.Mesher.cavity_capacitor_pairs,
        # via ac_model.ACModelBuilder.build()). Stamped vectorized here, next
        # to the R+jwL branches, rather than through the lumped mechanism
        # below: lumped elements cost one Python-level impedance() call per
        # DISTINCT element at every frequency, fine for a handful of ports
        # and capacitors but not for the tens of thousands of cavity pairs a
        # real board mesh produces.
        cavity = list(getattr(network, "cavity_capacitors", None) or ())
        cavity_count = len(cavity)
        cavity_a = np.fromiter((item[0] for item in cavity), dtype=np.int64, count=cavity_count)
        cavity_b = np.fromiter((item[1] for item in cavity), dtype=np.int64, count=cavity_count)
        self.cavity_capacitance = np.fromiter(
            (float(item[2]) for item in cavity), dtype=float, count=cavity_count,
        )
        end_a.append(cavity_a)
        end_b.append(cavity_b)

        # A lumped element (the source, a capacitor) spreads its admittance
        # evenly over every rail/return node pair it connects.
        self.impedances = []
        owner, share = [], []
        for connection, impedance in lumped_elements:
            rail = np.asarray(connection.rail_nodes, dtype=np.int64)
            ground = np.asarray(connection.ground_nodes, dtype=np.int64)
            if rail.size == 0 or ground.size == 0:
                continue
            pairs = rail.size * ground.size
            end_a.append(np.repeat(rail, ground.size))
            end_b.append(np.tile(ground, rail.size))
            owner.append(np.full(pairs, len(self.impedances), dtype=np.int64))
            share.append(np.full(pairs, 1.0 / pairs))
            self.impedances.append(impedance)
        self.lumped_owner = np.concatenate(owner) if owner else np.zeros(0, dtype=np.int64)
        self.lumped_share = np.concatenate(share) if share else np.zeros(0)

        end_a = np.concatenate(end_a)
        end_b = np.concatenate(end_b)
        rows = np.concatenate([end_a, end_b, end_a, end_b])
        cols = np.concatenate([end_a, end_b, end_b, end_a])
        self.anchors = np.unique(np.asarray(list(anchors), dtype=np.int64))
        pinned = np.zeros(self.node_count, dtype=bool)
        pinned[self.anchors] = True
        self.keep = ~(pinned[rows] | pinned[cols])
        self.rows = np.concatenate([rows[self.keep], self.anchors])
        self.cols = np.concatenate([cols[self.keep], self.anchors])

    def matrix(self, frequency):
        """The anchored admittance matrix at *frequency*, in CSC form."""
        omega = 2.0 * np.pi * frequency
        lumped = np.array(
            [1.0 / impedance(frequency) for impedance in self.impedances],
            dtype=np.complex128,
        )
        admittance = np.concatenate([
            1.0 / (self.resistance + 1j * omega * self.inductance),
            1j * omega * self.cavity_capacitance,
            lumped[self.lumped_owner] * self.lumped_share,
        ])
        stamped = np.concatenate([admittance, admittance, -admittance, -admittance])
        data = np.concatenate([
            stamped[self.keep], np.ones(self.anchors.size, dtype=np.complex128),
        ])
        return scipy.sparse.coo_matrix(
            (data, (self.rows, self.cols)), shape=(self.node_count, self.node_count),
        ).tocsc()


class _RecycledLUSolver:
    """Solves the points of a sweep with one LU carried between frequencies.

    Neighbouring points of a logarithmic sweep differ by a few percent, so
    the LU of one is a near-inverse of the next: GMRES preconditioned by it
    converges in a handful of iterations, each a pair of triangular solves,
    where a fresh factorisation costs seconds. The LU is refreshed once GMRES
    needs more than LU_REFRESH_ITERATIONS, and every answer, on a carried or
    a fresh LU alike, is accepted only after GMRES has driven the true
    residual under the solver tolerance: reuse changes the cost, never the
    accuracy.
    """

    def __init__(self, rtol):
        self.rtol = float(rtol)
        self.factorizations = 0
        self.iterations = 0
        self._lu = None
        self._refresh = False

    @staticmethod
    def _factorize(matrix, pivoting):
        if pivoting:
            return scipy.sparse.linalg.splu(matrix)
        # The anchored matrix is complex symmetric, and its real part -- the
        # conductances -- is positive definite once every element has some
        # resistance, so no pivot can vanish: SuperLU may keep a symmetric
        # fill-reducing order instead of pivoting across it. A pivot small
        # enough to cost accuracy fails the residual test and sends the point
        # to the pivoting factorisation.
        return scipy.sparse.linalg.splu(
            matrix, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.0,
            options={"SymmetricMode": True},
        )

    def _iterate(self, matrix, right_hand_sides):
        """GMRES on every right-hand side against the current LU.

        Returns ``(solutions or None, iterations spent, most one needed)``.
        """
        preconditioner = scipy.sparse.linalg.LinearOperator(
            matrix.shape, matvec=self._lu.solve, dtype=matrix.dtype,
        )
        solutions, spent, most = [], 0, 0
        for rhs in right_hand_sides:
            count = [0]

            def counted(_residual):
                count[0] += 1

            values, status = scipy.sparse.linalg.gmres(
                matrix, rhs, M=preconditioner, rtol=self.rtol, atol=0.0,
                restart=GMRES_RESTART, maxiter=GMRES_MAX_RESTARTS,
                callback=counted, callback_type="pr_norm",
            )
            spent += count[0]
            most = max(most, count[0])
            if status != 0 or not np.all(np.isfinite(values)):
                return None, spent, most
            solutions.append(values)
        return solutions, spent, most

    def solve(self, matrix, right_hand_sides):
        """Solutions for every right-hand side at one frequency, and metadata."""
        started = time.perf_counter()
        spent = 0
        reused = False
        solutions = None
        if self._lu is not None and not self._refresh:
            solutions, spent, most = self._iterate(matrix, right_hand_sides)
            reused = solutions is not None
            self._refresh = reused and most > LU_REFRESH_ITERATIONS
        if solutions is None:
            for pivoting in (False, True):
                self._lu = self._factorize(matrix, pivoting)
                self.factorizations += 1
                self._refresh = False
                solutions, extra, _most = self._iterate(matrix, right_hand_sides)
                spent += extra
                if solutions is not None:
                    break
            else:
                raise RuntimeError(
                    "the LU-preconditioned GMRES solve did not reach a relative "
                    f"residual of {self.rtol:g}, even on a pivoting factorisation."
                )
        self.iterations += spent
        residual = max(
            float(np.linalg.norm(matrix.dot(values) - rhs))
            / max(float(np.linalg.norm(rhs)), 1.0e-30)
            for values, rhs in zip(solutions, right_hand_sides)
        )
        return solutions, ComputeMetadata(
            backend=RECYCLED_LU_BACKEND,
            solve_seconds=time.perf_counter() - started,
            relative_residual=residual,
            iterations=max(1, spent),
            matrix_reused=reused,
            solver_method="LU_GMRES",
        )


class CandidateImpedances:
    """Port impedances of one configuration plus any candidate capacitors.

    Populating a footprint adds its admittance between a handful of mesh
    nodes: a low-rank change ``P C P^T`` to the network matrix ``A``. With
    ``U = A^-1 P`` solved once per frequency, the Woodbury identity gives the
    port voltages of ``A + P C P^T`` exactly from ``U`` and the base solution,
    through a system as small as the nodes touched. Every trial of the
    decoupling optimizer, over every footprint, value and round, is then
    dense algebra on those few nodes instead of a sweep of the whole network
    (AC-9).

    The identity holds only while populating a footprint leaves the anchors
    alone, i.e. while every node it touches already lies on the source's
    copper: :meth:`covers` says which footprints qualify.
    """

    def __init__(self, network, settings, frequencies, ports, excluded,
                 nodes, pinned, base, node_side, transfer, covered):
        self.network = network
        self.settings = settings
        self.frequencies = frequencies
        self.ports = ports
        self.excluded = excluded
        self._position = {node: index for index, node in enumerate(nodes)}
        self._pinned = pinned
        self._base = base            # (F, ports): port impedances of A
        self._node_side = node_side  # (F, nodes, ports): A^-1 J at the nodes
        self._transfer = transfer    # (F, ports + nodes, nodes): w^T U, then U at the nodes
        self._covered = covered

    def covers(self, ref_des):
        return ref_des in self._covered

    def _coupling(self, capacitors):
        """Touched positions and the added admittance on them, per frequency."""
        stamps = []
        for capacitor in capacitors:
            if capacitor.capacitance_f <= 0:
                continue
            if capacitor.ref_des not in self._covered:
                raise ValueError(f"{capacitor.ref_des} is not a covered candidate.")
            connection = self.network.capacitor_nodes[capacitor.ref_des]
            admittance = np.array([
                1.0 / ACSolver.capacitor_impedance(capacitor, frequency)
                for frequency in self.frequencies
            ], dtype=np.complex128)
            # Same spread as _NetworkStamps: evenly over every rail/return
            # pair, with the rows and columns of anchored nodes dropped.
            share = 1.0 / (len(connection.rail_nodes) * len(connection.ground_nodes))
            for rail in connection.rail_nodes:
                for ground in connection.ground_nodes:
                    stamps.append((rail, ground, admittance * share))
        touched = sorted({
            node for rail, ground, _ in stamps for node in (rail, ground)
            if node not in self._pinned
        })
        local = {node: index for index, node in enumerate(touched)}
        coupling = np.zeros((len(self.frequencies), len(touched), len(touched)), dtype=np.complex128)
        for rail, ground, admittance in stamps:
            a, b = local.get(rail), local.get(ground)
            if a is not None:
                coupling[:, a, a] += admittance
            if b is not None:
                coupling[:, b, b] += admittance
            if a is not None and b is not None:
                coupling[:, a, b] -= admittance
                coupling[:, b, a] -= admittance
        return np.array([self._position[node] for node in touched], dtype=np.int64), coupling

    def sweep(self, additions):
        """The worst-port sweep with *additions* populated, as a multiport sweep."""
        positions, coupling = self._coupling(additions)
        impedances = self._base.copy()
        if positions.size:
            port_count = self._base.shape[1]
            green = self._transfer[:, port_count + positions][:, :, positions]
            node_side = self._node_side[:, positions, :]
            port_side = self._transfer[:, :port_count][:, :, positions]
            system = np.eye(positions.size)[None, :, :] + coupling @ green
            correction = np.linalg.solve(system, coupling @ node_side)
            impedances = impedances - np.einsum("fpm,fmp->fp", port_side, correction)
        results = {
            ref: ACSolver._summarize(
                self.frequencies, [complex(value) for value in impedances[:, index]],
                self.settings, self.network,
            )
            for index, ref in enumerate(self.ports)
        }
        worst_ref = max(results, key=lambda ref: results[ref].worst_impedance_ohm)
        worst = results[worst_ref]
        worst.per_port_results = results
        worst.worst_port_ref_des = worst_ref
        worst.excluded_ports = list(self.excluded)
        return worst


class ACSolver:
    def __init__(self, debug=False, log_callback=None, compute_settings=None):
        self.debug = debug
        self.log_callback = log_callback
        # Consulted only when CUDA is forced: every other sweep runs on the
        # recycled CPU factorisation (see _cuda_forced).
        self.compute_backend = SparseComputeBackend(compute_settings, log_callback)
        # Built on demand, and only to audit a GPU answer against a direct
        # factorisation. Exposed as an attribute so a test can inject one.
        self._cpu_reference_backend = None
        if np is None or scipy is None:
            raise ImportError("NumPy and SciPy are required for AC analysis.")

    def _log(self, message):
        if self.log_callback:
            self.log_callback(f"[AC SOLVER] {message}")

    def _cpu_reference(self):
        """A CPU-pinned backend used solely to check a GPU result."""
        if self._cpu_reference_backend is None:
            settings = getattr(self.compute_backend, "settings", None)
            cpu_settings = None
            if settings is not None:
                try:
                    cpu_settings = replace(settings, backend="CPU")
                except TypeError:
                    # A stand-in settings object (tests) is not a dataclass;
                    # a default CPU backend is still a valid reference.
                    cpu_settings = None
            self._cpu_reference_backend = SparseComputeBackend(
                cpu_settings, self.log_callback,
            )
        return self._cpu_reference_backend

    def _cuda_forced(self):
        """Whether this sweep runs on the GPU: only when CUDA is forced.

        AUTO used to send a sweep to CUDA above a node threshold. On P02 Rev E
        (85,337 nodes) the GPU's Jacobi-preconditioned BiCGSTAB ran out of
        iterations (status=5000) and the run fell back to one direct solve
        per point: 733 s for 121 points. The recycled factorisation beats
        the GPU even where the GPU converges: on a synthetic 94,000-node
        network built like P02's, 0.35 s a point against 2.3-3.7 s for
        BiCGSTAB and 3.9 s for GMRES preconditioned by the same LU on the
        GPU, whose triangular solves are the slow part. AUTO therefore keeps
        sweeps on the CPU.
        """
        settings = getattr(self.compute_backend, "settings", None)
        return str(getattr(settings, "backend", "") or "").upper() == "CUDA"

    def _solver_rtol(self):
        settings = getattr(self.compute_backend, "settings", None)
        return float(getattr(settings, "solver_rtol", 1.0e-8) or 1.0e-8)

    @staticmethod
    def _frequencies(settings):
        return np.logspace(
            np.log10(settings.frequency_start_hz),
            np.log10(settings.frequency_stop_hz),
            int(settings.frequency_points),
        )

    def _stamps(self, network, settings, capacitors, anchors):
        """The network's stamps: its branches, source and enabled capacitors."""
        source_resistance = max(float(settings.source.resistance_ohm), 1e-9)
        source_inductance = max(0.0, float(settings.source.inductance_h))
        lumped = [(
            network.source,
            lambda frequency: complex(
                source_resistance, 2.0 * np.pi * frequency * source_inductance,
            ),
        )]
        for capacitor in capacitors:
            if not capacitor.enabled or capacitor.capacitance_f <= 0:
                continue
            connection = network.capacitor_nodes.get(capacitor.ref_des)
            if connection is None:
                continue
            lumped.append((
                connection,
                lambda frequency, capacitor=capacitor: self.capacitor_impedance(
                    capacitor, frequency,
                ),
            ))
        # Plane-pair (cavity) capacitance is not a lumped element: it is one
        # admittance per overlapping cell, which _NetworkStamps stamps
        # vectorised from network.cavity_capacitors.
        return _NetworkStamps(network, lumped, anchors)

    def _log_recycling(self, recycled, points, started):
        if recycled.factorizations:
            self._log(
                f"{points} frequency point(s) solved with {recycled.factorizations} "
                f"LU factorisation(s) and {recycled.iterations} GMRES iteration(s) "
                f"in {time.perf_counter() - started:.1f} s."
            )

    @staticmethod
    def _measured_impedance(network, voltage):
        rail_voltage = np.mean([voltage[node] for node in network.measurement.rail_nodes])
        ground_voltage = np.mean([voltage[node] for node in network.measurement.ground_nodes])
        return complex(rail_voltage - ground_voltage)

    def _audit_gpu_against_cpu(self, network, matrix, current, gpu_voltage):
        """Compare a GPU solution with a CPU direct solve of the same system.

        Returns ``(trusted, note, cpu_voltage)``. ``trusted`` is False when the
        two disagree by more than :data:`GPU_ACCURACY_TOLERANCE`, in which case
        the caller must abandon the GPU for the rest of the sweep and keep the
        CPU answer for this point -- the direct factorisation is the reference,
        not the thing under test.

        An exception here means the audit itself could not run. That is not
        evidence against the GPU, so the sweep continues on it and the note
        records that the check was inconclusive.
        """
        try:
            reference = self._cpu_reference().solve(
                matrix, current, system_kind="GENERAL",
            )
        except Exception as exc:
            return True, f"not verified: CPU reference solve failed ({exc})", None
        cpu_voltage = reference.values
        gpu_impedance = self._measured_impedance(network, gpu_voltage)
        cpu_impedance = self._measured_impedance(network, cpu_voltage)
        scale = max(abs(cpu_impedance), 1.0e-30)
        deviation = abs(gpu_impedance - cpu_impedance) / scale
        if not np.isfinite(deviation) or deviation > GPU_ACCURACY_TOLERANCE:
            return (
                False,
                (
                    f"failed: GPU and CPU disagree by {deviation:.3g} relative "
                    f"(limit {GPU_ACCURACY_TOLERANCE:.0e}); the sweep continued on CPU"
                ),
                cpu_voltage,
            )
        return (
            True,
            (
                f"passed: GPU matches the CPU direct solve to {deviation:.3g} "
                f"relative (limit {GPU_ACCURACY_TOLERANCE:.0e})"
            ),
            cpu_voltage,
        )

    @staticmethod
    def capacitor_impedance(capacitor, frequency_hz):
        omega = 2.0 * np.pi * frequency_hz
        capacitance = max(float(capacitor.capacitance_f), 1e-18)
        return complex(capacitor.esr_ohm, omega * capacitor.esl_h - 1.0 / (omega * capacitance))

    @staticmethod
    def _connectivity(network, capacitors):
        """Connected components of the AC network.

        Returns (anchors, component_by_node, source_component). The anchors
        pin one node per floating island so the system stays non-singular;
        the component map lets any port be checked against the source's
        island, not just the primary measurement.
        """
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

        anchors = [source_anchor]
        anchors.extend(component[0] for index, component in enumerate(components)
                       if index != source_component)
        return anchors, component_by_node, source_component

    @classmethod
    def _port_disconnection(cls, network, connection, component_by_node, source_component):
        """Why *connection* cannot be measured, or None when it can be.

        Same test the primary measurement has always had, factored out so
        every swept port gets it rather than only the first one.
        """
        nodes = list(connection.rail_nodes) + list(connection.ground_nodes)
        if not connection.rail_nodes or not connection.ground_nodes:
            return "not connected to both the rail and the return mesh"
        if any(node < 0 or node >= network.node_count for node in nodes):
            return "contains an invalid node reference"
        if any(component_by_node[node] != source_component for node in nodes):
            return "sits on copper that is not electrically connected to the AC source"
        return None

    @classmethod
    def _topology_anchors(cls, network, capacitors):
        """Anchors, with the primary measurement validated as before."""
        anchors, component_by_node, source_component = cls._connectivity(network, capacitors)
        nodes = network.measurement.rail_nodes + network.measurement.ground_nodes
        if any(node < 0 or node >= network.node_count for node in nodes):
            raise ValueError("The measurement port contains an invalid node reference.")
        if any(component_by_node[node] != source_component for node in nodes):
            raise ValueError("The measurement port is not electrically connected to the AC source.")
        return anchors

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
        # Flat to the step's bandwidth, rising with frequency above it; below
        # a converter's switching frequency the rail's averaged step sets it
        # (ac_model.target_mask, ac_model.converter_band_floor).
        try:
            from .ac_model import target_mask
        except ImportError:
            from ac_model import target_mask
        bandwidth = max(0.0, float(getattr(settings, "transient_bandwidth_hz", 0.0) or 0.0))
        floor_hz = max(0.0, float(getattr(settings, "target_floor_hz", 0.0) or 0.0))
        below_floor = max(0.0, float(getattr(settings, "target_below_floor_ohm", 0.0) or 0.0))
        allowed = np.array([
            target_mask(target, bandwidth, float(frequency), floor_hz, below_floor)
            for frequency in frequencies
        ], dtype=float)
        # A worst case sitting on the last swept point means the impedance was
        # still climbing when the window closed: the real maximum may be
        # outside it, so the number is a lower bound rather than a maximum.
        worst_at_edge = bool(len(frequencies) > 1 and worst_index == len(frequencies) - 1)
        limit_hz = float(getattr(network, "quasi_static_limit_hz", 0.0) or 0.0)
        beyond = (
            int(np.count_nonzero(np.asarray(frequencies, dtype=float) > limit_hz))
            if limit_hz > 0.0 else 0
        )
        # A worst case pinned to the edge and still rising means the real
        # maximum lies beyond the window. The tail there is dominated by loop
        # inductance (|Z| ~ 2*pi*f*L), so linearly scaling the last point to
        # the target approximates where it would be crossed -- e.g. P02
        # Rev E's /VBUS_SYS: 0.2947 ohm at 100 MHz, target 0.39226 ohm ->
        # 0.39226/0.2947 x 100 MHz = 133 MHz crossing, inside its own
        # ~140 MHz quasi-static limit, so its PASS is not established (AC-1).
        crossing_hz = 0.0
        if worst_at_edge and target > 0.0 and worst_index > 0:
            last = float(magnitudes[worst_index])
            prior = float(magnitudes[worst_index - 1])
            if last > prior > 0.0:
                crossing_hz = float(frequencies[worst_index]) * (target / last)
            # Above the bandwidth the allowed impedance rises as fast as an
            # inductive tail does, so it is never crossed there.
            if bandwidth > 0.0 and crossing_hz > bandwidth:
                crossing_hz = 0.0
        return ImpedanceSweepResult(
            frequencies_hz=[float(value) for value in frequencies],
            impedance_ohm=list(impedances),
            target_impedance_ohm=target,
            target_bandwidth_hz=bandwidth,
            target_floor_hz=floor_hz if below_floor > 0.0 else 0.0,
            target_below_floor_ohm=below_floor if floor_hz > 0.0 else 0.0,
            worst_frequency_hz=float(frequencies[worst_index]),
            worst_impedance_ohm=float(magnitudes[worst_index]),
            meets_target=bool(target > 0 and np.all(magnitudes <= allowed)),
            mesh_node_count=int(network.node_count),
            requested_grid_size_mm=float(network.requested_grid_size_mm),
            effective_grid_size_mm=float(network.effective_grid_size_mm),
            track_summary=dict(getattr(network, "track_summary", None) or {}),
            mesh_engine=str(getattr(network, "mesh_engine", "") or "raster"),
            quasi_static_limit_hz=limit_hz,
            points_beyond_quasi_static=beyond,
            worst_at_sweep_edge=worst_at_edge,
            extrapolated_crossing_hz=crossing_hz,
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
        right-hand side changes. Every port is therefore solved against the
        same factorisation, which is itself carried across frequencies.

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
        anchors, component_by_node, source_component = self._connectivity(network, capacitors)

        # Every port gets the connectivity test the primary measurement has
        # always had. A port on a copper island that never reaches the source
        # would otherwise return a number with no physical meaning.
        usable = {}
        excluded = []
        for ref in sorted(ports):
            reason = self._port_disconnection(
                network, ports[ref], component_by_node, source_component,
            )
            if reason is None:
                usable[ref] = ports[ref]
            else:
                excluded.append({"ref_des": ref, "reason": reason})
                self._log(f"Port '{ref}' excluded: {reason}.")
        if not usable:
            detail = "; ".join(f"{item['ref_des']} {item['reason']}" for item in excluded)
            raise ValueError(
                "No measurement port is connected to the AC network"
                + (f" ({detail})." if detail else ".")
            )

        frequencies = self._frequencies(settings)
        sweep_started = time.perf_counter()
        stamps = self._stamps(network, settings, capacitors, anchors)
        order = list(usable)
        injections = [self._injection(network, usable[ref]) for ref in order]
        for current in injections:
            current[stamps.anchors] = 0.0
        recycled = _RecycledLUSolver(self._solver_rtol())
        on_gpu = self._cuda_forced()
        per_port = {ref: [] for ref in usable}
        solve_seconds = 0.0
        compute_samples = []

        for index, frequency in enumerate(frequencies):
            matrix = stamps.matrix(frequency)
            started = time.perf_counter()
            try:
                if on_gpu:
                    solved = self.compute_backend.solve_many(
                        matrix.tocsr(), injections,
                        system_kind="GENERAL", cache_key=("ac-multiport", id(network)),
                    )
                    voltages = [item.values for item in solved]
                    compute_samples.extend(item.metadata for item in solved)
                else:
                    voltages, metadata = recycled.solve(matrix, injections)
                    compute_samples.append(metadata)
            except Exception as exc:
                raise ValueError(f"AC solve failed at {frequency:g} Hz: {exc}") from exc
            for ref, voltage in zip(order, voltages):
                if not np.all(np.isfinite(voltage)):
                    raise ValueError(f"AC solve produced non-finite values at {frequency:g} Hz.")
                per_port[ref].append(self._port_voltage(usable[ref], voltage))
            solve_seconds += time.perf_counter() - started

            if progress_callback:
                progress_callback(index + 1, len(frequencies), float(frequency))
        self._log_recycling(recycled, len(frequencies), sweep_started)

        # Report the backend that actually ran. A forced-CUDA group raises
        # rather than falling back, so the last sample describes every port.
        results = {
            ref: self._summarize(
                frequencies, impedances, settings, network,
                compute_backend=compute_samples[-1].backend if compute_samples else "CPU",
                compute_device=compute_samples[-1].device if compute_samples else "CPU",
                compute_solve_seconds=solve_seconds / max(len(usable), 1),
                compute_transfer_seconds=sum(
                    item.transfer_seconds for item in compute_samples
                ) / max(len(usable), 1),
                compute_relative_residual=max(
                    (item.relative_residual for item in compute_samples), default=0.0
                ),
                compute_iterations=sum(item.iterations for item in compute_samples),
                compute_cache_hits=sum(bool(item.cache_hit) for item in compute_samples),
            )
            for ref, impedances in per_port.items()
        }
        worst_ref = max(results, key=lambda ref: results[ref].worst_impedance_ohm)
        worst = results[worst_ref]
        worst.per_port_results = results
        worst.worst_port_ref_des = worst_ref
        worst.excluded_ports = excluded
        if self.debug:
            self._log(
                f"Swept {len(usable)} port(s); worst is '{worst_ref}' at "
                f"{worst.worst_impedance_ohm:.6g} ohm."
            )
        return worst

    def candidate_impedances(self, network, settings, capacitors, candidates):
        """A :class:`CandidateImpedances` over *capacitors* for *candidates*.

        One factorisation per frequency serves every port and every node the
        covered candidates touch, as a single block of right-hand sides.
        """
        if settings.frequency_points < 2:
            raise ValueError("At least two frequency points are required.")
        capacitors = list(capacitors)
        anchors, component_by_node, source_component = self._connectivity(network, capacitors)
        ports = dict(getattr(network, "ports", None) or {}) or {"": network.measurement}
        usable, excluded = {}, []
        for ref in sorted(ports):
            reason = self._port_disconnection(
                network, ports[ref], component_by_node, source_component,
            )
            if reason is None:
                usable[ref] = ports[ref]
            else:
                excluded.append({"ref_des": ref, "reason": reason})
        if not usable:
            raise ValueError("No measurement port is connected to the AC network.")

        covered = set()
        for ref in candidates:
            connection = network.capacitor_nodes.get(ref)
            if connection is not None and self._port_disconnection(
                network, connection, component_by_node, source_component,
            ) is None:
                covered.add(ref)
        pinned = set(int(node) for node in anchors)
        nodes = sorted({
            int(node) for ref in covered
            for node in list(network.capacitor_nodes[ref].rail_nodes)
            + list(network.capacitor_nodes[ref].ground_nodes)
            if int(node) not in pinned
        })

        frequencies = self._frequencies(settings)
        started = time.perf_counter()
        stamps = self._stamps(network, settings, capacitors, anchors)
        weights = np.array([self._injection(network, usable[ref]) for ref in usable])
        block = np.zeros((network.node_count, len(usable) + len(nodes)), dtype=np.complex128)
        block[:, :len(usable)] = weights.T
        block[stamps.anchors, :len(usable)] = 0.0
        block[nodes, len(usable) + np.arange(len(nodes))] = 1.0
        base, node_side, transfer = [], [], []
        for frequency in frequencies:
            try:
                solution = self._solve_block(stamps.matrix(frequency), block)
            except Exception as exc:
                raise ValueError(f"AC solve failed at {frequency:g} Hz: {exc}") from exc
            # A port's voltage is its injection pattern applied to the node
            # voltages (anchored nodes sit at zero either way).
            at_ports = weights @ solution
            base.append(np.diagonal(at_ports[:, :len(usable)]))
            node_side.append(solution[nodes, :len(usable)])
            transfer.append(np.vstack([
                at_ports[:, len(usable):], solution[nodes, len(usable):],
            ]))
        self._log(
            f"{len(frequencies)} frequency point(s) solved for {len(usable)} port(s) "
            f"and {len(nodes)} candidate node(s) in {time.perf_counter() - started:.1f} s."
        )
        return CandidateImpedances(
            network, settings, frequencies, list(usable), excluded, nodes, pinned,
            np.array(base), np.array(node_side), np.array(transfer), covered,
        )

    def _solve_block(self, matrix, block):
        """Every column of *block* solved on one fresh factorisation of *matrix*."""
        rtol = self._solver_rtol()
        scale = np.maximum(np.linalg.norm(block, axis=0), 1.0e-30)
        for pivoting in (False, True):
            try:
                solution = _RecycledLUSolver._factorize(matrix, pivoting).solve(block)
            except RuntimeError:
                continue
            residual = np.linalg.norm(matrix @ solution - block, axis=0) / scale
            if np.all(np.isfinite(solution)) and float(residual.max()) <= rtol:
                return solution
        # Neither factorisation alone reached the tolerance: let GMRES refine.
        solutions, _metadata = _RecycledLUSolver(rtol).solve(matrix, list(block.T))
        return np.column_stack(solutions)

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
        frequencies = self._frequencies(settings)
        sweep_started = time.perf_counter()
        stamps = self._stamps(network, settings, capacitors, anchors)
        # Anchored nodes sit at zero volts, removing the arbitrary common-mode
        # potential of every island.
        current = self._injection(network, network.measurement)
        current[stamps.anchors] = 0.0
        recycled = _RecycledLUSolver(self._solver_rtol())
        on_gpu = self._cuda_forced()
        impedances = []
        compute_samples = []
        audited = False
        accuracy_note = ""

        for index, frequency in enumerate(frequencies):
            matrix = stamps.matrix(frequency)
            with warnings.catch_warnings():
                warnings.simplefilter("error", scipy.sparse.linalg.MatrixRankWarning)
                try:
                    if on_gpu:
                        solved = self.compute_backend.solve(
                            matrix.tocsr(), current, system_kind="GENERAL",
                            cache_key=("ac", id(network)), matrix_values_static=False,
                        )
                        voltage, metadata = solved.values, solved.metadata
                    else:
                        (voltage,), metadata = recycled.solve(matrix, [current])
                    if not audited and str(
                        getattr(metadata, "backend", "")
                    ).upper().startswith("CUDA"):
                        # First point that genuinely ran on the GPU: audit it
                        # against a direct factorisation before letting the
                        # remaining points inherit that trust.
                        audited = True
                        if getattr(
                            self.compute_backend.settings, "verify_gpu_accuracy", True
                        ):
                            trusted, accuracy_note, cpu_voltage = self._audit_gpu_against_cpu(
                                network, matrix.tocsr(), current, voltage,
                            )
                            self._log(f"GPU accuracy check {accuracy_note}.")
                            if not trusted:
                                # The direct solve is the reference: keep its
                                # answer here and drop to CPU for the rest.
                                if cpu_voltage is not None:
                                    voltage = cpu_voltage
                                on_gpu = False
                        else:
                            accuracy_note = "skipped: verification disabled"
                except Exception as exc:
                    raise ValueError(f"AC solve failed at {frequency:g} Hz: {exc}") from exc

            compute_samples.append(metadata)
            impedances.append(self._measured_impedance(network, voltage))

            if progress_callback:
                progress_callback(index + 1, len(frequencies), float(frequency))
        self._log_recycling(recycled, len(frequencies), sweep_started)

        if self.debug:
            magnitudes = np.abs(np.asarray(impedances))
            worst_index = int(np.argmax(magnitudes))
            self._log(
                f"Solved {len(frequencies)} points; worst |Z|={magnitudes[worst_index]:.6g} ohm "
                f"at {frequencies[worst_index]:.6g} Hz."
            )

        # Reuse the shared summary so a single-port sweep gets the same
        # validity bounds a multi-port one does; this path used to build the
        # result inline and silently omitted them.
        return self._summarize(
            frequencies, impedances, settings, network,
            compute_backend=self._effective_backend_name(compute_samples),
            compute_device=compute_samples[-1].device if compute_samples else "CPU",
            compute_solve_seconds=sum(item.solve_seconds for item in compute_samples),
            compute_transfer_seconds=sum(item.transfer_seconds for item in compute_samples),
            compute_relative_residual=max(
                (item.relative_residual for item in compute_samples), default=0.0
            ),
            compute_iterations=sum(item.iterations for item in compute_samples),
            compute_cache_hits=sum(bool(item.cache_hit) for item in compute_samples),
            gpu_accuracy_check=accuracy_note,
        )

    @staticmethod
    def _effective_backend_name(compute_samples):
        """Name the arithmetic that actually produced the sweep.

        A sweep that starts on the GPU and finishes on the CPU is neither, and
        reporting only the last point would hide the transition while
        reporting only the first would be a straight falsehood.
        """
        if not compute_samples:
            return "CPU"
        names = [str(item.backend) for item in compute_samples]
        first, last = names[0], names[-1]
        if first == last:
            return last
        return f"{first} -> {last}"
