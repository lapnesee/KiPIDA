"""Surface exchange must follow the physics, not a constant.

The thermal mesh used a flat 5.0 W/m^2K for natural convection on every face
and linearised radiation about ambient. Both under-predict the heat leaving
the board, so the solver over-predicted its temperature -- the audit put the
reported hotspot 5 to 10 C high.
"""

import math
import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

import surface_convection as sc


class RayleighCorrelationTests(unittest.TestCase):
    def test_matches_a_hand_calculation_for_the_real_board(self):
        # 80 x 35 mm board, surface at 78 C, ambient 25 C. Every step is
        # recomputed here rather than compared to a stored constant.
        length_m = sc.characteristic_length_m(80.0 * 35.0, 2.0 * (80.0 + 35.0))
        self.assertAlmostEqual(length_m, 0.01217, places=4)

        delta_t, ambient = 53.0, 25.0
        film_c = ambient + delta_t / 2.0
        nu = sc.air_kinematic_viscosity(film_c)
        alpha = nu / sc.PRANDTL_AIR
        beta = 1.0 / (film_c + 273.15)
        expected_ra = sc.GRAVITY * beta * delta_t * length_m ** 3 / (nu * alpha)

        self.assertAlmostEqual(
            sc.rayleigh_number(delta_t, length_m, film_c), expected_ra, places=6,
        )
        expected_h = (
            0.54 * expected_ra ** 0.25 * sc.air_thermal_conductivity(film_c) / length_m
        )
        self.assertAlmostEqual(
            sc.natural_convection_h(delta_t, length_m, "up", ambient),
            expected_h, places=6,
        )
        # Sanity band: the hardcoded 5.0 it replaces is roughly half of this.
        self.assertGreater(expected_h, 9.0)
        self.assertLess(expected_h, 13.0)

    def test_a_face_pointing_down_exchanges_half_as_well(self):
        # 0.27 / 0.54 -- a hot surface facing up drives a plume, one facing
        # down traps its boundary layer.
        up = sc.natural_convection_h(50.0, 0.012, "up")
        down = sc.natural_convection_h(50.0, 0.012, "down")
        self.assertAlmostEqual(down / up, 0.5, places=6)

    def test_an_isothermal_board_still_exchanges(self):
        # Zero rise must not make the surface adiabatic.
        self.assertGreater(sc.natural_convection_h(0.0, 0.012, "up"), 0.0)

    def test_the_laminar_range_is_reported_not_silently_extrapolated(self):
        self.assertTrue(sc.natural_convection_in_range(1.0e5))
        self.assertFalse(sc.natural_convection_in_range(1.0e9))


class RadiationTests(unittest.TestCase):
    def test_linearising_about_the_surface_exceeds_linearising_about_ambient(self):
        emissivity, surface_c, ambient_c = 0.9, 78.0, 25.0
        at_surface = sc.radiation_h(emissivity, surface_c, ambient_c)
        at_ambient = (
            4.0 * emissivity * sc.SIGMA * (ambient_c + 273.15) ** 3
        )
        self.assertGreater(at_surface, at_ambient)
        # The old code's value, for the record.
        self.assertAlmostEqual(at_ambient, 5.41, places=1)

    def test_the_secant_form_reproduces_the_fourth_power_law_exactly(self):
        # h_r.(Ts - Ta) must equal eps.sigma.(Ts^4 - Ta^4) exactly; this is
        # what distinguishes it from the tangent form 4.eps.sigma.Ts^3.
        emissivity, surface_c, ambient_c = 0.85, 90.0, 20.0
        h_r = sc.radiation_h(emissivity, surface_c, ambient_c)
        surface_k, ambient_k = surface_c + 273.15, ambient_c + 273.15
        exact_flux = emissivity * sc.SIGMA * (surface_k ** 4 - ambient_k ** 4)
        self.assertAlmostEqual(h_r * (surface_k - ambient_k), exact_flux, places=9)

    def test_zero_emissivity_radiates_nothing(self):
        self.assertEqual(sc.radiation_h(0.0, 100.0, 25.0), 0.0)


class ForcedAndCombinedTests(unittest.TestCase):
    def test_forced_convection_matches_the_flat_plate_correlation(self):
        velocity, length_m = 2.0, 0.08
        film_c = 25.0
        reynolds = sc.reynolds_number(velocity, length_m, film_c)
        expected = (
            0.664 * math.sqrt(reynolds) * sc.PRANDTL_AIR ** (1.0 / 3.0)
            * sc.air_thermal_conductivity(film_c) / length_m
        )
        self.assertAlmostEqual(
            sc.forced_convection_h(velocity, length_m, ambient_c=25.0),
            expected, places=6,
        )

    def test_still_air_contributes_no_forced_component(self):
        self.assertEqual(sc.forced_convection_h(0.0, 0.08), 0.0)

    def test_blending_reduces_to_either_branch_alone(self):
        self.assertAlmostEqual(sc.combined_h(9.0, 0.0), 9.0)
        self.assertAlmostEqual(sc.combined_h(0.0, 14.0), 14.0)

    def test_blending_exceeds_both_branches(self):
        blended = sc.combined_h(9.0, 14.0)
        self.assertGreater(blended, 14.0)
        self.assertLess(blended, 23.0)

    def test_turbulent_reynolds_is_flagged(self):
        self.assertTrue(sc.forced_convection_in_range(1.0e4))
        self.assertFalse(sc.forced_convection_in_range(1.0e6))


if __name__ == "__main__":
    unittest.main()
