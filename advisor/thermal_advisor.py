"""Analytical thermal-via sizing.

How many vias under a hot part keep its rise under a target? That is a
one-dimensional conduction question -- ``R_th = L / (k·A)`` per barrel, N in
parallel -- and it needs no solver, so this module is purely analytical.

That also bounds what it may claim. A 1-D barrel model ignores spreading in
the planes, the board's lateral conduction and any convection, so every
:class:`~analysis_contract.Remediation` produced here carries
``verified=False``: it is a sizing estimate, not a simulated result. Only the
DC advisor re-simulates, and only it sets ``verified=True``.
"""

from __future__ import annotations

import math
from typing import Optional

try:
    from ..analysis_contract import Remediation, RemediationEffort
except (ImportError, ValueError):
    from analysis_contract import Remediation, RemediationEffort


# Thermal conductivity of copper at room temperature, W/(m·K)
K_COPPER_W_MK = 385.0

_MM_TO_M = 1e-3


def via_thermal_resistance_c_per_w(
    height_mm: float, drill_mm: float, plating_mm: float = 0.025,
    k_copper_w_mk: float = K_COPPER_W_MK,
) -> float:
    """Thermal resistance of one plated via barrel, in °C/W.

    ``R_th = L / (k·A)`` with ``A`` the annular copper cross-section -- the
    same thin-annulus geometry as
    :func:`~ingest.track_resistance.via_resistance`, so the electrical and
    thermal models describe the same physical barrel.

    Raises:
        ValueError: If any dimension or the conductivity is non-positive.
    """
    if height_mm <= 0:
        raise ValueError(f"height_mm must be > 0, got {height_mm}")
    if drill_mm <= 0:
        raise ValueError(f"drill_mm must be > 0, got {drill_mm}")
    if plating_mm <= 0:
        raise ValueError(f"plating_mm must be > 0, got {plating_mm}")
    if k_copper_w_mk <= 0:
        raise ValueError(f"k_copper_w_mk must be > 0, got {k_copper_w_mk}")
    length_m = height_mm * _MM_TO_M
    r_inner = (drill_mm / 2.0) * _MM_TO_M
    r_outer = r_inner + plating_mm * _MM_TO_M
    area_m2 = math.pi * (r_outer ** 2 - r_inner ** 2)
    return length_m / (k_copper_w_mk * area_m2)


def vias_required_for_delta_t(
    power_w: float, target_delta_t_c: float,
    height_mm: float, drill_mm: float, plating_mm: float = 0.025,
    k_copper_w_mk: float = K_COPPER_W_MK,
) -> int:
    """Parallel via count needed to hold the rise under *target_delta_t_c*.

    N barrels in parallel give ``R_total = R_single / N``, so
    ``N = ceil(power_w · R_single / target_delta_t_c)``, floored at 1.

    Raises:
        ValueError: If the power, the target rise, or any geometry is
            non-positive.
    """
    if power_w <= 0:
        raise ValueError(f"power_w must be > 0, got {power_w}")
    if target_delta_t_c <= 0:
        raise ValueError(f"target_delta_t_c must be > 0, got {target_delta_t_c}")
    r_single = via_thermal_resistance_c_per_w(
        height_mm, drill_mm, plating_mm, k_copper_w_mk,
    )
    return max(1, math.ceil(power_w * r_single / target_delta_t_c))


def _effort_for_added_vias(added: int) -> RemediationEffort:
    """A handful of vias is routine; dozens usually means re-placing the part."""
    if added <= 4:
        return RemediationEffort.LOW
    if added <= 16:
        return RemediationEffort.MEDIUM
    return RemediationEffort.HIGH


def build_thermal_via_remediation(
    component_ref: str, power_w: float, target_delta_t_c: float,
    height_mm: float, drill_mm: float, existing_via_count: int = 0,
    plating_mm: float = 0.025,
) -> Optional[Remediation]:
    """Advise adding thermal vias under *component_ref*, or ``None`` if enough.

    ``verified`` is always ``False``: this is a 1-D analytical sizing, and the
    returned ``predicted_gain`` says so explicitly.
    """
    required = vias_required_for_delta_t(
        power_w, target_delta_t_c, height_mm, drill_mm, plating_mm,
    )
    existing = max(0, int(existing_via_count))
    if required <= existing:
        return None

    r_single = via_thermal_resistance_c_per_w(height_mm, drill_mm, plating_mm)
    current_rise = (
        power_w * r_single / existing if existing > 0 else float("inf")
    )
    current_text = (
        f"{current_rise:.1f} °C" if existing > 0 else "no thermal path"
    )
    achieved_rise = power_w * r_single / required

    return Remediation(
        action="ADD_THERMAL_VIAS",
        target=component_ref,
        current_value=float(existing),
        proposed_value=float(required),
        unit="vias",
        predicted_gain=(
            f"conducted rise {current_text} -> {achieved_rise:.1f} °C at "
            f"{power_w:.3g} W (1-D barrel sizing, not simulated)"
        ),
        effort=_effort_for_added_vias(required - existing),
        verified=False,
        alternatives=[
            "Increase the copper pour area connected to the pad",
            "Move the part away from other heat sources",
        ],
    )
