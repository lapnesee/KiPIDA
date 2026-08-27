import unittest

from kipy.board_types import BoardLayer

from thermal_overlay import ThermalOverlayManager, _temperature_limits, heatmap_png, heatmap_scale_png


class _LegacyLayerBoard:
    """KiCad 10 IPC shape where layer calls live directly on Board."""

    def __init__(self):
        self.enabled = {BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu, BoardLayer.BL_Dwgs_User}
        self.set_calls = []

    def get_enabled_layers(self):
        return list(self.enabled)

    def get_copper_layer_count(self):
        return 2

    def set_enabled_layers(self, copper_count, layers):
        self.set_calls.append((copper_count, set(layers)))
        self.enabled = set(layers) | {BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu}
        return list(self.enabled)


class TestThermalOverlayLayers(unittest.TestCase):
    def test_legacy_board_layer_api_reserves_unused_user_layers(self):
        board = _LegacyLayerBoard()
        messages = []

        top, bottom = ThermalOverlayManager(board, messages.append)._layers()

        self.assertEqual((top, bottom), (BoardLayer.BL_User_1, BoardLayer.BL_User_2))
        self.assertEqual(len(board.set_calls), 1)
        copper_count, enabled = board.set_calls[0]
        self.assertEqual(copper_count, 2)
        self.assertNotIn(BoardLayer.BL_F_Cu, enabled)
        self.assertNotIn(BoardLayer.BL_B_Cu, enabled)
        self.assertIn(BoardLayer.BL_Dwgs_User, enabled)
        self.assertIn(BoardLayer.BL_User_1, enabled)
        self.assertIn(BoardLayer.BL_User_2, enabled)
        self.assertTrue(any("cannot rename User layers" in message for message in messages))

    def test_shared_scale_uses_the_whole_thermal_result(self):
        result = type("Result", (), {"temperature_vector_c": [31.25, 48.5, 92.75]})()
        self.assertEqual(_temperature_limits(result), (31.25, 92.75))

    def test_overlay_and_scale_pngs_embed_the_owner_marker(self):
        mesh = type("Mesh", (), {
            "bounds_mm": (0.0, 0.0, 2.0, 2.0),
            "grid_size_mm": 1.0,
            "layer_specs": [0, 1],
            "node_map": {
                (0, 0, 0): 0, (1, 0, 0): 1, (0, 1, 0): 2, (1, 1, 0): 3,
                (0, 0, 1): 4, (1, 0, 1): 5, (0, 1, 1): 6, (1, 1, 1): 7,
            },
        })()
        result = type("Result", (), {"temperature_vector_c": [25, 40, 55, 70, 30, 45, 60, 75]})()

        heat, origin, dpi = heatmap_png(mesh, result, "BOTTOM", "viridis", (25.0, 75.0))
        scale, width, height, scale_dpi = heatmap_scale_png("BOTTOM", "viridis", (25.0, 75.0))

        self.assertEqual(origin, (0.0, 0.0))
        self.assertGreater(dpi, 0.0)
        self.assertGreater(scale_dpi, 0.0)
        self.assertGreater(width, 0.0)
        self.assertGreater(height, 0.0)
        self.assertIn(b"KiPIDA-Thermal-Overlay-v1", heat)
        self.assertIn(b"KiPIDA-Thermal-Overlay-v1", scale)


if __name__ == "__main__":
    unittest.main()
