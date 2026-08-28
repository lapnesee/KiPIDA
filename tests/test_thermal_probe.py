import os
import sys
import unittest
from types import SimpleNamespace


plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

from thermal_mesh import ThermalLayerSpec, ThermalMesh
from thermal_probe import ThermalMapProbe


class TestThermalMapProbe(unittest.TestCase):
    def setUp(self):
        self.mesh = ThermalMesh(
            nodes=[0, 1, 2, 3],
            node_coords={
                0: (0.5, 0.5, 0.0), 1: (1.5, 0.5, 0.0),
                2: (0.5, 1.5, 0.0), 3: (1.5, 1.5, 0.0),
            },
            node_layers={0: 0, 1: 0, 2: 0, 3: 0},
            node_map={(0, 0, 0): 0, (1, 0, 0): 1, (0, 1, 0): 2, (1, 1, 0): 3},
            grid_size_mm=1.0,
            bounds_mm=(0.0, 0.0, 2.0, 2.0),
            layer_specs=[ThermalLayerSpec("F.Cu", 0.035, 0, "copper-layer")],
        )
        self.result = SimpleNamespace(temperature_vector_c=[41.5, 42.5, 43.5, 44.5])

    def test_maps_bitmap_coordinates_to_the_nearest_mesh_node(self):
        probe = ThermalMapProbe(
            self.mesh, self.result, 0, "F.Cu",
            axes_bounds=(0.1, 0.2, 0.7, 0.6), x_limits=(0.0, 2.0), y_limits=(2.0, 0.0),
        )

        reading = probe.sample(27.5, 35.0, 100, 100)

        self.assertIsNotNone(reading)
        self.assertEqual((reading.x_mm, reading.y_mm, reading.z_mm), (0.5, 0.5, 0.0))
        self.assertEqual(reading.temperature_c, 41.5)
        self.assertIn("F.Cu", reading.label())

    def test_ignores_mouse_positions_outside_the_plot_axes(self):
        probe = ThermalMapProbe(
            self.mesh, self.result, 0, "F.Cu",
            axes_bounds=(0.1, 0.2, 0.7, 0.6), x_limits=(0.0, 2.0), y_limits=(2.0, 0.0),
        )

        self.assertIsNone(probe.sample(2.0, 50.0, 100, 100))


if __name__ == "__main__":
    unittest.main()
