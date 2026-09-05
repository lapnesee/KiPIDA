"""IPC-2152 external-layer trace temperature rise model (curve-fit approximation).

Reference: the closed-form power-law fit of the IPC-2221/IPC-2152
external-layer, still-air chart:

    I = k * (delta_T ** b) * (A ** c)

with k=0.048, b=0.44, c=0.725 (external layer constants), A = cross-section
area in mils^2. This is the same fast approximation used across the
industry as an IPC-2152-consistent estimate; it is explicitly NOT the full
IPC-2152 lookup-table method, and callers must not present its output as a
measurement.

Geometry-only: neither function here depends on a DC solver result. A rule
that ties this to an actual load current belongs to a later phase once a
schematic-derived load current is available.
"""

from __future__ import annotations

_MILS_PER_MM = 1000.0 / 25.4  # 39.37007874...
_K_EXTERNAL = 0.048
_DELTA_T_EXPONENT = 0.44
_AREA_EXPONENT = 0.725


def _mm_to_mils(value_mm: float) -> float:
    return value_mm * _MILS_PER_MM


def external_layer_temp_rise_c(current_a: float, width_mm: float, thickness_mm: float) -> float:
    """Approximate temperature rise (degC above ambient) for an external
    copper trace carrying *current_a* amps.

    Raises ValueError for non-positive inputs.
    """
    if current_a <= 0 or width_mm <= 0 or thickness_mm <= 0:
        raise ValueError("current_a, width_mm, and thickness_mm must be positive")
    area_mils2 = _mm_to_mils(width_mm) * _mm_to_mils(thickness_mm)
    return (current_a / (_K_EXTERNAL * area_mils2 ** _AREA_EXPONENT)) ** (1.0 / _DELTA_T_EXPONENT)


def required_width_mm_for_current(
    current_a: float, thickness_mm: float, max_temp_rise_c: float,
) -> float:
    """Minimum external-layer trace width (mm) to keep temperature rise at
    or under *max_temp_rise_c* for *current_a* amps and the given copper
    thickness. Inverse of :func:`external_layer_temp_rise_c`.

    Raises ValueError for non-positive inputs.
    """
    if current_a <= 0 or thickness_mm <= 0 or max_temp_rise_c <= 0:
        raise ValueError("current_a, thickness_mm, and max_temp_rise_c must be positive")
    area_mils2 = (current_a / (_K_EXTERNAL * max_temp_rise_c ** _DELTA_T_EXPONENT)) ** (1.0 / _AREA_EXPONENT)
    thickness_mils = _mm_to_mils(thickness_mm)
    width_mils = area_mils2 / thickness_mils
    return width_mils / _MILS_PER_MM
