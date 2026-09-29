import unittest

from cfd_model import EnclosureModelBuilder, enclosure_air_rise_k, fit_unset_enclosure
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


class TestBoardConductivity(unittest.TestCase):
    def test_the_board_conducts_through_its_copper(self):
        # P02 Rev E: the board was an isotropic 0.6 W/mK solid, and Q10's
        # cell reached 293.7 C. Two 35 um layers fully covered over 1.53 mm
        # of laminate conduct (385 * 0.07 + 0.8 * 1.53) / 1.6 W/mK in plane.
        from shapely.geometry import box
        from cfd_mesh import CFDMeshGenerator

        board = ThermalBoardModel(
            bounds_mm=(0.0, 0.0, 30.0, 20.0), outline=None,
            stackup={"copper": {0: {"thickness_mm": 0.035}, 31: {"thickness_mm": 0.035}},
                     "substrate": [{"thickness_mm": 1.53}]},
            copper_by_layer={0: box(0.0, 0.0, 30.0, 20.0), 31: box(0.0, 0.0, 30.0, 20.0)},
        )
        expected = (385.0 * 0.07 + 0.8 * 1.53) / 1.6
        self.assertAlmostEqual(EnclosureModelBuilder.board_in_plane_conductivity(board), expected)

        settings = EnclosureCFDSettings()
        settings.geometry.width_mm, settings.geometry.depth_mm = 40.0, 30.0
        settings.geometry.height_mm, settings.geometry.board_offset_z_mm = 20.0, 5.0
        mesh = CFDMeshGenerator().generate_mesh(EnclosureModelBuilder().build(board, settings), settings)
        # Snapped into a 5 mm cell, the 1.6 mm board keeps its k * t.
        self.assertAlmostEqual(float(mesh.solid_conductivity_w_mk.max()), expected * 1.6 / 5.0, places=6)


class TestEnclosureAirRise(unittest.TestCase):
    def test_matches_p02_rev_e(self):
        # docs/audit-2026-09-23.md CFD-4, scratchpad/audit/cfd/p02_inspect.py
        # (2026-09-22): 4.755 W into P02 Rev E's 165 x 100 x 50 mm enclosure
        # (A = 0.0595 m^2) at wall_heat_transfer_w_m2k = 5 -> 16.0 K, matching
        # the solved wall-cell mean of 61.0 C - 45 C ambient.
        settings = EnclosureCFDSettings()
        settings.geometry.width_mm = 165.0
        settings.geometry.depth_mm = 100.0
        settings.geometry.height_mm = 50.0
        settings.geometry.wall_heat_transfer_w_m2k = 5.0

        self.assertAlmostEqual(enclosure_air_rise_k(settings, 4.755), 16.0, places=1)


if __name__ == "__main__":
    unittest.main()
