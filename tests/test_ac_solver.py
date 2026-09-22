import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

import ac_solver
from ac_model import ACNetwork, ACNodeConnection
from ac_solver import ACSolver
from decoupling_optimizer import DecouplingOptimizer
from models import ACAnalysisSettings, ACSourceModel, CapacitorModel


HAS_NUMERICAL_DEPS = ac_solver.np is not None and ac_solver.scipy is not None


@unittest.skipUnless(HAS_NUMERICAL_DEPS, "NumPy/SciPy are not installed in this test interpreter")
class TestACSolver(unittest.TestCase):
    @staticmethod
    def network(node_count=2, measurement=None, capacitor_nodes=None):
        connection = ACNodeConnection([0], [1])
        return ACNetwork(
            node_count=node_count,
            branches=[],
            source=connection,
            measurement=measurement or connection,
            capacitor_nodes=capacitor_nodes or {},
        )

    @staticmethod
    def settings(**overrides):
        values = dict(
            rail_name="3V3",
            frequency_start_hz=1e3,
            frequency_stop_hz=1e5,
            frequency_points=3,
            target_impedance_ohm=0.11,
            source=ACSourceModel(resistance_ohm=0.1, inductance_h=0.0),
        )
        values.update(overrides)
        return ACAnalysisSettings(**values)

    def test_source_impedance_is_recovered_by_one_ampere_injection(self):
        network = self.network()
        network.requested_grid_size_mm = 0.5
        network.effective_grid_size_mm = 0.62
        result = ACSolver().solve_sweep(network, self.settings())

        for impedance in result.impedance_ohm:
            self.assertAlmostEqual(abs(impedance), 0.1, places=9)
        self.assertTrue(result.meets_target)
        self.assertEqual(result.mesh_node_count, 2)
        self.assertAlmostEqual(result.requested_grid_size_mm, 0.5)
        self.assertAlmostEqual(result.effective_grid_size_mm, 0.62)

    def test_rlc_capacitor_resonance_approaches_esr(self):
        capacitor = CapacitorModel(
            ref_des="C1", capacitance_f=1e-6, esr_ohm=0.02, esl_h=1e-9,
        )
        network = self.network(capacitor_nodes={"C1": ACNodeConnection([0], [1])})
        resonance_hz = 1.0 / (2.0 * ac_solver.np.pi * (1e-6 * 1e-9) ** 0.5)
        settings = self.settings(
            frequency_start_hz=resonance_hz * 0.999,
            frequency_stop_hz=resonance_hz * 1.001,
            frequency_points=3,
            source=ACSourceModel(resistance_ohm=1000.0, inductance_h=0.0),
            capacitors=[capacitor],
        )

        result = ACSolver().solve_sweep(network, settings)

        self.assertAlmostEqual(abs(result.impedance_ohm[1]), 0.02, delta=5e-5)

    def test_disconnected_measurement_port_is_rejected(self):
        network = self.network(
            node_count=4,
            measurement=ACNodeConnection([2], [3]),
        )
        with self.assertRaisesRegex(ValueError, "not electrically connected"):
            ACSolver().solve_sweep(network, self.settings())

    def test_auto_sweeps_on_the_cpu_without_trying_the_gpu(self):
        # P02 Rev E, 85,337 nodes: AUTO sent the sweep to CUDA, whose BiCGSTAB
        # stopped at status=5000 before the run fell back to one direct solve
        # per point -- 733 s for 121 points.
        class RecordingBackend:
            def __init__(self):
                self.settings = SimpleNamespace(backend="AUTO")
                self.calls = 0

            def solve(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("AUTO reached the GPU")

        solver = ACSolver()
        solver.compute_backend = RecordingBackend()

        result = solver.solve_sweep(self.network(), self.settings())

        self.assertEqual(solver.compute_backend.calls, 0)
        self.assertEqual(result.compute_backend, ac_solver.RECYCLED_LU_BACKEND)
        for impedance in result.impedance_ohm:
            self.assertAlmostEqual(abs(impedance), 0.1, places=9)

    def test_a_reused_factorisation_matches_a_direct_solve_at_every_point(self):
        # The sweep carries one LU across frequencies and anchors nodes by row
        # and column; the reference is the historical formulation, a direct
        # solve per point with each anchor's row replaced by the identity.
        np = ac_solver.np
        from models import MeshBranch

        size = 8
        node = {(layer, i, j): (layer * size + i) * size + j
                for layer in (0, 1) for i in range(size) for j in range(size)}
        branches = [
            MeshBranch(node[(layer, i, j)], node[(layer, i + di, j + dj)],
                       resistance_ohm=1.1e-3, inductance_h=0.12e-9)
            for (layer, i, j) in node for di, dj in ((1, 0), (0, 1))
            if i + di < size and j + dj < size
        ]
        pair = lambda i, j: ACNodeConnection([node[(0, i, j)]], [node[(1, i, j)]])
        capacitors = [
            CapacitorModel(ref_des=f"C{k}", capacitance_f=value, esr_ohm=0.01, esl_h=0.5e-9)
            for k, value in enumerate((10e-6, 1e-6, 100e-9, 10e-9))
        ]
        network = ACNetwork(
            node_count=len(node), branches=branches, source=pair(0, 0),
            measurement=pair(size - 1, size - 1),
            capacitor_nodes={cap.ref_des: pair(k * 2 + 1, size - 2)
                             for k, cap in enumerate(capacitors)},
        )
        settings = self.settings(
            frequency_start_hz=1e3, frequency_stop_hz=1e9, frequency_points=61,
            source=ACSourceModel(resistance_ohm=5e-3, inductance_h=2e-9),
            capacitors=capacitors,
        )

        with mock.patch.object(ac_solver.scipy.sparse.linalg, "splu",
                               wraps=ac_solver.scipy.sparse.linalg.splu) as splu:
            result = ACSolver().solve_sweep(network, settings)

        anchors = ACSolver._topology_anchors(network, capacitors)
        elements = [(b.node_a, b.node_b, lambda f, b=b: complex(
            b.resistance_ohm, 2 * np.pi * f * b.inductance_h)) for b in branches]
        elements.append((node[(0, 0, 0)], node[(1, 0, 0)],
                         lambda f: complex(5e-3, 2 * np.pi * f * 2e-9)))
        elements += [(network.capacitor_nodes[cap.ref_des].rail_nodes[0],
                      network.capacitor_nodes[cap.ref_des].ground_nodes[0],
                      lambda f, cap=cap: ACSolver.capacitor_impedance(cap, f))
                     for cap in capacitors]
        for frequency, impedance in zip(result.frequencies_hz, result.impedance_ohm):
            matrix = np.zeros((len(node), len(node)), dtype=complex)
            for a, b, element in elements:
                y = 1.0 / element(frequency)
                matrix[a, a] += y
                matrix[b, b] += y
                matrix[a, b] -= y
                matrix[b, a] -= y
            current = np.zeros(len(node), dtype=complex)
            current[node[(0, size - 1, size - 1)]] = 1.0
            current[node[(1, size - 1, size - 1)]] = -1.0
            for anchor in anchors:
                matrix[anchor, :] = 0.0
                matrix[anchor, anchor] = 1.0
                current[anchor] = 0.0
            voltage = np.linalg.solve(matrix, current)
            expected = voltage[node[(0, size - 1, size - 1)]] - voltage[node[(1, size - 1, size - 1)]]
            self.assertLess(abs(impedance - expected) / abs(expected),
                            ac_solver.GPU_ACCURACY_TOLERANCE, f"{frequency:g} Hz")
        self.assertLess(splu.call_count, 61 // 4, "the factorisation was not reused")
        self.assertEqual(result.compute_backend, ac_solver.RECYCLED_LU_BACKEND)

    def test_optimizer_populates_existing_candidate(self):
        candidate = CapacitorModel(
            ref_des="C_DNP", capacitance_f=100e-9, esr_ohm=0.01,
            esl_h=0.0, enabled=False, candidate=True,
        )
        network = self.network(capacitor_nodes={"C_DNP": ACNodeConnection([0], [1])})
        settings = self.settings(
            frequency_start_hz=100.0,
            frequency_stop_hz=200.0,
            frequency_points=5,
            target_impedance_ohm=0.1,
            source=ACSourceModel(resistance_ohm=0.5, inductance_h=0.0),
            capacitors=[candidate],
            optimizer_values_f=[0.1],
            optimizer_max_additions=1,
        )

        result = DecouplingOptimizer(ACSolver()).optimize(network, settings)

        self.assertEqual([item.ref_des for item in result.recommendations], ["C_DNP"])
        self.assertLess(result.optimized.worst_impedance_ohm, result.baseline.worst_impedance_ohm)
        self.assertTrue(result.reached_target)


if __name__ == "__main__":
    unittest.main()
