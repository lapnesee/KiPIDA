"""Stackup-aware quasi-static differential transmission-line analysis."""

import math

try:
    from shapely.geometry import LineString, Point
    from shapely.ops import unary_union
except ImportError:
    LineString = Point = unary_union = None

try:
    from .differential_3d import Targeted3DRefiner
    from .differential_length import assess_length_symmetry
    from .differential_geometry import (
        COPLANAR_GEOMETRIES, GEOMETRY_MICROSTRIP, GEOMETRY_STRIPLINE,
        EdgeCoupledDifferentialSolver, GroundedCoplanarDifferentialSolver,
        normalize_geometry,
    )
    from .models import DifferentialPairResult, DifferentialSectionResult
    from .reference_plane_analyzer import ReferencePlaneAnalyzer
except (ImportError, ValueError):
    from differential_3d import Targeted3DRefiner
    from differential_length import assess_length_symmetry
    from differential_geometry import (
        COPLANAR_GEOMETRIES, GEOMETRY_MICROSTRIP, GEOMETRY_STRIPLINE,
        EdgeCoupledDifferentialSolver, GroundedCoplanarDifferentialSolver,
        normalize_geometry,
    )
    from models import DifferentialPairResult, DifferentialSectionResult
    from reference_plane_analyzer import ReferencePlaneAnalyzer


class DifferentialGeometrySnapshot:
    """Immutable board geometry captured before entering a worker thread."""

    def __init__(self, tracks_by_net=None, zones_by_net=None):
        self.tracks_by_net = dict(tracks_by_net or {})
        self.zones_by_net = dict(zones_by_net or {})

    @classmethod
    def capture(cls, extractor, pairs, reference_net_names):
        pair_nets = {
            net_name for pair in pairs
            for net_name in (pair.positive_net, pair.negative_net)
        }
        tracks = {
            net_name: extractor.get_net_tracks(net_name) for net_name in pair_nets
        }
        zones = {
            net_name: extractor.get_zone_geometry(net_name)
            for net_name in reference_net_names
        }
        return cls(tracks_by_net=tracks, zones_by_net=zones)

    def get_net_tracks(self, net_name):
        return list(self.tracks_by_net.get(net_name, []))

    def get_zone_geometry(self, net_name):
        return dict(self.zones_by_net.get(net_name, {}))


class DifferentialImpedanceSolver:
    """Analyze routed pair sections using edge-coupled planar-line estimates."""

    def __init__(self, extractor, stackup, settings, log_callback=None):
        self.extractor = extractor
        self.stackup = stackup
        self.settings = settings
        self.log_callback = log_callback
        self.plane_analyzer = ReferencePlaneAnalyzer(
            extractor,
            stackup,
            reference_net_names=settings.reference_net_names,
        )

    def log(self, message):
        if self.log_callback:
            self.log_callback(message)

    @staticmethod
    def _direction(segment):
        dx = segment["end"][0] - segment["start"][0]
        dy = segment["end"][1] - segment["start"][1]
        length = math.hypot(dx, dy)
        return (dx / length, dy / length) if length > 0 else (0.0, 0.0)

    @staticmethod
    def _is_breakout_geometry(width_mm, gap_mm):
        """Return true when two traces are no longer a coupled pair body."""
        return gap_mm > max(0.30, 3.0 * width_mm)

    @classmethod
    def _parallel_score(cls, first, second):
        a = cls._direction(first)
        b = cls._direction(second)
        cosine = abs(a[0] * b[0] + a[1] * b[1])
        if cosine < 0.85:
            return None
        origin = first["start"]
        first_length = first["length_mm"]
        second_projection = [
            (point[0] - origin[0]) * a[0] + (point[1] - origin[1]) * a[1]
            for point in (second["start"], second["end"])
        ]
        overlap = max(
            0.0,
            min(first_length, max(second_projection)) - max(0.0, min(second_projection)),
        )
        if overlap <= 0.01:
            return None
        if LineString is not None:
            distance = LineString([first["start"], first["end"]]).distance(
                LineString([second["start"], second["end"]])
            )
        else:
            distance = min(
                math.dist(first["start"], second["start"]),
                math.dist(first["start"], second["end"]),
            )
        max_distance = max(3.0, 12.0 * max(first["width_mm"], second["width_mm"]))
        if distance > max_distance:
            return None
        return distance + (1.0 - cosine) * max_distance, overlap

    def _match_sections(self, positive_tracks, negative_tracks):
        sections = []
        used_negative = set()
        for positive in positive_tracks:
            candidates = []
            for index, negative in enumerate(negative_tracks):
                if index in used_negative or positive["layer_id"] != negative["layer_id"]:
                    continue
                scored = self._parallel_score(positive, negative)
                if scored is not None:
                    score, overlap = scored
                    candidates.append((score, index, negative, overlap))
            if not candidates:
                continue
            _, index, negative, overlap = min(candidates, key=lambda item: item[0])
            used_negative.add(index)
            if LineString is not None:
                center_distance = LineString([positive["start"], positive["end"]]).distance(
                    LineString([negative["start"], negative["end"]])
                )
            else:
                center_distance = math.dist(positive["start"], negative["start"])
            gap = center_distance - 0.5 * (
                positive["width_mm"] + negative["width_mm"]
            )
            sections.append((
                positive,
                negative,
                max(0.001, gap),
                overlap,
            ))
        return sections

    def _copper_layer(self, layer_id):
        return next((
            layer for layer in self.stackup.layers
            if layer.kind.upper() == "COPPER" and layer.layer_id == layer_id
        ), None)

    def _measured_coplanar_ground_gap(self, layer_id, positive, negative):
        if LineString is None:
            return None
        geometries = []
        for net_name in self.settings.reference_net_names:
            geometry = self.extractor.get_zone_geometry(net_name).get(layer_id)
            if geometry is not None and not geometry.is_empty:
                geometries.append(geometry)
        if not geometries:
            return None
        distances = []
        for segment in (positive, negative):
            route = LineString([segment["start"], segment["end"]])
            center_to_ground = min(route.distance(geometry) for geometry in geometries)
            distances.append(max(0.0, center_to_ground - 0.5 * segment["width_mm"]))
        return min(distances) if distances else None

    def _coplanar_ground_coverage(self, layer_id, positive, negative):
        """Measure whether same-layer GND follows both traces continuously.

        A nearest-distance test alone can accept a small, unrelated copper
        island.  Sampling both route centerlines prevents that false positive.
        """
        if LineString is None or Point is None or unary_union is None:
            return "", 0.0
        search_gap = max(
            0.10,
            1.75 * float(self.settings.coplanar_ground_gap_mm),
            float(self.settings.coplanar_ground_gap_mm) + 0.10,
        )
        best_net, best_coverage = "", 0.0
        for net_name in self.settings.reference_net_names:
            geometry = self.extractor.get_zone_geometry(net_name).get(layer_id)
            if geometry is None or geometry.is_empty:
                continue
            coverages = []
            for segment in (positive, negative):
                line = LineString([segment["start"], segment["end"]])
                samples = max(9, min(41, int(math.ceil(line.length / 0.25)) + 1))
                covered = 0
                for index in range(samples):
                    point = line.interpolate(index / max(samples - 1, 1), normalized=True)
                    edge_gap = max(0.0, point.distance(geometry) - 0.5 * segment["width_mm"])
                    covered += edge_gap <= search_gap
                coverages.append(100.0 * covered / samples)
            coverage = min(coverages) if coverages else 0.0
            if coverage > best_coverage:
                best_net, best_coverage = net_name, coverage
        return best_net, best_coverage

    def _mask_adjusted_epsilon(self, epsilon_r, height_mm):
        if not self.settings.include_solder_mask or height_mm <= 0:
            return epsilon_r
        ratio = min(0.25, self.settings.solder_mask_thickness_mm / height_mm)
        return epsilon_r + ratio * 0.15 * (
            self.settings.solder_mask_epsilon_r - 1.0
        )

    @staticmethod
    def _microstrip(width, gap, thickness, height, epsilon_r):
        if min(width, gap, height) <= 0:
            raise ValueError("Microstrip dimensions must be positive.")
        argument = 5.98 * height / max(0.8 * width + thickness, 1e-12)
        if argument <= 1.0:
            raise ValueError("Trace is too wide for the microstrip approximation.")
        z0 = 87.0 / math.sqrt(epsilon_r + 1.41) * math.log(argument)
        zdiff = 2.0 * z0 * (1.0 - 0.48 * math.exp(-0.96 * gap / height))
        return z0, zdiff

    @staticmethod
    def _stripline(width, gap, thickness, height_above, height_below, epsilon_r):
        if min(width, gap, height_above, height_below) <= 0:
            raise ValueError("Stripline dimensions must be positive.")
        plane_spacing = height_above + height_below + thickness
        argument = 4.0 * plane_spacing / max(
            0.67 * math.pi * (0.8 * width + thickness), 1e-12
        )
        if argument <= 1.0:
            raise ValueError("Trace is too wide for the stripline approximation.")
        z0 = 60.0 / math.sqrt(epsilon_r) * math.log(argument)
        effective_height = 2.0 * height_above * height_below / (
            height_above + height_below
        )
        zdiff = 2.0 * z0 * (1.0 - 0.347 * math.exp(-2.9 * gap / effective_height))
        return z0, zdiff

    def _solve_section(self, positive, negative, gap_mm, length_mm):
        layer_id = positive["layer_id"]
        copper = self._copper_layer(layer_id)
        layer_name = copper.name if copper else str(layer_id)
        thickness = copper.thickness_mm if copper else 0.035
        width = 0.5 * (positive["width_mm"] + negative["width_mm"])
        context = self.plane_analyzer.analyze(
            layer_id, positive, negative, gap_mm
        )
        warnings = list(context.warnings)
        z0 = zdiff = 0.0
        reference_distance = 0.0
        reference_epsilon = 4.4
        effective_epsilon = 0.0
        requested_geometry = normalize_geometry(self.settings.geometry_mode)
        solved_topology = context.topology
        ground_clearance = 0.0
        reported_coverage = context.coverage_pct
        coplanar_without_backing = False
        if self._is_breakout_geometry(width, gap_mm):
            if requested_geometry in COPLANAR_GEOMETRIES:
                measured_ground_gap = self._measured_coplanar_ground_gap(
                    layer_id, positive, negative
                )
                ground_clearance = measured_ground_gap or max(
                    0.001, float(self.settings.coplanar_ground_gap_mm)
                )
            warnings = [
                warning for warning in warnings
                if "continuous adjacent ground plane" not in warning
                and "plane above covers only" not in warning
                and "plane below covers only" not in warning
            ]
            warnings.append(
                "Trace separation exceeds the coupled-pair limit; classified as a "
                "connector/breakout transition and excluded from impedance and GND-plane qualification."
            )
            return DifferentialSectionResult(
                layer_id=layer_id, layer_name=layer_name, length_mm=length_mm,
                width_mm=width, gap_mm=gap_mm, topology="BREAKOUT_TRANSITION",
                geometry_mode=requested_geometry, ground_clearance_mm=ground_clearance,
                reference_above=context.reference_above,
                reference_below=context.reference_below,
                reference_coverage_pct=context.coverage_pct,
                copper_thickness_mm=thickness,
                reference_above_distance_mm=context.distance_above_mm,
                reference_below_distance_mm=context.distance_below_mm,
                reference_above_epsilon_r=context.epsilon_r_above,
                reference_below_epsilon_r=context.epsilon_r_below,
                trustworthy=False, warnings=warnings,
            )
        try:
            if requested_geometry in COPLANAR_GEOMETRIES:
                configured_ground_gap = max(
                    0.001, float(self.settings.coplanar_ground_gap_mm)
                )
                measured_ground_gap = self._measured_coplanar_ground_gap(layer_id, positive, negative)
                coplanar_net, coplanar_coverage = self._coplanar_ground_coverage(
                    layer_id, positive, negative
                )
                if context.topology == "UNREFERENCED" and (
                    measured_ground_gap is None or coplanar_coverage < 90.0
                ):
                    warnings.append(
                        "Neither a continuous adjacent plane nor continuous same-layer GND was found."
                    )
                else:
                    valid_above = bool(context.reference_above and context.coverage_above_pct >= 90.0)
                    valid_below = bool(context.reference_below and context.coverage_below_pct >= 90.0)
                    if valid_above and valid_below:
                        height_below, epsilon_below = context.distance_below_mm, context.epsilon_r_below
                        height_above, epsilon_above = context.distance_above_mm, context.epsilon_r_above
                        solved_topology = "COPLANAR_STRIPLINE"
                    elif valid_below:
                        height_below, epsilon_below = context.distance_below_mm, context.epsilon_r_below
                        height_above, epsilon_above = 0.0, epsilon_below
                        solved_topology = "COPLANAR_MICROSTRIP"
                    else:
                        distances = [
                            (context.distance_above_mm, context.epsilon_r_above),
                            (context.distance_below_mm, context.epsilon_r_below),
                        ]
                        height_below, epsilon_below = next(
                            ((distance, epsilon) for distance, epsilon in distances if distance > 0.0),
                            (0.0, 4.4),
                        )
                        height_above, epsilon_above = 0.0, epsilon_below
                        coplanar_without_backing = context.topology == "UNREFERENCED"
                        solved_topology = (
                            "COPLANAR_WAVEGUIDE" if coplanar_without_backing
                            else "COPLANAR_MICROSTRIP"
                        )
                    if measured_ground_gap is None:
                        ground_clearance = configured_ground_gap
                        warnings.append("No same-layer ground pour was detected; the configured coplanar ground gap is used as design intent.")
                    else:
                        ground_clearance = measured_ground_gap
                    if measured_ground_gap is not None and abs(measured_ground_gap - configured_ground_gap) > max(
                        0.02, 0.2 * configured_ground_gap
                    ):
                        warnings.append(
                            f"Measured same-layer GND gap is about {measured_ground_gap:.3f} mm, "
                            f"not the configured {configured_ground_gap:.3f} mm; the measured gap is used."
                        )
                    coplanar = GroundedCoplanarDifferentialSolver.solve(
                        width, gap_mm, ground_clearance, height_below, epsilon_below,
                        copper_thickness_mm=thickness, height_above_mm=height_above,
                        epsilon_above=epsilon_above,
                        include_solder_mask=self.settings.include_solder_mask,
                        solder_mask_thickness_mm=self.settings.solder_mask_thickness_mm,
                        solder_mask_epsilon_r=self.settings.solder_mask_epsilon_r,
                        backing_plane=not coplanar_without_backing,
                    )
                    z0, zdiff = coplanar.odd_mode_impedance_ohm, coplanar.differential_impedance_ohm
                    effective_epsilon = coplanar.effective_epsilon_r
                    reference_distance = min(value for value in (height_below, height_above) if value > 0.0)
                    reference_epsilon = epsilon_below
                    if coplanar_without_backing:
                        reported_coverage = coplanar_coverage
                        warnings = [
                            warning for warning in warnings
                            if "adjacent ground plane" not in warning
                            and "plane above covers only" not in warning
                            and "plane below covers only" not in warning
                        ]
                        warnings.append(
                            f"No continuous opposite-layer plane; solved as unbacked CPW using "
                            f"{coplanar_net} on the signal layer ({coplanar_coverage:.1f}% coverage)."
                        )
                    warnings.append("Coplanar impedance uses a quasi-static 2-D cross-section; validate final production geometry with JLCPCB impedance control.")
            elif requested_geometry == GEOMETRY_MICROSTRIP and context.topology not in {"MICROSTRIP", "EMBEDDED_MICROSTRIP"}:
                warnings.append("The selected microstrip geometry is incompatible with this signal layer.")
            elif requested_geometry == GEOMETRY_STRIPLINE and context.topology not in {"STRIPLINE", "ASYMMETRIC_STRIPLINE"}:
                warnings.append("The selected stripline geometry is incompatible with this signal layer.")
            elif context.topology in {"MICROSTRIP", "EMBEDDED_MICROSTRIP"}:
                if context.reference_above and context.coverage_above_pct >= 90.0:
                    height = context.distance_above_mm
                    epsilon = context.epsilon_r_above
                else:
                    height = context.distance_below_mm
                    epsilon = context.epsilon_r_below
                reference_distance = height
                if context.topology == "MICROSTRIP":
                    epsilon = self._mask_adjusted_epsilon(epsilon, height)
                reference_epsilon = epsilon
                try:
                    solved = EdgeCoupledDifferentialSolver.solve_microstrip(
                        width, gap_mm, thickness, height, epsilon,
                        include_solder_mask=self.settings.include_solder_mask,
                        solder_mask_thickness_mm=self.settings.solder_mask_thickness_mm,
                        solder_mask_epsilon_r=self.settings.solder_mask_epsilon_r,
                    )
                    z0 = solved.odd_mode_impedance_ohm
                    zdiff = solved.differential_impedance_ohm
                    effective_epsilon = solved.effective_epsilon_r
                except ValueError:
                    z0, zdiff = self._microstrip(width, gap_mm, thickness, height, epsilon)
                    warnings.append(
                        "2-D field solve failed for this geometry; falling back to the "
                        "IPC-D-317A closed-form approximation (±10-25% typical error)."
                    )
            elif context.topology in {"STRIPLINE", "ASYMMETRIC_STRIPLINE"}:
                total = context.distance_above_mm + context.distance_below_mm
                epsilon = (
                    context.epsilon_r_above * context.distance_above_mm
                    + context.epsilon_r_below * context.distance_below_mm
                ) / max(total, 1e-12)
                reference_distance = min(context.distance_above_mm, context.distance_below_mm)
                reference_epsilon = epsilon
                try:
                    solved = EdgeCoupledDifferentialSolver.solve_stripline(
                        width, gap_mm, thickness,
                        context.distance_above_mm, context.epsilon_r_above,
                        context.distance_below_mm, context.epsilon_r_below,
                    )
                    z0 = solved.odd_mode_impedance_ohm
                    zdiff = solved.differential_impedance_ohm
                    effective_epsilon = solved.effective_epsilon_r
                except ValueError:
                    z0, zdiff = self._stripline(
                        width, gap_mm, thickness,
                        context.distance_above_mm, context.distance_below_mm, epsilon,
                    )
                    warnings.append(
                        "2-D field solve failed for this geometry; falling back to the "
                        "IPC-D-317A closed-form approximation (±10-25% typical error)."
                    )
            else:
                warnings.append("Impedance was not calculated without a reference plane.")
        except ValueError as exc:
            warnings.append(str(exc))

        return DifferentialSectionResult(
            layer_id=layer_id,
            layer_name=layer_name,
            length_mm=length_mm,
            width_mm=width,
            gap_mm=gap_mm,
            topology=solved_topology,
            geometry_mode=requested_geometry,
            ground_clearance_mm=ground_clearance,
            reference_above=context.reference_above,
            reference_below=context.reference_below,
            reference_coverage_pct=reported_coverage,
            single_ended_impedance_ohm=z0,
            differential_impedance_ohm=zdiff,
            copper_thickness_mm=thickness,
            reference_distance_mm=reference_distance,
            reference_above_distance_mm=context.distance_above_mm,
            reference_below_distance_mm=context.distance_below_mm,
            reference_above_epsilon_r=context.epsilon_r_above,
            reference_below_epsilon_r=context.epsilon_r_below,
            reference_epsilon_r=reference_epsilon,
            effective_epsilon_r=effective_epsilon,
            two_d_impedance_ohm=zdiff,
            three_d_impedance_ohm=zdiff,
            trustworthy=bool((context.trustworthy or coplanar_without_backing) and zdiff > 0),
            warnings=warnings,
        )

    def solve_pair(self, pair):
        positive_tracks = self.extractor.get_net_tracks(pair.positive_net)
        negative_tracks = self.extractor.get_net_tracks(pair.negative_net)
        result = DifferentialPairResult(pair=pair)
        matches = self._match_sections(positive_tracks, negative_tracks)
        for positive, negative, gap, length in matches:
            result.sections.append(self._solve_section(
                positive, negative, gap, length
            ))
        self._refine_problem_sections(result)
        epsilon_samples = [
            (section.effective_epsilon_r, max(section.length_mm, 1e-12))
            for section in result.sections
            if section.effective_epsilon_r > 0.0
        ]
        if epsilon_samples:
            epsilon_effective = sum(value * weight for value, weight in epsilon_samples) / sum(
                weight for _value, weight in epsilon_samples
            )
        else:
            dielectric_values = [
                layer.epsilon_r for layer in self.stackup.layers
                if layer.kind.upper() == "DIELECTRIC" and layer.epsilon_r > 0.0
            ]
            epsilon_effective = (
                sum(dielectric_values) / len(dielectric_values)
                if dielectric_values else 4.4
            )
        symmetry = assess_length_symmetry(
            positive_tracks, negative_tracks, pair.interface, epsilon_effective,
        )
        result.positive_length_mm = symmetry.positive_length_mm
        result.negative_length_mm = symmetry.negative_length_mm
        result.length_mismatch_mm = symmetry.mismatch_mm
        result.length_mismatch_pct = symmetry.mismatch_pct
        result.estimated_skew_ps = symmetry.estimated_skew_ps
        result.skew_limit_ps = symmetry.skew_limit_ps
        result.maximum_length_mismatch_mm = symmetry.maximum_mismatch_mm
        result.skew_margin_ps = symmetry.margin_ps
        result.length_symmetry_status = symmetry.status
        result.shorter_net = {
            "POSITIVE": pair.positive_net,
            "NEGATIVE": pair.negative_net,
        }.get(symmetry.shorter_polarity, "")
        if symmetry.status in {"MARGINAL", "FAIL"}:
            label = "marginal" if symmetry.status == "MARGINAL" else "failed"
            shorter = f"; {result.shorter_net} is shorter" if result.shorter_net else ""
            result.warnings.append(
                f"Length symmetry {label}: mismatch={symmetry.mismatch_mm:.3f} mm "
                f"({symmetry.estimated_skew_ps:.1f} ps), {pair.interface or 'GENERIC'} "
                f"limit={symmetry.skew_limit_ps:g} ps{shorter}."
            )
        for section in result.sections:
            if (
                section.differential_impedance_ohm > 0
                and section.length_mm + 1e-12 < section.width_mm
            ):
                section.warnings.append(
                    "Section is shorter than one trace width; treated as a localized "
                    "discontinuity and excluded from aggregate impedance/min-max."
                )
        solved = [
            section for section in result.sections
            if section.differential_impedance_ohm > 0
            and section.length_mm + 1e-12 >= section.width_mm
        ]
        unresolved = [
            section for section in result.sections
            if section.differential_impedance_ohm <= 0
            and section.topology != "BREAKOUT_TRANSITION"
        ]
        for section in result.sections:
            for warning in section.warnings:
                if warning not in result.warnings:
                    result.warnings.append(warning)
        if not solved:
            result.status = "NO_DATA"
            if not positive_tracks or not negative_tracks:
                result.warnings.append("One or both nets have no routed track segments.")
            else:
                result.warnings.append("No same-layer parallel route sections could be paired.")
            return result

        total_length = sum(max(section.length_mm, 1e-12) for section in solved)
        result.weighted_impedance_ohm = sum(
            section.differential_impedance_ohm * max(section.length_mm, 1e-12)
            for section in solved
        ) / total_length
        values = [section.differential_impedance_ohm for section in solved]
        result.minimum_impedance_ohm = min(values)
        result.maximum_impedance_ohm = max(values)
        target = max(pair.target_impedance_ohm, 1e-12)
        result.error_pct = 100.0 * (result.weighted_impedance_ohm - target) / target
        result.trustworthy = bool(
            self.stackup.trustworthy
            and not unresolved
            and all(section.trustworthy for section in result.sections)
        )
        if unresolved:
            result.status = "FAIL"
            result.warnings.append(
                f"{len(unresolved)} routed section(s) have no continuous reference plane; "
                "the solved-section average cannot qualify the complete pair."
            )
        elif result.length_symmetry_status == "FAIL":
            result.status = "FAIL"
        elif not result.trustworthy:
            result.status = "ESTIMATE"
        elif abs(result.error_pct) <= self.settings.target_tolerance_pct:
            result.status = "PASS"
        else:
            result.status = "FAIL"
        return result

    def _refine_problem_sections(self, result):
        """Apply bounded 3-D refinement only to the highest-risk sections."""
        for section in result.sections:
            section.two_d_impedance_ohm = section.differential_impedance_ohm
            section.three_d_impedance_ohm = section.differential_impedance_ohm
            section.refinement_status = (
                "NOT_APPLICABLE" if section.topology == "BREAKOUT_TRANSITION"
                else "2D_BASELINE"
            )
        if not self.settings.enable_targeted_3d_refinement:
            return
        refiner = Targeted3DRefiner(self.settings)
        candidates = [
            section for section in result.sections
            if section.topology != "BREAKOUT_TRANSITION"
            and refiner.select(section, result.pair.target_impedance_ohm)
        ]
        candidates.sort(key=lambda item: (
            item.reference_coverage_pct,
            -abs(item.differential_impedance_ohm - result.pair.target_impedance_ohm),
            -item.length_mm,
        ))
        for section in candidates[:self.settings.targeted_3d_max_sections]:
            refined = refiner.refine(section, result.pair.target_impedance_ohm)
            if refined is None:
                continue
            section.refinement_status = refined.status
            section.refinement_reason = refined.reason
            section.three_d_impedance_ohm = refined.impedance_ohm
            if refined.impedance_ohm > 0:
                section.differential_impedance_ohm = refined.impedance_ohm
            section.warnings.append(
                f"Targeted 3-D quasi-static refinement: {refined.reason}."
            )

    def solve(self, pairs, progress_callback=None):
        enabled = [pair for pair in pairs if pair.enabled]
        results = []
        for index, pair in enumerate(enabled, start=1):
            results.append(self.solve_pair(pair))
            if progress_callback:
                progress_callback(index, len(enabled), pair.name)
        self.log(f"Differential impedance analysis complete for {len(results)} pairs.")
        return results
