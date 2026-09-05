"""Quantitative near-end (NEXT) and far-end (FEXT) crosstalk for symmetric
edge-coupled trace pairs, using even/odd mode impedance and velocity from
the shared 2-D quasi-static field solver.

This module characterizes the PHYSICAL coupling of a cross-section (Z0e,
Z0o, ve, vo) and the resulting weak-coupling crosstalk coefficients. It
does not assume a specific aggressor waveform or PCB layout -- those are
supplied by the caller (rise time, coupled length) or a future rule that
extracts them from the board.

References: Bogatin, "Signal Integrity: Simplified"; Hall/Hall/McCall,
"High-Speed Digital System Design" -- weak-coupling backward/forward
crosstalk coefficients from even/odd mode impedance and velocity.
"""

from dataclasses import dataclass

try:
    from .differential_geometry import EdgeCoupledDifferentialSolver
except (ImportError, ValueError):
    from differential_geometry import EdgeCoupledDifferentialSolver

LIGHT_SPEED_M_S = 299792458.0


@dataclass(frozen=True)
class CoupledModeResult:
    odd_mode_impedance_ohm: float
    even_mode_impedance_ohm: float
    odd_mode_velocity_m_s: float
    even_mode_velocity_m_s: float


class SymmetricCoupledLineSolver:
    """Solves both odd and even modes for a symmetric two-conductor
    cross-section (equal trace width/thickness/height, separated by a gap),
    the standard input for weak-coupling crosstalk formulas."""

    LIGHT_SPEED_M_S = LIGHT_SPEED_M_S

    @classmethod
    def solve_microstrip_pair(
        cls, width_mm, gap_mm, copper_thickness_mm, height_below_mm, epsilon_below,
        include_solder_mask=True, solder_mask_thickness_mm=0.02, solder_mask_epsilon_r=3.3,
    ) -> CoupledModeResult:
        odd = EdgeCoupledDifferentialSolver.solve_microstrip(
            width_mm, gap_mm, copper_thickness_mm, height_below_mm, epsilon_below,
            include_solder_mask=include_solder_mask,
            solder_mask_thickness_mm=solder_mask_thickness_mm,
            solder_mask_epsilon_r=solder_mask_epsilon_r,
        )
        even = EdgeCoupledDifferentialSolver.solve_microstrip_even(
            width_mm, gap_mm, copper_thickness_mm, height_below_mm, epsilon_below,
            include_solder_mask=include_solder_mask,
            solder_mask_thickness_mm=solder_mask_thickness_mm,
            solder_mask_epsilon_r=solder_mask_epsilon_r,
        )
        return cls._to_coupled_modes(odd, even)

    @classmethod
    def solve_stripline_pair(
        cls, width_mm, gap_mm, copper_thickness_mm,
        height_above_mm, epsilon_above, height_below_mm, epsilon_below,
    ) -> CoupledModeResult:
        odd = EdgeCoupledDifferentialSolver.solve_stripline(
            width_mm, gap_mm, copper_thickness_mm,
            height_above_mm, epsilon_above, height_below_mm, epsilon_below,
        )
        even = EdgeCoupledDifferentialSolver.solve_stripline_even(
            width_mm, gap_mm, copper_thickness_mm,
            height_above_mm, epsilon_above, height_below_mm, epsilon_below,
        )
        return cls._to_coupled_modes(odd, even)

    @classmethod
    def _to_coupled_modes(cls, odd, even) -> CoupledModeResult:
        v_odd = cls.LIGHT_SPEED_M_S / (odd.effective_epsilon_r ** 0.5)
        v_even = cls.LIGHT_SPEED_M_S / (even.effective_epsilon_r ** 0.5)
        return CoupledModeResult(
            odd_mode_impedance_ohm=odd.odd_mode_impedance_ohm,
            even_mode_impedance_ohm=even.odd_mode_impedance_ohm,
            odd_mode_velocity_m_s=v_odd,
            even_mode_velocity_m_s=v_even,
        )


def near_end_crosstalk_coefficient(modes: CoupledModeResult) -> float:
    """Saturated backward (near-end) coupling coefficient
    Kb_sat = (Z0e - Z0o) / (Z0e + Z0o). Returns 0.0 if Z0e == Z0o."""
    z0e, z0o = modes.even_mode_impedance_ohm, modes.odd_mode_impedance_ohm
    if z0e == z0o:
        return 0.0
    return (z0e - z0o) / (z0e + z0o)


def saturation_length_m(modes: CoupledModeResult, rise_time_s: float) -> float:
    """L_sat = v_avg * rise_time / 2, using the average of odd/even velocity."""
    if rise_time_s <= 0.0:
        raise ValueError("rise_time_s must be positive.")
    v_avg = 0.5 * (modes.odd_mode_velocity_m_s + modes.even_mode_velocity_m_s)
    return v_avg * rise_time_s / 2.0


def near_end_crosstalk_ratio(modes: CoupledModeResult, coupled_length_m: float, rise_time_s: float) -> float:
    """NEXT/V_agg, accounting for saturation:
    Kb_sat * min(1.0, 2*coupled_length/L_sat)."""
    if rise_time_s <= 0.0:
        raise ValueError("rise_time_s must be positive.")
    if coupled_length_m < 0.0:
        raise ValueError("coupled_length_m must be non-negative.")
    kb_sat = near_end_crosstalk_coefficient(modes)
    l_sat = saturation_length_m(modes, rise_time_s)
    if l_sat <= 0.0:
        return 0.0
    saturation_fraction = min(1.0, 2.0 * coupled_length_m / l_sat)
    return kb_sat * saturation_fraction


def far_end_crosstalk_ratio(modes: CoupledModeResult, coupled_length_m: float, rise_time_s: float) -> float:
    """FEXT/V_agg = Kf_per_m * coupled_length_m / rise_time_s, where
    Kf_per_m = 0.5 * (1/v_even - 1/v_odd). Signed -- polarity relative to
    the aggressor edge is physically meaningful, do not abs() it."""
    if rise_time_s <= 0.0:
        raise ValueError("rise_time_s must be positive.")
    if coupled_length_m < 0.0:
        raise ValueError("coupled_length_m must be non-negative.")
    kf_per_m = 0.5 * (1.0 / modes.even_mode_velocity_m_s - 1.0 / modes.odd_mode_velocity_m_s)
    return kf_per_m * coupled_length_m / rise_time_s
