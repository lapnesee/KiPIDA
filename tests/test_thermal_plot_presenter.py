from types import SimpleNamespace
import unittest

from application.thermal_plot_presenter import internal_copper_slices, render_thermal_plots


class FakePlotter:
    calls = []

    def __init__(self, debug=False):
        self.calls.clear()

    def plot_thermal_3d(self, _mesh, _result, **kwargs):
        self.calls.append(("3d", kwargs))
        return b"3d"

    def plot_thermal_surface(self, _mesh, _result, side, **kwargs):
        self.calls.append((side, kwargs))
        return side.encode()

    def plot_thermal_layer(self, _mesh, _result, index, name, **kwargs):
        self.calls.append((index, name, kwargs))
        return name.encode()


class ThermalPlotPresenterTests(unittest.TestCase):
    def setUp(self):
        self.mesh = SimpleNamespace(
            bounds_mm=(0, 10, 0, 5),
            layer_specs=[
                SimpleNamespace(name="F.Cu", material="copper-layer"),
                SimpleNamespace(name="In1.Cu", material="copper-layer"),
                SimpleNamespace(name="Core", material="dielectric"),
                SimpleNamespace(name="In2.Cu", material="copper-layer"),
                SimpleNamespace(name="B.Cu", material="copper-layer"),
            ],
        )

    def test_internal_copper_slices_exclude_outer_layers_and_dielectrics(self):
        self.assertEqual(
            [(index, spec.name) for index, spec in internal_copper_slices(self.mesh)],
            [(1, "In1.Cu"), (3, "In2.Cu")],
        )

    def test_render_order_and_shared_colour_scale_are_consistent(self):
        plots = render_thermal_plots(
            self.mesh, object(), color_map="viridis",
            color_scale_minimum_c=20, color_scale_maximum_c=80,
            plotter_factory=FakePlotter,
        )
        self.assertEqual(
            [title for title, _data in plots],
            ["Thermal 3D", "Top Surface", "In1.Cu", "In2.Cu", "Bottom Surface"],
        )
        for call in FakePlotter.calls:
            kwargs = call[-1]
            self.assertEqual(kwargs["color_map"], "viridis")
            self.assertEqual(kwargs["color_scale_minimum_c"], 20)
            self.assertEqual(kwargs["color_scale_maximum_c"], 80)

    def test_internal_layers_can_be_hidden_without_changing_surfaces(self):
        plots = render_thermal_plots(
            self.mesh, object(), show_internal_copper_layers=False,
            plotter_factory=FakePlotter,
        )
        self.assertEqual(
            [title for title, _data in plots],
            ["Thermal 3D", "Top Surface", "Bottom Surface"],
        )


if __name__ == "__main__":
    unittest.main()
