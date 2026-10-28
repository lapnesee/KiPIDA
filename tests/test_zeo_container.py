"""A Zeo container's links are read from its declarations and checked against each board's PCB."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from ingest.project_context import HostDetection, ZEO, build_project_context
from ingest.zeo_container import MISSING, OK, STALE, validate_links


def _board(directory, name, pads):
    directory.mkdir(parents=True)
    pad_text = " ".join(f'(pad "{number}" smd rect (at {number} 0 0) (size 1 1) (layers "F.Cu") (net "{net}"))'
                        for number, net in pads)
    (directory / f"{name}.kicad_pcb").write_text(
        f'(kicad_pcb (version 20260206) (layers (0 "F.Cu" signal)) '
        f'(footprint "Conn" (layer "F.Cu") (at 0 0 0) (property "Reference" "{name.upper()}") {pad_text}))',
        encoding="utf-8")
    (directory / f"{name}.kicad_pro").write_text(json.dumps(
        {"multi_board": {"container": False, "container_project_relative_path": "../../system.kicad_pro"}}),
        encoding="utf-8")


def _endpoint(board, component, pin, pin_name):
    return {"sub_project_uuid": board, "component": component, "pin": pin, "pin_name": pin_name}


class ZeoContainerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        _board(root / "boards" / "a", "a", [("1", "+5V"), ("2", "SDA_OLD")])
        _board(root / "boards" / "b", "b", [("1", "+5V_IN"), ("2", "SDA")])
        (root / "system.kicad_pro").write_text(json.dumps({"multi_board": {
            "container": True,
            "sub_projects": [{"name": "a", "path": "boards/a/a.kicad_pro", "uuid": "A"},
                             {"name": "b", "path": "boards/b/b.kicad_pro", "uuid": "B"}],
            "cross_board_nets": [
                {"name": "+5V", "uuid": "l1", "endpoints": [_endpoint("A", "A", "1", "+5V"), _endpoint("B", "B", "1", "+5V_IN")]},
                {"name": "SDA", "uuid": "l2", "endpoints": [_endpoint("A", "A", "2", "SDA"), _endpoint("B", "B", "2", "SDA")]},
                {"name": "NC", "uuid": "l3", "endpoints": [_endpoint("B", "B", "9", "X")]},
            ],
            "cross_board_diff_pairs": [{"a": "SDA", "b": "SDB"}],
            "voltage_rules": {"+5V": 5.0},
        }}), encoding="utf-8")
        self.container = root / "system.kicad_pro"

    def tearDown(self):
        self._tmp.cleanup()

    def test_links_are_checked_against_each_board(self):
        zeo = validate_links(build_project_context(self.container, HostDetection(ZEO)).zeo)
        states = {link.name: [endpoint.state for endpoint in link.endpoints] for link in zeo.links}
        self.assertEqual(states, {"+5V": [OK, OK], "SDA": [STALE, OK], "NC": [MISSING]})
        self.assertEqual(zeo.links[1].endpoints[0].detail, "A pad 2 is on SDA_OLD, not SDA")

    def test_nets_are_qualified_by_board_and_joined_only_through_links(self):
        zeo = validate_links(build_project_context(self.container, HostDetection(ZEO)).zeo)
        self.assertIn({("A", "+5V"), ("B", "+5V_IN")}, zeo.system_nets)
        # The stale endpoint joins what its copper is on, not the name the link recorded.
        self.assertIn({("A", "SDA_OLD"), ("B", "SDA")}, zeo.system_nets)

    def test_a_diff_pair_naming_no_link_is_flagged_and_unknown_fields_are_kept(self):
        zeo = validate_links(build_project_context(self.container, HostDetection(ZEO)).zeo)
        self.assertEqual(zeo.diff_pair_issues,
                         ["differential pair SDA / SDB: SDB is no cross-board link, nor a net of any board"])
        self.assertEqual(zeo.raw, {"voltage_rules": {"+5V": 5.0}})


class SchematicRulesInZeoModeTests(ZeoContainerTests):
    def test_the_rules_report_out_of_date_links_only_in_a_zeo_mode(self):
        try:
            from application.schematic_controller import SchematicRuleEngine, SchematicRunRequest
        except ImportError as exc:  # pragma: no cover - the plugin's dependencies
            self.skipTest(str(exc))
        from ingest.project_context import UNKNOWN

        board = self.container.parent / "boards" / "a" / "a.kicad_pcb"

        def run(host):
            context = build_project_context(self.container, HostDetection(host))
            request = SchematicRunRequest(board_path=str(board), project_context=context)
            return SchematicRuleEngine().solve(request, lambda _m: None, lambda *_a: None, lambda: False)

        zeo = run(ZEO)
        self.assertEqual(sorted(f.title for f in zeo.findings if f.rule_id == "SCH-004"), [
            "Cross-board differential pair names no link",
            "Cross-board link out of date", "Cross-board link out of date",
        ])
        self.assertIn("Zeo container mode", zeo.provenance[0].detail)
        self.assertEqual([f for f in run(UNKNOWN).findings if f.rule_id == "SCH-004"], [])


if __name__ == "__main__":
    unittest.main()
