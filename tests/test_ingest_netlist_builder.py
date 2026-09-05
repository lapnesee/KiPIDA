"""Tests for ingest.netlist_builder — PCB+schematic fusion."""

import sys
import os
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest.board_reader import (
    ParsedBoard, Stackup, BoardBounds, Footprint, Pad, Segment, Via, Zone,
    CopperLayer,
)
from ingest.schematic_reader import ParsedSchematic, SymbolDef, SymbolInstance, PinDef
from ingest.multiboard import CrossBoardNet
from ingest.netlist_builder import build_netlist, _estimate_voltage


def _minimal_stackup():
    return Stackup(
        layers=[CopperLayer(0, "F.Cu", "signal", 0.035)],
        total_thickness_mm=1.6,
        copper_layer_count=1,
    )


def _minimal_bounds():
    return BoardBounds(x_min=0, y_min=0, x_max=50, y_max=30)


def _make_board(footprints=None, segments=None, vias=None, zones=None):
    return ParsedBoard(
        pcb_path=Path("/fake/board.kicad_pcb"),
        stackup=_minimal_stackup(),
        bounds=_minimal_bounds(),
        footprints=footprints or [],
        segments=segments or [],
        vias=vias or [],
        zones=zones or [],
    )


def _make_schematic(instances=None, global_nets=None):
    return ParsedSchematic(
        root_sch_path=Path("/fake/board.kicad_sch"),
        symbol_defs={},
        instances=instances or [],
        global_nets=global_nets or [],
    )


class TestBuildNetlistBasic(unittest.TestCase):
    def test_empty_board(self):
        board = _make_board()
        nl = build_netlist(board, None, "uuid-test")
        self.assertEqual(nl.board_uuid, "uuid-test")
        self.assertEqual(nl.components, [])
        self.assertEqual(nl.nets, [])

    def test_single_footprint_pads_create_nets(self):
        fp = Footprint(
            reference="R1", value="10k", lib_id="Resistor_SMD:R_0603",
            position=(10, 10), rotation=0, layer="F.Cu",
            sch_path="", sch_sheet_file="",
            pads=[
                Pad("1", "+3V3_MAIN", "passive", (9.5, 10)),
                Pad("2", "GND", "passive", (10.5, 10)),
            ],
        )
        board = _make_board(footprints=[fp])
        nl = build_netlist(board, None, "test-uuid")
        net_names = {n.name for n in nl.nets}
        self.assertIn("+3V3_MAIN", net_names)
        self.assertIn("GND", net_names)

    def test_component_in_components_list(self):
        fp = Footprint(
            reference="C1", value="100nF", lib_id="Device:C",
            position=(5, 5), rotation=0, layer="F.Cu",
            sch_path="", sch_sheet_file="",
            pads=[Pad("1", "+5V", "passive", (5, 5))],
        )
        board = _make_board(footprints=[fp])
        nl = build_netlist(board, None, "uuid-x")
        self.assertEqual(len(nl.components), 1)
        self.assertEqual(nl.components[0].reference, "C1")


class TestPowerRailClassification(unittest.TestCase):
    def _board_with_sch(self):
        # Footprint with sch_path linking to regulator
        fp = Footprint(
            reference="U1", value="LM7805", lib_id="Regulator_Linear:LM7805",
            position=(10, 10), rotation=0, layer="F.Cu",
            sch_path="/root-uuid/sym-uuid-1",
            sch_sheet_file="sheet.kicad_sch",
            pads=[
                Pad("1", "VIN", "passive", (9, 10)),    # power_in
                Pad("2", "+5V_OUT", "passive", (11, 10)),  # power_out
                Pad("3", "GND", "passive", (10, 11)),   # power_in
            ],
        )
        board = _make_board(footprints=[fp])

        sym = SymbolInstance(
            lib_id="Regulator_Linear:LM7805",
            uuid="sym-uuid-1",
            reference="U1",
            value="LM7805",
            footprint="",
            datasheet="",
            extra_fields={},
            sheet_path="/root-uuid/sym-uuid-1",
            pins=[
                PinDef("1", "VI", "power_in"),
                PinDef("2", "VO", "power_out"),
                PinDef("3", "GND", "power_in"),
            ],
        )
        sch = _make_schematic(instances=[sym])
        return board, sch

    def test_power_out_source_detected(self):
        board, sch = self._board_with_sch()
        nl = build_netlist(board, sch, "uuid-reg")
        out_net = next(n for n in nl.nets if n.name == "+5V_OUT")
        self.assertTrue(out_net.has_power_out_source)
        self.assertTrue(out_net.is_power_rail)

    def test_power_in_is_power_rail_but_not_source(self):
        board, sch = self._board_with_sch()
        nl = build_netlist(board, sch, "uuid-reg")
        vin_net = next(n for n in nl.nets if n.name == "VIN")
        self.assertTrue(vin_net.is_power_rail)
        self.assertFalse(vin_net.has_power_out_source)

    def test_pin_type_from_schematic(self):
        board, sch = self._board_with_sch()
        nl = build_netlist(board, sch, "uuid-reg")
        out_net = next(n for n in nl.nets if n.name == "+5V_OUT")
        pin = next(p for p in out_net.pins if p.footprint_ref == "U1")
        self.assertEqual(pin.electrical_type, "power_out")


class TestCrossBoardAliases(unittest.TestCase):
    def test_aliases_populated(self):
        fp = Footprint(
            reference="J1", value="Conn", lib_id="Connector:J",
            position=(5, 5), rotation=0, layer="F.Cu",
            sch_path="", sch_sheet_file="",
            pads=[Pad("1", "GND", "passive", (5, 5))],
        )
        board = _make_board(footprints=[fp])
        cross = [CrossBoardNet(net_name="GND", boards=["uuid-p02", "uuid-main"])]
        nl = build_netlist(board, None, "uuid-p02", cross_board_nets=cross)
        gnd = next(n for n in nl.nets if n.name == "GND")
        self.assertTrue(any("uuid-main" in a for a in gnd.aliases))

    def test_no_alias_for_same_board(self):
        fp = Footprint(
            reference="J1", value="Conn", lib_id="Connector:J",
            position=(5, 5), rotation=0, layer="F.Cu",
            sch_path="", sch_sheet_file="",
            pads=[Pad("1", "GND", "passive", (5, 5))],
        )
        board = _make_board(footprints=[fp])
        cross = [CrossBoardNet(net_name="GND", boards=["uuid-p02", "uuid-main"])]
        nl = build_netlist(board, None, "uuid-p02", cross_board_nets=cross)
        gnd = next(n for n in nl.nets if n.name == "GND")
        self.assertFalse(any("uuid-p02" in a for a in gnd.aliases))


class TestVoltageEstimation(unittest.TestCase):
    def test_3v3_name(self):
        self.assertAlmostEqual(_estimate_voltage("+3V3_MAIN"), 3.3)

    def test_5v_name(self):
        self.assertAlmostEqual(_estimate_voltage("+5V_RAIL"), 5.0)

    def test_12v_name(self):
        self.assertAlmostEqual(_estimate_voltage("+12V"), 12.0)

    def test_no_voltage(self):
        self.assertIsNone(_estimate_voltage("SCL"))
        self.assertIsNone(_estimate_voltage("GND"))
        self.assertIsNone(_estimate_voltage("SAFE_PWR_EN"))

    def test_vbus_raw_not_detected(self):
        # J2_VBUS_RAW has no +prefix and no NVN pattern → None
        self.assertIsNone(_estimate_voltage("J2_VBUS_RAW"))


class TestNetsFromSegmentsAndZones(unittest.TestCase):
    def test_segment_nets_included(self):
        seg = Segment(net_name="CLK", width_mm=0.1, layer="F.Cu",
                      start=(0, 0), end=(10, 0))
        board = _make_board(segments=[seg])
        nl = build_netlist(board, None, "uuid-x")
        self.assertIn("CLK", {n.name for n in nl.nets})

    def test_zone_nets_included(self):
        zone = Zone(net_name="GND", layer="B.Cu")
        board = _make_board(zones=[zone])
        nl = build_netlist(board, None, "uuid-x")
        self.assertIn("GND", {n.name for n in nl.nets})


if __name__ == "__main__":
    unittest.main()
