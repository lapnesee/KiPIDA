"""Analytical resistance models for PCB copper conductors.

All inputs in mm; resistivity in Ω·m; outputs in Ω.
No external dependencies.
"""

import math

# Copper resistivity at 20 °C, Ω·m
RHO_COPPER = 1.72e-8

_MM_TO_M = 1e-3


def segment_resistance(
    length_mm: float,
    width_mm: float,
    thickness_mm: float,
    rho: float = RHO_COPPER,
) -> float:
    """R = ρ·L / (w·t) for a rectangular copper segment.

    Args:
        length_mm: Conductor length in mm.
        width_mm: Conductor width in mm.
        thickness_mm: Copper thickness in mm.
        rho: Resistivity in Ω·m (default: copper at 20 °C).

    Returns:
        Resistance in Ω.

    Raises:
        ValueError: If any dimension is non-positive.
    """
    if length_mm <= 0:
        raise ValueError(f"length_mm must be > 0, got {length_mm}")
    if width_mm <= 0:
        raise ValueError(f"width_mm must be > 0, got {width_mm}")
    if thickness_mm <= 0:
        raise ValueError(f"thickness_mm must be > 0, got {thickness_mm}")
    L = length_mm * _MM_TO_M
    w = width_mm * _MM_TO_M
    t = thickness_mm * _MM_TO_M
    return rho * L / (w * t)


def via_resistance(
    height_mm: float,
    drill_mm: float,
    plating_mm: float = 0.025,
    rho: float = RHO_COPPER,
) -> float:
    """Resistance of a plated-through via barrel.

    Models the barrel as a thin annular cylinder with outer radius
    r_outer = drill_mm/2 + plating_mm and inner radius r_inner = drill_mm/2.
    Cross-sectional area ≈ π·drill_mm·plating_mm for thin plating
    (exact formula used when plating is significant).

    Args:
        height_mm: Board thickness / via height in mm.
        drill_mm: Finished drill diameter in mm.
        plating_mm: Copper plating thickness in mm (default 25 µm).
        rho: Resistivity in Ω·m.

    Returns:
        Resistance in Ω.

    Raises:
        ValueError: If any dimension is non-positive.
    """
    if height_mm <= 0:
        raise ValueError(f"height_mm must be > 0, got {height_mm}")
    if drill_mm <= 0:
        raise ValueError(f"drill_mm must be > 0, got {drill_mm}")
    if plating_mm <= 0:
        raise ValueError(f"plating_mm must be > 0, got {plating_mm}")
    h = height_mm * _MM_TO_M
    r_inner = (drill_mm / 2) * _MM_TO_M
    r_outer = r_inner + plating_mm * _MM_TO_M
    area = math.pi * (r_outer ** 2 - r_inner ** 2)
    return rho * h / area


def spreading_resistance(
    thickness_mm: float,
    r1_mm: float,
    r2_mm: float,
    rho: float = RHO_COPPER,
) -> float:
    """Radial spreading resistance between two radii r1 < r2 in a plane.

    R = ρ / (2π·t) · ln(r2 / r1)

    Used to model the resistance from a via/pad barrel to the surrounding
    copper plane at radius r2.

    Args:
        thickness_mm: Plane thickness in mm.
        r1_mm: Inner radius in mm (e.g. via barrel outer radius).
        r2_mm: Outer radius in mm (effective current-spreading radius).
        rho: Resistivity in Ω·m.

    Returns:
        Resistance in Ω.

    Raises:
        ValueError: If r1 >= r2 or any dimension is non-positive.
    """
    if thickness_mm <= 0:
        raise ValueError(f"thickness_mm must be > 0, got {thickness_mm}")
    if r1_mm <= 0:
        raise ValueError(f"r1_mm must be > 0, got {r1_mm}")
    if r2_mm <= r1_mm:
        raise ValueError(f"r2_mm must be > r1_mm, got r2={r2_mm}, r1={r1_mm}")
    t = thickness_mm * _MM_TO_M
    return rho / (2 * math.pi * t) * math.log(r2_mm / r1_mm)
