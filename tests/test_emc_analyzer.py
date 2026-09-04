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

    def test_detects_plane_void_switching_area_and_missing_return_via(self):
        result = EMCAnalyzer(self.snapshot(), self.settings()).analyze()
        rules = {finding.rule_id for finding in result.findings}
        self.assertIn("GP-001", rules)
        self.assertIn("SW-002", rules)
        self.assertIn("RP-001", rules)
        self.assertLess(result.risk_score, 100)
        self.assertTrue(result.probe_points)

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
        self.assertIn("PD-001", rules)

    def test_emission_output_is_explicitly_relative(self):
        result = EMCAnalyzer(self.snapshot(), self.settings()).analyze()
        self.assertTrue(result.frequency_risks)
        self.assertTrue(result.cavity_resonances_hz)
        self.assertTrue(any("cannot certify" in item for item in result.limitations))
        self.assertTrue(result.test_plan)

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
        finding = next(item for item in result.findings if item.rule_id == "DC-001")
        self.assertEqual(finding.severity, "MEDIUM")
        self.assertEqual(finding.confidence, "LOW")
        self.assertIn("rail-bypass", finding.title)

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
        # 0.55 mm sits inside the 3H gate and couples ~9 %: over the 1 %
        # sensitive budget but under the 10 % logic budget, so the same
        # coupling escalates for an ADC input and not for a data bus.
        ordinary = self.crosstalk(self.snapshot(0.55, victim="DATA_BUS"))
        sensitive = self.crosstalk(self.snapshot(0.55, victim="ADC_IN"))
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
