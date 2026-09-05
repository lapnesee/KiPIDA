"""Tests for ingest.project_resolver."""

import sys
import os
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest.project_resolver import resolve_project, BoardProject, MultiboardProject

MINIMAL_PRO = {
    "meta": {"filename": "myboard.kicad_pro", "version": 3},
    "board": {},
    "schematic": {},
    "sheets": [
        ["root-uuid", "Root"],
        ["sheet-uuid", "Main Sheet"],
    ],
}

MINIMAL_PRO_MULTIBOARD = {
    "meta": {"filename": "p02.kicad_pro", "version": 3},
    "board": {},
    "schematic": {},
    "sheets": [["root-uuid-p02", "Root"]],
    "multi_board": {"uuid": "uuid-p02", "filename": "../../parent.kicad_mbs"},
}

MINI_MBS = """\
(kicad_sch
    (version 20260306)
    (uuid "mbs-uuid")
    (module_block
        (sub_project "boards/p02/p02.kicad_pro")
        (sub_project_uuid "uuid-p02-x")
        (component "J1")
        (mbs_reference "B1")
        (name "P02 / J1")
        (uuid "block-1")
        (pin
            (uuid "p1")
            (component "J1")
            (number "1")
            (name "GND")
            (at 0 0)
            (electrical_type "passive")
        )
    )
    (module_block
        (sub_project "boards/main/main.kicad_pro")
        (sub_project_uuid "uuid-main-x")
        (component "J2")
        (mbs_reference "B2")
        (name "Main / J2")
        (uuid "block-2")
        (pin
            (uuid "p2")
            (component "J2")
            (number "1")
            (name "GND")
            (at 0 0)
            (electrical_type "passive")
        )
    )
)
"""


class TestResolveFromPro(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.pro_path = Path(self.tmpdir) / "myboard.kicad_pro"
        self.pcb_path = Path(self.tmpdir) / "myboard.kicad_pcb"
        self.sch_path = Path(self.tmpdir) / "myboard.kicad_sch"
        self.pro_path.write_text(json.dumps(MINIMAL_PRO), encoding="utf-8")
        self.pcb_path.write_text("(kicad_pcb)", encoding="utf-8")
        self.sch_path.write_text("(kicad_sch)", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_board_project(self):
        result = resolve_project(self.pro_path)
        self.assertIsInstance(result, BoardProject)

    def test_paths_resolved(self):
        result = resolve_project(self.pro_path)
        self.assertEqual(result.pro_path.name, "myboard.kicad_pro")
        self.assertEqual(result.pcb_path.name, "myboard.kicad_pcb")
        self.assertEqual(result.sch_root_path.name, "myboard.kicad_sch")

    def test_sheets_parsed(self):
        result = resolve_project(self.pro_path)
        self.assertEqual(len(result.sch_sheets), 2)
        self.assertEqual(result.sch_sheets[0][1], "Root")

    def test_not_multiboard(self):
        result = resolve_project(self.pro_path)
        self.assertFalse(result.is_part_of_multiboard)


class TestResolveFromDirectory(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        pro_path = Path(self.tmpdir) / "myboard.kicad_pro"
        pro_path.write_text(json.dumps(MINIMAL_PRO), encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_finds_pro_in_directory(self):
        result = resolve_project(Path(self.tmpdir))
        self.assertIsInstance(result, BoardProject)


class TestResolveFromMbs(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Create directory structure
        self.mbs_path = Path(self.tmpdir) / "container.kicad_mbs"
        self.mbs_path.write_text(MINI_MBS, encoding="utf-8")

        # Create sub-project directories and files
        p02_dir = Path(self.tmpdir) / "boards" / "p02"
        main_dir = Path(self.tmpdir) / "boards" / "main"
        p02_dir.mkdir(parents=True)
        main_dir.mkdir(parents=True)

        p02_pro = {"meta": {"filename": "p02.kicad_pro"}, "sheets": [["r1", "Root"]]}
        main_pro = {"meta": {"filename": "main.kicad_pro"}, "sheets": [["r2", "Root"]]}

        (p02_dir / "p02.kicad_pro").write_text(json.dumps(p02_pro), encoding="utf-8")
        (p02_dir / "p02.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
        (p02_dir / "p02.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
        (main_dir / "main.kicad_pro").write_text(json.dumps(main_pro), encoding="utf-8")
        (main_dir / "main.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
        (main_dir / "main.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_multiboard_project(self):
        result = resolve_project(self.mbs_path)
        self.assertIsInstance(result, MultiboardProject)

    def test_two_boards_found(self):
        result = resolve_project(self.mbs_path)
        self.assertEqual(len(result.boards), 2)

    def test_cross_board_gnd(self):
        result = resolve_project(self.mbs_path)
        net_names = {n.net_name for n in result.cross_board_nets}
        self.assertIn("GND", net_names)

    def test_boards_are_multiboard(self):
        result = resolve_project(self.mbs_path)
        for bp in result.boards:
            self.assertTrue(bp.is_part_of_multiboard)

    def test_mbs_directory_resolution(self):
        # Put a .kicad_mbs in a dir and resolve from directory
        result = resolve_project(Path(self.tmpdir))
        self.assertIsInstance(result, MultiboardProject)


class TestResolveErrors(unittest.TestCase):
    def test_missing_path_raises(self):
        with self.assertRaises((FileNotFoundError, ValueError)):
            resolve_project(Path("/nonexistent/path"))

    def test_unsupported_extension(self):
        with self.assertRaises(ValueError):
            resolve_project(Path("/some/file.kicad_sym"))


if __name__ == "__main__":
    unittest.main()
