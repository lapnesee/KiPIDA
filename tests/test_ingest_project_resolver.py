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


def _connector_board(directory, stem, reference, pads):
    directory.mkdir(parents=True)
    pad_text = " ".join(f'(pad "{n}" smd rect (at {n} 0 0) (size 1 1) (layers "F.Cu") (net "{net}"))' for n, net in pads)
    (directory / f"{stem}.kicad_pcb").write_text(
        f'(kicad_pcb (version 20260206) (layers (0 "F.Cu" signal)) (footprint "Conn" (layer "F.Cu") '
        f'(at 0 0 0) (property "Reference" "{reference}") {pad_text}))', encoding="utf-8")
    (directory / f"{stem}.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    (directory / f"{stem}.kicad_pro").write_text(json.dumps({
        "sheets": [["r", "Root"]],
        "multi_board": {"container": False, "container_project_relative_path": "../../container.kicad_pro"},
    }), encoding="utf-8")


class TestResolveFromMbs(unittest.TestCase):
    """Boards and links come from the container project, never from pin names in the drawing."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        root = Path(self.tmpdir)
        # The drawing labels a pin GND on a connector of each board.
        self.mbs_path = root / "container.kicad_mbs"
        self.mbs_path.write_text(MINI_MBS, encoding="utf-8")
        _connector_board(root / "boards" / "p02", "p02", "J1", [("1", "GND"), ("2", "+5V")])
        _connector_board(root / "boards" / "main", "main", "J2", [("1", "GND"), ("2", "VIN")])
        # The container declares one link, joining nets named differently on each board.
        (root / "container.kicad_pro").write_text(json.dumps({"multi_board": {
            "container": True, "mbs_file": "container.kicad_mbs",
            "sub_projects": [{"name": "p02", "path": "boards/p02/p02.kicad_pro", "uuid": "uuid-p02-x"},
                             {"name": "main", "path": "boards/main/main.kicad_pro", "uuid": "uuid-main-x"}],
            "cross_board_nets": [{"name": "+5V", "uuid": "l1", "endpoints": [
                {"sub_project_uuid": "uuid-p02-x", "component": "J1", "pin": "2", "pin_name": "+5V"},
                {"sub_project_uuid": "uuid-main-x", "component": "J2", "pin": "2", "pin_name": "VIN"},
            ]}],
        }}), encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_multiboard_project(self):
        result = resolve_project(self.mbs_path)
        self.assertIsInstance(result, MultiboardProject)

    def test_two_boards_found(self):
        result = resolve_project(self.mbs_path)
        self.assertEqual(sorted((b.pro_path.name, b.sub_project_uuid) for b in result.boards),
                         [("main.kicad_pro", "uuid-main-x"), ("p02.kicad_pro", "uuid-p02-x")])

    def test_cross_board_nets_are_the_declared_links_only(self):
        result = resolve_project(self.mbs_path)
        self.assertEqual([n.net_name for n in result.cross_board_nets], ["+5V"])
        self.assertEqual(result.cross_board_nets[0].members, [("uuid-main-x", "VIN"), ("uuid-p02-x", "+5V")])

    def test_boards_are_multiboard(self):
        result = resolve_project(self.mbs_path)
        for bp in result.boards:
            self.assertTrue(bp.is_part_of_multiboard)

    def test_mbs_directory_resolution(self):
        # Put a .kicad_mbs in a dir and resolve from directory
        result = resolve_project(Path(self.tmpdir))
        self.assertIsInstance(result, MultiboardProject)


class TestResolveIdentity(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.directory = Path(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_a_directory_with_two_projects_is_an_error_naming_both(self):
        for name in ("amp.kicad_pro", "amp_v2.kicad_pro"):
            (self.directory / name).write_text(json.dumps(MINIMAL_PRO), encoding="utf-8")
        with self.assertRaises(ValueError) as raised:
            resolve_project(self.directory)
        self.assertIn("amp.kicad_pro", str(raised.exception))
        self.assertIn("amp_v2.kicad_pro", str(raised.exception))

    def test_a_standalone_zeo_project_is_not_part_of_a_multi_board_project(self):
        # Zeo writes a multi_board block into every project it saves.
        pro = dict(MINIMAL_PRO, multi_board={"container": False, "container_project_relative_path": ""})
        pro_path = self.directory / "solo.kicad_pro"
        pro_path.write_text(json.dumps(pro), encoding="utf-8")
        (self.directory / "solo.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
        result = resolve_project(pro_path)
        self.assertFalse(result.is_part_of_multiboard)
        self.assertEqual(result.status, "schematic_only")


class TestResolveErrors(unittest.TestCase):
    def test_missing_path_raises(self):
        with self.assertRaises((FileNotFoundError, ValueError)):
            resolve_project(Path("/nonexistent/path"))

    def test_unsupported_extension(self):
        with self.assertRaises(ValueError):
            resolve_project(Path("/some/file.kicad_sym"))


if __name__ == "__main__":
    unittest.main()
