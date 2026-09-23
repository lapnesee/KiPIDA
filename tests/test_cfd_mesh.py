import unittest

import numpy as np

from cfd_mesh import CFDMeshGenerator
from cfd_model import CFDObstacle, EnclosureModel
from models import CFDBoundaryPatch, EnclosureCFDSettings


class TestCFDMeshGenerator(unittest.TestCase):
    def _settings(self, cell=5.0):
        settings = EnclosureCFDSettings()
        settings.solver.cell_size_mm = cell
        return settings

    def test_structured_mesh_maps_solids_heat_and_boundary_patches(self):
        model = EnclosureModel(
            dimensions_mm=(30.0, 20.0, 10.0),
            obstacles=[CFDObstacle("HOT", (10, 5, 2, 20, 15, 8), 8.0, 2.5)],
            patches=[CFDBoundaryPatch(
                "Inlet", "INLET", "XMIN", 0.5, 0.5, 0.5, 0.5, 1.0, 25.0,
            ), CFDBoundaryPatch(
                "Outlet", "OUTLET", "XMAX", 0.5, 0.5, 0.5, 0.5,
            )],
        )

        mesh = CFDMeshGenerator().generate_mesh(model, self._settings())

        self.assertEqual(mesh.shape, (6, 4, 3))
        self.assertGreater(np.count_nonzero(mesh.solid_mask), 0)
        self.assertAlmostEqual(float(np.sum(mesh.heat_sources_w)), 2.5)
        self.assertTrue(mesh.patch_cells[0].cells)
        self.assertTrue(all(cell[0] == 0 for cell in mesh.patch_cells[0].cells))
        self.assertTrue(all(mesh.fluid_mask[cell] for cell in mesh.patch_cells[0].cells))

    def test_subcell_obstacle_is_stamped_into_nearest_cell(self):
        # Cell centres at 5/15/25 mm; a 0.2 mm cube straddling 14 mm falls
        # between all three, so every axis falls back -- unlike a thin
        # board, this obstacle is genuinely a point and CFDMesh must say so.
        model = EnclosureModel(
            dimensions_mm=(30.0, 30.0, 30.0),
            obstacles=[CFDObstacle("TINY", (13.9, 13.9, 13.9, 14.1, 14.1, 14.1), 1.0, 1.0)],
        )
        mesh = CFDMeshGenerator().generate_mesh(model, self._settings(10.0))

        self.assertEqual(np.count_nonzero(mesh.solid_mask), 1)
        self.assertAlmostEqual(float(np.sum(mesh.heat_sources_w)), 1.0)
        self.assertEqual(mesh.collapsed_obstacles, ["TINY"])

    def test_thin_board_spans_its_full_footprint_not_one_cell(self):
        # docs/audit-2026-09-23.md CFD-1: P02 Rev E's 125 x 45 x 1.29 mm
        # board sat at z 15.0-16.29 mm, between the 12.5/17.5 mm cell
        # centres, so it fell back to the single cell nearest its 3D
        # centroid -- the whole board as one 5 mm cube, no conduction path
        # to the 20 components on it. A board thinner than one cell only on
        # its thickness axis must still span its full x/y footprint: only
        # that axis snaps to its nearest cell.
        model = EnclosureModel(
            dimensions_mm=(30.0, 30.0, 30.0),
            obstacles=[CFDObstacle("PCB", (0.0, 0.0, 14.0, 30.0, 20.0, 14.3), 0.6, 1.2)],
        )
        mesh = CFDMeshGenerator().generate_mesh(model, self._settings())

        self.assertEqual(int(np.count_nonzero(mesh.solid_mask)), 24)
        names = np.asarray(mesh.obstacle_names)
        board_cells = np.argwhere(names == "PCB")
        self.assertEqual(len({int(c[2]) for c in board_cells}), 1)  # one z layer
        self.assertEqual(len({int(c[0]) for c in board_cells}), 6)  # spans all of x
        self.assertEqual(mesh.collapsed_obstacles, [])
        self.assertAlmostEqual(float(mesh.heat_sources_w.max()), 1.2 / 24)

    def test_overlapping_boundary_patches_are_rejected(self):
        patch = CFDBoundaryPatch("A", "VENT", "XMIN", 0.5, 0.5, 0.5, 0.5)
        model = EnclosureModel(
            dimensions_mm=(30.0, 30.0, 30.0),
            patches=[patch, CFDBoundaryPatch("B", "OUTLET", "XMIN", 0.5, 0.5, 0.5, 0.5)],
        )

        with self.assertRaisesRegex(ValueError, "overlaps"):
            CFDMeshGenerator().generate_mesh(model, self._settings())

    def test_maximum_cell_guard_prevents_oversized_run(self):
        model = EnclosureModel(dimensions_mm=(100.0, 100.0, 100.0))
        settings = self._settings(1.0)
        settings.solver.max_cells = 10_000

        with self.assertRaisesRegex(ValueError, "Increase the CFD cell size"):
            CFDMeshGenerator().generate_mesh(model, settings)

    def test_invalid_inlet_velocity_is_rejected(self):
        model = EnclosureModel(
            dimensions_mm=(30.0, 30.0, 30.0),
            patches=[CFDBoundaryPatch("Bad fan", "FAN", "XMIN", velocity_m_s=0.0)],
        )

        with self.assertRaisesRegex(ValueError, "positive velocity"):
            CFDMeshGenerator().generate_mesh(model, self._settings())

    def test_forced_inlet_requires_an_exhaust_patch(self):
        model = EnclosureModel(
            dimensions_mm=(30.0, 30.0, 30.0),
            patches=[CFDBoundaryPatch(
                "Fan", "FAN", "XMIN", velocity_m_s=1.0,
            )],
        )

        with self.assertRaisesRegex(ValueError, "OUTLET or VENT"):
            CFDMeshGenerator().generate_mesh(model, self._settings())


if __name__ == "__main__":
    unittest.main()
