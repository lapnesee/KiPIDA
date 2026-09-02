"""Sidebar navigation for the top-level Ki-PIDA workspaces."""

from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Sequence

import wx


@dataclass(frozen=True)
class WorkspaceEntry:
    group: str
    title: str
    page_index: int
    description: str = ""


def build_workspace_entries(pages) -> tuple:
    """Build the canonical, grouped workspace information architecture."""
    return (
        WorkspaceEntry("Project", "Power Tree & DC", pages["config"],
                       "Configure rails, sources, loads, mesh, and DC limits."),
        WorkspaceEntry("Power Integrity", "AC Impedance", pages["ac"],
                       "Sweep rail impedance and optimize decoupling candidates."),
        WorkspaceEntry("Signal Integrity", "Differential Pairs", pages["differential"],
                       "Check impedance, reference planes, geometry, and length matching."),
        WorkspaceEntry("EMI / EMC", "Pre-compliance", pages["emc"],
                       "Inspect return paths, emissions risks, and field estimates."),
        WorkspaceEntry("Thermal", "3D Thermal", pages["thermal"],
                       "Solve PCB temperature and electro-thermal coupling."),
        WorkspaceEntry("Thermal", "Enclosure CFD", pages["cfd"],
                       "Model enclosure airflow and conjugate heat transfer."),
        WorkspaceEntry("Results", "Analysis Results", pages["results"],
                       "Review findings, metrics, plots, and saved campaigns."),
        WorkspaceEntry("Application", "Runtime & Acceleration", pages["runtime"],
                       "Configure CPU, CUDA, memory, and optional backends."),
        WorkspaceEntry("Application", "Diagnostics", pages["log"],
                       "Inspect execution logs and backend diagnostics."),
    )


class WorkspaceNavigator(wx.Panel):
    """Grouped tree navigation that exposes only selectable leaf workspaces."""

    def __init__(self, parent, entries: Iterable[WorkspaceEntry], on_select: Callable):
        super().__init__(parent)
        self.SetMinSize((220, -1))
        self._on_select = on_select
        self._items_by_page: Dict[int, object] = {}
        self._entries_by_page: Dict[int, WorkspaceEntry] = {}
        self._syncing = False

        title = wx.StaticText(self, label="Ki-PIDA")
        font = title.GetFont()
        font.SetPointSize(font.GetPointSize() + 3)
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        title.SetFont(font)
        subtitle = wx.StaticText(self, label="Engineering workspace")

        self.tree = wx.TreeCtrl(
            self, style=wx.TR_HIDE_ROOT | wx.TR_HAS_BUTTONS | wx.TR_SINGLE | wx.BORDER_NONE,
        )
        root = self.tree.AddRoot("Ki-PIDA")
        groups: Dict[str, list] = {}
        for entry in entries:
            if entry.page_index in self._entries_by_page:
                raise ValueError(f"Duplicate workspace page index: {entry.page_index}")
            self._entries_by_page[entry.page_index] = entry
            groups.setdefault(entry.group, []).append(entry)
        for group, children in groups.items():
            group_item = self.tree.AppendItem(root, group)
            self.tree.SetItemBold(group_item, True)
            for entry in children:
                item = self.tree.AppendItem(group_item, entry.title)
                self.tree.SetItemData(item, entry.page_index)
                self._items_by_page[entry.page_index] = item
            self.tree.Expand(group_item)

        self.tree.Bind(wx.EVT_TREE_SEL_CHANGED, self._on_tree_selection)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(title, 0, wx.LEFT | wx.RIGHT | wx.TOP, 14)
        sizer.Add(subtitle, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)
        sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(self.tree, 1, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)

    def entry_for_page(self, page_index: int) -> WorkspaceEntry:
        return self._entries_by_page[page_index]

    def select_page(self, page_index: int) -> None:
        try:
            deleting = self.IsBeingDeleted()
        except RuntimeError:
            return
        if deleting:
            return
        item = self._items_by_page.get(page_index)
        try:
            if item is None or self.tree.GetSelection() == item:
                return
        except RuntimeError:
            return
        self._syncing = True
        try:
            self.tree.SelectItem(item)
            self.tree.EnsureVisible(item)
        finally:
            self._syncing = False

    def _on_tree_selection(self, event) -> None:
        try:
            deleting = self.IsBeingDeleted()
        except RuntimeError:
            return
        if deleting:
            return
        try:
            item = event.GetItem()
            page_index = self.tree.GetItemData(item)
        except RuntimeError:
            return
        if page_index is None:
            child, _cookie = self.tree.GetFirstChild(item)
            if child.IsOk():
                self.tree.SelectItem(child)
            return
        if not self._syncing:
            self._on_select(self._entries_by_page[int(page_index)])
