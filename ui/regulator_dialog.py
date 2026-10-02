import wx
from ui.number_input import parse_number
try:
    from models import generate_regulator_name
except ImportError:
    from ..models import generate_regulator_name


CURVE_SOURCES = ("datasheet", "measurement")


def parse_efficiency_points(text):
    """loss_model["efficiency_table"] rows from one point per line.

    A line is "Iout efficiency", at the regulator's own input and output, or
    "Vin Vout Iout efficiency" for a datasheet's several curves. Fields part
    on spaces, tabs or ";", a decimal comma is read as a point, and an
    efficiency above 1 is a percentage. TH-008 asked for this curve while
    the dialog took a single efficiency, which power_loss always treats as
    a fallback, so the finding could never go away.
    """
    rows = []
    for number, line in enumerate(str(text or "").splitlines(), start=1):
        fields = line.replace(";", " ").split()
        if not fields:
            continue
        try:
            values = [float(field.replace(",", ".")) for field in fields]
        except ValueError:
            raise ValueError(f"Line {number}: '{line.strip()}' is not a list of numbers.") from None
        if len(values) not in (2, 4):
            raise ValueError(f"Line {number}: give 'Iout efficiency' or 'Vin Vout Iout efficiency'.")
        *voltages, current, efficiency = values
        if efficiency > 1.0:
            efficiency /= 100.0
        if not 0.0 < efficiency <= 1.0 or current < 0.0:
            raise ValueError(f"Line {number}: the efficiency must lie in (0, 100] % and the current "
                             "must not be negative.")
        row = {"iout_a": current, "efficiency": round(efficiency, 6)}
        if voltages:
            row = {"vin_v": voltages[0], "vout_v": voltages[1], **row}
        rows.append(row)
    return rows


def format_efficiency_points(table):
    """The text parse_efficiency_points reads back, efficiencies in %."""
    lines = []
    for row in table or ():
        efficiency = f"{float(row.get('efficiency', 0.0)) * 100.0:g}"
        current = f"{float(row.get('iout_a', 0.0)):g}"
        if "vin_v" in row or "vout_v" in row:
            lines.append(f"{float(row.get('vin_v', 0.0)):g} {float(row.get('vout_v', 0.0)):g} "
                         f"{current} {efficiency}")
        else:
            lines.append(f"{current} {efficiency}")
    return "\n".join(lines)


def apply_efficiency_curve(regulator, data):
    """Set or clear the regulator's efficiency curve from the dialog's values,
    leaving the rest of its loss model as it was."""
    model = dict(regulator.loss_model or {})
    table = list(data.get("efficiency_table") or ())
    if table:
        model["efficiency_table"] = table
        source = dict(model.get("efficiency_table_source") or {})
        source.update({
            "source": data.get("efficiency_table_source") or CURVE_SOURCES[0],
            "confidence": source.get("confidence", "medium"),
            "reference": data.get("efficiency_table_reference", ""),
        })
        model["efficiency_table_source"] = source
    else:
        model.pop("efficiency_table", None)
        model.pop("efficiency_table_source", None)
    regulator.loss_model = model

class RegulatorDialog(wx.Dialog):
    def __init__(self, parent, title, available_rails, discoverer, input_rail=None, output_rail=None):
        super(RegulatorDialog, self).__init__(parent, title=title, size=(520, 780))
        
        self.available_rails = sorted(available_rails)
        self.discoverer = discoverer
        self.input_rail = input_rail
        self.output_rail = output_rail
        
        self.input_comps = {} # ref -> [pads]
        self.output_comps = {} # ref -> [pads]
        self._last_input_ref = ""
        
        self._init_ui()
        self.Centre()
        
        # Trigger initial population
        if self.input_rail: self._on_input_rail_change(None)
        if self.output_rail: self._on_output_rail_change(None)
        
    def _init_ui(self):
        vbox = wx.BoxSizer(wx.VERTICAL)
        
        # Name
        hbox_name = wx.BoxSizer(wx.HORIZONTAL)
        hbox_name.Add(wx.StaticText(self, label="Regulator Name:"), 0, wx.ALIGN_CENTER_VERTICAL|wx.RIGHT, 5)
        self.txt_name = wx.TextCtrl(self, style=wx.TE_READONLY)
        hbox_name.Add(self.txt_name, 1, wx.EXPAND)
        vbox.Add(hbox_name, 0, wx.EXPAND | wx.ALL, 10)
        
        # --- Input Section ---
        sb_in = wx.StaticBoxSizer(wx.VERTICAL, self, "Input Side")
        
        # Input Rail
        grid_in = wx.FlexGridSizer(3, 2, 5, 5)
        grid_in.Add(wx.StaticText(sb_in.GetStaticBox(), label="Input Rail:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.cmb_input_rail = wx.ComboBox(sb_in.GetStaticBox(), choices=self.available_rails, style=wx.CB_READONLY)
        if self.input_rail: self.cmb_input_rail.SetValue(self.input_rail)
        self.cmb_input_rail.Bind(wx.EVT_COMBOBOX, self._on_input_rail_change)
        grid_in.Add(self.cmb_input_rail, 1, wx.EXPAND)
        
        # Input Component
        grid_in.Add(wx.StaticText(sb_in.GetStaticBox(), label="Input Component:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.cmb_input_comp = wx.ComboBox(sb_in.GetStaticBox(), style=wx.CB_READONLY)
        self.cmb_input_comp.Bind(wx.EVT_COMBOBOX, self._on_input_comp_change)
        grid_in.Add(self.cmb_input_comp, 1, wx.EXPAND)
        
        # Input Pads
        grid_in.Add(wx.StaticText(sb_in.GetStaticBox(), label="Input Pads:"), 0, wx.ALIGN_TOP)
        self.lst_input_pads = wx.CheckListBox(sb_in.GetStaticBox(), size=(-1, 80))
        grid_in.Add(self.lst_input_pads, 1, wx.EXPAND)
        
        grid_in.AddGrowableCol(1, 1)
        sb_in.Add(grid_in, 1, wx.EXPAND | wx.ALL, 5)
        vbox.Add(sb_in, 0, wx.EXPAND | wx.ALL, 5)
        
        # --- Output Section ---
        sb_out = wx.StaticBoxSizer(wx.VERTICAL, self, "Output Side")
        
        # Output Rail
        grid_out = wx.FlexGridSizer(3, 2, 5, 5)
        grid_out.Add(wx.StaticText(sb_out.GetStaticBox(), label="Output Rail:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.cmb_output_rail = wx.ComboBox(sb_out.GetStaticBox(), choices=self.available_rails, style=wx.CB_READONLY)
        if self.output_rail: self.cmb_output_rail.SetValue(self.output_rail)
        self.cmb_output_rail.Bind(wx.EVT_COMBOBOX, self._on_output_rail_change)
        grid_out.Add(self.cmb_output_rail, 1, wx.EXPAND)
        
        # Output Component
        grid_out.Add(wx.StaticText(sb_out.GetStaticBox(), label="Output Component:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.cmb_output_comp = wx.ComboBox(sb_out.GetStaticBox(), style=wx.CB_READONLY)
        self.cmb_output_comp.Bind(wx.EVT_COMBOBOX, self._on_output_comp_change)
        grid_out.Add(self.cmb_output_comp, 1, wx.EXPAND)
        
        # Output Pads
        grid_out.Add(wx.StaticText(sb_out.GetStaticBox(), label="Output Pads:"), 0, wx.ALIGN_TOP)
        self.lst_output_pads = wx.CheckListBox(sb_out.GetStaticBox(), size=(-1, 80))
        grid_out.Add(self.lst_output_pads, 1, wx.EXPAND)
        
        grid_out.AddGrowableCol(1, 1)
        sb_out.Add(grid_out, 1, wx.EXPAND | wx.ALL, 5)
        vbox.Add(sb_out, 0, wx.EXPAND | wx.ALL, 5)
        
        # --- Params Section ---
        sb_param = wx.StaticBoxSizer(wx.VERTICAL, self, "Parameters")
        # Rows as many as there are fields: (2, 2) held three rows already,
        # which wx asserts on.
        grid_p = wx.FlexGridSizer(0, 2, 5, 5)
        
        grid_p.Add(wx.StaticText(sb_param.GetStaticBox(), label="Type:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.cmb_type = wx.ComboBox(sb_param.GetStaticBox(), choices=["LINEAR", "SWITCHING"], style=wx.CB_READONLY)
        self.cmb_type.SetValue("LINEAR")
        self.cmb_type.Bind(wx.EVT_COMBOBOX, self.on_type_change)
        grid_p.Add(self.cmb_type, 1, wx.EXPAND)
        
        self.lbl_eff = wx.StaticText(sb_param.GetStaticBox(), label="Efficiency (0.0-1.0):")
        grid_p.Add(self.lbl_eff, 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_eff = wx.TextCtrl(sb_param.GetStaticBox(), value="0.85")
        self.txt_eff.Disable()
        grid_p.Add(self.txt_eff, 1, wx.EXPAND)

        grid_p.Add(wx.StaticText(
            sb_param.GetStaticBox(), label="Thermal loss component:"
        ), 0, wx.ALIGN_CENTER_VERTICAL)
        self.cmb_thermal_comp = wx.ComboBox(
            sb_param.GetStaticBox(), choices=[], style=wx.CB_READONLY
        )
        grid_p.Add(self.cmb_thermal_comp, 1, wx.EXPAND)

        # The curve replaces the single efficiency above at the operating
        # point; without one the loss rests on that estimate (TH-008).
        box = sb_param.GetStaticBox()
        self.lbl_curve = wx.StaticText(box, label="Efficiency curve:")
        grid_p.Add(self.lbl_curve, 0, wx.ALIGN_TOP)
        self.txt_curve = wx.TextCtrl(box, style=wx.TE_MULTILINE, size=(-1, 90))
        self.txt_curve.SetToolTip(
            "One point per line: 'Iout(A) efficiency(%)' at this regulator's input and output, "
            "or 'Vin Vout Iout efficiency' for several curves. Leave empty to use the single "
            "efficiency above."
        )
        grid_p.Add(self.txt_curve, 1, wx.EXPAND)
        self.lbl_curve_source = wx.StaticText(box, label="Curve from:")
        grid_p.Add(self.lbl_curve_source, 0, wx.ALIGN_CENTER_VERTICAL)
        source_row = wx.BoxSizer(wx.HORIZONTAL)
        self.cmb_curve_source = wx.ComboBox(box, choices=list(CURVE_SOURCES), style=wx.CB_READONLY)
        self.cmb_curve_source.SetValue(CURVE_SOURCES[0])
        source_row.Add(self.cmb_curve_source, 0, wx.RIGHT, 5)
        self.txt_curve_reference = wx.TextCtrl(box)
        self.txt_curve_reference.SetHint("Reference, e.g. datasheet figure")
        source_row.Add(self.txt_curve_reference, 1, wx.EXPAND)
        grid_p.Add(source_row, 1, wx.EXPAND)
        self._curve_controls = (self.txt_curve, self.cmb_curve_source, self.txt_curve_reference)
        for control in self._curve_controls:
            control.Disable()

        grid_p.AddGrowableCol(1, 1)
        sb_param.Add(grid_p, 1, wx.EXPAND | wx.ALL, 5)
        vbox.Add(sb_param, 0, wx.EXPAND | wx.ALL, 5)
        
        # Buttons
        btns = wx.StdDialogButtonSizer()
        self.btn_ok = wx.Button(self, wx.ID_OK)
        self.btn_ok.Bind(wx.EVT_BUTTON, self.on_ok)
        btns.AddButton(self.btn_ok)
        btns.AddButton(wx.Button(self, wx.ID_CANCEL))
        btns.Realize()
        vbox.Add(btns, 0, wx.EXPAND | wx.ALL, 10)
        
        self.SetSizer(vbox)
        
    def _on_input_rail_change(self, event):
        rail = self.cmb_input_rail.GetValue()
        self.input_comps = self.discoverer.get_components_on_net(rail)
        refs = sorted(self.input_comps.keys())
        self.cmb_input_comp.Set(refs)
        if refs:
            self.cmb_input_comp.SetSelection(0)
            self._on_input_comp_change(None)
        else:
            self.cmb_input_comp.Clear()
            self.lst_input_pads.Clear()

    def _on_input_comp_change(self, event):
        ref = self.cmb_input_comp.GetValue()
        pads = self.input_comps.get(ref, [])
        pad_names = [getattr(p, 'number', getattr(p, 'name', str(p))) for p in pads]
        self.lst_input_pads.Set(pad_names) 
        self._update_auto_name()
        thermal_ref = self.cmb_thermal_comp.GetValue()
        preferred = ref if not thermal_ref or thermal_ref == self._last_input_ref else None
        self._last_input_ref = ref
        self._update_thermal_components(preferred)

    def _on_output_rail_change(self, event):
        rail = self.cmb_output_rail.GetValue()
        self.output_comps = self.discoverer.get_components_on_net(rail)
        refs = sorted(self.output_comps.keys())
        self.cmb_output_comp.Set(refs)
        if refs:
            self.cmb_output_comp.SetSelection(0)
            self._on_output_comp_change(None)
        else:
            self.cmb_output_comp.Clear()
            self.lst_output_pads.Clear()
            self._update_auto_name()
            
    def _on_output_comp_change(self, event):
        ref = self.cmb_output_comp.GetValue()
        pads = self.output_comps.get(ref, [])
        pad_names = [getattr(p, 'number', getattr(p, 'name', str(p))) for p in pads]
        self.lst_output_pads.Set(pad_names)
        self._update_auto_name()
        self._update_thermal_components()

    def _update_thermal_components(self, preferred=None):
        """Keep thermal placement independent from connectivity endpoints."""
        current = preferred or self.cmb_thermal_comp.GetValue()
        refs = set(self.input_comps) | set(self.output_comps)
        refs.update(filter(None, (
            self.cmb_input_comp.GetValue(),
            self.cmb_output_comp.GetValue(),
            preferred,
        )))
        choices = sorted(refs)
        self.cmb_thermal_comp.Set(choices)
        if current in choices:
            self.cmb_thermal_comp.SetValue(current)
        elif self.cmb_input_comp.GetValue() in choices:
            self.cmb_thermal_comp.SetValue(self.cmb_input_comp.GetValue())
        elif choices:
            self.cmb_thermal_comp.SetSelection(0)

    def on_type_change(self, event):
        switching = self.cmb_type.GetValue() == "SWITCHING"
        for control in (self.txt_eff,) + self._curve_controls:
            control.Enable(switching)


    def _update_auto_name(self):
        inp_ref = self.cmb_input_comp.GetValue()
        out_ref = self.cmb_output_comp.GetValue()
        out_rail = self.cmb_output_rail.GetValue()
        
        name = generate_regulator_name(inp_ref, out_ref, out_rail)
        self.txt_name.ChangeValue(name)
    
    def prepopulate(self, regulator):
        """Prepopulate dialog with existing regulator data."""
        self.txt_name.SetValue(regulator.name)
        
        # Set rails
        self.cmb_input_rail.SetValue(regulator.input_rail_name)
        self.cmb_output_rail.SetValue(regulator.output_rail_name)
        
        # Trigger component population
        self._on_input_rail_change(None)
        self._on_output_rail_change(None)
        
        # Set components
        self.cmb_input_comp.SetValue(regulator.input_ref_des)
        self.cmb_output_comp.SetValue(regulator.output_ref_des)
        
        # Trigger pad population
        self._on_input_comp_change(None)
        self._on_output_comp_change(None)
        
        # Check the appropriate pads
        for i in range(self.lst_input_pads.GetCount()):
            pad_name = self.lst_input_pads.GetString(i)
            if pad_name in regulator.input_pad_names:
                self.lst_input_pads.Check(i, True)
        
        for i in range(self.lst_output_pads.GetCount()):
            pad_name = self.lst_output_pads.GetString(i)
            if pad_name in regulator.output_pad_names:
                self.lst_output_pads.Check(i, True)
        
        # Set type and efficiency
        self.cmb_type.SetValue(regulator.reg_type)
        self.txt_eff.SetValue(str(regulator.efficiency))
        self._update_thermal_components(
            regulator.thermal_ref_des or regulator.input_ref_des
        )
        
        model = dict(getattr(regulator, "loss_model", None) or {})
        self.txt_curve.SetValue(format_efficiency_points(model.get("efficiency_table")))
        source = dict(model.get("efficiency_table_source") or {})
        if source.get("source") in CURVE_SOURCES:
            self.cmb_curve_source.SetValue(source["source"])
        self.txt_curve_reference.SetValue(str(source.get("reference", "") or ""))

        # Enable/disable efficiency based on type
        self.on_type_change(None)
            
    def on_ok(self, event):
        name = self.txt_name.GetValue().strip()
        if not name:
            wx.MessageBox("Name is required.", "Error", wx.OK | wx.ICON_ERROR)
            return

        inp = self.cmb_input_rail.GetValue()
        out = self.cmb_output_rail.GetValue()
        
        if not inp or not out:
             wx.MessageBox("Select both Input and Output rails.", "Error", wx.OK | wx.ICON_ERROR)
             return
        if inp == out:
            wx.MessageBox("Input and Output rails cannot be the same.", "Error", wx.OK | wx.ICON_ERROR)
            return
            
        if not self.cmb_input_comp.GetValue() or not self.lst_input_pads.GetCheckedItems():
            wx.MessageBox("Select Input Component and at least one Pad.", "Error", wx.OK | wx.ICON_ERROR)
            return
            
        if not self.cmb_output_comp.GetValue() or not self.lst_output_pads.GetCheckedItems():
            wx.MessageBox("Select Output Component and at least one Pad.", "Error", wx.OK | wx.ICON_ERROR)
            return

        if not self.cmb_thermal_comp.GetValue():
            wx.MessageBox("Select the component that dissipates the regulator loss.", "Error", wx.OK | wx.ICON_ERROR)
            return

        if self.cmb_type.GetValue() == "SWITCHING":
            try:
                eff = parse_number(self.txt_eff.GetValue())
                if not (0.0 < eff <= 1.0): raise ValueError()
            except ValueError:
                wx.MessageBox("Efficiency must be between 0.0 and 1.0", "Error", wx.OK | wx.ICON_ERROR)
                return
            try:
                parse_efficiency_points(self.txt_curve.GetValue())
            except ValueError as exc:
                wx.MessageBox(f"Efficiency curve: {exc}", "Error", wx.OK | wx.ICON_ERROR)
                return

        event.Skip()
        
    def GetValue(self):
        # Get selected pads names
        in_pads = [self.lst_input_pads.GetString(i) for i in self.lst_input_pads.GetCheckedItems()]
        out_pads = [self.lst_output_pads.GetString(i) for i in self.lst_output_pads.GetCheckedItems()]
        
        return {
            'name': self.txt_name.GetValue().strip(),
            'input_rail': self.cmb_input_rail.GetValue(),
            'input_ref_des': self.cmb_input_comp.GetValue(),
            'input_pads': in_pads,
            'output_rail': self.cmb_output_rail.GetValue(),
            'output_ref_des': self.cmb_output_comp.GetValue(),
            'output_pads': out_pads,
            'type': self.cmb_type.GetValue(),
            'efficiency': parse_number(self.txt_eff.GetValue()) if self.cmb_type.GetValue() == "SWITCHING" else 1.0,
            'thermal_ref_des': self.cmb_thermal_comp.GetValue(),
            'efficiency_table': (
                parse_efficiency_points(self.txt_curve.GetValue())
                if self.cmb_type.GetValue() == "SWITCHING" else []
            ),
            'efficiency_table_source': self.cmb_curve_source.GetValue(),
            'efficiency_table_reference': self.txt_curve_reference.GetValue().strip(),
        }
