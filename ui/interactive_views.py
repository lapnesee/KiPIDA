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


class ProbePopup(wx.PopupWindow):
    """Persistent observation popup with click-close and double-click copy."""

    def __init__(self, parent, text, dismiss_callback=None, copy_callback=None):
        super().__init__(parent, wx.BORDER_SIMPLE)
        self._text = str(text)
        self._dismiss_callback = dismiss_callback
        self._copy_callback = copy_callback
        self._dismiss_later = None
        self.SetBackgroundColour(wx.Colour(255, 255, 235))
        label = wx.StaticText(self, label=self._text)
        label.SetForegroundColour(wx.Colour(25, 25, 25))
        label.Wrap(560)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(label, 1, wx.EXPAND | wx.ALL, 10)
        self.SetSizerAndFit(sizer)
        for window in (self, label):
            window.Bind(wx.EVT_LEFT_UP, self._on_left_up)
            window.Bind(wx.EVT_LEFT_DCLICK, self._on_left_dclick)

    def show_at(self, screen_position):
        anchor = wx.Point(screen_position.x + 12, screen_position.y + 12)
        self.Position(anchor, self.GetSize())
        self.Show()
        self.Raise()

    def dismiss(self):
        if self._dismiss_later is not None:
            self._dismiss_later.Stop()
            self._dismiss_later = None
        if self.IsShown():
            self.Hide()
        if self._dismiss_callback:
            self._dismiss_callback(self)
        wx.CallAfter(self.Destroy)

    def _on_left_up(self, event):
        # Delay the single-click action until wx has had time to distinguish
        # it from a native double-click sequence.
        if self._dismiss_later is not None:
            self._dismiss_later.Stop()
        self._dismiss_later = wx.CallLater(600, self.dismiss)
        event.Skip()

    def _on_left_dclick(self, event):
        if self._dismiss_later is not None:
            self._dismiss_later.Stop()
            self._dismiss_later = None
        if self._copy_callback:
            self._copy_callback(self._text)
        self.dismiss()
        event.Skip()


class ZoomableBitmapPanel(wx.ScrolledWindow):
    """A bitmap viewport with wheel zoom and drag pan.

    Matplotlib plots are rendered in background threads and delivered as wx
    bitmaps.  Keeping interaction at this layer avoids using a live matplotlib
    canvas in KiCad's wx event loop.
    """

    MIN_SCALE = 0.25
    MAX_SCALE = 5.0
    PAN_THRESHOLD_PX = 4

    def __init__(
        self, parent, bitmap, hover_probe=None, click_probe=None,
        status_callback=None,
    ):
        super().__init__(parent, style=wx.HSCROLL | wx.VSCROLL | wx.BORDER_NONE)
        self.SetScrollRate(10, 10)
        self._bitmap = bitmap
        self._scale = 1.0
        self._drag_origin = None
        self._view_origin = None
        self._left_down_position = None
        self._was_dragged = False
        self._hover_probe = hover_probe
        self._click_probe = click_probe
        self._status_callback = status_callback
        self._tip_window = None
        self._tip_text = None
        self._suppress_click_on_left_up = False
        self._is_destroyed = False
        # Keep fitting while the notebook/splitter settles. wx can emit an
        # early size event for a narrow temporary page before the maximized
        # dialog completes layout; treating that event as final leaves plots
        # small in a large viewport.
        self._auto_fit_width = True
        self._fit_mode = "WIDTH"
        controls = wx.BoxSizer(wx.HORIZONTAL)
        self._btn_fit_page = wx.Button(self, label="Fit page")
        self._btn_fit_width = wx.Button(self, label="Fit width")
        self._btn_actual_size = wx.Button(self, label="100%")
        self._zoom_label = wx.StaticText(self, label="Zoom 100%")
        for button in (self._btn_fit_page, self._btn_fit_width, self._btn_actual_size):
            controls.Add(button, 0, wx.RIGHT, 5)
        controls.AddStretchSpacer()
        controls.Add(self._zoom_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self._image = wx.StaticBitmap(self)
        self._sizer = wx.BoxSizer(wx.VERTICAL)
        self._sizer.Add(controls, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 5)
        self._sizer.Add(self._image, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 5)
        self.SetSizer(self._sizer)
        self._render()
        self.Bind(wx.EVT_SIZE, self._on_size)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)
        self._image.Bind(wx.EVT_MOUSEWHEEL, self._on_wheel)
        self._btn_fit_page.Bind(wx.EVT_BUTTON, self._on_fit_page)
        self._btn_fit_width.Bind(wx.EVT_BUTTON, self._on_fit_width)
        self._btn_actual_size.Bind(wx.EVT_BUTTON, self._on_actual_size)
        for window in (self, self._image):
            window.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
            window.Bind(wx.EVT_LEFT_UP, self._on_left_up)
            window.Bind(wx.EVT_LEFT_DCLICK, self._on_left_dclick)
            window.Bind(wx.EVT_MOTION, self._on_motion)
            window.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)
            window.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)

    @property
    def scale(self):
        return self._scale

    def _on_size(self, event):
        if self._auto_fit_width:
            wx.CallAfter(self._fit_to_width_if_alive)
        event.Skip()

    def _on_destroy(self, event):
        if event.GetEventObject() is self:
            self._is_destroyed = True
        event.Skip()

    def _fit_to_width_if_alive(self):
        """Ignore delayed size callbacks after wx has started destroying the view."""
        if self._is_destroyed:
            return
        try:
            self._fit_to_width()
        except RuntimeError:
            # wx raises when the native window was deleted between CallAfter
            # scheduling and callback execution.
            self._is_destroyed = True

    def _fit_to_width(self):
        if self._is_destroyed or not self._auto_fit_width or not self._bitmap or not self._bitmap.IsOk():
            return
        width = self.GetClientSize().width
        if width <= 100:
            return
        available = max(1, width - 20)
        scale = available / max(1, self._bitmap.GetWidth())
        if self._fit_mode == "PAGE":
            controls_height = self._btn_fit_page.GetBestSize().height + 15
            available_height = max(1, self.GetClientSize().height - controls_height)
            scale = min(scale, available_height / max(1, self._bitmap.GetHeight()))
        scale = max(self.MIN_SCALE, min(self.MAX_SCALE, scale))
        if not math.isclose(scale, self._scale, rel_tol=1e-3):
            self._scale = scale
            self._render()

    def _on_fit_page(self, _event):
        self._fit_mode = "PAGE"
        self._auto_fit_width = True
        self._fit_to_width()
        self.Scroll(0, 0)

    def _on_fit_width(self, _event):
        self._fit_mode = "WIDTH"
        self._auto_fit_width = True
        self._fit_to_width()
        self.Scroll(0, 0)

    def _on_actual_size(self, _event):
        self._auto_fit_width = False
        self._scale = 1.0
        self._render()
        self.Scroll(0, 0)

    def _render(self):
        if not self._bitmap or not self._bitmap.IsOk():
            return
        width = max(1, int(round(self._bitmap.GetWidth() * self._scale)))
        height = max(1, int(round(self._bitmap.GetHeight() * self._scale)))
        image = self._bitmap.ConvertToImage().Scale(width, height, wx.IMAGE_QUALITY_HIGH)
        self._image.SetBitmap(wx.Bitmap(image))
        self._image.SetMinSize((width, height))
        self._zoom_label.SetLabel(f"Zoom {self._scale * 100:.0f}%")
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
        self._auto_fit_width = False
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
        self._left_down_position = self.ScreenToClient(
            event.GetEventObject().ClientToScreen(event.GetPosition())
        )
        self._was_dragged = False
        if self._scale != 1.0:
            self._drag_origin = self._left_down_position
            self._view_origin = self.GetViewStart()
            if not self.HasCapture():
                self.CaptureMouse()
        event.Skip()

    def _on_left_up(self, event):
        if self._suppress_click_on_left_up:
            self._suppress_click_on_left_up = False
        elif not self._was_dragged:
            self._show_click_probe(event)
        self._finish_drag()
        event.Skip()

    def _on_capture_lost(self, event):
        self._finish_drag()
        event.Skip()

    def _finish_drag(self):
        self._drag_origin = self._view_origin = None
        self._left_down_position = None
        self._was_dragged = False
        if self.HasCapture():
            self.ReleaseMouse()

    def _update_hover_readout(self, event):
        if self._hover_probe is None:
            return
        screen_position = event.GetEventObject().ClientToScreen(event.GetPosition())
        image_position = self._image.ScreenToClient(screen_position)
        reading = self._hover_probe.sample(
            image_position.x / self._scale,
            image_position.y / self._scale,
            self._bitmap.GetWidth(),
            self._bitmap.GetHeight(),
        )
        if reading is None:
            self._set_status("")
            return
        self._set_status(reading.label())

    def _set_status(self, text):
        if self._status_callback:
            self._status_callback(text or "")

    def _probe_at_event(self, probe, event):
        if probe is None:
            return None
        screen_position = event.GetEventObject().ClientToScreen(event.GetPosition())
        image_position = self._image.ScreenToClient(screen_position)
        return probe.sample(
            image_position.x / self._scale,
            image_position.y / self._scale,
            self._bitmap.GetWidth(),
            self._bitmap.GetHeight(),
        )

    def _show_click_probe(self, event):
        reading = self._probe_at_event(self._click_probe, event)
        if reading is None:
            return
        text = reading.label()
        if self._tip_window is not None:
            same_observation = text == self._tip_text
            self._close_probe_popup()
            if same_observation:
                return
        self._set_status(text.splitlines()[0])
        if hasattr(wx, "PopupWindow"):
            self._tip_text = text
            self._tip_window = ProbePopup(
                self, text, dismiss_callback=self._on_probe_popup_dismissed,
                copy_callback=self._copy_probe_text,
            )
            screen_position = event.GetEventObject().ClientToScreen(event.GetPosition())
            self._tip_window.show_at(screen_position)
        else:
            wx.MessageBox(text, "EMI/EMC Observation", wx.OK | wx.ICON_INFORMATION)

    def _on_left_dclick(self, event):
        reading = self._probe_at_event(self._click_probe, event)
        if reading is not None:
            self._suppress_click_on_left_up = True
            self._copy_probe_text(reading.label())
            self._close_probe_popup()
        event.Skip()

    def _on_probe_popup_dismissed(self, popup):
        if popup is self._tip_window:
            self._tip_window = None
            self._tip_text = None

    def _close_probe_popup(self):
        popup = self._tip_window
        self._tip_window = None
        self._tip_text = None
        if popup is not None:
            popup.dismiss()

    def _copy_probe_text(self, text):
        data = wx.TextDataObject(str(text))
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(data)
                if hasattr(wx.TheClipboard, "Flush"):
                    wx.TheClipboard.Flush()
                self._set_status("EMI/EMC observation copied to clipboard")
            finally:
                wx.TheClipboard.Close()

    def _on_leave(self, event):
        # Keep the last valid probe value visible in the shared bottom status
        # bar.  wx may emit leave events while moving between the bitmap and
        # its scrolled parent, which previously erased a freshly sampled value.
        event.Skip()

    def _on_motion(self, event):
        if self._drag_origin is not None and event.LeftIsDown():
            point = self.ScreenToClient(event.GetEventObject().ClientToScreen(event.GetPosition()))
            if math.hypot(point.x - self._drag_origin.x, point.y - self._drag_origin.y) < self.PAN_THRESHOLD_PX:
                event.Skip()
                return
            self._was_dragged = True
            unit_x, unit_y = self.GetScrollPixelsPerUnit()
            self.Scroll(
                max(0, self._view_origin[0] - int((point.x - self._drag_origin.x) / max(unit_x, 1))),
                max(0, self._view_origin[1] - int((point.y - self._drag_origin.y) / max(unit_y, 1))),
            )
            return
        self._update_hover_readout(event)
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
