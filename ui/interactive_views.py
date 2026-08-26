"""Reusable zoom, pan and readable-output helpers for Ki-PIDA wx views."""

import math

import wx


def _copy_font(font):
    """Return a detached font across the wxPython versions bundled by KiCad.

    wxPython Phoenix accepts a font in the ``wx.Font`` constructor, while
    some KiCad builds do not expose the convenience ``Font.Copy`` method.
    Keep a native-info fallback for older wx builds with a narrower overload
    set.
    """
    try:
        return wx.Font(font)
    except (TypeError, AttributeError):
        copied = wx.Font()
        copied.SetNativeFontInfo(font.GetNativeFontInfo())
        return copied


class ZoomableBitmapPanel(wx.ScrolledWindow):
    """A bitmap viewport with wheel zoom and drag pan.

    Matplotlib plots are rendered in background threads and delivered as wx
    bitmaps.  Keeping interaction at this layer avoids using a live matplotlib
    canvas in KiCad's wx event loop.
    """

    MIN_SCALE = 0.25
    MAX_SCALE = 5.0

    def __init__(self, parent, bitmap):
        super().__init__(parent, style=wx.HSCROLL | wx.VSCROLL | wx.BORDER_NONE)
        self.SetScrollRate(10, 10)
        self._bitmap = bitmap
        self._scale = 1.0
        self._drag_origin = None
        self._view_origin = None
        self._image = wx.StaticBitmap(self)
        self._sizer = wx.BoxSizer(wx.VERTICAL)
        self._sizer.Add(self._image, 0, wx.ALL, 5)
        self.SetSizer(self._sizer)
        self._render()
        self.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)
        self._image.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)
        for window in (self, self._image):
            window.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
            window.Bind(wx.EVT_LEFT_UP, self._on_left_up)
            window.Bind(wx.EVT_MOTION, self._on_motion)
            window.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)

    @property
    def scale(self):
        return self._scale

    def _render(self):
        if not self._bitmap or not self._bitmap.IsOk():
            return
        width = max(1, int(round(self._bitmap.GetWidth() * self._scale)))
        height = max(1, int(round(self._bitmap.GetHeight() * self._scale)))
        image = self._bitmap.ConvertToImage().Scale(width, height, wx.IMAGE_QUALITY_HIGH)
        self._image.SetBitmap(wx.Bitmap(image))
        self._image.SetMinSize((width, height))
        self._sizer.Layout()
        self.FitInside()

    def _on_wheel(self, event):
        rotation = event.GetWheelRotation()
        if not rotation:
            return
        factor = 1.15 if rotation > 0 else 1.0 / 1.15
        new_scale = max(self.MIN_SCALE, min(self.MAX_SCALE, self._scale * factor))
        if math.isclose(new_scale, self._scale, rel_tol=1e-6):
            return
        position = self.ScreenToClient(event.GetEventObject().ClientToScreen(event.GetPosition()))
        old_x, old_y = self.GetViewStart()
        unit_x, unit_y = self.GetScrollPixelsPerUnit()
        virtual_x = old_x * unit_x + position.x
        virtual_y = old_y * unit_y + position.y
        image_x = virtual_x / self._scale
        image_y = virtual_y / self._scale
        self._scale = new_scale
        self._render()
        target_x = max(0, int((image_x * new_scale - position.x) / max(unit_x, 1)))
        target_y = max(0, int((image_y * new_scale - position.y) / max(unit_y, 1)))
        self.Scroll(target_x, target_y)

    def _on_left_down(self, event):
        if self._scale != 1.0:
            self._drag_origin = self.ScreenToClient(event.GetEventObject().ClientToScreen(event.GetPosition()))
            self._view_origin = self.GetViewStart()
            if not self.HasCapture():
                self.CaptureMouse()
        event.Skip()

    def _on_left_up(self, event):
        self._finish_drag()
        event.Skip()

    def _on_capture_lost(self, event):
        self._finish_drag()
        event.Skip()

    def _finish_drag(self):
        self._drag_origin = self._view_origin = None
        if self.HasCapture():
            self.ReleaseMouse()

    def _on_motion(self, event):
        if self._drag_origin is not None and event.LeftIsDown():
            point = self.ScreenToClient(event.GetEventObject().ClientToScreen(event.GetPosition()))
            unit_x, unit_y = self.GetScrollPixelsPerUnit()
            self.Scroll(
                max(0, self._view_origin[0] - int((point.x - self._drag_origin.x) / max(unit_x, 1))),
                max(0, self._view_origin[1] - int((point.y - self._drag_origin.y) / max(unit_y, 1))),
            )
            return
        event.Skip()


class ListZoomPanController:
    """Zoom a report list by font/column scaling and pan it after zooming."""

    MIN_SCALE = 0.75
    MAX_SCALE = 2.25
    PAN_THRESHOLD_PX = 4

    def __init__(self, control):
        self.control = control
        self.scale = 1.0
        self._font = control.GetFont()
        self._columns = [control.GetColumnWidth(index) for index in range(control.GetColumnCount())]
        self._drag_origin = None
        self._view_origin = None
        self._panning = False
        control.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)
        control.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        control.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        control.Bind(wx.EVT_MOTION, self._on_motion)
        control.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)

    def _apply(self):
        font = _copy_font(self._font)
        font.SetPointSize(max(6, int(round(self._font.GetPointSize() * self.scale))))
        self.control.SetFont(font)
        for index, width in enumerate(self._columns):
            self.control.SetColumnWidth(index, max(30, int(round(width * self.scale))))
        self.control.Refresh()

    def _on_wheel(self, event):
        factor = 1.12 if event.GetWheelRotation() > 0 else 1.0 / 1.12
        self.scale = max(self.MIN_SCALE, min(self.MAX_SCALE, self.scale * factor))
        self._apply()

    def _on_left_down(self, event):
        if self.scale != 1.0:
            self._drag_origin = event.GetPosition()
            self._view_origin = (
                self.control.GetScrollPos(wx.HORIZONTAL),
                self.control.GetScrollPos(wx.VERTICAL),
            )
            self._panning = False
        event.Skip()

    def _on_left_up(self, event):
        self._finish_drag()
        event.Skip()

    def _on_capture_lost(self, event):
        self._finish_drag()
        event.Skip()

    def _finish_drag(self):
        self._drag_origin = self._view_origin = None
        self._panning = False
        if self.control.HasCapture():
            self.control.ReleaseMouse()

    def _on_motion(self, event):
        if self._drag_origin is None or not event.LeftIsDown():
            event.Skip()
            return
        point = event.GetPosition()
        dx, dy = point.x - self._drag_origin.x, point.y - self._drag_origin.y
        if not self._panning and math.hypot(dx, dy) < self.PAN_THRESHOLD_PX:
            event.Skip()
            return
        if not self._panning:
            self._panning = True
            if not self.control.HasCapture():
                self.control.CaptureMouse()
        self.control.SetScrollPos(wx.HORIZONTAL, max(0, self._view_origin[0] - dx), True)
        self.control.SetScrollPos(wx.VERTICAL, max(0, self._view_origin[1] - dy), True)


class TextZoomController:
    """Ctrl+wheel and Ctrl+plus/minus font scaling for read-only consoles."""

    MIN_POINTS = 7
    MAX_POINTS = 28

    def __init__(self, control):
        self.control = control
        self._base_points = max(self.MIN_POINTS, control.GetFont().GetPointSize())
        self._points = self._base_points
        control.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)
        control.Bind(wx.EVT_CHAR_HOOK, self._on_key)

    def _set_points(self, points):
        self._points = max(self.MIN_POINTS, min(self.MAX_POINTS, points))
        font = _copy_font(self.control.GetFont())
        font.SetPointSize(self._points)
        self.control.SetFont(font)
        self.control.Refresh()

    def _on_wheel(self, event):
        if not event.ControlDown():
            event.Skip()
            return
        self._set_points(self._points + (1 if event.GetWheelRotation() > 0 else -1))

    def _on_key(self, event):
        if not event.ControlDown():
            event.Skip()
            return
        key = event.GetKeyCode()
        if key in (ord('+'), getattr(wx, "WXK_ADD", -1)):
            self._set_points(self._points + 1)
        elif key in (ord('-'), getattr(wx, "WXK_SUBTRACT", -1)):
            self._set_points(self._points - 1)
        elif key in (ord('0'), getattr(wx, "WXK_NUMPAD0", -1)):
            self._set_points(self._base_points)
        else:
            event.Skip()


def install_navigation(root):
    """Attach Phase 6 navigation to existing report tables and consoles once."""
    for child in root.GetChildren():
        if isinstance(child, wx.ListCtrl) and not hasattr(child, "_kipida_zoom_pan"):
            child._kipida_zoom_pan = ListZoomPanController(child)
        if isinstance(child, wx.TextCtrl):
            style = child.GetWindowStyleFlag()
            if style & wx.TE_MULTILINE and style & wx.TE_READONLY and not hasattr(child, "_kipida_text_zoom"):
                child._kipida_text_zoom = TextZoomController(child)
        install_navigation(child)
