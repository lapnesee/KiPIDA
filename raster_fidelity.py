"""What the raster mesh does to a track's resistance, as measured.

``mesh.Mesher`` joins 4-neighbour grid nodes inside the copper, so a track is
modelled as however many grid rows its outline happens to contain. The error
depends on the track's width in grid steps, its angle and its alignment to the
grid -- not on the board. These bounds are the measurement of
``validation/dc_raster_fidelity.py`` (a 20 mm track at 0 and 45 degrees, eight
grid alignments, R_mesh / R_exact), so every raster result can state the error
its narrowest track carries instead of leaving it to the reader.
"""

from __future__ import annotations

import math
from typing import Optional

MEASUREMENT = "validation/dc_raster_fidelity.py: a 20 mm track at 0 and 45 degrees, 8 grid alignments"

# (width in grid steps, straight (min, max), 45 degrees (min, max) or None when
# no alignment conducts), R_mesh / R_exact.
TRACK_RESISTANCE_RATIO = (
    (1.0, (0.50, 1.00), None),
    (1.5, (0.75, 1.50), (1.06, 2.12)),
    (2.0, (0.67, 1.00), (1.41, 1.41)),
    (2.5, (0.83, 1.25), (1.18, 1.77)),
    (3.0, (0.75, 1.00), (1.06, 1.41)),
    (4.0, (0.80, 1.00), (1.13, 1.41)),
    (5.0, (0.83, 1.00), (1.18, 1.18)),
    (10.0, (0.91, 1.00), (1.01, 1.09)),
)

# Below this width the plan requires the error to be quoted with the result.
DISCLOSE_BELOW_STEPS = 3.0
# Oblique tracks keep a measurable error well past three steps.
DISCLOSE_OBLIQUE_BELOW_STEPS = 10.0
# A track this far from the grid axes is oblique.
OBLIQUE_TOLERANCE_DEG = 0.5


def is_oblique(start, end, tolerance_deg: float = OBLIQUE_TOLERANCE_DEG) -> bool:
    dx, dy = end[0] - start[0], end[1] - start[1]
    if dx == 0.0 and dy == 0.0:
        return False
    angle = math.degrees(math.atan2(dy, dx)) % 90.0
    return min(angle, 90.0 - angle) > tolerance_deg


def ratio_bounds(width_steps: float):
    """``(straight, diagonal)`` envelopes for a track *width_steps* wide.

    Between two measured widths the envelope of both is quoted; nothing
    narrower than one step was measured, and at one step no diagonal
    alignment conducts. ``diagonal`` is ``None`` when a diagonal track may
    carry no current at all.
    """
    rows = TRACK_RESISTANCE_RATIO
    below = [row for row in rows if row[0] <= width_steps]
    above = [row for row in rows if row[0] >= width_steps]
    chosen = [below[-1] if below else rows[0], above[0] if above else rows[-1]]
    straight = (min(row[1][0] for row in chosen), max(row[1][1] for row in chosen))
    if any(row[2] is None for row in chosen) or not below:
        return straight, None
    diagonal = (min(row[2][0] for row in chosen), max(row[2][1] for row in chosen))
    return straight, diagonal


def width_in_steps(width_mm: float, step_mm: float) -> float:
    # 0.15 / 0.1 is 1.4999999999999998 in floating point; the table's rows are
    # decimal widths, and a hair below one must not fall into the row beneath.
    return round(width_mm / step_mm, 6) if step_mm > 0.0 else 0.0


def _span(bounds) -> str:
    low, high = bounds
    return f"x{low:.2f}" if abs(high - low) < 0.005 else f"x{low:.2f} to x{high:.2f}"


def track_limitation(finest_width_mm: float, step_mm: float, oblique_count: int,
                     mesh_name: str = "raster mesh") -> Optional[str]:
    """The error statement a raster result owes its reader, or ``None``."""
    if finest_width_mm <= 0.0 or step_mm <= 0.0:
        return None
    steps = width_in_steps(finest_width_mm, step_mm)
    if steps >= DISCLOSE_BELOW_STEPS and not (oblique_count and steps < DISCLOSE_OBLIQUE_BELOW_STEPS):
        return None
    head = (
        f"The {mesh_name} ran at {step_mm:g} mm and the narrowest track is "
        f"{finest_width_mm:g} mm, {steps:.3g} grid steps"
    )
    if steps < 1.0:
        return (
            head + ": a track narrower than one grid step may not be meshed at all, "
            "and nothing that narrow was measured, so its resistance in this result "
            f"is unbounded ({MEASUREMENT})."
        )
    straight, diagonal = ratio_bounds(steps)
    text = head + f": a straight track that narrow is modelled at {_span(straight)} its resistance"
    if oblique_count:
        text += f", and {oblique_count} oblique track(s) " + (
            f"at {_span(diagonal)}" if diagonal is not None
            else "may be left carrying no current at all"
        )
    return text + f", depending on grid alignment ({MEASUREMENT})."
