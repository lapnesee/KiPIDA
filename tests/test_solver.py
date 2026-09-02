
import unittest
import sys
import os
from unittest.mock import patch

import numpy as np

# Inject Mock modules to run this standalone (without KiCad) if needed
# But for Solver, we mostly need numpy/scipy, which we assume are available in the env we run.
# The solver doesn't depend on pcbnew directly, only the Mesh object structure.

class MockMesh:
    def __init__(self):
        self.nodes = []
        self.edges = [] 

class TestSolver(unittest.TestCase):
    def setUp(self):
        # Ensure we can import solver
        plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if plugin_dir not in sys.path:
            sys.path.insert(0, plugin_dir)
            
        from solver import Solver
        self.solver_class = Solver

    def test_series_resistors(self):
        """
        Test 3 nodes in series:
        (Src=10V) --[R1=1ohm]-- (Node1) --[R2=1ohm]-- (Load=1A)
        
        Edges: (0, 1, G=1), (1, 2, G=1)
        Src at 0: 10V
        Load at 2: 1.0A
        
        Expected:
        Current I = 1A flows through entire string.
        V2 = ? 
        Voltage drop across R2 = I*R = 1*1 = 1V.
        Voltage drop across R1 = I*R = 1*1 = 1V.
        
        Wait, load at Node 2 sinks 1A.
        KCL Node 2: (V1-V2)*G = 1A -> V1-V2 = 1 -> V2 = V1-1
        KCL Node 1: (V0-V1)*G + (V2-V1)*G = 0 
                     (10-V1) + (V1-1-V1) = 0 ?? No
                     
        Let's solve by hand:
        V0 = 10
        I_path = 1A (Since load sinks 1A at end and no other path)
        V_drop_R1 = 1A * 1ohm = 1V => V1 = 9V
        V_drop_R2 = 1A * 1ohm = 1V => V2 = 8V
        """
        solver = self.solver_class()
        mesh = MockMesh()
        mesh.nodes = [0, 1, 2]
        # Conductance 1.0 = Resistance 1.0
        mesh.edges = [
            (0, 1, 1.0),
            (1, 2, 1.0)
        ]
        
        sources = [{'node_id': 0, 'voltage': 10.0}]
        loads = [{'node_id': 2, 'current': 1.0}]
        
        result = solver.solve(mesh, sources, loads)
        
        self.assertAlmostEqual(result[0], 10.0, places=5)
        self.assertAlmostEqual(result[1], 9.0, places=5)
        self.assertAlmostEqual(result[2], 8.0, places=5)

    def test_parallel_resistors(self):
        """
        (Src=10V) ----+----[R1=1]---- (Node1)
                      |
                      +----[R2=1]---- (Node1)
                      
        Wait, parallel resistors between same two nodes?
        (Src=10V @ 0) --[R1=1]-- (Node1)
                      --[R2=1]--
                      
        Edges: (0, 1, 1.0), (0, 1, 1.0). Total G = 2.0.
        Combined R = 0.5.
        
        Load 2A at Node 1.
        
        V_drop = I * R = 2A * 0.5ohm = 1V.
        V1 should be 9V.
        """
        solver = self.solver_class()
        mesh = MockMesh()
        mesh.nodes = [0, 1]
        mesh.edges = [
            (0, 1, 1.0),
            (0, 1, 1.0)
        ]
        
        sources = [{'node_id': 0, 'voltage': 10.0}]
        loads = [{'node_id': 1, 'current': 2.0}]
        
        result = solver.solve(mesh, sources, loads)
        
        self.assertAlmostEqual(result[0], 10.0, places=5)
        self.assertAlmostEqual(result[1], 9.0, places=5)

    def test_detailed_result_reports_branch_loss(self):
        from mesh import Mesh

        solver = self.solver_class()
        mesh = Mesh()
        mesh.nodes = [0, 1]
        mesh.node_coords = {0: (0.0, 0.0, 0), 1: (1.0, 0.0, 0)}
        mesh.add_edge_direct(0, 1, g=1.0)

        result = solver.solve_detailed(
            mesh,
            sources=[{"node_id": 0, "voltage": 10.0}],
            loads=[{"node_id": 1, "current": 1.0}],
        )

        self.assertAlmostEqual(result.voltages[1], 9.0, places=5)
        self.assertAlmostEqual(result.branch_currents_a[0], 1.0, places=5)
        self.assertAlmostEqual(result.branch_losses_w[0], 1.0, places=5)
        self.assertAlmostEqual(result.total_loss_w, 1.0, places=5)

    def test_floating_island_is_excluded_from_results_and_losses(self):
        from mesh import Mesh

        messages = []
        solver = self.solver_class(log_callback=messages.append)
        mesh = Mesh()
        mesh.nodes = [0, 1, 2, 3]
        mesh.node_coords = {
            0: (0.0, 0.0, 0),
            1: (1.0, 0.0, 0),
            2: (10.0, 0.0, 0),
            3: (11.0, 0.0, 0),
        }
        mesh.add_edge_direct(0, 1, g=1.0)
        mesh.add_edge_direct(2, 3, g=1.0)

        result = solver.solve_detailed(
            mesh,
            sources=[{"node_id": 0, "voltage": 5.0}],
            loads=[{"node_id": 1, "current": 1.0}],
        )

        self.assertEqual(set(result.voltages), {0, 1})
        self.assertAlmostEqual(result.voltages[1], 4.0, places=5)
        self.assertAlmostEqual(result.branch_currents_a[0], 1.0, places=5)
        self.assertAlmostEqual(result.branch_currents_a[1], 0.0, places=5)
        self.assertAlmostEqual(result.total_loss_w, 1.0, places=5)
        self.assertTrue(any("Ignoring floating island" in message for message in messages))

    def test_load_on_source_free_island_is_reported_and_excluded(self):
        from mesh import Mesh

        messages = []
        solver = self.solver_class(log_callback=messages.append)
        mesh = Mesh()
        mesh.nodes = [0, 1, 2, 3]
        mesh.node_coords = {
            0: (0.0, 0.0, 0),
            1: (1.0, 0.0, 0),
            2: (10.0, 0.0, 0),
            3: (11.0, 0.0, 0),
        }
        mesh.add_edge_direct(0, 1, g=1.0)
        mesh.add_edge_direct(2, 3, g=1.0)

        result = solver.solve(
            mesh,
            sources=[{"node_id": 0, "voltage": 5.0}],
            loads=[
                {"node_id": 1, "current": 1.0},
                {"node_id": 3, "current": 0.5, "ref_des": "U99"},
            ],
        )

        self.assertEqual(set(result), {0, 1})
        self.assertTrue(any("ERROR:" in message and "load node" in message for message in messages))

    def test_detailed_result_marks_excluded_load_island_invalid(self):
        from mesh import Mesh

        solver = self.solver_class()
        mesh = Mesh()
        mesh.nodes = [0, 1, 2, 3]
        mesh.node_coords = {node: (float(node), 0.0, 0) for node in mesh.nodes}
        mesh.add_edge_direct(0, 1, g=1.0)
        mesh.add_edge_direct(2, 3, g=1.0)

        result = solver.solve_detailed(
            mesh,
            sources=[{"node_id": 0, "voltage": 5.0}],
            loads=[
                {"node_id": 1, "current": 1.0},
                {"node_id": 3, "current": 0.5, "ref_des": "U99"},
            ],
        )

        self.assertFalse(result.valid)
        self.assertEqual(result.excluded_load_node_count, 1)
        self.assertEqual(result.excluded_load_references, ["U99"])
        self.assertTrue(result.warnings)
        self.assertAlmostEqual(result.branch_losses_w[1], 0.0, places=12)

    def test_detailed_vectorized_losses_match_temperature_scaled_resistance(self):
        from mesh import Mesh

        solver = self.solver_class()
        mesh = Mesh()
        mesh.nodes = [0, 1, 2]
        mesh.node_coords = {node: (float(node), 0.0, 0) for node in mesh.nodes}
        mesh.add_edge_direct(0, 1, g=1.0)
        mesh.add_edge_direct(1, 2, g=0.5)

        result = solver.solve_detailed(
            mesh,
            sources=[{"node_id": 0, "voltage": 10.0}],
            loads=[{"node_id": 2, "current": 1.0}],
            branch_resistance_scales=np.array([2.0, 0.5]),
            initial_voltages={0: 10.0, 1: 8.0, 2: 7.0},
        )

        np.testing.assert_allclose(result.branch_currents_a, [1.0, 1.0], rtol=1e-10)
        np.testing.assert_allclose(result.branch_losses_w, [2.0, 1.0], rtol=1e-10)
        self.assertAlmostEqual(result.total_loss_w, 3.0, places=10)

    def test_dc_constraints_preserve_symmetric_positive_definite_matrix(self):
        from compute_backend import ComputeMetadata, ComputeSolution

        solver = self.solver_class()
        mesh = MockMesh()
        mesh.nodes = [0, 1, 2]
        mesh.edges = [(0, 1, 2.0), (1, 2, 1.0)]
        captured = {}

        def solve_spd(matrix, rhs, system_kind, **kwargs):
            captured["matrix"] = matrix.copy()
            captured["kind"] = system_kind
            captured["initial_guess"] = kwargs.get("initial_guess")
            values = np.linalg.solve(matrix.toarray(), rhs)
            return ComputeSolution(values, ComputeMetadata("TEST", solver_method="CG"))

        with patch.object(solver.compute_backend, "solve", side_effect=solve_spd):
            result = solver.solve(
                mesh,
                sources=[{"node_id": 0, "voltage": 10.0}],
                loads=[{"node_id": 2, "current": 1.0}],
            )

        matrix = captured["matrix"].toarray()
        self.assertEqual(captured["kind"], "SPD")
        np.testing.assert_allclose(captured["initial_guess"], np.full(3, 10.0))
        np.testing.assert_allclose(matrix, matrix.T, rtol=0.0, atol=0.0)
        self.assertTrue(np.all(np.linalg.eigvalsh(matrix) > 0.0))
        self.assertAlmostEqual(result[0], 10.0, places=12)
        self.assertAlmostEqual(result[1], 9.5, places=12)
        self.assertAlmostEqual(result[2], 8.5, places=12)

    def test_multiple_voltage_sources_are_eliminated_exactly(self):
        solver = self.solver_class()
        mesh = MockMesh()
        mesh.nodes = [0, 1, 2]
        mesh.edges = [(0, 1, 1.0), (1, 2, 1.0)]

        result = solver.solve(
            mesh,
            sources=[
                {"node_id": 0, "voltage": 10.0},
                {"node_id": 2, "voltage": 4.0},
            ],
            loads=[],
        )

        self.assertAlmostEqual(result[0], 10.0, places=12)
        self.assertAlmostEqual(result[1], 7.0, places=12)
        self.assertAlmostEqual(result[2], 4.0, places=12)

    def test_conflicting_sources_on_same_node_are_rejected(self):
        solver = self.solver_class()
        mesh = MockMesh()
        mesh.nodes = [0, 1]
        mesh.edges = [(0, 1, 1.0)]

        with self.assertRaisesRegex(ValueError, "Conflicting source voltages"):
            solver.solve(
                mesh,
                sources=[
                    {"node_id": 0, "voltage": 5.0},
                    {"node_id": 0, "voltage": 3.3},
                ],
                loads=[],
            )

if __name__ == '__main__':
    unittest.main()
