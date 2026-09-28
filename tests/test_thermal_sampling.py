"""Dependency-light regression tests for the fine-mesh sampling fast path."""

import os
import sys
import unittest
from types import SimpleNamespace


plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

import thermal_mesh


class _Rectangle:
    def __init__(self, min_x, min_y, max_x, max_y):
        self.bounds = (min_x, min_y, max_x, max_y)
        self.is_empty = False

    def covers(self, _other):
        return True


def _rectangle_intersects_xy(rectangle, x_values, y_values):
    min_x, min_y, max_x, max_y = rectangle.bounds
    return (
        (x_values >= min_x) & (x_values <= max_x) &
        (y_values >= min_y) & (y_values <= max_y)
    )


class TestVectorizedThermalSampling(unittest.TestCase):
    def test_vectorized_band_preserves_sparse_cell_coordinates_and_copper(self):
        # Real geometry: edge cells now carry their exact copper fraction,
        # which a stub rectangle cannot compute. Cells fully inside or
        # outside the copper still read 1.0 and 0.0.
        from shapely.geometry import box
        cells = thermal_mesh.ThermalMesher._sample_layer_band(
            box(0.0, 0.0, 4.0, 3.0), box(1.0, 1.0, 3.0, 3.0),
            0.0, 0.0, 4, 1, 3, 1.0,
        )
        self.assertEqual(
            cells,
            [
                (0, 1, 0.0), (1, 1, 1.0), (2, 1, 1.0), (3, 1, 0.0),
                (0, 2, 0.0), (1, 2, 1.0), (2, 2, 1.0), (3, 2, 0.0),
            ],
        )

    def test_rectangular_outline_skips_outline_query_without_changing_cells(self):
        original = thermal_mesh.intersects_xy
        calls = []

        def copper_only(rectangle, x_values, y_values):
            calls.append(rectangle)
            return _rectangle_intersects_xy(rectangle, x_values, y_values)

        thermal_mesh.intersects_xy = copper_only
        try:
            cells = thermal_mesh.ThermalMesher._sample_layer_band(
                _Rectangle(0.0, 0.0, 2.0, 2.0), None,
                0.0, 0.0, 2, 0, 2, 1.0, outline_is_rectangular=True,
            )
        finally:
            thermal_mesh.intersects_xy = original

        self.assertEqual(cells, [(0, 0, False), (1, 0, False), (0, 1, False), (1, 1, False)])
        self.assertEqual(calls, [])

    def test_parallel_row_bands_accept_vector_fast_path_argument(self):
        original = {
            "intersects_xy": thermal_mesh.intersects_xy,
            "point": thermal_mesh.Point,
            "prep": thermal_mesh.prep,
            "box": thermal_mesh.box,
        }
        thermal_mesh.intersects_xy = _rectangle_intersects_xy
        # This test deliberately runs without Shapely installed: the vector
        # path does not need scalar Point/prep objects after the API check.
        thermal_mesh.Point = object()
        thermal_mesh.prep = object()
        thermal_mesh.box = lambda *values: values
        messages = []
        model = SimpleNamespace(
            bounds_mm=(0.0, 0.0, 60.0, 60.0),
            outline=_Rectangle(0.0, 0.0, 60.0, 60.0),
            stackup={
                "copper": {
                    0: {"name": "F.Cu", "thickness_mm": 0.035},
                    31: {"name": "B.Cu", "thickness_mm": 0.035},
                },
                "layer_order": [0, 31],
                "substrate": [{"between": [0, 31], "thickness_mm": 1.53}],
            },
            copper_by_layer={}, vias=[], components=[], placements={}, copper_losses=[],
        )
        settings = SimpleNamespace(
            grid_size_mm=1.0, ambient_c=25.0, include_radiation=False,
            emissivity=0.9,
            airflow=SimpleNamespace(
                mode="NATURAL", custom_h_w_m2k=10.0, velocity_m_s=0.0,
                direction_deg=0.0,
                expose_top=True, expose_bottom=True, expose_edges=True,
            ),
        )
        try:
            mesh = thermal_mesh.ThermalMesher(
                log_callback=messages.append,
                compute_settings=SimpleNamespace(
                    cpu_multithread=True, cpu_threads=4,
                    cuda_enabled=False, backend="CPU", memory_limit_gib=0.0,
                ),
            ).generate_mesh(model, settings)

            # Stackup order is F.Cu -> B.Cu.  Component heat and the named
            # convective surfaces must follow that physical order, not the old
            # inverted z-index convention.
            top_component = SimpleNamespace(ref_des="TOP", enabled=True, power_w=1.0)
            bottom_component = SimpleNamespace(ref_des="BOTTOM", enabled=True, power_w=1.0)
            model.components = [top_component, bottom_component]
            model.placements = {
                "TOP": SimpleNamespace(x_mm=10.0, y_mm=10.0, width_mm=2.0, depth_mm=2.0, side="TOP"),
                "BOTTOM": SimpleNamespace(x_mm=50.0, y_mm=50.0, width_mm=2.0, depth_mm=2.0, side="BOTTOM"),
            }
            face_mesh = thermal_mesh.ThermalMesher().generate_mesh(model, settings)
        finally:
            thermal_mesh.intersects_xy = original["intersects_xy"]
            thermal_mesh.Point = original["point"]
            thermal_mesh.prep = original["prep"]
            thermal_mesh.box = original["box"]

        self.assertEqual(len(mesh.nodes), 10_800)
        # 3 full 60x60 layers: two lateral directions per layer plus two
        # vertical interfaces.  This guards the vectorised connectivity path
        # against dropping or duplicating finite-volume conductances.
        self.assertEqual(len(mesh.branches), 28_440)
        self.assertEqual(len(mesh.boundaries), 7_920)
        self.assertTrue(any("4 row-band work items with 4 CPU workers" in message for message in messages))

        self.assertEqual(face_mesh.node_layers[face_mesh.component_nodes["TOP"][0]], 0)
        self.assertEqual(face_mesh.node_layers[face_mesh.component_nodes["BOTTOM"][0]], 31)
        self.assertTrue(all(face_mesh.node_layers[item.node_id] == 0 for item in face_mesh.boundaries if item.kind == "top"))
        self.assertTrue(all(face_mesh.node_layers[item.node_id] == 31 for item in face_mesh.boundaries if item.kind == "bottom"))



class CopperFractionTests(unittest.TestCase):
    def test_a_trace_narrower_than_a_cell_is_a_fraction_of_it(self):
        # A cell was copper or laminate by its centre alone: a 0.1 mm trace
        # on a 0.5 mm grid was either a 0.5 mm copper strip or nothing
        # (open since the 2026-09-04 thermal audit).
        from shapely.geometry import box
        from thermal_mesh import ThermalMesher
        outline = box(0.0, 0.0, 2.0, 2.0)
        trace = box(0.0, 0.95, 2.0, 1.05)
        cells = ThermalMesher._sample_layer_band(outline, trace, 0.0, 0.0, 4, 0, 4, 0.5)
        fractions = {(ix, iy): fraction for ix, iy, fraction in cells}
        # Rows 1 and 2 straddle y = 1.0; each holds part of the trace.
        self.assertTrue(all(0.0 < fractions[(ix, 1)] < 1.0 for ix in range(4)))
        self.assertEqual(fractions[(0, 0)], 0.0)

if __name__ == "__main__":
    unittest.main()
