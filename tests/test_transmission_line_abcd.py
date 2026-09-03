"""Tests for ABCD transmission-line matrix primitives.

Scope: analytically-verifiable special cases (quarter-wave, half-wave,
matched line, group property), not a parameter grid.
"""

import math
import unittest

import numpy as np

from transmission_line_abcd import (
    abcd_line,
    abcd_series_impedance,
    abcd_shunt_admittance,
    cascade,
    input_impedance,
    open_stub_impedance,
    reflection_coefficient,
)

Z0 = 50.0
FREQ_HZ = 1.0e9
VELOCITY_M_S = 2.0e8


def _quarter_wave_length_m():
    wavelength = VELOCITY_M_S / FREQ_HZ
    return wavelength / 4.0


def _half_wave_length_m():
    wavelength = VELOCITY_M_S / FREQ_HZ
    return wavelength / 2.0


class AbcdLineSpecialCasesTests(unittest.TestCase):
    def test_vanishingly_short_line_is_near_identity(self):
        # length_m must be > 0 (guarded), so approach zero rather than use it.
        abcd = abcd_line(Z0, 1e-9, FREQ_HZ, VELOCITY_M_S)
        np.testing.assert_allclose(abcd, np.eye(2, dtype=complex), atol=1e-4)

    def test_quarter_wave_line_inverts_impedance(self):
        abcd = abcd_line(Z0, _quarter_wave_length_m(), FREQ_HZ, VELOCITY_M_S)
        self.assertAlmostEqual(abcd[0, 0].real, 0.0, places=9)
        self.assertAlmostEqual(abcd[1, 1].real, 0.0, places=9)
        z_load = 25.0 + 0j
        z_in = input_impedance(abcd, z_load)
        self.assertAlmostEqual(z_in, Z0 ** 2 / z_load, places=6)

    def test_half_wave_line_is_transparent(self):
        abcd = abcd_line(Z0, _half_wave_length_m(), FREQ_HZ, VELOCITY_M_S)
        for z_load in (25.0 + 0j, 130.0 + 20j):
            z_in = input_impedance(abcd, z_load)
            self.assertAlmostEqual(z_in, z_load, places=6)

    def test_matched_line_has_zero_reflection_regardless_of_length(self):
        for length_m in (_quarter_wave_length_m(), 0.037):
            abcd = abcd_line(Z0, length_m, FREQ_HZ, VELOCITY_M_S)
            gamma = reflection_coefficient(abcd, Z0, Z0)
            self.assertLess(abs(gamma), 1e-9)

    def test_non_positive_parameters_raise(self):
        with self.assertRaises(ValueError):
            abcd_line(0.0, 0.01, FREQ_HZ, VELOCITY_M_S)
        with self.assertRaises(ValueError):
            abcd_line(Z0, 0.0, FREQ_HZ, VELOCITY_M_S)
        with self.assertRaises(ValueError):
            abcd_line(Z0, 0.01, FREQ_HZ, 0.0)


class ImpedanceStepTests(unittest.TestCase):
    def test_short_matched_line_into_stepped_load_matches_dc_reflection_formula(self):
        z1, z2 = 50.0, 75.0
        # A very short segment of Z0=z1 line has negligible phase delay, so
        # the reflection into a resistive load z2 should match the classic
        # abrupt-step formula Gamma = (Z2-Z1)/(Z2+Z1).
        abcd = abcd_line(z1, 1e-6, FREQ_HZ, VELOCITY_M_S)
        gamma = reflection_coefficient(abcd, z1, z2)
        expected = (z2 - z1) / (z2 + z1)
        self.assertAlmostEqual(gamma.real, expected, places=4)
        self.assertAlmostEqual(gamma.imag, 0.0, places=4)


class OpenStubTests(unittest.TestCase):
    def test_quarter_wave_open_stub_behaves_as_short_circuit(self):
        # Approach beta*length -> pi/2 from below without hitting the
        # tan() singularity exactly; the impedance magnitude relative to
        # z0 must shrink monotonically as the singularity is approached.
        near_quarter = _quarter_wave_length_m() * 0.999
        closer_to_quarter = _quarter_wave_length_m() * 0.9999
        z_near = abs(open_stub_impedance(Z0, near_quarter, FREQ_HZ, VELOCITY_M_S))
        z_closer = abs(open_stub_impedance(Z0, closer_to_quarter, FREQ_HZ, VELOCITY_M_S))
        self.assertLess(z_closer, z_near)
        self.assertLess(z_closer / Z0, 0.05)


class CascadeTests(unittest.TestCase):
    def test_cascade_is_associative(self):
        a = abcd_series_impedance(10 + 5j)
        b = abcd_shunt_admittance(0.002 - 0.001j)
        c = abcd_line(Z0, 0.01, FREQ_HZ, VELOCITY_M_S)
        left_assoc = cascade(cascade(a, b), c)
        right_assoc = cascade(a, cascade(b, c))
        np.testing.assert_allclose(left_assoc, right_assoc, atol=1e-12)
        np.testing.assert_allclose(cascade(a, b, c), left_assoc, atol=1e-12)

    def test_cascade_requires_at_least_one_matrix(self):
        with self.assertRaises(ValueError):
            cascade()


if __name__ == "__main__":
    unittest.main()
