"""Dependency-light regression tests for the fine-mesh sampling fast path."""

import os
import sys
import unittest


plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

import thermal_mesh


class _Rectangle:
    def __init__(self, min_x, min_y, max_x, max_y):
        self.bounds = (min_x, min_y, max_x, max_y)
        self.is_empty = False


def _rectangle_intersects_xy(rectangle, x_values, y_values):
    min_x, min_y, max_x, max_y = rectangle.bounds
    return (
        (x_values >= min_x) & (x_values <= max_x) &
        (y_values >= min_y) & (y_values <= max_y)
    )


class TestVectorizedThermalSampling(unittest.TestCase):
    def test_vectorized_band_preserves_sparse_cell_coordinates_and_copper(self):
        original = thermal_mesh.intersects_xy
        thermal_mesh.intersects_xy = _rectangle_intersects_xy
        try:
            cells = thermal_mesh.ThermalMesher._sample_layer_band(
                _Rectangle(0.0, 0.0, 4.0, 3.0),
                _Rectangle(1.0, 1.0, 3.0, 3.0),
                0.0, 0.0, 4, 1, 3, 1.0,
            )
        finally:
            thermal_mesh.intersects_xy = original

        self.assertEqual(
            cells,
            [
                (0, 1, False), (1, 1, True), (2, 1, True), (3, 1, False),
                (0, 2, False), (1, 2, True), (2, 2, True), (3, 2, False),
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


if __name__ == "__main__":
    unittest.main()
