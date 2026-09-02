from types import SimpleNamespace
import unittest

from application.dc_plot_presenter import (
    flatten_dc_plot_groups,
    layer_name,
    render_dc_plots,
)


class FakePlotter:
    calls = []

    def __init__(self, debug=False):
        self.calls.clear()

    def plot_3d_mesh(self, _mesh, _stackup, **kwargs):
        self.calls.append(("3d", kwargs))
        return b"3d"

    def plot_layer_2d(self, _mesh, layer_id, _stackup, **kwargs):
        self.calls.append((layer_id, kwargs))
        return str(layer_id).encode()


class DCPlotPresenterTests(unittest.TestCase):
    def test_layer_name_uses_stackup_with_stable_fallback(self):
        stackup = {"copper": {2: {"name": "In1.Cu"}}}
        self.assertEqual(layer_name(stackup, 2), "In1.Cu")
        self.assertEqual(layer_name(stackup, 7), "7")

    def test_render_groups_rails_and_sorts_layers(self):
        mesh = SimpleNamespace(
            node_coords={1: (0, 0, 2), 2: (1, 0, 0)}, nodes=[1, 2],
        )
        groups = render_dc_plots(
            {"VCC": {"mesh": mesh, "results": {1: 3.2, 2: 3.3}, "stats": (3.2, 3.3, 0.1)}},
            stackup={"copper": {0: {"name": "F.Cu"}, 2: {"name": "In1.Cu"}}},
            drop_pct=10, plotter_factory=FakePlotter,
        )
        self.assertEqual(groups[0][0], "VCC")
        self.assertEqual([title for title, _png in groups[0][1]], ["3D View", "F.Cu", "In1.Cu"])
        for _kind, kwargs in FakePlotter.calls:
            self.assertTrue(kwargs["as_png"])
            self.assertAlmostEqual(kwargs["vmin"], 2.97)
            self.assertEqual(kwargs["vmax"], 3.3)

    def test_drop_percentage_is_clamped(self):
        mesh = SimpleNamespace(node_coords={1: (0, 0, 0)}, nodes=[1])
        render_dc_plots(
            {"V": {"mesh": mesh, "results": {1: 5.0}, "stats": (5.0, 5.0, 0.0)}},
            drop_pct=150, plotter_factory=FakePlotter,
        )
        self.assertEqual(FakePlotter.calls[0][1]["vmin"], 0.0)

    def test_flattened_titles_identify_rail_and_view_for_history(self):
        flattened = flatten_dc_plot_groups([
            ("VCC", [("3D View", b"a"), ("F.Cu", b"b")]),
            ("1V8", [("F.Cu", b"c")]),
        ])
        self.assertEqual(
            [title for title, _png in flattened],
            ["VCC — 3D View", "VCC — F.Cu", "1V8 — F.Cu"],
        )


if __name__ == "__main__":
    unittest.main()
