"""Session-scoped result pages that keep each analysis independent."""

import wx

from .interactive_views import TextZoomController, ZoomableBitmapPanel


class AnalysisResultPage(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        splitter = wx.SplitterWindow(self)
        text_panel = wx.Panel(splitter)
        text_sizer = wx.BoxSizer(wx.VERTICAL)
        self.text = wx.TextCtrl(text_panel, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.text_zoom = TextZoomController(self.text)
        text_sizer.Add(self.text, 1, wx.EXPAND | wx.ALL, 5)
        text_panel.SetSizer(text_sizer)
        plots_panel = wx.Panel(splitter)
        plots_sizer = wx.BoxSizer(wx.VERTICAL)
        self.plots = wx.Notebook(plots_panel)
        plots_sizer.Add(self.plots, 1, wx.EXPAND)
        plots_panel.SetSizer(plots_sizer)
        splitter.SplitHorizontally(text_panel, plots_panel, 140)
        splitter.SetMinimumPaneSize(50)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(splitter, 1, wx.EXPAND)
        self.SetSizer(sizer)

    def set_report(self, text):
        self.text.SetValue(text)

    def set_plots(self, plots):
        self.plots.Freeze()
        try:
            self.plots.DeleteAllPages()
            for title, bitmap in plots:
                if bitmap and bitmap.IsOk():
                    self.plots.AddPage(ZoomableBitmapPanel(self.plots, bitmap), title)
        finally:
            self.plots.Thaw()

    def show_rendering(self, message):
        self.plots.DeleteAllPages()
        page = wx.Panel(self.plots)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.AddStretchSpacer()
        sizer.Add(wx.StaticText(page, label=message), 0, wx.ALIGN_CENTER | wx.ALL, 12)
        sizer.AddStretchSpacer()
        page.SetSizer(sizer)
        self.plots.AddPage(page, "Rendering")


class ResultsWorkspace(wx.Notebook):
    """A result notebook keyed by analysis type, retained for this session."""

    TITLES = {
        "DC": "DC Power",
        "AC": "AC Impedance",
        "DIFFERENTIAL": "Differential Pairs",
        "THERMAL": "3D Thermal",
        "CFD": "Enclosure CFD",
        "DEBUG": "Debug",
    }

    def __init__(self, parent):
        super().__init__(parent)
        self._pages = {}

    def page_for(self, analysis_id):
        page = self._pages.get(analysis_id)
        if page is None:
            page = AnalysisResultPage(self)
            self._pages[analysis_id] = page
            self.AddPage(page, self.TITLES.get(analysis_id, analysis_id))
        return page

    def publish(self, analysis_id, report, plots=None, select=True):
        page = self.page_for(analysis_id)
        page.set_report(report)
        if plots is not None:
            page.set_plots(plots)
        if select:
            self.SetSelection(self.FindPage(page))
        return page
