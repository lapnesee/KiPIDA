"""wxPython configuration surface for Phase 8 EMI/EMC pre-compliance."""

import threading
import wx

from i18n import _

try:
    from emc_analyzer import EMCSourceDiscoverer
    from extractor import GeometryExtractor
    from models import EMCAnalysisSettings, EMCSignalSource
    from palace_remote import (
        PalaceRemoteClient, PalaceRemoteConnection, bundled_palace_smoke_config,
    )
except (ImportError, ValueError):
    from ..emc_analyzer import EMCSourceDiscoverer
    from ..extractor import GeometryExtractor
    from ..models import EMCAnalysisSettings, EMCSignalSource
    from ..palace_remote import (
        PalaceRemoteClient, PalaceRemoteConnection, bundled_palace_smoke_config,
    )


STANDARD_CHOICES = [
    (_("CISPR 32 Class B"), "CISPR_32_CLASS_B"),
    (_("CISPR 32 Class A"), "CISPR_32_CLASS_A"),
    (_("FCC Part 15 Class B"), "FCC_PART_15_CLASS_B"),
    (_("FCC Part 15 Class A"), "FCC_PART_15_CLASS_A"),
    (_("CISPR 25 Class 5"), "CISPR_25_CLASS_5"),
    (_("MIL-STD-461G RE102"), "MIL_STD_461G_RE102"),
]
CATEGORIES = [
    (_("Ground planes"), "GROUND"), (_("Decoupling"), "DECOUPLING"),
    (_("I/O filtering"), "IO"), (_("Switching"), "SWITCHING"),
    (_("Clocks"), "CLOCK"), (_("Stackup"), "STACKUP"),
    (_("Differential pairs"), "DIFFERENTIAL"), (_("Board edge"), "BOARD_EDGE"),
    (_("PDN"), "PDN"), (_("Return paths"), "RETURN_PATH"),
    (_("Crosstalk"), "CROSSTALK"), (_("ESD"), "ESD"),
    (_("Shielding"), "SHIELDING"), (_("Via stitching"), "STITCHING"),
    (_("Thermal interaction"), "THERMAL"), (_("Emission estimates"), "EMISSIONS"),
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
        self.voltage = wx.TextCtrl(self, value="3.3")
        self.current = wx.TextCtrl(self, value="0.1")
        self.negative_net = wx.TextCtrl(self)
        for label, control in (
            ("Name:", self.name), ("Net + / signal:", self.net),
            ("Net - (differential, optional):", self.negative_net), ("Type:", self.kind),
            ("Fundamental (MHz):", self.frequency), ("Rise time (ns):", self.rise),
            ("Voltage swing (V):", self.voltage), ("Trace current (A):", self.current),
            ("External interface:", self.external), ("Cable length (m):", self.cable),
        ):
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(control, 1, wx.EXPAND)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 10)
        sizer.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 10)
        self.SetSizerAndFit(sizer)
        self.SetMinSize((440, 400))
        if source is not None:
            self.name.SetValue(source.name)
            self.net.SetValue(source.net_name)
            self.kind.SetStringSelection(source.kind)
            self.frequency.SetValue(f"{source.frequency_hz / 1e6:g}")
            self.rise.SetValue(f"{source.rise_time_ns:g}")
            self.external.SetValue(source.external)
            self.cable.SetValue(f"{source.cable_length_m:g}")
            self.voltage.SetValue(f"{source.voltage_swing_v:g}")
            self.current.SetValue(f"{source.current_a:g}")
            self.negative_net.SetValue(source.negative_net_name)

    def get_source(self):
        net_name = self.net.GetValue().strip()
        if not net_name:
            raise ValueError("Enter a source net name.")
        frequency = float(self.frequency.GetValue()) * 1e6
        rise_time = float(self.rise.GetValue())
        cable = float(self.cable.GetValue())
        voltage = float(self.voltage.GetValue())
        current = float(self.current.GetValue())
        if frequency < 0 or rise_time <= 0 or cable < 0 or voltage < 0 or current < 0:
            raise ValueError(
                "Frequency, cable length, voltage and current must be non-negative; "
                "rise time must be positive."
            )
        return EMCSignalSource(
            name=self.name.GetValue().strip() or net_name,
            net_name=net_name,
            kind=self.kind.GetStringSelection() or "DIGITAL",
            frequency_hz=frequency,
            rise_time_ns=rise_time,
            external=self.external.GetValue(),
            cable_length_m=cable,
            enabled=True,
            source="manual",
            voltage_swing_v=voltage,
            current_a=current,
            negative_net_name=self.negative_net.GetValue().strip(),
            parameter_confidence="HIGH",
            parameter_notes="User-configured source parameters.",
        )


class EMCAnalysisPanel(wx.ScrolledWindow):
    def __init__(self, parent, board, differential_pairs_provider=None,
                 rails_provider=None, log_callback=None):
        super().__init__(parent, style=wx.VSCROLL | wx.BORDER_NONE)
        self.SetScrollRate(0, 10)
        self.board = board
        self.differential_pairs_provider = differential_pairs_provider or (lambda: [])
        self.rails_provider = rails_provider or (lambda: [])
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

        field_box = wx.StaticBoxSizer(wx.VERTICAL, self, "Near-field E/H Simulation")
        field_parent = field_box.GetStaticBox()
        field_grid = wx.FlexGridSizer(cols=4, hgap=8, vgap=7)
        field_grid.AddGrowableCol(1, 1); field_grid.AddGrowableCol(3, 1)
        self.field_enabled = wx.CheckBox(field_parent, label="Compute electric and magnetic maps")
        self.field_enabled.SetValue(True)
        self.field_height = wx.TextCtrl(field_parent, value="3")
        self.field_grid_size = wx.TextCtrl(field_parent, value="1")
        self.field_frequency = wx.TextCtrl(field_parent, value="0")
        field_grid.Add(wx.StaticText(field_parent, label="Simulation:"), 0, wx.ALIGN_CENTER_VERTICAL)
        field_grid.Add(self.field_enabled, 1, wx.EXPAND)
        field_grid.Add(wx.StaticText(field_parent, label="Probe height (mm):"), 0, wx.ALIGN_CENTER_VERTICAL)
        field_grid.Add(self.field_height, 1, wx.EXPAND)
        field_grid.Add(wx.StaticText(field_parent, label="Grid size (mm):"), 0, wx.ALIGN_CENTER_VERTICAL)
        field_grid.Add(self.field_grid_size, 1, wx.EXPAND)
        field_grid.Add(wx.StaticText(field_parent, label="Frequency (MHz, 0=first in-band harmonics):"), 0, wx.ALIGN_CENTER_VERTICAL)
        field_grid.Add(self.field_frequency, 1, wx.EXPAND)
        field_box.Add(field_grid, 0, wx.EXPAND | wx.ALL, 7)
        field_box.Add(wx.StaticText(
            field_parent,
            label=(
                "Quasi-static engineering estimate. Source voltage/current are edited in the source list; "
                "continuous adjacent GND is approximated by an image return. With frequency set to 0, "
                "each source is evaluated at its first harmonic inside the selected compliance band."
            ),
        ), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 7)
        main.Add(field_box, 0, wx.EXPAND | wx.ALL, 5)

        self.phase10_pane = wx.CollapsiblePane(self, label="Phase 10 — Multi-fidelity EMC")
        phase10_parent = self.phase10_pane.GetPane()
        phase10_box = wx.BoxSizer(wx.VERTICAL)
        phase10_grid = wx.FlexGridSizer(cols=4, hgap=8, vgap=7)
        phase10_grid.AddGrowableCol(1, 1); phase10_grid.AddGrowableCol(3, 1)
        self.phase10_enabled = wx.CheckBox(phase10_parent, label="Enable Phase 10 pipeline")
        self.phase10_enabled.SetValue(True)
        self.phase10_spice = wx.CheckBox(phase10_parent, label="Generate ngspice excitations")
        self.phase10_spice.SetValue(True)
        self.phase10_full_wave = wx.CheckBox(phase10_parent, label="Enable full-wave backend")
        self.phase10_full_wave.SetValue(True)
        self.phase10_run_solver = wx.CheckBox(phase10_parent, label="Run selected backend")
        self.phase10_backend = wx.Choice(
            phase10_parent, choices=["openEMS — local", "Palace — LAN server"],
        )
        self.phase10_backend.SetSelection(0)
        self.phase10_ngspice = wx.TextCtrl(phase10_parent, value=r"C:\Spice64\bin\ngspice_con.exe")
        self.phase10_spice_library = wx.TextCtrl(
            phase10_parent,
            value=r"C:\Users\jbc66\Documents\DAW CONTROLEUR\Lib\SPICE",
        )
        self.phase10_openems = wx.TextCtrl(phase10_parent, value=r"C:\openEMS")
        self.phase10_regions = wx.TextCtrl(phase10_parent, value="3")
        self.phase10_mesh = wx.TextCtrl(phase10_parent, value="0.25")
        self.phase10_cells = wx.TextCtrl(phase10_parent, value="2000000")
        self.phase10_timesteps = wx.TextCtrl(phase10_parent, value="8000")
        self.phase10_timeout = wx.TextCtrl(phase10_parent, value="600")
        self.phase10_diff_mode = wx.Choice(
            phase10_parent, choices=["DIFFERENTIAL", "COMMON_MODE"],
        )
        self.phase10_diff_mode.SetSelection(0)
        self.phase10_diff_leg_z = wx.TextCtrl(phase10_parent, value="45")
        for label, control in (
            ("Pipeline:", self.phase10_enabled), ("Circuit source:", self.phase10_spice),
            ("Full-wave:", self.phase10_full_wave), ("Execution:", self.phase10_run_solver),
            ("Full-wave backend:", self.phase10_backend),
            ("ngspice executable:", self.phase10_ngspice), ("openEMS root:", self.phase10_openems),
            ("SPICE model library:", self.phase10_spice_library),
            ("Maximum regions:", self.phase10_regions), ("Mesh resolution (mm):", self.phase10_mesh),
            ("Maximum cells:", self.phase10_cells),
            ("Maximum time steps:", self.phase10_timesteps),
            ("Timeout per region (s):", self.phase10_timeout),
            ("Differential excitation:", self.phase10_diff_mode),
            ("Per-leg impedance (ohm):", self.phase10_diff_leg_z),
        ):
            phase10_grid.Add(wx.StaticText(phase10_parent, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            phase10_grid.Add(control, 1, wx.EXPAND)
        phase10_box.Add(phase10_grid, 0, wx.EXPAND | wx.ALL, 7)
        palace_box = wx.StaticBoxSizer(wx.VERTICAL, phase10_parent, "Palace server on local network")
        palace_parent = palace_box.GetStaticBox()
        palace_grid = wx.FlexGridSizer(cols=4, hgap=8, vgap=7)
        palace_grid.AddGrowableCol(1, 1); palace_grid.AddGrowableCol(3, 1)
        self.palace_host = wx.TextCtrl(palace_parent)
        self.palace_port = wx.TextCtrl(palace_parent, value="22")
        self.palace_username = wx.TextCtrl(palace_parent)
        self.palace_identity = wx.FilePickerCtrl(
            palace_parent, message="Select the SSH private key used for Palace",
            style=wx.FLP_OPEN | wx.FLP_FILE_MUST_EXIST | wx.FLP_USE_TEXTCTRL,
        )
        self.palace_remote_root = wx.TextCtrl(palace_parent, value="~/kipida-palace")
        self.palace_executable = wx.TextCtrl(palace_parent, value="palace")
        self.palace_config = wx.FilePickerCtrl(
            palace_parent, message="Select the Palace JSON configuration",
            wildcard="Palace configuration (*.json)|*.json|All files (*.*)|*.*",
            style=wx.FLP_OPEN | wx.FLP_FILE_MUST_EXIST | wx.FLP_USE_TEXTCTRL,
        )
        smoke_config = bundled_palace_smoke_config()
        if smoke_config.is_file():
            self.palace_config.SetPath(str(smoke_config))
        self.palace_mpi_processes = wx.TextCtrl(palace_parent, value="1")
        self.palace_host_key_policy = wx.Choice(
            palace_parent, choices=["Strict (known host only)", "Accept new host key"],
        )
        self.palace_host_key_policy.SetSelection(0)
        self.palace_connect_timeout = wx.TextCtrl(palace_parent, value="10")
        self.palace_keep_remote = wx.CheckBox(
            palace_parent, label="Keep remote job files for reproducibility",
        )
        self.palace_keep_remote.SetValue(True)
        for label, control in (
            ("Server host / IP:", self.palace_host), ("SSH port:", self.palace_port),
            ("SSH username:", self.palace_username), ("SSH private key:", self.palace_identity),
            ("Remote job root:", self.palace_remote_root),
            ("Palace executable:", self.palace_executable),
            ("Palace config JSON:", self.palace_config),
            ("MPI processes:", self.palace_mpi_processes),
            ("Host-key policy:", self.palace_host_key_policy),
            ("Connection timeout (s):", self.palace_connect_timeout),
            ("Remote retention:", self.palace_keep_remote),
        ):
            palace_grid.Add(wx.StaticText(palace_parent, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            palace_grid.Add(control, 1, wx.EXPAND)
        palace_box.Add(palace_grid, 0, wx.EXPAND | wx.ALL, 7)
        palace_actions = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_test_palace = wx.Button(palace_parent, label="Test Palace connection")
        self.palace_connection_status = wx.StaticText(
            palace_parent, label="Not tested — OpenSSH key or agent authentication is required.",
        )
        palace_actions.Add(self.btn_test_palace, 0, wx.RIGHT, 8)
        palace_actions.Add(self.palace_connection_status, 1, wx.ALIGN_CENTER_VERTICAL)
        palace_box.Add(palace_actions, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 7)
        palace_box.Add(wx.StaticText(
            palace_parent,
            label=(
                "Data disclosure: the directory containing the selected Palace JSON, mesh, and "
                "associated files is transferred to this LAN server. No password is stored."
            ),
        ), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 7)
        phase10_box.Add(palace_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 7)
        phase10_box.Add(wx.StaticText(
            phase10_parent,
            label=(
                "External solvers run in isolated processes. Relative spectra are never compared "
                "with regulatory limits until calibrated far-field data are available."
            ),
        ), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 7)
        phase10_parent.SetSizer(phase10_box)
        main.Add(self.phase10_pane, 0, wx.EXPAND | wx.ALL, 5)

        source_box = wx.StaticBoxSizer(wx.VERTICAL, self, "Detected and Manual Emission Sources")
        source_parent = source_box.GetStaticBox()
        self.source_list = wx.ListCtrl(source_parent, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.source_list.SetMinSize((-1, 190))
        for index, (title, width) in enumerate((
            ("Use", 45), ("Name", 145), ("Net", 180), ("Type", 95),
            ("MHz", 80), ("Rise ns", 75), ("External", 70), ("Cable m", 70), ("Origin", 100),
            ("Swing V", 70), ("Current A", 75), ("Confidence", 85),
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

        inductor_box = wx.StaticBoxSizer(wx.VERTICAL, self, "Switching Inductor Models")
        inductor_parent = inductor_box.GetStaticBox()
        self.inductor_list = wx.ListCtrl(inductor_parent, style=wx.LC_REPORT)
        self.inductor_list.SetMinSize((-1, 125))
        for index, (title, width) in enumerate((
            ("Ref", 55), ("MPN", 165), ("Shield", 85), ("L (uH)", 75),
            ("Size mm", 120), ("Shield attenuation", 135), ("Confidence", 90),
        )):
            self.inductor_list.InsertColumn(index, title, width=width)
        inductor_box.Add(self.inductor_list, 1, wx.EXPAND | wx.ALL, 5)
        inductor_box.Add(wx.StaticText(
            inductor_parent,
            label=(
                "Shield presence is reported independently from numerical attenuation. "
                "No dB reduction is applied without a manufacturer curve or measured value."
            ),
        ), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 7)
        main.Add(inductor_box, 0, wx.EXPAND | wx.ALL, 5)

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
        self.FitInside()

        self.phase10_pane.Bind(wx.EVT_COLLAPSIBLEPANE_CHANGED, self._on_phase10_changed)
        self.phase10_backend.Bind(wx.EVT_CHOICE, self._on_backend_changed)
        self.btn_test_palace.Bind(wx.EVT_BUTTON, self._on_test_palace)
        self.btn_scan.Bind(wx.EVT_BUTTON, lambda _event: self.refresh_live_board())
        self.btn_add.Bind(wx.EVT_BUTTON, self._on_add)
        self.btn_edit.Bind(wx.EVT_BUTTON, self._on_edit)
        self.btn_toggle.Bind(wx.EVT_BUTTON, self._on_toggle)
        self.source_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_edit)
        self._update_backend_controls()

    def _on_phase10_changed(self, event):
        # Native CollapsiblePane geometry is updated after the event. Defer
        # recalculation so following sections receive their real non-overlap
        # positions and the panel grows a vertical scrollbar when required.
        wx.CallAfter(self._relayout_scrolled_content)
        event.Skip()

    def _relayout_scrolled_content(self):
        self.phase10_pane.GetPane().Layout()
        self.Layout()
        self.FitInside()

    def _on_backend_changed(self, event):
        self._update_backend_controls()
        event.Skip()

    def _update_backend_controls(self):
        palace = self.phase10_backend.GetSelection() == 1
        self.phase10_openems.Enable(not palace)
        for control in (
            self.palace_host, self.palace_port, self.palace_username,
            self.palace_identity, self.palace_remote_root, self.palace_executable,
            self.palace_config, self.palace_mpi_processes,
            self.palace_host_key_policy, self.palace_connect_timeout,
            self.palace_keep_remote, self.btn_test_palace,
        ):
            control.Enable(palace)

    def _palace_connection_from_controls(self):
        return PalaceRemoteConnection(
            host=self.palace_host.GetValue().strip(),
            username=self.palace_username.GetValue().strip(),
            port=int(self.palace_port.GetValue()),
            identity_file=self.palace_identity.GetPath().strip(),
            remote_root=self.palace_remote_root.GetValue().strip(),
            executable=self.palace_executable.GetValue().strip(),
            mpi_processes=int(self.palace_mpi_processes.GetValue()),
            host_key_policy=(
                "ACCEPT_NEW" if self.palace_host_key_policy.GetSelection() == 1 else "STRICT"
            ),
            connect_timeout_s=float(self.palace_connect_timeout.GetValue()),
            run_timeout_s=float(self.phase10_timeout.GetValue()),
            keep_remote_files=self.palace_keep_remote.GetValue(),
        ).validate()

    def _on_test_palace(self, _event):
        try:
            connection = self._palace_connection_from_controls()
        except Exception as exc:
            wx.MessageBox(str(exc), "Palace connection settings", wx.OK | wx.ICON_ERROR)
            return
        self.btn_test_palace.Disable()
        self.palace_connection_status.SetLabel("Connecting...")

        def worker():
            try:
                detail = PalaceRemoteClient(connection).probe()
                wx.CallAfter(self._finish_palace_probe, True, detail)
            except Exception as exc:
                wx.CallAfter(self._finish_palace_probe, False, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_palace_probe(self, success, detail):
        self.btn_test_palace.Enable()
        self.palace_connection_status.SetLabel(
            ("Ready — " if success else "Unavailable — ") + detail[:500]
        )

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
            displayed_net = (
                f"{source.net_name} / {source.negative_net_name}"
                if source.negative_net_name else source.net_name
            )
            values = (source.name, displayed_net, source.kind, f"{source.frequency_hz / 1e6:g}",
                      f"{source.rise_time_ns:g}", "Yes" if source.external else "No",
                      f"{source.cable_length_m:g}", source.source,
                      f"{source.voltage_swing_v:g}", f"{source.current_a:g}",
                      source.parameter_confidence)
            for column, value in enumerate(values, start=1):
                self.source_list.SetItem(row, column, str(value))

    def _update_inductors(self):
        self.inductor_list.DeleteAllItems()
        for model in self.settings.inductor_models:
            row = self.inductor_list.InsertItem(
                self.inductor_list.GetItemCount(), model.ref_des,
            )
            attenuation = (
                f"{model.shielding_attenuation_db:g} dB"
                if model.shielding_attenuation_db is not None else "Not quantified"
            )
            values = (
                model.mpn or "Unknown", model.shield_state,
                f"{model.inductance_h * 1e6:g}" if model.inductance_h else "-",
                f"{model.width_mm:g} x {model.depth_mm:g} x {model.height_mm:g}",
                attenuation, model.parameter_confidence,
            )
            for column, value in enumerate(values, start=1):
                self.inductor_list.SetItem(row, column, str(value))

    def refresh_live_board(self):
        pairs = self.differential_pairs_provider() or []
        switching_frequencies = {}
        rails = list(self.rails_provider() or [])
        rails_by_name = {rail.net_name: rail for rail in rails}
        current_cache = {}

        def rail_current(rail_name, visiting=None):
            if rail_name in current_cache:
                return current_cache[rail_name]
            visiting = set(visiting or ())
            if rail_name in visiting:
                return 0.0
            visiting.add(rail_name)
            rail = rails_by_name.get(rail_name)
            if rail is None:
                return 0.0
            total = sum(float(getattr(load, "total_current", 0.0) or 0.0)
                        for load in getattr(rail, "loads", []) or [])
            for regulator in getattr(rail, "child_regulators", []) or []:
                downstream = rail_current(regulator.output_rail_name, visiting)
                if str(regulator.reg_type).upper() == "SWITCHING":
                    output_rail = rails_by_name.get(regulator.output_rail_name)
                    vout = float(getattr(output_rail, "nominal_voltage", 0.0) or 0.0)
                    vin = float(getattr(rail, "nominal_voltage", 0.0) or 0.0)
                    efficiency = max(float(getattr(regulator, "efficiency", 1.0) or 1.0), 1e-6)
                    total += downstream * vout / (vin * efficiency) if vin > 0.0 else downstream
                else:
                    total += downstream
            current_cache[rail_name] = total
            return total

        for rail in rails:
            for regulator in getattr(rail, "child_regulators", []) or []:
                model = getattr(regulator, "loss_model", {}) or {}
                raw = model.get("switching_frequency_hz", 0.0)
                value = raw.get("value", 0.0) if isinstance(raw, dict) else raw
                ref_des = str(model.get("controller_ref_des", "") or "").upper()
                if ref_des and float(value or 0.0) > 0.0:
                    switching_frequencies[ref_des] = {
                        "frequency_hz": float(value),
                        "voltage_swing_v": float(getattr(rail, "nominal_voltage", 0.0) or 0.0),
                        "current_a": rail_current(regulator.output_rail_name),
                    }
        self.settings.sources = EMCSourceDiscoverer.discover(
            self._net_names(), self.settings.sources, pairs, switching_frequencies,
        )
        self._update_sources()
        self._update_inductors()
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
        height = float(self.field_height.GetValue())
        grid_size = float(self.field_grid_size.GetValue())
        frequency = float(self.field_frequency.GetValue()) * 1e6
        if height <= 0 or grid_size <= 0 or frequency < 0:
            raise ValueError(
                "Near-field height/grid size must be positive and its frequency must be non-negative."
            )
        self.settings.field_simulation_enabled = self.field_enabled.GetValue()
        self.settings.field_probe_height_mm = height
        self.settings.field_grid_size_mm = grid_size
        self.settings.field_frequency_hz = frequency
        phase10 = self.settings.phase10
        phase10.enabled = self.phase10_enabled.GetValue()
        phase10.spice_enabled = self.phase10_spice.GetValue()
        phase10.full_wave_enabled = self.phase10_full_wave.GetValue()
        phase10.auto_run_full_wave = self.phase10_run_solver.GetValue()
        phase10.full_wave_backend = (
            "PALACE_REMOTE" if self.phase10_backend.GetSelection() == 1 else "OPENEMS_LOCAL"
        )
        phase10.ngspice_path = self.phase10_ngspice.GetValue().strip()
        phase10.spice_library_path = self.phase10_spice_library.GetValue().strip()
        phase10.openems_root = self.phase10_openems.GetValue().strip()
        phase10.palace_remote_host = self.palace_host.GetValue().strip()
        phase10.palace_remote_port = int(self.palace_port.GetValue())
        phase10.palace_remote_username = self.palace_username.GetValue().strip()
        phase10.palace_remote_identity_file = self.palace_identity.GetPath().strip()
        phase10.palace_remote_root = self.palace_remote_root.GetValue().strip()
        phase10.palace_remote_executable = self.palace_executable.GetValue().strip()
        phase10.palace_remote_config_path = self.palace_config.GetPath().strip()
        phase10.palace_remote_mpi_processes = int(self.palace_mpi_processes.GetValue())
        phase10.palace_remote_host_key_policy = (
            "ACCEPT_NEW" if self.palace_host_key_policy.GetSelection() == 1 else "STRICT"
        )
        phase10.palace_remote_connect_timeout_s = float(
            self.palace_connect_timeout.GetValue()
        )
        phase10.palace_remote_keep_files = self.palace_keep_remote.GetValue()
        phase10.maximum_regions = max(0, int(self.phase10_regions.GetValue()))
        phase10.mesh_resolution_mm = float(self.phase10_mesh.GetValue())
        phase10.maximum_cells = int(self.phase10_cells.GetValue())
        phase10.openems_max_timesteps = int(self.phase10_timesteps.GetValue())
        phase10.solver_timeout_s = float(self.phase10_timeout.GetValue())
        phase10.differential_excitation_mode = (
            self.phase10_diff_mode.GetStringSelection() or "DIFFERENTIAL"
        )
        phase10.differential_leg_impedance_ohm = float(
            self.phase10_diff_leg_z.GetValue()
        )
        if (phase10.mesh_resolution_mm <= 0 or phase10.maximum_cells < 1000
                or phase10.openems_max_timesteps < 100 or phase10.solver_timeout_s <= 0
                or phase10.differential_leg_impedance_ohm <= 0):
            raise ValueError(
                "Phase 10 requires positive mesh/timeout values, at least 1,000 cells "
                "and at least 100 time steps."
            )
        if (phase10.full_wave_enabled and phase10.auto_run_full_wave
                and phase10.full_wave_backend == "PALACE_REMOTE"):
            self._palace_connection_from_controls()
            if not phase10.palace_remote_config_path:
                raise ValueError("Select the Palace JSON configuration to run remotely.")
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
        self.field_enabled.SetValue(self.settings.field_simulation_enabled)
        self.field_height.SetValue(f"{self.settings.field_probe_height_mm:g}")
        self.field_grid_size.SetValue(f"{self.settings.field_grid_size_mm:g}")
        self.field_frequency.SetValue(f"{self.settings.field_frequency_hz / 1e6:g}")
        phase10 = self.settings.phase10
        self.phase10_enabled.SetValue(phase10.enabled)
        self.phase10_spice.SetValue(phase10.spice_enabled)
        self.phase10_full_wave.SetValue(phase10.full_wave_enabled)
        self.phase10_run_solver.SetValue(phase10.auto_run_full_wave)
        self.phase10_backend.SetSelection(
            1 if str(phase10.full_wave_backend).upper() == "PALACE_REMOTE" else 0
        )
        self.phase10_ngspice.SetValue(phase10.ngspice_path)
        self.phase10_spice_library.SetValue(phase10.spice_library_path)
        self.phase10_openems.SetValue(phase10.openems_root)
        self.palace_host.SetValue(phase10.palace_remote_host)
        self.palace_port.SetValue(str(phase10.palace_remote_port))
        self.palace_username.SetValue(phase10.palace_remote_username)
        self.palace_identity.SetPath(phase10.palace_remote_identity_file)
        self.palace_remote_root.SetValue(phase10.palace_remote_root)
        self.palace_executable.SetValue(phase10.palace_remote_executable)
        palace_config_path = phase10.palace_remote_config_path
        if not palace_config_path and bundled_palace_smoke_config().is_file():
            palace_config_path = str(bundled_palace_smoke_config())
        self.palace_config.SetPath(palace_config_path)
        self.palace_mpi_processes.SetValue(str(phase10.palace_remote_mpi_processes))
        self.palace_host_key_policy.SetSelection(
            1 if phase10.palace_remote_host_key_policy == "ACCEPT_NEW" else 0
        )
        self.palace_connect_timeout.SetValue(
            f"{phase10.palace_remote_connect_timeout_s:g}"
        )
        self.palace_keep_remote.SetValue(phase10.palace_remote_keep_files)
        self.phase10_regions.SetValue(str(phase10.maximum_regions))
        self.phase10_mesh.SetValue(f"{phase10.mesh_resolution_mm:g}")
        self.phase10_cells.SetValue(str(phase10.maximum_cells))
        self.phase10_timesteps.SetValue(str(phase10.openems_max_timesteps))
        self.phase10_timeout.SetValue(f"{phase10.solver_timeout_s:g}")
        if self.phase10_diff_mode.FindString(phase10.differential_excitation_mode) != wx.NOT_FOUND:
            self.phase10_diff_mode.SetStringSelection(phase10.differential_excitation_mode)
        else:
            self.phase10_diff_mode.SetStringSelection("DIFFERENTIAL")
        self.phase10_diff_leg_z.SetValue(f"{phase10.differential_leg_impedance_ohm:g}")
        self._update_backend_controls()
        enabled = set(self.settings.enabled_categories)
        for key, check in self._category_checks.items():
            check.SetValue(key in enabled)
        self._update_sources()
        self._update_inductors()

    def apply_results(self, result):
        self.results = result
        counts = result.severity_counts
        self.summary.SetLabel(
            _(
                "Risk score {score}/100 — {total} findings: {critical} critical, "
                "{high} high, {medium} medium."
            ).format(
                score=result.risk_score, total=len(result.findings),
                critical=counts.get('CRITICAL', 0), high=counts.get('HIGH', 0),
                medium=counts.get('MEDIUM', 0),
            )
        )
