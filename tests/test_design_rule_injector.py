import json
import tempfile
import unittest
from pathlib import Path

from design_rule_injector import DifferentialRuleInjector
from models import DifferentialRecommendation


class DesignRuleInjectorTests(unittest.TestCase):
    def test_creates_netclass_patterns_and_predefined_sizes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.kicad_pro"
            path.write_text(json.dumps({
                "net_settings": {"classes": [{"name": "Default", "priority": 9, "clearance": 0.2}], "netclass_patterns": []},
                "board": {"design_settings": {"track_widths": [0.0], "diff_pair_dimensions": [{"width": 0.0, "gap": 0.0, "via_gap": 0.0}]}},
            }), encoding="utf-8")
            recommendation = DifferentialRecommendation(
                pair_signature="USB_DM|USB_DP", pair_name="USB Data",
                recommended_width_mm=0.112, recommended_gap_mm=0.11,
                recommended_ground_clearance_mm=0.20,
            )
            applied = DifferentialRuleInjector(path).apply([recommendation])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(applied[0][1], "KiPIDA_DIFF_USB_DATA")
            entry = next(item for item in data["net_settings"]["classes"] if item["name"] == "KiPIDA_DIFF_USB_DATA")
            self.assertEqual(entry["diff_pair_width"], 0.112)
            self.assertEqual(entry["diff_pair_gap"], 0.11)
            patterns = data["net_settings"]["netclass_patterns"]
            self.assertEqual({item["pattern"] for item in patterns}, {"USB_DP", "USB_DM"})
            self.assertIn(0.112, data["board"]["design_settings"]["track_widths"])


if __name__ == "__main__":
    unittest.main()
