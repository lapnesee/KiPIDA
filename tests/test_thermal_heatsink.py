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
    from thermal_model import ThermalBoardModel, ThermalPlacement
    from thermal_solver import ThermalSolver
    from models import (
        AirflowSettings, ThermalAnalysisSettings, ThermalComponentModel, ThermalHeatsink,
    )
    THERMAL_AVAILABLE = True
except ImportError:
    THERMAL_AVAILABLE = False


@unittest.skipUnless(THERMAL_AVAILABLE, "thermal numerical dependencies unavailable")
class TestThermalHeatsink(unittest.TestCase):
    """A heatsink bonded to a board face or to a part's top, as a resistance network."""

    def _model(self, theta=2.0, power_w=4.0):
        component = ThermalComponentModel(
            ref_des="U1", power_w=power_w, width_mm=20.0, depth_mm=10.0, height_mm=5.0,
            theta_jb_c_per_w=theta, max_junction_c=125.0,
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
            placements={"U1": ThermalPlacement("U1", 15.0, 10.0, 20.0, 10.0, "TOP")},
            components=[component],
        )

    def _settings(self, heatsinks=(), exposed=True):
        return ThermalAnalysisSettings(
            ambient_c=25.0, grid_size_mm=1.0,
            airflow=AirflowSettings(mode="NATURAL", expose_top=exposed, expose_bottom=exposed,
                                    expose_edges=exposed),
            heatsinks=list(heatsinks),
        )

    def _solve(self, model, settings):
        mesh = ThermalMesher().generate_mesh(model, settings)
        return mesh, ThermalSolver().solve_with_surface_refinement(mesh, settings)

    def test_a_board_heatsink_is_its_interface_and_theta_sa_in_series(self):
        # Every face of the board closed: all 4 W leave through the heatsink.
        # A 1 mm gap pad at 3 W/mK over the 600 mm2 underside is 0.556 K/W,
        # and the heatsink sits P x thetaSA over the ambient.
        heatsink = ThermalHeatsink(
            name="HS1", side="BOTTOM", x_min_mm=0.0, y_min_mm=0.0, x_max_mm=30.0, y_max_mm=20.0,
            interface_thickness_mm=1.0, interface_k_w_mk=3.0, theta_sa_c_per_w=8.0,
        )
        _mesh, result = self._solve(self._model(), self._settings([heatsink], exposed=False))
        row = result.heatsink_results[0]
        interface = 1.0e-3 / 3.0 / 600.0e-6
        self.assertAlmostEqual(row.interface_c_per_w, interface, places=9)
        self.assertAlmostEqual(row.contact_area_mm2, 600.0, places=6)
        self.assertAlmostEqual(row.to_air_w, 4.0, places=6)
        self.assertAlmostEqual(row.temperature_c, 25.0 + 4.0 * 8.0, places=6)
        # Uniform per-area conductance: the area-weighted base temperature is
        # the heatsink's plus P x R_interface.
        self.assertAlmostEqual(row.base_c - row.temperature_c, 4.0 * interface, places=6)
        self.assertLess(result.energy_balance_error_pct, 1e-6)

    def test_a_heatsink_on_a_part_is_theta_jc_interface_and_theta_sa_in_series(self):
        # The board's faces closed again: the board is a dead end, and the
        # junction is the ambient plus P x (thetaJC + interface + thetaSA).
        # 1 K cm2/W over the 20 x 10 mm top is 0.5 K/W.
        heatsink = ThermalHeatsink(
            name="HS2", mount="COMPONENT", ref_des="U1", theta_jc_c_per_w=0.5,
            interface_resistance_k_cm2_w=1.0, theta_sa_c_per_w=5.0,
        )
        mesh, result = self._solve(self._model(), self._settings([heatsink], exposed=False))
        part = result.component_results[0]
        self.assertIsNone(mesh.body_nodes["U1"]["boundary"])
        self.assertAlmostEqual(part.junction_temperature_c, 25.0 + 4.0 * (0.5 + 0.5 + 5.0), places=6)
        self.assertAlmostEqual(part.body_to_heatsink_w, 4.0, places=6)
        self.assertAlmostEqual(part.body_to_board_w, 0.0, places=6)
        self.assertEqual(result.heatsink_results[0].ref_des, "U1")
        self.assertNotIn(result.hotspot.node_id, {entry["node"] for entry in mesh.heatsink_nodes})

    def test_a_heatsink_on_a_part_with_a_body_shares_the_heat_three_ways(self):
        model = self._model()
        model.components[0].body_exchange = True
        heatsink = ThermalHeatsink(name="HS2", mount="COMPONENT", ref_des="U1", theta_sa_c_per_w=10.0)
        mesh, result = self._solve(model, self._settings([heatsink]))
        part = result.component_results[0]
        self.assertEqual(mesh.body_nodes["U1"]["geometry"]["top_m2"], 0.0)
        self.assertGreater(part.body_to_air_w, 0.0)
        self.assertGreater(part.body_to_heatsink_w, 0.0)
        self.assertGreater(part.body_to_board_w, 0.0)
        self.assertAlmostEqual(
            part.body_to_air_w + part.body_to_heatsink_w + part.body_to_board_w, 4.0, places=9,
        )
        self.assertLess(result.energy_balance_error_pct, 1e-5)

    def test_the_zone_takes_its_area_off_the_face_and_cools_the_board(self):
        settings = self._settings([ThermalHeatsink(
            name="HS1", side="BOTTOM", x_min_mm=0.0, y_min_mm=0.0, x_max_mm=15.0, y_max_mm=20.0,
            interface_thickness_mm=1.0, interface_k_w_mk=3.0, theta_sa_c_per_w=8.0,
        )])
        bare = ThermalMesher().generate_mesh(self._model(), self._settings())
        mesh = ThermalMesher().generate_mesh(self._model(), settings)
        _ids, bare_g = bare.boundaries.arrays()
        _ids, cooled_g = mesh.boundaries.arrays()
        bottom = bare.boundaries.kind_mask("bottom")
        self.assertAlmostEqual(
            float(np.sum(cooled_g[mesh.boundaries.kind_mask("bottom")])),
            float(np.sum(bare_g[bottom])) / 2.0, places=9,
        )
        self.assertAlmostEqual(mesh.surface_area_m2["bottom"], bare.surface_area_m2["bottom"] / 2.0)
        _mesh, without = self._solve(self._model(), self._settings())
        _mesh, with_heatsink = self._solve(self._model(), settings)
        self.assertLess(with_heatsink.hotspot.temperature_c, without.hotspot.temperature_c)
        self.assertLess(with_heatsink.energy_balance_error_pct, 1e-5)

    def test_fins_refine_their_exchange_on_the_heatsink_temperature(self):
        settings = self._settings([ThermalHeatsink(
            name="HS1", side="BOTTOM", x_min_mm=0.0, y_min_mm=0.0, x_max_mm=30.0, y_max_mm=20.0,
            interface_resistance_k_cm2_w=1.0, fin_area_mm2=20000.0, fin_length_mm=20.0,
            emissivity=0.85,
        )])
        mesh = ThermalMesher().generate_mesh(self._model(), settings)
        entry = mesh.heatsink_nodes[0]
        nominal = float(mesh.boundaries.conductance[entry["boundary"]])
        result = ThermalSolver().solve_with_surface_refinement(mesh, settings)
        row = result.heatsink_results[0]
        expected = ThermalMesher.heatsink_air_conductance(
            settings, entry["heatsink"], entry["geometry"], row.temperature_c,
        )
        refined = float(mesh.boundaries.conductance[entry["boundary"]])
        self.assertNotAlmostEqual(refined, nominal, places=6)
        self.assertAlmostEqual(refined, expected, delta=expected * 0.05)
        self.assertIn("20000 mm2 of fins", row.theta_sa_source)

    def test_a_heatsink_that_cannot_be_bonded_is_named_not_dropped(self):
        settings = self._settings([
            ThermalHeatsink(name="OFF", side="BOTTOM", x_min_mm=100.0, y_min_mm=100.0,
                            x_max_mm=110.0, y_max_mm=110.0, theta_sa_c_per_w=8.0),
            ThermalHeatsink(name="BARE", side="BOTTOM", x_min_mm=0.0, y_min_mm=0.0,
                            x_max_mm=10.0, y_max_mm=10.0),
            ThermalHeatsink(name="GONE", mount="COMPONENT", ref_des="U9", theta_sa_c_per_w=8.0),
            ThermalHeatsink(name="IDLE", enabled=False, theta_sa_c_per_w=8.0),
        ])
        _mesh, result = self._solve(self._model(), settings)
        self.assertEqual(result.heatsink_results, [])
        self.assertEqual([line.split(":")[0] for line in result.heatsink_skipped], ["BARE", "GONE", "OFF"])
        from analysis_adapters import adapt_thermal_result
        adapted = adapt_thermal_result(result)
        finding = next(item for item in adapted.findings if item.rule_id == "TH-010")
        self.assertIn("OFF", finding.description)

    def test_the_heatsinks_survive_a_save_and_an_old_config_has_none(self):
        from config_manager import _dict_to_thermal_settings, _thermal_settings_to_dict
        settings = self._settings([ThermalHeatsink(
            name="HS1", side="BOTTOM", x_min_mm=137.0, y_min_mm=92.0, x_max_mm=161.0, y_max_mm=112.0,
            interface_thickness_mm=1.0, interface_k_w_mk=3.0, theta_sa_c_per_w=8.0,
            polygon_mm=[[137.0, 92.0], [161.0, 92.0], [161.0, 112.0]],
        )])
        data = _thermal_settings_to_dict(settings)
        self.assertEqual(_dict_to_thermal_settings(data).heatsinks, settings.heatsinks)
        del data["heatsinks"]
        self.assertEqual(_dict_to_thermal_settings(data).heatsinks, [])

    def test_a_heatsink_or_a_body_changes_the_thermal_cache_key(self):
        # A cached mesh would hand back the board solved without the change.
        from application.thermal_controller import thermal_cache_key
        settings = self._settings()
        settings.components = list(self._model().components)
        key = thermal_cache_key(settings, None, False, [])
        settings.heatsinks = [ThermalHeatsink(name="HS1", theta_sa_c_per_w=8.0)]
        with_heatsink = thermal_cache_key(settings, None, False, [])
        settings.heatsinks[0].theta_sa_c_per_w = 4.0
        self.assertEqual(len({key, with_heatsink, thermal_cache_key(settings, None, False, [])}), 3)
        settings.heatsinks = []
        settings.components[0].body_exchange = True
        self.assertNotEqual(thermal_cache_key(settings, None, False, []), key)


if __name__ == "__main__":
    unittest.main()
