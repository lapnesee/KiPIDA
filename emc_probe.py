"""Click-probe metadata for rendered EMI/EMC result plots."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class EMCProbeReading:
    """User-facing details associated with one plotted EMC observation."""

    title: str
    severity: str
    description: str
    recommendation: str
    rule_id: str = ""
    confidence: str = ""
    nets: tuple = ()
    components: tuple = ()
    evidence: str = ""

    def label(self):
        heading = self.title
        qualifiers = []
        if self.rule_id:
            qualifiers.append(f"Rule: {self.rule_id}")
        if self.severity:
            qualifiers.append(f"Severity: {self.severity}")
        if self.confidence:
            qualifiers.append(f"Confidence: {self.confidence}")
        lines = [heading]
        if qualifiers:
            lines.append(" | ".join(qualifiers))
        if self.description:
            lines.extend(("", f"Observation: {self.description}"))
        targets = []
        if self.nets:
            targets.append("Nets: " + ", ".join(self.nets))
        if self.components:
            targets.append("Components: " + ", ".join(self.components))
        if targets:
            lines.append("; ".join(targets))
        if self.evidence:
            lines.append(f"Evidence: {self.evidence}")
        if self.recommendation:
            lines.extend(("", f"Recommendation: {self.recommendation}"))
        return "\n".join(lines)

    def to_dict(self):
        return {
            "title": self.title, "severity": self.severity,
            "description": self.description, "recommendation": self.recommendation,
            "rule_id": self.rule_id, "confidence": self.confidence,
            "nets": list(self.nets), "components": list(self.components),
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, value):
        return cls(
            title=str(value.get("title", "EMI/EMC observation")),
            severity=str(value.get("severity", "INFO")),
            description=str(value.get("description", "")),
            recommendation=str(value.get("recommendation", "")),
            rule_id=str(value.get("rule_id", "")),
            confidence=str(value.get("confidence", "")),
            nets=tuple(str(item) for item in value.get("nets", [])),
            components=tuple(str(item) for item in value.get("components", [])),
            evidence=str(value.get("evidence", "")),
        )


class RenderedPointProbe:
    """Resolve bitmap clicks against points captured in normalized figure space."""

    def __init__(self, points, maximum_distance_px=18.0):
        self.points = tuple(points)
        self.maximum_distance_px = float(maximum_distance_px)

    def sample(self, pixel_x, pixel_y, bitmap_width, bitmap_height):
        if not self.points or bitmap_width <= 0 or bitmap_height <= 0:
            return None
        nearest = None
        nearest_distance = self.maximum_distance_px
        for normalized_x, normalized_y, reading in self.points:
            distance = math.hypot(
                pixel_x - normalized_x * bitmap_width,
                pixel_y - normalized_y * bitmap_height,
            )
            if distance <= nearest_distance:
                nearest = reading
                nearest_distance = distance
        return nearest

    def to_dict(self):
        return {
            "maximum_distance_px": self.maximum_distance_px,
            "points": [
                [normalized_x, normalized_y, reading.to_dict()]
                for normalized_x, normalized_y, reading in self.points
            ],
        }

    @classmethod
    def from_dict(cls, value):
        points = []
        for item in value.get("points", []):
            if not isinstance(item, (list, tuple)) or len(item) != 3:
                continue
            points.append((float(item[0]), float(item[1]), EMCProbeReading.from_dict(item[2])))
        return cls(points, maximum_distance_px=float(value.get("maximum_distance_px", 18.0)))


def capture_axis_points(figure, axis, data_points, maximum_distance_px=18.0):
    """Capture data-space points after Matplotlib has finalized axis transforms."""
    # Plotter exports PNGs at 160 dpi.  Match that render density before
    # capturing coordinates so click targets align pixel-for-pixel.
    figure.set_dpi(160)
    figure.canvas.draw()
    width, height = figure.canvas.get_width_height()
    rendered = []
    for x_value, y_value, reading in data_points:
        display_x, display_y = axis.transData.transform((x_value, y_value))
        if math.isfinite(display_x) and math.isfinite(display_y):
            rendered.append((display_x / width, 1.0 - display_y / height, reading))
    return RenderedPointProbe(rendered, maximum_distance_px=maximum_distance_px)
