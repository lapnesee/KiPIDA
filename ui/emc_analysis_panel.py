"""wxPython configuration surface for Phase 8 EMI/EMC pre-compliance."""

import wx

try:
    from emc_analyzer import EMCSourceDiscoverer
    from extractor import GeometryExtractor
    from models import EMCAnalysisSettings, EMCSignalSource
except (ImportError, ValueError):
    from ..emc_analyzer import EMCSourceDiscoverer
    from ..extractor import GeometryExtractor
    from ..models import EMCAnalysisSettings, EMCSignalSource


STANDARD_CHOICES = [
    ("CISPR 32 Class B", "CISPR_32_CLASS_B"),
    ("CISPR 32 Class A", "CISPR_32_CLASS_A"),
    ("FCC Part 15 Class B", "FCC_PART_15_CLASS_B"),
    ("FCC Part 15 Class A", "FCC_PART_15_CLASS_A"),
    ("CISPR 25 Class 5", "CISPR_25_CLASS_5"),
    ("MIL-STD-461G RE102", "MIL_STD_461G_RE102"),
]
CATEGORIES = [
    ("Ground planes", "GROUND"), ("Decoupling", "DECOUPLING"),
    ("I/O filtering", "IO"), ("Switching", "SWITCHING"),
    ("Clocks", "CLOCK"), ("Stackup", "STACKUP"),
    ("Differential pairs", "DIFFERENTIAL"), ("Board edge", "BOARD_EDGE"),
    ("PDN", "PDN"), ("Return paths", "RETURN_PATH"),
    ("Crosstalk", "CROSSTALK"), ("ESD", "ESD"),
    ("Shielding", "SHIELDING"), ("Via stitching", "STITCHING"),
    ("Thermal interaction", "THERMAL"), ("Emission estimates", "EMISSIONS"),
]


class EMCSourceDialog(wx.Dialog):
    def __init__(self, parent, source=None):
        super().__init__(parent, title="Edit EMI/EMC Source" if source else "Add EMI/EMC Source")
        grid = wx.FlexGridSizer(cols=2, hgap=8, vgap=8)
        grid.AddGrowableCol(1, 1)
        self.name = wx.TextCtrl(self)
        self.net = wx.TextCtrl(self)
        self.kind = wx.Choice(self, choices=["DIGITAL", "CLOCK", "SWITCHING", "DIFFERENTIAL", "EXTERNAL"])
        self.kind.SetStringSelection("DIGITAL")
        self.frequency = wx.TextCtrl(self, value="25")
        self.rise = wx.TextCtrl(self, value="2")
        self.external = wx.CheckBox(self, label="Connected to an external cable")
        self.cable = wx.TextCtrl(self, value="0")
        for label, control in (
            ("Name:", self.name), ("Net:", self.net), ("Type:", self.kind),
            ("Fundamental (MHz):", self.frequency), ("Rise time (ns):", self.rise),
            ("External interface:", self.external), ("Cable length (m):", self.cable),
        ):
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(control, 1, wx.EXPAND)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 10)
        sizer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 10)
        self.SetSizerAndFit(sizer)
        self.SetMinSize((440, 330))
        if source is not None:
            self.name.SetValue(source.name)
            self.net.SetValue(source.net_name)
            self.kind.SetStringSelection(source.kind)
            self.frequency.SetValue(f"{source.frequency_hz / 1e6:g}")
            self.rise.SetValue(f"{source.rise_time_ns:g}")
            self.external.SetValue(source.external)
            self.cable.SetValue(f"{source.cable_length_m:g}")

    def get_source(self):
        net_name = self.net.GetValue().strip()
        if not net_name:
            raise ValueError("Enter a source net name.")
        frequency = float(self.frequency.GetValue()) * 1e6
        rise_time = float(self.rise.GetValue())
        cable = float(self.cable.GetValue())
        if frequency < 0 or rise_time <= 0 or cable < 0:
            raise ValueError("Frequency/cable must be non-negative and rise time must be positive.")
        return EMCSignalSource(
            self.name.GetValue().strip() or net_name, net_name,
            self.kind.GetStringSelection() or "DIGITAL", frequency, rise_time,
            self.external.GetValue(), cable, True, "manual",
        )


class EMCAnalysisPanel(wx.Panel):
    def __init__(self, parent, board, differential_pairs_provider=None, log_callback=None):
        super().__init__(parent)
        self.board = board
        self.differential_pairs_provider = differential_pairs_provider or (lambda: [])
        self.log_callback = log_callback
        self.settings = EMCAnalysisSettings()
        self.results = None
        self._category_checks = {}
        self._init_ui()

    def log(self, message):
        if self.log_callback:
            self.log_callback(message)

    def _init_ui(self):
        main = wx.BoxSizer(wx.VERTICAL)
        setup = wx.StaticBoxSizer(wx.VERTICAL, self, "Pre-compliance Target")
        parent = setup.GetStaticBox()
        grid = wx.FlexGridSizer(cols=4, hgap=8, vgap=7)
        grid.AddGrowableCol(1, 1); grid.AddGrowableCol(3, 1)
        self.standard = wx.Choice(parent, choices=[label for label, _ in STANDARD_CHOICES])
        self.standard.SetSelection(0)
        self.market = wx.Choice(parent, choices=["EU", "US", "AUTOMOTIVE", "MILITARY", "CUSTOM"])
        self.market.SetStringSelection("EU")
        self.frequency_start = wx.TextCtrl(parent, value="30")
        self.frequency_stop = wx.TextCtrl(parent, value="1000")
        self.ground_nets = wx.TextCtrl(parent, value="GND, AGND, DGND, PGND")
        self.connector_prefixes = wx.TextCtrl(parent, value="J, P, CN")
        for label, control in (
            ("Standard:", self.standard), ("Market:", self.market),
            ("Start frequency (MHz):", self.frequency_start),
            ("Stop frequency (MHz):", self.frequency_stop),
            ("Ground-net aliases:", self.ground_nets),
            ("Connector prefixes:", self.connector_prefixes),
        ):
            grid.Add(wx.StaticText(parent, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(control, 1, wx.EXPAND)
        setup.Add(grid, 0, wx.EXPAND | wx.ALL, 7)
        main.Add(setup, 0, wx.EXPAND | wx.ALL, 5)

        source_box = wx.StaticBoxSizer(wx.VERTICAL, self, "Detected and Manual Emission Sources")
        source_parent = source_box.GetStaticBox()
        self.source_list = wx.ListCtrl(source_parent, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for index, (title, width) in enumerate((
            ("Use", 45), ("Name", 145), ("Net", 180), ("Type", 95),
            ("MHz", 80), ("Rise ns", 75), ("External", 70), ("Cable m", 70), ("Origin", 100),
        )):
            self.source_list.InsertColumn(index, title, width=width)
        source_box.Add(self.source_list, 1, wx.EXPAND | wx.ALL, 5)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_scan = wx.Button(source_parent, label="Scan Live PCB")
        self.btn_add = wx.Button(source_parent, label="Add Manual Source")
        self.btn_edit = wx.Button(source_parent, label="Edit Selected")
        self.btn_toggle = wx.Button(source_parent, label="Enable / Disable")
        for button in (self.btn_scan, self.btn_add, self.btn_edit, self.btn_toggle):
            buttons.Add(button, 0, wx.RIGHT, 5)
        source_box.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        main.Add(source_box, 1, wx.EXPAND | wx.ALL, 5)

        rules = wx.StaticBoxSizer(wx.VERTICAL, self, "Rule Families")
        rules_parent = rules.GetStaticBox()
        grid = wx.GridSizer(cols=8, hgap=5, vgap=4)
        for label, key in CATEGORIES:
            check = wx.CheckBox(rules_parent, label=label)
            check.SetValue(True)
            self._category_checks[key] = check
            grid.Add(check, 0, wx.EXPAND)
        rules.Add(grid, 0, wx.EXPAND | wx.ALL, 6)
        main.Add(rules, 0, wx.EXPAND | wx.ALL, 5)

        self.summary = wx.StaticText(self, label="No EMI/EMC analysis has been run in this session.")
        main.Add(self.summary, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.SetSizer(main)

        self.btn_scan.Bind(wx.EVT_BUTTON, lambda _event: self.refresh_live_board())
        self.btn_add.Bind(wx.EVT_BUTTON, self._on_add)
        self.btn_edit.Bind(wx.EVT_BUTTON, self._on_edit)
        self.btn_toggle.Bind(wx.EVT_BUTTON, self._on_toggle)
        self.source_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_edit)

    def _net_names(self):
        extractor = GeometryExtractor(self.board)
        names = set()
        for collection in ("tracks", "vias", "zones", "footprints"):
            for item in extractor._get_board_items(collection) or []:
                candidates = extractor._get_val(item, "pads", []) if collection == "footprints" else [item]
                if collection == "footprints" and not candidates:
                    candidates = extractor._get_val(extractor._get_val(item, "definition"), "pads", []) or []
                for candidate in candidates:
                    name = extractor._get_val(extractor._get_val(candidate, "net"), "name", "")
                    if name:
                        names.add(str(name))
        return names

    def _update_sources(self):
        self.source_list.DeleteAllItems()
        for source in self.settings.sources:
            row = self.source_list.InsertItem(self.source_list.GetItemCount(), "Yes" if source.enabled else "No")
            values = (source.name, source.net_name, source.kind, f"{source.frequency_hz / 1e6:g}",
                      f"{source.rise_time_ns:g}", "Yes" if source.external else "No",
                      f"{source.cable_length_m:g}", source.source)
            for column, value in enumerate(values, start=1):
                self.source_list.SetItem(row, column, str(value))

    def refresh_live_board(self):
        pairs = self.differential_pairs_provider() or []
        self.settings.sources = EMCSourceDiscoverer.discover(
            self._net_names(), self.settings.sources, pairs,
        )
        self._update_sources()
        self.log(f"EMI/EMC source scan complete: {len(self.settings.sources)} candidate(s).")

    def _selected_index(self):
        index = self.source_list.GetFirstSelected()
        return index if 0 <= index < len(self.settings.sources) else None

    def _show_source_dialog(self, source=None):
        dialog = EMCSourceDialog(self, source)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return None
            return dialog.get_source()
        finally:
            dialog.Destroy()

    def _on_add(self, _event):
        try:
            source = self._show_source_dialog()
            if source is not None:
                self.settings.sources.append(source)
                self._update_sources()
        except ValueError as exc:
            wx.MessageBox(str(exc), "EMI/EMC source", wx.OK | wx.ICON_ERROR)

    def _on_edit(self, _event):
        index = self._selected_index()
        if index is None:
            return
        try:
            source = self._show_source_dialog(self.settings.sources[index])
            if source is not None:
                self.settings.sources[index] = source
                self._update_sources()
        except ValueError as exc:
            wx.MessageBox(str(exc), "EMI/EMC source", wx.OK | wx.ICON_ERROR)

    def _on_toggle(self, _event):
        index = self._selected_index()
        if index is not None:
            self.settings.sources[index].enabled = not self.settings.sources[index].enabled
            self._update_sources()

    def get_settings(self):
        start = float(self.frequency_start.GetValue()) * 1e6
        stop = float(self.frequency_stop.GetValue()) * 1e6
        if start <= 0 or stop <= start:
            raise ValueError("EMI/EMC stop frequency must be greater than the positive start frequency.")
        self.settings.frequency_start_hz = start
        self.settings.frequency_stop_hz = stop
        self.settings.standard = STANDARD_CHOICES[max(0, self.standard.GetSelection())][1]
        self.settings.market = self.market.GetStringSelection() or "CUSTOM"
        self.settings.reference_net_names = [item.strip() for item in self.ground_nets.GetValue().split(",") if item.strip()]
        self.settings.external_connector_prefixes = [item.strip() for item in self.connector_prefixes.GetValue().split(",") if item.strip()]
        self.settings.enabled_categories = [key for key, check in self._category_checks.items() if check.GetValue()]
        if not self.settings.reference_net_names:
            raise ValueError("Enter at least one ground-net alias.")
        return self.settings

    def set_settings(self, settings):
        self.settings = settings or EMCAnalysisSettings()
        standard_index = next((index for index, (_, value) in enumerate(STANDARD_CHOICES)
                               if value == self.settings.standard), 0)
        self.standard.SetSelection(standard_index)
        if self.market.FindString(self.settings.market) != wx.NOT_FOUND:
            self.market.SetStringSelection(self.settings.market)
        self.frequency_start.SetValue(f"{self.settings.frequency_start_hz / 1e6:g}")
        self.frequency_stop.SetValue(f"{self.settings.frequency_stop_hz / 1e6:g}")
        self.ground_nets.SetValue(", ".join(self.settings.reference_net_names))
        self.connector_prefixes.SetValue(", ".join(self.settings.external_connector_prefixes))
        enabled = set(self.settings.enabled_categories)
        for key, check in self._category_checks.items():
            check.SetValue(key in enabled)
        self._update_sources()

    def apply_results(self, result):
        self.results = result
        counts = result.severity_counts
        self.summary.SetLabel(
            f"Risk score {result.risk_score}/100 — {len(result.findings)} findings: "
            f"{counts.get('CRITICAL', 0)} critical, {counts.get('HIGH', 0)} high, "
            f"{counts.get('MEDIUM', 0)} medium."
        )
