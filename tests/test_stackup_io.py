import json
import os
import tempfile
import unittest

from stackup_io import load_stackup_profile, stackup_profile_from_dict


class StackupIOTests(unittest.TestCase):
    def test_imports_ordered_stackup(self):
        data = {
            "layers": [
                {"name": "F.Cu", "kind": "COPPER", "layer_id": 0, "thickness_mm": 0.035},
                {"name": "Prepreg", "kind": "DIELECTRIC", "thickness_mm": 0.18, "epsilon_r": 4.1},
                {"name": "In1.Cu", "kind": "COPPER", "layer_id": 2, "thickness_mm": 0.035},
            ]
        }
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json", encoding="utf-8") as handle:
            json.dump(data, handle)
            path = handle.name
        try:
            profile = load_stackup_profile(path)
            self.assertEqual(profile.source, "IMPORTED")
            self.assertTrue(profile.trustworthy)
            self.assertEqual(profile.layers[1].epsilon_r, 4.1)
        finally:
            os.unlink(path)

    def test_rejects_adjacent_copper_layers(self):
        with self.assertRaises(ValueError):
            stackup_profile_from_dict({"layers": [
                {"name": "F.Cu", "kind": "COPPER", "layer_id": 0, "thickness_mm": 0.035},
                {"name": "B.Cu", "kind": "COPPER", "layer_id": 31, "thickness_mm": 0.035},
            ]})


if __name__ == "__main__":
    unittest.main()
