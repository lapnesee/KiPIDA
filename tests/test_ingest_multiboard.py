"""Tests for ingest.multiboard — .kicad_mbs parser."""

import sys
import os
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest.multiboard import parse_mbs, extract_cross_board_nets, MbsPin, MbsModuleBlock, CrossBoardNet

MINI_MBS = """\
(kicad_sch
    (version 20260306)
    (generator "eeschema")
    (uuid "top-uuid")
    (module_block
        (at 39.37 26.67)
        (size 50.8 45.72)
        (sub_project "boards/p02/p02.kicad_pro")
        (sub_project_uuid "uuid-p02")
        (component "J6")
        (mbs_reference "B5")
        (name "P02 / J6")
        (uuid "block-uuid-1")
        (pin
            (uuid "pin-uuid-1")
            (component "J6")
            (number "6")
            (name "+5V_RAIL")
            (at 0 10.16)
            (electrical_type "passive")
        )
        (pin
            (uuid "pin-uuid-2")
            (component "J6")
            (number "7")
            (name "GND")
            (at 0 16.51)
            (electrical_type "passive")
        )
    )
    (module_block
        (at 39.37 97.79)
        (size 50.8 45.72)
        (sub_project "boards/main/main.kicad_pro")
        (sub_project_uuid "uuid-main")
        (component "J1")
        (mbs_reference "B13")
        (name "Main / J1")
        (uuid "block-uuid-2")
        (pin
            (uuid "pin-uuid-3")
            (component "J1")
            (number "6")
            (name "+5V_RAIL")
            (at 0 10.16)
            (electrical_type "passive")
        )
        (pin
            (uuid "pin-uuid-4")
            (component "J1")
            (number "7")
            (name "GND")
            (at 0 16.51)
            (electrical_type "passive")
        )
        (pin
            (uuid "pin-uuid-5")
            (component "J1")
            (number "4")
            (name "J1.4")
            (at 50.8 16.51)
            (electrical_type "passive")
        )
    )
)
"""


class TestParseMbs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".kicad_mbs", mode="w",
                                               encoding="utf-8", delete=False)
        self.tmp.write(MINI_MBS)
        self.tmp.close()
        self.mbs_path = Path(self.tmp.name)

    def tearDown(self):
        self.mbs_path.unlink(missing_ok=True)

    def test_block_count(self):
        blocks = parse_mbs(self.mbs_path)
        self.assertEqual(len(blocks), 2)

    def test_first_block_fields(self):
        blocks = parse_mbs(self.mbs_path)
        b = blocks[0]
        self.assertEqual(b.sub_project_uuid, "uuid-p02")
        self.assertEqual(b.component, "J6")
        self.assertEqual(b.mbs_reference, "B5")
        self.assertIn("P02", b.name)

    def test_first_block_pins(self):
        blocks = parse_mbs(self.mbs_path)
        b = blocks[0]
        self.assertEqual(len(b.pins), 2)
        self.assertEqual(b.pins[0].name, "+5V_RAIL")
        self.assertEqual(b.pins[1].name, "GND")
        self.assertEqual(b.pins[0].electrical_type, "passive")

    def test_second_block_has_three_pins(self):
        blocks = parse_mbs(self.mbs_path)
        self.assertEqual(len(blocks[1].pins), 3)

    def test_empty_file(self):
        tmp2 = tempfile.NamedTemporaryFile(suffix=".kicad_mbs", mode="w",
                                            encoding="utf-8", delete=False)
        tmp2.write("")
        tmp2.close()
        blocks = parse_mbs(Path(tmp2.name))
        self.assertEqual(blocks, [])
        Path(tmp2.name).unlink(missing_ok=True)


class TestExtractCrossBoardNets(unittest.TestCase):
    def _blocks(self):
        return [
            MbsModuleBlock(
                sub_project_path="boards/p02/p02.kicad_pro",
                sub_project_uuid="uuid-p02",
                component="J6",
                mbs_reference="B5",
                name="P02",
                pins=[
                    MbsPin("6", "+5V_RAIL", "passive"),
                    MbsPin("7", "GND", "passive"),
                ],
            ),
            MbsModuleBlock(
                sub_project_path="boards/main/main.kicad_pro",
                sub_project_uuid="uuid-main",
                component="J1",
                mbs_reference="B13",
                name="Main",
                pins=[
                    MbsPin("6", "+5V_RAIL", "passive"),
                    MbsPin("7", "GND", "passive"),
                    MbsPin("4", "J1.4", "passive"),  # auto-name → excluded
                ],
            ),
        ]

    def test_cross_nets_found(self):
        nets = extract_cross_board_nets(self._blocks())
        names = {n.net_name for n in nets}
        self.assertIn("+5V_RAIL", names)
        self.assertIn("GND", names)

    def test_auto_name_excluded(self):
        nets = extract_cross_board_nets(self._blocks())
        names = {n.net_name for n in nets}
        self.assertNotIn("J1.4", names)

    def test_boards_list(self):
        nets = extract_cross_board_nets(self._blocks())
        rail = next(n for n in nets if n.net_name == "+5V_RAIL")
        self.assertIn("uuid-p02", rail.boards)
        self.assertIn("uuid-main", rail.boards)

    def test_single_board_net_not_cross(self):
        # Only one block → no cross-board nets
        nets = extract_cross_board_nets([self._blocks()[0]])
        self.assertEqual(nets, [])


if __name__ == "__main__":
    unittest.main()
