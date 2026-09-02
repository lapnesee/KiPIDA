"""Current-density post-processing for solved DC mesh branches.

The solver is the source of branch current.  This module only converts those
currents to planar copper-face and plated-barrel current densities; it never
reconstructs current from a voltage gradient and has no wxPython dependency.
"""

from dataclasses import dataclass, field
import math
from typing import Dict, List, Optional

try:
    from shapely.geometry import Point
    from shapely.prepared import prep
except ImportError:  # pragma: no cover - the mesher already requires Shapely
    Point = prep = None


@dataclass(frozen=True)
class PlanarCurrentDensitySample:
    branch_index: int
    x_mm: float
    y_mm: float
    layer_id: int
    layer_name: str
    current_a: float
    density_a_per_mm2: float
    copper_kind: str
    loss_w: float = 0.0


@dataclass(frozen=True)
class VerticalCurrentDensitySample:
    branch_index: int
    x_mm: float
    y_mm: float
    start_layer_id: int
    end_layer_id: int
    current_a: float
    density_a_per_mm2: float
    kind: str
    cross_section_mm2: float
    geometry_source: str


@dataclass
class DCCurrentDensityResult:
    planar_samples: List[PlanarCurrentDensitySample] = field(default_factory=list)
    vertical_samples: List[VerticalCurrentDensitySample] = field(default_factory=list)
    node_density_a_per_mm2: Dict[int, float] = field(default_factory=dict)
    maximum_planar_a_per_mm2: float = 0.0
    percentile_99_5_a_per_mm2: float = 0.0
    maximum_track_a_per_mm2: float = 0.0
    maximum_zone_a_per_mm2: float = 0.0
    maximum_via_current_a: float = 0.0
    maximum_via_a_per_mm2: float = 0.0
    planar_hotspot: Optional[PlanarCurrentDensitySample] = None
    warnings: List[str] = field(default_factory=list)
    confidence: str = "ESTIMATED"


def _percentile(values, percentile):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * float(percentile) / 100.0
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _prepared_layers(geometry_by_layer):
    if prep is None:
        return {}
    return {
        int(layer_id): prep(geometry)
        for layer_id, geometry in (geometry_by_layer or {}).items()
        if geometry is not None and not geometry.is_empty
    }


def calculate_current_density(mesh, detailed_result, stackup, classification_geometry=None):
    """Convert exact solved branch currents to A/mm² samples.

    Planar branches use ``copper thickness * effective mesh step``.  Via and
    plated-through-hole branches use the barrel wall section captured by the
    mesher.  Classification is geometric and non-exclusive at route/zone
    overlaps, which are reported as ``TRACK+ZONE``.
    """
    result = DCCurrentDensityResult()
    branches = list(getattr(mesh, "branches", ()) or ())
    currents = list(getattr(detailed_result, "branch_currents_a", ()) or ())
    losses = list(getattr(detailed_result, "branch_losses_w", ()) or ())
    if len(currents) != len(branches):
        result.warnings.append(
            f"Branch-current count mismatch ({len(currents)} currents for {len(branches)} branches)."
        )
    stackup = stackup or {}
    if not stackup.get("trustworthy", False):
        result.warnings.append(
            "Copper thickness is not verified; current-density values are estimates."
        )
    for warning in stackup.get("warnings", ()) or ():
        if warning not in result.warnings:
            result.warnings.append(str(warning))

    classification_geometry = classification_geometry or {}
    tracks = _prepared_layers(classification_geometry.get("track", {}))
    zones = _prepared_layers(classification_geometry.get("zone", {}))
    node_density = {}
    invalid_thickness_layers = set()
    estimated_barrels = False

    for index, (branch, current) in enumerate(zip(branches, currents)):
        coord_a = mesh.node_coords.get(branch.node_a)
        coord_b = mesh.node_coords.get(branch.node_b)
        if coord_a is None or coord_b is None:
            continue
        current = float(current)
        x_mm = (float(coord_a[0]) + float(coord_b[0])) / 2.0
        y_mm = (float(coord_a[1]) + float(coord_b[1])) / 2.0
        kind = str(getattr(branch, "kind", "lateral") or "lateral").lower()
        if kind == "lateral":
            layer_id = int(coord_a[2])
            copper = stackup.get("copper", {}).get(layer_id, {})
            thickness_mm = float(copper.get("thickness_mm", 0.0) or 0.0)
            face_width_mm = float(getattr(mesh, "grid_step", 0.0) or 0.0)
            if thickness_mm <= 0.0 or face_width_mm <= 0.0:
                invalid_thickness_layers.add(layer_id)
                continue
            density = abs(current) / (thickness_mm * face_width_mm)
            point = Point(x_mm, y_mm) if Point is not None else None
            on_track = bool(
                point is not None and layer_id in tracks
                and tracks[layer_id].intersects(point)
            )
            in_zone = bool(
                point is not None and layer_id in zones
                and zones[layer_id].intersects(point)
            )
            copper_kind = (
                "TRACK+ZONE" if on_track and in_zone else
                "TRACK" if on_track else "ZONE" if in_zone else "PAD_OR_OTHER"
            )
            sample = PlanarCurrentDensitySample(
                index, x_mm, y_mm, layer_id,
                str(copper.get("name", layer_id)), current, density, copper_kind,
                float(losses[index]) if index < len(losses) else 0.0,
            )
            result.planar_samples.append(sample)
            # The displayed node map is the maximum of its solved adjacent
            # copper faces.  A vector reconstruction would create a false
            # sqrt(2) peak at a simple 90-degree bend.
            for node_id in (branch.node_a, branch.node_b):
                node_density[node_id] = max(node_density.get(node_id, 0.0), density)
        elif kind in ("via", "pth"):
            section = float(getattr(branch, "cross_section_mm2", 0.0) or 0.0)
            if section <= 0.0:
                result.warnings.append(
                    f"Vertical branch {index} has no usable barrel cross-section."
                )
                continue
            density = abs(current) / section
            source = str(getattr(branch, "geometry_source", "") or "UNKNOWN")
            estimated_barrels = estimated_barrels or "ESTIMATED" in source
            result.vertical_samples.append(VerticalCurrentDensitySample(
                index, x_mm, y_mm, int(coord_a[2]), int(coord_b[2]), current,
                density, kind.upper(), section, source,
            ))

    if invalid_thickness_layers:
        result.warnings.append(
            "Missing or invalid copper thickness on layer(s): "
            + ", ".join(str(layer) for layer in sorted(invalid_thickness_layers)) + "."
        )
    if estimated_barrels:
        result.warnings.append(
            "At least one vertical barrel section uses an explicit drill or plating-thickness estimate."
        )
    result.node_density_a_per_mm2 = node_density
    densities = [sample.density_a_per_mm2 for sample in result.planar_samples]
    if densities:
        result.planar_hotspot = max(
            result.planar_samples, key=lambda sample: sample.density_a_per_mm2,
        )
        result.maximum_planar_a_per_mm2 = result.planar_hotspot.density_a_per_mm2
        result.percentile_99_5_a_per_mm2 = _percentile(densities, 99.5)
        result.maximum_track_a_per_mm2 = max(
            (
                sample.density_a_per_mm2 for sample in result.planar_samples
                if "TRACK" in sample.copper_kind
            ),
            default=0.0,
        )
        result.maximum_zone_a_per_mm2 = max(
            (
                sample.density_a_per_mm2 for sample in result.planar_samples
                if "ZONE" in sample.copper_kind
            ),
            default=0.0,
        )
    via_samples = [sample for sample in result.vertical_samples if sample.kind == "VIA"]
    result.maximum_via_current_a = max(
        (abs(sample.current_a) for sample in via_samples), default=0.0,
    )
    result.maximum_via_a_per_mm2 = max(
        (sample.density_a_per_mm2 for sample in via_samples), default=0.0,
    )
    return result
