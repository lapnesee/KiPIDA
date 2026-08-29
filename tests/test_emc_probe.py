import unittest

from emc_probe import EMCProbeReading, RenderedPointProbe


class EMCProbeTests(unittest.TestCase):
    def test_nearest_point_and_distance_limit(self):
        reading = EMCProbeReading("Clock edge", "HIGH", "Fast edge", "Add damping")
        probe = RenderedPointProbe([(0.25, 0.5, reading)], maximum_distance_px=12)
        self.assertIs(probe.sample(51, 49, 200, 100), reading)
        self.assertIsNone(probe.sample(90, 49, 200, 100))

    def test_label_contains_traceable_actionable_fields(self):
        reading = EMCProbeReading(
            title="Return path discontinuity", rule_id="RET-001", severity="HIGH",
            confidence="HIGH", description="Signal crosses a reference-plane void.",
            recommendation="Reroute above continuous reference copper.",
            nets=("CLK",), components=("U1",), evidence="BOARD_GEOMETRY: crossing at via",
        )
        label = reading.label()
        for expected in ("RET-001", "HIGH", "CLK", "U1", "Recommendation"):
            self.assertIn(expected, label)

        restored = RenderedPointProbe.from_dict(
            RenderedPointProbe([(0.2, 0.3, reading)]).to_dict()
        )
        self.assertEqual(restored.points[0][2], reading)


if __name__ == "__main__":
    unittest.main()
