import os
import sys
import unittest

plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

from models import ComponentRef, PowerRail, ThermalComponentModel, UnifiedLoad, VoltageRegulator
from thermal_model import PowerLossEstimator, ThermalModelBuilder, merge_component_heat_sources


class TestPowerLossEstimator(unittest.TestCase):
    def _load(self, ref_des, current, thermal_mode="AUTO"):
        return UnifiedLoad(
            component_ref=ComponentRef(ref_des=ref_des),
            total_current=current,
            thermal_mode=thermal_mode,
        )

    def test_switching_regulator_loss(self):
        rail_12v = PowerRail(net_name="12V", nominal_voltage=12.0)
        rail_5v = PowerRail(net_name="5V", nominal_voltage=5.0)
        rail_5v.add_load(self._load("U_LOAD", 1.0))
        rail_12v.add_child_regulator(VoltageRegulator(
            name="BUCK", input_rail_name="12V", input_ref_des="U1",
            input_pad_names=["VIN"],
            output_rail_name="5V", output_ref_des="L1",
            output_pad_names=["VOUT"],
            reg_type="SWITCHING", efficiency=0.8,
        ))

        models = {item.ref_des: item for item in PowerLossEstimator.estimate([rail_12v, rail_5v])}

        self.assertAlmostEqual(models["U_LOAD"].power_w, 5.0)
        self.assertAlmostEqual(models["U1"].power_w, 1.25)
        self.assertNotIn("L1", models)

    def test_linear_regulator_loss(self):
        rail_5v = PowerRail(net_name="5V", nominal_voltage=5.0)
        rail_3v3 = PowerRail(net_name="3V3", nominal_voltage=3.3)
        rail_3v3.add_load(self._load("U_LOAD", 0.5))
        rail_5v.add_child_regulator(VoltageRegulator(
            name="LDO", input_rail_name="5V", input_ref_des="U2",
            input_pad_names=["IN"],
            output_rail_name="3V3", output_ref_des="U2",
            output_pad_names=["OUT"],
            reg_type="LINEAR", efficiency=1.0,
        ))

        models = {item.ref_des: item for item in PowerLossEstimator.estimate([rail_5v, rail_3v3])}

        self.assertAlmostEqual(models["U2"].power_w, 0.85)

    def test_auto_connector_load_is_exported_but_drives_converter_loss(self):
        rail_12v = PowerRail(net_name="12V", nominal_voltage=12.0)
        rail_5v = PowerRail(net_name="5V", nominal_voltage=5.0)
        rail_5v.add_load(self._load("J6", 2.0))
        rail_12v.add_child_regulator(VoltageRegulator(
            name="BUCK", input_rail_name="12V", input_ref_des="U4",
            input_pad_names=["VIN"],
            output_rail_name="5V", output_ref_des="L1",
            output_pad_names=["1"],
            reg_type="SWITCHING", efficiency=0.8,
        ))

        models = {item.ref_des: item for item in PowerLossEstimator.estimate([rail_12v, rail_5v])}

        self.assertEqual(models["J6"].power_w, 0.0)
        self.assertEqual(models["J6"].model_source, "power-tree-external-load")
        self.assertAlmostEqual(models["U4"].power_w, 2.5)

    def test_connector_can_be_marked_as_local_load(self):
        rail = PowerRail(net_name="5V", nominal_voltage=5.0)
        rail.add_load(self._load("J6", 2.0, thermal_mode="LOCAL"))

        models = {item.ref_des: item for item in PowerLossEstimator.estimate([rail])}

        self.assertAlmostEqual(models["J6"].power_w, 10.0)
        self.assertEqual(models["J6"].model_source, "power-tree-load")

    def test_non_connector_can_be_marked_as_external_load(self):
        rail = PowerRail(net_name="5V", nominal_voltage=5.0)
        rail.add_load(self._load("U_REMOTE", 1.0, thermal_mode="EXTERNAL"))

        models = {item.ref_des: item for item in PowerLossEstimator.estimate([rail])}

        self.assertEqual(models["U_REMOTE"].power_w, 0.0)
        self.assertEqual(models["U_REMOTE"].model_source, "power-tree-external-load")

    def test_explicit_regulator_thermal_component_overrides_input(self):
        rail_12v = PowerRail(net_name="12V", nominal_voltage=12.0)
        rail_5v = PowerRail(net_name="5V", nominal_voltage=5.0)
        rail_5v.add_load(self._load("U_LOAD", 1.0))
        rail_12v.add_child_regulator(VoltageRegulator(
            name="BUCK", input_rail_name="12V", input_ref_des="U1",
            input_pad_names=["VIN"],
            output_rail_name="5V", output_ref_des="L1",
            output_pad_names=["1"],
            reg_type="SWITCHING", efficiency=0.8,
            thermal_ref_des="L1",
        ))

        models = {item.ref_des: item for item in PowerLossEstimator.estimate([rail_12v, rail_5v])}

        self.assertAlmostEqual(models["L1"].power_w, 1.25)
        self.assertNotIn("U1", models)

    def test_refresh_keeps_saved_physical_model_for_automatic_source(self):
        estimated = [ThermalComponentModel(
            ref_des="U4", power_w=1.43, model_source="regulator-loss",
        )]
        saved = [ThermalComponentModel(
            ref_des="U4", power_w=1.07, width_mm=3.0, depth_mm=2.0,
            height_mm=1.0, theta_jb_c_per_w=18.7, max_junction_c=125.0,
            enabled=False, model_source="regulator-loss",
            thermal_source="datasheet", thermal_condition="JEDEC 2s2p",
            parameter_provenance={"theta_jb_c_per_w": "TPS552882 SLVSFC5 6.4"},
        )]

        merged = merge_component_heat_sources(estimated, saved)

        self.assertEqual(len(merged), 1)
        self.assertAlmostEqual(merged[0].power_w, 1.43)
        self.assertEqual(merged[0].model_source, "regulator-loss")
        self.assertEqual(
            (merged[0].width_mm, merged[0].depth_mm, merged[0].height_mm),
            (3.0, 2.0, 1.0),
        )
        self.assertAlmostEqual(merged[0].theta_jb_c_per_w, 18.7)
        self.assertEqual(merged[0].max_junction_c, 125.0)
        self.assertFalse(merged[0].enabled)
        # The P02 Rev E sidecar's datasheet thetaJB came back as an estimate.
        self.assertEqual(merged[0].thermal_source, "datasheet")
        self.assertEqual(merged[0].thermal_condition, "JEDEC 2s2p")
        self.assertEqual(merged[0].parameter_provenance, {"theta_jb_c_per_w": "TPS552882 SLVSFC5 6.4"})

    def test_refresh_keeps_fully_manual_heat_source(self):
        estimated = [ThermalComponentModel(
            ref_des="U4", power_w=1.43, model_source="regulator-loss",
        )]
        saved = [ThermalComponentModel(
            ref_des="U4", power_w=0.72, width_mm=4.0, model_source="user",
        )]

        merged = merge_component_heat_sources(estimated, saved)

        self.assertAlmostEqual(merged[0].power_w, 0.72)
        self.assertEqual(merged[0].width_mm, 4.0)
        self.assertEqual(merged[0].model_source, "user")


class EmptyLiveBoardTests(unittest.TestCase):
    """A board whose IPC answers empty must not be solved as bare laminate."""

    class _EmptyBoard:
        def get_nets(self):
            return []

        def get_tracks(self):
            return []

        def get_vias(self):
            return []

        def get_zones(self):
            return []

        def get_footprints(self):
            return []

        def get_shapes(self):
            return []

    def test_a_board_without_copper_is_refused(self):
        # P02 Rev E, 2026-09-22 21:35: KiCad returned 0 tracks, vias, pads and
        # zones while the editor synchronised. AC and the differential pairs
        # refused; the thermal built its mesh from the saved outline alone and
        # reported a 145.8 C hotspot from 0.43 W of copper loss it had nowhere
        # to spread, which the EMC domain then quoted as a fact.
        from models import ThermalAnalysisSettings
        from thermal_model import ThermalModelBuilder

        with self.assertRaises(ValueError) as refusal:
            ThermalModelBuilder(self._EmptyBoard()).build(ThermalAnalysisSettings())

        self.assertIn("no copper geometry", str(refusal.exception))
        self.assertIn("retry", str(refusal.exception))


class FootprintSizeTests(unittest.TestCase):
    def test_a_small_part_keeps_its_own_footprint(self):
        # The heat of an 0402 was spread over the model's 3 x 3 mm default,
        # floored there whatever its pads measured (previous audits' "3 mm
        # package floor").
        from types import SimpleNamespace
        builder = ThermalModelBuilder.__new__(ThermalModelBuilder)
        pad = lambda x: SimpleNamespace(position=SimpleNamespace(x=x, y=0.0))
        footprint = SimpleNamespace(pads=[pad(0.0), pad(1.0)])
        self.assertEqual(builder._footprint_size(footprint, 3.0, 3.0), (1.5, 0.5))
        self.assertEqual(builder._footprint_size(footprint, 3.0, 3.0, user_sized=True), (3.0, 3.0))


class HeatPadTests(unittest.TestCase):
    def test_a_mounting_hole_is_no_heat_pad(self):
        # A module with 2.8 x 2.0 mm pins and 3.2 mm unplated fixing holes:
        # KiCad writes each hole as a pad of its drill's size on every copper
        # layer, the holes outsized the pins, and heat_pads() put the module's
        # whole loss into them -- a 200 C+ hotspot on bare laminate.
        from types import SimpleNamespace
        from extractor import GeometryExtractor
        from thermal_model import heat_pads

        def pad(x_mm, size_mm, shape, pad_type):
            nm = lambda value: value * 1e6
            return SimpleNamespace(
                position=SimpleNamespace(x=nm(x_mm), y=0.0),
                size=SimpleNamespace(x=nm(size_mm[0]), y=nm(size_mm[1])),
                shape=shape, layers=[3, 34], pad_type=pad_type,
            )

        # kipy's PadType: 1 a plated pin, 4 an unplated hole.
        pins = [pad(x, (2.8, 2.0), 1, 1) for x in (0.0, 5.0)]
        holes = [pad(x, (3.2, 3.2), 0, 4) for x in (-5.0, 10.0)]
        footprint = SimpleNamespace(pads=pins + holes)

        builder = ThermalModelBuilder(board=None)
        outlines = builder._joint_outlines(footprint, GeometryExtractor.__new__(GeometryExtractor), {3, 34})
        chosen = heat_pads(outlines)

        self.assertEqual(len(chosen), 2)
        self.assertTrue(all(abs(item.area - 5.6) < 1e-6 for item in chosen))
        # The file's spelling of the same hole, as ingest.board_reader reads it.
        self.assertTrue(builder._is_unplated_hole("np_thru_hole"))
        self.assertFalse(builder._is_unplated_hole("thru_hole"))


class GeometrySignatureTests(unittest.TestCase):
    def test_the_cache_key_follows_the_copper(self):
        # The dialog keeps a thermal mesh while this signature is unchanged,
        # and nothing tested it: a signature that missed an edit would serve
        # a stale result. A via's drill sets its conductance since TH-4 and
        # was not part of it.
        from types import SimpleNamespace

        def board(width=0.25, drill=0.3):
            track = SimpleNamespace(start=SimpleNamespace(x=0.0, y=0.0), end=SimpleNamespace(x=5.0, y=0.0),
                                    layer=0, width=width, net=SimpleNamespace(name="VCC"))
            via = SimpleNamespace(position=SimpleNamespace(x=5.0, y=0.0), diameter=0.6,
                                  drill_diameter=drill, net=SimpleNamespace(name="VCC"))
            return SimpleNamespace(tracks=[track], vias=[via], zones=[], footprints=[], shapes=[])

        signature = ThermalModelBuilder.board_geometry_signature
        self.assertEqual(signature(board()), signature(board()))
        self.assertNotEqual(signature(board()), signature(board(width=0.5)))
        self.assertNotEqual(signature(board()), signature(board(drill=0.4)))


class ViaDiameterTests(unittest.TestCase):
    """TH-4: kipy's Via has no ``width`` attribute, only ``diameter`` and
    ``drill_diameter`` (kipy/board_types.py). thermal_model.py:514 used to
    read ``width``, so every P02 Rev E via (685 of them, real drills
    0.3/0.4 mm) silently fell back to a 0.6 mm default -- understating via
    conductance by x2.07 / x1.53."""

    class _KipyLikeVia:
        """Exposes only drill_diameter, like the real kipy.Via."""

        def __init__(self, drill_diameter_nm):
            self.drill_diameter = drill_diameter_nm

    class _OuterDiameterOnlyVia:
        """A via with no drill_diameter at all (outer diameter only)."""

        def __init__(self, diameter_nm):
            self.diameter = diameter_nm

    def test_reads_drill_diameter_not_width(self):
        builder = ThermalModelBuilder(board=None)
        # 300000 nm = 0.3 mm, P02 Rev E's smaller via drill.
        self.assertAlmostEqual(
            builder._via_diameter_mm(self._KipyLikeVia(300000)), 0.3,
        )

    def test_falls_back_to_outer_diameter_without_a_drill(self):
        builder = ThermalModelBuilder(board=None)
        self.assertAlmostEqual(
            builder._via_diameter_mm(self._OuterDiameterOnlyVia(450000)), 0.45,
        )


if __name__ == "__main__":
    unittest.main()
