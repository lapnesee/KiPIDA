"""Tests for ingest.schematic_reader — .kicad_sch parser."""

import sys
import os
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest.schematic_reader import read_schematic

MINI_SCH = """\
(kicad_sch
    (version 20260306)
    (generator "eeschema")
    (uuid "root-uuid")
    (paper "A4")
    (lib_symbols
        (symbol "Device:C"
            (pin_names (offset 0.254))
            (exclude_from_sim no)
            (in_bom yes)
            (on_board yes)
            (symbol "C_0_1"
                (pin passive line
                    (at 0 3.81 270)
                    (length 1.27)
                    (name "~" (effects (font (size 1.27 1.27))))
                    (number "1" (effects (font (size 1.27 1.27))))
                )
                (pin passive line
                    (at 0 -3.81 90)
                    (length 1.27)
                    (name "~" (effects (font (size 1.27 1.27))))
                    (number "2" (effects (font (size 1.27 1.27))))
                )
            )
        )
        (symbol "power:VCC"
            (power global)
            (symbol "VCC_0_1"
                (pin power_out line
                    (at 0 0 270)
                    (length 0)
                    (name "PWR" (effects (font (size 1.27 1.27))))
                    (number "1" (effects (font (size 1.27 1.27))))
                )
            )
        )
        (symbol "Regulator_Linear:LM7805"
            (exclude_from_sim no)
            (in_bom yes)
            (on_board yes)
            (symbol "LM7805_0_1"
                (pin power_in line
                    (at -5.08 0 0)
                    (length 1.27)
                    (name "VI" (effects (font (size 1.27 1.27))))
                    (number "1" (effects (font (size 1.27 1.27))))
                )
                (pin power_out line
                    (at 5.08 0 180)
                    (length 1.27)
                    (name "VO" (effects (font (size 1.27 1.27))))
                    (number "2" (effects (font (size 1.27 1.27))))
                )
                (pin power_in line
                    (at 0 -5.08 90)
                    (length 1.27)
                    (name "GND" (effects (font (size 1.27 1.27))))
                    (number "3" (effects (font (size 1.27 1.27))))
                )
            )
        )
    )
    (global_label "+5V_RAIL"
        (shape bidirectional)
        (at 100 100 0)
        (uuid "gl-uuid-1")
    )
    (global_label "GND"
        (shape bidirectional)
        (at 100 110 0)
        (uuid "gl-uuid-2")
    )
    (symbol
        (lib_id "Regulator_Linear:LM7805")
        (at 100 50 0)
        (unit 1)
        (body_style 1)
        (exclude_from_sim no)
        (in_bom yes)
        (on_board yes)
        (uuid "sym-uuid-1")
        (property "Reference" "U1"
            (at 105 44 0)
        )
        (property "Value" "LM7805"
            (at 105 46 0)
        )
        (property "Footprint" "Package_TO_SOT_THT:TO-220-3_Vertical"
            (at 105 48 0)
            (hide yes)
        )
        (property "Datasheet" "http://www.ti.com/lit/ds/symlink/lm340.pdf"
            (at 105 50 0)
            (hide yes)
        )
        (property "Mouser Part Number" "926-LM7805CT"
            (at 105 52 0)
            (hide yes)
        )
        (pin "1" (uuid "pin-inst-1"))
        (pin "2" (uuid "pin-inst-2"))
        (pin "3" (uuid "pin-inst-3"))
        (instances
            (project "test_proj"
                (path "/root-uuid/sub-sheet-uuid"
                    (reference "U1")
                    (unit 1)
                )
            )
        )
    )
    (symbol
        (lib_id "Device:C")
        (at 80 50 0)
        (unit 1)
        (body_style 1)
        (exclude_from_sim no)
        (in_bom yes)
        (on_board yes)
        (uuid "sym-uuid-2")
        (property "Reference" "C1"
            (at 82 44 0)
        )
        (property "Value" "100nF"
            (at 82 46 0)
        )
        (property "Footprint" "Capacitor_SMD:C_0603_1608Metric"
            (at 82 48 0)
            (hide yes)
        )
        (property "Datasheet" ""
            (at 82 50 0)
            (hide yes)
        )
        (instances
            (project "test_proj"
                (path "/root-uuid"
                    (reference "C1")
                    (unit 1)
                )
            )
        )
    )
)
"""


class TestReadSchematic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            suffix=".kicad_sch", mode="w", encoding="utf-8", delete=False
        )
        self.tmp.write(MINI_SCH)
        self.tmp.close()
        self.sch_path = Path(self.tmp.name)
        self.result = read_schematic(self.sch_path)

    def tearDown(self):
        self.sch_path.unlink(missing_ok=True)

    def test_root_path_stored(self):
        self.assertEqual(self.result.root_sch_path.resolve(), self.sch_path.resolve())

    def test_symbol_defs_extracted(self):
        self.assertIn("Regulator_Linear:LM7805", self.result.symbol_defs)
        self.assertIn("Device:C", self.result.symbol_defs)

    def test_regulator_pin_types(self):
        defn = self.result.symbol_defs["Regulator_Linear:LM7805"]
        types = {p.number: p.electrical_type for p in defn.pins}
        self.assertEqual(types["1"], "power_in")
        self.assertEqual(types["2"], "power_out")
        self.assertEqual(types["3"], "power_in")

    def test_capacitor_pin_types(self):
        defn = self.result.symbol_defs["Device:C"]
        for p in defn.pins:
            self.assertEqual(p.electrical_type, "passive")

    def test_instances_count(self):
        refs = {i.reference for i in self.result.instances}
        self.assertIn("U1", refs)
        self.assertIn("C1", refs)

    def test_u1_properties(self):
        u1 = next(i for i in self.result.instances if i.reference == "U1")
        self.assertEqual(u1.value, "LM7805")
        self.assertEqual(u1.lib_id, "Regulator_Linear:LM7805")
        self.assertIn("Mouser Part Number", u1.extra_fields)

    def test_u1_pin_types_resolved(self):
        u1 = next(i for i in self.result.instances if i.reference == "U1")
        types = {p.number: p.electrical_type for p in u1.pins}
        self.assertEqual(types.get("2"), "power_out")

    def test_global_nets(self):
        self.assertIn("+5V_RAIL", self.result.global_nets)
        self.assertIn("GND", self.result.global_nets)

    def test_sheet_path_extracted(self):
        u1 = next(i for i in self.result.instances if i.reference == "U1")
        self.assertIn("root-uuid", u1.sheet_path)


class TestReadSchematicMissingFile(unittest.TestCase):
    def test_missing_file_raises(self):
        with self.assertRaises(OSError):
            read_schematic(Path("/nonexistent/path/x.kicad_sch"))


if __name__ == "__main__":
    unittest.main()
