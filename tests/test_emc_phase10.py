import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from emc_analyzer import EMCGeometrySnapshot, EMCTrack, EMCVia
from emc_phase10 import (
    EMCPhase10Pipeline, Phase10Toolchain, SpiceExcitationRunner, SpiceModelInventory,
    VirtualEMIReceiver,
    _run_monitored, _supports_openems_port, parse_openems_log,
    select_target_regions, serialize_region,
)
from emc_analyzer import EMCFootprint
from models import (
    EMCAnalysisResult, EMCAnalysisSettings, EMCEvidence, EMCFinding, EMCFrequencyRisk,
    EMCPalaceRemoteRunResult,
    EMCPhase10RegionResult, EMCSignalSource, StackupLayerModel, StackupProfile,
)


class Phase10Tests(unittest.TestCase):
    def settings(self):
        settings = EMCAnalysisSettings(
            sources=[EMCSignalSource(
                "U4 switch", "U4_SW", kind="SWITCHING", frequency_hz=600e3,
                rise_time_ns=5.0, voltage_swing_v=12.0, current_a=4.0,
            )]
        )
        settings.phase10.maximum_regions = 2
        settings.phase10.region_margin_mm = 3.0
        settings.phase10.mesh_resolution_mm = 0.5
        return settings

    def snapshot(self):
        return EMCGeometrySnapshot(
            bounds_mm=(0.0, 0.0, 30.0, 20.0),
            stackup=StackupProfile(layers=[
                StackupLayerModel("F.Cu", "COPPER", 0.035, layer_id=3),
                StackupLayerModel("Core", "DIELECTRIC", 1.0, material="FR4", epsilon_r=4.2),
                StackupLayerModel("B.Cu", "COPPER", 0.035, layer_id=31),
            ], source="TEST", trustworthy=True),
            tracks=[EMCTrack("U4_SW", (9.0, 10.0), (12.0, 10.0), 0.3, 3, 3.0)],
            vias=[EMCVia("GND", (10.0, 11.0), (3, 31))],
        )

    def test_region_selection_is_bounded_and_deterministic(self):
        settings = self.settings()
        settings.phase10._parent_sources = settings.sources
        findings = [EMCFinding(
            "SW-002", "SWITCHING", "HIGH", "Hot loop", "", "",
            evidence=[EMCEvidence("board", "located", 10.0, 10.0, 3)],
            nets=["U4_SW"],
        )]
        regions = select_target_regions(self.snapshot(), findings, settings.phase10)
        del settings.phase10._parent_sources
        self.assertEqual(len(regions), 1)
        # EMC-4: a SWITCHING region is the evidence point +/- margin UNIONED
        # with the switch node's own copper (here the U4_SW track, x 9-12)
        # plus margin, not the point alone -- P02 Rev E's region_1 held L1
        # but neither Q10 nor Q11, 20.1 mm apart, because it never looked
        # past one point.
        self.assertEqual(regions[0].bounds_mm, (4.0, 4.0, 16.0, 16.0))
        self.assertEqual(regions[0].source_names, ["U4 switch"])
        self.assertGreater(regions[0].estimated_cells, 0)

    def test_a_region_over_the_cell_limit_is_meshed_coarser_not_skipped(self):
        # P02 Rev E: both regions exceeded 2,000,000 cells at 0.25 mm (9.2M
        # and 2.1M) and were skipped on every run.
        settings = self.settings()
        settings.phase10.mesh_resolution_mm = 0.1
        settings.phase10.maximum_cells = 200000
        settings.phase10._parent_sources = settings.sources
        findings = [EMCFinding(
            "SW-002", "SWITCHING", "HIGH", "Hot loop", "", "",
            evidence=[EMCEvidence("board", "located", 10.0, 10.0, 3)], nets=["U4_SW"],
        )]
        (region,) = select_target_regions(self.snapshot(), findings, settings.phase10)
        del settings.phase10._parent_sources
        self.assertLessEqual(region.estimated_cells, 200000)
        self.assertGreater(region.mesh_resolution_mm, 0.1)
        self.assertIn("to fit the 200,000-cell limit", region.warnings[0])

    def test_source_linked_emission_region_precedes_passive_high_finding(self):
        settings = self.settings()
        settings.phase10.maximum_regions = 1
        settings.phase10._parent_sources = settings.sources
        findings = [
            EMCFinding(
                "ES-001", "ESD", "HIGH", "Missing TVS", "", "",
                evidence=[EMCEvidence("board", "connector", 2.0, 2.0, 3)],
            ),
            EMCFinding(
                "SW-001", "SWITCHING", "LOW", "Switch harmonic", "", "",
                evidence=[EMCEvidence("board", "source", 10.0, 10.0, 3)],
                nets=["U4_SW"],
            ),
        ]
        regions = select_target_regions(self.snapshot(), findings, settings.phase10)
        del settings.phase10._parent_sources
        self.assertEqual(regions[0].finding_ids, ["SW-001"])
        self.assertEqual(regions[0].source_names, ["U4 switch"])

    def test_geometry_manifest_contains_stackup_tracks_and_vias(self):
        settings = self.settings()
        region = EMCPhase10RegionResult(
            "region_1", "SELECTED", (7.0, 7.0, 13.0, 13.0), estimated_cells=100,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = serialize_region(
                self.snapshot(), region, settings, settings.sources,
                Path(directory) / "input.json",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "KIPIDA_PHASE10_OPENEMS_3")
            self.assertEqual(payload["tracks"][0]["net_name"], "U4_SW")
            self.assertEqual(payload["source_ports"][0]["geometry_source"], "ROUTED_TRACK")
            self.assertEqual(payload["vias"][0]["net_name"], "GND")
            self.assertEqual(len(payload["stackup"]), 3)
            self.assertEqual(payload["openems_max_timesteps"], 8000)
            self.assertAlmostEqual(payload["openems_end_criteria"], 1.0e-3)

    def test_zone_only_source_creates_pad_anchored_port_candidate(self):
        from shapely.geometry import box
        snapshot = self.snapshot()
        snapshot.tracks = []
        snapshot.zones_by_net = {"U4_SW": {3: box(8.0, 8.0, 12.0, 12.0)}}
        snapshot.footprints = [EMCFootprint(
            "U4", "TPS568236RJNR", (10.0, 10.0), ("U4_SW",),
            (("U4_SW", 10.0, 10.0),),
        )]
        region = EMCPhase10RegionResult(
            "zone", "SELECTED", (7.0, 7.0, 13.0, 13.0), estimated_cells=100,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = serialize_region(
                snapshot, region, self.settings(), self.settings().sources,
                Path(directory) / "input.json",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(payload["tracks"])
            self.assertEqual(len(payload["zones"]), 1)
            self.assertEqual(payload["source_ports"][0]["geometry_source"], "ZONE_PAD_ANCHORED")
            self.assertEqual(payload["source_ports"][0]["net_name"], "U4_SW")

    def test_spice_inventory_applies_verified_mapping_without_claiming_ngspice_test(self):
        snapshot = self.snapshot()
        snapshot.footprints = [EMCFootprint("U4", "TPS568236RJNR", (10.0, 10.0))]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model.lib").write_text(
                ".subckt TPS568236_TRANS VIN SW GND\n.ends TPS568236_TRANS\n",
                encoding="utf-8",
            )
            (root / "MODEL_CATALOG.csv").write_text(
                "Component;SPICE_status;Model_or_action;Location_or_source;Notes\n"
                "TPS568236RJNR;PRET_A_ASSOCIER_MANUEL;TPS568236_TRANS;model.lib;mapping required\n",
                encoding="utf-8",
            )
            audits = SpiceModelInventory(root).audit(snapshot, self.settings().sources)
            self.assertEqual(len(audits), 1)
            self.assertEqual(audits[0].status, "MAPPING_VERIFIED_NGSPICE_NOT_TESTED")
            self.assertFalse(audits[0].used)
            self.assertTrue(Path(audits[0].model_path).is_file())
            self.assertIn("1:VIN", audits[0].pin_mapping)

    def test_encrypted_verified_model_is_pspice_only(self):
        snapshot = self.snapshot()
        snapshot.footprints = [EMCFootprint("U4", "TPS568236RJNR", (10.0, 10.0))]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model.lib").write_text(
                "**$ENCRYPTED_LIB\n.subckt TPS568236_TRANS VIN VBST EN SW PG FB VCC SS MODE AGND PGND\n",
                encoding="utf-8",
            )
            (root / "MODEL_CATALOG.csv").write_text(
                "Component;SPICE_status;Model_or_action;Location_or_source;Notes\n"
                "TPS568236RJNR;PRET_A_ASSOCIER_MANUEL;TPS568236_TRANS;model.lib;\n",
                encoding="utf-8",
            )
            audits = SpiceModelInventory(root, audit_directory=root / "audit").audit(
                snapshot, self.settings().sources
            )
            self.assertEqual(audits[0].status, "MAPPING_VERIFIED_PSPICE_ONLY")
            self.assertEqual(audits[0].compatibility, "MAPPING_VERIFIED_PSPICE_ONLY")
            self.assertTrue(Path(audits[0].wrapper_path).is_file())
            self.assertIn("XCORE VIN VBST EN SW", Path(audits[0].wrapper_path).read_text())

    def test_an_encrypted_model_runs_in_its_manufacturer_bench_under_pspice(self):
        # TI ships SLVMDD9's TPS55288 with "TPS55288 Startup.net", whose
        # instance puts the boost-side SW2 pin on net SW2. PSpice for TI runs
        # what ngspice cannot; a fake runner stands in for pspice.exe.
        from emc_phase10 import read_pspice_print

        class FakePSpice:
            available = True

            def run(self, netlist):
                text = Path(netlist).read_text()
                self.circuit = text
                out = Path(netlist).with_suffix(".out")
                rows = [
                    f"  {k * 0.25e-6:.7E}  {9.0:.7E}  {12.0 if (k // 5) % 2 else 0.0:.7E}"
                    for k in range(400)
                ]
                out.write_text("   TIME   V(SW1)   V(SW2)\n" + "\n".join(rows) + "\n JOB CONCLUDED\n")
                return "concluded", out, 1.0

        snapshot = self.snapshot()
        snapshot.footprints = [EMCFootprint("U4", "TPS552882RPMR", (10.0, 10.0))]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tps55288.lib").write_text(
                ".SUBCKT TPS55288 DR1L DR1H VIN SW1 SW2 PGND\n+ PARAMS: SS=0\n$CDNENCSTART_ADV2\n",
                encoding="utf-8",
            )
            (root / "bench").mkdir()
            (root / "bench" / "TPS55288 Startup.net").write_text(
                "X_U2 DR1L DR1H VIN SW1 SW2 0 TPS55288 PARAMS:\n+ SS=0\n", encoding="utf-8",
            )
            (root / "MODEL_CATALOG.csv").write_text(
                "Component;SPICE_status;Model_or_action;Location_or_source;Notes\n"
                "TPS552882RPMR;OFFICIEL_PSPICE_CHIFFRE;TPS55288;tps55288.lib;\n",
                encoding="utf-8",
            )
            fake = FakePSpice()
            audits = SpiceModelInventory(
                root, audit_directory=root / "audit", pspice_runner=fake,
                probe_cache_directory=root / "cache",
            ).audit(snapshot, self.settings().sources)
            self.assertEqual(audits[0].status, "PSPICE_BENCH_PASS")
            self.assertIn(".PRINT TRAN V([SW1]) V([SW2])", fake.circuit)
            self.assertIn("V(SW2) switched at 400 kHz", audits[0].notes)
        # PSpice splits a wide table; the columns join by name.
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "wide.out"
            out.write_text(
                "   TIME   V(SW)   V(OUT)   I(L1)\n  1.0E-06  1.0  2.0  3.0\n  2.0E-06  4.0  5.0  6.0\n"
                "   TIME   I(R1)\n  1.0E-06  7.0\n  2.0E-06  8.0\n JOB CONCLUDED\n"
            )
            columns = read_pspice_print(out)
            self.assertEqual(columns["TIME"], [1.0e-6, 2.0e-6])
            self.assertEqual(columns["I(R1)"], [7.0, 8.0])
            # A name filling its 16-character column runs into the next, as
            # in P02 Rev E's U5 board bench.
            out.write_text(
                "  TIME            V(N_MAIN_3V3_SW)I(L_L2)         V(N__3V3_MAIN)  \n"
                "   2.8000000E-04  -1.7752400E-02   2.4656173E-01   9.0192461E-01\n JOB CONCLUDED\n"
            )
            columns = read_pspice_print(out)
            self.assertEqual(columns["I(L_L2)"], [0.24656173])

    def test_an_encrypted_model_without_a_verified_mapping_is_pspice_only(self):
        # TI's model for P02 Rev E's TPS552882 (SLVMDD9, sub-circuit
        # TPS55288) is Cadence-encrypted; found through the catalog it read
        # AVAILABLE_REQUIRES_PIN_MAPPING, as if ngspice could run it.
        snapshot = self.snapshot()
        snapshot.footprints = [EMCFootprint("U4", "TPS552882RPMR", (10.0, 10.0))]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tps55288.lib").write_text(
                ".SUBCKT TPS55288 DR1L DR1H VIN EN SCL_NULL SDA_NULL\n$CDNENCSTART_ADV2\n",
                encoding="utf-8",
            )
            (root / "MODEL_CATALOG.csv").write_text(
                "Component;SPICE_status;Model_or_action;Location_or_source;Notes\n"
                "TPS552882RPMR;OFFICIEL_PSPICE_CHIFFRE;TPS55288;tps55288.lib;\n",
                encoding="utf-8",
            )
            audits = SpiceModelInventory(root).audit(snapshot, self.settings().sources)
            self.assertEqual(audits[0].status, "AVAILABLE_PSPICE_ONLY")
            self.assertIn("PSpice only", audits[0].notes)

    @staticmethod
    def p02_u14_board():
        """P02 Rev E's U14 (LMR51450SDRRR) and the parts around it."""
        from ingest.board_reader import Footprint, Pad, ParsedBoard

        def part(reference, value, *pads, dnp=False):
            return Footprint(reference, value, "", (0.0, 0.0), 0.0, "F.Cu", "", "", [
                Pad(number, net, "passive", (0.0, 0.0), pinfunction=function)
                for number, function, net in pads
            ], dnp=dnp)

        u14 = part("U14", "LMR51450SDRRR",
                   *[(str(k), f"SW_{k}", "/J2_SRC_SW") for k in (1, 2, 3)],
                   ("4", "BOOT_4", "/J2_SRC_BOOT"), ("5", "PG_5", "unconnected-(U14-PG-Pad5)"),
                   ("6", "RT_6", "unconnected-(U14-RT-Pad6)"), ("7", "FB_7", "/J2_SRC_FB"),
                   ("8", "AGND_8", "/J2_SRC_AGND"), ("9", "EN_9", "/J2_SRC_EN"),
                   *[(str(k), f"VIN_{k}", "/VBUS_SYS") for k in (10, 11, 12)],
                   ("13", "PGND_13", "GND"))
        two = [
            ("L3", "4.7uH / Isat>=12A", "/J2_SRC_SW", "/J2_SRC_VBUS"),
            ("C54", "100n 50V X7R", "/J2_SRC_BOOT", "/J2_SRC_SW"),
            ("R59", "100k 0.1%", "/J2_SRC_VBUS", "/J2_SRC_FB"),
            ("R60", "18.2k 0.1%", "/J2_SRC_FB", "/J2_SRC_AGND"),
            ("R62", "47k 1%", "/VBUS_SYS", "/J2_SRC_EN"),
            ("R64", "21k 0.1%", "/J2_SRC_FB", "/J2_SRC_FB_SWITCH"),
            ("C36", "22u 35V X5R", "/VBUS_SYS", "GND"),
            ("C49", "47u 16V X5R", "/J2_SRC_VBUS", "GND"),
        ]
        footprints = [u14] + [part(ref, value, ("1", "", a), ("2", "", b)) for ref, value, a, b in two]
        footprints += [
            part("C60", "330p DNP", ("1", "", "/J2_SRC_SW"), ("2", "", "GND"), dnp=True),
            part("Q13", "BC847B", ("1", "B", "/J2_PDO2"), ("2", "E", "GND"), ("3", "C", "/J2_SRC_FB_SWITCH")),
        ]
        return ParsedBoard(Path("p02.kicad_pcb"), None, None, footprints, [], [], [])

    def test_a_converter_is_read_from_the_board_into_a_pspice_bench(self):
        # P02 Rev E's U14 leaves RT open, which its datasheet sets to 500 kHz
        # and TI's model does not switch with; R64 leads to a transistor, C60
        # is not fitted.
        from emc_phase10 import board_converter_bench

        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "LMR51450_TRANS.lib"
            model.write_text(
                ".SUBCKT LMR51450_TRANS AGND BOOT EN FB GND PGOOD RT SW1 SW2 SW3 VIN1 VIN2 VIN3\n"
                "+  PARAMS: MODE=0\n$ENCRYPTED_LIB\n", encoding="utf-8",
            )
            bench = board_converter_bench(
                self.p02_u14_board(), "U14", model, "LMR51450_TRANS", "/J2_SRC_SW",
                {"/VBUS_SYS": 20.0, "/J2_SRC_VBUS": 5.2}, 3.0, 20.0,
            )
        text = bench["text"]
        self.assertIn("R_KIPIDA_RT N_OPEN_U14_RT 0 31600", text)
        self.assertIn("L_L3 N_J2_SRC_SW N_J2_SRC_VBUS 4.7e-06 IC=3", text)
        self.assertIn("C_C49 N_J2_SRC_VBUS 0 4.7e-05 IC=5.2", text)
        self.assertIn("R_R62 N_VBUS_SYS N_J2_SRC_EN 47000", text)
        self.assertIn("R_KIPIDA_LOAD N_J2_SRC_VBUS 0 1.73333", text)
        self.assertIn("LMR51450_TRANS PARAMS: MODE=1", text)
        self.assertNotIn("C60", text)
        self.assertNotIn("R64", text)
        self.assertIn("GND=0", bench["pin_mapping"])
        self.assertIn("SW3=/J2_SRC_SW", bench["pin_mapping"])

    def test_a_board_circuit_gives_the_excitation_its_switched_current(self):
        # P02 Rev E's U14 in its board circuit under PSpice for TI: 478.5 kHz,
        # 5.2 V, inductor 2.16 to 3.82 A under the source's 3 A load.
        import re
        from application.emc_controller import _phase10_spice_findings
        from emc_phase10 import BoardCircuitSimulator, board_circuit_excitation
        from models import EMCPhase10Result

        class FakePSpice:
            calls = 0

            def run(self, netlist):
                self.calls += 1
                step, stop, start = (float(item) for item in re.search(
                    r"\.TRAN (\S+) (\S+) (\S+)", Path(netlist).read_text()).groups())
                rows = []
                for k in range(int(round((stop - start) / step)) + 1):
                    t = start + k * step
                    phase = (t * 478.5e3) % 1.0
                    ramp = 2.1 + 1.8 * (phase / 0.27 if phase < 0.27 else (1 - phase) / 0.73)
                    current = max(2.158, min(3.824, ramp))
                    rows.append(f"  {t:.8E}  {20.0 if phase < 0.27 else 0.0:.7E}  {current:.7E}  {5.2:.7E}")
                out = Path(netlist).with_suffix(".out")
                out.write_text("  TIME            V(N_J2_SRC_SW)  I(L_L3)         V(N_J2_SRC_VBUS)\n"
                               + "\n".join(rows) + "\n JOB CONCLUDED\n")
                return "concluded", out, 88.0

        source = EMCSignalSource("U14 SW (J2 source buck)", "/J2_SRC_SW", kind="SWITCHING",
                                 frequency_hz=500e3, rise_time_ns=5.0, voltage_swing_v=20.0, current_a=3.0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "LMR51450_TRANS.lib"
            model.write_text(".SUBCKT LMR51450_TRANS AGND BOOT EN FB GND PGOOD RT SW1 SW2 SW3 VIN1 VIN2 "
                             "VIN3\n+  PARAMS: MODE=0\n", encoding="utf-8")
            fake = FakePSpice()
            simulator = BoardCircuitSimulator(fake, root / "cache")
            rails = {"/VBUS_SYS": 20.0, "/J2_SRC_VBUS": 5.2}
            result = simulator.run(self.p02_u14_board(), source, "U14", model, "LMR51450_TRANS", rails,
                                   root / "run")
            again = simulator.run(self.p02_u14_board(), source, "U14", model, "LMR51450_TRANS", rails,
                                  root / "again")
        self.assertEqual(result.status, "SWITCHING")
        self.assertAlmostEqual(result.frequency_hz / 1e3, 478.5, delta=2.0)
        self.assertEqual((again.status, fake.calls), ("SWITCHING", 1))
        # 478.5 kHz is within 10 % of the configured 500 kHz: nothing to report.
        self.assertEqual(_phase10_spice_findings(EMCPhase10Result(board_circuits=[result])), [])
        excitation = board_circuit_excitation(source, result)
        self.assertEqual((excitation.provenance, excitation.peak_current_a), ("BOARD_CIRCUIT_PSPICE", 3.824))
        self.assertAlmostEqual(excitation.maximum_dv_dt_v_s, 20.0 / 5e-9)

    @unittest.skipUnless(
        Path(r"C:\Spice64\bin\ngspice_con.exe").is_file()
        and Path(r"C:\Users\jbc66\Documents\DAW CONTROLEUR\Lib\SPICE\official\TPS562200DDCT\extracted\TPS562200_PSPICE_TRANS\TPS562200_TRANS.LIB").is_file(),
        "TPS562200 model or ngspice is unavailable",
    )
    def test_real_tps562200_model_minimal_ngspice_compatibility(self):
        source = EMCSignalSource(
            "Net-(U5-SW)", "Net-(U5-SW)", kind="SWITCHING",
            frequency_hz=650e3, voltage_swing_v=5.0, current_a=0.295,
        )
        snapshot = self.snapshot()
        snapshot.footprints = [EMCFootprint("U5", "TPS562200DDCT", (10.0, 10.0))]
        with tempfile.TemporaryDirectory() as directory:
            audits = SpiceModelInventory(
                r"C:\Users\jbc66\Documents\DAW CONTROLEUR\Lib\SPICE",
                r"C:\Spice64\bin\ngspice_con.exe", directory,
            ).audit(snapshot, [source])
            self.assertEqual(
                audits[0].status, "MAPPING_VERIFIED_NGSPICE_TRANSIENT_UNSTABLE"
            )
            self.assertTrue(Path(audits[0].probe_log_path).is_file())

    def test_external_worker_timeout_is_bounded_and_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            messages = []
            returncode, status, log_path = _run_monitored(
                [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(10)"],
                timeout=0.5, cwd=directory, environment=os.environ.copy(),
                log_callback=messages.append, progress_interval_s=0.1,
            )
            self.assertIsNone(returncode)
            self.assertEqual(status, "TIMEOUT")
            self.assertTrue(log_path.is_file())
            self.assertTrue(messages)

    def test_external_worker_honours_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            returncode, status, _ = _run_monitored(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                timeout=10, cwd=directory, environment=os.environ.copy(),
                cancellation_callback=lambda: True,
            )
            self.assertIsNone(returncode)
            self.assertEqual(status, "CANCELLED")

    def test_openems_log_parser_rejects_timestep_limited_result(self):
        text = """Operator::CalcGaussianPulsExcitation: Requested excitation pusle would be 207556 timesteps or 1 s long. Cutting to max number of timesteps!
Warning: Unused primitive (type: Polygon) detected in property: copper_3!
[@ 1m] Timestep: 7999 || Energy: ~1.31e-20 (- 0.00dB)
RunFDTD: Warning: Max. number of timesteps was reached before the end-criteria of -30dB was reached...
Time for 8000 iterations with 399562.00 cells : 117.83 sec
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "solver.log"
            path.write_text(text, encoding="utf-8")
            result = parse_openems_log(path, 8000)
        self.assertFalse(result["converged"])
        self.assertEqual(result["iterations"], 8000)
        self.assertEqual(result["cells"], 399562)
        self.assertEqual(result["energy_decay_db"], 0.0)
        self.assertEqual(result["unused_primitives"], 1)
        self.assertTrue(any("207,556" in warning for warning in result["warnings"]))

    def test_openems_port_accepts_complete_differential_source(self):
        switching = self.settings().sources[0]
        differential = EMCSignalSource(
            "USB", "USB_D+", kind="DIFFERENTIAL", negative_net_name="USB_D-",
        )
        incomplete = EMCSignalSource("USB", "USB_D+", kind="DIFFERENTIAL")
        self.assertTrue(_supports_openems_port(switching))
        self.assertTrue(_supports_openems_port(differential))
        self.assertFalse(_supports_openems_port(incomplete))

    def test_differential_manifest_contains_two_role_tagged_conductors(self):
        from shapely.geometry import box
        settings = self.settings()
        settings.sources = [EMCSignalSource(
            "USB", "USB_D+", kind="DIFFERENTIAL", negative_net_name="USB_D-",
            frequency_hz=480e6, voltage_swing_v=0.4,
        )]
        snapshot = self.snapshot()
        snapshot.tracks = [
            EMCTrack("USB_D+", (9.0, 9.8), (12.0, 9.8), 0.15, 3, 3.0),
            EMCTrack("USB_D-", (9.0, 10.2), (12.0, 10.2), 0.15, 3, 3.0),
        ]
        snapshot.zones_by_net = {"GND": {31: box(7.0, 7.0, 13.0, 13.0)}}
        region = EMCPhase10RegionResult(
            "usb", "SELECTED", (7.0, 7.0, 13.0, 13.0), estimated_cells=100,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = serialize_region(
                snapshot, region, settings, settings.sources,
                Path(directory) / "input.json", run_solver=False,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            {(item["net_name"], item["conductor_role"]) for item in payload["source_ports"]},
            {("USB_D+", "POSITIVE"), ("USB_D-", "NEGATIVE")},
        )
        self.assertEqual(payload["differential_excitation_mode"], "DIFFERENTIAL")
        self.assertAlmostEqual(payload["differential_leg_impedance_ohm"], 45.0)

    def test_virtual_receiver_refuses_false_compliance_margin(self):
        receiver = VirtualEMIReceiver(self.settings().phase10)
        points = receiver.process_relative([
            EMCFrequencyRisk(30e6, -12.0, "U4", "HARMONIC"),
            EMCFrequencyRisk(30.01e6, -8.0, "U5", "HARMONIC"),
        ])
        self.assertEqual(len(points), 1)
        self.assertIsNone(points[0].limit_dbuv_m)
        self.assertIsNone(points[0].margin_db)
        self.assertIn("RELATIVE", points[0].provenance)
        # A relative peak, not a quasi-peak field strength (EMC-8).
        self.assertEqual(points[0].detector, "PEAK_RELATIVE")
        self.assertAlmostEqual(points[0].level_rel_db, -8.0)

    def test_pipeline_routes_full_wave_execution_to_remote_palace_backend(self):
        settings = self.settings()
        settings.phase10.spice_enabled = False
        settings.phase10.full_wave_backend = "PALACE_REMOTE"
        settings.phase10.auto_run_full_wave = True
        settings.phase10.palace_remote_host = "palace.lan"
        settings.phase10.palace_remote_username = "solver"
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "case.json"
            config.write_text(json.dumps({"Problem": {"Type": "Driven"}}))
            settings.phase10.palace_remote_config_path = str(config)
            palace_result = EMCPalaceRemoteRunResult(
                status="SOLVED_REMOTE", server="solver@palace.lan",
                problem_type="Driven", palace_version="Palace test",
                local_artifact_directory=str(Path(directory) / "artifacts"),
                dry_run_passed=True,
            )
            with patch("emc_phase10.Phase10Toolchain.detect", return_value=[]), patch(
                "emc_phase10.PalaceRemoteClient"
            ) as client_class:
                client_class.return_value.run_project.return_value = palace_result
                result = EMCPhase10Pipeline(
                    self.snapshot(), settings, EMCAnalysisResult(),
                    board_file_path=str(Path(directory) / "board.kicad_pcb"),
                ).run()
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.palace_runs[0].status, "SOLVED_REMOTE")
        client_class.return_value.run_project.assert_called_once()
        self.assertEqual(result.tools[-1].name, "PALACE_REMOTE")

    def test_spice_missing_tool_retains_parametric_provenance(self):
        source = self.settings().sources[0]
        with tempfile.TemporaryDirectory() as directory:
            result = SpiceExcitationRunner("").run(source, directory)
            self.assertEqual(result.status, "SKIPPED_TOOL_MISSING")
            self.assertGreater(result.maximum_dv_dt_v_s, 0.0)
            self.assertIn("PARAMETRIC", result.provenance)

    @unittest.skipUnless(
        Path(r"C:\Spice64\bin\ngspice_con.exe").is_file(),
        "ngspice is not installed",
    )
    def test_ngspice_supports_output_directory_with_spaces(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "project with spaces" / "EMC-PHASE10"
            result = SpiceExcitationRunner(
                r"C:\Spice64\bin\ngspice_con.exe"
            ).run(self.settings().sources[0], directory)
            self.assertEqual(result.status, "SIMULATED", result.notes)
            self.assertTrue(Path(result.waveform_path).is_file())

    @unittest.skipUnless(
        Path(r"C:\Spice64\bin\ngspice_con.exe").is_file(),
        "ngspice is not installed",
    )
    def test_installed_ngspice_generates_waveform(self):
        with tempfile.TemporaryDirectory() as directory:
            result = SpiceExcitationRunner(
                r"C:\Spice64\bin\ngspice_con.exe"
            ).run(self.settings().sources[0], directory)
            self.assertEqual(result.status, "SIMULATED")
            # EMC-6: this is an ideal pulse into a resistive load, not a
            # device-model circuit simulation; "PARAMETRIC_NGSPICE" said
            # otherwise.
            self.assertEqual(result.provenance, "IDEAL_PULSE")
            self.assertTrue(Path(result.waveform_path).is_file())

    @unittest.skipUnless(
        Path(r"C:\openEMS\phase10-venv\Scripts\python.exe").is_file(),
        "Phase 10 openEMS runtime is not installed",
    )
    def test_installed_openems_worker_exports_geometry(self):
        settings = self.settings()
        region = EMCPhase10RegionResult(
            "integration", "SELECTED", (7.0, 7.0, 13.0, 13.0), estimated_cells=100,
        )
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            input_path = serialize_region(
                self.snapshot(), region, settings, settings.sources,
                directory / "input.json", run_solver=False,
            )
            result_path = directory / "result.json"
            worker = Path(__file__).parents[1] / "phase10_openems_worker.py"
            environment = os.environ.copy()
            environment["CSXCAD_INSTALL_PATH"] = r"C:\openEMS"
            environment["PATH"] = r"C:\openEMS" + os.pathsep + environment.get("PATH", "")
            completed = subprocess.run(
                [r"C:\openEMS\phase10-venv\Scripts\python.exe", str(worker),
                 str(input_path), str(result_path)],
                capture_output=True, text=True, timeout=60, env=environment,
            )
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(payload["status"], "GEOMETRY_EXPORTED")
            self.assertTrue((directory / "openems" / "phase10-openems.xml").is_file())

    @unittest.skipUnless(
        Path(r"C:\openEMS\phase10-venv\Scripts\python.exe").is_file(),
        "Phase 10 openEMS runtime is not installed",
    )
    def test_installed_openems_worker_uses_zone_source_and_real_reference_zone(self):
        from shapely.geometry import box
        settings = self.settings()
        snapshot = self.snapshot()
        snapshot.tracks = []
        snapshot.zones_by_net = {
            "U4_SW": {3: box(8.0, 8.0, 12.0, 12.0)},
            "GND": {31: box(7.0, 7.0, 13.0, 13.0)},
        }
        snapshot.footprints = [EMCFootprint(
            "U4", "TPS568236RJNR", (10.0, 10.0), ("U4_SW",),
            (("U4_SW", 10.0, 10.0),),
        )]
        region = EMCPhase10RegionResult(
            "zone_integration", "SELECTED", (7.0, 7.0, 13.0, 13.0),
            estimated_cells=100,
        )
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            input_path = serialize_region(
                snapshot, region, settings, settings.sources,
                directory / "input.json", run_solver=False,
            )
            result_path = directory / "result.json"
            worker = Path(__file__).parents[1] / "phase10_openems_worker.py"
            environment = os.environ.copy()
            environment["CSXCAD_INSTALL_PATH"] = r"C:\openEMS"
            environment["PATH"] = r"C:\openEMS" + os.pathsep + environment.get("PATH", "")
            completed = subprocess.run(
                [r"C:\openEMS\phase10-venv\Scripts\python.exe", str(worker),
                 str(input_path), str(result_path)],
                capture_output=True, text=True, timeout=60, env=environment,
            )
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(payload["port_net_name"], "U4_SW")
            self.assertEqual(payload["port_geometry_source"], "ZONE_PAD_ANCHORED")
            self.assertEqual(payload["port_confidence"], "HIGH")
            self.assertEqual(payload["port_reference_layer_id"], 31)

    @unittest.skipUnless(
        Path(r"C:\openEMS\phase10-venv\Scripts\python.exe").is_file(),
        "Phase 10 openEMS runtime is not installed",
    )
    def test_installed_openems_worker_builds_two_leg_differential_port(self):
        from shapely.geometry import box
        settings = self.settings()
        settings.sources = [EMCSignalSource(
            "USB", "USB_D+", kind="DIFFERENTIAL", negative_net_name="USB_D-",
            frequency_hz=480e6, voltage_swing_v=0.4,
        )]
        snapshot = self.snapshot()
        snapshot.tracks = [
            EMCTrack("USB_D+", (9.0, 9.8), (12.0, 9.8), 0.15, 3, 3.0),
            EMCTrack("USB_D-", (9.0, 10.2), (12.0, 10.2), 0.15, 3, 3.0),
        ]
        snapshot.zones_by_net = {"GND": {31: box(7.0, 7.0, 13.0, 13.0)}}
        region = EMCPhase10RegionResult(
            "usb_integration", "SELECTED", (7.0, 7.0, 13.0, 13.0),
            estimated_cells=100,
        )
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            input_path = serialize_region(
                snapshot, region, settings, settings.sources,
                directory / "input.json", run_solver=False,
            )
            result_path = directory / "result.json"
            worker = Path(__file__).parents[1] / "phase10_openems_worker.py"
            environment = os.environ.copy()
            environment["CSXCAD_INSTALL_PATH"] = r"C:\openEMS"
            environment["PATH"] = r"C:\openEMS" + os.pathsep + environment.get("PATH", "")
            completed = subprocess.run(
                [r"C:\openEMS\phase10-venv\Scripts\python.exe", str(worker),
                 str(input_path), str(result_path)],
                capture_output=True, text=True, timeout=60, env=environment,
            )
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(payload["status"], "GEOMETRY_EXPORTED")
            self.assertEqual(payload["port_mode"], "DIFFERENTIAL_MODAL")
            self.assertEqual(payload["port_count"], 2)
            self.assertEqual(payload["port_net_names"], ["USB_D+", "USB_D-"])
            self.assertEqual(payload["port_reference_layer_ids"], [31, 31])
            self.assertEqual(payload["port_excitations"], [0.5, -0.5])
            self.assertAlmostEqual(payload["port_leg_impedance_ohm"], 45.0)
            self.assertTrue((directory / "openems" / "phase10-openems.xml").is_file())

            manifest = json.loads(input_path.read_text(encoding="utf-8"))
            manifest["differential_excitation_mode"] = "COMMON_MODE"
            input_path.write_text(json.dumps(manifest), encoding="utf-8")
            common_result_path = directory / "common-result.json"
            completed = subprocess.run(
                [r"C:\openEMS\phase10-venv\Scripts\python.exe", str(worker),
                 str(input_path), str(common_result_path)],
                capture_output=True, text=True, timeout=60, env=environment,
            )
            common = json.loads(common_result_path.read_text(encoding="utf-8"))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(common["port_mode"], "COMMON_MODE_MODAL")
            self.assertEqual(common["port_excitations"], [0.5, 0.5])


if __name__ == "__main__":
    unittest.main()
