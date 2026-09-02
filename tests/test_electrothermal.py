import os
import sys
import unittest

plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

try:
    from shapely.geometry import box
    from electrothermal import ElectroThermalSolver
    from mesh import Mesh
    from models import (
        AirflowSettings, ComponentRef, PowerRail, ThermalAnalysisSettings,
        ThermalComponentModel, UnifiedLoad, VoltageRegulator,
    )
    from thermal_mesh import ThermalMesher
    from thermal_model import PowerLossEstimator, ThermalBoardModel, ThermalPlacement
    from runtime_config import RuntimeComputeSettings
    ELECTROTHERMAL_AVAILABLE = True
except ImportError:
    ELECTROTHERMAL_AVAILABLE = False


@unittest.skipUnless(ELECTROTHERMAL_AVAILABLE, "thermal numerical dependencies unavailable")
class TestElectroThermalSolver(unittest.TestCase):
    def test_component_losses_follow_local_temperature_without_double_counting(self):
        settings = ThermalAnalysisSettings(
            ambient_c=25.0, grid_size_mm=2.0,
            airflow=AirflowSettings(
                mode="CUSTOM", custom_h_w_m2k=50.0,
                expose_top=True, expose_bottom=True, expose_edges=True,
            ),
            include_radiation=False, coupled_iterations=10,
            convergence_c=0.01, relaxation=1.0,
        )
        regulator = VoltageRegulator(
            "buck", "12V", "U1", [], "5V", "L1", [], reg_type="SWITCHING",
            loss_model={"kind": "buck", "inductors": [{
                "ref_des": "L1", "dcr_ohm": {"value": 0.1, "source": "test"},
                "reference_temperature_c": 25.0, "tempco_per_c": 0.004,
            }]},
        )
        rail_in, rail_out = PowerRail("12V", 12.0), PowerRail("5V", 5.0)
        rail_in.child_regulators.append(regulator)
        rail_out.loads.append(UnifiedLoad(ComponentRef("J1"), 2.0, thermal_mode="EXTERNAL"))
        rails = [rail_in, rail_out]
        initial = PowerLossEstimator.estimate_details(rails)
        settings.power_stage_reports = initial.stages
        initial_l1 = next(
            item.power_w for item in initial.components if item.ref_des == "L1"
        )
        board_model = ThermalBoardModel(
            bounds_mm=(0.0, 0.0, 10.0, 10.0),
            outline=box(0.0, 0.0, 10.0, 10.0),
            stackup={
                "copper": {0: {"name": "F.Cu", "thickness_mm": 0.035}},
                "layer_order": [0], "substrate": [],
            },
            copper_by_layer={0: box(0.0, 0.0, 10.0, 10.0)},
            placements={"L1": ThermalPlacement("L1", 5.0, 5.0, 4.0, 4.0)},
            components=[ThermalComponentModel(
                "L1", power_w=initial_l1, width_mm=4.0, depth_mm=4.0,
                theta_jb_c_per_w=0.0, model_source="regulator-loss",
            )],
        )
        thermal_mesh = ThermalMesher().generate_mesh(board_model, settings)
        electrical_mesh = Mesh()
        electrical_mesh.nodes = [0, 1]
        electrical_mesh.node_coords = {0: (2.0, 5.0, 0), 1: (8.0, 5.0, 0)}
        electrical_mesh.add_edge_direct(0, 1, g=10.0)
        contexts = {"12V": {
            "mesh": electrical_mesh,
            "sources": [{"node_id": 0, "voltage": 12.0}],
            "loads": [{"node_id": 1, "current": 0.1}],
        }}

        result = ElectroThermalSolver().solve(
            thermal_mesh, settings, contexts, rails=rails
        )

        final_stage = next(stage for stage in settings.power_stage_reports if stage.name == "buck")
        final_l1 = next(loss for loss in final_stage.losses if loss.ref_des == "L1")
        self.assertGreater(final_l1.provenance["temperature_c"], 25.0)
        self.assertGreater(final_l1.power_w, initial_l1)
        self.assertAlmostEqual(result.thermal.total_input_power_w,
                               final_l1.power_w + result.dc_results["12V"].total_loss_w,
                               places=6)
        self.assertEqual(len(result.thermal.temperatures_c), len(thermal_mesh.nodes))

    def test_dc_loss_heats_board_and_updates_resistance(self):
        settings = ThermalAnalysisSettings(
            ambient_c=25.0,
            grid_size_mm=2.0,
            airflow=AirflowSettings(
                mode="CUSTOM", custom_h_w_m2k=500.0,
                expose_top=True, expose_bottom=True, expose_edges=True,
            ),
            include_radiation=False,
            coupled_iterations=10,
            convergence_c=0.01,
            relaxation=1.0,
        )
        board_model = ThermalBoardModel(
            bounds_mm=(0.0, 0.0, 10.0, 10.0),
            outline=box(0.0, 0.0, 10.0, 10.0),
            stackup={
                "copper": {
                    0: {"name": "F.Cu", "thickness_mm": 0.035},
                    31: {"name": "B.Cu", "thickness_mm": 0.035},
                },
                "layer_order": [0, 31],
                "substrate": [{"between": [0, 31], "thickness_mm": 1.53}],
            },
            copper_by_layer={
                0: box(0.0, 0.0, 10.0, 10.0),
                31: box(0.0, 0.0, 10.0, 10.0),
            },
        )
        thermal_mesh = ThermalMesher().generate_mesh(board_model, settings)

        electrical_mesh = Mesh()
        electrical_mesh.nodes = [0, 1]
        electrical_mesh.node_coords = {0: (2.0, 5.0, 0), 1: (8.0, 5.0, 0)}
        electrical_mesh.add_edge_direct(0, 1, g=1.0)
        contexts = {
            "5V": {
                "mesh": electrical_mesh,
                "sources": [{"node_id": 0, "voltage": 5.0}],
                "loads": [{"node_id": 1, "current": 1.0}],
            }
        }

        result = ElectroThermalSolver().solve(thermal_mesh, settings, contexts)

        self.assertGreater(result.thermal.hotspot.temperature_c, settings.ambient_c)
        self.assertGreater(result.dc_results["5V"].total_loss_w, 1.0)
        self.assertLessEqual(result.iterations, settings.coupled_iterations)
        self.assertTrue(result.converged)

    def test_thermal_sampling_uses_configured_workers_as_row_bands(self):
        settings = ThermalAnalysisSettings(
            ambient_c=25.0, grid_size_mm=1.0,
            airflow=AirflowSettings(expose_top=True, expose_bottom=True, expose_edges=True),
        )
        board_model = ThermalBoardModel(
            bounds_mm=(0.0, 0.0, 100.0, 100.0),
            outline=box(0.0, 0.0, 100.0, 100.0),
            stackup={
                "copper": {0: {"name": "F.Cu", "thickness_mm": 0.035}, 31: {"name": "B.Cu", "thickness_mm": 0.035}},
                "layer_order": [0, 31],
                "substrate": [{"between": [0, 31], "thickness_mm": 1.53}],
            },
            copper_by_layer={0: box(0.0, 0.0, 100.0, 100.0), 31: box(0.0, 0.0, 100.0, 100.0)},
        )
        messages = []
        parallel = ThermalMesher(
            log_callback=messages.append,
            compute_settings=RuntimeComputeSettings(cpu_threads=4),
        ).generate_mesh(board_model, settings)
        serial = ThermalMesher().generate_mesh(board_model, settings)

        self.assertEqual(len(parallel.nodes), len(serial.nodes))
        self.assertEqual(len(parallel.branches), len(serial.branches))
        self.assertTrue(any("4 row-band work items with 4 CPU workers" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
