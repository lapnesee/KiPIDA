import json
from pathlib import Path
import unittest

from palace_remote import _parse_palace_config, bundled_palace_smoke_config


class PalaceSmokeProjectTests(unittest.TestCase):
    def test_bundled_project_has_required_palace_sections_and_mesh(self):
        config_path = bundled_palace_smoke_config()
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(payload), {"Problem", "Model", "Domains", "Boundaries", "Solver"}
        )
        self.assertEqual(payload["Problem"]["Type"], "Electrostatic")
        mesh_path = config_path.parent / payload["Model"]["Mesh"]
        self.assertTrue(mesh_path.is_file())
        mesh = mesh_path.read_text(encoding="ascii")
        self.assertIn("$Nodes\n8\n", mesh)
        self.assertIn("$Elements\n17\n", mesh)
        self.assertIn(" 2 2 3 3 ", mesh)
        self.assertIn(" 4 2 1 1 ", mesh)

    def test_bundled_project_metadata_is_discoverable(self):
        problem_type, output, warning = _parse_palace_config(
            bundled_palace_smoke_config()
        )
        self.assertEqual(problem_type, "Electrostatic")
        self.assertEqual(output, "postpro")
        self.assertEqual(warning, "")


if __name__ == "__main__":
    unittest.main()
