"""A project's declarations are read once while its files stay unchanged, and not at any size."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from ingest.declarations import DesignReadDeclined, design_netlist
from ingest.project_resolver import BoardProject


class DesignNetlistCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.pcb = root / "board.kicad_pcb"
        self.pcb.write_text('(kicad_pcb (version 20260206) (layers (0 "F.Cu" signal)))', encoding="utf-8")
        (root / "board.kicad_sch").write_text(
            '(kicad_sch (version 20260306) (uuid "r") (sheet (uuid "s") (property "Sheetfile" "child.kicad_sch")))',
            encoding="utf-8")
        self.child = root / "child.kicad_sch"
        self.child.write_text('(kicad_sch (version 20260306) (uuid "c"))', encoding="utf-8")
        self.project = BoardProject(root / "board.kicad_pro", self.pcb, root / "board.kicad_sch", [], False, "")

    def tearDown(self):
        self._tmp.cleanup()

    def test_read_once_until_any_file_read_changes(self):
        first = design_netlist(self.project)
        self.assertIs(design_netlist(self.project), first)
        # An edit below the root sheet is a different design.
        time.sleep(0.01)
        self.child.write_text('(kicad_sch (version 20260306) (uuid "c") (global_label "X"))', encoding="utf-8")
        self.assertIsNot(design_netlist(self.project), first)

    def test_a_board_over_the_limit_is_declined_with_its_size(self):
        with self.assertRaises(DesignReadDeclined) as raised:
            design_netlist(self.project, size_limit_mb=1e-6)
        self.assertIn("board.kicad_pcb", str(raised.exception))
        self.assertIsNotNone(design_netlist(self.project, size_limit_mb=None))



class ProtectionClassTests(unittest.TestCase):
    def test_tvs_and_esd_parts_are_classified_from_their_symbols(self):
        # TVS/ESD protection was recognised only by a regex on the value
        # (previous audits' item B); the schematic now names it.
        from types import SimpleNamespace
        from ingest.declarations import PROTECTION, component_classes

        def part(reference, symbol):
            return SimpleNamespace(reference=reference, symbol_lib_id=symbol, lib_id="",
                                   pin_types_by_pad={})
        netlist = SimpleNamespace(components=[
            part("U2", "Power_Protection:USBLC6-2SC6"), part("D5", "Device:D_TVS"),
            part("D6", "Device:D_Schottky"),
        ])
        classes = component_classes(netlist)
        self.assertIn(PROTECTION, classes["U2"])
        self.assertIn(PROTECTION, classes["D5"])
        self.assertNotIn(PROTECTION, classes["D6"])

if __name__ == "__main__":
    unittest.main()
