"""Finding what feeds a rail, and placing its pads on the mesh.

A real power board declares its rails with zero UnifiedSource entries -- the
reference board has +5V_RAIL fed by regulator Q3 -- so asking only for a
declared source refuses almost every rail. These tests pin the resolution
order and the pad-to-node mapping the solver needs.
"""

import os
import sys
import unittest
from dataclasses import dataclass, field

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from advisor.rail_sources import (
    map_pads_to_nodes, pad_pin_type, pad_positions, resolve_rail_source,
    solver_loads, unreachable_nodes,
)


# --- minimal stand-ins for ingest.board_reader / models -------------------

@dataclass
class _Pad:
    number: str
    net_name: str
    pintype: str
    position: tuple


@dataclass
class _Footprint:
    reference: str
    pads: list = field(default_factory=list)


@dataclass
class _Board:
    footprints: list = field(default_factory=list)


@dataclass
class _Ref:
    ref_des: str


@dataclass
class _Source:
    component_ref: _Ref
    pad_names: list = field(default_factory=list)


@dataclass
class _Load:
    component_ref: _Ref
    total_current: float = 0.0
    pad_names: list = field(default_factory=list)


@dataclass
class _Regulator:
    output_rail_name: str
    output_ref_des: str
    output_pad_names: list = field(default_factory=list)


@dataclass
class _Rail:
    net_name: str
    nominal_voltage: float = 5.0
    sources: list = field(default_factory=list)
    loads: list = field(default_factory=list)
    child_regulators: list = field(default_factory=list)


class _Mesh:
    """Just the node_coords surface find_node_at reads."""

    def __init__(self, coords):
        self.node_coords = dict(coords)
        self.nodes = list(coords)


def _board_with(*footprints):
    return _Board(footprints=list(footprints))


# --- resolution order -----------------------------------------------------

class ResolutionOrderTests(unittest.TestCase):
    def test_a_declared_source_wins_over_a_regulator(self):
        rail = _Rail(
            "+5V", sources=[_Source(_Ref("U1"), ["3"])],
        )
        upstream = _Rail("VIN", child_regulators=[_Regulator("+5V", "Q3", ["7"])])
        ref, pads, rule = resolve_rail_source(_board_with(), rail, [rail, upstream])
        self.assertEqual((ref, pads, rule), ("U1", ["3"], "declared"))

    def test_a_regulator_wins_over_a_power_out_pad(self):
        rail = _Rail("+5V")
        upstream = _Rail("VIN", child_regulators=[_Regulator("+5V", "Q3", ["7"])])
        board = _board_with(_Footprint("U9", [_Pad("1", "+5V", "power_out", (0.0, 0.0))]))
        ref, pads, rule = resolve_rail_source(board, rail, [rail, upstream])
        self.assertEqual((ref, pads, rule), ("Q3", ["7"], "regulator-output"))

    def test_the_regulator_is_found_on_another_rails_list(self):
        # child_regulators lists what a rail *feeds*, so the regulator
        # producing +5V sits on its upstream rail. Scanning only the rail
        # itself finds nothing -- the trap this ordering exists to avoid.
        rail = _Rail("+5V")
        upstream = _Rail("VIN", child_regulators=[_Regulator("+5V", "Q3", ["7", "8"])])
        ref, pads, rule = resolve_rail_source(_board_with(), rail, [rail, upstream])
        self.assertEqual(ref, "Q3")
        self.assertEqual(rule, "regulator-output")
        self.assertIsNone(
            resolve_rail_source(_board_with(), rail, [rail])[0],
            "scanning only the rail itself must not find its own producer",
        )

    def test_a_power_out_pad_is_used_when_nothing_else_exists(self):
        rail = _Rail("+5V")
        board = _board_with(_Footprint("U9", [_Pad("1", "+5V", "power_out", (0.0, 0.0))]))
        ref, pads, rule = resolve_rail_source(board, rail, [rail])
        self.assertEqual((ref, pads, rule), ("U9", ["1"], "pin-type:power_out"))

    def test_nothing_found_returns_a_usable_reason(self):
        rail = _Rail("+5V")
        upstream = _Rail("VIN", child_regulators=[_Regulator("+3V3", "L2", ["2"])])
        board = _board_with(_Footprint("R1", [_Pad("1", "+5V", "passive", (0.0, 0.0))]))
        ref, pads, reason = resolve_rail_source(board, rail, [rail, upstream])
        self.assertIsNone(ref)
        self.assertEqual(pads, [])
        self.assertIn("+5V", reason)
        self.assertIn("power_out", reason)
        self.assertIn("1 known regulator", reason)


class PinTypeTests(unittest.TestCase):
    def test_a_no_connect_suffix_is_stripped(self):
        # KiCad writes compound types; the leading segment is the role.
        self.assertEqual(
            pad_pin_type(_Pad("1", "N", "power_out+no_connect", (0, 0))), "power_out",
        )

    def test_a_power_out_no_connect_pad_still_resolves(self):
        rail = _Rail("+5V")
        board = _board_with(
            _Footprint("U9", [_Pad("1", "+5V", "power_out+no_connect", (0.0, 0.0))]),
        )
        self.assertEqual(resolve_rail_source(board, rail, [rail])[0], "U9")

    def test_a_power_input_pad_is_never_a_source(self):
        rail = _Rail("+5V")
        board = _board_with(_Footprint("U7", [_Pad("1", "+5V", "power_in", (0.0, 0.0))]))
        self.assertIsNone(resolve_rail_source(board, rail, [rail])[0])


# --- pads to nodes --------------------------------------------------------

class PadToNodeTests(unittest.TestCase):
    def setUp(self):
        self.board = _board_with(_Footprint("Q3", [
            _Pad("7", "+5V", "passive", (1.0, 1.0)),
            _Pad("8", "+5V", "passive", (2.0, 1.0)),
            _Pad("1", "GND", "passive", (3.0, 1.0)),
        ]))
        self.mesh = _Mesh({10: (1.0, 1.0, 0), 11: (2.0, 1.0, 0), 12: (9.0, 9.0, 0)})

    def test_located_pads_give_node_ids_present_in_the_mesh(self):
        nodes, unlocated = map_pads_to_nodes(
            self.mesh, self.board, "Q3", ["7", "8"], net_name="+5V",
        )
        self.assertEqual(sorted(nodes), [10, 11])
        self.assertEqual(unlocated, [])
        for node_id in nodes:
            self.assertIn(node_id, self.mesh.node_coords)

    def test_a_pad_far_from_any_node_is_reported_not_snapped(self):
        # Attaching it to whatever is nearest would move current somewhere it
        # does not flow; a refused anchor is better than a quietly wrong one.
        board = _board_with(_Footprint("Q3", [
            _Pad("7", "+5V", "passive", (50.0, 50.0)),
        ]))
        nodes, unlocated = map_pads_to_nodes(
            self.mesh, board, "Q3", ["7"], net_name="+5V",
        )
        self.assertEqual(nodes, [])
        self.assertEqual(unlocated, ["7"])

    def test_pads_on_another_net_are_not_used(self):
        found = pad_positions(self.board, "Q3", [], net_name="+5V")
        self.assertEqual([number for number, _ in found], ["7", "8"])


class SolverLoadTests(unittest.TestCase):
    def test_current_is_split_evenly_across_located_pads(self):
        board = _board_with(_Footprint("J6", [
            _Pad("5", "+5V", "passive", (1.0, 1.0)),
            _Pad("6", "+5V", "passive", (2.0, 1.0)),
        ]))
        mesh = _Mesh({10: (1.0, 1.0, 0), 11: (2.0, 1.0, 0)})
        rail = _Rail("+5V", loads=[_Load(_Ref("J6"), 3.8, ["5", "6"])])
        loads = solver_loads(mesh, board, rail)
        self.assertEqual(len(loads), 2)
        self.assertAlmostEqual(sum(item["current"] for item in loads), 3.8)
        self.assertTrue(all(item["node_id"] in mesh.node_coords for item in loads))

    def test_an_unplaceable_load_is_announced(self):
        board = _board_with(_Footprint("J6", [
            _Pad("5", "+5V", "passive", (50.0, 50.0)),
        ]))
        mesh = _Mesh({10: (1.0, 1.0, 0)})
        rail = _Rail("+5V", loads=[_Load(_Ref("J6"), 3.8, ["5"])])
        messages = []
        loads = solver_loads(mesh, board, rail, log_callback=messages.append)
        self.assertEqual(loads, [])
        self.assertTrue(any("not modelled" in message for message in messages))


@dataclass
class _Branch:
    node_a: int
    node_b: int


class _BranchMesh:
    def __init__(self, branches):
        self.branches = [_Branch(a, b) for a, b in branches]


class ReachabilityTests(unittest.TestCase):
    def test_a_load_on_its_own_island_is_reported(self):
        # The real board meshes +5V_RAIL into 63,897 components: its loads sit
        # on track-segment islands the plane never joins. Left unchecked the
        # solver drops them, load_drop_v returns 0.0 V, and the rail is
        # declared within target -- a pass that never happened.
        mesh = _BranchMesh([(1, 2), (2, 3), (90, 91)])
        self.assertEqual(unreachable_nodes(mesh, [1], [3, 90]), [90])

    def test_a_connected_load_is_not_reported(self):
        mesh = _BranchMesh([(1, 2), (2, 3)])
        self.assertEqual(unreachable_nodes(mesh, [1], [3]), [])


if __name__ == "__main__":
    unittest.main()
