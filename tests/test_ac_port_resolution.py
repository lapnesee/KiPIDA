"""AC port resolution: geometric return, automatic source, multi-port sweep.

Regression origin: on p02_alimentation every AC run failed with

    AC Analysis Error: The AC source must map to pads on both the rail
    and the return net.

because +5V_RAIL is fed by a regulator (no UnifiedSource) whose output is an
inductor with no ground pad, and the return was looked up on that same
component.
"""

import os
import sys
import time
import unittest
from types import SimpleNamespace

plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

from ac_model import (
    EPT_POWER_INPUT, EPT_POWER_OUTPUT, EPT_UNKNOWN,
    ACModelBuilder, nearest_ground_nodes, pad_electrical_type,
)
from models import (
    ACAnalysisSettings, ACMeasurementPort, ACSourceModel, ComponentRef,
    PowerRail, UnifiedLoad, UnifiedSource, VoltageRegulator,
)


def pad(number, net_name, x=0.0, y=0.0):
    return SimpleNamespace(
        number=number,
        net=SimpleNamespace(name=net_name),
        position=SimpleNamespace(x=x, y=y),
    )


def typed_pad(number, net_name, electrical_type, x=0.0, y=0.0):
    """A pad carrying a schematic pin type, shaped like kipy's Pad.proto."""
    item = pad(number, net_name, x, y)
    item.proto = SimpleNamespace(
        symbol_pin=SimpleNamespace(name="", type=electrical_type, no_connect=False),
    )
    return item


class FakeMesh:
    """Minimal stand-in for mesh.Mesh with the attributes ports need."""

    def __init__(self, coords, grid_step=1.0, grid_origin=(0.0, 0.0)):
        self.node_coords = dict(coords)
        self.nodes = sorted(self.node_coords)
        self.grid_step = grid_step
        self.grid_origin = grid_origin
        self.node_map = {}
        for node_id, (x, y, layer) in self.node_coords.items():
            ix = int(round((x - grid_origin[0]) / grid_step))
            iy = int(round((y - grid_origin[1]) / grid_step))
            self.node_map[(ix, iy, layer)] = node_id


class NearestGroundNodeTests(unittest.TestCase):
    def test_picks_the_closest_ground_node_under_the_rail_pad(self):
        rail = FakeMesh({0: (10.0, 10.0, 0)})
        ground = FakeMesh({
            0: (0.0, 0.0, 1),
            1: (10.2, 10.1, 1),      # closest
            2: (50.0, 50.0, 1),
        })
        self.assertEqual(nearest_ground_nodes(ground, rail, [0]), [1])

    def test_empty_ground_mesh_returns_nothing(self):
        rail = FakeMesh({0: (1.0, 1.0, 0)})
        self.assertEqual(nearest_ground_nodes(FakeMesh({}), rail, [0]), [])

    def test_max_distance_rejects_a_far_plane(self):
        rail = FakeMesh({0: (0.0, 0.0, 0)})
        ground = FakeMesh({0: (100.0, 100.0, 1)})
        self.assertEqual(
            nearest_ground_nodes(ground, rail, [0], max_distance_mm=1.0), [],
        )

    def test_twenty_thousand_ground_nodes_resolve_quickly(self):
        # Guards the spatial index: a naive O(N*M) scan over a real ground
        # plane made model build time unacceptable.
        ground = FakeMesh(
            {i: (float(i % 200), float(i // 200), 1) for i in range(20000)},
            grid_step=1.0,
        )
        rail = FakeMesh({i: (float(i % 50) + 0.25, float(i) * 0.01, 0) for i in range(50)})
        started = time.perf_counter()
        resolved = nearest_ground_nodes(ground, rail, list(range(50)))
        elapsed = time.perf_counter() - started
        self.assertTrue(resolved)
        self.assertLess(elapsed, 1.0, f"nearest_ground_nodes took {elapsed:.3f}s")


class SourceGroundFallbackTests(unittest.TestCase):
    """The reported bug: an inductor source with no ground pad must still map."""

    def _builder_and_meshes(self):
        # L1 sits on the rail only -- exactly a buck output inductor.
        inductor = SimpleNamespace(
            reference="L1", value="2u2",
            pads=[pad("1", "SW", 5.0, 5.0), pad("2", "+5V_RAIL", 6.0, 5.0)],
        )
        load = SimpleNamespace(
            reference="U2", value="MCU",
            pads=[pad("1", "+5V_RAIL", 20.0, 5.0), pad("2", "GND", 20.0, 6.0)],
        )
        builder = ACModelBuilder(SimpleNamespace(footprints=[inductor, load]))
        rail_mesh = FakeMesh({0: (6.0, 5.0, 0), 1: (20.0, 5.0, 0)})
        ground_mesh = FakeMesh({0: (6.0, 5.2, 1), 1: (20.0, 6.0, 1)})
        return builder, rail_mesh, ground_mesh

    def test_inductor_without_ground_pad_maps_via_the_plane(self):
        builder, rail_mesh, ground_mesh = self._builder_and_meshes()
        settings = ACAnalysisSettings(rail_name="+5V_RAIL", ground_net_name="GND")

        connection = builder._connection(
            rail_mesh, ground_mesh, len(rail_mesh.nodes),
            "L1", ["2"], [], settings, label="AC source",
        )

        self.assertTrue(connection.rail_nodes, "rail pad must map")
        self.assertTrue(
            connection.ground_nodes,
            "an inductor with no GND pad must still get a return via the plane",
        )
        # Ground ids are offset into the combined network.
        self.assertTrue(all(node >= len(rail_mesh.nodes) for node in connection.ground_nodes))

    def test_component_with_its_own_ground_pad_still_uses_it(self):
        builder, rail_mesh, ground_mesh = self._builder_and_meshes()
        settings = ACAnalysisSettings(rail_name="+5V_RAIL", ground_net_name="GND")

        connection = builder._connection(
            rail_mesh, ground_mesh, len(rail_mesh.nodes),
            "U2", ["1"], ["2"], settings, label="measurement port",
        )

        self.assertTrue(connection.rail_nodes)
        self.assertTrue(connection.ground_nodes)


class SourceResolutionTests(unittest.TestCase):
    def _builder(self):
        inductor = SimpleNamespace(
            reference="L1", value="2u2",
            pads=[pad("1", "SW"), pad("2", "+5V_RAIL")],
        )
        regulator_ic = SimpleNamespace(
            reference="U4", value="TPS62",
            pads=[pad("1", "VBUS"), pad("2", "SW"), pad("3", "GND")],
        )
        load = SimpleNamespace(
            reference="J6", value="Conn",
            pads=[pad("5", "+5V_RAIL"), pad("7", "GND")],
        )
        return ACModelBuilder(SimpleNamespace(
            footprints=[inductor, regulator_ic, load],
        ))

    def test_regulator_output_is_used_when_the_rail_has_no_source(self):
        # p02_alimentation's +5V_RAIL: sources=[] and fed by a regulator whose
        # output lives on another rail's child_regulators list.
        rail = PowerRail(net_name="+5V_RAIL", nominal_voltage=5.0)
        upstream = PowerRail(net_name="VBUS", nominal_voltage=12.0)
        upstream.child_regulators.append(VoltageRegulator(
            name="Buck 1",
            input_rail_name="VBUS", input_ref_des="U4", input_pad_names=["1"],
            output_rail_name="+5V_RAIL", output_ref_des="L1", output_pad_names=["2"],
        ))
        settings = ACAnalysisSettings(rail_name="+5V_RAIL", ground_net_name="GND")

        ref_des, pads, rule = self._builder().resolve_source(
            rail, settings, all_rails=[upstream, rail],
        )

        self.assertEqual(ref_des, "L1")
        self.assertEqual(pads, ["2"])
        self.assertIn("regulator", rule)

    def test_explicit_setting_wins_over_everything(self):
        rail = PowerRail(net_name="+5V_RAIL")
        rail.sources.append(UnifiedSource(ComponentRef("U4"), ["1"]))
        settings = ACAnalysisSettings(
            rail_name="+5V_RAIL",
            source=ACSourceModel(ref_des="J6", rail_pad_names=["5"]),
        )

        ref_des, _pads, rule = self._builder().resolve_source(rail, settings)

        self.assertEqual(ref_des, "J6")
        self.assertEqual(rule, "explicit")

    def test_heuristic_prefers_an_inductor_over_a_connector(self):
        rail = PowerRail(net_name="+5V_RAIL")
        settings = ACAnalysisSettings(rail_name="+5V_RAIL")

        ref_des, _pads, rule = self._builder().resolve_source(rail, settings, all_rails=[rail])

        self.assertEqual(ref_des, "L1", "L/FB outranks other rail components")
        self.assertIn("heuristic", rule)

    def test_rail_with_nothing_on_it_reports_what_to_do(self):
        rail = PowerRail(net_name="+12V_ABSENT")
        settings = ACAnalysisSettings(rail_name="+12V_ABSENT")
        builder = self._builder()

        ref_des, _pads, rule = builder.resolve_source(rail, settings, all_rails=[rail])
        self.assertEqual(ref_des, "")
        self.assertEqual(rule, "unresolved")

        message = builder._unmapped_message("AC source", ref_des, settings)
        self.assertIn("+12V_ABSENT", message)
        self.assertIn("Power Tree", message)


class PinTypeSourceTests(unittest.TestCase):
    """Schematic pin type beats guessing from a reference designator."""

    def _builder(self, footprints):
        return ACModelBuilder(SimpleNamespace(footprints=footprints))

    def test_power_output_pin_outranks_the_naming_heuristic(self):
        # L1 would win on name alone; U4's declared power output must win
        # instead, because that is a fact off the board rather than a guess.
        inductor = SimpleNamespace(
            reference="L1", value="2u2",
            pads=[pad("1", "SW"), pad("2", "+5V_RAIL")],
        )
        regulator = SimpleNamespace(
            reference="U4", value="TPS62",
            pads=[typed_pad("5", "+5V_RAIL", EPT_POWER_OUTPUT)],
        )
        rail = PowerRail(net_name="+5V_RAIL")
        settings = ACAnalysisSettings(rail_name="+5V_RAIL")

        ref_des, pads, rule = self._builder([inductor, regulator]).resolve_source(
            rail, settings, all_rails=[rail],
        )

        self.assertEqual(ref_des, "U4")
        self.assertEqual(pads, ["5"])
        self.assertEqual(rule, "pin-type:power_output")

    def test_power_input_is_never_chosen_as_a_source(self):
        # A connector declaring a power input is a load; with nothing else on
        # the rail there is no source to report, and saying so beats returning
        # the load as if it supplied the net.
        connector = SimpleNamespace(
            reference="J6", value="Conn",
            pads=[typed_pad("5", "+5V_RAIL", EPT_POWER_INPUT)],
        )
        rail = PowerRail(net_name="+5V_RAIL")
        settings = ACAnalysisSettings(rail_name="+5V_RAIL")

        ref_des, _pads, rule = self._builder([connector]).resolve_source(
            rail, settings, all_rails=[rail],
        )

        self.assertEqual(ref_des, "")
        self.assertIn("power input", rule)

    def test_power_input_is_excluded_leaving_the_heuristic_the_rest(self):
        inductor = SimpleNamespace(
            reference="L1", value="2u2", pads=[pad("2", "+5V_RAIL")],
        )
        connector = SimpleNamespace(
            reference="J6", value="Conn",
            pads=[typed_pad("5", "+5V_RAIL", EPT_POWER_INPUT)],
        )
        rail = PowerRail(net_name="+5V_RAIL")
        settings = ACAnalysisSettings(rail_name="+5V_RAIL")

        ref_des, _pads, rule = self._builder([connector, inductor]).resolve_source(
            rail, settings, all_rails=[rail],
        )

        self.assertEqual(ref_des, "L1")
        self.assertIn("excluded 1 power-input", rule)

    def test_board_without_pin_metadata_degrades_to_the_heuristic(self):
        # Synthetic pads carry no `proto`; that must not raise, it must simply
        # leave the earlier behaviour intact.
        self.assertIsNone(pad_electrical_type(pad("1", "+5V_RAIL")))

        inductor = SimpleNamespace(
            reference="L1", value="2u2", pads=[pad("2", "+5V_RAIL")],
        )
        rail = PowerRail(net_name="+5V_RAIL")
        settings = ACAnalysisSettings(rail_name="+5V_RAIL")

        ref_des, _pads, rule = self._builder([inductor]).resolve_source(
            rail, settings, all_rails=[rail],
        )

        self.assertEqual(ref_des, "L1")
        self.assertIn("heuristic", rule)

    def test_unset_enum_reads_as_no_information(self):
        # An unset protobuf enum is 0 (EPT_UNKNOWN), which is absence of data,
        # not a pin type -- it must not be treated as a usable answer.
        self.assertIsNone(pad_electrical_type(typed_pad("1", "N", EPT_UNKNOWN)))
        self.assertEqual(
            pad_electrical_type(typed_pad("1", "N", EPT_POWER_OUTPUT)), EPT_POWER_OUTPUT,
        )


class PortResolutionTests(unittest.TestCase):
    def test_every_load_becomes_a_candidate_port(self):
        rail = PowerRail(net_name="+5V_RAIL")
        rail.loads.append(UnifiedLoad(ComponentRef("J6"), 3.8, ["5", "6"]))
        rail.loads.append(UnifiedLoad(ComponentRef("U7"), 0.001, ["12"]))
        settings = ACAnalysisSettings(rail_name="+5V_RAIL")

        ports = ACModelBuilder(SimpleNamespace(footprints=[])).resolve_ports(rail, settings)

        self.assertEqual([ref for ref, _pads in ports], ["J6", "U7"])

    def test_explicit_port_suppresses_the_sweep(self):
        rail = PowerRail(net_name="+5V_RAIL")
        rail.loads.append(UnifiedLoad(ComponentRef("J6"), 3.8, ["5"]))
        settings = ACAnalysisSettings(
            rail_name="+5V_RAIL",
            measurement_port=ACMeasurementPort(ref_des="U7", rail_pad_names=["12"]),
        )

        ports = ACModelBuilder(SimpleNamespace(footprints=[])).resolve_ports(rail, settings)

        self.assertEqual(ports, [("U7", ["12"])])


class MultiPortSweepTests(unittest.TestCase):
    """Sweeping every port replaces making the user try combinations."""

    def setUp(self):
        try:
            import ac_solver
        except ImportError:  # pragma: no cover
            self.skipTest("solver dependencies unavailable")
        if ac_solver.np is None or ac_solver.scipy is None:
            self.skipTest("NumPy/SciPy are not installed in this test interpreter")

    @staticmethod
    def _network_two_ports():
        """Source at node 0/1; a near port and a port behind a resistor."""
        from ac_model import ACNetwork, ACNodeConnection
        from models import MeshBranch

        near = ACNodeConnection([0], [1])
        far = ACNodeConnection([2], [1])
        return ACNetwork(
            node_count=3,
            # 1 ohm between the rail node and the far port's rail node.
            branches=[MeshBranch(node_a=0, node_b=2, resistance_ohm=1.0)],
            source=near,
            measurement=near,
            capacitor_nodes={},
            ports={"NEAR": near, "FAR": far},
        )

    @staticmethod
    def _settings():
        from models import ACAnalysisSettings, ACSourceModel
        return ACAnalysisSettings(
            rail_name="3V3", frequency_start_hz=1e3, frequency_stop_hz=1e5,
            frequency_points=3, target_impedance_ohm=0.11,
            source=ACSourceModel(resistance_ohm=0.1, inductance_h=0.0),
        )

    def test_worst_case_is_the_far_port(self):
        from ac_solver import ACSolver

        result = ACSolver().solve_sweep_multiport(
            self._network_two_ports(), self._settings(),
        )

        self.assertEqual(set(result.per_port_results), {"NEAR", "FAR"})
        self.assertEqual(result.worst_port_ref_des, "FAR")
        near = result.per_port_results["NEAR"].worst_impedance_ohm
        far = result.per_port_results["FAR"].worst_impedance_ohm
        # Near port sees the 0.1 ohm source; far port adds the 1 ohm branch.
        self.assertAlmostEqual(near, 0.1, places=6)
        self.assertAlmostEqual(far, 1.1, places=6)
        self.assertEqual(result.worst_impedance_ohm, far)

    def test_single_port_matches_the_historical_solve(self):
        from ac_model import ACNodeConnection
        from ac_solver import ACSolver

        network = self._network_two_ports()
        network.ports = {"NEAR": ACNodeConnection([0], [1])}
        settings = self._settings()

        single = ACSolver().solve_sweep(network, settings)
        swept = ACSolver().solve_sweep_multiport(network, settings)

        self.assertEqual(
            [abs(z) for z in single.impedance_ohm],
            [abs(z) for z in swept.impedance_ohm],
        )
        self.assertEqual(swept.worst_port_ref_des, "NEAR")


class DisconnectedPortTests(unittest.TestCase):
    """A port on isolated copper must be reported, never silently measured."""

    def setUp(self):
        try:
            import ac_solver
        except ImportError:  # pragma: no cover
            self.skipTest("solver dependencies unavailable")
        if ac_solver.np is None or ac_solver.scipy is None:
            self.skipTest("NumPy/SciPy are not installed in this test interpreter")

    @staticmethod
    def _network_with_island():
        """NEAR reaches the source; ISLAND sits on copper joined to nothing."""
        from ac_model import ACNetwork, ACNodeConnection
        from models import MeshBranch

        near = ACNodeConnection([0], [1])
        island = ACNodeConnection([2], [3])
        return ACNetwork(
            node_count=4,
            branches=[MeshBranch(node_a=2, node_b=3, resistance_ohm=1.0)],
            source=near,
            measurement=near,
            capacitor_nodes={},
            ports={"NEAR": near, "ISLAND": island},
        )

    def test_island_port_is_excluded_with_a_reason_and_the_sweep_survives(self):
        from ac_solver import ACSolver

        result = ACSolver().solve_sweep_multiport(
            self._network_with_island(), MultiPortSweepTests._settings(),
        )

        self.assertEqual(set(result.per_port_results), {"NEAR"})
        self.assertEqual(
            [item["ref_des"] for item in result.excluded_ports], ["ISLAND"],
        )
        self.assertIn("not electrically connected", result.excluded_ports[0]["reason"])

    def test_exclusion_reaches_the_report_as_a_limitation(self):
        from analysis_adapters import adapt_ac_result
        from ac_solver import ACSolver

        result = ACSolver().solve_sweep_multiport(
            self._network_with_island(), MultiPortSweepTests._settings(),
        )
        adapted = adapt_ac_result(result, settings=MultiPortSweepTests._settings())

        self.assertTrue(
            any("ISLAND" in text for text in adapted.limitations),
            "a dropped observation point must be visible in the report",
        )

    def test_disconnected_primary_measurement_still_raises(self):
        # Non-regression: the single-port path keeps failing loudly.
        from ac_model import ACNodeConnection
        from ac_solver import ACSolver

        network = self._network_with_island()
        network.measurement = ACNodeConnection([2], [3])

        with self.assertRaises(ValueError):
            ACSolver().solve_sweep(network, MultiPortSweepTests._settings())


class SolveManyTests(unittest.TestCase):
    """Multi-RHS must be an efficiency change, never a physics change."""

    def setUp(self):
        try:
            import numpy, scipy.sparse  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("NumPy/SciPy are not installed in this test interpreter")

    @staticmethod
    def _system():
        import numpy as np
        import scipy.sparse

        # A small SPD matrix and three unrelated right-hand sides.
        matrix = scipy.sparse.csr_matrix(np.array([
            [4.0, -1.0, 0.0],
            [-1.0, 4.0, -1.0],
            [0.0, -1.0, 4.0],
        ]))
        rhs = [
            np.array([1.0, 0.0, 0.0]),
            np.array([0.0, 1.0, 0.0]),
            np.array([0.5, -0.25, 2.0]),
        ]
        return matrix, rhs

    def test_matches_individual_solves(self):
        import numpy as np
        from compute_backend import SparseComputeBackend
        from runtime_config import RuntimeComputeSettings

        matrix, rhs = self._system()
        backend = SparseComputeBackend(RuntimeComputeSettings(backend="CPU"))

        grouped = backend.solve_many(matrix, rhs, system_kind="SPD")
        individually = [backend.solve(matrix, item, system_kind="SPD") for item in rhs]

        self.assertEqual(len(grouped), len(rhs))
        for many, one in zip(grouped, individually):
            np.testing.assert_allclose(many.values, one.values, rtol=1e-9, atol=1e-12)

    def test_solutions_actually_satisfy_the_system(self):
        import numpy as np
        from compute_backend import SparseComputeBackend
        from runtime_config import RuntimeComputeSettings

        matrix, rhs = self._system()
        backend = SparseComputeBackend(RuntimeComputeSettings(backend="CPU"))

        for solved, expected in zip(backend.solve_many(matrix, rhs), rhs):
            np.testing.assert_allclose(matrix.dot(solved.values), expected, atol=1e-10)

    def test_complex_right_hand_sides_are_supported(self):
        # The AC matrix is complex; a real-only path would silently truncate.
        import numpy as np
        import scipy.sparse
        from compute_backend import SparseComputeBackend
        from runtime_config import RuntimeComputeSettings

        matrix = scipy.sparse.csr_matrix(
            np.array([[2.0 + 1.0j, -1.0], [-1.0, 2.0 - 1.0j]])
        )
        rhs = [np.array([1.0 + 0.0j, 0.0]), np.array([0.0, 1.0 + 1.0j])]
        backend = SparseComputeBackend(RuntimeComputeSettings(backend="CPU"))

        for solved, expected in zip(backend.solve_many(matrix, rhs), rhs):
            self.assertTrue(np.iscomplexobj(solved.values))
            np.testing.assert_allclose(matrix.dot(solved.values), expected, atol=1e-10)

    def test_empty_input_is_not_an_error(self):
        from compute_backend import SparseComputeBackend
        from runtime_config import RuntimeComputeSettings

        matrix, _rhs = self._system()
        backend = SparseComputeBackend(RuntimeComputeSettings(backend="CPU"))
        self.assertEqual(backend.solve_many(matrix, []), [])

    def test_cuda_group_matches_cpu_when_a_gpu_is_present(self):
        # Skips wherever CuPy is absent, which includes this development venv.
        # It runs inside KiCad's interpreter, where the GPU the multi-port
        # path exists for actually lives. Simulating a GPU here would assert
        # nothing about the code that runs there.
        import numpy as np
        from compute_backend import SparseComputeBackend, cuda_diagnostics
        from runtime_config import RuntimeComputeSettings

        if not cuda_diagnostics()["available"]:
            self.skipTest("no CUDA device available in this interpreter")

        matrix, rhs = self._system()
        reference = SparseComputeBackend(
            RuntimeComputeSettings(backend="CPU")
        ).solve_many(matrix, rhs, system_kind="SPD")
        gpu = SparseComputeBackend(
            RuntimeComputeSettings(backend="CUDA", cuda_min_nodes=0)
        ).solve_many(matrix, rhs, system_kind="SPD", cache_key=("test", 1))

        for on_gpu, on_cpu in zip(gpu, reference):
            np.testing.assert_allclose(on_gpu.values, on_cpu.values, rtol=1e-6, atol=1e-9)
        self.assertTrue(gpu[0].metadata.backend.startswith("CUDA"))
        # The point of the exercise: the matrix is uploaded and preconditioned
        # for the first RHS only, then reused for the rest.
        self.assertTrue(any(item.metadata.matrix_reused for item in gpu[1:]))

    def test_multiport_reports_the_backend_that_ran(self):
        # Honesty check: the sweep must not claim a device it did not use.
        from ac_solver import ACSolver

        result = ACSolver().solve_sweep_multiport(
            MultiPortSweepTests._network_two_ports(), MultiPortSweepTests._settings(),
        )

        self.assertTrue(result.compute_backend.startswith("CPU"))
        for port in result.per_port_results.values():
            self.assertEqual(port.compute_backend, result.compute_backend)


if __name__ == "__main__":
    unittest.main()
