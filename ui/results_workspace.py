"""Session and project-persistent result pages for Ki-PIDA analyses."""

from pathlib import Path

import wx

from emc_probe import RenderedPointProbe
from i18n import _
from analysis_contract import AnalysisResult, AnalysisStatus, FindingSeverity
from analysis_registry import DEFAULT_ANALYSES
from application.result_detail_presenter import format_finding_detail, format_result_basis
from application.result_filter import filter_findings
from result_history import ProjectResultsHistory
from .interactive_views import TextZoomController, ZoomableBitmapPanel


class AnalysisResultPage(wx.Panel):
    def __init__(self, parent, status_callback=None):
        super().__init__(parent)
        self._status_callback = status_callback
        self._result = None
        self._visible_findings = []
        summary_panel = wx.Panel(self)
        summary_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.status_label = wx.StaticText(summary_panel, label="NO DATA")
        status_font = self.status_label.GetFont()
        status_font.SetWeight(wx.FONTWEIGHT_BOLD)
        self.status_label.SetFont(status_font)
        self.summary_label = wx.StaticText(summary_panel, label="No structured result loaded")
        summary_sizer.Add(self.status_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16)
        summary_sizer.Add(self.summary_label, 1, wx.ALIGN_CENTER_VERTICAL)
        summary_panel.SetSizer(summary_sizer)
        self.basis_label = wx.StaticText(
            self, label="Evidence basis unavailable", style=getattr(wx, "ST_ELLIPSIZE_END", 0),
        )
        self.finding_tools = wx.Panel(self)
        finding_tools_sizer = wx.BoxSizer(wx.HORIZONTAL)
        finding_tools_sizer.Add(
            wx.StaticText(self.finding_tools, label="Findings:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.finding_filter = wx.Choice(self.finding_tools)
        for label in ("All", "Critical / High", "Actionable (Critical–Medium)", "Low / Info"):
            self.finding_filter.Append(label)
        self.finding_filter.SetSelection(0)
        finding_tools_sizer.Add(self.finding_filter, 0, wx.RIGHT, 8)
        finding_tools_sizer.Add(
            wx.StaticText(self.finding_tools, label="Search:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.finding_search = wx.TextCtrl(self.finding_tools)
        self.finding_search.SetHint("rule, text, net, or component")
        finding_tools_sizer.Add(self.finding_search, 1, wx.RIGHT, 8)
        self.finding_count = wx.StaticText(self.finding_tools, label="0 / 0")
        finding_tools_sizer.Add(self.finding_count, 0, wx.ALIGN_CENTER_VERTICAL)
        self.finding_tools.SetSizer(finding_tools_sizer)
        self.finding_tools.Hide()
        self.findings = wx.ListCtrl(
            self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SIMPLE,
        )
        self.findings.InsertColumn(0, "Severity", width=85)
        self.findings.InsertColumn(1, "Rule", width=100)
        self.findings.InsertColumn(2, "Finding", width=680)
        self.findings.InsertColumn(3, "Confidence", width=130)
        self.findings.SetMinSize((-1, 120))
        self.findings.Hide()
        splitter = wx.SplitterWindow(self)
        text_panel = wx.Panel(splitter)
        text_sizer = wx.BoxSizer(wx.VERTICAL)
        detail_bar = wx.BoxSizer(wx.HORIZONTAL)
        detail_bar.Add(
            wx.StaticText(text_panel, label="Detail view:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.detail_view = wx.Choice(text_panel)
        for label in ("Report", "Selected finding", "Evidence and limits"):
            self.detail_view.Append(label)
        self.detail_view.SetSelection(0)
        detail_bar.Add(self.detail_view, 0)
        detail_bar.AddStretchSpacer()
        text_sizer.Add(detail_bar, 0, wx.EXPAND | wx.ALL, 5)
        self.text = wx.TextCtrl(text_panel, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.text_zoom = TextZoomController(self.text)
        # install_navigation() walks the completed dialog later. Mark this
        # control so it does not bind a second native zoom handler.
        self.text._kipida_text_zoom = self.text_zoom
        text_sizer.Add(self.text, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        text_panel.SetSizer(text_sizer)
        plots_panel = wx.Panel(splitter)
        plots_sizer = wx.BoxSizer(wx.VERTICAL)
        plot_bar = wx.BoxSizer(wx.HORIZONTAL)
        plot_bar.Add(
            wx.StaticText(plots_panel, label="Plot view:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.plot_view = wx.Choice(plots_panel)
        plot_bar.Add(self.plot_view, 0)
        plot_bar.AddStretchSpacer()
        plots_sizer.Add(plot_bar, 0, wx.EXPAND | wx.ALL, 5)
        self.plots_host = wx.Panel(plots_panel)
        self.plots_host_sizer = wx.BoxSizer(wx.VERTICAL)
        self.plots_host.SetSizer(self.plots_host_sizer)
        plots_sizer.Add(self.plots_host, 1, wx.EXPAND)
        plots_panel.SetSizer(plots_sizer)
        splitter.SplitHorizontally(text_panel, plots_panel, 220)
        splitter.SetMinimumPaneSize(50)
        splitter.SetSashGravity(0.38)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(summary_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(self.basis_label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(self.finding_tools, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        sizer.Add(self.findings, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(splitter, 1, wx.EXPAND)
        self.SetSizer(sizer)
        self.finding_filter.Bind(wx.EVT_CHOICE, self._on_findings_filter_changed)
        self.finding_search.Bind(wx.EVT_TEXT, self._on_findings_filter_changed)
        self.findings.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_finding_selected)
        self.detail_view.Bind(wx.EVT_CHOICE, self._on_detail_view_changed)
        self.plot_view.Bind(wx.EVT_CHOICE, self._on_plot_view_changed)
        self._report_text = ""
        self._finding_detail_text = format_finding_detail(None)
        self._basis_detail_text = format_result_basis(None)
        self._plot_views = []

    @property
    def result(self):
        """The structured AnalysisResult currently shown, or None."""
        return self._result

    def set_result(self, result):
        self._result = result
        self.findings.DeleteAllItems()
        if result is None:
            self.status_label.SetLabel("NO DATA")
            self.summary_label.SetLabel("No structured result loaded")
            self.basis_label.SetLabel("Evidence basis unavailable")
            self.basis_label.SetToolTip("")
            self._finding_detail_text = format_finding_detail(None)
            self._basis_detail_text = format_result_basis(None)
            self.finding_tools.Hide()
            self.findings.Hide()
            self.detail_view.SetSelection(0)
            self._show_selected_detail()
            self.Layout()
            return
        counts = result.severity_counts
        is_legacy = bool(result.summary.get("legacy_report"))
        important = counts[FindingSeverity.CRITICAL.value] + counts[FindingSeverity.HIGH.value]
        details = []
        if is_legacy:
            details.extend([
                "Legacy format",
                "structured metrics unavailable",
                f"{len(result.artifacts)} plot(s)",
            ])
        else:
            details.append(f"{len(result.findings)} finding(s)")
        if important:
            details.append(f"{important} critical/high")
        if result.elapsed_seconds:
            details.append(f"{result.elapsed_seconds:.3f} s")
        for metric in result.metrics[:3]:
            value = metric.value
            if isinstance(value, float):
                value = f"{value:.5g}"
            details.append(f"{metric.label}: {value}{(' ' + metric.unit) if metric.unit else ''}")
        self.status_label.SetLabel(
            "LEGACY" if is_legacy else result.status.value.replace("_", " ")
        )
        self.summary_label.SetLabel(" · ".join(details))
        if is_legacy:
            basis_summary = "Evidence basis: legacy report · structured provenance unavailable"
        else:
            basis_summary = (
                f"Evidence basis: {len(result.provenance)} source(s)"
                f" · {len(result.limitations)} model limitation(s)"
            )
        basis_details = []
        basis_details.extend(
            f"{item.source}: {item.detail}" for item in result.provenance
        )
        basis_details.extend(f"Limitation: {item}" for item in result.limitations)
        self.basis_label.SetLabel(basis_summary)
        self.basis_label.SetToolTip("\n".join(basis_details))
        self._basis_detail_text = format_result_basis(result)
        self._finding_detail_text = format_finding_detail(None)
        self.detail_view.SetSelection(0)
        self._show_selected_detail()
        self._refresh_findings()
        self.Layout()

    def _selected_severity_filter(self):
        selection = self.finding_filter.GetSelection()
        return self.finding_filter.GetString(selection) if selection != wx.NOT_FOUND else "All"

    def _refresh_findings(self):
        self.findings.DeleteAllItems()
        all_findings = list(getattr(self._result, "findings", ()) or ())
        self._visible_findings = filter_findings(
            all_findings,
            severity_filter=self._selected_severity_filter(),
            query=self.finding_search.GetValue(),
        )
        colors = {
            FindingSeverity.CRITICAL: wx.Colour(170, 0, 0),
            FindingSeverity.HIGH: wx.Colour(190, 55, 0),
            FindingSeverity.MEDIUM: wx.Colour(145, 105, 0),
            FindingSeverity.LOW: wx.Colour(55, 85, 125),
            FindingSeverity.INFO: wx.Colour(70, 70, 70),
        }
        for finding in self._visible_findings:
            row = self.findings.InsertItem(self.findings.GetItemCount(), finding.severity.value)
            self.findings.SetItem(row, 1, finding.rule_id)
            self.findings.SetItem(row, 2, finding.title)
            self.findings.SetItem(row, 3, finding.confidence.value.replace("_", " "))
            self.findings.SetItemTextColour(row, colors[finding.severity])
        self.finding_count.SetLabel(f"{len(self._visible_findings)} / {len(all_findings)}")
        self.finding_tools.Show(bool(all_findings))
        self.findings.Show(bool(all_findings))
        if all_findings and not self._visible_findings:
            self._finding_detail_text = "No finding matches the current filters."
            if self.detail_view.GetSelection() == 1:
                self._show_selected_detail()
        self.Layout()

    def _on_findings_filter_changed(self, _event):
        self._refresh_findings()

    def _on_finding_selected(self, event):
        index = event.GetIndex()
        if 0 <= index < len(self._visible_findings):
            self._finding_detail_text = format_finding_detail(self._visible_findings[index])
            self.detail_view.SetSelection(1)
            self._show_selected_detail()

    def _on_detail_view_changed(self, _event):
        self._show_selected_detail()

    def _show_selected_detail(self):
        selection = self.detail_view.GetSelection()
        content = {
            1: self._finding_detail_text,
            2: self._basis_detail_text,
        }.get(selection, self._report_text)
        if self.text.GetValue() != content:
            self.text.SetValue(content)

    def set_report(self, text):
        self._report_text = text or ""
        if self.detail_view.GetSelection() == 0:
            self._show_selected_detail()

    def set_plots(self, plots):
        self.plots_host.Freeze()
        try:
            self._clear_plot_views()
            for entry in plots:
                title, bitmap = entry[:2]
                hover_probe = entry[2] if len(entry) > 2 else None
                click_probe = entry[3] if len(entry) > 3 else None
                if bitmap and bitmap.IsOk():
                    view = ZoomableBitmapPanel(
                        self.plots_host, bitmap, hover_probe=hover_probe,
                        click_probe=click_probe, status_callback=self._status_callback,
                    )
                    self._plot_views.append(view)
                    self.plot_view.Append(title)
                    self.plots_host_sizer.Add(view, 1, wx.EXPAND)
                    view.Hide()
            if self._plot_views:
                self.plot_view.SetSelection(0)
                self.plot_view.Enable(True)
                self._show_selected_plot()
            else:
                self.plot_view.Enable(False)
        finally:
            self.plots_host.Thaw()
            self.plots_host.Layout()

    def _clear_plot_views(self):
        self.plot_view.Clear()
        for view in self._plot_views:
            self.plots_host_sizer.Detach(view)
            view.Destroy()
        self._plot_views = []

    def _on_plot_view_changed(self, _event):
        self._show_selected_plot()

    def _show_selected_plot(self):
        selection = self.plot_view.GetSelection()
        for index, view in enumerate(self._plot_views):
            view.Show(index == selection)
        self.plots_host.Layout()
        if 0 <= selection < len(self._plot_views):
            wx.CallAfter(self._plot_views[selection]._fit_to_width_if_alive)

    def show_rendering(self, message):
        self.plots_host.Freeze()
        self._clear_plot_views()
        page = wx.Panel(self.plots_host)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.AddStretchSpacer()
        sizer.Add(wx.StaticText(page, label=message), 0, wx.ALIGN_CENTER | wx.ALL, 12)
        sizer.AddStretchSpacer()
        page.SetSizer(sizer)
        self._plot_views.append(page)
        self.plots_host_sizer.Add(page, 1, wx.EXPAND)
        self.plot_view.Append("Rendering")
        self.plot_view.SetSelection(0)
        self.plot_view.Enable(False)
        self.plots_host.Thaw()
        self.plots_host.Layout()


class ResultsWorkspace(wx.Panel):
    """Result notebook with project-persistent, user-managed history."""

    TITLES = {item.analysis_id: _(item.title) for item in DEFAULT_ANALYSES.all()}

    def __init__(
        self, parent, history_directory=None, log_callback=None,
        interaction_status_callback=None,
    ):
        super().__init__(parent)
        self._pages = {}
        self._log_callback = log_callback
        self._interaction_status_callback = interaction_status_callback
        self._history = ProjectResultsHistory(history_directory)
        self._active_entries = {}
        self._displayed_entries = {}
        self._analysis_page_ids = []

        history_bar = wx.BoxSizer(wx.HORIZONTAL)
        history_bar.Add(wx.StaticText(self, label="Show:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.history_filter = wx.Choice(self)
        self._history_filters = [
            ("Latest per analysis", "LATEST"),
            ("All saved results", "ALL"),
        ] + [
            (item.title, item.analysis_id) for item in DEFAULT_ANALYSES.all()
        ]
        for label, _value in self._history_filters:
            self.history_filter.Append(label)
        self.history_filter.SetSelection(0)
        history_bar.Add(self.history_filter, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        history_bar.Add(
            wx.StaticText(self, label="Analysis:"),
            0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6,
        )
        self.analysis_choice = wx.Choice(self)
        self.analysis_choice.Enable(False)
        history_bar.Add(self.analysis_choice, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        history_bar.Add(wx.StaticText(self, label="Saved results:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.history_choice = wx.Choice(self)
        history_bar.Add(self.history_choice, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.btn_delete = wx.Button(self, label="Delete selected")
        self.btn_clear = wx.Button(self, label="Clear history")
        history_bar.Add(self.btn_delete, 0, wx.RIGHT, 4)
        history_bar.Add(self.btn_clear, 0)

        self.notebook = wx.Simplebook(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(history_bar, 0, wx.EXPAND | wx.ALL, 4)
        sizer.Add(self.notebook, 1, wx.EXPAND)
        self.SetSizer(sizer)
        self.history_filter.Bind(wx.EVT_CHOICE, self._on_history_filter)
        self.analysis_choice.Bind(wx.EVT_CHOICE, self._on_analysis_selected)
        self.history_choice.Bind(wx.EVT_CHOICE, self._on_history_selected)
        self.btn_delete.Bind(wx.EVT_BUTTON, self._on_delete_selected)
        self.btn_clear.Bind(wx.EVT_BUTTON, self._on_clear_history)
        self.refresh_history()

    def _log(self, message):
        if self._log_callback:
            self._log_callback(message)

    @staticmethod
    def _entry_label(entry):
        created = str(entry.get("created_at", "")).replace("T", " ")
        title = str(entry.get("title") or entry.get("analysis_id") or "Result")
        status = str(entry.get("status") or "LEGACY")
        return _("{created} — {title} — {status}").format(
            created=created, title=_(title), status=_(status),
        )

    def _selected_history_filter(self):
        selection = self.history_filter.GetSelection()
        if selection == wx.NOT_FOUND or selection >= len(self._history_filters):
            return "LATEST"
        return self._history_filters[selection][1]

    def _on_history_filter(self, _event):
        self.refresh_history()

    def refresh_history(self, select_entry=None):
        selected_filter = self._selected_history_filter()
        if selected_filter == "LATEST":
            entries = self._history.entries(latest_per_analysis=True)
        elif selected_filter == "ALL":
            entries = self._history.entries()
        else:
            entries = self._history.entries(analysis_id=selected_filter)
        self._history_entries = entries
        self.history_choice.Clear()
        for entry in entries:
            self.history_choice.Append(self._entry_label(entry))
        if entries:
            selected = 0
            if select_entry is not None:
                for index, entry in enumerate(entries):
                    if Path(entry["directory"]) == Path(select_entry.get("directory", "")):
                        selected = index
                        break
            self.history_choice.SetSelection(selected)
        self.btn_delete.Enable(bool(entries))
        self.btn_clear.Enable(bool(entries))

    def _on_history_selected(self, _event):
        selection = self.history_choice.GetSelection()
        if selection == wx.NOT_FOUND or selection >= len(self._history_entries):
            return
        entry = self._history_entries[selection]
        try:
            report, plots = self._history.load(entry)
            result = self._history.load_result(entry)
        except (OSError, ValueError, TypeError) as exc:
            wx.MessageBox(str(exc), "Result History Error", wx.OK | wx.ICON_ERROR)
            return
        analysis_id = str(entry["analysis_id"])
        page = self.page_for(analysis_id)
        page.set_result(result)
        page.set_report(report)
        page.set_plots(plots)
        self._displayed_entries[analysis_id] = entry
        self._select_analysis_page(analysis_id)
        self._log(f"Loaded saved {entry.get('title', analysis_id)} result from history.")

    def _on_analysis_selected(self, _event):
        selection = self.analysis_choice.GetSelection()
        if selection == wx.NOT_FOUND or selection >= len(self._analysis_page_ids):
            return
        analysis_id = self._analysis_page_ids[selection]
        self.notebook.SetSelection(selection)
        self._sync_history_to_analysis(analysis_id)

    def _select_analysis_page(self, analysis_id):
        if analysis_id not in self._analysis_page_ids:
            return
        selection = self._analysis_page_ids.index(analysis_id)
        self.notebook.SetSelection(selection)
        self.analysis_choice.SetSelection(selection)
        self._sync_history_to_analysis(analysis_id)

    def _sync_history_to_analysis(self, analysis_id):
        displayed = self._displayed_entries.get(analysis_id)
        if displayed is not None:
            displayed_path = Path(displayed.get("directory", ""))
            for index, entry in enumerate(getattr(self, "_history_entries", ())):
                if Path(entry.get("directory", "")) == displayed_path:
                    self.history_choice.SetSelection(index)
                    break

    def _on_delete_selected(self, _event):
        selection = self.history_choice.GetSelection()
        if selection == wx.NOT_FOUND or selection >= len(self._history_entries):
            return
        entry = self._history_entries[selection]
        answer = wx.MessageBox(
            f"Delete saved result '{self._entry_label(entry)}'?", "Delete saved result",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        if answer == wx.YES and self._history.delete(entry):
            self._log("Deleted saved Ki-PIDA result.")
            self.refresh_history()

    def _on_clear_history(self, _event):
        answer = wx.MessageBox(
            "Delete every saved Ki-PIDA result for this PCB?", "Clear result history",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        if answer == wx.YES:
            deleted = self._history.clear()
            self._log(f"Deleted {deleted} saved Ki-PIDA result(s).")
            self.refresh_history()

    def page_for(self, analysis_id):
        page = self._pages.get(analysis_id)
        if page is None:
            page = AnalysisResultPage(
                self.notebook, status_callback=self._interaction_status_callback,
            )
            self._pages[analysis_id] = page
            self._analysis_page_ids.append(analysis_id)
            self.notebook.AddPage(page, self.TITLES.get(analysis_id, analysis_id))
            self.analysis_choice.Append(self.TITLES.get(analysis_id, analysis_id))
            self.analysis_choice.Enable(True)
        return page

    def publish(self, analysis_id, report, plots=None, select=True, result=None):
        page = self.page_for(analysis_id)
        title = self.TITLES.get(analysis_id, analysis_id)
        result = result or AnalysisResult.legacy_report(
            analysis_id, title, report,
            [entry[0] for entry in (plots or []) if entry],
        )
        page.set_result(result)
        page.set_report(report)
        if plots is not None:
            page.set_plots(plots)
        if select:
            self._select_analysis_page(analysis_id)
        entry = self._history.save(
            analysis_id, title, report, plots, result=result,
        )
        if entry is not None:
            self._active_entries[analysis_id] = entry
            self._displayed_entries[analysis_id] = entry
            self.refresh_history(select_entry=entry)
            self._log(f"Saved {self.TITLES.get(analysis_id, analysis_id)} result history entry.")
        return page

    def session_results(self):
        """Every structured result published in this session, in page order.

        This is what the consolidated report aggregates: the user runs
        whichever analyses their board and configuration support, and each one
        leaves its AnalysisResult here. Domains never run are simply absent --
        campaign scoring treats an absent domain as NO_DATA, never as passing.
        """
        results = []
        for analysis_id in self._analysis_page_ids:
            page = self._pages.get(analysis_id)
            result = getattr(page, "result", None) if page is not None else None
            if result is not None:
                results.append(result)
        return results

    def update_history_plots(self, analysis_id, plots):
        entry = self._active_entries.get(analysis_id)
        if entry is None:
            return
        self._history.update_plots(entry, plots)
        self.refresh_history(select_entry=entry)
