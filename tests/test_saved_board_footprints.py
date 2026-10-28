"""Analyses that read the saved files say when the open board is no longer that board."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

try:
    import shapely  # noqa: F401  (application.dc_controller needs the plugin's dependencies)
    _DEPS = True
except ImportError:  # pragma: no cover
    _DEPS = False

BOARD = """(kicad_pcb (version 20260206) (generator "pcbnew")
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal))
  (footprint "R" (layer "F.Cu") (uuid "r1") (at 10 10 30)
    (property "Reference" "R1")
    (pad "1" smd rect (at -1 0 30) (size 1 1) (layers "F.Cu") (net "A"))
    (pad "2" smd rect (at 1 0 30) (size 1 1) (layers "F.Cu") (net "B")))
  (footprint "C" (layer "F.Cu") (uuid "c1") (at 20 10 0)
    (property "Reference" "C1")
    (pad "1" smd rect (at 0 0 0) (size 1 1) (layers "F.Cu") (net "A"))))
"""

NM = 1e6


def _snapshot(board, move=None, drop=(), add=(), jitter_mm=0.0):
    from application.dc_controller import (
        DCBoardSnapshot, DCFootprintSnapshot, DCNetSnapshot, DCPadSnapshot, DCPointSnapshot,
    )

    footprints = []
    for footprint in board.footprints:
        if footprint.reference in drop:
            continue
        dx, dy = (move or {}).get(footprint.reference, (0.0, 0.0))
        pads = tuple(
            DCPadSnapshot(pad.number, pad.number,
                          DCPointSnapshot((pad.position[0] + dx + jitter_mm) * NM, (pad.position[1] + dy - jitter_mm) * NM),
                          DCNetSnapshot(pad.net_name))
            for pad in footprint.pads
        )
        footprints.append(DCFootprintSnapshot(footprint.reference, pads))
    for reference in add:
        footprints.append(DCFootprintSnapshot(reference, ()))
    return DCBoardSnapshot(tuple(footprints), ())


@unittest.skipUnless(_DEPS, "the plugin's geometry dependencies are not installed")
class SavedFootprintDifferenceTests(unittest.TestCase):
    def setUp(self):
        from ingest.board_reader import read_board

        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "board.kicad_pcb"
        self.path.write_text(BOARD, encoding="utf-8")
        self.board = read_board(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_unedited_board_differs_in_nothing_despite_rounding(self):
        from application.dc_controller import saved_footprint_differences

        self.assertEqual(saved_footprint_differences(_snapshot(self.board, jitter_mm=0.004), self.board), [])

    def test_added_deleted_and_moved_footprints_are_named(self):
        from application.dc_controller import saved_footprint_differences

        snapshot = _snapshot(self.board, move={"R1": (0.5, 0.0)}, drop=("C1",), add=("U9",))
        self.assertEqual(saved_footprint_differences(snapshot, self.board), [
            "1 footprint(s) only on the open board (U9)",
            "1 footprint(s) only in the saved file (C1)",
            "1 footprint(s) placed differently (R1)",
        ])

    def test_the_schematic_rules_report_unsaved_changes(self):
        from application.schematic_controller import SchematicRuleEngine, SchematicRunRequest

        request = SchematicRunRequest(board_path=str(self.path),
                                      board_snapshot=_snapshot(self.board, drop=("C1",)))
        result = SchematicRuleEngine().solve(request, lambda _m: None, lambda *_a: None, lambda: False)
        self.assertTrue(any(text.startswith("Unsaved changes") and "(C1)" in text for text in result.limitations))

    def test_the_power_tree_gives_the_rules_a_rail_voltage(self):
        # Review of 2026-10-02: P02 Rev E's /VBUS_SYS (20 V) names no voltage,
        # so the resistor rules never looked at R62 (47k) on it.
        from application.schematic_controller import SchematicRuleEngine, SchematicRunRequest

        def resistor_rules(rail_voltages):
            request = SchematicRunRequest(board_path=str(self.path), rail_voltages=rail_voltages)
            result = SchematicRuleEngine().solve(request, lambda _m: None, lambda *_a: None, lambda: False)
            return [(f.rule_id, f.components) for f in result.findings
                    if f.rule_id in ("SCH-003", "SCH-007")]

        self.assertEqual(resistor_rules({}), [])
        self.assertEqual(resistor_rules({"A": 20.0}), [("SCH-003", ["R1"])])


if __name__ == "__main__":
    unittest.main()
