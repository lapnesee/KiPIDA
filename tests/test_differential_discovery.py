import unittest

from differential_discovery import DifferentialPairDiscoverer
from models import DifferentialPairCandidate


class Obj:
    def __init__(self, **values):
        self.__dict__.update(values)


def pad(net_name, function=""):
    return Obj(net=Obj(name=net_name), pin_function=function)


class KiCadConventionTests(unittest.TestCase):
    """Pairs as KiCad's router pairs them, and as the project's netclasses declare them."""

    def test_the_complement_follows_kicads_router(self):
        from differential_discovery import kicad_pair_complement

        self.assertEqual(kicad_pair_complement("USB_D+"), ("USB_D-", "P"))
        self.assertEqual(kicad_pair_complement("LANE0_N"), ("LANE0_P", "N"))
        self.assertEqual(kicad_pair_complement("TX_P_1"), ("TX_N_1", "P"))
        self.assertEqual(kicad_pair_complement("CLKP"), ("CLKN", "P"))
        self.assertIsNone(kicad_pair_complement("+5V"))
        self.assertIsNone(kicad_pair_complement("USB_DM"))

    def test_a_kicad_suffix_pair_is_likely_and_a_netclass_states_its_target(self):
        import json
        import os
        import tempfile
        from differential_discovery import netclass_membership

        board = Obj(nets=[Obj(name=n) for n in ("/USB/D+", "/USB/D-", "LANE0_P", "LANE0_N")],
                    tracks=[], zones=[], vias=[], footprints=[])
        with tempfile.TemporaryDirectory() as tmp:
            project = os.path.join(tmp, "board.kicad_pro")
            with open(project, "w", encoding="utf-8") as handle:
                json.dump({"net_settings": {
                    "netclass_assignments": {"LANE0_P": ["Default"]},
                    "netclass_patterns": [{"netclass": "90ohm", "pattern": "/*usb*"}],
                }}, handle)
            pairs = DifferentialPairDiscoverer(
                board, netclasses=lambda nets: netclass_membership(project, nets),
            ).discover()
        by_positive = {pair.positive_net: pair for pair in pairs}
        usb = by_positive["/USB/D+"]
        self.assertEqual((usb.negative_net, usb.confidence, usb.target_impedance_ohm), ("/USB/D-", "LIKELY", 90.0))
        self.assertIn("netclass:90ohm", usb.evidence)
        lane = by_positive["LANE0_P"]
        # A suffix alone, on no identified interface, only suggests a pair
        # (SI-7): it is kept but not analysed until confirmed.
        self.assertEqual((lane.confidence, lane.evidence), ("SUSPECTED", ["kicad_pair_suffix"]))
        self.assertFalse(lane.enabled)
        # Evidence labels are read by the interface classifier; one containing "D-" made this USB.
        self.assertEqual(lane.interface, "GENERIC")

    def test_a_power_netclass_does_not_declare_its_two_conductors_a_pair(self):
        # A supply's two conductors share a power netclass and KiCad's suffix
        # rule pairs VIN_P with VIN_N. The class kept the Default class's
        # pair geometry, so it groups nets by current, not as a pair; a class
        # that sets its own pair width and gap still declares one.
        import json
        import os
        import tempfile
        from differential_discovery import netclass_membership, pair_netclasses

        board = Obj(nets=[Obj(name=n) for n in ("/VIN_P", "/VIN_N", "/LINK_P", "/LINK_N")],
                    tracks=[], zones=[], vias=[], footprints=[])
        with tempfile.TemporaryDirectory() as tmp:
            project = os.path.join(tmp, "board.kicad_pro")
            with open(project, "w", encoding="utf-8") as handle:
                json.dump({"net_settings": {
                    "classes": [
                        {"name": "Default", "track_width": 0.2, "diff_pair_width": 0.2, "diff_pair_gap": 0.25},
                        {"name": "SUPPLY", "track_width": 1.5, "diff_pair_width": 0.2, "diff_pair_gap": 0.25},
                        {"name": "LINK", "track_width": 0.12, "diff_pair_width": 0.12, "diff_pair_gap": 0.15},
                    ],
                    "netclass_patterns": [
                        {"netclass": "SUPPLY", "pattern": "*VIN_*"},
                        {"netclass": "LINK", "pattern": "*LINK_*"},
                    ],
                }}, handle)
            pairs = DifferentialPairDiscoverer(
                board, netclasses=lambda nets: netclass_membership(project, nets),
                pair_classes=lambda: pair_netclasses(project),
            ).discover()
        by_positive = {pair.positive_net: pair for pair in pairs}
        supply = by_positive["/VIN_P"]
        self.assertEqual((supply.confidence, supply.enabled), ("SUSPECTED", False))
        self.assertIn("netclass-group:SUPPLY", supply.evidence)
        link = by_positive["/LINK_P"]
        self.assertEqual((link.confidence, link.enabled), ("LIKELY", True))

    def test_power_and_led_pins_are_not_analysed_as_differential_pairs(self):
        # audit-2026-09-23.md SI-7: BAT+/BAT- matched KiCad's suffix rule and
        # took a GENERIC 100-ohm target, and LED_DP/LED_DM became USB because
        # "DP" occurs in the name. P02 Rev E's five USB pairs must stay.
        board = Obj(nets=[Obj(name=n) for n in (
            "BAT+", "BAT-", "LED_DP", "LED_DM", "/USB_HOST_D+", "/USB_HOST_D-",
        )], tracks=[], zones=[], vias=[], footprints=[])
        pairs = {pair.positive_net: pair for pair in DifferentialPairDiscoverer(board).discover()}
        self.assertFalse(pairs["BAT+"].enabled)
        self.assertEqual(pairs["LED_DP"].interface, "GENERIC")
        self.assertFalse(pairs["LED_DP"].enabled)
        usb = pairs["/USB_HOST_D+"]
        self.assertEqual((usb.interface, usb.enabled), ("USB", True))


class DifferentialDiscoveryTests(unittest.TestCase):
    def test_name_based_pairs_are_separate_candidates(self):
        board = Obj(
            nets=[Obj(name="USB_DP"), Obj(name="USB_DM"), Obj(name="+5V")],
            tracks=[], zones=[], vias=[], footprints=[],
        )
        pairs = DifferentialPairDiscoverer(board).discover()
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].positive_net, "USB_DP")
        self.assertEqual(pairs[0].negative_net, "USB_DM")
        self.assertEqual(pairs[0].interface, "USB")
        self.assertEqual(pairs[0].target_impedance_ohm, 90.0)
        self.assertEqual(pairs[0].confidence, "SUSPECTED")

    def test_pin_functions_trace_through_series_resistors(self):
        controller = Obj(reference="U1", pads=[
            pad("N_USB_P_LOCAL", "USB_DP"), pad("N_USB_N_LOCAL", "USB_DM"),
        ])
        resistor_p = Obj(reference="R1", pads=[pad("N_USB_P_LOCAL"), pad("USB_DP")])
        resistor_n = Obj(reference="R2", pads=[pad("N_USB_N_LOCAL"), pad("USB_DM")])
        board = Obj(
            nets=[], tracks=[], zones=[], vias=[],
            footprints=[controller, resistor_p, resistor_n],
        )
        pairs = DifferentialPairDiscoverer(board).discover()
        pair = next(item for item in pairs if item.positive_net == "USB_DP")
        self.assertEqual(pair.negative_net, "USB_DM")
        self.assertEqual(pair.confidence, "LIKELY")
        self.assertTrue(any(item.startswith("pin-functions:U1") for item in pair.evidence))
        self.assertTrue(any(item.startswith("series-path:") for item in pair.evidence))

    def test_user_decisions_survive_rescan_and_ignored_pairs_stay_hidden(self):
        board = Obj(
            nets=[Obj(name="LANE_P"), Obj(name="LANE_N"), Obj(name="CLK_P"), Obj(name="CLK_N")],
            tracks=[], zones=[], vias=[], footprints=[],
        )
        saved = DifferentialPairCandidate(
            name="My Lane", positive_net="LANE_P", negative_net="LANE_N",
            interface="PCIE", target_impedance_ohm=87.0,
            confidence="CONFIRMED", source="auto",
        )
        ignored = "|".join(sorted(("CLK_P", "CLK_N")))
        pairs = DifferentialPairDiscoverer(board).discover([saved], [ignored])
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].name, "My Lane")
        self.assertEqual(pairs[0].target_impedance_ohm, 87.0)
        self.assertEqual(pairs[0].confidence, "CONFIRMED")


if __name__ == "__main__":
    unittest.main()
