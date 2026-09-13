"""Files saved by KiCad 7 or earlier are refused by name and version, not read halfway."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from ingest.board_reader import read_board
from ingest.file_format import UnsupportedFileFormat
from ingest.schematic_reader import read_schematic


class FileFormatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, text):
        path = self.directory / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_kicad7_board_is_refused_naming_its_version(self):
        board = self._write("old.kicad_pcb", "(kicad_pcb (version 20221018) (generator pcbnew))")
        with self.assertRaises(UnsupportedFileFormat) as raised:
            read_board(board)
        self.assertIn("20221018", str(raised.exception))
        self.assertIn("old.kicad_pcb", str(raised.exception))

    def test_a_kicad7_root_schematic_is_refused(self):
        root = self._write("old.kicad_sch", '(kicad_sch (version 20230121) (uuid "root"))')
        with self.assertRaises(UnsupportedFileFormat):
            read_schematic(root)

    def test_an_old_sub_sheet_of_a_current_project_is_read_and_flagged(self):
        # KiCad does not rewrite a sheet nobody edited; the vme-wren demo keeps one at
        # 20230819, from KiCad 8's development cycle, and that one is not flagged.
        self._write("kept.kicad_sch", '(kicad_sch (version 20230819) (uuid "k") '
                                      '(symbol (lib_id "Device:R") (uuid "r1") (property "Reference" "R1")))')
        self._write("older.kicad_sch", '(kicad_sch (version 20230121) (uuid "o") '
                                       '(symbol (lib_id "Device:R") (uuid "r2") (property "Reference" "R2")))')
        root = self._write("root.kicad_sch", '(kicad_sch (version 20250114) (uuid "root") '
                                             '(sheet (uuid "a") (property "Sheetfile" "kept.kicad_sch")) '
                                             '(sheet (uuid "b") (property "Sheetfile" "older.kicad_sch")))')
        parsed = read_schematic(root)
        self.assertEqual(sorted(i.reference for i in parsed.instances), ["R1", "R2"])
        self.assertEqual([(i.kind, i.sheet_file) for i in parsed.issues], [("old_sheet_format", "older.kicad_sch")])


if __name__ == "__main__":
    unittest.main()
