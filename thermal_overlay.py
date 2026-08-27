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
    iz = len(mesh.layer_specs) - 1 if str(side).upper() == "TOP" else 0
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


def heatmap_png(mesh, result, side):
    """Build a transparent, embedded-PNG heat map and its native DPI."""
    try:
        from matplotlib import colormaps
        from matplotlib import image as mpl_image
    except ImportError as exc:
        raise RuntimeError("Matplotlib is required to create a KiCad heat overlay.") from exc

    field, min_x, min_y, max_x, max_y = _surface_field(mesh, result, side)
    valid = np.isfinite(field)
    if not np.any(valid):
        raise ValueError(f"No {str(side).lower()} thermal surface cells are available.")
    low = float(np.nanmin(field))
    high = float(np.nanmax(field))
    if high - low < 1.0e-9:
        high = low + 1.0
    normalized = np.clip((field - low) / (high - low), 0.0, 1.0)
    rgba = colormaps["inferno"](np.nan_to_num(normalized, nan=0.0))
    rgba[..., 3] = np.where(valid, 0.72, 0.0)

    # The physical image dimensions follow the board aspect ratio.  KiCad
    # reads the PNG pHYs/DPI metadata when image_scale=1.0.
    width_mm = max(1.0e-6, max_x - min_x)
    dpi = max(25.4, 25.4 * float(rgba.shape[1]) / width_mm)
    stream = BytesIO()
    mpl_image.imsave(
        stream, rgba, format="png", dpi=dpi,
        metadata={"KiPIDA": OVERLAY_MARKER.decode("ascii"), "Surface": str(side).upper()},
    )
    return stream.getvalue(), (min_x, min_y), dpi


class ThermalOverlayManager:
    """Inject/remove the two KiCad user-layer images owned by Ki-PIDA."""

    TOP_LAYER_NAME = "User.Drawings"
    BOTTOM_LAYER_NAME = "User.Comments"

    def __init__(self, board, log_callback=None):
        self.board = board
        self.log_callback = log_callback

    def _log(self, message):
        if self.log_callback:
            self.log_callback(f"[THERMAL OVERLAY] {message}")

    @staticmethod
    def _layers():
        try:
            from kipy.board_types import BoardLayer
            return BoardLayer.BL_Dwgs_User, BoardLayer.BL_Cmts_User
        except (ImportError, AttributeError) as exc:
            raise RuntimeError("This KiCad IPC API does not expose user drawing layers.") from exc

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

    def inject(self, mesh, result):
        ReferenceImage = _reference_image_type()
        top_layer, bottom_layer = self._layers()
        try:
            from kipy.geometry import Vector2
        except ImportError as exc:
            raise RuntimeError("KiCad IPC geometry bindings are not available.") from exc

        prepared = []
        for side, layer in (("TOP", top_layer), ("BOTTOM", bottom_layer)):
            png, (x_mm, y_mm), dpi = heatmap_png(mesh, result, side)
            min_x, min_y, max_x, max_y = mesh.bounds_mm
            image = ReferenceImage()
            image.layer = layer
            # KiCad reference-image position is the image centre, whereas the
            # heat field is expressed from the board's minimum X/Y corner.
            image.position = Vector2.from_xy_mm(
                x_mm + (max_x - min_x) / 2.0,
                y_mm + (max_y - min_y) / 2.0,
            )
            image.transform_origin_offset = Vector2.from_xy_mm(0.0, 0.0)
            image.image_scale = 1.0
            image.image_data = png
            image.locked = True
            prepared.append(image)
            self._log(f"Prepared {side} heat image ({len(png) / 1024:.0f} KiB, {dpi:.1f} DPI).")

        # Only remove an old overlay after both new surfaces were generated.
        self.clear()
        created = self.board.create_items(prepared)
        self._log(
            "Injected thermal overlays on User.Drawings (Top) and User.Comments (Bottom). "
            "Toggle those layers in Appearance to inspect each side."
        )
        return created
