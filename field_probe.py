"""Mouse readout for rendered electromagnetic near-field maps."""

from dataclasses import dataclass
from typing import Optional

import numpy as np

from i18n import _


@dataclass(frozen=True)
class EMFieldProbeReading:
    x_mm: float
    y_mm: float
    value: float
    quantity: str
    unit: str
    probe_height_mm: float

    def label(self):
        return _(
            "EM {quantity}-field  {value:.5g} {unit}  |  X {x:.2f} mm, Y {y:.2f} mm  |  "
            "probe height {height:g} mm"
        ).format(
            quantity=self.quantity, value=self.value, unit=self.unit,
            x=self.x_mm, y=self.y_mm, height=self.probe_height_mm,
        )


class EMFieldMapProbe:
    """Map bitmap pixels to the nearest regular-grid field sample."""

    def __init__(
        self, x_coordinates_mm, y_coordinates_mm, values, quantity, unit,
        probe_height_mm, axes_bounds, x_limits, y_limits,
    ):
        self.x_coordinates_mm = np.asarray(x_coordinates_mm, dtype=float)
        self.y_coordinates_mm = np.asarray(y_coordinates_mm, dtype=float)
        self.values = np.asarray(values, dtype=float)
        self.quantity = str(quantity)
        self.unit = str(unit)
        self.probe_height_mm = float(probe_height_mm)
        self.axes_bounds = tuple(float(value) for value in axes_bounds)
        self.x_limits = tuple(float(value) for value in x_limits)
        self.y_limits = tuple(float(value) for value in y_limits)

    def sample(self, pixel_x, pixel_y, bitmap_width, bitmap_height) -> Optional[EMFieldProbeReading]:
        if bitmap_width <= 0 or bitmap_height <= 0:
            return None
        left, bottom, width, height = self.axes_bounds
        figure_x = float(pixel_x) / float(bitmap_width)
        figure_y = 1.0 - float(pixel_y) / float(bitmap_height)
        if not (left <= figure_x <= left + width and bottom <= figure_y <= bottom + height):
            return None
        x_mm = self.x_limits[0] + (figure_x - left) / width * (self.x_limits[1] - self.x_limits[0])
        y_mm = self.y_limits[0] + (figure_y - bottom) / height * (self.y_limits[1] - self.y_limits[0])
        x_index = int(np.argmin(np.abs(self.x_coordinates_mm - x_mm)))
        y_index = int(np.argmin(np.abs(self.y_coordinates_mm - y_mm)))
        if self.values.shape != (self.y_coordinates_mm.size, self.x_coordinates_mm.size):
            return None
        return EMFieldProbeReading(
            float(self.x_coordinates_mm[x_index]), float(self.y_coordinates_mm[y_index]),
            float(self.values[y_index, x_index]), self.quantity, self.unit,
            self.probe_height_mm,
        )
