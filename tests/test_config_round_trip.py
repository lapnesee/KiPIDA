"""Loading a project, refreshing it as a batch does, and saving it gives the file back.

poe_bt's generated configuration was saved again before each batch and came
out changed without anyone touching it: its capacitor models replaced by the
case size table, a source's ground pads doubled, a J2 at 0 W added and the
heat sources reordered.
"""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

from ac_model import ACModelBuilder, merge_discovered_capacitors
from config_manager import load_project_config, save_config
from models import (
    ACAnalysisSettings, ACMeasurementPort, ACSourceModel, ComponentRef, PowerRail,
    ThermalAnalysisSettings, ThermalComponentModel, UnifiedLoad, UnifiedSource,
)
from thermal_model import PowerLossEstimator, merge_component_heat_sources


def pad(number, net_name):
    return SimpleNamespace(number=number, net=SimpleNamespace(name=net_name))


def capacitor(reference, value, footprint="Capacitor_SMD:C_0805"):
    return SimpleNamespace(reference=reference, value=value, footprint_name=footprint,
                           pads=[pad("1", "+15V"), pad("2", "GND")], dnp=False)


def board(*capacitors):
    # U1 has two pads numbered 12, as poe_bt's Ag5810 does.
    regulator = SimpleNamespace(reference="U1", value="REG", footprint_name="Package:SIL",
                                pads=[pad("1", "+15V"), pad("9", "GND"), pad("11", "GND"),
                                      pad("12", "GND"), pad("12", "GND")], dnp=False)
    return SimpleNamespace(footprints=[regulator, *capacitors])


def rails():
    rail = PowerRail(net_name="+15V", nominal_voltage=15.0)
    rail.add_source(UnifiedSource(component_ref=ComponentRef(ref_des="U1"), pad_names=["1"]))
    # J2 exports its current (a 0 W row in a fresh estimate); U5 burns 1.5 W.
    rail.add_load(UnifiedLoad(component_ref=ComponentRef(ref_des="J2"), total_current=0.5))
    rail.add_load(UnifiedLoad(component_ref=ComponentRef(ref_des="U5"), total_current=0.1))
    return [rail]


def refresh(project, builder, log):
    """What the AC and thermal tabs do with a loaded project before a batch."""
    for profile in project.ac_profiles.values():
        discovered = builder.discover_capacitors(profile.rail_name, profile.ground_net_name,
                                                 rail_voltage_v=15.0)
        profile.capacitors = merge_discovered_capacitors(discovered, profile.capacitors, log=log)
        profile.source.ground_pad_names = builder.pad_names_for_net(
            profile.source.ref_des, profile.ground_net_name)
    estimate = PowerLossEstimator.estimate_details(project.rails).components
    project.thermal_profile.components = merge_component_heat_sources(
        estimate, project.thermal_profile.components)


class ConfigRoundTripTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".kipida.json")
        os.close(handle)
        self.log = []

    def tearDown(self):
        for name in os.listdir(os.path.dirname(self.path)):
            if name.startswith(os.path.basename(self.path)):
                os.unlink(os.path.join(os.path.dirname(self.path), name))

    def written(self, parts):
        """A file as a generator writes it: models of every source, in its own order."""
        builder = ACModelBuilder(board(*parts))
        found = {cap.ref_des: cap for cap in builder.discover_capacitors("+15V", "GND", rail_voltage_v=15.0)}
        found["C1"].enabled = False  # Ki-PIDA's own estimate, switched off
        found["C2"].capacitance_f, found["C2"].esr_ohm, found["C2"].esl_h = 1e-3, 0.025, 13e-9
        found["C2"].model_source = "estimate: TDK B41866 1000 uF; ESR 25 mOhm, ESL 13 nH"
        found["C3"].capacitance_f, found["C3"].model_source = 4.5e-6, "datasheet: TDK CGA5L1X8L1E106K"
        found["C4"].esr_ohm, found["C4"].model_source = 0.02, "user"
        profile = ACAnalysisSettings(
            rail_name="+15V", ground_net_name="GND",
            source=ACSourceModel(ref_des="U1", rail_pad_names=["1"], ground_pad_names=["9", "11", "12"]),
            measurement_port=ACMeasurementPort(ref_des="U5", rail_pad_names=["1"], ground_pad_names=["2"]),
            capacitors=[found[ref] for ref in ("C4", "C2", "C1", "C3")],
        )
        thermal = ThermalAnalysisSettings(components=[
            ThermalComponentModel(ref_des="FB1", power_w=0.05, model_source="estimate: ferrite DCR 30 mOhm"),
            ThermalComponentModel(ref_des="U5", power_w=1.5, width_mm=5.0, model_source="power-tree-load"),
            ThermalComponentModel(ref_des="D1", power_w=0.4, model_source="user"),
        ])
        save_config(rails(), self.path, {"+15V": profile}, thermal_profile=thermal)
        return builder

    def resave(self, builder):
        project = load_project_config(self.path)
        refresh(project, builder, self.log.append)
        save_config(project.rails, self.path, project.ac_profiles,
                    thermal_profile=project.thermal_profile)

    def test_loading_refreshing_and_saving_gives_the_file_back(self):
        builder = self.written([capacitor("C1", "1u 25V X7R"), capacitor("C2", "1000u"),
                                capacitor("C3", "10u 25V X7R", "Capacitor_SMD:C_1206"),
                                capacitor("C4", "100n 50V X7R", "Capacitor_SMD:C_0603")])
        with open(self.path, encoding="utf-8") as handle:
            before = json.load(handle)

        self.resave(builder)
        self.resave(builder)

        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), before)
        self.assertEqual(self.log, [])

    def test_a_board_change_keeps_the_saved_models_and_says_what_moved(self):
        builder = self.written([capacitor("C1", "1u 25V X7R"), capacitor("C2", "1000u"),
                                capacitor("C3", "10u 25V X7R", "Capacitor_SMD:C_1206"),
                                capacitor("C4", "100n 50V X7R", "Capacitor_SMD:C_0603")])
        # C3 left the board, C7 joined it.
        builder = ACModelBuilder(board(capacitor("C1", "1u 25V X7R"), capacitor("C2", "1000u"),
                                       capacitor("C4", "100n 50V X7R", "Capacitor_SMD:C_0603"),
                                       capacitor("C7", "22u 25V X5R", "Capacitor_SMD:C_1210")))

        self.resave(builder)

        profile = load_project_config(self.path).ac_profiles["+15V"]
        caps = {cap.ref_des: cap for cap in profile.capacitors}
        self.assertEqual([cap.ref_des for cap in profile.capacitors], ["C4", "C2", "C1", "C7"])
        self.assertEqual((caps["C2"].esr_ohm, caps["C2"].esl_h), (0.025, 13e-9))
        self.assertTrue(caps["C2"].model_source.startswith("estimate: TDK"))
        self.assertEqual(caps["C4"].esr_ohm, 0.02)
        self.assertFalse(caps["C1"].enabled)
        self.assertEqual(self.log, [
            "AC: C3 no longer bridge the rail and its return; left out of the profile.",
            "AC: C7 found across the rail and its return; added to the profile.",
        ])

    def test_a_ki_pida_estimate_still_follows_the_board(self):
        # AC-7: Ki-PIDA's own estimate is redone, not kept: C1 is now 4u7.
        builder = self.written([capacitor("C1", "1u 25V X7R"), capacitor("C2", "1000u"),
                                capacitor("C3", "10u 25V X7R", "Capacitor_SMD:C_1206"),
                                capacitor("C4", "100n 50V X7R", "Capacitor_SMD:C_0603")])
        builder = ACModelBuilder(board(capacitor("C1", "4u7"), capacitor("C2", "1000u"),
                                       capacitor("C3", "10u 25V X7R", "Capacitor_SMD:C_1206"),
                                       capacitor("C4", "100n 50V X7R", "Capacitor_SMD:C_0603")))

        self.resave(builder)

        caps = {cap.ref_des: cap for cap in load_project_config(self.path).ac_profiles["+15V"].capacitors}
        self.assertAlmostEqual(caps["C1"].capacitance_f, 4.7e-6)
        self.assertAlmostEqual(caps["C3"].capacitance_f, 4.5e-6)


if __name__ == "__main__":
    unittest.main()
