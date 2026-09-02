"""Build board-level thermal models from KiCad geometry and power-tree data."""

from dataclasses import dataclass, field, replace
from collections import defaultdict
import hashlib
import time
from typing import Dict, List, Optional, Tuple

try:
    from shapely.geometry import Polygon, box
    from shapely.ops import unary_union
except ImportError:
    Polygon = box = unary_union = None

try:
    from .extractor import GeometryExtractor
    from .models import PowerRail, PowerStageResult, ThermalAnalysisSettings, ThermalComponentModel
    from .power_loss import estimate_stage
except (ImportError, ValueError):
    from extractor import GeometryExtractor
    from models import PowerRail, PowerStageResult, ThermalAnalysisSettings, ThermalComponentModel
    from power_loss import estimate_stage


@dataclass
class ThermalVia:
    x_mm: float
    y_mm: float
    diameter_mm: float = 0.6


@dataclass
class ThermalPlacement:
    ref_des: str
    x_mm: float
    y_mm: float
    width_mm: float
    depth_mm: float
    side: str = "TOP"


@dataclass
class CopperLossPoint:
    x_mm: float
    y_mm: float
    layer_id: int
    power_w: float


@dataclass
class ThermalBoardModel:
    bounds_mm: Tuple[float, float, float, float]
    outline: object
    stackup: dict
    copper_by_layer: Dict[int, object] = field(default_factory=dict)
    vias: List[ThermalVia] = field(default_factory=list)
    placements: Dict[str, ThermalPlacement] = field(default_factory=dict)
    components: List[ThermalComponentModel] = field(default_factory=list)
    copper_losses: List[CopperLossPoint] = field(default_factory=list)


@dataclass
class PowerLossEstimate:
    components: List[ThermalComponentModel] = field(default_factory=list)
    stages: List[PowerStageResult] = field(default_factory=list)


class PowerLossEstimator:
    """Estimate component heat from the existing power-tree semantics."""

    LOAD_THERMAL_MODES = {"AUTO", "LOCAL", "EXTERNAL"}

    @classmethod
    def _load_dissipates_locally(cls, load):
        mode = str(getattr(load, "thermal_mode", "AUTO") or "AUTO").upper()
        if mode not in cls.LOAD_THERMAL_MODES:
            mode = "AUTO"
        if mode == "LOCAL":
            return True
        if mode == "EXTERNAL":
            return False
        ref_des = str(load.component_ref.ref_des or "").strip().upper()
        return not ref_des.startswith("J")

    @staticmethod
    def estimate_details(rails: List[PowerRail],
                         component_temperatures_c: Optional[Dict[str, float]] = None
                         ) -> PowerLossEstimate:
        rail_by_name = {rail.net_name: rail for rail in rails}
        component_power = {}
        model_source = {}
        mechanisms = {}
        stages = []

        for rail in rails:
            voltage = max(0.0, float(rail.nominal_voltage))
            for load in rail.loads:
                ref_des = load.component_ref.ref_des
                if PowerLossEstimator._load_dissipates_locally(load):
                    component_power[ref_des] = component_power.get(ref_des, 0.0) + voltage * max(
                        0.0, float(load.total_current)
                    )
                    model_source[ref_des] = "power-tree-load"
                else:
                    # Keep a visible zero-watt row in the Thermal GUI. The
                    # current still participates in upstream regulator losses.
                    component_power.setdefault(ref_des, 0.0)
                    model_source.setdefault(ref_des, "power-tree-external-load")

        memo = {}
        visiting = set()

        def rail_current(rail_name):
            if rail_name in memo:
                return memo[rail_name]
            if rail_name in visiting:
                return 0.0
            visiting.add(rail_name)
            rail = rail_by_name.get(rail_name)
            if rail is None:
                visiting.remove(rail_name)
                return 0.0
            total = sum(max(0.0, float(load.total_current)) for load in rail.loads)
            for regulator in rail.child_regulators:
                output_rail = rail_by_name.get(regulator.output_rail_name)
                output_current = rail_current(regulator.output_rail_name)
                output_voltage = max(0.0, float(output_rail.nominal_voltage)) if output_rail else 0.0
                input_voltage = max(0.0, float(rail.nominal_voltage))
                stage = estimate_stage(
                    regulator, input_voltage, output_voltage, output_current,
                    component_temperatures_c=component_temperatures_c,
                )
                stages.append(stage)
                for contribution in stage.losses:
                    if not contribution.ref_des:
                        continue
                    component_power[contribution.ref_des] = (
                        component_power.get(contribution.ref_des, 0.0) + contribution.power_w
                    )
                    model_source[contribution.ref_des] = "regulator-loss"
                    mechanisms.setdefault(contribution.ref_des, []).append({
                        "mechanism": contribution.mechanism,
                        "power_w": contribution.power_w,
                        "provenance": contribution.provenance,
                    })
                total += stage.iin_a
            visiting.remove(rail_name)
            memo[rail_name] = total
            return total

        for rail in rails:
            rail_current(rail.net_name)

        components = [ThermalComponentModel(
            ref_des=ref_des,
            power_w=power,
            model_source=model_source.get(ref_des, "estimated"),
            loss_mechanisms=mechanisms.get(ref_des, []),
        ) for ref_des, power in sorted(component_power.items()) if (
            power > 0 or model_source.get(ref_des) == "power-tree-external-load"
        )]
        return PowerLossEstimate(components=components, stages=stages)

    @staticmethod
    def estimate(rails: List[PowerRail]) -> List[ThermalComponentModel]:
        """Compatibility wrapper retained for existing callers and project files."""
        return PowerLossEstimator.estimate_details(rails).components


def merge_component_heat_sources(estimated, saved_components, preserve_user=True):
    """Merge fresh electrical losses with persisted thermal component models.

    Automatic entries retain their current loss estimate but keep the physical
    package and junction settings explicitly saved by the user.  User-owned
    entries remain fully manual, including their power value.
    """
    saved_by_ref = {component.ref_des: component for component in saved_components}
    merged = []
    for component in estimated:
        prior = saved_by_ref.get(component.ref_des)
        if prior is None or not preserve_user:
            merged.append(component)
        elif prior.model_source == "user":
            merged.append(replace(prior))
        else:
            merged.append(replace(
                component,
                width_mm=prior.width_mm,
                depth_mm=prior.depth_mm,
                height_mm=prior.height_mm,
                theta_jb_c_per_w=prior.theta_jb_c_per_w,
                max_junction_c=prior.max_junction_c,
                enabled=prior.enabled,
            ))
    return merged


class ThermalModelBuilder:
    # The dialog invalidates this cache on every live-board refresh.  Keeping it
    # here avoids re-unioning all copper when users rerun thermal/CFD analysis
    # without editing the PCB.
    _copper_cache = {}

    def __init__(self, board, debug=False, log_callback=None, board_file_path=None):
        self.board = board
        self.debug = debug
        self.log_callback = log_callback
        # KiCad's IPC board object can omit Edge.Cuts drawings.  Retain the
        # project board path so thermal plots use the physical board outline
        # rather than only the copper/footprint extents.
        self.board_file_path = board_file_path

    @classmethod
    def invalidate_board_cache(cls, board=None):
        """Drop cached thermal copper, optionally for one live KiCad board."""
        if board is None:
            cls._copper_cache.clear()
            return
        cls._copper_cache.pop(id(board), None)

    @classmethod
    def board_geometry_signature(cls, board):
        """Return a lightweight signature of live geometry relevant to heat.

        The IPC board does not expose a portable dirty/revision counter.  This
        fingerprint lets the dialog retain a thermal mesh/CSR between unchanged
        reruns while still invalidating it immediately after track, via, zone,
        footprint or outline edits made in the PCB editor.
        """
        helper = cls(board)
        digest = hashlib.blake2b(digest_size=16)

        def value(item, name):
            current = helper._get_val(item, name, "")
            if current is None:
                return ""
            if name in {"position", "start", "end", "mid"}:
                position = helper._position(item) if name == "position" else current
                if position is not None and name != "position":
                    position = (
                        helper._to_mm(helper._get_val(current, "x", 0.0)),
                        helper._to_mm(helper._get_val(current, "y", 0.0)),
                    )
                return position or ""
            if name == "net":
                return helper._get_val(current, "name", "")
            return current

        fields = ("position", "start", "mid", "end", "layer", "width", "diameter", "net")
        for collection in ("tracks", "vias", "zones", "footprints", "shapes"):
            try:
                items = list(helper._items(collection))
            except Exception:
                # A missing optional IPC collection is safe; the next refresh
                # remains conservative if the board adapter cannot enumerate it.
                items = []
            digest.update(f"{collection}:{len(items)}|".encode("utf-8"))
            for item in items:
                values = [str(value(item, field)) for field in fields]
                # Graphic shapes include Edge.Cuts.  Their geometry is exposed
                # consistently in the wrapper representation even when they do
                # not have a generic ``position`` field.
                if collection == "shapes":
                    values.append(repr(item))
                if collection == "footprints":
                    values.append(str(helper._reference(item)))
                    for pad in helper._pads(item):
                        values.extend(str(value(pad, field)) for field in fields)
                digest.update(";".join(values).encode("utf-8", "replace"))
                digest.update(b"\n")
        return digest.hexdigest()

    def _log(self, message):
        if self.log_callback:
            self.log_callback(f"[THERMAL MODEL] {message}")

    def _get_val(self, obj, attr_name, default=None):
        if obj is None:
            return default
        if hasattr(obj, attr_name):
            value = getattr(obj, attr_name)
            if value is not None and not callable(value):
                return value
        for method_name in (f"get_{attr_name}", attr_name):
            if hasattr(obj, method_name):
                try:
                    value = getattr(obj, method_name)()
                    if value is not None:
                        return value
                except Exception:
                    pass
        return default

    def _items(self, name):
        return self._get_val(self.board, name, []) or []

    @staticmethod
    def _to_mm(value):
        value = float(value or 0.0)
        return value / 1e6 if abs(value) > 10000 else value

    def _position(self, item):
        position = self._get_val(item, "position", self._get_val(item, "start"))
        if position is None:
            return None
        return self._to_mm(self._get_val(position, "x", 0.0)), self._to_mm(
            self._get_val(position, "y", 0.0)
        )

    def _reference(self, footprint):
        value = self._get_val(footprint, "reference", self._get_val(footprint, "ref_des", ""))
        if value:
            return str(value)
        field = self._get_val(footprint, "reference_field")
        text = self._get_val(field, "text")
        return str(self._get_val(text, "value", "") or "")

    def _pads(self, footprint):
        pads = self._get_val(footprint, "pads")
        if pads is None:
            pads = self._get_val(self._get_val(footprint, "definition"), "pads", [])
        return pads or []

    def _footprint_size(self, footprint, default_width, default_depth):
        points = [self._position(pad) for pad in self._pads(footprint)]
        points = [point for point in points if point is not None]
        if len(points) >= 2:
            width = max(point[0] for point in points) - min(point[0] for point in points)
            depth = max(point[1] for point in points) - min(point[1] for point in points)
            return max(default_width, width + 0.5), max(default_depth, depth + 0.5)
        return default_width, default_depth

    @staticmethod
    def _layer_matches(first, second):
        """Compare IPC layer enums across KiCad 9/10 wrapper variants."""
        try:
            return int(first) == int(second)
        except (TypeError, ValueError):
            return str(first).upper() == str(second).upper()

    @classmethod
    def _is_bottom_layer(cls, layer, bottom_layer_id=None):
        if bottom_layer_id is not None and cls._layer_matches(layer, bottom_layer_id):
            return True
        # Legacy pcbnew exposed B.Cu as 31; KiCad 10 IPC uses 34.  Keep the
        # fallback only for incomplete/no-stackup board adapters.
        text = str(layer).upper().replace(" ", "")
        try:
            numeric = int(layer)
        except (TypeError, ValueError):
            numeric = None
        return text in {"B.CU", "B_CU", "BL_B_CU"} or numeric in {31, 34}

    def _discover_net_names(self):
        names = set()
        for collection in ("tracks", "vias", "zones", "footprints"):
            for item in self._items(collection):
                candidates = self._pads(item) if collection == "footprints" else [item]
                for candidate in candidates:
                    net = self._get_val(candidate, "net")
                    name = self._get_val(net, "name", "")
                    if name:
                        names.add(str(name))
        return sorted(names)

    def _extract_copper(self, extractor):
        cached = self._copper_cache.get(id(self.board))
        if cached is not None:
            self._log("Reusing cached merged copper geometry for the live board.")
            return cached

        shapes_by_layer = defaultdict(list)
        names = self._discover_net_names()
        started = time.perf_counter()
        for index, net_name in enumerate(names):
            # Keep the inexpensive per-net collections, then union each board
            # layer exactly once.  Repeated unary_union([previous, next]) work
            # grew quadratically on boards with many nets.
            geometry = extractor.get_net_geometry(net_name, merge=False)
            for layer, polygon in geometry.items():
                if polygon is None or polygon.is_empty:
                    continue
                if hasattr(polygon, "geoms"):
                    shapes_by_layer[layer].extend(
                        shape for shape in polygon.geoms if not shape.is_empty
                    )
                else:
                    shapes_by_layer[layer].append(polygon)
            if self.debug and (index + 1) % 20 == 0:
                self._log(f"Aggregated copper for {index + 1}/{len(names)} nets.")
        by_layer = {}
        for layer, shapes in shapes_by_layer.items():
            if not shapes:
                continue
            layer_started = time.perf_counter()
            by_layer[layer] = unary_union(shapes)
            self._log(
                f"Merged {len(shapes):,} copper shapes on layer {layer} in "
                f"{time.perf_counter() - layer_started:.3f} s."
            )
        self._copper_cache[id(self.board)] = by_layer
        self._log(
            f"Built merged thermal copper for {len(by_layer)} layer(s) in "
            f"{time.perf_counter() - started:.3f} s."
        )
        return by_layer

    def _outline_and_bounds(self, copper_by_layer, placements):
        polygons = [
            polygon for polygon in copper_by_layer.values()
            if polygon is not None and not polygon.is_empty
        ]
        extents = list(polygons)
        if box is not None:
            extents.extend(box(
                placement.x_mm - placement.width_mm / 2.0,
                placement.y_mm - placement.depth_mm / 2.0,
                placement.x_mm + placement.width_mm / 2.0,
                placement.y_mm + placement.depth_mm / 2.0,
            ) for placement in placements.values())
        if extents and unary_union is not None:
            bounds = unary_union(extents).bounds
        elif placements:
            xs = [placement.x_mm for placement in placements.values()]
            ys = [placement.y_mm for placement in placements.values()]
            bounds = min(xs), min(ys), max(xs), max(ys)
        else:
            bounds = (0.0, 0.0, 50.0, 50.0)
        min_x, min_y, max_x, max_y = bounds
        if max_x - min_x < 1.0:
            max_x = min_x + 10.0
        if max_y - min_y < 1.0:
            max_y = min_y + 10.0
        margin = 1.0
        bounds = min_x - margin, min_y - margin, max_x + margin, max_y + margin
        return (box(*bounds) if box is not None else None), bounds

    def build(self, settings: ThermalAnalysisSettings, rails=None, copper_losses=None):
        if Polygon is None or unary_union is None:
            raise ImportError("Shapely is required for 3D thermal model extraction.")
        extractor = GeometryExtractor(self.board, debug=self.debug, log_callback=self.log_callback)
        stackup = extractor.get_board_stackup()
        layer_order = list(stackup.get("layer_order", []))
        bottom_layer_id = layer_order[-1] if layer_order else None

        saved_components = {component.ref_des: replace(component) for component in settings.components}
        if not saved_components and rails:
            saved_components = {
                component.ref_des: component for component in PowerLossEstimator.estimate(list(rails))
            }

        placements = {}
        for footprint in self._items("footprints"):
            ref_des = self._reference(footprint)
            position = self._position(footprint)
            if not ref_des or position is None:
                continue
            component = saved_components.get(ref_des, ThermalComponentModel(ref_des=ref_des))
            width, depth = self._footprint_size(footprint, component.width_mm, component.depth_mm)
            layer = self._get_val(footprint, "layer", 0)
            placements[ref_des] = ThermalPlacement(
                ref_des=ref_des,
                x_mm=position[0],
                y_mm=position[1],
                width_mm=width,
                depth_mm=depth,
                side="BOTTOM" if self._is_bottom_layer(layer, bottom_layer_id) else "TOP",
            )
            if ref_des in saved_components:
                saved_components[ref_des].width_mm = width
                saved_components[ref_des].depth_mm = depth

        if placements:
            top_count = sum(item.side == "TOP" for item in placements.values())
            self._log(
                f"Mapped {top_count} Top / {len(placements) - top_count} Bottom footprint placements "
                f"(F.Cu={layer_order[0] if layer_order else 'auto'}, "
                f"B.Cu={bottom_layer_id if bottom_layer_id is not None else 'auto'})."
            )

        copper_by_layer = self._extract_copper(extractor)
        outline, bounds = self._outline_and_bounds(copper_by_layer, placements)
        live_outline = extractor.get_board_outline(board_file_path=self.board_file_path)
        if live_outline is not None:
            outline = live_outline
            bounds = tuple(float(value) for value in live_outline.bounds)

        vias = []
        for via in self._items("vias"):
            position = self._position(via)
            if position is None:
                continue
            width = self._to_mm(self._get_val(via, "width", 0.6e6))
            vias.append(ThermalVia(position[0], position[1], max(width, 0.1)))

        return ThermalBoardModel(
            bounds_mm=bounds,
            outline=outline,
            stackup=stackup,
            copper_by_layer=copper_by_layer,
            vias=vias,
            placements=placements,
            components=list(saved_components.values()),
            copper_losses=list(copper_losses or []),
        )
