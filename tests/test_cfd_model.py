import unittest

from cfd_model import EnclosureModelBuilder, fit_unset_enclosure
from models import EnclosureCFDSettings, ThermalComponentModel
from thermal_model import CopperLossPoint, ThermalBoardModel, ThermalPlacement


class TestEnclosureModelBuilder(unittest.TestCase):
    def _board(self):
        return ThermalBoardModel(
            bounds_mm=(10.0, 20.0, 30.0, 30.0),
            outline=None,
            stackup={"copper": {0: {"thickness_mm": 0.035}},
                     "substrate": [{"thickness_mm": 1.5}]},
            placements={
                "U1": ThermalPlacement("U1", 15.0, 25.0, 4.0, 2.0, "TOP"),
            },
            components=[ThermalComponentModel(
                "U1", power_w=1.2, width_mm=4.0, depth_mm=2.0, height_mm=2.0,
            )],
            copper_losses=[CopperLossPoint(12.0, 22.0, 0, 0.3)],
        )

    def test_xy_board_and_component_are_mapped_as_solid_obstacles(self):
        settings = EnclosureCFDSettings()
        settings.geometry.width_mm = 40.0
        settings.geometry.depth_mm = 30.0
        settings.geometry.height_mm = 20.0
        settings.geometry.board_offset_z_mm = 5.0

        model = EnclosureModelBuilder().build(self._board(), settings)

        self.assertEqual(model.dimensions_mm, (40.0, 30.0, 20.0))
        self.assertEqual([item.name for item in model.obstacles], ["PCB", "U1"])
        self.assertAlmostEqual(model.obstacles[0].heat_w, 0.3)
        self.assertAlmostEqual(model.obstacles[1].heat_w, 1.2)
        self.assertEqual(model.obstacles[1].bounds_mm, (13.0, 14.0, 6.535, 17.0, 16.0, 8.535))

    def test_vertical_orientation_swaps_board_axes(self):
        settings = EnclosureCFDSettings()
        settings.geometry.width_mm = 40.0
        settings.geometry.depth_mm = 30.0
        settings.geometry.height_mm = 30.0
        settings.geometry.board_orientation = "XZ"

        model = EnclosureModelBuilder().build(self._board(), settings)
        board = model.obstacles[0]

        self.assertAlmostEqual(board.bounds_mm[3] - board.bounds_mm[0], 20.0)
        self.assertAlmostEqual(board.bounds_mm[4] - board.bounds_mm[1], 1.535)
        self.assertAlmostEqual(board.bounds_mm[5] - board.bounds_mm[2], 10.0)

    def test_board_must_fit_inside_enclosure(self):
        settings = EnclosureCFDSettings()
        settings.geometry.width_mm = 10.0
        settings.geometry.depth_mm = 10.0
        settings.geometry.height_mm = 10.0

        with self.assertRaisesRegex(ValueError, "does not fit"):
            EnclosureModelBuilder().build(self._board(), settings)

    def test_an_enclosure_never_entered_grows_to_hold_a_longer_board(self):
        # The P02 Rev E board is 125 x 45 mm: with the 120 mm default
        # enclosure nobody had entered, the CFD refused to run at all.
        board = self._board()
        board.bounds_mm = (158.05, 48.86, 283.05, 93.86)
        board.placements, board.components = {}, []

        settings, note = fit_unset_enclosure(EnclosureCFDSettings(), board)

        self.assertEqual((settings.geometry.width_mm, settings.geometry.depth_mm), (165.0, 100.0))
        self.assertIn("never set", note)
        EnclosureModelBuilder().build(board, settings)
        entered = EnclosureCFDSettings()
        entered.geometry.width_mm = 121.0
        self.assertIs(fit_unset_enclosure(entered, board)[0], entered)


if __name__ == "__main__":
    unittest.main()
