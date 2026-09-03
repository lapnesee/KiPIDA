"""Tests for analytical thermal-via sizing (advisor/thermal_advisor.py).

Scope: the sizing law against an explicitly recomputed value, its scaling
behaviour, and the contract that this advisor never claims to be verified.
"""

import math
import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from advisor.thermal_advisor import (  # noqa: E402
    K_COPPER_W_MK,
    build_thermal_via_remediation,
    via_thermal_resistance_c_per_w,
    vias_required_for_delta_t,
)


# A 1.6 mm board, 0.3 mm drill, 25 um plating -- ordinary thermal via.
GEOMETRY = dict(height_mm=1.6, drill_mm=0.3, plating_mm=0.025)


class ViaSizingTests(unittest.TestCase):
    def test_count_matches_the_formula_recomputed_from_geometry(self):
        """N = ceil(P * R_single / dT), checked against R_th built from scratch."""
        r_inner = (GEOMETRY["drill_mm"] / 2.0) * 1e-3
        r_outer = r_inner + GEOMETRY["plating_mm"] * 1e-3
        area = math.pi * (r_outer ** 2 - r_inner ** 2)
        expected_r = (GEOMETRY["height_mm"] * 1e-3) / (K_COPPER_W_MK * area)

        self.assertAlmostEqual(
            via_thermal_resistance_c_per_w(**GEOMETRY), expected_r, places=9,
        )

        power_w, delta_t = 2.0, 10.0
        self.assertEqual(
            vias_required_for_delta_t(power_w, delta_t, **GEOMETRY),
            math.ceil(power_w * expected_r / delta_t),
        )

    def test_doubling_power_doubles_the_via_count(self):
        few = vias_required_for_delta_t(2.0, 10.0, **GEOMETRY)
        many = vias_required_for_delta_t(4.0, 10.0, **GEOMETRY)
        self.assertAlmostEqual(many, 2 * few, delta=1)  # delta=1 for ceil rounding

    def test_non_positive_power_or_delta_t_raises(self):
        with self.assertRaises(ValueError):
            vias_required_for_delta_t(0.0, 10.0, **GEOMETRY)
        with self.assertRaises(ValueError):
            vias_required_for_delta_t(2.0, 0.0, **GEOMETRY)


class RemediationTests(unittest.TestCase):
    def test_returns_none_when_enough_vias_already_exist(self):
        needed = vias_required_for_delta_t(2.0, 10.0, **GEOMETRY)
        self.assertIsNone(build_thermal_via_remediation(
            "U1", 2.0, 10.0,
            GEOMETRY["height_mm"], GEOMETRY["drill_mm"],
            existing_via_count=needed,
            plating_mm=GEOMETRY["plating_mm"],
        ))

    def test_advises_more_vias_and_never_claims_verification(self):
        needed = vias_required_for_delta_t(2.0, 10.0, **GEOMETRY)
        remediation = build_thermal_via_remediation(
            "U1", 2.0, 10.0,
            GEOMETRY["height_mm"], GEOMETRY["drill_mm"],
            existing_via_count=0,
            plating_mm=GEOMETRY["plating_mm"],
        )
        self.assertIsNotNone(remediation)
        self.assertEqual(remediation.action, "ADD_THERMAL_VIAS")
        self.assertEqual(remediation.target, "U1")
        self.assertEqual(remediation.proposed_value, float(needed))
        self.assertEqual(remediation.unit, "vias")
        self.assertFalse(remediation.verified)
        self.assertIn("not simulated", remediation.predicted_gain)


if __name__ == "__main__":
    unittest.main()
