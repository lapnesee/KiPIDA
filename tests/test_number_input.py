"""A number typed with a decimal comma is read (review 2026-10-02)."""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


class ParseNumberTests(unittest.TestCase):
    def test_a_french_number_reads_as_typed(self):
        # 71 fields of ui/ refused "0,1", and the regulator dialog's
        # "Rendement (0,0-1,0)" invited it.
        from ui.number_input import parse_number

        self.assertEqual(parse_number("0,93"), 0.93)
        self.assertEqual(parse_number(" 4.726 "), 4.726)
        self.assertEqual(parse_number("102 048"), 102048.0)
        self.assertEqual(parse_number("1 530"), 1530.0)
        self.assertEqual(parse_number("2,5e-3"), 0.0025)
        for text in ("", "abc", "1,000,5"):
            with self.assertRaises(ValueError):
                parse_number(text)


if __name__ == "__main__":
    unittest.main()
