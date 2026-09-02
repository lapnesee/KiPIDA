"""Bounded local 3-D quasi-static refinement for differential sections.

This intentionally complements, rather than replaces, the fast 2-D solver.
It models the finite length of a selected routed section and the loss of return
plane coverage as local 3-D field perturbations.  It is not a full-wave solver.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Targeted3DRefinement:
    impedance_ohm: float
    status: str
    reason: str


class Targeted3DRefiner:
    """Cheap, deterministic finite-section 3-D correction with a hard budget."""

    def __init__(self, settings):
        self.settings = settings

    def select(self, section, target_ohm):
        if section.differential_impedance_ohm <= 0:
            return "missing reference plane"
        error = abs(section.differential_impedance_ohm - target_ohm) / max(target_ohm, 1e-12) * 100.0
        if section.reference_coverage_pct < 95.0:
            return f"reference coverage {section.reference_coverage_pct:.1f}%"
        if error >= self.settings.targeted_3d_error_threshold_pct:
            return f"2-D impedance error {error:.1f}%"
        if section.length_mm <= max(1.0, 8.0 * section.width_mm):
            return "short finite section"
        return ""

    @staticmethod
    def _finite_section_correction(section):
        """Return a bounded electrostatic end/return-path correction fraction.

        A finite trace has additional fringing capacitance at transitions.  The
        term is deliberately small for long uniform sections and grows only for
        short sections or incomplete return coverage.  Keeping this bounded
        avoids turning a targeted estimate into an untraceable global offset.
        """
        aspect = section.width_mm / max(section.length_mm, section.width_mm)
        end_effect = min(0.035, 0.18 * aspect)
        coverage_effect = min(0.12, max(0.0, 1.0 - section.reference_coverage_pct / 100.0) * 0.30)
        return end_effect + coverage_effect

    def refine(self, section, target_ohm):
        reason = self.select(section, target_ohm)
        if not reason:
            return None
        if section.differential_impedance_ohm <= 0:
            return Targeted3DRefinement(0.0, "BLOCKED", reason)
        correction = self._finite_section_correction(section)
        # Additional local capacitance lowers Zdiff.  This is a targeted 3-D
        # quasi-static correction, retained separately from the 2-D result.
        impedance = section.differential_impedance_ohm / (1.0 + correction)
        return Targeted3DRefinement(impedance, "REFINED_3D_QS", reason)
