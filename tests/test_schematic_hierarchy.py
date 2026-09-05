"""The reader must follow the sheet hierarchy, not stop at the root.

KiCad 6+ names a child sheet in a property, not a node of its own:

    (sheet ... (property "Sheetfile" "child.kicad_sch" ...) ...)

The reader looked only for a bare (sheetfile ...), matched nothing, and never
recursed. On the reference project that meant 11 symbols instead of 125, and
SCH-005 then reported 119 footprints as lacking a schematic symbol -- all
false.
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

_CHILD = """(kicad_sch
	(uuid "child-uuid")
	(symbol
		(lib_id "Device:R")
		(uuid "r1-uuid")
		(property "Reference" "R1")
		(property "Value" "10k")
	)
	(symbol
		(lib_id "Device:C")
		(uuid "c1-uuid")
		(property "Reference" "C1")
		(property "Value" "100nF")
	)
)
"""

# The property form, which is what KiCad actually writes.
_ROOT_PROPERTY_FORM = """(kicad_sch
	(uuid "root-uuid")
	(symbol
		(lib_id "Connector:Conn_01x02")
		(uuid "j1-uuid")
		(property "Reference" "J1")
		(property "Value" "Conn_01x02")
	)
	(sheet
		(uuid "sheet-uuid")
		(property "Sheetname" "Child")
		(property "Sheetfile" "child.kicad_sch")
	)
)
"""

# The node form, kept working for older files.
_ROOT_NODE_FORM = """(kicad_sch
	(uuid "root-uuid")
	(sheet
		(uuid "sheet-uuid")
		(sheetfile "child.kicad_sch")
	)
)
"""


class SheetRecursionTests(unittest.TestCase):
    def _read(self, root_text):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "child.kicad_sch").write_text(_CHILD, encoding="utf-8")
            root = directory / "root.kicad_sch"
            root.write_text(root_text, encoding="utf-8")
            return read_schematic(root)

    def test_the_property_form_is_followed(self):
        parsed = self._read(_ROOT_PROPERTY_FORM)
        references = sorted(item.reference for item in parsed.instances)
        self.assertEqual(references, ["C1", "J1", "R1"])

    def test_the_node_form_still_works(self):
        parsed = self._read(_ROOT_NODE_FORM)
        self.assertEqual(
            sorted(item.reference for item in parsed.instances), ["C1", "R1"],
        )

    def test_a_missing_child_does_not_lose_the_root(self):
        root_text = _ROOT_PROPERTY_FORM.replace("child.kicad_sch", "absent.kicad_sch")
        parsed = self._read(root_text)
        self.assertEqual([item.reference for item in parsed.instances], ["J1"])


if __name__ == "__main__":
    unittest.main()
