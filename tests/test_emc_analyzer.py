from dataclasses import replace
import re
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from shapely.geometry import box
except ImportError:
    box = None

from emc_analyzer import (
    EMCAnalyzer, EMCGeometrySnapshot, EMCTrack, EMCVia, EMCFootprint,
    EMCSourceDiscoverer,
)
from extractor import GeometryExtractor
from models import (
    EMCAnalysisSettings, EMCInductorModel, EMCSignalSource, DifferentialPairCandidate,
    ImpedanceSweepResult, StackupLayerModel, StackupProfile,
)


def stackup():
    return StackupProfile(layers=[
        StackupLayerModel("F.Cu", "COPPER", 0.035, layer_id=0),
        StackupLayerModel("Prepreg", "DIELECTRIC", 0.2, epsilon_r=4.4),
        StackupLayerModel("In1.GND", "COPPER", 0.035, layer_id=1),
        StackupLayerModel("Core", "DIELECTRIC", 1.0, epsilon_r=4.4),
        StackupLayerModel("B.Cu", "COPPER", 0.035, layer_id=31),
    ], source="TEST", trustworthy=True)


@unittest.skipUnless(box is not None, "Shapely is required for geometric EMC tests")
class TestEMCAnalyzer(unittest.TestCase):
    def test_capture_rejects_missing_live_board(self):
        with self.assertRaisesRegex(RuntimeError, "No live KiCad PCB"):
            EMCGeometrySnapshot.capture(None, EMCAnalysisSettings())

    def settings(self):
        return EMCAnalysisSettings(
            sources=[EMCSignalSource("CLK", "SYS_CLK", "CLOCK", 25e6, 2.0),
                     EMCSignalSource("Buck SW", "BUCK_SW", "SWITCHING", 500e3, 5.0)],
        )

    def snapshot(self):
        return EMCGeometrySnapshot(
            bounds_mm=(0.0, 0.0, 100.0, 80.0),
            stackup=stackup(),
            tracks=[
                EMCTrack("SYS_CLK", (5.0, 10.0), (95.0, 10.0), 0.2, 0, 90.0),
                EMCTrack("SYS_CLK", (50.0, 10.0), (55.0, 10.0), 0.2, 31, 5.0),
                EMCTrack("BUCK_SW", (45.0, 40.0), (95.0, 40.0), 3.0, 0, 50.0),
                EMCTrack("USB_D+", (10.0, 30.0), (50.0, 30.0), 0.2, 0, 40.0),
                EMCTrack("USB_D-", (10.0, 30.3), (48.0, 30.3), 0.2, 0, 38.0),
            ],
            vias=[EMCVia("SYS_CLK", (50.0, 10.0), (0, 31)),
                  EMCVia("GND", (70.0, 70.0), (0, 31))],
            footprints=[EMCFootprint("U1", "MCU", (10.0, 10.0)),
                        EMCFootprint("C1", "100n", (18.0, 10.0)),
                        EMCFootprint("J1", "USB", (2.0, 30.0))],
            zones_by_net={"GND": {1: box(0.0, 0.0, 45.0, 80.0)},
                          "BUCK_SW": {0: box(40.0, 35.0, 50.0, 45.0)}},
        )

    def test_hot_loop_rule_flags_a_spread_out_external_fet_stage(self):
        # EMC-4, P02 Rev E: U4's /SYS_5V_SW1 hot loop is Q10 (274.5, 85.6)
        # and Q11 (269.8, 66.1), 20.1 mm apart, with L1 between them; no
        # rule flagged it and SW-002 recommended "IC-to-inductor", wrong for
        # an external-FET controller (U4 drives Q10/Q11, it is not itself
        # the switch).
        snap = EMCGeometrySnapshot(
            bounds_mm=(0.0, 0.0, 300.0, 100.0),
            stackup=stackup(),
            tracks=[EMCTrack("SW1", (269.8, 66.1), (274.5, 85.6), 3.0, 0, 20.06)],
            footprints=[
                EMCFootprint("Q10", "FET", (274.5, 85.6), ("SW1",), (("SW1", 274.5, 85.6),)),
                EMCFootprint("Q11", "FET", (269.8, 66.1), ("SW1",), (("SW1", 269.8, 66.1),)),
                EMCFootprint("L1", "1uH", (272.0, 76.0), ("SW1",), (("SW1", 272.0, 76.0),)),
            ],
        )
        settings = EMCAnalysisSettings(sources=[
            EMCSignalSource("U4 SW1 buck leg", "SW1", "SWITCHING", 400e3, 10.0,
                             voltage_swing_v=20.0, current_a=4.36),
        ])
        result = EMCAnalyzer(snap, settings).analyze()
        by_rule = {finding.rule_id: finding for finding in result.findings}
        self.assertIn("SW-003", by_rule)
        self.assertEqual(by_rule["SW-003"].severity, "HIGH")
        self.assertIn("20.1", by_rule["SW-003"].description)
        self.assertEqual(sorted(by_rule["SW-003"].components), ["L1", "Q10", "Q11"])
        self.assertIn("SW-002", by_rule)
        self.assertIn("FET-to-FET", by_rule["SW-002"].recommendation)
        self.assertNotIn("IC-to-inductor", by_rule["SW-002"].recommendation)

    def test_the_hot_loop_runs_through_the_input_capacitor_not_the_inductor(self):
        # P02 Rev E, 2026-09-29: SW-003 on /SYS_5V_SW1 listed U4, L1, the
        # bootstrap capacitor C43 and R57 but no input capacitor. The loop is
        # C37 -> Q10 -> Q11: C37's supply pad sits near Q10's, its ground pad
        # about 18 mm from Q11's, and the two FETs 21.3 mm apart.
        snap = EMCGeometrySnapshot(
            bounds_mm=(0.0, 0.0, 300.0, 100.0),
            stackup=stackup(),
            footprints=[
                EMCFootprint("Q10", "FET", (274.24, 86.73), ("SW1", "VBUS"),
                             (("VBUS", 274.24, 84.0), ("SW1", 274.24, 89.0))),
                EMCFootprint("Q11", "FET", (271.74, 65.58), ("GND", "SW1"),
                             (("SW1", 271.74, 68.0), ("GND", 271.74, 63.0))),
                EMCFootprint("C37", "22u", (278.95, 80.40), ("GND", "VBUS"),
                             (("VBUS", 278.95, 81.3), ("GND", 278.95, 79.5))),
                EMCFootprint("C43", "100n", (262.52, 70.49), ("BOOT1", "SW1"),
                             (("BOOT1", 262.0, 70.49), ("SW1", 263.0, 70.49))),
                EMCFootprint("L1", "4u7", (269.09, 75.86), ("SW1", "SW2"),
                             (("SW1", 265.0, 75.86), ("SW2", 273.0, 75.86))),
            ],
            power_nets={"VBUS"},
        )
        settings = EMCAnalysisSettings(
            reference_net_names=["GND"],
            sources=[EMCSignalSource("U4 SW1 buck leg", "SW1", "SWITCHING", 400e3, 10.0,
                                     voltage_swing_v=20.0, current_a=4.36)],
        )
        result = EMCAnalyzer(snap, settings).analyze()
        finding = next(item for item in result.findings if item.rule_id == "SW-003")

        self.assertEqual(finding.components, ["C37", "Q10", "Q11"])
        self.assertEqual(finding.severity, "HIGH")
        self.assertIn("ground pad 18.0 mm from Q11's", finding.description)

    def test_detects_plane_void_switching_area_and_missing_return_via(self):
        result = EMCAnalyzer(self.snapshot(), self.settings()).analyze()
        rules = {finding.rule_id for finding in result.findings}
        self.assertIn("GP-001", rules)
        self.assertIn("SW-002", rules)
        self.assertIn("RP-001", rules)
        self.assertLess(result.risk_score, 100)
        self.assertTrue(result.probe_points)

    def test_analyze_refuses_a_capture_with_no_copper_at_all(self):
        # P02 Rev E, 2026-09-22 21:39 (report campaign-...-213933): the PCB
        # editor was still synchronising, IPC returned every collection
        # empty, and the old code scored that as 62/100 -- with the only
        # geometric finding being GP-002 CRITICAL "No ground plane
        # detected" -- against the real board's 61/100 that day.
        empty = EMCGeometrySnapshot(bounds_mm=(0.0, 0.0, 100.0, 80.0), stackup=stackup())
        with self.assertRaisesRegex(ValueError, "no copper geometry"):
            EMCAnalyzer(empty, self.settings()).analyze()

    def test_gp002_needs_a_captured_zone_not_just_layer_count(self):
        # A capture with real tracks/vias/pads but zero zones anywhere (a
        # partial IPC sync) must not read as "no ground plane": zone
        # capture itself has not produced anything to judge yet.
        four_layer = StackupProfile(layers=[
            StackupLayerModel("F.Cu", "COPPER", 0.035, layer_id=0),
            StackupLayerModel("In1.Cu", "COPPER", 0.035, layer_id=1),
            StackupLayerModel("In2.Cu", "COPPER", 0.035, layer_id=2),
            StackupLayerModel("B.Cu", "COPPER", 0.035, layer_id=31),
        ], source="TEST", trustworthy=True)
        snap = EMCGeometrySnapshot(
            bounds_mm=(0.0, 0.0, 100.0, 80.0),
            stackup=four_layer,
            tracks=[EMCTrack("SYS_CLK", (5.0, 10.0), (95.0, 10.0), 0.2, 0, 90.0)],
            vias=[EMCVia("SYS_CLK", (50.0, 10.0), (0, 31))],
            footprints=[EMCFootprint(
                "U1", "MCU", (10.0, 10.0), ("SYS_CLK",), (("SYS_CLK", 10.0, 10.0),),
            )],
            zones_by_net={},
        )
        result = EMCAnalyzer(snap, self.settings()).analyze()
        self.assertNotIn("GP-002", {finding.rule_id for finding in result.findings})

    def test_live_board_capture_converts_ipc_values_before_worker_use(self):
        net = SimpleNamespace(name="SYS_CLK")
        track = SimpleNamespace(
            net=net,
            start=SimpleNamespace(x=1_000_000, y=2_000_000),
            end=SimpleNamespace(x=11_000_000, y=2_000_000),
            width=200_000,
            layer=0,
        )
        offboard_track = SimpleNamespace(
            net=SimpleNamespace(name="STALE"),
            start=SimpleNamespace(x=30_000_000, y=2_000_000),
            end=SimpleNamespace(x=35_000_000, y=2_000_000),
            width=200_000, layer=0,
        )
        pad = SimpleNamespace(net=net, position=SimpleNamespace(x=1_000_000, y=2_000_000))
        footprint = SimpleNamespace(
            reference="U1", value="MCU", position=SimpleNamespace(x=1_000_000, y=2_000_000),
            pads=[pad],
        )
        via = SimpleNamespace(
            net=net, position=SimpleNamespace(x=5_000_000, y=2_000_000),
            padstack=SimpleNamespace(drill=SimpleNamespace(start_layer=3, end_layer=34)),
        )
        board = SimpleNamespace(
            tracks=[track, offboard_track], vias=[via], zones=[], footprints=[footprint],
        )
        settings = self.settings()
        with patch.object(GeometryExtractor, "get_board_bounds", return_value=(0.0, 0.0, 20.0, 10.0)), \
             patch.object(GeometryExtractor, "get_stackup_profile", return_value=stackup()):
            captured = EMCGeometrySnapshot.capture(board, settings)
        self.assertEqual(captured.tracks[0].start, (1.0, 2.0))
        self.assertEqual(captured.tracks[0].end, (11.0, 2.0))
        self.assertEqual(captured.footprints[0].reference, "U1")
        self.assertEqual(captured.vias[0].layer_ids, (3, 34))
        self.assertEqual(captured.ignored_offboard_items, 1)
        self.assertEqual(captured.ignored_offboard_nets, ("STALE",))
        self.assertEqual(captured.ignored_offboard_counts, {"tracks": 1})

    def test_capture_reads_protobuf_value_field_and_saved_board_fallback(self):
        net = SimpleNamespace(name="3V3")
        pad = SimpleNamespace(net=net, position=SimpleNamespace(x=5_000_000, y=5_000_000))
        ipc_field = SimpleNamespace(text=SimpleNamespace(value="IPC_VALUE"))
        footprints = [
            SimpleNamespace(
                reference="U1", value_field=ipc_field,
                position=SimpleNamespace(x=5_000_000, y=5_000_000), pads=[pad],
            ),
            SimpleNamespace(
                reference="U9", position=SimpleNamespace(x=8_000_000, y=5_000_000), pads=[pad],
            ),
        ]
        board = SimpleNamespace(tracks=[], vias=[], zones=[], footprints=footprints)
        with tempfile.TemporaryDirectory() as directory:
            board_path = Path(directory) / "board.kicad_pcb"
            board_path.write_text(
                '(footprint "Package:One"\n'
                '  (property "Reference" "U9")\n'
                '  (property "Value" "LTC4417IUF")\n'
                ')\n',
                encoding="utf-8",
            )
            with patch.object(GeometryExtractor, "get_board_bounds", return_value=(0.0, 0.0, 20.0, 10.0)), \
                 patch.object(GeometryExtractor, "get_stackup_profile", return_value=stackup()):
                captured = EMCGeometrySnapshot.capture(
                    board, self.settings(), board_file_path=board_path,
                )
        by_reference = {item.reference: item.value for item in captured.footprints}
        self.assertEqual(by_reference["U1"], "IPC_VALUE")
        self.assertEqual(by_reference["U9"], "LTC4417IUF")

    def test_differential_skew_and_pdn_result_are_reused(self):
        pair = DifferentialPairCandidate("USB", "USB_D+", "USB_D-", interface="USB_HS")
        ac = ImpedanceSweepResult(
            [1e6], [0.2 + 0j], target_impedance_ohm=0.05,
            worst_frequency_hz=1e6, worst_impedance_ohm=0.2,
        )
        result = EMCAnalyzer(
            self.snapshot(), self.settings(), differential_pairs=[pair],
            ac_results=[("3V3", ac)],
        ).analyze()
        rules = {finding.rule_id for finding in result.findings}
        self.assertIn("DP-001", rules)
        # The AC domain judges the sweep; EMC restates it without scoring it
        # again (P02 Rev E: three failing rails took EMC from 49 to 25).
        pdn = next(finding for finding in result.findings if finding.rule_id == "PD-001")
        self.assertEqual(pdn.severity, "INFO")

    def test_pdn_restatement_quotes_the_impedance_allowed_there(self):
        # P02 Rev E, 2026-09-30 00:09: "/VBUS_SYS: worst impedance 0.2061
        # ohm is within the 0.08889 ohm target", 0.392 ohm being allowed at
        # its 38.3 kHz.
        ac = ImpedanceSweepResult(
            [38.3e3], [0.2061 + 0j], target_impedance_ohm=0.08889,
            worst_frequency_hz=38.3e3, worst_impedance_ohm=0.2061, meets_target=True,
            critical_frequency_hz=38.3e3, critical_impedance_ohm=0.2061,
            critical_allowed_ohm=0.392,
        )
        result = EMCAnalyzer(self.snapshot(), self.settings(), ac_results=[("/VBUS_SYS", ac)]).analyze()
        pdn = next(finding for finding in result.findings if finding.rule_id == "PD-002")
        self.assertIn("within the 0.392 ohm allowed there", pdn.description)

    def test_emission_output_is_explicitly_relative(self):
        result = EMCAnalyzer(self.snapshot(), self.settings()).analyze()
        self.assertTrue(result.frequency_risks)
        self.assertTrue(result.cavity_resonances_hz)
        self.assertTrue(any("cannot certify" in item for item in result.limitations))
        self.assertTrue(result.test_plan)

    def test_a_hot_board_is_noted_not_scored_twice(self):
        # P02 Rev E: TE-001 took 4 EMC points for the 114.7 C hotspot the
        # thermal domain already judges (EMC-9).
        from types import SimpleNamespace
        hot = SimpleNamespace(hotspot=SimpleNamespace(temperature_c=114.7, x_mm=274.8, y_mm=86.6))
        result = EMCAnalyzer(self.snapshot(), self.settings(), thermal_result=hot).analyze()
        te = next(item for item in result.findings if item.rule_id == "TE-001")
        self.assertEqual(te.severity, "INFO")
        self.assertNotIn("TE-001", result.score_penalties_by_rule)
        self.assertFalse(any("editable defaults" in item for item in result.limitations))

    def test_an_entered_converter_current_off_the_power_tree_is_named(self):
        # P02 Rev E: U4's SW1 source was entered at 4.36 A; the power tree's
        # loads give U4 4.726 A, which the AC target used. U14's 3 A agrees.
        from power_tree import ConverterOutput
        snap = self.snapshot()
        snap.converter_outputs = {
            "U4": ConverterOutput("U4 buck-boost 5 V machine", "/SYS_5V_PRE_SENSE", 4.7262, 400e3),
            "U14": ConverterOutput("U14 buck J2 source", "/J2_SRC_VBUS", 3.0, 500e3),
        }
        settings = self.settings()
        settings.sources = [
            EMCSignalSource("U4 SW1 buck leg (Q10/Q11/L1)", "/SYS_5V_SW1", "SWITCHING", 400e3,
                            voltage_swing_v=20.0, current_a=4.36),
            EMCSignalSource("U14 SW (J2 source buck)", "/J2_SRC_SW", "SWITCHING", 500e3,
                            voltage_swing_v=20.0, current_a=3.0),
        ]
        # U14's source revalued from its board circuit carries the 3.87 A
        # inductor peak, not an output current (16:51 run).
        settings.sources[1] = replace(settings.sources[1], current_a=3.87, source="board-circuit")
        result = EMCAnalyzer(snap, settings).analyze()
        (finding,) = [item for item in result.findings if item.rule_id == "SW-004"]
        self.assertEqual(finding.components, ["U4"])
        self.assertIn("U4 SW1 buck leg (Q10/Q11/L1) at 4.36 A entered", finding.description)
        self.assertIn("U4 buck-boost 5 V machine 4.73 A at 400 kHz", finding.description)

    def test_a_capacitor_above_its_dielectric_rating_is_flagged(self):
        # P02 Rev E: C37 (22u 35V X5R) sits 4.4 mm from the 118.99 C hotspot
        # on Q10's drain pad; X5R is rated to 85 C, C63's X7R to 125 C.
        from types import SimpleNamespace
        snap = self.snapshot()
        snap.footprints += [EMCFootprint("C37", "22u 35V X5R", (271.0, 84.0)),
                            EMCFootprint("C63", "22u 10V X7R", (200.0, 50.0))]
        thermal = SimpleNamespace(
            hotspot=SimpleNamespace(temperature_c=118.99, x_mm=273.35, y_mm=86.76),
            placement_temperatures_c={"C37": 96.0, "C63": 96.0, "U1": 60.0},
        )
        snap.footprints += [EMCFootprint("C25", "47u 16V X5R", (240.0, 80.0))]
        thermal.placement_temperatures_c["C25"] = 87.3
        # The 21:42 run: the hotspot moved 4.02 K at twice the grid step,
        # more than several capacitors' excess.
        thermal.mesh_sensitivity = {"fine_grid_mm": 0.2, "coarse_grid_mm": 0.4,
                                    "placement_c": {"C37": (96.0, 93.1), "C25": (87.3, 84.4)}}
        def te002(**changes):
            for key, value in changes.items():
                setattr(thermal, key, value)
            result = EMCAnalyzer(snap, self.settings(), thermal_result=thermal).analyze()
            return result, [item for item in result.findings if item.rule_id == "TE-002"]

        # The 16:51 run: thirteen X5R capacitors over 85 C made thirteen
        # actions and took 24 points off the EMC score; the review of
        # 2026-10-02: C49's 0.7 K was "fix before manufacture" as much as
        # C37's 21.8 K. An excess within the model's uncertainty is to review.
        result, (fix, review) = te002()
        self.assertEqual((fix.severity, fix.components), ("HIGH", ["C37"]))
        self.assertIn("1 X5R capacitor(s), rated to 85 C", fix.description)
        self.assertIn("C37 (22u 35V X5R) 96.0 C, +11.0 K.", fix.description)
        self.assertEqual((review.severity, review.components), ("MEDIUM", ["C25"]))
        self.assertIn("C25 (47u 16V X5R) 87.3 C, +2.3 K against 2.9 K.", review.description)
        # 22:29: with no capacitor within it, the report said nothing.
        thermal.mesh_sensitivity["placement_c"]["C25"] = (87.3, 86.9)
        result, (fix,) = te002()
        self.assertEqual(fix.components, ["C37", "C25"])
        self.assertTrue(fix.description.endswith(
            "Each excess is larger than the model's uncertainty under the capacitor, 2.9 K at most "
            "(C37): the move of the board under each between the 0.2 and 0.4 mm thermal grids."))
        # The default wall coefficient adds 2.7 K under every capacitor.
        result, (fix, review) = te002(ambient_uncertainty_k=2.7)
        self.assertEqual((fix.components, review.components), (["C37"], ["C25"]))
        self.assertIn("+2.3 K against 3.1 K", review.description)
        self.assertIn("the 2.7 K the internal air would drop at 1 W/m2K more", review.description)
        self.assertNotIn("TE-002", result.score_penalties_by_rule)
        te001 = next(item for item in result.findings if item.rule_id == "TE-001")
        self.assertEqual(te001.components, ["C37", "C63", "C25"])

    def test_a_track_on_its_own_pour_counts_once_in_the_switch_node_area(self):
        # P02 Rev E's /J2_SRC_SW read 41.8 mm2 for 31.8 mm2 of copper,
        # because tracks lying on the pour were added to it (EMC-7).
        from types import SimpleNamespace
        from shapely.geometry import box
        from emc_analyzer import EMCTrack
        analyzer = EMCAnalyzer.__new__(EMCAnalyzer)
        analyzer.snapshot = SimpleNamespace(zones_by_net={"SW": {0: box(0, 0, 10, 2)}})
        track = EMCTrack("SW", (1.0, 1.0), (9.0, 1.0), 0.5, 0, 8.0)
        self.assertAlmostEqual(analyzer._copper_area_mm2("SW", [track]), 20.0, places=6)

    def test_sources_on_the_default_current_are_named_and_findings_carry_provenance(self):
        # The 0.1 A default current was assumed and never said, and a
        # finding about a source did not say where its parameters came from
        # (previous audits' items).
        result = EMCAnalyzer(self.snapshot(), self.settings()).analyze()
        assumed = next(item for item in result.limitations if "assumed 0.1 A" in item)
        self.assertIn("CLK", assumed)
        switching = [item for item in result.findings if item.rule_id == "SW-001"]
        self.assertTrue(all("Source parameters:" in item.description for item in switching))

    def test_jst_and_df_harness_connectors_are_recognised(self):
        # The pattern held literal backspace characters where  was meant,
        # so no JST or DF connector ever matched (found by the 2026-09-23
        # fixes); an underscore after JST must not break the match either.
        from emc_analyzer import _HARNESS_CONNECTOR_RE
        for value in ("JST_PH_S4B", "Connector_JST:JST_XH_B2B", "DF13-6P"):
            self.assertTrue(_HARNESS_CONNECTOR_RE.search(value), value)
        self.assertFalse(_HARNESS_CONNECTOR_RE.search("ADJUSTER"))

    def test_source_scan_rejects_switched_dc_rail_and_uses_verified_frequency(self):
        sources = EMCSourceDiscoverer.discover(
            {"VBUS_PD_SW", "/P02 Alimentation/U4_SW", "Net-(U5-SW)", "USB_HOST_D_+"},
            switching_frequencies={"U4": {
                "frequency_hz": 600e3, "voltage_swing_v": 12.0, "current_a": 4.2,
            }, "U5": 650e3},
        )
        by_net = {source.net_name: source for source in sources}
        self.assertNotIn("VBUS_PD_SW", by_net)
        self.assertEqual(by_net["/P02 Alimentation/U4_SW"].frequency_hz, 600e3)
        self.assertAlmostEqual(by_net["/P02 Alimentation/U4_SW"].voltage_swing_v, 12.0)
        self.assertAlmostEqual(by_net["/P02 Alimentation/U4_SW"].current_a, 4.2)
        self.assertEqual(by_net["/P02 Alimentation/U4_SW"].source, "power-tree")
        self.assertEqual(by_net["Net-(U5-SW)"].frequency_hz, 650e3)
        self.assertAlmostEqual(by_net["USB_HOST_D_+"].voltage_swing_v, 0.4)
        self.assertAlmostEqual(by_net["USB_HOST_D_+"].current_a, 0.008)

    def test_a_rescan_keeps_the_frequency_a_saved_source_states(self):
        # P02 Rev E saved /USB_J2_MUX_D at 240 MHz. The pair's interface says
        # only "USB", the rescan dropped the value, and EE-003 then reported
        # the frequency unknown.
        saved = EMCSignalSource(
            "/USB_J2_MUX_D", "/USB_J2_MUX_D+", "DIFFERENTIAL", 240e6, 0.8,
            source="differential-scan", negative_net_name="/USB_J2_MUX_D-",
        )
        pair = DifferentialPairCandidate("/USB_J2_MUX_D", "/USB_J2_MUX_D+", "/USB_J2_MUX_D-", interface="USB")
        sources = EMCSourceDiscoverer.discover(
            {"/USB_J2_MUX_D+", "/USB_J2_MUX_D-"}, existing=[saved], differential_pairs=[pair],
        )
        self.assertEqual([(s.net_name, s.frequency_hz) for s in sources], [("/USB_J2_MUX_D+", 240e6)])

    def test_a_switch_node_is_known_by_its_parts_and_an_open_pin_is_not(self):
        # Nets of the P02 Rev E power board. Its switch nodes carry no Ux_SW
        # name, and the U6 module's internal SW pin, left open, is a net KiCad
        # names unconnected-(U6-SW-Pad5). A switched rail joins no inductor.
        from emc_analyzer import switch_node_evidence

        def part(reference, *nets):
            return SimpleNamespace(reference=reference, pads=[SimpleNamespace(net_name=net) for net in nets])

        switch_nodes = switch_node_evidence([
            part("U14", "/J2_SRC_SW", "/VBUS_SYS", "GND"),
            part("L3", "/J2_SRC_SW", "/J2_SRC_VBUS"),
            part("U3", "/VBUS_PD", "/VBUS_PD_SW"),
            part("U6", "unconnected-(U6-SW-Pad5)", "/+3V3AO"),
        ])
        self.assertEqual(switch_nodes, {"/J2_SRC_SW": ("U14", "L3")})
        sources = {source.net_name: source for source in EMCSourceDiscoverer.discover(
            {"/J2_SRC_SW", "/VBUS_PD_SW", "unconnected-(U6-SW-Pad5)"},
            switching_frequencies={"U14": {"frequency_hz": 500e3, "voltage_swing_v": 20.0, "current_a": 3.0}},
            switch_nodes=switch_nodes,
        )}
        self.assertEqual(set(sources), {"/J2_SRC_SW"})
        self.assertEqual(sources["/J2_SRC_SW"].frequency_hz, 500e3)
        self.assertEqual(sources["/J2_SRC_SW"].source, "power-tree")

    def test_a_source_without_a_loss_model_swings_to_the_rail_powering_its_parts(self):
        # A flat 3.3 V used to stand in for every such source. The oscillator
        # on MCLK is powered from +1V8; the MCU it drives carries +1V8 and
        # +3V3, so the highest configured rail on the net is +3V3.
        from emc_analyzer import supply_voltages_by_net

        def part(reference, *nets):
            return SimpleNamespace(reference=reference, pads=[SimpleNamespace(net_name=net) for net in nets])

        rails = [
            SimpleNamespace(net_name="+3V3", nominal_voltage=3.3),
            SimpleNamespace(net_name="+1V8", nominal_voltage=1.8),
            SimpleNamespace(net_name="+12V", nominal_voltage=12.0),
        ]
        footprints = [
            part("X1", "+1V8", "GND", "MCLK"),
            part("U1", "+3V3", "+1V8", "GND", "MCLK", "ETH_TX"),
            part("U9", "+12V", "GND", "Net-(U9-SW)"),
            part("R5", "NC_A", "NC_B"),
        ]
        supplies = supply_voltages_by_net(footprints, rails)
        self.assertEqual(supplies["MCLK"][0], 3.3)
        self.assertIn("+3V3", supplies["MCLK"][1])
        self.assertIn("U1", supplies["MCLK"][1])
        self.assertNotIn("+3V3", supplies)
        self.assertNotIn("NC_A", supplies)

        sources = {source.net_name: source for source in EMCSourceDiscoverer.discover(
            {"MCLK", "ETH_TX", "Net-(U9-SW)", "USB_D_P", "SPI_CLK"},
            switching_frequencies={"U9": 500e3}, supply_voltages=supplies,
        )}
        self.assertAlmostEqual(sources["MCLK"].voltage_swing_v, 3.3)
        self.assertIn("+3V3", sources["MCLK"].parameter_notes)
        self.assertAlmostEqual(sources["Net-(U9-SW)"].voltage_swing_v, 12.0)
        self.assertIn("assumed", sources["Net-(U9-SW)"].parameter_notes)
        self.assertAlmostEqual(sources["Net-(U9-SW)"].current_a, 0.1)
        self.assertAlmostEqual(sources["ETH_TX"].voltage_swing_v, 3.3)
        self.assertIn("+3V3", sources["ETH_TX"].parameter_notes)
        # USB levels come from the standard, and a net no rail reaches keeps
        # the default and says it is one.
        self.assertAlmostEqual(sources["USB_D_P"].voltage_swing_v, 0.4)
        self.assertAlmostEqual(sources["SPI_CLK"].voltage_swing_v, 3.3)
        self.assertIn("defaults", sources["SPI_CLK"].parameter_notes)

    def test_a_source_frequency_comes_from_the_parts_or_is_unknown(self):
        from emc_analyzer import declared_frequencies

        def part(reference, value, nets, lib_id="", **fields):
            return SimpleNamespace(reference=reference, value=value, lib_id=lib_id, extra_fields=fields,
                                   nets_by_pad={str(i): net for i, net in enumerate(nets, start=1)})

        by_net, by_part = declared_frequencies([
            part("Y1", "25MHz", ["XTAL_IN", "XTAL_OUT"]),
            part("X2", "SG-8018CA", ["+3V3", "GND", "OSC_CLK"], lib_id="Oscillator:SG-8018", Frequency="12M"),
            part("U4", "TPS54560", ["Net-(U4-SW)", "+12V"], Fsw="600 kHz"),
            part("U1", "STM32", ["MCU_CLK", "+3V3"]),
            part("Y3", "32.768KHz", ["/XI", "/XO"]),
        ], rail_nets={"+3V3", "GND", "+12V"})
        self.assertEqual({net: hz for net, (hz, _where) in by_net.items()},
                         {"XTAL_IN": 25e6, "XTAL_OUT": 25e6, "OSC_CLK": 12e6, "/XI": 32768.0, "/XO": 32768.0})
        self.assertEqual(by_part["U4"][0], 600e3)

        pair = DifferentialPairCandidate("USB_D", "USB_D_P", "USB_D_N", interface="USB")
        sources = {source.net_name: source for source in EMCSourceDiscoverer.discover(
            {"XTAL_IN", "MCU_CLK", "/P02/U4_SW", "Net-(U7-SW)", "ETH_TX", "USB_D_P", "USB_D_N"},
            differential_pairs=[pair], net_frequencies=by_net, part_frequencies=by_part,
        )}
        self.assertEqual(sources["XTAL_IN"].frequency_hz, 25e6)
        self.assertIn("the value of Y1 (25MHz)", sources["XTAL_IN"].parameter_notes)
        self.assertEqual(sources["/P02/U4_SW"].frequency_hz, 600e3)
        # Nothing states these: no 25 MHz, 500 kHz, 100 MHz or 240 MHz stands in.
        for net in ("MCU_CLK", "Net-(U7-SW)", "ETH_TX", "USB_D_P"):
            self.assertEqual(sources[net].frequency_hz, 0.0, net)
            self.assertIn("Frequency unknown", sources[net].parameter_notes, net)
        # Y3's nets match no clock name; its declared frequency makes it one source.
        self.assertEqual(sources["/XI"].frequency_hz, 32768.0)
        self.assertNotIn("/XO", sources)
        # Y1 already has a named source on XTAL_IN, so it adds none on XTAL_OUT.
        self.assertNotIn("XTAL_OUT", sources)

    def test_an_unknown_source_frequency_is_one_finding_not_harmonics(self):
        settings = self.settings()
        settings.sources.append(EMCSignalSource("MCU clock", "MCU_CLK", "CLOCK", 0.0, 2.0))
        result = EMCAnalyzer(self.snapshot(), settings).analyze()
        unknown = [finding for finding in result.findings if finding.rule_id == "EE-003"]
        self.assertEqual(len(unknown), 1)
        self.assertIn("MCU clock", unknown[0].description)
        self.assertFalse(any(risk.source_name == "MCU clock" for risk in result.frequency_risks))

    def test_source_scan_collapses_confirmed_differential_pair(self):
        pair = DifferentialPairCandidate(
            "USB_HOST_D", "USB_HOST_D_+", "USB_HOST_D_-", interface="USB_HS",
        )
        sources = EMCSourceDiscoverer.discover(
            {"USB_HOST_D_+", "USB_HOST_D_-"}, differential_pairs=[pair],
        )
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].kind, "DIFFERENTIAL")
        self.assertEqual(sources[0].frequency_hz, 240e6)
        self.assertEqual(sources[0].net_name, "USB_HOST_D_+")
        self.assertEqual(sources[0].negative_net_name, "USB_HOST_D_-")

    def test_slow_net_void_and_single_layer_via_do_not_create_findings(self):
        snap = self.snapshot()
        snap.tracks.append(EMCTrack("I2C_SDA", (60.0, 20.0), (90.0, 20.0), 0.2, 0, 30.0))
        snap.vias.append(EMCVia("USB_D+", (20.0, 30.0), (0, 31)))
        result = EMCAnalyzer(snap, self.settings()).analyze()
        self.assertFalse(any(
            finding.rule_id == "GP-001" and "I2C_SDA" in finding.nets
            for finding in result.findings
        ))
        self.assertFalse(any(
            finding.rule_id == "RP-001" and "USB_D+" in finding.nets
            for finding in result.findings
        ))

    def test_outer_pair_with_continuous_reference_does_not_trigger_dp004(self):
        snap = self.snapshot()
        snap.zones_by_net["GND"] = {1: box(0.0, 0.0, 100.0, 80.0)}
        pair = DifferentialPairCandidate("USB", "USB_D+", "USB_D-", interface="USB_HS")
        result = EMCAnalyzer(snap, self.settings(), differential_pairs=[pair]).analyze()
        self.assertFalse(any(finding.rule_id == "DP-004" for finding in result.findings))

    def test_coplanar_ground_closes_adjacent_plane_gaps_for_dp004(self):
        snap = self.snapshot()
        snap.zones_by_net["GND"] = {
            1: box(0.0, 0.0, 30.0, 80.0),
            0: box(0.0, 29.45, 100.0, 29.70).union(
                box(0.0, 30.60, 100.0, 30.85)
            ),
        }
        pair = DifferentialPairCandidate("USB", "USB_D+", "USB_D-", interface="USB_HS")
        result = EMCAnalyzer(
            snap, self.settings(), differential_pairs=[pair], differential_results={},
        ).analyze()
        self.assertFalse(any(finding.rule_id == "DP-004" for finding in result.findings))

    def test_dp004_reports_longest_uncovered_interval_coordinates(self):
        snap = self.snapshot()
        pair = DifferentialPairCandidate("USB", "USB_D+", "USB_D-", interface="USB_HS")
        result = EMCAnalyzer(snap, self.settings(), differential_pairs=[pair]).analyze()
        finding = next(item for item in result.findings if item.rule_id == "DP-004")
        self.assertIn("Longest uncovered interval", finding.description)
        self.assertTrue(any("Uncovered return interval" in item.detail for item in finding.evidence))
        self.assertTrue(all(item.x_mm is not None and item.y_mm is not None for item in finding.evidence))

    def test_dp004_short_local_gap_is_low_not_global_plane_failure(self):
        snap = EMCGeometrySnapshot(
            bounds_mm=(0.0, 0.0, 10.0, 10.0), stackup=stackup(),
            tracks=[
                EMCTrack("USB_D+", (1.0, 4.0), (5.0, 4.0), 0.15, 0, 4.0),
                EMCTrack("USB_D-", (1.0, 4.3), (5.0, 4.3), 0.15, 0, 4.0),
            ],
            zones_by_net={
                "GND": {1: box(0.0, 0.0, 2.6, 10.0).union(box(3.1, 0.0, 10.0, 10.0))},
            },
        )
        pair = DifferentialPairCandidate("USB", "USB_D+", "USB_D-", interface="USB_HS")
        result = EMCAnalyzer(snap, self.settings(), differential_pairs=[pair]).analyze()
        finding = next(item for item in result.findings if item.rule_id == "DP-004")
        self.assertEqual(finding.severity, "LOW")
        self.assertIn("Localized", finding.title)

    def test_a_pair_s_return_gap_is_reported_once(self):
        # P02 Rev E: USB_HOST_D+ drew GP-001 (HIGH, 78.7 % coverage) and its
        # pair DP-004 (MEDIUM, 89.6 %) for the same gap by the board edge.
        snap = EMCGeometrySnapshot(
            bounds_mm=(0.0, 0.0, 10.0, 10.0), stackup=stackup(),
            tracks=[
                EMCTrack("USB_D+", (1.0, 4.0), (9.0, 4.0), 0.15, 0, 8.0),
                EMCTrack("USB_D-", (1.0, 4.3), (9.0, 4.3), 0.15, 0, 8.0),
            ],
            zones_by_net={"GND": {1: box(0.0, 0.0, 3.0, 10.0).union(box(5.0, 0.0, 10.0, 10.0))}},
        )
        settings = EMCAnalysisSettings(sources=[EMCSignalSource(
            "USB", "USB_D+", "DIFFERENTIAL", 240e6, 0.8, negative_net_name="USB_D-",
        )])
        pair = DifferentialPairCandidate("USB", "USB_D+", "USB_D-", interface="USB_HS")
        rules = {f.rule_id for f in EMCAnalyzer(snap, settings, differential_pairs=[pair]).analyze().findings}
        self.assertIn("DP-004", rules)
        self.assertNotIn("GP-001", rules)
        alone = {f.rule_id for f in EMCAnalyzer(snap, settings).analyze().findings}
        self.assertIn("GP-001", alone)

    def test_high_order_low_drive_switching_harmonic_is_low_severity(self):
        settings = EMCAnalysisSettings(sources=[
            EMCSignalSource(
                "U5_SW", "BUCK_SW", "SWITCHING", 650e3, 10.0,
                voltage_swing_v=5.0, current_a=0.295, source="power-tree",
            ),
        ])
        result = EMCAnalyzer(self.snapshot(), settings).analyze()
        finding = next(item for item in result.findings if item.rule_id == "SW-001")
        self.assertEqual(finding.severity, "LOW")
        self.assertIn("dB relative", finding.description)

    def test_power_management_decoupling_is_not_high_confidence_digital_rule(self):
        snap = EMCGeometrySnapshot(
            bounds_mm=(0.0, 0.0, 30.0, 20.0), stackup=stackup(), power_nets={"12V"},
            footprints=[
                EMCFootprint("U9", "LTC4417IUF", (5.0, 5.0), ("12V", "GND"),
                             (("12V", 5.0, 5.0),)),
                EMCFootprint("C27", "22uF", (15.0, 5.0), ("12V", "GND"),
                             (("12V", 15.0, 5.0),)),
            ],
        )
        result = EMCAnalyzer(snap, EMCAnalysisSettings(sources=[])).analyze()
        finding = next(item for item in result.findings if item.rule_id == "DE-001")
        self.assertEqual(finding.severity, "MEDIUM")
        self.assertEqual(finding.confidence, "LOW")
        self.assertIn("rail-bypass", finding.title)

    def test_an_i2c_only_ic_keeps_a_distant_bypass_at_medium(self):
        # P02 Rev E: the EEPROM U18 (SDA/SCL/WP) with C13 22.9 mm away ranked
        # the campaign's first HIGH action, on the same bar as a processor.
        def snap(signal_nets):
            return EMCGeometrySnapshot(
                bounds_mm=(0.0, 0.0, 40.0, 20.0), stackup=stackup(), power_nets={"LDO3V3"},
                footprints=[
                    EMCFootprint("U18", "CAT24C512", (5.0, 5.0), ("GND", "LDO3V3") + signal_nets,
                                 (("LDO3V3", 5.0, 5.0),)),
                    EMCFootprint("C13", "100n", (27.9, 5.0), ("GND", "LDO3V3"),
                                 (("LDO3V3", 27.9, 5.0),)),
                ],
            )

        eeprom = ("/J2_EEPROM_SCL", "/J2_EEPROM_SDA", "/J2_EEPROM_WP")
        finding = next(item for item in EMCAnalyzer(snap(eeprom), EMCAnalysisSettings(sources=[]))
                       .analyze().findings if item.rule_id == "DE-001")
        self.assertEqual(finding.severity, "MEDIUM")
        self.assertIn("I2C/SMBus", finding.description)
        clocked = next(item for item in EMCAnalyzer(snap(eeprom + ("SPI_CLK",)), EMCAnalysisSettings(sources=[]))
                       .analyze().findings if item.rule_id == "DE-001")
        self.assertEqual(clocked.severity, "HIGH")

    def test_power_planes_are_no_signal_layers(self):
        # P02 Rev E: PWR.L3 and PWR.L4 carry the rails' pours and no track, and
        # SU-001 (HIGH) called them adjacent signal layers, SU-002 twice far
        # from a ground plane.
        names = ("F.Signal", "GND.L2", "PWR.L3", "PWR.L4", "GND.L5", "B.Signal")
        ids = (3, 4, 5, 6, 7, 34)
        dielectrics = (0.0994, 0.35, 0.1088, 0.35, 0.0994)
        layers = []
        for index, (name, layer_id) in enumerate(zip(names, ids)):
            layers.append(StackupLayerModel(name, "COPPER", 0.035, layer_id=layer_id))
            if index < len(dielectrics):
                layers.append(StackupLayerModel("FR4", "DIELECTRIC", dielectrics[index], epsilon_r=4.4))
        plane = box(0.0, 0.0, 20.0, 20.0)

        def stackup_findings(tracks):
            snap = EMCGeometrySnapshot(
                bounds_mm=(0.0, 0.0, 20.0, 20.0),
                stackup=StackupProfile(layers=layers, source="TEST", trustworthy=True),
                tracks=tracks, power_nets={"+5V"},
                zones_by_net={"GND": {4: plane, 7: plane}, "+5V": {5: plane, 6: plane}},
            )
            findings = EMCAnalyzer(snap, EMCAnalysisSettings(sources=[])).analyze().findings
            return sorted(f.rule_id for f in findings if f.rule_id.startswith("SU-"))

        outer = [EMCTrack("SDA", (1.0, 1.0), (9.0, 1.0), 0.2, 3, 8.0),
                 EMCTrack("SCL", (1.0, 2.0), (9.0, 2.0), 0.2, 34, 8.0)]
        self.assertEqual(stackup_findings(outer), [])
        # A rail routed on the planes (P02's /J2_VBUS_FUSED) is no signal.
        rail = [EMCTrack("+5V", (1.0, 5.0), (9.0, 5.0), 0.5, layer, 8.0) for layer in (5, 6)]
        self.assertEqual(stackup_findings(outer + rail), [])
        inner = [EMCTrack("CLK", (1.0, 3.0), (9.0, 3.0), 0.2, 5, 8.0),
                 EMCTrack("DATA", (1.0, 4.0), (9.0, 4.0), 0.2, 6, 8.0)]
        self.assertIn("SU-001", stackup_findings(outer + inner))

    def test_a_tvs_is_no_part_to_decouple(self):
        # P02 Rev E: the TVS2200 on VBUS_PD types its ground pin power_in, and
        # DE-001 (HIGH) asked for C1, 24 mm away, to move closer to it.
        snap = EMCGeometrySnapshot(
            bounds_mm=(0.0, 0.0, 40.0, 20.0), stackup=stackup(), power_nets={"VBUS_PD"},
            footprints=[
                EMCFootprint("D1", "TVS2200DRVR", (5.0, 5.0), ("VBUS_PD", "GND"),
                             (("VBUS_PD", 5.0, 5.0),)),
                EMCFootprint("C1", "4.7u 50V X7R", (29.0, 5.0), ("VBUS_PD", "GND"),
                             (("VBUS_PD", 29.0, 5.0),)),
            ],
            component_classes={"D1": {"powered"}},
        )
        findings = EMCAnalyzer(snap, EMCAnalysisSettings(sources=[])).analyze().findings
        self.assertFalse([f.rule_id for f in findings if f.rule_id.startswith("DE-")])

    def test_parts_are_classified_by_what_the_project_files_say(self):
        from ingest.declarations import component_classes
        from ingest.netlist_builder import ComponentInfo

        def component(reference, symbol, pins, footprint=""):
            return ComponentInfo(reference, "", footprint, "", {}, (0, 0), {}, pins, symbol_lib_id=symbol)

        netlist = SimpleNamespace(components=[
            component("M1", "Module:ESP32", {"1": "power_in"}),          # a module needs decoupling
            component("CX1", "Device:C", {"1": "passive"}),              # a capacitor by its symbol
            component("C9", "Connector:Conn_01x02", {"1": "power_in"}),  # a connector, not a part to decouple
        ])
        classes = component_classes(netlist)
        self.assertEqual(classes, {"M1": {"powered"}, "CX1": {"capacitor"}, "C9": {"connector", "powered"}})

        snap = EMCGeometrySnapshot(
            bounds_mm=(0.0, 0.0, 30.0, 10.0), stackup=stackup(), tracks=[], vias=[],
            footprints=[
                EMCFootprint("M1", "ESP32", (5.0, 5.0), ("3V3", "GND"), (("3V3", 5.0, 5.0),)),
                EMCFootprint("C9", "Conn", (25.0, 5.0), ("3V3", "GND"), (("3V3", 25.0, 5.0),)),
            ],
            power_nets={"3V3"}, component_classes=classes,
        )
        findings = EMCAnalyzer(snap, EMCAnalysisSettings(sources=[])).analyze().findings
        # M1 is no U or IC, and was never checked; its nearest "capacitor" C9 is a connector.
        self.assertEqual([(f.rule_id, f.components) for f in findings if f.rule_id.startswith("DE-")],
                         [("DE-002", ["M1"])])

    def test_connected_usblc_value_suppresses_connector_protection_false_positive(self):
        snap = EMCGeometrySnapshot(
            bounds_mm=(0.0, 0.0, 30.0, 20.0), stackup=stackup(),
            footprints=[
                EMCFootprint("J1", "USB4730-GF-A-KIT", (2.0, 10.0),
                             ("USB_D+", "USB_D-", "VBUS", "GND")),
                EMCFootprint("U7", "USBLC6-2SC6", (5.0, 10.0),
                             ("USB_D+", "USB_D-", "GND")),
            ],
            power_nets={"VBUS"},
        )
        result = EMCAnalyzer(snap, EMCAnalysisSettings(sources=[])).analyze()
        self.assertFalse(any(item.rule_id in {"ES-001", "IO-001"} for item in result.findings))

    def test_a_cable_header_is_not_taken_for_an_external_port(self):
        # P02 Rev E: J8, a Pico-Clasp header labelled "USB HOST", takes USB
        # through a mux to the main board; it drew ES-001, IO-001 and SH-001.
        snap = EMCGeometrySnapshot(
            bounds_mm=(0.0, 0.0, 40.0, 20.0), stackup=stackup(),
            footprints=[
                EMCFootprint("J8", "MOLEX PICO-CLASP 1x04 USB HOST", (2.0, 10.0),
                             ("USB_HOST_D+", "USB_HOST_D-", "GND")),
                EMCFootprint("U10", "TS3USB221ARSER", (8.0, 10.0),
                             ("USB_HOST_D+", "USB_HOST_D-", "GND")),
            ],
        )
        findings = EMCAnalyzer(snap, EMCAnalysisSettings(sources=[])).analyze().findings
        self.assertEqual(sorted(f.rule_id for f in findings if f.rule_id[:2] in {"ES", "IO", "SH"}), ["IO-002"])

    def test_a_far_esd_filter_is_named_rather_than_called_missing(self):
        # P02 Rev E: J1's USB lines pass U2, an ECMF02 filter with ESD, 17 mm away.
        snap = EMCGeometrySnapshot(
            bounds_mm=(0.0, 0.0, 40.0, 20.0), stackup=stackup(),
            footprints=[
                EMCFootprint("J1", "USB4730-GF-A-KIT", (2.0, 10.0), ("USB_D+", "USB_D-", "GND")),
                EMCFootprint("U2", "ECMF02-2AMX6", (19.0, 10.0), ("USB_D+", "USB_D-", "GND")),
            ],
        )
        findings = EMCAnalyzer(snap, EMCAnalysisSettings(sources=[])).analyze().findings
        esd = next(f for f in findings if f.rule_id == "ES-001")
        self.assertEqual(esd.severity, "MEDIUM")
        self.assertIn("U2 (ECMF02-2AMX6)", esd.description)

    def test_unconnected_nearby_protector_does_not_hide_missing_esd_path(self):
        snap = EMCGeometrySnapshot(
            bounds_mm=(0.0, 0.0, 30.0, 20.0), stackup=stackup(),
            footprints=[
                EMCFootprint("J1", "USB Type-C", (2.0, 10.0), ("USB_D+", "USB_D-")),
                EMCFootprint("U7", "USBLC6-2SC6", (3.0, 10.0), ("OTHER+", "OTHER-", "GND")),
            ],
        )
        result = EMCAnalyzer(snap, EMCAnalysisSettings(sources=[])).analyze()
        self.assertTrue(any(item.rule_id == "ES-001" for item in result.findings))
        self.assertTrue(any(item.rule_id == "IO-001" for item in result.findings))

    def test_score_exposes_per_rule_deductions(self):
        result = EMCAnalyzer(self.snapshot(), self.settings()).analyze()
        self.assertTrue(result.score_penalties_by_rule)
        self.assertEqual(result.risk_score, 100 - sum(result.score_penalties_by_rule.values()))

    def test_shielded_inductor_without_curve_is_disclosed_without_score_penalty(self):
        snap = self.snapshot()
        snap.footprints.append(EMCFootprint("L1", "2.2uH", (45.0, 40.0)))
        snap.inductors = [EMCInductorModel(
            "L1", mpn="SPM6530T-2R2M", output_current_a=4.0,
            ripple_current_pp_a=2.2, isat_a=8.4, shield_state="SHIELDED",
            parameter_source="datasheet", parameter_confidence="HIGH",
        )]
        result = EMCAnalyzer(snap, self.settings()).analyze()
        finding = next(item for item in result.findings if item.rule_id == "IN-001")
        self.assertEqual(finding.severity, "INFO")
        self.assertNotIn("IN-001", result.score_penalties_by_rule)
        self.assertFalse(any(item.rule_id == "IN-004" for item in result.findings))

    def test_decoupling_requires_shared_supply_rail_and_uses_pad_distance(self):
        snap = EMCGeometrySnapshot(
            bounds_mm=(0.0, 0.0, 30.0, 20.0), stackup=stackup(), power_nets={"3V3"},
            footprints=[
                EMCFootprint("U1", "MCU", (5.0, 5.0), ("3V3", "GND"),
                             (("3V3", 5.0, 5.0),)),
                EMCFootprint("C1", "100n", (25.0, 5.0), ("5V", "GND"),
                             (("5V", 25.0, 5.0),)),
                EMCFootprint("C2", "100n", (6.0, 5.0), ("3V3", "GND"),
                             (("3V3", 6.0, 5.0),)),
            ],
        )
        result = EMCAnalyzer(snap, EMCAnalysisSettings(sources=[])).analyze()
        self.assertFalse(any(finding.rule_id.startswith("DC-") for finding in result.findings))


@unittest.skipUnless(box is not None, "Shapely is required for geometric EMC tests")
class TestQuantifiedCrosstalk(unittest.TestCase):
    """XT-001 compared a spacing to a 3H threshold and reported nothing about
    coupling.  It now solves the even/odd modes of the cross-section, so the
    finding carries a percentage of the aggressor swing."""

    def settings(self, rise_time_ns=2.0):
        return EMCAnalysisSettings(
            sources=[EMCSignalSource("CLK", "SYS_CLK", "CLOCK", 25e6, rise_time_ns)],
        )

    def snapshot(self, separation_mm, victim="VICTIM", layers=None):
        return EMCGeometrySnapshot(
            bounds_mm=(0.0, 0.0, 100.0, 80.0),
            stackup=layers if layers is not None else stackup(),
            tracks=[
                EMCTrack("SYS_CLK", (5.0, 10.0), (55.0, 10.0), 0.2, 0, 50.0),
                EMCTrack(victim, (5.0, 10.0 + separation_mm),
                         (55.0, 10.0 + separation_mm), 0.2, 0, 50.0),
            ],
            vias=[], footprints=[],
            zones_by_net={"GND": {1: box(0.0, 0.0, 100.0, 80.0)}},
        )

    def crosstalk(self, snapshot, settings=None):
        result = EMCAnalyzer(snapshot, settings or self.settings()).analyze()
        return next(
            (item for item in result.findings if item.rule_id == "XT-001"), None,
        )

    def coupling_percent(self, finding):
        """The leading '<x> % into <victim>' figure of a quantified finding."""
        match = re.search(r"couples ([\d.]+) %", finding.description)
        self.assertIsNotNone(match, f"not a quantified finding: {finding.description}")
        return float(match.group(1))

    def test_closer_traces_couple_more_and_the_figure_is_calculated(self):
        close = self.crosstalk(self.snapshot(0.3))
        far = self.crosstalk(self.snapshot(0.5))
        self.assertIsNotNone(close)
        self.assertIsNotNone(far)
        # Calculated, not merely ranked: both carry a coupling percentage and
        # the tighter gap couples more.
        self.assertGreater(self.coupling_percent(close), self.coupling_percent(far))

    def test_a_quantified_finding_reports_both_modal_impedances(self):
        finding = self.crosstalk(self.snapshot(0.3))
        self.assertIn("Z0e=", finding.description)
        self.assertIn("Z0o=", finding.description)
        # The odd mode is always the lower impedance of a coupled pair.
        even = float(re.search(r"Z0e=([\d.]+)", finding.description).group(1))
        odd = float(re.search(r"Z0o=([\d.]+)", finding.description).group(1))
        self.assertGreater(even, odd)

    def test_a_quantified_finding_is_estimated_not_heuristic(self):
        finding = self.crosstalk(self.snapshot(0.3))
        self.assertEqual(finding.confidence, "MEDIUM")
        self.assertEqual(finding.title, "Quantified parallel trace coupling")

    def test_without_a_reference_plane_it_falls_back_and_says_so(self):
        # No GND pour means no reference plane, so no cross-section to solve.
        snapshot = self.snapshot(0.3)
        snapshot.zones_by_net = {}
        finding = self.crosstalk(snapshot)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.title, "Long parallel trace coupling")
        self.assertIn("not quantified", finding.description)
        self.assertIn("spacing check", finding.description)

    def test_the_geometric_fallback_is_labelled_heuristic(self):
        # A spacing check must not carry the confidence of a solved coupling.
        snapshot = self.snapshot(0.3)
        snapshot.zones_by_net = {}
        self.assertEqual(self.crosstalk(snapshot).confidence, "LOW")

    def test_a_sensitive_victim_escalates_on_a_tenth_of_the_budget(self):
        # 0.35 mm sits inside the 3H gate and couples ~8.4 %: over the 1 %
        # sensitive budget but under the 10 % logic budget, so the same
        # coupling escalates for an ADC input and not for a data bus. (It was
        # 0.55 mm while NEXT carried a spurious factor 2 below saturation.)
        ordinary = self.crosstalk(self.snapshot(0.35, victim="DATA_BUS"))
        sensitive = self.crosstalk(self.snapshot(0.35, victim="ADC_IN"))
        self.assertEqual(ordinary.severity, "MEDIUM")
        self.assertEqual(sensitive.severity, "HIGH")
        # Same geometry, so the coupling itself must be identical.
        self.assertAlmostEqual(
            self.coupling_percent(ordinary), self.coupling_percent(sensitive), places=6,
        )

    def test_a_degenerate_cross_section_does_not_abort_the_analysis(self):
        # Zero-thickness copper cannot be solved; the EMC run must still finish.
        broken = StackupProfile(layers=[
            StackupLayerModel("F.Cu", "COPPER", 0.0, layer_id=0),
            StackupLayerModel("Prepreg", "DIELECTRIC", 0.0, epsilon_r=4.4),
            StackupLayerModel("In1.GND", "COPPER", 0.0, layer_id=1),
        ], source="TEST", trustworthy=True)
        result = EMCAnalyzer(
            self.snapshot(0.3, layers=broken), self.settings(),
        ).analyze()
        self.assertIsNotNone(result)

    def test_no_rise_time_still_reports_the_saturated_worst_case(self):
        finding = self.crosstalk(self.snapshot(0.3), self.settings(rise_time_ns=0.0))
        self.assertIn("Saturated near-end coupling", finding.description)
        self.assertIn("No aggressor rise time", finding.description)


if __name__ == "__main__":
    unittest.main()
