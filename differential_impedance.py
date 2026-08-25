"""Stackup-aware quasi-static differential transmission-line analysis."""

import math

try:
    from shapely.geometry import LineString
except ImportError:
    LineString = None

try:
    from .models import DifferentialPairResult, DifferentialSectionResult
    from .reference_plane_analyzer import ReferencePlaneAnalyzer
except (ImportError, ValueError):
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
    """Analyze routed pair sections using coupled microstrip/stripline estimates."""

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
        try:
            if context.topology in {"MICROSTRIP", "EMBEDDED_MICROSTRIP"}:
                if context.reference_above and context.coverage_above_pct >= 90.0:
                    height = context.distance_above_mm
                    epsilon = context.epsilon_r_above
                else:
                    height = context.distance_below_mm
                    epsilon = context.epsilon_r_below
                if context.topology == "MICROSTRIP":
                    epsilon = self._mask_adjusted_epsilon(epsilon, height)
                z0, zdiff = self._microstrip(width, gap_mm, thickness, height, epsilon)
            elif context.topology in {"STRIPLINE", "ASYMMETRIC_STRIPLINE"}:
                total = context.distance_above_mm + context.distance_below_mm
                epsilon = (
                    context.epsilon_r_above * context.distance_above_mm
                    + context.epsilon_r_below * context.distance_below_mm
                ) / max(total, 1e-12)
                z0, zdiff = self._stripline(
                    width, gap_mm, thickness,
                    context.distance_above_mm, context.distance_below_mm, epsilon,
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
            topology=context.topology,
            reference_above=context.reference_above,
            reference_below=context.reference_below,
            reference_coverage_pct=context.coverage_pct,
            single_ended_impedance_ohm=z0,
            differential_impedance_ohm=zdiff,
            trustworthy=bool(context.trustworthy and zdiff > 0),
            warnings=warnings,
        )

    def solve_pair(self, pair):
        positive_tracks = self.extractor.get_net_tracks(pair.positive_net)
        negative_tracks = self.extractor.get_net_tracks(pair.negative_net)
        result = DifferentialPairResult(pair=pair)
        result.length_mismatch_mm = abs(
            sum(track["length_mm"] for track in positive_tracks)
            - sum(track["length_mm"] for track in negative_tracks)
        )
        matches = self._match_sections(positive_tracks, negative_tracks)
        for positive, negative, gap, length in matches:
            result.sections.append(self._solve_section(
                positive, negative, gap, length
            ))
        solved = [
            section for section in result.sections
            if section.differential_impedance_ohm > 0
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
            self.stackup.trustworthy and all(section.trustworthy for section in solved)
        )
        if not result.trustworthy:
            result.status = "ESTIMATE"
        elif abs(result.error_pct) <= self.settings.target_tolerance_pct:
            result.status = "PASS"
        else:
            result.status = "FAIL"
        return result

    def solve(self, pairs, progress_callback=None):
        enabled = [pair for pair in pairs if pair.enabled]
        results = []
        for index, pair in enumerate(enabled, start=1):
            results.append(self.solve_pair(pair))
            if progress_callback:
                progress_callback(index, len(enabled), pair.name)
        self.log(f"Differential impedance analysis complete for {len(results)} pairs.")
        return results
