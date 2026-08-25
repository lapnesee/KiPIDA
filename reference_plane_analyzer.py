"""Local adjacent-reference-plane analysis for differential routes."""

from dataclasses import dataclass, field

try:
    from shapely.geometry import LineString
    from shapely.ops import unary_union
except ImportError:
    LineString = unary_union = None


@dataclass
class ReferencePlaneContext:
    topology: str = "UNREFERENCED"
    reference_above: str = ""
    reference_below: str = ""
    layer_above_id: int = -1
    layer_below_id: int = -1
    distance_above_mm: float = 0.0
    distance_below_mm: float = 0.0
    epsilon_r_above: float = 4.4
    epsilon_r_below: float = 4.4
    coverage_above_pct: float = 0.0
    coverage_below_pct: float = 0.0
    trustworthy: bool = False
    warnings: list = field(default_factory=list)

    @property
    def coverage_pct(self):
        values = []
        if self.reference_above:
            values.append(self.coverage_above_pct)
        if self.reference_below:
            values.append(self.coverage_below_pct)
        return min(values) if values else 0.0


class ReferencePlaneAnalyzer:
    """Inspect filled GND polygons on physically adjacent copper layers."""

    def __init__(self, extractor, stackup, reference_net_names=None, coverage_threshold_pct=90.0):
        self.extractor = extractor
        self.stackup = stackup
        self.reference_net_names = list(reference_net_names or ["GND", "AGND", "DGND", "PGND"])
        self.coverage_threshold_pct = float(coverage_threshold_pct)
        self._zone_cache = {}

    def _copper_positions(self):
        return [
            (index, layer) for index, layer in enumerate(self.stackup.layers)
            if layer.kind.upper() == "COPPER" and layer.layer_id is not None
        ]

    def _adjacent_copper(self, layer_id):
        positions = self._copper_positions()
        current = next(((index, layer) for index, layer in positions if layer.layer_id == layer_id), None)
        if current is None:
            return None, None
        current_index = current[0]
        above = max((item for item in positions if item[0] < current_index), default=None, key=lambda x: x[0])
        below = min((item for item in positions if item[0] > current_index), default=None, key=lambda x: x[0])
        return above, below

    def _dielectric_between(self, index_a, index_b):
        low, high = sorted((index_a, index_b))
        layers = self.stackup.layers[low + 1:high]
        dielectrics = [layer for layer in layers if layer.kind.upper() != "COPPER"]
        distance = sum(max(0.0, layer.thickness_mm) for layer in dielectrics)
        if distance <= 0:
            return 0.0, 4.4
        epsilon = sum(
            max(1.0, layer.epsilon_r) * max(0.0, layer.thickness_mm)
            for layer in dielectrics
        ) / distance
        return distance, epsilon

    def _zones_for(self, net_name):
        if net_name not in self._zone_cache:
            self._zone_cache[net_name] = self.extractor.get_zone_geometry(net_name)
        return self._zone_cache[net_name]

    def _coverage(self, layer_id, corridor):
        if corridor is None or corridor.is_empty or corridor.area <= 0:
            return "", 0.0
        best_net = ""
        best_coverage = 0.0
        for net_name in self.reference_net_names:
            geometry = self._zones_for(net_name).get(layer_id)
            if geometry is None or geometry.is_empty:
                continue
            coverage = 100.0 * geometry.intersection(corridor).area / corridor.area
            if coverage > best_coverage:
                best_net = net_name
                best_coverage = coverage
        return best_net, min(100.0, best_coverage)

    def analyze(self, layer_id, positive_segment, negative_segment, gap_mm):
        context = ReferencePlaneContext()
        above, below = self._adjacent_copper(layer_id)
        if LineString is None:
            context.warnings.append("Shapely unavailable; reference-plane coverage cannot be checked.")
            return context

        route_lines = [
            LineString([positive_segment["start"], positive_segment["end"]]),
            LineString([negative_segment["start"], negative_segment["end"]]),
        ]
        route = unary_union(route_lines)
        corridor_width = max(
            0.1,
            positive_segment["width_mm"] + negative_segment["width_mm"] + gap_mm,
        )
        corridor = route.buffer(corridor_width, cap_style=2)

        signal_index = next((
            index for index, layer in self._copper_positions() if layer.layer_id == layer_id
        ), None)
        if signal_index is None:
            context.warnings.append(f"Signal layer {layer_id} is absent from the stackup.")
            return context

        if above is not None:
            context.layer_above_id = above[1].layer_id
            context.distance_above_mm, context.epsilon_r_above = self._dielectric_between(
                signal_index, above[0]
            )
            context.reference_above, context.coverage_above_pct = self._coverage(
                above[1].layer_id, corridor
            )
        if below is not None:
            context.layer_below_id = below[1].layer_id
            context.distance_below_mm, context.epsilon_r_below = self._dielectric_between(
                signal_index, below[0]
            )
            context.reference_below, context.coverage_below_pct = self._coverage(
                below[1].layer_id, corridor
            )

        valid_above = bool(context.reference_above) and context.coverage_above_pct >= self.coverage_threshold_pct
        valid_below = bool(context.reference_below) and context.coverage_below_pct >= self.coverage_threshold_pct
        if valid_above and valid_below:
            asymmetry = abs(context.distance_above_mm - context.distance_below_mm)
            denominator = max(context.distance_above_mm, context.distance_below_mm, 1e-12)
            context.topology = "STRIPLINE" if asymmetry / denominator < 0.1 else "ASYMMETRIC_STRIPLINE"
        elif valid_above or valid_below:
            outer = above is None or below is None
            context.topology = "MICROSTRIP" if outer else "EMBEDDED_MICROSTRIP"
        else:
            context.topology = "UNREFERENCED"

        for side, net_name, coverage in (
            ("above", context.reference_above, context.coverage_above_pct),
            ("below", context.reference_below, context.coverage_below_pct),
        ):
            if net_name and coverage < self.coverage_threshold_pct:
                context.warnings.append(
                    f"{net_name} plane {side} covers only {coverage:.1f}% of the route corridor."
                )
        if context.topology == "UNREFERENCED":
            context.warnings.append("No continuous adjacent ground plane was found.")
        context.trustworthy = bool(
            self.stackup.trustworthy and context.topology != "UNREFERENCED"
        )
        if not self.stackup.trustworthy:
            context.warnings.append("Stackup source is not trusted; impedance is indicative only.")
        return context
