import unittest

from kipy.board_types import BoardLayer

from thermal_overlay import ThermalOverlayManager


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


if __name__ == "__main__":
    unittest.main()
