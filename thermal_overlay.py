"""KiCad non-electrical thermal heat-map overlays.

The KiCad IPC API represents board reference images as embedded PNG blobs.  A
reference image is non-plotting and can live on a user layer, which makes it a
safe way to inspect thermal hotspots directly in PCB Editor without changing
the electrical design.
"""

from io import BytesIO
import math

import numpy as np


OVERLAY_MARKER = b"KiPIDA-Thermal-Overlay-v1"


_COLOR_STOPS = {
    "inferno": ((0.00, (0, 0, 4)), (0.20, (87, 15, 109)), (0.40, (188, 55, 84)),
                (0.60, (249, 142, 8)), (0.80, (249, 201, 50)), (1.00, (252, 255, 164))),
    "viridis": ((0.00, (68, 1, 84)), (0.25, (59, 82, 139)), (0.50, (33, 145, 140)),
                (0.75, (94, 201, 98)), (1.00, (253, 231, 37))),
    "turbo": ((0.00, (48, 18, 59)), (0.20, (50, 98, 141)), (0.40, (32, 190, 172)),
              (0.60, (164, 252, 60)), (0.80, (250, 136, 25)), (1.00, (122, 4, 3))),
    "plasma": ((0.00, (13, 8, 135)), (0.25, (126, 3, 168)), (0.50, (204, 71, 120)),
               (0.75, (248, 149, 64)), (1.00, (240, 249, 33))),
    "cividis": ((0.00, (0, 32, 76)), (0.25, (40, 67, 105)), (0.50, (102, 105, 113)),
                (0.75, (170, 145, 95)), (1.00, (255, 233, 69))),
}


def _reference_image_type():
    """Return a ReferenceImage wrapper, including support for older kipy.

    KiCad 10.0 ships an IPC protobuf for reference images, while some bundled
    kipy versions do not yet expose its Python wrapper.  Registering this
    minimal wrapper lets create/get/delete work on those versions too.
    """
    try:
        import kipy.board_types as board_types
        from kipy.board_types import BoardItem
        from kipy.geometry import Vector2
        from kipy.proto.board import board_types_pb2
        from kipy.proto.common.types.base_types_pb2 import LockedState
    except ImportError as exc:
        raise RuntimeError("KiCad IPC Python bindings are not available.") from exc

    existing = getattr(board_types, "ReferenceImage", None)
    if existing is not None:
        return existing

    class ReferenceImage(BoardItem):
        def __init__(self, proto=None):
            self._proto = board_types_pb2.ReferenceImage()
            if proto is not None:
                self._proto.CopyFrom(proto)

        @property
        def id(self):
            return self._proto.id

        @property
        def layer(self):
            return self._proto.layer

        @layer.setter
        def layer(self, value):
            self._proto.layer = value

        @property
        def position(self):
            return Vector2(self._proto.position)

        @position.setter
        def position(self, value):
            self._proto.position.CopyFrom(value.proto)

        @property
        def transform_origin_offset(self):
            return Vector2(self._proto.transform_origin_offset)

        @transform_origin_offset.setter
        def transform_origin_offset(self, value):
            self._proto.transform_origin_offset.CopyFrom(value.proto)

        @property
        def image_scale(self):
            return float(self._proto.image_scale.value)

        @image_scale.setter
        def image_scale(self, value):
            self._proto.image_scale.value = float(value)

        @property
        def image_data(self):
            return bytes(self._proto.image_data)

        @image_data.setter
        def image_data(self, value):
            self._proto.image_data = bytes(value)

        @property
        def locked(self):
            return self._proto.locked == LockedState.LS_LOCKED

        @locked.setter
        def locked(self, value):
            self._proto.locked = (
                LockedState.LS_LOCKED if value else LockedState.LS_UNLOCKED
            )

    # CRUD unwrap uses this table internally.  The API already knows the
    # protobuf type; this only fills the short kipy wrapper-version gap.
    board_types._proto_to_object[board_types_pb2.ReferenceImage] = ReferenceImage
    board_types.ReferenceImage = ReferenceImage
    return ReferenceImage


def _surface_field(mesh, result, side, max_dimension=1800):
    """Sample one outer surface to a bounded RGBA source grid."""
    min_x, min_y, max_x, max_y = mesh.bounds_mm
    grid = float(mesh.grid_size_mm)
    nx = max(1, int(math.ceil((max_x - min_x) / grid)))
    ny = max(1, int(math.ceil((max_y - min_y) / grid)))
    # Thermal stackup order follows KiCad's F.Cu -> B.Cu order.
    iz = 0 if str(side).upper() == "TOP" else len(mesh.layer_specs) - 1
    temperatures = result.temperature_vector_c
    if temperatures is None:
        temperatures = np.fromiter(
            (result.temperatures_c[node] for node in mesh.nodes), dtype=float,
            count=len(mesh.nodes),
        )
    temperatures = np.asarray(temperatures, dtype=float)
    field = np.full((ny, nx), np.nan, dtype=np.float32)
    for (ix, iy, layer_index), node in mesh.node_map.items():
        if layer_index == iz and 0 <= ix < nx and 0 <= iy < ny:
            field[iy, ix] = temperatures[int(node)]

    step = max(1, int(math.ceil(max(nx, ny) / float(max_dimension))))
    if step > 1:
        # A local NaN-aware average preserves the temperature field while
        # reducing the board payload and keeping a uniform physical scale.
        pad_y = (-ny) % step
        pad_x = (-nx) % step
        padded = np.pad(field, ((0, pad_y), (0, pad_x)), constant_values=np.nan)
        with np.errstate(invalid="ignore"):
            field = np.nanmean(
                padded.reshape(padded.shape[0] // step, step, padded.shape[1] // step, step),
                axis=(1, 3),
            )
    return field, min_x, min_y, max_x, max_y


def _temperature_limits(result):
    """Return one stable scale shared by every thermal surface overlay."""
    temperatures = result.temperature_vector_c
    if temperatures is None:
        temperatures = list(result.temperatures_c.values())
    values = np.asarray(temperatures, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        raise ValueError("No thermal temperatures are available for an overlay scale.")
    low = float(np.min(values))
    high = float(np.max(values))
    return (low, high if high - low >= 1.0e-9 else low + 1.0)


def _colorize(values, name):
    """Map normalized values to portable RGBA colours without Matplotlib."""
    name = str(name or "inferno").lower()
    stops = _COLOR_STOPS.get(name, _COLOR_STOPS["inferno"])
    positions = np.asarray([position for position, _ in stops], dtype=float)
    colours = np.asarray([colour for _, colour in stops], dtype=float)
    values = np.clip(np.asarray(values, dtype=float), 0.0, 1.0)
    channels = [np.interp(values, positions, colours[:, channel]) for channel in range(3)]
    rgb = np.stack(channels, axis=-1).astype(np.uint8)
    return rgb


def _png_info(**values):
    try:
        from PIL import PngImagePlugin
    except ImportError as exc:
        raise RuntimeError("Pillow is required to create a KiCad heat overlay.") from exc
    info = PngImagePlugin.PngInfo()
    info.add_text("KiPIDA", OVERLAY_MARKER.decode("ascii"))
    for key, value in values.items():
        info.add_text(str(key), str(value))
    return info


def heatmap_png(mesh, result, side, color_map="inferno", temperature_limits=None):
    """Build a transparent, embedded-PNG heat map and its native DPI."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required to create a KiCad heat overlay.") from exc

    field, min_x, min_y, max_x, max_y = _surface_field(mesh, result, side)
    valid = np.isfinite(field)
    if not np.any(valid):
        raise ValueError(f"No {str(side).lower()} thermal surface cells are available.")
    low, high = temperature_limits or _temperature_limits(result)
    normalized = np.clip((field - low) / (high - low), 0.0, 1.0)
    rgb = _colorize(np.nan_to_num(normalized, nan=0.0), color_map)
    rgba = np.empty((*rgb.shape[:2], 4), dtype=np.uint8)
    rgba[..., :3] = rgb
    rgba[..., 3] = np.where(valid, 184, 0)

    # The physical image dimensions follow the board aspect ratio.  KiCad
    # reads the PNG pHYs/DPI metadata when image_scale=1.0.
    width_mm = max(1.0e-6, max_x - min_x)
    dpi = max(25.4, 25.4 * float(rgba.shape[1]) / width_mm)
    stream = BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(
        stream, format="PNG", dpi=(dpi, dpi), pnginfo=_png_info(
            Surface=str(side).upper(), ColorMap=str(color_map),
            TemperatureRange=f"{low:.4g},{high:.4g}",
        ),
    )
    return stream.getvalue(), (min_x, min_y), dpi


def heatmap_scale_png(side, color_map, temperature_limits):
    """Build a compact transparent colour scale for a KiCad reference image."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("Pillow is required to create a KiCad heat overlay scale.") from exc

    low, high = temperature_limits
    width_mm, height_mm, dpi = 20.0, 52.0, 180.0
    width_px = max(1, int(round(width_mm / 25.4 * dpi)))
    height_px = max(1, int(round(height_mm / 25.4 * dpi)))
    image = Image.new("RGBA", (width_px, height_px), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image, "RGBA")
    margin = max(4, width_px // 20)
    draw.rectangle((margin, margin, width_px - margin, height_px - margin), fill=(255, 255, 255, 220))
    font = ImageFont.load_default()
    draw.text((2 * margin, 2 * margin), f"Ki-PIDA {str(side).title()}", fill=(0, 0, 0, 255), font=font)
    bar_left, bar_right = 2 * margin, max(2 * margin + 8, width_px // 2)
    bar_top, bar_bottom = height_px // 5, height_px - height_px // 5
    gradient = _colorize(np.linspace(1.0, 0.0, bar_bottom - bar_top + 1)[:, None], color_map)
    gradient = np.repeat(gradient, bar_right - bar_left + 1, axis=1)
    image.paste(Image.fromarray(gradient, mode="RGB"), (bar_left, bar_top))
    draw.rectangle((bar_left, bar_top, bar_right, bar_bottom), outline=(0, 0, 0, 255), width=1)
    label_x = bar_right + margin
    draw.text((label_x, bar_top - 2), f"{high:.1f} °C", fill=(0, 0, 0, 255), font=font)
    draw.text((label_x, (bar_top + bar_bottom) // 2 - 5), f"{(low + high) / 2.0:.1f}", fill=(0, 0, 0, 255), font=font)
    draw.text((label_x, bar_bottom - 9), f"{low:.1f} °C", fill=(0, 0, 0, 255), font=font)
    stream = BytesIO()
    image.save(
        stream, format="PNG", dpi=(dpi, dpi), pnginfo=_png_info(
            Kind="Scale", Surface=str(side).upper(), ColorMap=str(color_map),
        ),
    )
    return stream.getvalue(), width_mm, height_mm, dpi


class ThermalOverlayManager:
    """Inject/remove the two KiCad user-layer images owned by Ki-PIDA."""

    TOP_LAYER_NAME = "X.Thermal.Top"
    BOTTOM_LAYER_NAME = "X.Thermal.Bottom"

    def __init__(self, board, log_callback=None):
        self.board = board
        self.log_callback = log_callback

    def _log(self, message):
        if self.log_callback:
            self.log_callback(f"[THERMAL OVERLAY] {message}")

    def _layers(self):
        """Reserve two unused KiCad user layers for the overlay.

        KiCad has a fixed set of non-electrical User.1..User.45 layers; it
        does not create arbitrary layer IDs.  We enable two unused ones and
        assign their user-visible names to X.Thermal.Top/Bottom, without ever
        repurposing User.Drawings, User.Comments or an already enabled layer.
        """
        try:
            from kipy.board_types import BoardLayer
            candidates = [
                getattr(BoardLayer, f"BL_User_{index}") for index in range(1, 46)
                if hasattr(BoardLayer, f"BL_User_{index}")
            ]
        except (ImportError, AttributeError) as exc:
            raise RuntimeError("This KiCad IPC API does not expose user drawing layers.") from exc

        # KiCad 10 IPC has shipped both forms: recent builds expose a
        # ``board.layers`` operation group whereas earlier 10.x builds expose
        # the same methods directly on ``Board``.  Prefer the former but keep
        # the overlay usable on the latter.
        layers_api = getattr(self.board, "layers", None) or self.board
        try:
            get_info = getattr(layers_api, "get_layers_info", None)
            info = list(get_info()) if callable(get_info) else []
            enabled = set(layers_api.get_enabled_layers())
            copper_count = layers_api.get_copper_layer_count()
        except Exception as exc:
            raise RuntimeError("KiCad could not query the board user-layer configuration.") from exc

        named = {}
        for item in info:
            name = str(item.get("user_name") or item.get("name") or "")
            if name in {self.TOP_LAYER_NAME, self.BOTTOM_LAYER_NAME}:
                named[name] = item["layer"]
        needed = [name for name in (self.TOP_LAYER_NAME, self.BOTTOM_LAYER_NAME) if name not in named]
        available = [layer for layer in candidates if layer not in enabled and layer not in named.values()]
        if len(available) < len(needed):
            raise RuntimeError(
                "No unused KiCad User layer is available for X.Thermal. "
                "Free two User.n layers or remove a previous custom layer assignment."
            )
        assigned = dict(named)
        if needed:
            selected = available[:len(needed)]
            try:
                # KiCad's IPC setter takes the copper count separately and
                # expects *only* enabled non-copper layer IDs in ``layers``.
                # ``get_enabled_layers`` also contains F/B/inner copper IDs.
                if info:
                    non_copper = {
                        item["layer"] for item in info
                        if not str(item.get("layer_name", "")).endswith("_Cu")
                    }
                else:
                    # Older IPC builds cannot provide layer metadata.  Their
                    # enabled-layer list still contains copper, so remove the
                    # complete fixed KiCad copper-layer enum range ourselves.
                    copper_layers = {
                        getattr(BoardLayer, name) for name in ("BL_F_Cu", "BL_B_Cu")
                        if hasattr(BoardLayer, name)
                    }
                    copper_layers.update(
                        getattr(BoardLayer, f"BL_In{index}_Cu")
                        for index in range(1, 31)
                        if hasattr(BoardLayer, f"BL_In{index}_Cu")
                    )
                    non_copper = enabled - copper_layers
                layers_api.set_enabled_layers(
                    copper_count, list(non_copper | set(selected)),
                )
                set_name = getattr(layers_api, "set_layer_name", None)
                for name, layer in zip(needed, selected):
                    if callable(set_name):
                        set_name(layer, name)
                    assigned[name] = layer
            except Exception as exc:
                raise RuntimeError("KiCad could not create the dedicated X.Thermal user layers.") from exc
            if not callable(getattr(layers_api, "set_layer_name", None)):
                self._log(
                    "This KiCad IPC version cannot rename User layers; reserved "
                    "User.n layers remain dedicated to X.Thermal for this board."
                )
            self._log(
                "Reserved non-electrical layers " + ", ".join(
                    f"{name} ({layer})" for name, layer in assigned.items()
                ) + "."
            )
        return assigned[self.TOP_LAYER_NAME], assigned[self.BOTTOM_LAYER_NAME]

    def _owned_images(self):
        _reference_image_type()
        try:
            from kipy.proto.common.types import KiCadObjectType
            images = self.board.get_items(KiCadObjectType.KOT_PCB_REFERENCE_IMAGE)
        except Exception as exc:
            raise RuntimeError(
                "KiCad did not expose board reference-image IPC operations. "
                "Update KiCad 10 to a current maintenance release."
            ) from exc
        return [image for image in images if OVERLAY_MARKER in bytes(image.image_data)]

    def clear(self):
        images = self._owned_images()
        if images:
            self.board.remove_items(images)
        self._log(f"Removed {len(images)} Ki-PIDA thermal overlay image(s).")
        return len(images)

    @staticmethod
    def _reference_image(ReferenceImage, layer, png, center_x_mm, center_y_mm):
        from kipy.geometry import Vector2
        image = ReferenceImage()
        image.layer = layer
        image.position = Vector2.from_xy_mm(center_x_mm, center_y_mm)
        image.transform_origin_offset = Vector2.from_xy_mm(0.0, 0.0)
        image.image_scale = 1.0
        image.image_data = png
        image.locked = True
        return image

    def inject(self, mesh, result, color_map="inferno"):
        ReferenceImage = _reference_image_type()
        top_layer, bottom_layer = self._layers()
        try:
            from kipy.geometry import Vector2  # noqa: F401 - capability check
        except ImportError as exc:
            raise RuntimeError("KiCad IPC geometry bindings are not available.") from exc

        color_map = str(color_map or "inferno").lower()
        limits = _temperature_limits(result)
        min_x, min_y, max_x, max_y = mesh.bounds_mm
        prepared = []
        for side, layer in (("TOP", top_layer), ("BOTTOM", bottom_layer)):
            png, (x_mm, y_mm), dpi = heatmap_png(
                mesh, result, side, color_map=color_map, temperature_limits=limits,
            )
            # KiCad reference-image position is the image centre, whereas the
            # heat field is expressed from the board's minimum X/Y corner.
            prepared.append(self._reference_image(
                ReferenceImage, layer, png,
                x_mm + (max_x - min_x) / 2.0,
                y_mm + (max_y - min_y) / 2.0,
            ))
            scale_png, scale_width_mm, scale_height_mm, scale_dpi = heatmap_scale_png(
                side, color_map, limits,
            )
            # Place the scale at the upper-right of the drawing area, outside
            # the PCB, so it remains readable without covering copper or pads.
            prepared.append(self._reference_image(
                ReferenceImage, layer, scale_png,
                max_x + 3.0 + scale_width_mm / 2.0,
                min_y + 3.0 + scale_height_mm / 2.0,
            ))
            self._log(f"Prepared {side} heat image ({len(png) / 1024:.0f} KiB, {dpi:.1f} DPI).")
            self._log(
                f"Prepared {side} colour scale ({limits[0]:.1f} to {limits[1]:.1f} C, "
                f"{scale_dpi:.0f} DPI)."
            )

        # Only remove an old overlay after both new surfaces were generated.
        self.clear()
        created = self.board.create_items(prepared)
        self._log(
            "Injected thermal overlays and colour scales on X.Thermal.Top and "
            "X.Thermal.Bottom. Toggle those layers in Appearance to inspect each side."
        )
        return created
