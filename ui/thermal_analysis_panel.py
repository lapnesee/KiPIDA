import math

import wx

from i18n import _

try:
    from models import AirflowSettings, ThermalAnalysisSettings
    from thermal_model import PowerLossEstimator
    from thermal_mesh import estimate_thermal_mesh_cost
except (ImportError, ValueError):
    from models import AirflowSettings, ThermalAnalysisSettings
    from thermal_model import PowerLossEstimator
    from thermal_mesh import estimate_thermal_mesh_cost


THERMAL_COLOR_MAPS = (
    (_("Inferno (default)"), "inferno"),
    (_("Viridis"), "viridis"),
    (_("Turbo"), "turbo"),
    (_("Plasma"), "plasma"),
    (_("Cividis"), "cividis"),
)

THERMAL_COLOR_MINIMUM_MODES = (
    (_("Ambient temperature"), "AMBIENT"),
    (_("Automatic (calculated minimum)"), "AUTO"),
    (_("Custom temperature"), "CUSTOM"),
)

THERMAL_COLOR_MAXIMUM_MODES = (
    (_("Automatic (calculated hotspot)"), "AUTO"),
    (_("Custom temperature"), "CUSTOM"),
)


class ThermalComponentDialog(wx.Dialog):
    def __init__(self, parent, component):
        super().__init__(parent, title=_("Thermal Model: {reference}").format(reference=component.ref_des))
        sizer = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(cols=2, hgap=8, vgap=8)
        grid.AddGrowableCol(1, 1)
        self.txt_power = wx.TextCtrl(self, value=f"{component.power_w:g}")
        self.txt_width = wx.TextCtrl(self, value=f"{component.width_mm:g}")
        self.txt_depth = wx.TextCtrl(self, value=f"{component.depth_mm:g}")
        self.txt_height = wx.TextCtrl(self, value=f"{component.height_mm:g}")
        self.txt_theta = wx.TextCtrl(self, value=f"{component.theta_jb_c_per_w:g}")
        self.txt_tjmax = wx.TextCtrl(self, value=f"{component.max_junction_c:g}")
        self.chk_enabled = wx.CheckBox(self, label="Include as heat source")
        self.chk_enabled.SetValue(component.enabled)
        for label, control in (
            ("Power (W):", self.txt_power),
            ("Width (mm):", self.txt_width),
            ("Depth (mm):", self.txt_depth),
            ("Height (mm):", self.txt_height),
            ("Theta JB (C/W):", self.txt_theta),
            ("Max junction (C):", self.txt_tjmax),
        ):
            grid.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(control, 1, wx.EXPAND)
        grid.Add(wx.StaticText(self, label="Enabled:"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.chk_enabled, 0)
        sizer.Add(grid, 1, wx.EXPAND | wx.ALL, 12)
        sizer.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 12)
        self.SetSizerAndFit(sizer)

    def apply_to(self, component):
        values = [
            float(self.txt_power.GetValue()),
            float(self.txt_width.GetValue()),
            float(self.txt_depth.GetValue()),
            float(self.txt_height.GetValue()),
            float(self.txt_theta.GetValue()),
            float(self.txt_tjmax.GetValue()),
        ]
        if any(value < 0 for value in values[:-1]) or values[-1] <= -273.15:
            raise ValueError("Thermal model values must be non-negative and physically valid.")
        component.power_w, component.width_mm, component.depth_mm = values[:3]
        component.height_mm, component.theta_jb_c_per_w, component.max_junction_c = values[3:]
        component.enabled = self.chk_enabled.GetValue()
        component.model_source = "user"


class ThermalAnalysisPanel(wx.Panel):
    def __init__(
        self, parent, rails_provider, log_callback=None, mesh_context_provider=None,
        inject_overlay_callback=None, clear_overlay_callback=None, clear_cache_callback=None,
    ):
        super().__init__(parent)
        self.rails_provider = rails_provider
        self.log_callback = log_callback
        self.mesh_context_provider = mesh_context_provider
        self.inject_overlay_callback = inject_overlay_callback
        self.clear_overlay_callback = clear_overlay_callback
        self.clear_cache_callback = clear_cache_callback
        self.settings = ThermalAnalysisSettings()
        self._init_ui()

    def _init_ui(self):
        main = wx.BoxSizer(wx.VERTICAL)
        settings_box = wx.StaticBoxSizer(wx.VERTICAL, self, "3D Thermal and Airflow Setup")
        settings_parent = settings_box.GetStaticBox()
        grid = wx.FlexGridSizer(cols=4, hgap=6, vgap=6)
        grid.AddGrowableCol(1, 1)
        grid.AddGrowableCol(3, 1)

        self.txt_ambient = wx.TextCtrl(settings_parent, value="25")
        self.txt_grid = wx.SpinCtrlDouble(
            settings_parent, min=0.01, max=5.0, initial=1.0, inc=0.01,
        )
        self.txt_grid.SetDigits(2)
        self.choice_airflow = wx.Choice(settings_parent, choices=["NATURAL", "FORCED", "CUSTOM"])
        self.choice_airflow.SetSelection(0)
        self.txt_velocity = wx.TextCtrl(settings_parent, value="0")
        self.txt_direction = wx.TextCtrl(settings_parent, value="0")
        self.txt_custom_h = wx.TextCtrl(settings_parent, value="10")
        self.txt_iterations = wx.TextCtrl(settings_parent, value="10")
        self.txt_convergence = wx.TextCtrl(settings_parent, value="0.1")
        self.choice_color_map = wx.Choice(
            settings_parent, choices=[label for label, _ in THERMAL_COLOR_MAPS],
        )
        self.choice_color_map.SetSelection(0)
        self.choice_color_minimum = wx.Choice(
            settings_parent, choices=[label for label, _ in THERMAL_COLOR_MINIMUM_MODES],
        )
        self.choice_color_minimum.SetSelection(0)
        self.txt_color_minimum = wx.TextCtrl(settings_parent, value="25")
        self.txt_color_minimum.Enable(False)
        self.choice_color_maximum = wx.Choice(
            settings_parent, choices=[label for label, _ in THERMAL_COLOR_MAXIMUM_MODES],
        )
        self.choice_color_maximum.SetSelection(0)
        self.txt_color_maximum = wx.TextCtrl(settings_parent, value="125")
        self.txt_color_maximum.Enable(False)
        colour_note = wx.StaticText(
            settings_parent, label="Applied to results and KiCad overlay",
        )

        rows = [
            ("Ambient (C):", self.txt_ambient, "Grid size (mm):", self.txt_grid),
            ("Airflow mode:", self.choice_airflow, "Air speed (m/s):", self.txt_velocity),
            ("Air direction (deg):", self.txt_direction, "Custom h (W/m2K):", self.txt_custom_h),
            ("Coupled iterations:", self.txt_iterations, "Convergence (C):", self.txt_convergence),
            ("Thermal colors:", self.choice_color_map, "", colour_note),
            ("Color scale minimum:", self.choice_color_minimum, "Custom minimum (C):", self.txt_color_minimum),
            ("Color scale maximum:", self.choice_color_maximum, "Custom maximum (C):", self.txt_color_maximum),
        ]
        for left_label, left_control, right_label, right_control in rows:
            grid.Add(wx.StaticText(settings_parent, label=left_label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(left_control, 1, wx.EXPAND)
            grid.Add(wx.StaticText(settings_parent, label=right_label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(right_control, 1, wx.EXPAND)
        settings_box.Add(grid, 0, wx.EXPAND | wx.ALL, 8)

        mesh_controls = wx.BoxSizer(wx.HORIZONTAL)
        mesh_controls.Add(wx.StaticText(settings_parent, label="Mesh preset:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.choice_mesh_preset = wx.Choice(
            settings_parent,
            choices=[
                _("Fast (1.5 mm)"), _("Normal (1.0 mm)"), _("Fine (0.5 mm)"),
                _("Super (0.1 mm)"), _("Expert / custom"),
            ],
        )
        self.choice_mesh_preset.SetSelection(1)
        self.txt_expert_grid = wx.TextCtrl(settings_parent, value="1.0", size=(75, -1))
        self.txt_expert_grid.Hide()
        self.lbl_mesh_cost = wx.StaticText(settings_parent, label="Estimating mesh...")
        mesh_controls.Add(self.choice_mesh_preset, 0, wx.RIGHT, 12)
        mesh_controls.Add(self.txt_expert_grid, 0, wx.RIGHT, 12)
        mesh_controls.Add(self.lbl_mesh_cost, 0, wx.ALIGN_CENTER_VERTICAL)
        settings_box.Add(mesh_controls, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        checks = wx.BoxSizer(wx.HORIZONTAL)
        self.chk_top = wx.CheckBox(settings_parent, label="Top exposed")
        self.chk_bottom = wx.CheckBox(settings_parent, label="Bottom exposed")
        self.chk_edges = wx.CheckBox(settings_parent, label="Edges exposed")
        self.chk_radiation = wx.CheckBox(settings_parent, label="Include radiation")
        self.chk_dc_loss = wx.CheckBox(settings_parent, label="Include DC copper losses")
        self.chk_internal_layers = wx.CheckBox(settings_parent, label="Show internal copper maps in Results")
        for check in (
            self.chk_top, self.chk_bottom, self.chk_edges, self.chk_radiation,
            self.chk_dc_loss, self.chk_internal_layers,
        ):
            check.SetValue(True)
            checks.Add(check, 0, wx.RIGHT, 14)
        settings_box.Add(checks, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        overlay_controls = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_inject_overlay = wx.Button(settings_parent, label="Inject KiCad Heat Overlay")
        self.btn_clear_overlay = wx.Button(settings_parent, label="Clear KiCad Heat Overlay")
        self.btn_clear_mesh_cache = wx.Button(settings_parent, label="Clear Thermal Cache")
        overlay_controls.Add(self.btn_inject_overlay, 0, wx.RIGHT, 6)
        overlay_controls.Add(self.btn_clear_overlay, 0, wx.RIGHT, 14)
        overlay_controls.Add(self.btn_clear_mesh_cache, 0)
        overlay_controls.Add(wx.StaticText(
            settings_parent,
            label="Dedicated non-electrical layers: X.Thermal.Top / X.Thermal.Bottom.",
        ), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)
        settings_box.Add(overlay_controls, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        main.Add(settings_box, 0, wx.EXPAND | wx.ALL, 5)

        component_box = wx.StaticBoxSizer(wx.VERTICAL, self, "Component Heat Sources")
        component_parent = component_box.GetStaticBox()
        self.component_list = wx.ListCtrl(component_parent, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for index, (label, width) in enumerate((
            ("Use", 50), ("Ref", 70), ("Power (W)", 90), ("Size (mm)", 105),
            ("Theta JB", 80), ("Tj max", 75), ("Source", 150),
        )):
            self.component_list.InsertColumn(index, label, width=width)
        component_box.Add(self.component_list, 1, wx.EXPAND | wx.ALL, 5)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_estimate = wx.Button(component_parent, label="Refresh Power Estimates")
        self.btn_toggle = wx.Button(component_parent, label="Enable / Disable Selected")
        self.btn_edit = wx.Button(component_parent, label="Edit Selected Model")
        buttons.Add(self.btn_estimate, 0, wx.RIGHT, 5)
        buttons.Add(self.btn_toggle, 0, wx.RIGHT, 5)
        buttons.Add(self.btn_edit, 0)
        component_box.Add(buttons, 0, wx.ALL, 5)
        main.Add(component_box, 1, wx.EXPAND | wx.ALL, 5)
        main.Add(wx.StaticText(
            self,
            label=("Steady-state 3D solid conduction with convective boundaries. "
                   "Estimated powers and package models must be reviewed before sign-off."),
        ), 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.SetSizer(main)

        self.btn_estimate.Bind(wx.EVT_BUTTON, self._on_estimate)
        self.btn_toggle.Bind(wx.EVT_BUTTON, self._on_toggle)
        self.btn_edit.Bind(wx.EVT_BUTTON, self._on_edit)
        self.component_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_edit)
        self.choice_mesh_preset.Bind(wx.EVT_CHOICE, self._on_mesh_preset)
        self.choice_color_minimum.Bind(wx.EVT_CHOICE, self._on_color_minimum_mode)
        self.choice_color_maximum.Bind(wx.EVT_CHOICE, self._on_color_maximum_mode)
        self.txt_grid.Bind(wx.EVT_SPINCTRLDOUBLE, self._on_grid_changed)
        self.txt_grid.Bind(wx.EVT_TEXT, self._on_grid_changed)
        self.txt_expert_grid.Bind(wx.EVT_TEXT, self._on_expert_grid_changed)
        self.btn_inject_overlay.Bind(wx.EVT_BUTTON, self._on_inject_overlay)
        self.btn_clear_overlay.Bind(wx.EVT_BUTTON, self._on_clear_overlay)
        self.btn_clear_mesh_cache.Bind(wx.EVT_BUTTON, self._on_clear_mesh_cache)

    def _on_inject_overlay(self, _event):
        if self.inject_overlay_callback:
            self.inject_overlay_callback()

    def _on_clear_overlay(self, _event):
        if self.clear_overlay_callback:
            self.clear_overlay_callback()

    def _on_clear_mesh_cache(self, _event):
        if self.clear_cache_callback:
            self.clear_cache_callback()

    def _on_mesh_preset(self, event):
        values = {0: 1.5, 1: 1.0, 2: 0.5, 3: 0.1}
        selected = self.choice_mesh_preset.GetSelection()
        if selected in values:
            self.txt_grid.SetValue(values[selected])
        self.txt_expert_grid.Show(selected == 4)
        if selected == 4:
            self.txt_expert_grid.SetValue(f"{self.txt_grid.GetValue():g}")
            self.txt_expert_grid.SetFocus()
        self.Layout()
        self._update_mesh_cost()

    def _on_color_minimum_mode(self, event):
        selected = self.choice_color_minimum.GetSelection()
        mode = THERMAL_COLOR_MINIMUM_MODES[
            selected if selected != wx.NOT_FOUND else 0
        ][1]
        self.txt_color_minimum.Enable(mode == "CUSTOM")
        if mode == "AMBIENT":
            self.txt_color_minimum.SetValue(self.txt_ambient.GetValue())
        event.Skip()

    def _on_color_maximum_mode(self, event):
        selected = self.choice_color_maximum.GetSelection()
        mode = THERMAL_COLOR_MAXIMUM_MODES[
            selected if selected != wx.NOT_FOUND else 0
        ][1]
        self.txt_color_maximum.Enable(mode == "CUSTOM")
        event.Skip()

    def _on_expert_grid_changed(self, event):
        try:
            value = float(self.txt_expert_grid.GetValue().replace(",", "."))
            if 0.01 <= value <= 5.0:
                self.txt_grid.SetValue(value)
        except ValueError:
            pass
        self._update_mesh_cost()
        event.Skip()

    def _on_grid_changed(self, event):
        self._update_mesh_cost()
        event.Skip()

    def _update_mesh_cost(self):
        try:
            size = max(0.01, float(self.txt_grid.GetValue()))
            context = self.mesh_context_provider() if self.mesh_context_provider else {}
            estimate = estimate_thermal_mesh_cost(context, size)
            mib = 1024 ** 2
            ceiling = (
                _(", RAM ceiling {memory:g} GiB").format(
                    memory=estimate['memory_budget_bytes'] / (1024 ** 3)
                )
                if estimate["memory_budget_bytes"] else ""
            )
            self.lbl_mesh_cost.SetLabel(
                _(
                    "~{nodes:,} nodes / {branches:,} branches — host peak {host:.0f} MiB, "
                    "GPU {gpu:.0f} MiB — limit {limit:,} nodes{ceiling} — recommended: {backend}"
                ).format(
                    nodes=estimate['nodes'], branches=estimate['branches'],
                    host=estimate['cpu_bytes']/mib, gpu=estimate['gpu_bytes']/mib,
                    limit=estimate['node_limit'], ceiling=ceiling, backend=estimate['backend'],
                )
            )
            node_limit = estimate["node_limit"]
            colour = wx.Colour(190, 35, 35) if estimate["exceeds_memory_limit"] else (
                wx.Colour(190, 105, 0) if estimate["nodes"] > node_limit * 0.7 else wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
            )
            self.lbl_mesh_cost.SetForegroundColour(colour)
            # The StaticBoxSizer is owned by this panel, not by the StaticBox
            # child. Laying out the child can move wx.Choice controls to (0,0)
            # until their next native selection event.
            self.Layout()
        except (TypeError, ValueError):
            self.lbl_mesh_cost.SetLabel("Relative XY cells: invalid")

    def refresh_components(self, preserve_user=True):
        saved = {component.ref_des: component for component in self.settings.components}
        estimated = PowerLossEstimator.estimate(list(self.rails_provider() or []))
        if preserve_user:
            for index, component in enumerate(estimated):
                prior = saved.get(component.ref_des)
                if prior is not None and prior.model_source == "user":
                    estimated[index] = prior
        self.settings.components = estimated
        self._update_component_list()

    def _update_component_list(self):
        self.component_list.DeleteAllItems()
        for component in self.settings.components:
            row = self.component_list.InsertItem(
                self.component_list.GetItemCount(), "Yes" if component.enabled else "No"
            )
            self.component_list.SetItem(row, 1, component.ref_des)
            self.component_list.SetItem(row, 2, f"{component.power_w:g}")
            self.component_list.SetItem(row, 3, f"{component.width_mm:g} x {component.depth_mm:g}")
            self.component_list.SetItem(row, 4, f"{component.theta_jb_c_per_w:g}")
            self.component_list.SetItem(row, 5, f"{component.max_junction_c:g}")
            self.component_list.SetItem(row, 6, component.model_source)

    def _selected_component(self):
        selected = self.component_list.GetFirstSelected()
        if selected == -1 or selected >= len(self.settings.components):
            return None
        return self.settings.components[selected]

    def _on_estimate(self, event):
        self.refresh_components(preserve_user=True)

    def _on_toggle(self, event):
        component = self._selected_component()
        if component is not None:
            component.enabled = not component.enabled
            self._update_component_list()

    def _on_edit(self, event):
        component = self._selected_component()
        if component is None:
            return
        dialog = ThermalComponentDialog(self, component)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                dialog.apply_to(component)
                self._update_component_list()
        except ValueError as exc:
            wx.MessageBox(str(exc), "Invalid Thermal Model", wx.OK | wx.ICON_ERROR)
        finally:
            dialog.Destroy()

    def get_settings(self):
        mode = self.choice_airflow.GetStringSelection() or "NATURAL"
        self.settings.ambient_c = float(self.txt_ambient.GetValue())
        self.settings.grid_size_mm = float(self.txt_grid.GetValue())
        self.settings.airflow = AirflowSettings(
            mode=mode,
            velocity_m_s=float(self.txt_velocity.GetValue()),
            direction_deg=float(self.txt_direction.GetValue()),
            custom_h_w_m2k=float(self.txt_custom_h.GetValue()),
            expose_top=self.chk_top.GetValue(),
            expose_bottom=self.chk_bottom.GetValue(),
            expose_edges=self.chk_edges.GetValue(),
        )
        self.settings.include_radiation = self.chk_radiation.GetValue()
        self.settings.include_dc_copper_losses = self.chk_dc_loss.GetValue()
        selected = self.choice_color_map.GetSelection()
        self.settings.color_map = THERMAL_COLOR_MAPS[
            selected if selected != wx.NOT_FOUND else 0
        ][1]
        minimum_selected = self.choice_color_minimum.GetSelection()
        minimum_mode = THERMAL_COLOR_MINIMUM_MODES[
            minimum_selected if minimum_selected != wx.NOT_FOUND else 0
        ][1]
        self.settings.color_scale_minimum_mode = minimum_mode
        self.settings.color_scale_minimum_c = None
        if minimum_mode == "CUSTOM":
            minimum = float(self.txt_color_minimum.GetValue())
            if not math.isfinite(minimum) or minimum <= -273.15:
                raise ValueError("Custom thermal colour minimum must be above absolute zero.")
            self.settings.color_scale_minimum_c = minimum
        maximum_selected = self.choice_color_maximum.GetSelection()
        maximum_mode = THERMAL_COLOR_MAXIMUM_MODES[
            maximum_selected if maximum_selected != wx.NOT_FOUND else 0
        ][1]
        self.settings.color_scale_maximum_mode = maximum_mode
        self.settings.color_scale_maximum_c = None
        if maximum_mode == "CUSTOM":
            maximum = float(self.txt_color_maximum.GetValue())
            if not math.isfinite(maximum) or maximum <= -273.15:
                raise ValueError("Custom thermal colour maximum must be above absolute zero.")
            self.settings.color_scale_maximum_c = maximum
        minimum = self.settings.resolved_color_scale_minimum_c()
        maximum = self.settings.resolved_color_scale_maximum_c()
        if minimum is not None and maximum is not None and maximum <= minimum:
            raise ValueError("Thermal colour maximum must be greater than the colour minimum.")
        self.settings.show_internal_copper_layers = self.chk_internal_layers.GetValue()
        self.settings.coupled_iterations = int(self.txt_iterations.GetValue())
        self.settings.convergence_c = float(self.txt_convergence.GetValue())
        if not 0.01 <= self.settings.grid_size_mm <= 5.0 or self.settings.coupled_iterations < 1:
            raise ValueError("Thermal grid size must be between 0.01 and 5 mm; coupled iterations must be positive.")
        return self.settings

    def set_settings(self, settings):
        self.settings = settings or ThermalAnalysisSettings()
        airflow = self.settings.airflow
        self.txt_ambient.SetValue(f"{self.settings.ambient_c:g}")
        self.txt_grid.SetValue(float(self.settings.grid_size_mm))
        preset = {1.5: 0, 1.0: 1, 0.5: 2, 0.1: 3}.get(
            round(float(self.settings.grid_size_mm), 2), 4,
        )
        self.choice_mesh_preset.SetSelection(preset)
        self.txt_expert_grid.SetValue(f"{self.settings.grid_size_mm:g}")
        self.txt_expert_grid.Show(preset == 4)
        self._update_mesh_cost()
        index = self.choice_airflow.FindString(airflow.mode)
        self.choice_airflow.SetSelection(index if index != wx.NOT_FOUND else 0)
        self.txt_velocity.SetValue(f"{airflow.velocity_m_s:g}")
        self.txt_direction.SetValue(f"{airflow.direction_deg:g}")
        self.txt_custom_h.SetValue(f"{airflow.custom_h_w_m2k:g}")
        self.txt_iterations.SetValue(str(self.settings.coupled_iterations))
        self.txt_convergence.SetValue(f"{self.settings.convergence_c:g}")
        self.chk_top.SetValue(airflow.expose_top)
        self.chk_bottom.SetValue(airflow.expose_bottom)
        self.chk_edges.SetValue(airflow.expose_edges)
        self.chk_radiation.SetValue(self.settings.include_radiation)
        self.chk_dc_loss.SetValue(self.settings.include_dc_copper_losses)
        self.chk_internal_layers.SetValue(
            bool(getattr(self.settings, "show_internal_copper_layers", True))
        )
        colour_map = str(getattr(self.settings, "color_map", "inferno")).lower()
        colour_index = next(
            (index for index, (_, value) in enumerate(THERMAL_COLOR_MAPS) if value == colour_map),
            0,
        )
        self.choice_color_map.SetSelection(colour_index)
        minimum_mode = str(
            getattr(self.settings, "color_scale_minimum_mode", "AMBIENT")
        ).upper()
        minimum_index = next(
            (index for index, (_, value) in enumerate(THERMAL_COLOR_MINIMUM_MODES)
             if value == minimum_mode),
            0,
        )
        self.choice_color_minimum.SetSelection(minimum_index)
        minimum_value = getattr(self.settings, "color_scale_minimum_c", None)
        self.txt_color_minimum.SetValue(
            f"{float(minimum_value):g}" if minimum_value is not None
            else f"{self.settings.ambient_c:g}"
        )
        self.txt_color_minimum.Enable(
            THERMAL_COLOR_MINIMUM_MODES[minimum_index][1] == "CUSTOM"
        )
        maximum_mode = str(
            getattr(self.settings, "color_scale_maximum_mode", "AUTO")
        ).upper()
        maximum_index = next(
            (index for index, (_, value) in enumerate(THERMAL_COLOR_MAXIMUM_MODES)
             if value == maximum_mode),
            0,
        )
        self.choice_color_maximum.SetSelection(maximum_index)
        maximum_value = getattr(self.settings, "color_scale_maximum_c", None)
        self.txt_color_maximum.SetValue(
            f"{float(maximum_value):g}" if maximum_value is not None else "125"
        )
        self.txt_color_maximum.Enable(
            THERMAL_COLOR_MAXIMUM_MODES[maximum_index][1] == "CUSTOM"
        )
        # Recompute all automatic entries when loading a project so configs
        # saved by older versions cannot retain V*I connector heat or output-
        # inductor regulator placement. Explicit user models remain untouched.
        self.refresh_components(preserve_user=True)
