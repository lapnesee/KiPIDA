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

    def test_default_priority_sentinel_is_not_incremented(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.kicad_pro"
            path.write_text(json.dumps({
                "net_settings": {
                    "classes": [
                        {"name": "Default", "priority": 2147483647, "clearance": 0.2},
                        {"name": "Existing", "priority": 4, "clearance": 0.2},
                    ],
                    "netclass_patterns": [],
                },
                "board": {"design_settings": {}},
            }), encoding="utf-8")
            recommendation = DifferentialRecommendation(
                pair_signature="DP|DN", pair_name="USB",
                recommended_width_mm=0.12, recommended_gap_mm=0.10,
                recommended_ground_clearance_mm=0.20,
            )
            DifferentialRuleInjector(path).apply([recommendation])
            data = json.loads(path.read_text(encoding="utf-8"))
            rule = next(
                item for item in data["net_settings"]["classes"]
                if item["name"] == "KiPIDA_DIFF_USB"
            )
            self.assertEqual(rule["priority"], 5)
            self.assertLess(rule["priority"], 2147483647)

    def test_applies_netclass_and_assignments_to_live_project_api(self):
        class FakeProject:
            def __init__(self):
                self.classes = []
                self.assignments = [{"pattern": "CLK", "netclass": "Clock"}]

            def get_net_classes(self):
                return []

            def set_net_classes(self, classes):
                self.classes = classes

            def get_net_class_assignments(self):
                return list(self.assignments)

            def set_net_class_assignments(self, assignments):
                self.assignments = assignments

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.kicad_pro"
            path.write_text(json.dumps({
                "net_settings": {
                    "classes": [{"name": "Default", "priority": 2147483647, "clearance": 0.2}],
                    "netclass_patterns": [],
                },
                "board": {"design_settings": {}},
            }), encoding="utf-8")
            project = FakeProject()
            recommendation = DifferentialRecommendation(
                pair_signature="USB_D+|USB_D-", pair_name="USB Data",
                recommended_width_mm=0.112, recommended_gap_mm=0.11,
                recommended_ground_clearance_mm=0.20,
            )
            injector = DifferentialRuleInjector(path, project_api=project)
            injector.apply([recommendation])
            self.assertTrue(injector.live_applied)
            self.assertEqual(len(project.classes), 1)
            self.assertEqual(project.classes[0].name, "KiPIDA_DIFF_USB_DATA")
            self.assertEqual(project.classes[0].diff_pair_track_width, 112000)
            self.assertEqual(project.classes[0].diff_pair_gap, 110000)
            self.assertIn({"pattern": "CLK", "netclass": "Clock"}, project.assignments)
            self.assertIn({"pattern": "USB_D+", "netclass": "KiPIDA_DIFF_USB_DATA"}, project.assignments)
            self.assertIn({"pattern": "USB_D-", "netclass": "KiPIDA_DIFF_USB_DATA"}, project.assignments)

    def test_keeps_file_update_when_live_api_fails(self):
        class FailingProject:
            def get_net_classes(self):
                raise RuntimeError("IPC unavailable")

            def set_net_classes(self, classes):
                pass

            def get_net_class_assignments(self):
                return []

            def set_net_class_assignments(self, assignments):
                pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.kicad_pro"
            path.write_text(json.dumps({
                "net_settings": {
                    "classes": [{"name": "Default", "priority": 2147483647, "clearance": 0.2}],
                    "netclass_patterns": [],
                },
                "board": {"design_settings": {}},
            }), encoding="utf-8")
            recommendation = DifferentialRecommendation(
                pair_signature="P|N", pair_name="Pair",
                recommended_width_mm=0.12, recommended_gap_mm=0.10,
                recommended_ground_clearance_mm=0.20,
            )
            injector = DifferentialRuleInjector(path, project_api=FailingProject())
            applied = injector.apply([recommendation])
            self.assertEqual(len(applied), 1)
            self.assertFalse(injector.live_applied)
            self.assertIn("IPC unavailable", injector.live_error)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(any(
                item["name"] == "KiPIDA_DIFF_PAIR"
                for item in data["net_settings"]["classes"]
            ))
    def test_rejects_rule_below_any_configured_minimum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "board.kicad_pro"
            path.write_text(json.dumps({"net_settings": {}}), encoding="utf-8")
            recommendation = DifferentialRecommendation(
                pair_signature="USB_DM|USB_DP", pair_name="USB Data",
                recommended_width_mm=0.15, recommended_gap_mm=0.18,
                recommended_ground_clearance_mm=0.19,
            )
            with self.assertRaisesRegex(ValueError, "GND 0.190 < 0.200"):
                DifferentialRuleInjector(path).apply(
                    [recommendation], minimum_width_mm=0.13,
                    minimum_gap_mm=0.13, minimum_ground_clearance_mm=0.20,
                )
            # Validation happens before the project is touched.
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"net_settings": {}})


if __name__ == "__main__":
    unittest.main()
