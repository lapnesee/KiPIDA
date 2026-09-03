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

from ac_model import ACModelBuilder, nearest_ground_nodes
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


if __name__ == "__main__":
    unittest.main()
