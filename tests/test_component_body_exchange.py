import os
import sys
import unittest

plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

try:
    import numpy as np
    import scipy.sparse  # noqa: F401
    from shapely.geometry import box
    from thermal_mesh import ThermalMesher
    from thermal_model import ThermalBoardModel, ThermalPlacement, merge_component_heat_sources
    from thermal_solver import ThermalSolver
    from models import AirflowSettings, ThermalAnalysisSettings, ThermalComponentModel
    THERMAL_AVAILABLE = True
except ImportError:
    THERMAL_AVAILABLE = False


@unittest.skipUnless(THERMAL_AVAILABLE, "thermal numerical dependencies unavailable")
class TestComponentBodyExchange(unittest.TestCase):
    """A part that sheds heat through its own surface: a power module on a small board."""

    def _model(self, body=True, theta=2.0, power_w=4.0, height_mm=12.0, side="TOP"):
        component = ThermalComponentModel(
            ref_des="U1", power_w=power_w, width_mm=20.0, depth_mm=10.0, height_mm=height_mm,
            theta_jb_c_per_w=theta, max_junction_c=125.0, body_exchange=body,
        )
        return ThermalBoardModel(
            bounds_mm=(0.0, 0.0, 30.0, 20.0),
            outline=box(0.0, 0.0, 30.0, 20.0),
            stackup={
                "copper": {
                    0: {"name": "F.Cu", "thickness_mm": 0.035},
                    31: {"name": "B.Cu", "thickness_mm": 0.035},
                },
                "layer_order": [0, 31],
                "substrate": [{"between": [0, 31], "material": "FR4", "thickness_mm": 1.53}],
            },
            copper_by_layer={0: box(0.0, 0.0, 30.0, 20.0), 31: box(0.0, 0.0, 30.0, 20.0)},
            vias=[],
            placements={"U1": ThermalPlacement("U1", 15.0, 10.0, 20.0, 10.0, side)},
            components=[component],
        )

    def _settings(self):
        return ThermalAnalysisSettings(
            ambient_c=25.0, grid_size_mm=1.0,
            airflow=AirflowSettings(mode="NATURAL", expose_top=True, expose_bottom=True,
                                    expose_edges=True),
        )

    def _solve(self, model, settings=None):
        settings = settings or self._settings()
        mesh = ThermalMesher().generate_mesh(model, settings)
        result = ThermalSolver().solve_with_surface_refinement(mesh, settings)
        return mesh, result

    def test_the_heat_goes_into_one_body_node_tied_to_the_board_and_the_air(self):
        mesh = ThermalMesher().generate_mesh(self._model(), self._settings())
        body = mesh.body_nodes["U1"]["node"]
        self.assertEqual(mesh.component_heat_nodes["U1"], [body])
        self.assertEqual(dict(mesh.heat_sources_w), {body: 4.0})
        self.assertIsNone(mesh.node_layers[body])
        self.assertGreater(mesh.node_coords[body][2], 1.0)   # above the board top
        _a, _b, conductance = mesh.branches.arrays()
        node_a, node_b, _g = mesh.branches.arrays()
        to_board = conductance[(node_a == body) | (node_b == body)]
        # thetaJB is the whole body-to-board resistance, spread over the pads.
        self.assertAlmostEqual(1.0 / float(np.sum(to_board)), 2.0, places=9)
        self.assertTrue(mesh.boundaries.kind_mask("body").any())

    def test_the_body_splits_its_loss_between_air_and_board_and_balances(self):
        _mesh, result = self._solve(self._model())
        part = result.component_results[0]
        self.assertTrue(part.body_exchange)
        self.assertGreater(part.body_to_air_w, 0.0)
        self.assertGreater(part.body_to_board_w, 0.0)
        self.assertAlmostEqual(part.body_to_air_w + part.body_to_board_w, 4.0, places=9)
        self.assertLess(result.energy_balance_error_pct, 1e-5)
        # The body is the junction, hotter than the board it heats through thetaJB:
        # T_body - T_board(max under it) <= P_board x thetaJB, since the board
        # under the part is not uniform.
        self.assertGreater(part.junction_temperature_c, part.board_temperature_c)
        self.assertLessEqual(
            part.junction_temperature_c - part.board_temperature_c, part.body_to_board_w * 2.0 + 1e-9,
        )

    def test_the_board_hotspot_leaves_out_the_body_node(self):
        mesh, result = self._solve(self._model())
        self.assertNotIn(result.hotspot.node_id, {entry["node"] for entry in mesh.body_nodes.values()})
        self.assertLess(result.hotspot.temperature_c, result.component_results[0].junction_temperature_c)

    def test_a_body_runs_the_board_cooler_than_the_direct_deposit(self):
        _mesh, direct = self._solve(self._model(body=False))
        _mesh, body = self._solve(self._model(body=True))
        self.assertLess(body.hotspot.temperature_c, direct.hotspot.temperature_c)

    def test_a_loosely_coupled_body_sheds_most_of_its_heat_to_the_air(self):
        _mesh, tight = self._solve(self._model(theta=0.5))
        _mesh, loose = self._solve(self._model(theta=200.0))
        self.assertGreater(loose.component_results[0].body_to_air_w,
                           tight.component_results[0].body_to_air_w)
        self.assertGreater(loose.component_results[0].body_to_air_w, 0.8 * 4.0)

    def test_a_stated_body_area_sets_the_air_exchange(self):
        small = self._model()
        small.components[0].body_area_mm2 = 100.0
        large = self._model()
        large.components[0].body_area_mm2 = 5000.0
        _mesh, small_result = self._solve(small)
        _mesh, large_result = self._solve(large)
        self.assertGreater(large_result.component_results[0].body_to_air_w,
                           small_result.component_results[0].body_to_air_w)

    def test_surface_refinement_follows_the_body_temperature(self):
        settings = self._settings()
        mesh = ThermalMesher().generate_mesh(self._model(), settings)
        boundary = mesh.body_nodes["U1"]["boundary"]
        nominal = float(mesh.boundaries.conductance[boundary])
        solver = ThermalSolver()
        result = solver.solve_with_surface_refinement(mesh, settings)
        body_c = result.component_results[0].junction_temperature_c
        expected = ThermalMesher.body_air_conductance(settings, mesh.body_nodes["U1"]["geometry"], body_c)
        refined = float(mesh.boundaries.conductance[boundary])
        self.assertNotAlmostEqual(refined, nominal, places=6)
        self.assertAlmostEqual(refined, expected, delta=expected * 0.05)

    def test_a_part_on_the_bottom_side_hangs_its_body_below_the_board(self):
        mesh = ThermalMesher().generate_mesh(self._model(side="BOTTOM"), self._settings())
        self.assertLess(mesh.node_coords[mesh.body_nodes["U1"]["node"]][2], 0.0)
        self.assertEqual(mesh.body_nodes["U1"]["geometry"]["facing"], "down")

    def test_without_the_flag_the_default_model_is_unchanged(self):
        mesh, result = self._solve(self._model(body=False))
        self.assertEqual(mesh.body_nodes, {})
        part = result.component_results[0]
        self.assertFalse(part.body_exchange)
        self.assertAlmostEqual(part.junction_temperature_c, part.board_temperature_c + 4.0 * 2.0)

    def test_a_zero_theta_keeps_the_direct_deposit(self):
        mesh = ThermalMesher().generate_mesh(self._model(theta=0.0), self._settings())
        self.assertEqual(mesh.body_nodes, {})

    def test_the_coupling_deposits_and_reads_the_part_on_its_body(self):
        from electrothermal import ElectroThermalSolver
        mesh = ThermalMesher().generate_mesh(self._model(), self._settings())
        body = mesh.body_nodes["U1"]["node"]
        self.assertEqual(ElectroThermalSolver._heat_nodes(mesh, "U1"), [body])
        temperatures = np.full(len(mesh.nodes), 40.0)
        temperatures[body] = 90.0
        read = ElectroThermalSolver._component_temperatures(mesh, temperatures, 25.0, True, None)
        self.assertEqual(read["U1"], 90.0)
        direct = ThermalMesher().generate_mesh(self._model(body=False), self._settings())
        self.assertEqual(ElectroThermalSolver._heat_nodes(direct, "U1"), direct.component_nodes["U1"])

    def test_the_flag_survives_the_estimate_merge(self):
        saved = [ThermalComponentModel(ref_des="U1", body_exchange=True, body_area_mm2=3880.0,
                                       theta_jb_c_per_w=1.0, model_source="estimated")]
        fresh = [ThermalComponentModel(ref_des="U1", power_w=6.0)]
        merged = merge_component_heat_sources(fresh, saved)
        self.assertTrue(merged[0].body_exchange)
        self.assertEqual(merged[0].body_area_mm2, 3880.0)
        self.assertEqual(merged[0].power_w, 6.0)


if __name__ == "__main__":
    unittest.main()
