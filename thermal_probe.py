"""Fast, exact node readout for rendered two-dimensional thermal maps."""

from dataclasses import dataclass
from typing import Optional, Tuple

from i18n import _


@dataclass(frozen=True)
class ThermalProbeReading:
    x_mm: float
    y_mm: float
    z_mm: float
    temperature_c: float
    layer_name: str

    def label(self) -> str:
        return _(
            "Thermal probe  {temperature:.2f} C  |  X {x:.2f} mm, Y {y:.2f} mm, "
            "Z {z:.3f} mm  |  {layer}"
        ).format(
            temperature=self.temperature_c, x=self.x_mm, y=self.y_mm,
            z=self.z_mm, layer=self.layer_name,
        )


class ThermalMapProbe:
    """Map rendered-plot pixels back to the nearest thermal mesh node.

    ``axes_bounds`` uses Matplotlib figure coordinates (left, bottom, width,
    height).  The lookup itself delegates to ThermalMesh.nearest_node(), which
    indexes the regular mesh locally instead of scanning every thermal cell.
    """

    def __init__(self, mesh, result, layer_index, layer_name, axes_bounds, x_limits, y_limits):
        self.mesh = mesh
        self.result = result
        self.layer_index = int(layer_index)
        self.layer_name = str(layer_name)
        self.axes_bounds = tuple(float(value) for value in axes_bounds)
        self.x_limits = tuple(float(value) for value in x_limits)
        self.y_limits = tuple(float(value) for value in y_limits)

    def sample(self, pixel_x, pixel_y, bitmap_width, bitmap_height) -> Optional[ThermalProbeReading]:
        """Return a reading for a bitmap position, or None outside its axes."""
        if bitmap_width <= 0 or bitmap_height <= 0:
            return None
        left, bottom, width, height = self.axes_bounds
        figure_x = float(pixel_x) / float(bitmap_width)
        # wx bitmap coordinates start at the top while Matplotlib figure
        # coordinates start at the bottom.
        figure_y = 1.0 - float(pixel_y) / float(bitmap_height)
        if not (left <= figure_x <= left + width and bottom <= figure_y <= bottom + height):
            return None
        fraction_x = (figure_x - left) / width
        fraction_y = (figure_y - bottom) / height
        x_mm = self.x_limits[0] + fraction_x * (self.x_limits[1] - self.x_limits[0])
        y_mm = self.y_limits[0] + fraction_y * (self.y_limits[1] - self.y_limits[0])
        specs = list(getattr(self.mesh, "layer_specs", []) or [])
        layer_id = specs[self.layer_index].layer_id if 0 <= self.layer_index < len(specs) else None
        node = self.mesh.nearest_node(x_mm, y_mm, layer_id=layer_id)
        if node is None:
            return None
        temperatures = getattr(self.result, "temperature_vector_c", None)
        if temperatures is not None:
            temperature = float(temperatures[node])
        else:
            temperature = float(self.result.temperatures_c[node])
        x_mm, y_mm, z_mm = self.mesh.node_coords[node]
        return ThermalProbeReading(
            float(x_mm), float(y_mm), float(z_mm), temperature, self.layer_name,
        )
