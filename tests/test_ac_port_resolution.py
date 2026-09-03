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


if __name__ == "__main__":
    unittest.main()
