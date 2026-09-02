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
        self.assertEqual(regions[0].bounds_mm, (7.0, 7.0, 13.0, 13.0))
        self.assertEqual(regions[0].source_names, ["U4 switch"])
        self.assertGreater(regions[0].estimated_cells, 0)

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

    def test_known_windows_tool_paths_are_considered(self):
        statuses = Phase10Toolchain(self.settings().phase10).detect()
        names = {item.name for item in statuses}
        self.assertEqual(names, {"NGSPICE", "OPENEMS", "OPENEMS_PYTHON", "GMSH", "PALACE"})

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
            self.assertEqual(result.provenance, "PARAMETRIC_NGSPICE")
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
