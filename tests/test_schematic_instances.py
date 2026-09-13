"""The schematic is read by sheet instance, not by file.

A sheet file placed twice was read once: complex_hierarchy gave 73 of its 119
symbols and multichannel 61 of 169, each copy under whatever reference its own
property held, so the rules saw references that exist nowhere on the board.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from ingest.schematic_reader import read_schematic


def _symbol(uuid, reference_property, entries, project="demo"):
    paths = "".join(
        f'(path "{path}" (reference "{reference}") (unit 1))' for path, reference in entries
    )
    return (
        f'(symbol (lib_id "Device:R") (uuid "{uuid}") '
        f'(property "Reference" "{reference_property}") (property "Value" "10k") '
        f'(instances (project "{project}" {paths})))'
    )


def _sheet(uuid, filename):
    return f'(sheet (uuid "{uuid}") (property "Sheetname" "{uuid}") (property "Sheetfile" "{filename}"))'


class SheetInstanceTests(unittest.TestCase):
    def _read(self, files):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for name, body in files.items():
                (directory / name).write_text(f"(kicad_sch (version 20250114) {body})", encoding="utf-8")
            return read_schematic(directory / "root.kicad_sch")

    def test_a_sheet_placed_twice_contributes_its_symbols_twice(self):
        parsed = self._read({
            "root.kicad_sch": '(uuid "root") ' + _sheet("left", "amp.kicad_sch") + _sheet("right", "amp.kicad_sch"),
            "amp.kicad_sch": '(uuid "amp") ' + _symbol("r", "R?", [("/root/left", "R201"), ("/root/right", "R301")]),
        })
        by_reference = {item.reference: item for item in parsed.instances}
        self.assertEqual(sorted(by_reference), ["R201", "R301"])
        # The key a PCB footprint carries: sheet instance path without the root, then the symbol.
        self.assertEqual(by_reference["R201"].footprint_path, "/left/r")
        self.assertEqual(by_reference["R301"].footprint_path, "/right/r")
        self.assertEqual(by_reference["R301"].sheet_path, "/root/right")

    def test_the_instance_entry_is_chosen_by_path_whatever_the_project_name(self):
        parsed = self._read({
            "root.kicad_sch": '(uuid "root") ' + _symbol("r", "R?", [("/other/place", "R9"), ("/root", "R1")],
                                                         project="name_before_the_rename"),
        })
        self.assertEqual([item.reference for item in parsed.instances], ["R1"])
        self.assertEqual(parsed.instances[0].footprint_path, "/r")

    def test_three_levels_chain_every_sheet_uuid(self):
        parsed = self._read({
            "root.kicad_sch": '(uuid "root") ' + _sheet("a", "mid.kicad_sch"),
            "mid.kicad_sch": '(uuid "mid") ' + _sheet("b", "leaf.kicad_sch"),
            "leaf.kicad_sch": '(uuid "leaf") ' + _symbol("r", "R?", [("/root/a/b", "R7")]),
        })
        self.assertEqual([(i.reference, i.footprint_path) for i in parsed.instances], [("R7", "/a/b/r")])


class SheetFileIssueTests(unittest.TestCase):
    """A sheet that cannot be followed, or only on some machines, is said so."""

    def _read(self, sheetfile, disk_name="amp.kicad_sch", subdirectory=""):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / subdirectory).mkdir(exist_ok=True)
            (directory / subdirectory / disk_name).write_text(
                '(kicad_sch (uuid "amp") ' + _symbol("r", "R?", [("/root/s", "R1")]) + ")", encoding="utf-8")
            root = directory / "root.kicad_sch"
            root.write_text('(kicad_sch (uuid "root") ' + _sheet("s", sheetfile) + ")", encoding="utf-8")
            return read_schematic(root)

    def test_a_missing_sheet_is_an_issue_naming_it(self):
        parsed = self._read("absent.kicad_sch")
        self.assertEqual(parsed.instances, [])
        self.assertEqual([(i.kind, i.sheet_file, i.sheet_path) for i in parsed.issues],
                         [("missing_sheet", "absent.kicad_sch", "/root/s")])

    def test_a_name_differing_in_case_is_read_and_flagged(self):
        parsed = self._read("AMP.kicad_sch")
        self.assertEqual([i.reference for i in parsed.instances], ["R1"])
        self.assertEqual([i.kind for i in parsed.issues], ["sheet_file_case"])

    def test_a_backslash_separator_is_read_and_flagged(self):
        parsed = self._read("sub\\amp.kicad_sch", subdirectory="sub")
        self.assertEqual([i.reference for i in parsed.instances], ["R1"])
        self.assertEqual([i.kind for i in parsed.issues], ["sheet_file_separator"])


if __name__ == "__main__":
    unittest.main()
