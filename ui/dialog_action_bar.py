"""Context-aware action bar for the main Ki-PIDA workspace."""

import wx


class DialogActionBar(wx.Panel):
    """Own analysis buttons, bindings, status text, and page visibility."""

    BUTTON_SPECS = (
        ("dc", "Run DC Simulation"),
        ("ac", "Run AC Analysis"),
        ("optimize", "Optimize Decoupling"),
        ("differential", "Run Differential Z"),
        ("emc", "Run EMI/EMC"),
        ("cancel_emc", "Cancel EMI/EMC"),
        ("thermal", "Run Thermal"),
        ("coupled", "Run Coupled"),
        ("cfd", "Run Enclosure CFD"),
        ("campaign", "Build Consolidated Report"),
    )

    def __init__(self, parent, handlers, actions_by_page):
        super().__init__(parent)
        self.status = wx.StaticText(
            self, label="", style=getattr(wx, "ST_ELLIPSIZE_END", 0),
        )
        self.status.SetMinSize((-1, 22))
        self.buttons = {
            key: wx.Button(self, label=label) for key, label in self.BUTTON_SPECS
        }
        self.close_button = wx.Button(self, wx.ID_CANCEL, "Close")
        self.buttons["cancel_emc"].Disable()
        self._actions_by_page = {
            int(page): tuple(self.buttons[key] for key in keys)
            for page, keys in actions_by_page.items()
        }
        self._analysis_buttons = tuple(dict.fromkeys(
            button for buttons in self._actions_by_page.values() for button in buttons
        ))
        self._build_layout()
        self._bind(handlers)

    def _build_layout(self):
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.AddStretchSpacer()
        for key, _label in self.BUTTON_SPECS:
            buttons.Add(self.buttons[key], 0, wx.ALL, 5)
        buttons.Add(self.close_button, 0, wx.ALL, 5)
        sizer.Add(buttons, 0, wx.EXPAND)
        self.SetSizer(sizer)

    def _bind(self, handlers):
        for key, button in self.buttons.items():
            button.Bind(wx.EVT_BUTTON, handlers[key])
        self.close_button.Bind(wx.EVT_BUTTON, handlers["close"])

    def set_active_page(self, page_index):
        visible = set(self._actions_by_page.get(int(page_index), ()))
        for button in self._analysis_buttons:
            button.Show(button in visible)
        self.close_button.Show()
        self.Layout()
        parent = self.GetParent()
        if parent:
            parent.Layout()

    def set_status(self, text):
        self.status.SetLabel(str(text or ""))
        self.status.SetToolTip(str(text or ""))
