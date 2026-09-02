import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import gmsh  # noqa: F401
except ImportError:
    gmsh = None


@unittest.skipUnless(gmsh is not None, "Gmsh is installed only in the Phase 10 runtime")
class PalaceWorkerTests(unittest.TestCase):
    def test_saved_via_geometry_is_meshed_and_source_length_is_trace_width(self):
        from phase10_palace_worker import build

        payload = {
            "region": {"bounds_mm": [0.0, 0.0, 2.0, 2.0]},
            "stackup": [
                {"kind": "COPPER", "thickness_mm": 0.035, "layer_id": 3},
                {"kind": "DIELECTRIC", "thickness_mm": 0.2,
                 "epsilon_r": 4.2, "loss_tangent": 0.02},
                {"kind": "COPPER", "thickness_mm": 0.035, "layer_id": 34},
            ],
            "mesh_resolution_mm": 0.4,
            "frequency_start_hz": 30.0e6,
            "frequency_stop_hz": 1.0e9,
            "tracks": [{
                "net_name": "SW", "start": [0.3, 1.0], "end": [1.7, 1.0],
                "width_mm": 0.2, "layer_id": 3, "length_mm": 1.4,
            }],
            "zones": [],
            "vias": [{
                "net_name": "SW", "position": [1.0, 1.0],
                "layer_ids": [3, 34], "diameter_mm": 0.6, "drill_mm": 0.3,
            }],
            "source_ports": [{
                "source_name": "switch", "net_name": "SW", "x_mm": 0.3, "y_mm": 1.0,
            }],
            "sources": [{
                "name": "switch", "net_name": "SW", "kind": "SWITCHING",
                "enabled": True, "frequency_hz": 600.0e3, "current_a": 2.0,
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            result = build(payload, directory)
            provenance = json.loads(
                (Path(directory) / "palace-region-provenance.json").read_text()
            )
            config = json.loads((Path(directory) / "palace-region.json").read_text())
        self.assertEqual(result["status"], "PROJECT_GENERATED")
        self.assertEqual(provenance["modeled_via_count"], 1)
        self.assertEqual(provenance["omitted_via_count"], 0)
        self.assertEqual(provenance["source_injection_lengths_mm"], [0.2])
        self.assertAlmostEqual(provenance["mesh_characteristic_max_mm"], 1.2)
        self.assertEqual(provenance["requested_via_count"], 1)
        self.assertEqual(provenance["via_model"], "CYLINDRICAL")
        self.assertFalse(provenance["via_geometry_fallback"])
        self.assertAlmostEqual(
            config["Domains"]["CurrentDipole"][0]["Moment"], 4.0e-4,
        )

    def test_plc_failure_retries_with_all_vias_as_conductive_solids(self):
        import phase10_palace_worker as worker

        payload = {"vias": [{"position": [1.0, 1.0]}]}
        recovered = {
            "status": "PROJECT_GENERATED", "via_geometry_fallback": True,
            "via_model": "CONDUCTIVE_SOLID", "modeled_via_count": 1,
        }
        with patch.object(
            worker, "_build_once",
            side_effect=[Exception("PLC Error: segment/facet intersection"), recovered],
        ) as build_once:
            result = worker.build(payload, "unused")
        self.assertIs(result, recovered)
        self.assertEqual(build_once.call_count, 2)
        self.assertEqual(
            build_once.call_args_list[1].kwargs["via_model"], "CONDUCTIVE_SOLID",
        )


if __name__ == "__main__":
    unittest.main()
