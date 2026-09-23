import os
import sys
import unittest
from types import SimpleNamespace

plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

from ac_model import ACModelBuilder, format_capacitance, parse_capacitance


def pad(number, net_name):
    return SimpleNamespace(number=number, net=SimpleNamespace(name=net_name))


class TestCapacitorDiscovery(unittest.TestCase):
    def test_parses_common_kicad_values(self):
        self.assertAlmostEqual(parse_capacitance("100n"), 100e-9)
        self.assertAlmostEqual(parse_capacitance("4u7"), 4.7e-6)
        self.assertAlmostEqual(parse_capacitance("10uF 16V"), 10e-6)
        self.assertAlmostEqual(parse_capacitance("1.5pF"), 1.5e-12)
        self.assertIsNone(parse_capacitance("DNP"))
        self.assertIsNone(parse_capacitance("TPS7A0233PDBVR"))

    def test_parses_bom_values_that_go_on_after_the_rating(self):
        # Values from the P02 Rev E board, which the AC tab used to take for
        # 100 nF candidates; a unitless leading number still reads as nothing.
        self.assertAlmostEqual(parse_capacitance("47u 16V X5R"), 47e-6)
        self.assertAlmostEqual(parse_capacitance("4.7u 50V X7R; Ceff>=1u"), 4.7e-6)
        self.assertAlmostEqual(parse_capacitance("68u / 35V 20% hybride"), 68e-6)
        self.assertIsNone(parse_capacitance("1206 X7R"))

    def test_formats_engineering_units(self):
        self.assertEqual(format_capacitance(4.7e-6), "4.7uF")
        self.assertEqual(format_capacitance(100e-9), "100nF")

    def test_discovers_populated_and_dnp_rail_capacitors(self):
        populated = SimpleNamespace(
            reference="C1", value="100n", footprint_name="Capacitor_SMD:C_0402",
            pads=[pad("1", "3V3"), pad("2", "GND")], dnp=False,
        )
        candidate = SimpleNamespace(
            reference="C2", value="DNP", footprint_name="Capacitor_SMD:C_0805",
            pads=[pad("1", "3V3"), pad("2", "GND")], dnp=True,
        )
        unrelated = SimpleNamespace(
            reference="C3", value="1u", footprint_name="Capacitor_SMD:C_0603",
            pads=[pad("1", "5V"), pad("2", "GND")], dnp=False,
        )
        integrated_circuit = SimpleNamespace(
            reference="U7", value="320000mF", footprint_name="mouser:RWB0012A",
            pads=[pad("1", "3V3"), pad("2", "GND")], dnp=False,
        )
        builder = ACModelBuilder(SimpleNamespace(
            footprints=[populated, candidate, unrelated, integrated_circuit],
        ))

        capacitors = builder.discover_capacitors("3V3", "GND")

        self.assertEqual([cap.ref_des for cap in capacitors], ["C1", "C2"])
        self.assertTrue(capacitors[0].enabled)
        self.assertFalse(capacitors[0].candidate)
        self.assertFalse(capacitors[1].enabled)
        self.assertTrue(capacitors[1].candidate)
        self.assertLess(capacitors[0].esl_h, capacitors[1].esl_h)
        self.assertEqual(builder.discover_ground_nets(), ["GND"])

    def test_a_class_ii_capacitor_is_derated_at_the_rail_voltage(self):
        # C56 of the P02 Rev E board, 47u 16V X5R on the 5.08 V +5V_RAIL,
        # entered the sweep at its nominal 47 uF: an optimistic Z(f).
        def part(reference, value):
            return SimpleNamespace(reference=reference, value=value, footprint_name="Capacitor_SMD:C_1210",
                                   pads=[pad("1", "+5V_RAIL"), pad("2", "GND")], dnp=False)

        builder = ACModelBuilder(SimpleNamespace(footprints=[
            part("C56", "47u 16V X5R"), part("C60", "330p 50V C0G"), part("C81", "68u / 35V 20% hybride"),
        ]))
        capacitors = {cap.ref_des: cap for cap in builder.discover_capacitors("+5V_RAIL", "GND", rail_voltage_v=5.08)}

        retained = 1.0 / (1.0 + ((5.08 / 16.0) / 0.6) ** 1.5)
        self.assertAlmostEqual(capacitors["C56"].capacitance_f, 47e-6 * retained)
        self.assertIn("DC bias X5R 16 V at 5.08 V", capacitors["C56"].model_source)
        self.assertIn("estimate", capacitors["C56"].model_source)
        # Class I and non-ceramic parts keep their nominal value.
        self.assertAlmostEqual(capacitors["C60"].capacitance_f, 330e-12)
        self.assertAlmostEqual(capacitors["C81"].capacitance_f, 68e-6)

    def test_esr_and_derating_come_from_case_size_not_a_fixed_constant(self):
        # AC-7: a fixed 10 mOhm ESR and a single generic derating curve
        # regardless of case size were both optimistic for small, high-CV
        # parts. A 22uF/35V X5R in 0603 must derate MORE (lower retention,
        # AC-7's "25-40% typical in small high-CV MLCC") and carry a HIGHER
        # typical ESR than the same part in 1210.
        def part(reference, footprint):
            return SimpleNamespace(reference=reference, value="22u 35V X5R", footprint_name=footprint,
                                   pads=[pad("1", "/VBUS_SYS"), pad("2", "GND")], dnp=False)

        builder = ACModelBuilder(SimpleNamespace(footprints=[
            part("C1", "Capacitor_SMD:C_0603"), part("C2", "Capacitor_SMD:C_1210"),
        ]))
        capacitors = {
            cap.ref_des: cap for cap in
            builder.discover_capacitors("/VBUS_SYS", "GND", rail_voltage_v=20.0)
        }
        self.assertLess(capacitors["C1"].capacitance_f, capacitors["C2"].capacitance_f)
        self.assertGreater(capacitors["C1"].esr_ohm, capacitors["C2"].esr_ohm)


if __name__ == "__main__":
    unittest.main()
