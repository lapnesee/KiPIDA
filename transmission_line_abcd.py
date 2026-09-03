"""ABCD (transmission) matrix primitives for cascaded transmission-line
segments, impedance discontinuities, and open/short stubs.

Standard two-port network theory (Pozar, Microwave Engineering). Used to
compute reflection coefficients at impedance transitions (connector
launches, trace-width steps, via stubs) from a chain of known Z0/length
segments -- a physically grounded alternative to a flat "keep stubs
short" rule of thumb.
"""

import math

import numpy as np


def abcd_line(z0_ohm, length_m, frequency_hz, velocity_m_s):
    """2x2 complex ABCD matrix for a lossless transmission-line segment.

    A = D = cos(beta*length), B = j*z0*sin(beta*length),
    C = j*sin(beta*length)/z0, with beta = 2*pi*frequency/velocity.
    """
    if z0_ohm <= 0.0:
        raise ValueError("z0_ohm must be positive.")
    if length_m <= 0.0:
        raise ValueError("length_m must be positive.")
    if velocity_m_s <= 0.0:
        raise ValueError("velocity_m_s must be positive.")
    beta = 2.0 * math.pi * frequency_hz / velocity_m_s
    theta = beta * length_m
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    return np.array([
        [cos_t, 1j * z0_ohm * sin_t],
        [1j * sin_t / z0_ohm, cos_t],
    ], dtype=complex)


def abcd_series_impedance(z_ohm):
    """A=1, B=z_ohm, C=0, D=1 -- a series lumped impedance."""
    return np.array([[1.0 + 0j, complex(z_ohm)], [0j, 1.0 + 0j]], dtype=complex)


def abcd_shunt_admittance(y_siemens):
    """A=1, B=0, C=y_siemens, D=1 -- a shunt lumped admittance."""
    return np.array([[1.0 + 0j, 0j], [complex(y_siemens), 1.0 + 0j]], dtype=complex)


def cascade(*matrices):
    """Matrix-multiply a sequence of 2x2 ABCD matrices in cascade order
    (leftmost = closest to source)."""
    if not matrices:
        raise ValueError("cascade() requires at least one matrix.")
    result = matrices[0]
    for matrix in matrices[1:]:
        result = result @ matrix
    return result


def input_impedance(abcd, z_load_ohm):
    """Zin = (A*Zload + B) / (C*Zload + D)."""
    a, b = abcd[0, 0], abcd[0, 1]
    c, d = abcd[1, 0], abcd[1, 1]
    return (a * z_load_ohm + b) / (c * z_load_ohm + d)


def reflection_coefficient(abcd, z_source_ohm, z_load_ohm):
    """Gamma = (Zin - Zsource) / (Zin + Zsource)."""
    z_in = input_impedance(abcd, z_load_ohm)
    return (z_in - z_source_ohm) / (z_in + z_source_ohm)


def open_stub_impedance(z0_ohm, length_m, frequency_hz, velocity_m_s):
    """Zin = -j*z0/tan(beta*length) for an open-circuited stub."""
    if z0_ohm <= 0.0:
        raise ValueError("z0_ohm must be positive.")
    if length_m <= 0.0:
        raise ValueError("length_m must be positive.")
    if velocity_m_s <= 0.0:
        raise ValueError("velocity_m_s must be positive.")
    beta = 2.0 * math.pi * frequency_hz / velocity_m_s
    return -1j * z0_ohm / math.tan(beta * length_m)


def short_stub_impedance(z0_ohm, length_m, frequency_hz, velocity_m_s):
    """Zin = j*z0*tan(beta*length) for a short-circuited stub."""
    if z0_ohm <= 0.0:
        raise ValueError("z0_ohm must be positive.")
    if length_m <= 0.0:
        raise ValueError("length_m must be positive.")
    if velocity_m_s <= 0.0:
        raise ValueError("velocity_m_s must be positive.")
    beta = 2.0 * math.pi * frequency_hz / velocity_m_s
    return 1j * z0_ohm * math.tan(beta * length_m)
