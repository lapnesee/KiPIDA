"""The Zeo multi-board layer applies only when Zeo launched the plugin.

One test per row of docs/plan-universalite.md section 3.1, on a synthetic tree
shaped like a Zeo container: the container's .kicad_pro lists its boards in
multi_board.sub_projects, and each board names its container by a relative path.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from ingest.project_context import (
    HostDetection, KICAD, SINGLE_BOARD, UNKNOWN, ZEO, ZEO_CONTAINER, ZEO_MEMBER,
    build_project_context, detect_host,
)


def _write_pro(path, multi_board):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"meta": {"version": 3}, "multi_board": multi_board}), encoding="utf-8")
    path.with_suffix(".kicad_pcb").write_text("(kicad_pcb (version 20260206))", encoding="utf-8")
    path.with_suffix(".kicad_sch").write_text('(kicad_sch (version 20260306) (uuid "r"))', encoding="utf-8")


class ProjectContextTableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.container = root / "system.kicad_pro"
        self.power = root / "boards" / "power" / "power.kicad_pro"
        self.stray = root / "boards" / "stray" / "stray.kicad_pro"
        self.solo = root / "solo" / "solo.kicad_pro"
        _write_pro(self.container, {
            "container": True, "container_project_relative_path": "", "mbs_file": "system.kicad_mbs",
            "sub_projects": [{"name": "power", "path": "boards/power/power.kicad_pro", "uuid": "uuid-power"},
                             {"name": "gone", "path": "boards/gone/gone.kicad_pro", "uuid": "uuid-gone"}],
        })
        (root / "system.kicad_mbs").write_text("(kicad_sch (version 20260306))", encoding="utf-8")
        member = {"container": False, "container_project_relative_path": "../../system.kicad_pro"}
        _write_pro(self.power, member)
        _write_pro(self.stray, member)
        _write_pro(self.solo, {"container": False, "container_project_relative_path": ""})

    def tearDown(self):
        self._tmp.cleanup()

    def _context(self, path, host):
        return build_project_context(path, HostDetection(host))

    def test_kicad_reads_a_member_as_a_single_board_without_opening_the_container(self):
        opened = []
        real = Path.read_text

        def spy(path, *args, **kwargs):
            opened.append(Path(path).name)
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", spy):
            context = self._context(self.power, KICAD)
        self.assertEqual((context.mode, context.notes), (SINGLE_BOARD, []))
        self.assertNotIn("system.kicad_pro", opened)
        self.assertNotIn("system.kicad_mbs", opened)

    def test_zeo_on_a_container_analyses_the_boards_it_lists(self):
        context = self._context(self.container, ZEO)
        self.assertEqual(context.mode, ZEO_CONTAINER)
        self.assertEqual([(b.pro_path.name, b.sub_project_uuid) for b in context.boards],
                         [("power.kicad_pro", "uuid-power")])
        self.assertEqual(context.notes, ["board gone listed by the container does not exist"])

    def test_zeo_on_a_listed_board_is_member_mode(self):
        context = self._context(self.power, ZEO)
        self.assertEqual((context.mode, context.container_path), (ZEO_MEMBER, self.container.resolve()))
        self.assertEqual(context.project.sub_project_uuid, "uuid-power")

    def test_zeo_on_a_standalone_project_is_single_board_and_says_so(self):
        context = self._context(self.solo, ZEO)
        self.assertEqual((context.mode, context.notes), (SINGLE_BOARD, ["Zeo, standalone project"]))

    def test_zeo_on_a_board_its_container_does_not_list_is_an_orphan(self):
        context = self._context(self.stray, ZEO)
        self.assertEqual(context.mode, SINGLE_BOARD)
        self.assertEqual(context.notes, ["orphan board: its container system.kicad_pro does not list it"])

    def test_container_paths_written_with_backslashes_resolve_on_any_system(self):
        _write_pro(self.container, {
            "container": True, "container_project_relative_path": "",
            "sub_projects": [{"name": "power", "path": "boards\\power\\power.kicad_pro", "uuid": "uuid-power"}],
        })
        _write_pro(self.power, {"container": False, "container_project_relative_path": "..\\..\\system.kicad_pro"})
        self.assertEqual([b.pro_path.name for b in self._context(self.container, ZEO).boards], ["power.kicad_pro"])
        self.assertEqual(self._context(self.power, ZEO).mode, ZEO_MEMBER)

    def test_an_unknown_host_is_single_board_whatever_the_project(self):
        context = self._context(self.container, UNKNOWN)
        self.assertEqual((context.mode, [b.pro_path.name for b in context.boards]),
                         (SINGLE_BOARD, ["system.kicad_pro"]))
        self.assertIn("inconclusive", context.notes[0])


class ProvenanceTests(unittest.TestCase):
    def test_the_campaign_report_names_the_host_and_the_mode(self):
        from campaign import CampaignResult
        from report.html_report import render_campaign_html

        context = {"host": "unknown", "mode": "single_board",
                   "notes": ["host detection inconclusive, so the Zeo multi-board layer is not applied"]}
        campaign = CampaignResult.from_dict(CampaignResult.from_results([], project_context=context).to_dict())
        self.assertEqual(campaign.project_context["mode"], "single_board")
        html = render_campaign_html(campaign)
        self.assertIn("<th>Project mode</th><td>single-board &mdash; host detection inconclusive", html)


class DetectHostTests(unittest.TestCase):
    def test_no_rule_is_chosen_yet_and_evidence_carries_names_only(self):
        detection = detect_host(client=None, environ={"KICAD_API_TOKEN": "secret-value"}, ancestry=["Zeo.exe"])
        self.assertEqual(detection.host, UNKNOWN)
        joined = " ".join(detection.evidence)
        self.assertIn("KICAD_API_TOKEN", joined)
        self.assertNotIn("secret-value", joined)


if __name__ == "__main__":
    unittest.main()
