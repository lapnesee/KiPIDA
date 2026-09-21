"""Rails come from what the project declares; a net name is the last resort, and says so."""

import os
import sys
import unittest
from types import SimpleNamespace as NS

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from discovery import NetDiscoverer, rail_label


def _board(net_names):
    net = lambda name: NS(name=name)  # noqa: E731
    return NS(
        footprints=[NS(pads=[NS(net=net(name)) for name in net_names])],
        tracks=[NS(net=net(name)) for name in net_names],
        zones=[],
    )


class RailDiscoveryTests(unittest.TestCase):
    NETS = ["GND", "+3V3", "VBAT", "VCC_IO", "SDA"]

    def test_declared_rails_come_first_and_a_name_guess_comes_last(self):
        declared = {"+3V3": "power_symbol", "VBAT": "power_pin"}
        rails = NetDiscoverer(_board(self.NETS), log_callback=lambda _m: None,
                              declared_rails=lambda: declared).discover_power_nets()
        # VBAT has no power-looking name; VCC_IO has one and no declaration;
        # GND is a ground, which no source feeds.
        self.assertEqual([(r.net_name, r.discovery) for r in rails],
                         [("+3V3", "power_symbol"), ("VBAT", "power_pin"), ("VCC_IO", "name")])

    def test_without_declarations_a_name_guess_says_it_is_one(self):
        rails = NetDiscoverer(_board(self.NETS), log_callback=lambda _m: None).discover_power_nets()
        self.assertEqual({r.net_name: r.discovery for r in rails}, {"+3V3": "name", "VCC_IO": "name"})
        vcc_io = next(rail for rail in rails if rail.net_name == "VCC_IO")
        self.assertEqual(rail_label(vcc_io), "VCC_IO [guessed from its name]")

    def test_grounds_and_converter_or_control_nodes_are_not_guessed_rails(self):
        # Undeclared nets of the P02 Rev E power board. Its rails there were
        # buried under grounds, switch/feedback/boot nodes and enables, while
        # the USB-C connectors' VBUS, on passive pins, was missing.
        nets = ["GND", "/SYS_5V_AGND", "/MAIN_3V3_SW", "/MAIN_3V3_FB", "/MAIN_3V3_BOOT",
                "/PWR_VALID2", "/J1_SAFE_PWR_EN", "/J1_VBUS_SENSE", "/J1_VBUS_RAW", "/+3V3_MAIN"]
        rails = NetDiscoverer(_board(nets), log_callback=lambda _m: None).discover_power_nets()
        self.assertEqual({rail.net_name for rail in rails}, {"/J1_VBUS_RAW", "/+3V3_MAIN"})

    def test_the_dc_result_names_the_rails_it_took_from_the_scan(self):
        from analysis_adapters import adapt_dc_run
        from models import PowerRail

        rails = [PowerRail("+3V3", 3.3, discovery="power_symbol"), PowerRail("VCC", 5.0, discovery="name"),
                 PowerRail("+5V_RAIL", 5.0)]
        result = adapt_dc_run({}, 5.0, rails=rails)
        evidence = [item.detail for item in result.provenance if item.source == "RAIL_DISCOVERY"]
        self.assertEqual(len(evidence), 1)
        self.assertIn("+3V3 (declared by a power symbol); VCC (guessed from its name)", evidence[0])
        self.assertNotIn("+5V_RAIL", evidence[0])
        self.assertTrue(any("guessed from their names: VCC." in text for text in result.limitations))


if __name__ == "__main__":
    unittest.main()
