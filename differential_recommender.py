"""Manufacturability-aware recommendations for Phase 5 differential results."""

try:
    from .differential_impedance import DifferentialImpedanceSolver
    from .models import DifferentialRecommendation
except (ImportError, ValueError):
    from differential_impedance import DifferentialImpedanceSolver
    from models import DifferentialRecommendation


class DifferentialRecommendationEngine:
    """Invert the Phase 5 closed-form model without altering PCB geometry."""

    def __init__(self, settings):
        self.settings = settings

    @staticmethod
    def _solve_dimension(function, target, low, high):
        low_value, high_value = function(low), function(high)
        increasing = high_value > low_value
        if not min(low_value, high_value) <= target <= max(low_value, high_value):
            return None
        for _ in range(48):
            middle = 0.5 * (low + high)
            value = function(middle)
            if abs(value - target) <= 0.01:
                return middle
            if increasing:
                low, high = (middle, high) if value < target else (low, middle)
            else:
                low, high = (middle, high) if value > target else (low, middle)
        return 0.5 * (low + high)

    @staticmethod
    def _predict(section, width, gap):
        if section.topology in {"MICROSTRIP", "EMBEDDED_MICROSTRIP"}:
            return DifferentialImpedanceSolver._microstrip(
                width, gap, section.copper_thickness_mm,
                section.reference_distance_mm, section.reference_epsilon_r,
            )[1]
        if section.topology in {"STRIPLINE", "ASYMMETRIC_STRIPLINE"}:
            return DifferentialImpedanceSolver._stripline(
                width, gap, section.copper_thickness_mm,
                section.reference_above_distance_mm,
                section.reference_below_distance_mm,
                section.reference_epsilon_r,
            )[1]
        raise ValueError("No continuous reference plane is available.")

    def recommend_pair(self, result):
        pair = result.pair
        if not result.sections:
            return [DifferentialRecommendation(
                pair_signature=pair.signature, pair_name=pair.name,
                action="ROUTE_PAIR", feasibility="BLOCKED", confidence="LOW",
                warnings=["No paired same-layer routing section was detected."],
            )]
        section = max(result.sections, key=lambda item: item.length_mm)
        base = dict(
            pair_signature=pair.signature, pair_name=pair.name,
            layer_name=section.layer_name, topology=section.topology,
            current_width_mm=section.width_mm, current_gap_mm=section.gap_mm,
            current_impedance_ohm=section.differential_impedance_ohm,
            target_impedance_ohm=pair.target_impedance_ohm,
            reference_distance_mm=section.reference_distance_mm,
            recommended_ground_clearance_mm=max(
                self.settings.minimum_ground_clearance_mm, section.width_mm * 3.0,
            ),
            confidence="HIGH" if result.trustworthy else "ESTIMATE",
        )
        if section.differential_impedance_ohm <= 0:
            return [DifferentialRecommendation(
                **base, action="RESTORE_GND_REFERENCE", feasibility="BLOCKED",
                warnings=["Add or restore a continuous adjacent reference plane before tuning width or gap."],
            )]
        tolerance = self.settings.target_tolerance_pct
        if abs(result.error_pct) <= tolerance:
            return [DifferentialRecommendation(
                **base, action="KEEP_GEOMETRY", feasibility="PASS",
                recommended_width_mm=section.width_mm, recommended_gap_mm=section.gap_mm,
                predicted_impedance_ohm=section.differential_impedance_ohm,
                warnings=self._warnings(result, section),
            )]
        target = pair.target_impedance_ohm
        min_width = max(0.001, self.settings.minimum_width_mm)
        min_gap = max(0.001, self.settings.minimum_gap_mm)
        try:
            width = self._solve_dimension(
                lambda candidate: self._predict(section, candidate, section.gap_mm),
                target, min_width, max(section.width_mm * 6.0, section.reference_distance_mm * 5.0),
            )
        except ValueError:
            width = None
        if width is not None:
            return [DifferentialRecommendation(
                **base, action="ADJUST_WIDTH", feasibility="REVIEW",
                recommended_width_mm=width, recommended_gap_mm=section.gap_mm,
                predicted_impedance_ohm=self._predict(section, width, section.gap_mm),
                warnings=self._warnings(result, section),
            )]
        try:
            gap = self._solve_dimension(
                lambda candidate: self._predict(section, section.width_mm, candidate),
                target, min_gap, max(section.gap_mm * 8.0, section.reference_distance_mm * 12.0),
            )
        except ValueError:
            gap = None
        if gap is not None:
            return [DifferentialRecommendation(
                **base, action="ADJUST_GAP", feasibility="REVIEW",
                recommended_width_mm=section.width_mm, recommended_gap_mm=gap,
                predicted_impedance_ohm=self._predict(section, section.width_mm, gap),
                warnings=self._warnings(result, section),
            )]
        return [DifferentialRecommendation(
            **base, action="REVIEW_STACKUP", feasibility="BLOCKED",
            warnings=self._warnings(result, section) + [
                "Target cannot be reached with the selected fabrication limits; use a closer ground plane or another signal layer."
            ],
        )]

    @staticmethod
    def _warnings(result, section):
        warnings = list(section.warnings)
        if not result.trustworthy:
            warnings.append("Stackup or ground-plane coverage is not trusted; validate with the fabricator.")
        warnings.append("Recommendation is quasi-static; via transitions, roughness and etch compensation are outside this model.")
        return list(dict.fromkeys(warnings))

    def recommend(self, results):
        for result in results:
            result.recommendations = self.recommend_pair(result)
        return results
