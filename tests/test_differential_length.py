import unittest

from differential_length import assess_length_symmetry, protocol_skew_limit_ps


def track(length_mm):
    return {"length_mm": length_mm}


class DifferentialLengthTests(unittest.TestCase):
    def test_specific_protocol_limit_wins_over_usb_substring(self):
        self.assertEqual(protocol_skew_limit_ps("USB_SS"), 5.0)
        self.assertEqual(protocol_skew_limit_ps("SUPER_USB_SS_LANE"), 5.0)
        self.assertEqual(protocol_skew_limit_ps("USB_HS"), 25.0)

    def test_pass_reports_both_lengths_and_relative_mismatch(self):
        result = assess_length_symmetry([track(10.0)], [track(10.2)], "USB", 4.0)
        self.assertEqual(result.status, "PASS")
        self.assertAlmostEqual(result.positive_length_mm, 10.0)
        self.assertAlmostEqual(result.negative_length_mm, 10.2)
        self.assertAlmostEqual(result.mismatch_mm, 0.2)
        self.assertAlmostEqual(result.mismatch_pct, 100.0 * 0.2 / 10.1)
        self.assertEqual(result.shorter_polarity, "POSITIVE")

    def test_marginal_uses_half_of_protocol_budget(self):
        result = assess_length_symmetry([track(10.0)], [track(12.0)], "USB", 4.0)
        self.assertEqual(result.status, "MARGINAL")
        self.assertGreater(result.estimated_skew_ps, 12.5)
        self.assertLess(result.estimated_skew_ps, 25.0)

    def test_fail_when_estimated_skew_exceeds_protocol_limit(self):
        result = assess_length_symmetry([track(10.0)], [track(14.0)], "USB", 4.0)
        self.assertEqual(result.status, "FAIL")
        self.assertGreater(result.estimated_skew_ps, result.skew_limit_ps)
        self.assertLess(result.margin_ps, 0.0)

    def test_no_data_when_one_conductor_is_unrouted(self):
        result = assess_length_symmetry([track(10.0)], [], "USB", 4.0)
        self.assertEqual(result.status, "NO_DATA")


if __name__ == "__main__":
    unittest.main()
