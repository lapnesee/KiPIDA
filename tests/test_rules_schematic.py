import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from ingest.board_reader import BoardBounds, Stackup
from ingest.netlist_builder import BoardNetlist, ComponentInfo, NetInfo, NetPin
from ingest.schematic_reader import ParsedSchematic, SymbolInstance
from rules.schematic_rules import (
    SchematicRuleContext,
    _check_undecoupled_power_pins,
    _check_unconnected_pins,
    _check_missing_power_rating,
    _check_cross_board_links,
    _check_pcb_schematic_consistency,
)

_STACKUP = Stackup(layers=[], total_thickness_mm=1.6, copper_layer_count=2)
_BOUNDS = BoardBounds(0, 0, 10, 10)


def _netlist(board_uuid, components, nets):
    return BoardNetlist(
        board_uuid=board_uuid, pcb_path=None,
        components=components, nets=nets,
        stackup=_STACKUP, bounds=_BOUNDS,
    )


class TestUndecoupledPowerPin(unittest.TestCase):

    def _ic(self, decoupled: bool):
        ic = ComponentInfo(
            reference="U1", value="TPS1234", lib_id="ic:TPS1234", datasheet="",
            extra_fields={}, position=(0, 0),
            nets_by_pad={"1": "+3V3", "2": "GND"},
            pin_types_by_pad={"1": "power_in", "2": "power_in"},
        )
        components = [ic]
        if decoupled:
            components.append(ComponentInfo(
                reference="C1", value="100nF", lib_id="Device:C", datasheet="",
                extra_fields={}, position=(1, 0),
                nets_by_pad={"1": "+3V3", "2": "GND"},
                pin_types_by_pad={"1": "passive", "2": "passive"},
            ))
        nets = [NetInfo("+3V3", "b1", True, False, 3.3), NetInfo("GND", "b1", True, False, None)]
        return SchematicRuleContext(netlist=_netlist("b1", components, nets))

    def test_missing_decoupling_flagged(self):
        findings = _check_undecoupled_power_pins(self._ic(decoupled=False))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule_id, "SCH-001")
        # A verdict drawn from recognising capacitors and grounds, not a fact.
        self.assertEqual(findings[0].confidence.value, "ESTIMATED")

    def test_present_decoupling_not_flagged(self):
        findings = _check_undecoupled_power_pins(self._ic(decoupled=True))
        self.assertEqual(findings, [])

    def test_capacitances_written_without_farad_decouple(self):
        # cm5_minima writes 17 of its 19 capacitors this way.
        for value in ("100n", "4u7", "10u", "100n / INA238"):
            context = self._ic(decoupled=True)
            context.netlist.components[1].value = value
            self.assertEqual(_check_undecoupled_power_pins(context), [], value)

    def test_an_inductor_valued_like_a_capacitor_does_not_decouple(self):
        context = self._ic(decoupled=True)
        context.netlist.components[1] = ComponentInfo(
            reference="L1", value="10u", lib_id="Inductor_SMD:L_0603", datasheet="", extra_fields={},
            position=(1, 0), nets_by_pad={"1": "+3V3", "2": "GND"}, pin_types_by_pad={"1": "passive", "2": "passive"},
        )
        self.assertEqual(len(_check_undecoupled_power_pins(context)), 1)

    def test_an_unconnected_power_input_is_not_undecoupled(self):
        context = self._ic(decoupled=False)
        context.netlist.components[0].nets_by_pad["1"] = "unconnected-(U1-Pad1)"
        self.assertEqual(_check_undecoupled_power_pins(context), [])


class TestUnconnectedPin(unittest.TestCase):

    def _context(self, documented: bool):
        component = ComponentInfo(
            reference="J1", value="Conn", lib_id="Connector:Conn", datasheet="",
            extra_fields={}, position=(0, 0),
            nets_by_pad={"1": "unconnected-(J1-Pad1)"},
            pin_types_by_pad={"1": "passive"} if documented else {},
        )
        return SchematicRuleContext(netlist=_netlist("b1", [component], []))

    def test_unconnected_pad_flagged_low_when_documented(self):
        findings = _check_unconnected_pins(self._context(documented=True))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity.value, "LOW")

    def test_a_pin_the_designer_marked_no_connect_is_not_reported(self):
        from ingest.board_reader import Footprint, Pad, ParsedBoard
        from ingest.netlist_builder import build_netlist

        board = ParsedBoard(None, _STACKUP, _BOUNDS, footprints=[Footprint(
            reference="U1", value="MCU", lib_id="Package:QFN", position=(0, 0), rotation=0, layer="F.Cu",
            sch_path="/u1", sch_sheet_file="",
            pads=[Pad("7", "unconnected-(U1-Pad7)", "passive", (0, 0)), Pad("8", "unconnected-(U1-Pad8)", "passive", (1, 0))],
        )], segments=[], vias=[], zones=[])
        # Unit 1 is on the board; the marker sits on a pin of unit 2.
        units = [
            SymbolInstance(lib_id="x", uuid="u1", reference="U1", value="MCU", footprint="", datasheet="",
                           extra_fields={}, sheet_path="/r", pins=[], footprint_path="/u1", unit=1),
            SymbolInstance(lib_id="x", uuid="u2", reference="U1", value="MCU", footprint="", datasheet="",
                           extra_fields={}, sheet_path="/r", pins=[], footprint_path="/u2", unit=2,
                           no_connect_pins={"7"}),
        ]
        schematic = ParsedSchematic(root_sch_path=None, symbol_defs={}, instances=units, global_nets=[])
        netlist = build_netlist(board, schematic, "b1")
        findings = _check_unconnected_pins(SchematicRuleContext(netlist=netlist, schematic=schematic))
        self.assertEqual([f.description for f in findings], ["U1 pad 8 is unconnected (unconnected-(U1-Pad8))."])

        # A pin its library types no_connect is left open by design.
        netlist.components[0].pin_types_by_pad["8"] = "no_connect"
        self.assertEqual(_check_unconnected_pins(SchematicRuleContext(netlist=netlist, schematic=schematic)), [])

    def test_no_finding_for_normal_net(self):
        component = ComponentInfo(
            reference="J1", value="Conn", lib_id="Connector:Conn", datasheet="",
            extra_fields={}, position=(0, 0),
            nets_by_pad={"1": "GND"}, pin_types_by_pad={"1": "passive"},
        )
        ctx = SchematicRuleContext(netlist=_netlist("b1", [component], []))
        self.assertEqual(_check_unconnected_pins(ctx), [])


class TestMissingPowerRating(unittest.TestCase):

    def _resistor(self, has_power_field: bool):
        return ComponentInfo(
            reference="R5", value="4.7k", lib_id="Device:R", datasheet="",
            extra_fields={"Power": "0.1W"} if has_power_field else {},
            position=(0, 0), nets_by_pad={"1": "+5V_RAIL"}, pin_types_by_pad={"1": "passive"},
        )

    def test_flags_resistor_without_power_field(self):
        nets = [NetInfo("+5V_RAIL", "b1", True, False, 5.0)]
        ctx = SchematicRuleContext(netlist=_netlist("b1", [self._resistor(False)], nets))
        findings = _check_missing_power_rating(ctx)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity.value, "INFO")

    def test_one_finding_names_every_resistor(self):
        nets = [NetInfo("+5V_RAIL", "b1", True, False, 5.0)]
        second = self._resistor(False)
        second.reference = "R6"
        ctx = SchematicRuleContext(netlist=_netlist("b1", [self._resistor(False), second], nets))
        findings = _check_missing_power_rating(ctx)
        self.assertEqual([f.components for f in findings], [["R5", "R6"]])

    def test_no_finding_when_power_field_present(self):
        nets = [NetInfo("+5V_RAIL", "b1", True, False, 5.0)]
        ctx = SchematicRuleContext(netlist=_netlist("b1", [self._resistor(True)], nets))
        self.assertEqual(_check_missing_power_rating(ctx), [])

    def test_a_dielectric_code_does_not_make_a_capacitor_a_resistor(self):
        # P02 Rev E: "5R" in "X5R" read as 5 ohms put 22 capacitors, C56 among
        # them, in the list of resistors lacking a power rating.
        nets = [NetInfo("+5V_RAIL", "b1", True, False, 5.0)]
        capacitor = ComponentInfo(
            reference="C56", value="47u 16V X5R", lib_id="Device:C", datasheet="",
            extra_fields={}, position=(0, 0), nets_by_pad={"1": "+5V_RAIL"},
            pin_types_by_pad={"1": "passive"},
        )
        ctx = SchematicRuleContext(netlist=_netlist("b1", [capacitor, self._resistor(False)], nets))
        self.assertEqual([f.components for f in _check_missing_power_rating(ctx)], [["R5"]])


class TestCrossBoardLinks(unittest.TestCase):

    def _zeo(self):
        from pathlib import Path
        from ingest.project_resolver import BoardProject
        from ingest.zeo_container import CrossBoardLink, LinkEndpoint, ZeoContainer

        def board(stem, uuid):
            return BoardProject(Path(f"{stem}.kicad_pro"), Path(f"{stem}.kicad_pcb"), Path(f"{stem}.kicad_sch"),
                                [], True, uuid)
        return ZeoContainer(
            path=Path("system.kicad_pro"),
            boards={"A": board("power", "A"), "B": board("main", "B")},
            links=[
                CrossBoardLink("+5V", "l1", [LinkEndpoint("A", "J1", "1", "+5V", "ok", "+5V"),
                                             LinkEndpoint("B", "J2", "1", "+5V", "ok", "+5V")]),
                CrossBoardLink("SDA", "l2", [LinkEndpoint("A", "J1", "2", "SDA", "stale", "SDA_OLD",
                                                          "J1 pad 2 is on SDA_OLD, not SDA"),
                                             LinkEndpoint("B", "J2", "2", "SDA", "ok", "SDA")]),
            ],
            diff_pairs=[],
        )

    def test_same_named_nets_on_two_boards_are_no_finding(self):
        # Two boards each with a GND nothing joins: a name is not a link.
        nl_a = _netlist("A", [], [NetInfo("GND", "A", True, False, None)])
        nl_b = _netlist("B", [], [NetInfo("GND", "B", True, False, None)])
        ctx = SchematicRuleContext(netlist=nl_a, all_board_netlists=[nl_a, nl_b])
        self.assertEqual(_check_cross_board_links(ctx), [])

    def test_a_stale_endpoint_is_reported_on_the_boards_its_link_reaches(self):
        ctx = SchematicRuleContext(netlist=_netlist("A", [], []), zeo=self._zeo())
        findings = _check_cross_board_links(ctx)
        self.assertEqual([(f.rule_id, f.severity.value, f.components) for f in findings],
                         [("SCH-004", "HIGH", ["J1"])])
        self.assertIn("on board power, J1 pad 2 is on SDA_OLD, not SDA", findings[0].description)


class TestPcbSchematicConsistency(unittest.TestCase):

    def _schematic(self, refs):
        instances = [
            SymbolInstance(lib_id="x", uuid=f"u{i}", reference=ref, value="", footprint="",
                            datasheet="", extra_fields={}, sheet_path="", pins=[])
            for i, ref in enumerate(refs)
        ]
        return ParsedSchematic(root_sch_path=None, symbol_defs={}, instances=instances, global_nets=[])

    def test_missing_symbol_flagged(self):
        component = ComponentInfo("U9", "", "", "", {}, (0, 0), {}, {}, schematic_link="symbol_missing")
        ctx = SchematicRuleContext(
            netlist=_netlist("b1", [component], []),
            schematic=self._schematic(["U1"]),
        )
        findings = _check_pcb_schematic_consistency(ctx)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].components, ["U9"])

    def test_a_board_only_footprint_is_one_note_not_a_mismatch(self):
        logos = [ComponentInfo(ref, "", "", "", {}, (0, 0), {}, {}, schematic_link="pcb_only")
                 for ref in ("G1", "MH1")]
        ctx = SchematicRuleContext(netlist=_netlist("b1", logos, []), schematic=self._schematic(["U1"]))
        self.assertEqual(_check_pcb_schematic_consistency(ctx), [])

        from rules.schematic_rules import _report_board_only_footprints
        self.assertEqual([(f.rule_id, f.components) for f in _report_board_only_footprints(ctx)],
                         [("SCH-006", ["G1", "MH1"])])

    def test_no_schematic_no_findings(self):
        component = ComponentInfo("U9", "", "", "", {}, (0, 0), {}, {})
        ctx = SchematicRuleContext(netlist=_netlist("b1", [component], []), schematic=None)
        self.assertEqual(_check_pcb_schematic_consistency(ctx), [])


if __name__ == "__main__":
    unittest.main()
