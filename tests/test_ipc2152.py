import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from rules.ipc2152 import external_layer_temp_rise_c, required_width_mm_for_current


class TestIpc2152CurveFit(unittest.TestCase):

    def test_higher_current_raises_temperature(self):
        low = external_layer_temp_rise_c(current_a=0.5, width_mm=1.0, thickness_mm=0.035)
        high = external_layer_temp_rise_c(current_a=2.0, width_mm=1.0, thickness_mm=0.035)
        self.assertGreater(high, low)

    def test_inverse_round_trip(self):
        # required_width_mm_for_current must be the inverse of external_layer_temp_rise_c
        current_a, thickness_mm, target_rise_c = 1.5, 0.035, 20.0
        width = required_width_mm_for_current(current_a, thickness_mm, target_rise_c)
        rise = external_layer_temp_rise_c(current_a, width, thickness_mm)
        self.assertAlmostEqual(rise, target_rise_c, places=6)

    def test_non_positive_inputs_raise(self):
        with self.assertRaises(ValueError):
            external_layer_temp_rise_c(current_a=0, width_mm=1.0, thickness_mm=0.035)
        with self.assertRaises(ValueError):
            required_width_mm_for_current(current_a=1.0, thickness_mm=0.035, max_temp_rise_c=0)


if __name__ == "__main__":
    unittest.main()
