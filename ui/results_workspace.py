"""Session and project-persistent result pages for Ki-PIDA analyses."""

from datetime import datetime
import json
from pathlib import Path
import re
import shutil

import wx

from emc_probe import RenderedPointProbe
from .interactive_views import TextZoomController, ZoomableBitmapPanel


class AnalysisResultPage(wx.Panel):
    def __init__(self, parent, status_callback=None):
        super().__init__(parent)
        self._status_callback = status_callback
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
            for entry in plots:
                title, bitmap = entry[:2]
                hover_probe = entry[2] if len(entry) > 2 else None
                click_probe = entry[3] if len(entry) > 3 else None
                if bitmap and bitmap.IsOk():
                    self.plots.AddPage(
                        ZoomableBitmapPanel(
                            self.plots, bitmap, hover_probe=hover_probe,
                            click_probe=click_probe, status_callback=self._status_callback,
                        ), title,
                    )
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


class ProjectResultsHistory:
    """Small on-disk index of reports and plot PNGs for one KiCad board."""

    INDEX_NAME = "result.json"

    def __init__(self, directory):
        self.directory = Path(directory) if directory else None

    @staticmethod
    def _safe_name(value):
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "result")).strip("_.") or "result"

    def _ensure_directory(self):
        if self.directory is None:
            return None
        self.directory.mkdir(parents=True, exist_ok=True)
        return self.directory

    def entries(self):
        if self.directory is None or not self.directory.is_dir():
            return []
        entries = []
        for index_path in self.directory.glob(f"*/{self.INDEX_NAME}"):
            try:
                metadata = json.loads(index_path.read_text(encoding="utf-8"))
                if not isinstance(metadata, dict) or not metadata.get("analysis_id"):
                    continue
                metadata["directory"] = index_path.parent
                entries.append(metadata)
            except (OSError, ValueError, TypeError):
                continue
        return sorted(entries, key=lambda item: str(item.get("created_at", "")), reverse=True)

    def save(self, analysis_id, title, report, plots=None):
        root = self._ensure_directory()
        if root is None:
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = f"{stamp}-{self._safe_name(analysis_id)}"
        entry_dir = root / base
        suffix = 1
        while entry_dir.exists():
            entry_dir = root / f"{base}-{suffix}"
            suffix += 1
        entry_dir.mkdir()
        metadata = {
            "version": 1,
            "analysis_id": str(analysis_id),
            "title": str(title),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "report_file": "report.txt",
            "plots": [],
        }
        (entry_dir / metadata["report_file"]).write_text(str(report), encoding="utf-8")
        metadata["directory"] = entry_dir
        self.update_plots(metadata, plots or [])
        return metadata

    def update_plots(self, metadata, plots):
        if not metadata or not metadata.get("directory"):
            return
        entry_dir = Path(metadata["directory"])
        for previous in entry_dir.glob("plot-*.png"):
            try:
                previous.unlink()
            except OSError:
                pass
        saved = []
        for index, entry in enumerate(plots or [], start=1):
            title, bitmap = entry[:2]
            click_probe = entry[3] if len(entry) > 3 else None
            if bitmap is None or not bitmap.IsOk():
                continue
            filename = f"plot-{index:02d}-{self._safe_name(title)}.png"
            image = bitmap.ConvertToImage()
            if image.SaveFile(str(entry_dir / filename), wx.BITMAP_TYPE_PNG):
                plot_metadata = {"title": str(title), "file": filename}
                if click_probe is not None and hasattr(click_probe, "to_dict"):
                    plot_metadata["click_probe"] = click_probe.to_dict()
                saved.append(plot_metadata)
        metadata["plots"] = saved
        serializable = {key: value for key, value in metadata.items() if key != "directory"}
        (entry_dir / self.INDEX_NAME).write_text(
            json.dumps(serializable, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )

    def load(self, metadata):
        entry_dir = Path(metadata["directory"])
        report_path = entry_dir / str(metadata.get("report_file", "report.txt"))
        report = report_path.read_text(encoding="utf-8")
        plots = []
        for plot in metadata.get("plots", []):
            if not isinstance(plot, dict):
                continue
            image = wx.Image(str(entry_dir / str(plot.get("file", ""))))
            if image.IsOk():
                click_probe = None
                if isinstance(plot.get("click_probe"), dict):
                    click_probe = RenderedPointProbe.from_dict(plot["click_probe"])
                plots.append((
                    str(plot.get("title", "Plot")), wx.Bitmap(image), None, click_probe,
                ))
        return report, plots

    def delete(self, metadata):
        if not metadata or self.directory is None:
            return False
        target = Path(metadata.get("directory", "")).resolve()
        root = self.directory.resolve()
        if target.parent != root:
            return False
        shutil.rmtree(target)
        return True

    def clear(self):
        deleted = 0
        for entry in self.entries():
            if self.delete(entry):
                deleted += 1
        return deleted


class ResultsWorkspace(wx.Panel):
    """Result notebook with project-persistent, user-managed history."""

    TITLES = {
        "DC": "DC Power",
        "AC": "AC Impedance",
        "DIFFERENTIAL": "Differential Pairs",
        "EMC": "EMI / EMC",
        "THERMAL": "3D Thermal",
        "CFD": "Enclosure CFD",
        "DEBUG": "Debug",
    }

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

        history_bar = wx.BoxSizer(wx.HORIZONTAL)
        history_bar.Add(wx.StaticText(self, label="Saved results:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.history_choice = wx.Choice(self)
        history_bar.Add(self.history_choice, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.btn_delete = wx.Button(self, label="Delete selected")
        self.btn_clear = wx.Button(self, label="Clear history")
        history_bar.Add(self.btn_delete, 0, wx.RIGHT, 4)
        history_bar.Add(self.btn_clear, 0)

        self.notebook = wx.Notebook(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(history_bar, 0, wx.EXPAND | wx.ALL, 4)
        sizer.Add(self.notebook, 1, wx.EXPAND)
        self.SetSizer(sizer)
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
        return f"{created} — {title}"

    def refresh_history(self, select_entry=None):
        entries = self._history.entries()
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
        except OSError as exc:
            wx.MessageBox(str(exc), "Result History Error", wx.OK | wx.ICON_ERROR)
            return
        analysis_id = str(entry["analysis_id"])
        page = self.page_for(analysis_id)
        page.set_report(report)
        page.set_plots(plots)
        self.notebook.SetSelection(self.notebook.FindPage(page))
        self._log(f"Loaded saved {entry.get('title', analysis_id)} result from history.")

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
            self.notebook.AddPage(page, self.TITLES.get(analysis_id, analysis_id))
        return page

    def publish(self, analysis_id, report, plots=None, select=True):
        page = self.page_for(analysis_id)
        page.set_report(report)
        if plots is not None:
            page.set_plots(plots)
        if select:
            self.notebook.SetSelection(self.notebook.FindPage(page))
        entry = self._history.save(
            analysis_id, self.TITLES.get(analysis_id, analysis_id), report, plots,
        )
        if entry is not None:
            self._active_entries[analysis_id] = entry
            self.refresh_history(select_entry=entry)
            self._log(f"Saved {self.TITLES.get(analysis_id, analysis_id)} result history entry.")
        return page

    def update_history_plots(self, analysis_id, plots):
        entry = self._active_entries.get(analysis_id)
        if entry is None:
            return
        self._history.update_plots(entry, plots)
        self.refresh_history(select_entry=entry)
