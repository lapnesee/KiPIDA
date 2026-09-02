"""Construction of the ordered Ki-PIDA workspace pages."""

import wx

from ui.ac_analysis_panel import ACAnalysisPanel
from ui.cfd_analysis_panel import CFDAnalysisPanel
from ui.differential_analysis_panel import DifferentialAnalysisPanel
from ui.emc_analysis_panel import EMCAnalysisPanel
from ui.power_tree_panel import PowerTreePanel
from ui.runtime_settings_panel import RuntimeSettingsPanel
from ui.thermal_analysis_panel import ThermalAnalysisPanel


class DialogPages:
    """Build panels in the exact order expected by the workspace page constants."""

    def __init__(
        self, notebook, *, board, project, log_callback,
        init_results, init_log, thermal_callbacks,
    ):
        self.notebook = notebook
        self.board = board
        self.project = project
        self.log = log_callback

        self.tab_config = wx.Panel(notebook)
        self.power_tree = PowerTreePanel(
            self.tab_config, board, project=project, log_callback=log_callback,
        )
        config_sizer = wx.BoxSizer(wx.VERTICAL)
        config_sizer.Add(self.power_tree, 1, wx.EXPAND | wx.ALL, 5)
        settings = wx.BoxSizer(wx.HORIZONTAL)
        settings.Add(wx.StaticText(self.tab_config, label="Mesh Resolution (mm):"),
                     0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.txt_grid_size = wx.TextCtrl(self.tab_config, value="0.1", size=(60, -1))
        settings.Add(self.txt_grid_size, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)
        settings.Add(wx.StaticText(self.tab_config, label="Max Drop %:"),
                     0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        self.txt_drop_pct = wx.TextCtrl(self.tab_config, value="5", size=(60, -1))
        settings.Add(self.txt_drop_pct, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)
        self.chk_debug = wx.CheckBox(self.tab_config, label="Enable Debug Log")
        settings.Add(self.chk_debug, 0, wx.ALIGN_CENTER_VERTICAL)
        config_sizer.Add(settings, 0, wx.EXPAND | wx.ALL, 5)
        self.tab_config.SetSizer(config_sizer)
        notebook.AddPage(self.tab_config, "Power Tree & Config")

        self.tab_ac, self.ac_panel = self._panel_page(
            "AC Impedance", ACAnalysisPanel,
            board, rails_provider=lambda: self.power_tree.rails, log_callback=log_callback,
        )
        self.power_tree.ac_profiles_provider = self.ac_panel.get_profiles
        self.power_tree.ac_profiles_consumer = self.ac_panel.set_profiles

        self.tab_differential, self.differential_panel = self._panel_page(
            "Differential Pairs", DifferentialAnalysisPanel,
            board, project=project, log_callback=log_callback,
        )
        self.power_tree.differential_profile_provider = self.differential_panel.get_settings
        self.power_tree.differential_profile_consumer = self.differential_panel.set_settings

        self.tab_emc, self.emc_panel = self._panel_page(
            "EMI / EMC", EMCAnalysisPanel,
            board,
            differential_pairs_provider=lambda: self.differential_panel.settings.pairs,
            rails_provider=lambda: self.power_tree.rails,
            log_callback=log_callback,
        )
        self.power_tree.emc_profile_provider = self.emc_panel.get_settings
        self.power_tree.emc_profile_consumer = self.emc_panel.set_settings

        self.tab_thermal, self.thermal_panel = self._panel_page(
            "3D Thermal", ThermalAnalysisPanel,
            rails_provider=lambda: self.power_tree.rails,
            log_callback=log_callback,
            **thermal_callbacks,
        )
        self.power_tree.thermal_profile_provider = self.thermal_panel.get_settings
        self.power_tree.thermal_profile_consumer = self.thermal_panel.set_settings

        self.tab_cfd, self.cfd_panel = self._panel_page(
            "Enclosure CFD", CFDAnalysisPanel, log_callback=log_callback,
        )
        self.power_tree.cfd_profile_provider = self.cfd_panel.get_settings
        self.power_tree.cfd_profile_consumer = self.cfd_panel.set_settings

        self.tab_runtime, self.runtime_panel = self._panel_page(
            "Runtime & Acceleration", RuntimeSettingsPanel, log_callback=log_callback,
        )

        self.tab_results = wx.Panel(notebook)
        init_results(self.tab_results)
        notebook.AddPage(self.tab_results, "Results")
        self.tab_log = wx.Panel(notebook)
        init_log(self.tab_log)
        notebook.AddPage(self.tab_log, "Log")

    def _panel_page(self, title, panel_type, *args, **kwargs):
        tab = wx.Panel(self.notebook)
        panel = panel_type(tab, *args, **kwargs)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(panel, 1, wx.EXPAND | wx.ALL, 5)
        tab.SetSizer(sizer)
        self.notebook.AddPage(tab, title)
        return tab, panel
