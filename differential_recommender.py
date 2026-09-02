"""Manufacturability-aware recommendations for Phase 5 differential results."""

from dataclasses import replace

try:
    from .differential_geometry import COPLANAR_GEOMETRIES, GroundedCoplanarDifferentialSolver
    from .differential_impedance import DifferentialImpedanceSolver
    from .models import DifferentialRecommendation
except (ImportError, ValueError):
    from differential_geometry import COPLANAR_GEOMETRIES, GroundedCoplanarDifferentialSolver
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

    def _predict(self, section, width, gap):
        if section.geometry_mode in COPLANAR_GEOMETRIES or section.topology.startswith("COPLANAR_"):
            if section.topology == "COPLANAR_STRIPLINE":
                height_below, epsilon_below = section.reference_below_distance_mm, section.reference_below_epsilon_r
                height_above, epsilon_above = section.reference_above_distance_mm, section.reference_above_epsilon_r
            else:
                height_below, epsilon_below = section.reference_distance_mm, section.reference_epsilon_r
                height_above, epsilon_above = 0.0, epsilon_below
            return GroundedCoplanarDifferentialSolver.solve(
                width, gap, section.ground_clearance_mm, height_below, epsilon_below,
                copper_thickness_mm=section.copper_thickness_mm,
                height_above_mm=height_above, epsilon_above=epsilon_above,
                include_solder_mask=self.settings.include_solder_mask,
                solder_mask_thickness_mm=self.settings.solder_mask_thickness_mm,
                solder_mask_epsilon_r=self.settings.solder_mask_epsilon_r,
                backing_plane=section.topology != "COPLANAR_WAVEGUIDE",
            ).differential_impedance_ohm
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

    @staticmethod
    def _weighted_mean(sections, attribute):
        total = sum(max(item.length_mm, 1e-12) for item in sections)
        return sum(
            getattr(item, attribute) * max(item.length_mm, 1e-12)
            for item in sections
        ) / total

    @staticmethod
    def _manufacturable(value, minimum, step=0.005):
        """Round a theoretical solution to an ordinary PCB geometry grid."""
        return max(minimum, round(float(value) / step) * step)

    def _primary_route_section(self, result):
        """Select the repeated coupled route body, excluding breakout gaps."""
        solved = [
            item for item in result.sections
            if item.differential_impedance_ohm > 0 and item.length_mm > 0
        ]
        if not solved:
            return None, 0.0, []

        # A gap above roughly three trace widths is normally a breakout,
        # connector escape, or unmatched transition.  It must be normalized,
        # not compensated by widening that isolated trace section.
        coupled = [
            item for item in solved
            if item.gap_mm <= max(0.30, 3.0 * item.width_mm)
            and item.reference_coverage_pct >= 90.0
        ] or solved

        clusters = []
        for section in sorted(coupled, key=lambda item: -item.length_mm):
            cluster = next((members for members in clusters if (
                members[0].layer_name == section.layer_name
                and members[0].topology == section.topology
                and abs(members[0].width_mm - section.width_mm)
                    <= max(0.010, 0.10 * members[0].width_mm)
                and abs(members[0].gap_mm - section.gap_mm)
                    <= max(0.020, 0.15 * members[0].gap_mm)
            )), None)
            if cluster is None:
                clusters.append([section])
            else:
                cluster.append(section)
        primary_members = max(
            clusters,
            key=lambda members: sum(item.length_mm for item in members),
        )
        template = max(primary_members, key=lambda item: item.length_mm)
        # Preserve the measured route dimensions here. Fabrication floors are
        # constraints on the recommendation, not a rewrite of observed PCB
        # geometry.
        width = self._manufacturable(
            self._weighted_mean(primary_members, "width_mm"), 0.001
        )
        gap = self._manufacturable(
            self._weighted_mean(primary_members, "gap_mm"), 0.001
        )
        ground_clearance = max(
            template.ground_clearance_mm,
            self.settings.minimum_ground_clearance_mm,
        )
        if template.geometry_mode in COPLANAR_GEOMETRIES:
            ground_clearance = self._manufacturable(
                self.settings.coplanar_ground_gap_mm,
                self.settings.minimum_ground_clearance_mm,
            )
        else:
            ground_clearance = self._manufacturable(
                ground_clearance, self.settings.minimum_ground_clearance_mm,
            )
        primary = replace(
            template,
            width_mm=width,
            gap_mm=gap,
            ground_clearance_mm=ground_clearance,
        )
        try:
            primary.differential_impedance_ohm = self._predict(primary, width, gap)
        except ValueError:
            pass
        primary_length = sum(item.length_mm for item in primary_members)
        # Coverage must include unreferenced paired sections as well. Excluding
        # them made a fragmented route look more representative precisely when
        # its return path was missing.
        routed_length = sum(
            max(item.length_mm, 0.0) for item in result.sections
            if item.topology != "BREAKOUT_TRANSITION"
        )
        member_ids = {id(item) for item in primary_members}
        outliers = [item for item in solved if id(item) not in member_ids]
        return primary, 100.0 * primary_length / max(routed_length, 1e-12), outliers

    def _recommend_primary_geometry(self, result, section, coverage_pct):
        pair = result.pair
        target = max(pair.target_impedance_ohm, 1e-12)
        actual_width = section.width_mm
        actual_gap = section.gap_mm
        current = self._predict(section, actual_width, actual_gap)
        error_pct = 100.0 * (current - target) / target
        min_width = max(0.001, self.settings.minimum_width_mm)
        min_gap = max(0.001, self.settings.minimum_gap_mm)
        width = self._manufacturable(actual_width, min_width)
        gap = self._manufacturable(actual_gap, min_gap)
        violates_fabrication_floor = (
            actual_width + 1e-9 < min_width or actual_gap + 1e-9 < min_gap
        )
        if abs(error_pct) > self.settings.target_tolerance_pct:
            solution = self._solve_dimension(
                lambda candidate: self._predict(section, candidate, gap),
                target,
                min_width,
                max(actual_width * 6.0, section.reference_distance_mm * 5.0),
            )
            if solution is not None:
                width = self._manufacturable(solution, min_width)
            else:
                solution = self._solve_dimension(
                    lambda candidate: self._predict(section, width, candidate),
                    target,
                    min_gap,
                    max(actual_gap * 8.0, section.reference_distance_mm * 12.0),
                )
                if solution is not None:
                    gap = self._manufacturable(solution, min_gap)
        predicted = self._predict(section, width, gap)
        unchanged = (
            not violates_fabrication_floor
            and abs(width - actual_width) < 0.0025
            and abs(gap - actual_gap) < 0.0025
        )
        action = "KEEP_PRIMARY_ROUTE" if unchanged else "SET_PRIMARY_ROUTE"
        feasibility = "PASS" if unchanged else "APPLY"
        if coverage_pct < 50.0:
            action = "REROUTE_PAIR"
            feasibility = "REVIEW"
        elif abs(100.0 * (predicted - target) / target) > self.settings.target_tolerance_pct:
            action = "REVIEW_STACKUP"
            feasibility = "BLOCKED"
        return DifferentialRecommendation(
            pair_signature=pair.signature,
            pair_name=pair.name,
            layer_name=section.layer_name,
            topology=section.topology,
            geometry_mode=section.geometry_mode,
            current_width_mm=actual_width,
            current_gap_mm=actual_gap,
            current_impedance_ohm=current,
            target_impedance_ohm=target,
            recommended_width_mm=width,
            recommended_gap_mm=gap,
            recommended_ground_clearance_mm=section.ground_clearance_mm,
            reference_distance_mm=section.reference_distance_mm,
            predicted_impedance_ohm=predicted,
            action=action,
            feasibility=feasibility,
            confidence="HIGH" if section.trustworthy and coverage_pct >= 50.0 else "ESTIMATE",
            warnings=[
                f"Primary repeated geometry represents {coverage_pct:.1f}% of the paired routed length."
            ],
        )

    def recommend_pair(self, result):
        pair = result.pair
        if not result.sections:
            return [DifferentialRecommendation(
                pair_signature=pair.signature, pair_name=pair.name,
                action="ROUTE_PAIR", feasibility="BLOCKED", confidence="LOW",
                warnings=["No paired same-layer routing section was detected."],
            )]
        section, primary_coverage_pct, outliers = self._primary_route_section(result)
        if section is None:
            return [DifferentialRecommendation(
                pair_signature=pair.signature, pair_name=pair.name,
                action="RESTORE_GND_REFERENCE", feasibility="BLOCKED", confidence="LOW",
                recommended_ground_clearance_mm=self.settings.minimum_ground_clearance_mm,
                warnings=["No routed section has a usable continuous reference plane."],
            )]

        unresolved = [
            item for item in result.sections
            if item.differential_impedance_ohm <= 0
            and item.topology != "BREAKOUT_TRANSITION"
        ]
        transitions = [
            item for item in result.sections
            if item.topology == "BREAKOUT_TRANSITION"
        ]
        primary = self._recommend_primary_geometry(result, section, primary_coverage_pct)
        if outliers:
            primary.warnings.append(
                f"Normalize {len(outliers)} breakout/transition section(s) toward the primary "
                f"gap of {primary.recommended_gap_mm:.3f} mm; do not compensate them with another trace width."
            )
        if transitions:
            primary.warnings.append(
                f"Review {len(transitions)} connector/breakout transition(s) locally; "
                "they are excluded from the global W/G rule and GND-plane qualification."
            )
        if unresolved:
            primary.feasibility = "AFTER_REFERENCE_FIX"
            primary.warnings.append(
                f"Restore continuous adjacent GND under {len(unresolved)} section(s) before sign-off."
            )
            blocker = DifferentialRecommendation(
                pair_signature=pair.signature,
                pair_name=pair.name,
                layer_name=unresolved[0].layer_name,
                topology="UNREFERENCED",
                geometry_mode=unresolved[0].geometry_mode,
                target_impedance_ohm=pair.target_impedance_ohm,
                recommended_ground_clearance_mm=self.settings.minimum_ground_clearance_mm,
                action="RESTORE_GND_REFERENCE",
                feasibility="BLOCKED",
                confidence="HIGH",
                warnings=[
                    "Restore the reference plane; this is a return-path correction, not an alternate impedance geometry."
                ],
            )
            return [primary, blocker]
        return [primary]

    def recommend(self, results):
        for result in results:
            result.recommendations = self.recommend_pair(result)
            blocking_actions = {
                "REROUTE_PAIR", "RESTORE_GND_REFERENCE",
                "REVIEW_STACKUP", "REVIEW_DISCONTINUITIES",
            }
            blockers = [
                item.action for item in result.recommendations
                if item.action in blocking_actions
            ]
            if blockers and result.status != "NO_DATA":
                result.status = "FAIL"
                warning = (
                    "Routing qualification failed: "
                    + ", ".join(dict.fromkeys(blockers))
                    + "."
                )
                if warning not in result.warnings:
                    result.warnings.append(warning)
        return results
