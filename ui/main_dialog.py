import wx
import wx.dataview
import sys
import os
import threading
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path

# Ensure plugin dir is in path to import modules
plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

from extractor import GeometryExtractor
from ac_model import ACModelBuilder, resolve_target_impedance
from differential_impedance import DifferentialGeometrySnapshot
from emc_analyzer import EMCGeometrySnapshot
from thermal_model import ThermalModelBuilder
from thermal_overlay import ThermalOverlayManager
from ui.dialog_pages import DialogPages
from ui.interactive_views import install_navigation
from ui.results_workspace import ResultsWorkspace
from ui.workspace_navigation import WorkspaceNavigator, build_workspace_entries
from ui.dialog_action_bar import DialogActionBar
from ui.log_stream import DialogStreamCapture
from plotter import Plotter
from analysis_adapters import (
    adapt_ac_result, adapt_cfd_result, adapt_dc_result, adapt_differential_result,
    attach_dc_remediations,
    adapt_emc_result, adapt_thermal_result,
)
from application.ac_controller import (
    ACAnalysisCancelled, ACAnalysisController, ACControllerCallbacks, ACRunRequest,
)
from application.dc_controller import (
    DCAnalysisCancelled, DCAnalysisController, DCControllerCallbacks,
    prepare_dc_request,
)
from application.thermal_controller import (
    ThermalAnalysisCancelled, ThermalAnalysisController, ThermalControllerCallbacks,
    ThermalRunRequest, dc_copper_loss_points,
)
from application.differential_controller import (
    DifferentialAnalysisCancelled, DifferentialAnalysisController,
    DifferentialControllerCallbacks, DifferentialRunRequest,
)
from application.emc_controller import (
    EMCAnalysisCancelled, EMCAnalysisController, EMCControllerCallbacks,
    EMCRunRequest,
)
from application.cfd_controller import (
    CFDAnalysisCancelled, CFDAnalysisController, CFDControllerCallbacks,
    CFDRunRequest,
)
from application.report_presenters import (
    format_ac_report, format_cfd_report, format_dc_report,
    format_differential_report, format_emc_report, format_thermal_report,
)
from application.thermal_plot_presenter import render_thermal_plots
from application.dc_plot_presenter import flatten_dc_plot_groups, render_dc_plots

class KiPIDA_MainDialog(wx.Dialog):
    PAGE_CONFIG = 0
    PAGE_AC = 1
    PAGE_DIFFERENTIAL = 2
    PAGE_EMC = 3
    PAGE_THERMAL = 4
    PAGE_CFD = 5
    PAGE_RUNTIME = 6
    PAGE_RESULTS = 7
    PAGE_LOG = 8
    AC_MAX_NETWORK_NODES = 100000

    ANALYSIS_CONTROLLERS = (
        ("DC analysis", "dc_controller"),
        ("AC analysis", "ac_controller"),
        ("Differential analysis", "differential_controller"),
        ("EMI/EMC analysis", "emc_controller"),
        ("Thermal analysis", "thermal_controller"),
        ("Enclosure CFD", "cfd_controller"),
    )

    def __init__(self, parent, board_adapter, project=None):
        super(KiPIDA_MainDialog, self).__init__(
            parent,
            title="Ki-PIDA: Power Integrity Analyzer",
            style=(
                wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER |
                wx.MAXIMIZE_BOX | wx.MINIMIZE_BOX
            ),
        )
        
        self.SetSize((1180, 760))
        self.SetMinSize((950, 600))
        
        self.board = board_adapter
        self.project = project
        self.cfd_controller = CFDAnalysisController(
            dispatch=lambda callback, *args: wx.CallAfter(callback, *args),
        )
        self.ac_controller = ACAnalysisController(
            dispatch=lambda callback, *args: wx.CallAfter(callback, *args),
        )
        self.dc_controller = DCAnalysisController(
            dispatch=lambda callback, *args: wx.CallAfter(callback, *args),
        )
        self.thermal_controller = ThermalAnalysisController(
            dispatch=lambda callback, *args: wx.CallAfter(callback, *args),
        )
        self.differential_controller = DifferentialAnalysisController(
            dispatch=lambda callback, *args: wx.CallAfter(callback, *args),
        )
        self.emc_controller = EMCAnalysisController(
            dispatch=lambda callback, *args: wx.CallAfter(callback, *args),
        )
        self._thermal_plot_thread = None
        self._dc_plot_thread = None
        self._result_generation = 0
        self._thermal_result_generation = 0
        self._closing = False
        self._plot_lock = threading.Lock()
        # Kept only for the dialog lifetime.  Mesh and CSR reuse is guarded by
        # a live-board fingerprint, so unsaved edits in PCB Editor invalidate
        # it without requiring a KiCad/plugin restart.
        self._thermal_session_cache = {}
        self._thermal_board_signature = None
        self._last_valid_differential_snapshot = None
        self._last_valid_differential_pair_signature = ()
        
        self._init_ui()
        self.Center()
        
        self._stream_capture = DialogStreamCapture(self.log)
        self._stream_capture.install()
        
        self.log("Ki-PIDA UI Initialized.")
        if not self.board:
             self.log("ERROR: No board object connected. Plugin will not function properly.")
        else:
             self.log(f"Board object connected: {type(self.board)}")
        
        if self.project:
            self.log(f"Project: {self.project.name} at {self.project.path}")
        else:
            self.log("WARNING: No project object available.")

        
    def _init_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        self.workspace_panel = wx.Panel(self)
        workspace_sizer = wx.BoxSizer(wx.HORIZONTAL)
        content_panel = wx.Panel(self.workspace_panel)
        content_sizer = wx.BoxSizer(wx.VERTICAL)
        self.lbl_workspace_title = wx.StaticText(content_panel, label="Power Tree & DC")
        title_font = self.lbl_workspace_title.GetFont()
        title_font.SetPointSize(title_font.GetPointSize() + 2)
        title_font.SetWeight(wx.FONTWEIGHT_BOLD)
        self.lbl_workspace_title.SetFont(title_font)
        self.lbl_workspace_description = wx.StaticText(content_panel, label="")
        content_sizer.Add(self.lbl_workspace_title, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)
        content_sizer.Add(self.lbl_workspace_description, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.notebook = wx.Simplebook(content_panel)
        
        self.pages = DialogPages(
            self.notebook, board=self.board, project=self.project,
            log_callback=self.log, init_results=self._init_results_tab,
            init_log=self._init_log_tab,
            thermal_callbacks={
                "mesh_context_provider": self._thermal_mesh_context,
                "inject_overlay_callback": self._inject_thermal_overlay,
                "clear_overlay_callback": self._clear_thermal_overlay,
                "clear_cache_callback": self._clear_thermal_session_cache,
            },
        )
        for name in (
            "tab_config", "power_tree", "txt_grid_size", "txt_drop_pct", "chk_debug",
            "tab_ac", "ac_panel", "tab_differential", "differential_panel",
            "tab_emc", "emc_panel", "tab_thermal", "thermal_panel",
            "tab_cfd", "cfd_panel", "tab_runtime", "runtime_panel",
            "tab_results", "tab_log",
        ):
            setattr(self, name, getattr(self.pages, name))
        
        content_sizer.Add(self.notebook, 1, wx.EXPAND)
        content_panel.SetSizer(content_sizer)
        self.workspace_nav = WorkspaceNavigator(
            self.workspace_panel,
            entries=self._workspace_entries(),
            on_select=self._on_workspace_selected,
        )
        workspace_sizer.Add(self.workspace_nav, 0, wx.EXPAND | wx.ALL, 5)
        workspace_sizer.Add(content_panel, 1, wx.EXPAND | wx.TOP | wx.RIGHT | wx.BOTTOM, 5)
        self.workspace_panel.SetSizer(workspace_sizer)
        main_sizer.Add(self.workspace_panel, 1, wx.EXPAND)
        
        actions_by_page = {
            self.PAGE_CONFIG: ("dc",),
            self.PAGE_AC: ("ac", "optimize"),
            self.PAGE_DIFFERENTIAL: ("differential",),
            self.PAGE_EMC: ("emc", "cancel_emc"),
            self.PAGE_THERMAL: ("thermal", "coupled"),
            self.PAGE_CFD: ("cfd",),
            self.PAGE_RESULTS: ("campaign",),
        }
        handlers = {
            "dc": self.on_run, "ac": self.on_run_ac,
            "optimize": self.on_optimize_decoupling,
            "differential": self.on_run_differential,
            "emc": self.on_run_emc, "cancel_emc": self.on_cancel_emc,
            "thermal": self.on_run_thermal, "coupled": self.on_run_coupled_thermal,
            "cfd": self.on_run_cfd, "campaign": self.on_build_campaign_report,
            "close": self.on_close,
        }
        self.action_bar = DialogActionBar(self, handlers, actions_by_page)
        self.lbl_interaction_status = self.action_bar.status
        self.btn_run = self.action_bar.buttons["dc"]
        self.btn_run_ac = self.action_bar.buttons["ac"]
        self.btn_optimize = self.action_bar.buttons["optimize"]
        self.btn_run_differential = self.action_bar.buttons["differential"]
        self.btn_run_emc = self.action_bar.buttons["emc"]
        self.btn_cancel_emc = self.action_bar.buttons["cancel_emc"]
        self.btn_run_thermal = self.action_bar.buttons["thermal"]
        self.btn_run_coupled = self.action_bar.buttons["coupled"]
        self.btn_run_cfd = self.action_bar.buttons["cfd"]
        self.btn_cancel = self.action_bar.close_button
        main_sizer.Add(self.action_bar, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        
        self.SetSizer(main_sizer)
        
        # Bind events
        self.notebook.Bind(wx.EVT_BOOKCTRL_PAGE_CHANGED, self.on_notebook_page_changed)
        self._update_contextual_actions(self.PAGE_CONFIG)
        self.workspace_nav.select_page(self.PAGE_CONFIG)
        install_navigation(self)
        
        # Auto-scan board after UI is ready
        wx.CallAfter(self.power_tree.auto_scan)

    def on_notebook_page_changed(self, event):
        selection = event.GetSelection()
        self._sync_workspace_chrome(selection)
        if selection == self.PAGE_AC:
            wx.CallAfter(self.ac_panel.refresh)
        elif selection == self.PAGE_DIFFERENTIAL:
            wx.CallAfter(self.differential_panel.refresh)
        elif selection == self.PAGE_EMC:
            wx.CallAfter(self.emc_panel.refresh_live_board)
        elif selection == self.PAGE_THERMAL:
            if not self.thermal_panel.settings.components:
                wx.CallAfter(self.thermal_panel.refresh_components)
            wx.CallAfter(self.thermal_panel._update_mesh_cost)
        elif selection == self.PAGE_CFD:
            wx.CallAfter(self.cfd_panel._update_estimate)
        elif selection == self.PAGE_RUNTIME:
            wx.CallAfter(self.runtime_panel.refresh_status)
        event.Skip()

    @classmethod
    def _workspace_entries(cls):
        return build_workspace_entries({
            "config": cls.PAGE_CONFIG, "ac": cls.PAGE_AC,
            "differential": cls.PAGE_DIFFERENTIAL, "emc": cls.PAGE_EMC,
            "thermal": cls.PAGE_THERMAL, "cfd": cls.PAGE_CFD,
            "results": cls.PAGE_RESULTS, "runtime": cls.PAGE_RUNTIME,
            "log": cls.PAGE_LOG,
        })

    def _on_workspace_selected(self, entry):
        self._select_workspace(entry.page_index)

    def _select_workspace(self, page_index):
        page_index = int(page_index)
        previous = self.notebook.GetSelection()
        if previous != page_index:
            self.notebook.SetSelection(page_index)
        # Some wx builds bundled by KiCad change a Simplebook page without
        # emitting EVT_BOOKCTRL_PAGE_CHANGED.  Keep the surrounding chrome in
        # sync here as well as in the native event handler.
        self._sync_workspace_chrome(page_index)

    def _sync_workspace_chrome(self, page_index):
        self._update_workspace_header(page_index)
        self.workspace_nav.select_page(page_index)
        self._update_contextual_actions(page_index)

    def _update_workspace_header(self, page_index):
        entry = self.workspace_nav.entry_for_page(int(page_index))
        self.lbl_workspace_title.SetLabel(entry.title)
        self.lbl_workspace_description.SetLabel(entry.description)
        self.lbl_workspace_description.Wrap(max(300, self.GetClientSize().width - 280))

    def _update_contextual_actions(self, page_index):
        """Only expose actions that are meaningful in the active workspace."""
        self.action_bar.set_active_page(page_index)

    def _running_analysis_label(self):
        for label, attribute in self.ANALYSIS_CONTROLLERS:
            controller = getattr(self, attribute, None)
            if controller is not None and controller.is_running:
                return label
        return None

    def _ensure_analysis_slot(self):
        """Allow only one resource-intensive analysis at a time."""
        running = self._running_analysis_label()
        if running is None:
            return True
        message = (
            f"{running} is already running. Wait for it to complete or cancel it "
            "before starting another analysis."
        )
        self.log(message)
        self._set_interaction_status(f"{running} · running")
        self._select_workspace(self.PAGE_LOG)
        wx.MessageBox(message, "Analysis already running", wx.OK | wx.ICON_INFORMATION)
        return False

    def _refresh_live_board_state(self):
        """Re-read KiCad's live IPC board data before a new analysis run."""
        if self.board is None:
            raise RuntimeError(
                "No live KiCad PCB is connected. Close this window and relaunch Ki-PIDA "
                "from an open PCB Editor document."
            )
        try:
            self._thermal_geometry_context = None
            signature = ThermalModelBuilder.board_geometry_signature(self.board)
            board_changed = signature != self._thermal_board_signature
            if board_changed:
                ThermalModelBuilder.invalidate_board_cache(self.board)
                self._thermal_session_cache.clear()
                self._thermal_board_signature = signature
                self.log("Live PCB geometry changed; invalidated thermal mesh/CSR cache.")
            else:
                self.log("Live PCB geometry unchanged; thermal mesh/CSR cache remains eligible.")
            self.ac_panel.refresh(force_discovery=True)
            self.differential_panel.refresh_live_board()
            self.emc_panel.refresh_live_board()
            self.thermal_panel.refresh_components(preserve_user=True)
            self.log("Refreshed live PCB geometry and component discovery.")
        except Exception as exc:
            self.log(f"Live PCB refresh warning: {exc}")
            raise RuntimeError(f"Live KiCad PCB refresh failed: {exc}") from exc

    def _clear_thermal_session_cache(self):
        self._thermal_session_cache.clear()
        ThermalModelBuilder.invalidate_board_cache(self.board)
        self.log("Cleared the in-session thermal mesh, CSR, CUDA workspace and copper-geometry cache.")

    def _inject_thermal_overlay(self):
        if getattr(self, "thermal_mesh", None) is None or getattr(self, "thermal_result", None) is None:
            message = "Run a thermal analysis before injecting its KiCad heat overlay."
            self.log(message)
            wx.MessageBox(message, "Thermal overlay", wx.OK | wx.ICON_INFORMATION)
            return
        try:
            manager = ThermalOverlayManager(self.board, log_callback=self.log)
            settings = self.thermal_panel.get_settings()
            manager.inject(
                self.thermal_mesh, self.thermal_result,
                color_map=settings.color_map,
                color_scale_minimum_c=settings.resolved_color_scale_minimum_c(),
                color_scale_maximum_c=settings.resolved_color_scale_maximum_c(),
            )
            self.log("KiCad thermal overlay injection completed.")
        except Exception as exc:
            self.log(f"Thermal overlay injection error: {exc}")
            wx.MessageBox(str(exc), "Thermal overlay", wx.OK | wx.ICON_ERROR)

    def _clear_thermal_overlay(self):
        try:
            manager = ThermalOverlayManager(self.board, log_callback=self.log)
            removed = manager.clear()
            self.log(f"KiCad thermal overlay clear completed ({removed} image(s)).")
        except Exception as exc:
            self.log(f"Thermal overlay clear error: {exc}")
            wx.MessageBox(str(exc), "Thermal overlay", wx.OK | wx.ICON_ERROR)

    def _board_file_path(self):
        project_path = getattr(self.project, "path", "")
        if not project_path:
            return None
        candidate = Path(project_path).with_suffix(".kicad_pcb")
        return str(candidate) if candidate.exists() else None
    
    def _init_results_tab(self, parent):
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.results_workspace = ResultsWorkspace(
            parent,
            history_directory=self._results_history_directory(),
            log_callback=self.log,
            interaction_status_callback=self._set_interaction_status,
        )
        sizer.Add(self.results_workspace, 1, wx.EXPAND | wx.ALL, 5)
        parent.SetSizer(sizer)

    def _set_interaction_status(self, text):
        if not hasattr(self, "action_bar"):
            return
        value = str(text or "")
        self.action_bar.set_status(value.replace("\n", " — "))

    def _results_history_directory(self):
        """Keep analysis archives alongside the active KiCad board/project."""
        board_path = self._board_file_path()
        if board_path:
            return Path(board_path).parent / "KiPIDA-results"
        project_path = getattr(self.project, "path", "") if self.project else ""
        if project_path:
            return Path(project_path).parent / "KiPIDA-results"
        return None

    def _publish_results(self, analysis_id, report, plots=None, structured_result=None):
        """Publish one analysis without discarding other session results."""
        page = self.results_workspace.publish(
            analysis_id, report, plots, result=structured_result,
        )
        self._select_workspace(self.PAGE_RESULTS)
        return page


    def _campaign_output_directory(self):
        """Where the consolidated report is written: beside the board."""
        history = self._results_history_directory()
        return history if history is not None else Path.cwd() / "KiPIDA-results"

    @staticmethod
    def _board_fingerprint(board_path):
        """SHA-256 of the .kicad_pcb, used to tell campaigns apart.

        The per-domain adapters do not currently stamp board_fingerprint, so
        without this the field is empty and comparing a before/after campaign
        cannot tell whether the board changed. Hashing the file is the same
        thing the DesignModel does, and is cheap enough at report time.
        """
        if not board_path:
            return ""
        try:
            import hashlib

            digest = hashlib.sha256()
            with open(board_path, "rb") as handle:
                for block in iter(lambda: handle.read(131072), b""):
                    digest.update(block)
            return digest.hexdigest()
        except OSError:
            return ""

    def on_build_campaign_report(self, _event):
        """Aggregate this session's analyses into one report and open it.

        This consolidates rather than re-runs.  Each per-domain button already
        produces an AnalysisResult; this merges them, deduplicates findings
        that describe the same physical defect across domains, ranks the
        resulting actions by gain over effort, and writes a standalone HTML
        report plus CSV exports.  Domains never run are absent, which the
        campaign scores as NO_DATA -- never as passing.
        """
        from campaign import CampaignResult
        from report.html_report import write_campaign_html
        from report.csv_export import write_actions_csv, write_findings_csv

        results = self.results_workspace.session_results()
        if not results:
            wx.MessageBox(
                "No analysis has produced a result yet in this session.\n\n"
                "Run at least one analysis (DC, AC, Differential, EMI/EMC, "
                "Thermal or CFD), then build the consolidated report.",
                "Nothing to consolidate", wx.OK | wx.ICON_INFORMATION,
            )
            return

        board_path = self._board_file_path()
        try:
            self._set_interaction_status("Consolidated report · building")
            campaign = CampaignResult(
                project_name=Path(board_path).stem if board_path else "KiPIDA",
                board_fingerprint=next(
                    (r.board_fingerprint for r in results if r.board_fingerprint),
                    self._board_fingerprint(board_path),
                ),
                results=list(results),
            ).recompute()

            directory = self._campaign_output_directory()
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            html_path = directory / f"campaign-{stamp}.html"
            # Plots live in per-analysis subdirectories of the history folder,
            # and are recorded by bare file name, so the renderer needs a root
            # to search or every figure reports itself unavailable.
            write_campaign_html(campaign, html_path, artifact_roots=(directory,))
            write_findings_csv(campaign, directory / f"findings-{stamp}.csv")
            write_actions_csv(campaign, directory / f"actions-{stamp}.csv")

            domains = ", ".join(score.domain for score in campaign.domain_scores)
            self.log(
                f"Consolidated report: {campaign.overall_status} across [{domains}], "
                f"{len(campaign.actions)} action(s) from {len(results)} analysis result(s)."
            )
            self.log(f"Report written to {html_path}")
            self._set_interaction_status(
                f"Consolidated report · {campaign.overall_status} · {len(campaign.actions)} action(s)"
            )
            wx.LaunchDefaultBrowser(html_path.as_uri())
        except Exception as exc:
            self.log(f"Consolidated report failed: {exc}")
            self._set_interaction_status("Consolidated report · failed")
            wx.MessageBox(str(exc), "Consolidated Report Error", wx.OK | wx.ICON_ERROR)

    def _init_log_tab(self, parent):
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.log_ctrl = wx.TextCtrl(parent, style=wx.TE_MULTILINE | wx.TE_READONLY)
        sizer.Add(self.log_ctrl, 1, wx.EXPAND | wx.ALL, 5)
        parent.SetSizer(sizer)

    def log(self, msg):
        if not hasattr(self, 'log_ctrl'): return
        if not wx.IsMainThread():
            if not self._closing:
                wx.CallAfter(self.log, msg)
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_ctrl.AppendText(f"[{timestamp}] {msg}\n")
        self.log_ctrl.ShowPosition(self.log_ctrl.GetLastPosition())
        
    def _thermal_mesh_context(self):
        try:
            geometry = getattr(self, "_thermal_geometry_context", None)
            if geometry is None:
                extractor = GeometryExtractor(self.board)
                bounds = extractor.get_board_bounds(board_file_path=self._board_file_path())
                stackup = extractor.get_board_stackup()
                if not bounds:
                    return {}
                copper_layers = max(1, len(stackup.get("layer_order", [])))
                geometry = {
                    "width_mm": bounds[2] - bounds[0],
                    "height_mm": bounds[3] - bounds[1],
                    "thermal_layers": copper_layers * 2 - 1,
                }
                self._thermal_geometry_context = geometry
            runtime = self.runtime_panel.get_settings() if hasattr(self, "runtime_panel") else None
            return dict(geometry, **{
                "cuda_available": bool(
                    runtime and runtime.cuda_enabled and getattr(self.runtime_panel, "_devices", [])
                ),
                "cuda_min_nodes": runtime.cuda_min_nodes if runtime else 100000,
                "memory_limit_gib": runtime.memory_limit_gib if runtime else 0.0,
            })
        except Exception:
            return {}

    def on_run(self, event, update_results=True):
        if not self._ensure_analysis_slot():
            return
        self._select_workspace(self.PAGE_LOG)
        try:
            self._refresh_live_board_state()
            system_rails = self.power_tree.rails
            if not system_rails:
                raise ValueError("No power rails defined.")
            grid_size = max(0.01, float(self.txt_grid_size.GetValue()))
            request = prepare_dc_request(
                self.board,
                system_rails,
                grid_size,
                self.runtime_panel.get_settings(persist=True),
                self.chk_debug.GetValue(),
                log_callback=self.log,
                board_path=self._board_file_path(),
            )
            callbacks = DCControllerCallbacks(
                on_progress=self._dc_progress,
                on_complete=lambda result: self._finish_dc_job(result, update_results),
                on_error=self._fail_dc_job,
                on_log=self._dc_worker_log,
            )
            self.btn_run.Disable()
            self._set_interaction_status("DC analysis · running")
            self.dc_controller.start(request, callbacks)
        except ValueError as exc:
            self.log(f"DC Analysis Error: {exc}")
            wx.MessageBox(str(exc), "DC Analysis Error", wx.OK | wx.ICON_ERROR)
        except Exception as exc:
            self.log(f"DC preparation failed: {exc}")
            wx.MessageBox(str(exc), "DC Preparation Error", wx.OK | wx.ICON_ERROR)

    def _dc_worker_log(self, message):
        if not self._closing:
            self.log(message)

    def _dc_progress(self, completed, total, detail):
        if self._closing:
            return
        self.log(f"DC progress: {completed}/{total} ({detail})")
        self._set_interaction_status(f"DC analysis · {completed}/{total} · {detail}")

    def _finish_dc_job(self, result, update_results=True):
        if self._closing:
            return
        self.btn_run.Enable()
        self.system_results = result
        self._set_interaction_status("DC analysis · complete")
        if update_results and result:
            self._update_results_ui()

    def _fail_dc_job(self, exc):
        if self._closing:
            return
        self.btn_run.Enable()
        if isinstance(exc, DCAnalysisCancelled):
            self.log("DC analysis cancelled.")
            self._set_interaction_status("DC analysis · cancelled")
            return
        self.log(f"DC Analysis Error: {exc}")
        self._set_interaction_status("DC analysis · failed")
        wx.MessageBox(str(exc), "DC Analysis Error", wx.OK | wx.ICON_ERROR)

    def _prepare_ac_analysis(self):
        self._refresh_live_board_state()
        if self.ac_panel.choice_rail.GetCount() == 0:
            self.ac_panel.refresh(force_discovery=True)
        settings = self.ac_panel.get_settings()
        if settings is None:
            raise ValueError("Select a power rail for AC analysis.")

        rail = next((item for item in self.power_tree.rails if item.net_name == settings.rail_name), None)
        if rail is None:
            raise ValueError(f"Power rail '{settings.rail_name}' is not available.")
        # Source and port are resolved automatically when left unset. A rail
        # fed by a regulator has no UnifiedSource to select, so demanding one
        # here made every such rail unanalysable whatever the user picked.
        #
        # The impedance target is resolved the same way: left blank it follows
        # from the rail's own voltage and configured load, so a board is never
        # judged against a number nobody chose.
        target_ohm, target_provenance = resolve_target_impedance(rail, settings)
        settings = replace(
            settings,
            target_impedance_ohm=target_ohm,
            target_impedance_provenance=target_provenance,
        )
        self.log(f"AC target impedance: {target_provenance}.")
        debug_mode = self.chk_debug.GetValue()
        builder = ACModelBuilder(self.board, debug=debug_mode, log_callback=self.log)
        network = builder.build(
            rail, settings, grid_size_mm=max(0.1, float(settings.mesh_resolution_mm)),
            all_rails=self.power_tree.rails,
        )
        self.log(
            f"AC network: {network.node_count:,} nodes, requested grid "
            f"{network.requested_grid_size_mm:g} mm, effective grid "
            f"{network.effective_grid_size_mm:g} mm, "
            f"{settings.frequency_points} frequency points."
        )
        if network.node_count > self.AC_MAX_NETWORK_NODES:
            raise ValueError(
                f"AC mesh contains {network.node_count:,} nodes, above the safe limit of "
                f"{self.AC_MAX_NETWORK_NODES:,}. Increase 'AC mesh resolution' above "
                f"{settings.mesh_resolution_mm:g} mm and retry."
            )
        return settings, network

    def _ac_progress(self, completed, total, detail):
        if self._closing:
            return
        interval = max(1, total // 10)
        if completed == total or completed % interval == 0:
            self.log(f"AC progress: {completed}/{total} ({detail})")
            self._set_interaction_status(f"AC analysis · {completed}/{total} · {detail}")

    def on_run_ac(self, event):
        if self.ac_controller.is_running:
            self.ac_controller.cancel()
            self.btn_run_ac.Disable()
            self.btn_run_ac.SetLabel("Cancelling AC Analysis...")
            self._set_interaction_status("AC analysis · cancelling")
            self.log("Cancellation requested for AC impedance analysis.")
            return
        self._start_ac_job(optimize=False)

    def on_optimize_decoupling(self, event):
        if self.ac_controller.is_running:
            return
        self._start_ac_job(optimize=True)

    def _start_ac_job(self, optimize=False):
        if not self._ensure_analysis_slot():
            return
        self._select_workspace(self.PAGE_LOG)
        label = "Decoupling Optimization" if optimize else "AC Impedance Analysis"
        self.log(f"--- Starting {label} ---")
        try:
            settings, network = self._prepare_ac_analysis()
            compute_settings = self.runtime_panel.get_settings(persist=True)
            if compute_settings.backend == "AUTO":
                # AUTO used to be rewritten to CPU here, before the solver had
                # any say. The backend already handles a complex non-Hermitian
                # matrix with BiCGSTAB, and the sweep already falls back to CPU
                # on non-convergence, so let it choose -- guarded by the
                # first-point accuracy audit.
                #
                # Report the decision the backend will actually make rather
                # than an intention: AUTO only reaches CUDA once the network
                # clears the threshold, and announcing an attempt that the
                # node count rules out is how a log starts lying.
                #
                # A sweep is judged by cuda_min_nodes_sweep, not the
                # single-solve cuda_min_nodes, so quote the bar that applies.
                nodes = int(getattr(network, "node_count", 0) or 0)
                threshold = int(
                    getattr(compute_settings, "cuda_min_nodes_sweep", None)
                    or getattr(compute_settings, "cuda_min_nodes", 0) or 0
                )
                if nodes >= threshold:
                    self.log(
                        f"AC backend AUTO: {nodes:,} nodes reaches the {threshold:,}-node "
                        "CUDA threshold; the first frequency point will be verified "
                        "against a CPU direct solve, falling back to CPU if they "
                        "disagree or if CUDA fails to converge."
                    )
                else:
                    self.log(
                        f"AC backend AUTO: {nodes:,} nodes is below the {threshold:,}-node "
                        "CUDA threshold, so this sweep runs on CPU. Force CUDA in Runtime "
                        "settings to use the GPU and its accuracy audit anyway."
                    )
            request = ACRunRequest(
                settings=settings,
                network=network,
                compute_settings=compute_settings,
                debug=self.chk_debug.GetValue(),
                optimize=bool(optimize),
            )
            self._active_ac_settings = deepcopy(settings)
            callbacks = ACControllerCallbacks(
                on_progress=self._ac_progress,
                on_complete=self._finish_ac_job,
                on_error=self._fail_ac_job,
                on_log=self._ac_worker_log,
            )
            self.btn_run_ac.SetLabel("Cancel AC Analysis")
            self.btn_run_ac.Enable()
            self.btn_optimize.Disable()
            self._set_interaction_status(f"{label} · running")
            self.ac_controller.start(request, callbacks)
        except Exception as exc:
            self.btn_run_ac.Enable()
            self.btn_run_ac.SetLabel("Run AC Analysis")
            self.btn_optimize.Enable()
            self._fail_ac_job(exc)

    def _finish_ac_job(self, result, optimization=None):
        if self._closing:
            return
        self.btn_run_ac.Enable()
        self.btn_run_ac.SetLabel("Run AC Analysis")
        self.btn_optimize.Enable()
        self.ac_result = optimization.optimized if optimization else result
        self.ac_optimization_result = optimization
        settings = getattr(self, "_active_ac_settings", None)
        self._active_ac_settings = None
        self._set_interaction_status("AC analysis · complete")
        self._update_ac_results_ui(result, optimization, settings)

    def _fail_ac_job(self, exc):
        if self._closing:
            return
        self.btn_run_ac.Enable()
        self.btn_run_ac.SetLabel("Run AC Analysis")
        self.btn_optimize.Enable()
        self._active_ac_settings = None
        if isinstance(exc, ACAnalysisCancelled):
            self._set_interaction_status("AC analysis · cancelled")
            self.log("AC analysis cancelled.")
            return
        self._set_interaction_status("AC analysis · failed")
        self.log(f"AC Analysis Error: {exc}")
        wx.MessageBox(str(exc), "AC Analysis Error", wx.OK | wx.ICON_ERROR)

    def _ac_worker_log(self, message):
        if not self._closing:
            self.log(message)

    def _update_ac_results_ui(self, result, optimization=None, settings=None):
        self._result_generation += 1
        plotter = Plotter(debug=self.chk_debug.GetValue())
        bitmap = plotter.plot_impedance_sweep(
            result,
            optimization.optimized if optimization else None,
        )
        self._publish_results(
            "AC", format_ac_report(result, optimization, settings),
            [("AC Impedance", bitmap)] if bitmap else [],
            structured_result=adapt_ac_result(result, optimization, settings),
        )

    def _prepare_differential_analysis(self):
        self._refresh_live_board_state()
        settings = self.differential_panel.get_settings()
        pairs = [pair for pair in settings.pairs if pair.enabled]
        if not pairs:
            raise ValueError("No enabled differential pairs are available. Scan or add a pair first.")
        stackup = self.differential_panel.get_stackup()
        if stackup is None or not stackup.layers:
            raise ValueError("No PCB stackup is available for differential impedance analysis.")
        extractor = GeometryExtractor(
            self.board,
            debug=self.chk_debug.GetValue(),
            log_callback=self.log,
            board_path=self._board_file_path(),
        )
        snapshot = DifferentialGeometrySnapshot.capture(
            extractor, pairs, settings.reference_net_names
        )
        track_count = sum(len(items) for items in snapshot.tracks_by_net.values())
        pair_signature = tuple(sorted(
            (pair.positive_net, pair.negative_net) for pair in pairs
        ))
        if track_count == 0:
            # KiCad IPC can transiently return empty collection facades while
            # the editor refreshes.  Retry with a new extractor before ever
            # publishing a misleading NO_DATA result.
            retry = DifferentialGeometrySnapshot.capture(
                GeometryExtractor(
                    self.board, debug=self.chk_debug.GetValue(),
                    log_callback=self.log, board_path=self._board_file_path(),
                ),
                pairs, settings.reference_net_names,
            )
            retry_count = sum(len(items) for items in retry.tracks_by_net.values())
            if retry_count:
                snapshot = retry
                track_count = retry_count
                self.log("Differential geometry retry recovered live routed tracks.")
            elif (
                self._last_valid_differential_snapshot is not None
                and self._last_valid_differential_pair_signature == pair_signature
            ):
                snapshot = self._last_valid_differential_snapshot
                self.log(
                    "WARNING: KiCad IPC returned zero tracks/zones; using the last valid "
                    "differential snapshot for this unchanged pair set."
                )
            else:
                raise RuntimeError(
                    "KiCad IPC returned zero routed tracks. Analysis was cancelled to avoid "
                    "false NO_DATA findings; wait for PCB Editor synchronization and retry."
                )
        if track_count:
            self._last_valid_differential_snapshot = snapshot
            self._last_valid_differential_pair_signature = pair_signature
        return settings, pairs, stackup, snapshot

    def _differential_progress(self, completed, total, detail):
        if self._closing:
            return
        self.log(f"Differential progress: {completed}/{total} ({detail})")
        self._set_interaction_status(
            f"Differential analysis · {completed}/{total} · {detail}"
        )

    def on_run_differential(self, event):
        if self.differential_controller.is_running:
            return
        if not self._ensure_analysis_slot():
            return
        self._select_workspace(self.PAGE_LOG)
        self.log("--- Starting Differential Pair Impedance Analysis ---")
        try:
            settings, pairs, stackup, snapshot = self._prepare_differential_analysis()
        except Exception as exc:
            self.log(f"Differential Setup Error: {exc}")
            wx.MessageBox(str(exc), "Differential Setup Error", wx.OK | wx.ICON_ERROR)
            return
        self.btn_run_differential.Disable()
        self._set_interaction_status("Differential analysis · running")
        request = DifferentialRunRequest(
            settings=settings,
            pairs=tuple(pairs),
            stackup=stackup,
            snapshot=snapshot,
            debug=self.chk_debug.GetValue(),
            plot_lock=self._plot_lock,
        )
        callbacks = DifferentialControllerCallbacks(
            on_progress=self._differential_progress,
            on_complete=self._finish_differential_job,
            on_error=self._fail_differential_analysis,
            on_log=lambda message: self.log(message) if not self._closing else None,
        )
        self.differential_controller.start(request, callbacks)

    def _finish_differential_job(self, outcome):
        if self._closing:
            return
        self._finish_differential_analysis(
            list(outcome.results), outcome.stackup, outcome.impedance_png,
            outcome.stackup_png, outcome.target_tolerance_pct,
        )

    def _finish_differential_analysis(self, results, stackup, impedance_png, stackup_png,
                                      target_tolerance_pct):
        self.btn_run_differential.Enable()
        self._set_interaction_status("Differential analysis · complete")
        self.differential_panel.apply_results(results)
        report = format_differential_report(results, stackup, target_tolerance_pct)
        plots = []
        if impedance_png:
            plots.append(("Differential Z", Plotter.bitmap_from_png(impedance_png)))
        if stackup_png:
            plots.append(("Stackup", Plotter.bitmap_from_png(stackup_png)))
        self._publish_results(
            "DIFFERENTIAL", report, plots,
            structured_result=adapt_differential_result(results, stackup, target_tolerance_pct),
        )
        self.log("Differential impedance results ready.")

    def _fail_differential_analysis(self, exc):
        if self._closing:
            return
        self.btn_run_differential.Enable()
        if isinstance(exc, DifferentialAnalysisCancelled):
            self.log("Differential analysis cancelled.")
            self._set_interaction_status("Differential analysis · cancelled")
            return
        self.log(f"Differential Analysis Error: {exc}")
        self._set_interaction_status("Differential analysis · failed")
        wx.MessageBox(str(exc), "Differential Analysis Error", wx.OK | wx.ICON_ERROR)

    def _prepare_emc_analysis(self):
        self._refresh_live_board_state()
        settings = self.emc_panel.get_settings()
        enabled_sources = [source for source in settings.sources if source.enabled]
        if not enabled_sources:
            self.log("EMI/EMC note: no clock or switching source is enabled; geometry-only rules will run.")
        pairs = [pair for pair in self.differential_panel.settings.pairs if pair.enabled]
        snapshot = EMCGeometrySnapshot.capture(
            self.board,
            settings,
            rails=self.power_tree.rails,
            differential_pairs=pairs,
            board_file_path=self._board_file_path(),
            log_callback=self.log,
        )
        ac_results = []
        ac_result = getattr(self, "ac_result", None)
        if ac_result is not None:
            rail_name = self.ac_panel._selected_text(self.ac_panel.choice_rail) or "Last analysed rail"
            ac_results.append((rail_name, ac_result))
        differential_results = dict(self.differential_panel.results)
        thermal_result = getattr(self, "thermal_result", None)
        return (
            settings, pairs, snapshot, ac_results, differential_results,
            thermal_result, self._board_file_path(),
        )

    def _emc_progress(self, completed, total, detail):
        if self._closing:
            return
        self.log(f"EMI/EMC progress: {completed}/{total} ({detail})")
        self._set_interaction_status(f"EMI/EMC · {completed}/{total} · {detail}")

    def on_run_emc(self, _event):
        if self.emc_controller.is_running:
            return
        if not self._ensure_analysis_slot():
            return
        self._select_workspace(self.PAGE_LOG)
        self.log("--- Starting EMI / EMC Pre-compliance Analysis ---")
        try:
            (
                settings, pairs, snapshot, ac_results, differential_results,
                thermal_result, board_file_path,
            ) = self._prepare_emc_analysis()
        except Exception as exc:
            self.log(f"EMI/EMC Setup Error: {exc}")
            wx.MessageBox(str(exc), "EMI/EMC Setup Error", wx.OK | wx.ICON_ERROR)
            return
        if settings.phase10.enabled and settings.phase10.auto_run_full_wave:
            backend = str(settings.phase10.full_wave_backend).upper()
            if backend == "PALACE_REMOTE":
                execution_detail = (
                    "Ki-PIDA will transfer the selected Palace project directory to "
                    f"{settings.phase10.palace_remote_host} and run Palace with "
                    f"{settings.phase10.palace_remote_mpi_processes} MPI process(es). "
                    f"The total timeout is {settings.phase10.solver_timeout_s:g} seconds. "
                )
                title = "Confirm Palace LAN execution"
                duration_detail = "Continue?"
            else:
                execution_detail = "Phase 10 will run targeted local openEMS simulations. "
                title = "Confirm openEMS execution"
                duration_detail = (
                    f"This can take up to {settings.phase10.solver_timeout_s:g} seconds per "
                    f"region for {settings.phase10.maximum_regions} region(s). Continue?"
                )
            answer = wx.MessageBox(
                execution_detail + duration_detail,
                title,
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
            )
            if answer != wx.YES:
                settings.phase10.auto_run_full_wave = False
                self.log("[PHASE 10] Full-wave execution declined; exporting regions only.")
        self.btn_run_emc.Disable()
        self.btn_cancel_emc.Enable()
        self._set_interaction_status("EMI/EMC · running")
        request = EMCRunRequest(
            settings=settings,
            pairs=tuple(pairs),
            snapshot=snapshot,
            ac_results=tuple(ac_results),
            differential_results=differential_results,
            thermal_result=thermal_result,
            debug=self.chk_debug.GetValue(),
            board_file_path=board_file_path,
            plot_lock=self._plot_lock,
        )
        callbacks = EMCControllerCallbacks(
            on_progress=self._emc_progress,
            on_complete=self._finish_emc_job,
            on_error=self._fail_emc_analysis,
            on_log=lambda message: self.log(message) if not self._closing else None,
        )
        self.emc_controller.start(request, callbacks)

    def on_cancel_emc(self, _event):
        if not self.emc_controller.is_running:
            return
        self.emc_controller.cancel()
        self.btn_cancel_emc.Disable()
        self.btn_cancel_emc.SetLabel("Cancelling EMI/EMC...")
        self.log("EMI/EMC cancellation requested; stopping the active Phase 10 worker.")

    def _finish_emc_job(self, outcome):
        if self._closing:
            return
        self._finish_emc_analysis(
            outcome.settings, outcome.result, outcome.risk_png,
            outcome.spectrum_png, outcome.field_e_png, outcome.field_h_png,
        )

    def _finish_emc_analysis(
        self, settings, result, risk_png, spectrum_png, field_e_png=None, field_h_png=None,
    ):
        self.btn_run_emc.Enable()
        self.btn_cancel_emc.Disable()
        self.btn_cancel_emc.SetLabel("Cancel EMI/EMC")
        self.emc_panel.apply_results(result)
        plots = []
        if risk_png:
            plots.append((
                "Risk Map", Plotter.bitmap_from_png(risk_png.png_bytes), None,
                risk_png.click_probe,
            ))
        if spectrum_png:
            plots.append((
                "Relative Spectrum", Plotter.bitmap_from_png(spectrum_png.png_bytes), None,
                spectrum_png.click_probe,
            ))
        if field_e_png:
            plots.append((
                "Electric Field", Plotter.bitmap_from_png(field_e_png.png_bytes),
                field_e_png.hover_probe, None,
            ))
        if field_h_png:
            plots.append((
                "Magnetic Field", Plotter.bitmap_from_png(field_h_png.png_bytes),
                field_h_png.hover_probe, None,
            ))
        self._publish_results(
            "EMC", format_emc_report(settings, result), plots,
            structured_result=adapt_emc_result(settings, result),
        )
        self.log("EMI/EMC pre-compliance results ready.")
        self._set_interaction_status("EMI/EMC · complete")

    def _fail_emc_analysis(self, exc):
        if self._closing:
            return
        self.btn_run_emc.Enable()
        self.btn_cancel_emc.Disable()
        self.btn_cancel_emc.SetLabel("Cancel EMI/EMC")
        if isinstance(exc, EMCAnalysisCancelled):
            self.log("EMI/EMC analysis cancelled.")
            self._set_interaction_status("EMI/EMC · cancelled")
            return
        self.log(f"EMI/EMC Analysis Error: {exc}")
        self._set_interaction_status("EMI/EMC · failed")
        wx.MessageBox(str(exc), "EMI/EMC Analysis Error", wx.OK | wx.ICON_ERROR)

    def _dc_copper_loss_points(self, system_results=None):
        results = system_results if system_results is not None else getattr(self, "system_results", {})
        return dc_copper_loss_points(results)

    def on_run_thermal(self, event):
        if self.thermal_controller.is_running:
            return
        if not self._ensure_analysis_slot():
            return
        self._select_workspace(self.PAGE_LOG)
        self.log("--- Starting 3D Thermal Analysis ---")
        try:
            self._start_thermal_pipeline(coupled=False)
        except Exception as exc:
            self.log(f"Thermal Analysis Error: {exc}")
            wx.MessageBox(str(exc), "Thermal Analysis Error", wx.OK | wx.ICON_ERROR)

    def on_run_coupled_thermal(self, event):
        if self.thermal_controller.is_running:
            return
        if not self._ensure_analysis_slot():
            return
        self._select_workspace(self.PAGE_LOG)
        self.log("--- Starting Coupled DC / 3D Thermal Analysis ---")
        try:
            self._start_thermal_pipeline(coupled=True)
        except Exception as exc:
            self.log(f"Coupled Thermal Analysis Error: {exc}")
            wx.MessageBox(str(exc), "Coupled Thermal Analysis Error", wx.OK | wx.ICON_ERROR)

    def _cfd_free_stream_for_thermal(self):
        """CFD free-stream speed to drive the surface coefficients, or None.

        The campaign engine grew this handover first, but nothing in this
        dialog reaches CampaignEngine -- on_build_campaign_report consolidates
        finished results rather than running the engine -- so the coupling was
        unreachable code. This is the same rule on the path the user actually
        takes: run the enclosure CFD, then run thermal.

        Forced flow only. Under buoyancy the velocity is *caused* by the
        temperature field thermal is about to compute, so handing it a speed
        resolved against a cold board would feed the answer back into its own
        question.
        """
        result = getattr(self, "cfd_result", None)
        if result is None:
            return None
        if not getattr(self, "_cfd_forced_flow", False):
            self.log(
                "Enclosure CFD was buoyancy-driven, so its velocity is not used "
                "for the thermal surface coefficients: it depends on the "
                "temperature field rather than setting it."
            )
            return None
        velocity = float(getattr(result, "board_free_stream_velocity_m_s", 0.0) or 0.0)
        cells = int(getattr(result, "board_free_stream_cells", 0) or 0)
        if velocity <= 0.0 or cells <= 0:
            self.log(
                "Enclosure CFD produced no usable free-stream sample over the "
                "board (mesh too coarse to offer cells clear of every solid); "
                "thermal keeps its configured airflow."
            )
            return None
        self.log(
            f"Thermal will use the CFD free-stream speed of {velocity:.4g} m/s "
            f"sampled over {cells} cell(s) -- estimated, not measured."
        )
        return velocity

    def _start_thermal_pipeline(self, coupled):
        """Capture wx settings, then prepare and solve entirely off the GUI thread."""
        self._refresh_live_board_state()
        if not self.thermal_panel.settings.components:
            self.thermal_panel.refresh_components(preserve_user=True)
        settings = self.thermal_panel.get_settings()
        compute_settings = self.runtime_panel.get_settings(persist=True)
        try:
            dc_grid_size = max(0.01, float(self.txt_grid_size.GetValue()))
        except ValueError:
            dc_grid_size = 0.1
        debug_mode = self.chk_debug.GetValue()
        rails = list(self.power_tree.rails)
        board_file_path = self._board_file_path()
        if coupled and not rails:
            raise ValueError("Coupled analysis requires at least one configured power rail.")
        dc_request = None
        if coupled or settings.include_dc_copper_losses:
            self.log("Capturing live PCB data for the thermal pipeline DC stage.")
            dc_request = prepare_dc_request(
                self.board, rails, dc_grid_size, compute_settings, debug_mode,
                log_callback=self.log, board_path=board_file_path,
            )

        self.log("Capturing the thermal board model on the KiCad UI thread.")
        board_model = ThermalModelBuilder(
            self.board,
            debug=debug_mode,
            log_callback=self.log,
            board_file_path=board_file_path,
        ).build(settings, rails=rails, copper_losses=[])
        request = ThermalRunRequest(
            settings=settings,
            board_model=board_model,
            rails=tuple(rails),
            compute_settings=compute_settings,
            debug=debug_mode,
            coupled=bool(coupled),
            dc_request=dc_request,
            board_signature=self._thermal_board_signature,
            cached_entries=dict(self._thermal_session_cache),
            air_velocity_m_s=self._cfd_free_stream_for_thermal(),
        )
        callbacks = ThermalControllerCallbacks(
            on_progress=self._thermal_worker_progress,
            on_complete=lambda outcome: self._finish_thermal_job(outcome, settings),
            on_error=lambda exc: self._fail_thermal_worker(coupled, exc),
            on_log=self._thermal_worker_log,
        )
        self.btn_run_thermal.Disable()
        self.btn_run_coupled.Disable()
        self._set_interaction_status("Coupled thermal · running" if coupled else "Thermal · running")
        self.thermal_controller.start(request, callbacks)

    def _thermal_worker_log(self, message):
        if not self._closing:
            self.log(message)

    def _thermal_worker_progress(self, completed, total, detail):
        if self._closing:
            return
        self.log(f"Thermal progress: {completed}/{total} ({detail})")
        self._set_interaction_status(f"Thermal · {completed}/{total} · {detail}")

    def _finish_thermal_job(self, outcome, settings):
        if self._closing:
            return
        self._thermal_session_cache = {outcome.cache_key: outcome.cache_value}
        self._finish_thermal_worker(
            outcome.mesh,
            outcome.result,
            bool(outcome.coupled_result is not None),
            outcome.coupled_result,
            outcome.system_results,
            outcome.elapsed_seconds,
            settings.color_map,
            settings.resolved_color_scale_minimum_c(),
            settings.color_scale_minimum_mode,
            settings.resolved_color_scale_maximum_c(),
            settings.color_scale_maximum_mode,
            settings.show_internal_copper_layers,
        )

    def _finish_thermal_worker(
        self, mesh, result, coupled, coupled_result, system_results, elapsed_seconds,
        color_map, color_scale_minimum_c, color_scale_minimum_mode,
        color_scale_maximum_c, color_scale_maximum_mode,
        show_internal_copper_layers,
    ):
        self.btn_run_thermal.Enable()
        self.btn_run_coupled.Enable()
        if system_results:
            self.system_results = system_results
        self.thermal_mesh = mesh
        self.thermal_result = result
        if coupled_result is not None:
            self.electrothermal_result = coupled_result
        self._update_thermal_results_ui(
            mesh, result, coupled=coupled, elapsed_seconds=elapsed_seconds, color_map=color_map,
            color_scale_minimum_c=color_scale_minimum_c,
            color_scale_minimum_mode=color_scale_minimum_mode,
            color_scale_maximum_c=color_scale_maximum_c,
            color_scale_maximum_mode=color_scale_maximum_mode,
            show_internal_copper_layers=show_internal_copper_layers,
        )
        self.log(f"Thermal analysis completed in {elapsed_seconds:.3f} s.")
        self._set_interaction_status("Thermal analysis · complete")

    def _fail_thermal_worker(self, coupled, exc):
        self.btn_run_thermal.Enable()
        self.btn_run_coupled.Enable()
        label = "Coupled Thermal" if coupled else "Thermal"
        if isinstance(exc, ThermalAnalysisCancelled):
            self.log(f"{label} analysis cancelled.")
            self._set_interaction_status(f"{label} · cancelled")
            return
        self.log(f"{label} Analysis Error: {exc}")
        self._set_interaction_status(f"{label} · failed")
        wx.MessageBox(str(exc), f"{label} Analysis Error", wx.OK | wx.ICON_ERROR)

    def _update_thermal_results_ui(
        self, mesh, result, coupled=False, elapsed_seconds=None, color_map="inferno",
        color_scale_minimum_c=None, color_scale_minimum_mode="AUTO",
        color_scale_maximum_c=None, color_scale_maximum_mode="AUTO",
        show_internal_copper_layers=True,
    ):
        report = format_thermal_report(
            mesh, result, coupled=coupled,
            coupled_result=getattr(self, "electrothermal_result", None),
            elapsed_seconds=elapsed_seconds, color_map=color_map,
            color_scale_minimum_c=color_scale_minimum_c,
            color_scale_minimum_mode=color_scale_minimum_mode,
            color_scale_maximum_c=color_scale_maximum_c,
            color_scale_maximum_mode=color_scale_maximum_mode,
            show_internal_copper_layers=show_internal_copper_layers,
            power_stage_reports=list(
                getattr(self.thermal_panel.settings, "power_stage_reports", []) or []
            ),
        )
        self._thermal_result_generation += 1
        generation = self._thermal_result_generation
        page = self._publish_results(
            "THERMAL", report, [],
            structured_result=adapt_thermal_result(result, coupled, elapsed_seconds or 0.0),
        )
        page.show_rendering("Rendering thermal plots in background...")
        self.log("Thermal solve complete; rendering plots in background.")

        self._thermal_plot_thread = threading.Thread(
            target=self._render_thermal_plots_worker,
            args=(
                mesh, result, generation, color_map, color_scale_minimum_c,
                color_scale_maximum_c,
                show_internal_copper_layers,
            ),
            name="KiPIDA-Thermal-Plots",
            daemon=True,
        )
        self._thermal_plot_thread.start()

    def _render_thermal_plots_worker(
        self, mesh, result, generation, color_map, color_scale_minimum_c,
        color_scale_maximum_c,
        show_internal_copper_layers,
    ):
        try:
            with self._plot_lock:
                plots = render_thermal_plots(
                    mesh, result, color_map=color_map,
                    color_scale_minimum_c=color_scale_minimum_c,
                    color_scale_maximum_c=color_scale_maximum_c,
                    show_internal_copper_layers=show_internal_copper_layers,
                )
            if not self._closing:
                wx.CallAfter(self._finish_thermal_plots, generation, plots)
        except Exception as exc:
            if not self._closing:
                wx.CallAfter(self._fail_thermal_plots, generation, exc)

    def _finish_thermal_plots(self, generation, plots):
        self._thermal_plot_thread = None
        if self._closing or generation != self._thermal_result_generation:
            return
        available_plots = [(title, data) for title, data in plots if data]
        if not available_plots:
            self._fail_thermal_plots(
                generation,
                RuntimeError("Matplotlib did not produce any thermal plot."),
            )
            return
        page = self.results_workspace.page_for("THERMAL")
        bitmaps = []
        history_bitmaps = []
        for title, rendered in available_plots:
            png_bytes = getattr(rendered, "png_bytes", rendered)
            hover_probe = getattr(rendered, "hover_probe", None)
            bitmap = Plotter.bitmap_from_png(png_bytes)
            bitmaps.append((title, bitmap, hover_probe))
            history_bitmaps.append((title, bitmap))
        page.set_plots(bitmaps)
        self.results_workspace.update_history_plots("THERMAL", history_bitmaps)
        self.log("Thermal result plots ready.")

    def _fail_thermal_plots(self, generation, exc):
        self._thermal_plot_thread = None
        if self._closing or generation != self._thermal_result_generation:
            return
        page = self.results_workspace.page_for("THERMAL")
        current_page = page.plots.GetCurrentPage()
        if current_page:
            labels = [
                child for child in current_page.GetChildren()
                if isinstance(child, wx.StaticText)
            ]
            if labels:
                labels[0].SetLabel("Thermal plot rendering failed; see Log for details.")
                current_page.Layout()
        self.log(f"Thermal plot rendering error: {exc}")

    def _prepare_cfd_analysis(self):
        """Extract all KiCad-dependent data before starting the worker thread."""
        self._refresh_live_board_state()
        settings = self.cfd_panel.get_settings()
        # Captured now, not when the result arrives: the user is free to edit
        # the patch list while the solve runs, and the coupling rule must
        # describe the run that actually happened.
        self._cfd_forced_flow = any(
            str(getattr(patch, "kind", "")).upper() in {"INLET", "FAN"}
            and float(getattr(patch, "velocity_m_s", 0.0) or 0.0) > 0.0
            for patch in (settings.patches or ())
        )
        if not self.thermal_panel.settings.components:
            self.thermal_panel.refresh_components(preserve_user=True)
        thermal_settings = self.thermal_panel.get_settings()
        dc_request = None
        if settings.include_dc_copper_losses:
            if not self.power_tree.rails:
                raise ValueError(
                    "DC copper-loss heat sources require at least one configured power rail."
                )
            try:
                grid_size = max(0.01, float(self.txt_grid_size.GetValue()))
            except ValueError:
                grid_size = 0.1
            dc_request = prepare_dc_request(
                self.board, self.power_tree.rails, grid_size,
                self.runtime_panel.get_settings(persist=True),
                self.chk_debug.GetValue(), log_callback=self.log,
                board_path=self._board_file_path(),
            )
        builder = ThermalModelBuilder(
            self.board,
            debug=self.chk_debug.GetValue(),
            log_callback=self.log,
            board_file_path=self._board_file_path(),
        )
        board_model = builder.build(
            thermal_settings,
            rails=self.power_tree.rails,
            copper_losses=[],
        )
        return settings, board_model, dc_request

    def _cfd_worker_log(self, message):
        if not self._closing:
            self.log(message)

    def _cfd_worker_progress(self, completed, total, detail):
        if self._closing:
            return
        self.log(f"CFD progress: {completed}/{total} ({detail})")
        self._set_interaction_status(f"Enclosure CFD · {completed}/{total} · {detail}")

    def on_run_cfd(self, event):
        if self.cfd_controller.is_running:
            self.cfd_controller.cancel()
            self.btn_run_cfd.Disable()
            self.btn_run_cfd.SetLabel("Cancelling CFD...")
            self.log("Cancellation requested for enclosure CFD.")
            return

        if not self._ensure_analysis_slot():
            return

        self._select_workspace(self.PAGE_LOG)
        self.log("--- Starting Phase 4 Enclosure CFD Analysis ---")
        try:
            settings, board_model, dc_request = self._prepare_cfd_analysis()
        except Exception as exc:
            self.log(f"Enclosure CFD setup error: {exc}")
            wx.MessageBox(str(exc), "Enclosure CFD Setup Error", wx.OK | wx.ICON_ERROR)
            return

        self.btn_run_cfd.SetLabel("Cancel Enclosure CFD")
        debug_mode = self.chk_debug.GetValue()
        compute_settings = self.runtime_panel.get_settings(persist=True)
        request = CFDRunRequest(
            board_model=board_model,
            settings=settings,
            compute_settings=compute_settings,
            debug=debug_mode,
            dc_request=dc_request,
            plot_lock=self._plot_lock,
        )
        callbacks = CFDControllerCallbacks(
            on_progress=self._cfd_worker_progress,
            on_complete=self._finish_cfd_job,
            on_error=self._fail_cfd_analysis,
            on_log=self._cfd_worker_log,
        )
        self._set_interaction_status("Enclosure CFD · running")
        self.cfd_controller.start(request, callbacks)

    def _finish_cfd_job(self, outcome):
        if self._closing:
            return
        if outcome.system_results:
            self.system_results = outcome.system_results
        self._finish_cfd_analysis(outcome.mesh, outcome.result, outcome.plots)

    def _finish_cfd_analysis(self, mesh, result, rendered_plots=None):
        self.btn_run_cfd.Enable()
        self.btn_run_cfd.SetLabel("Run Enclosure CFD")
        self.cfd_mesh = mesh
        self.cfd_result = result
        self.log(
            f"Enclosure CFD complete: {result.iterations} iterations, "
            f"Vmax={result.maximum_velocity_m_s:.4g} m/s."
        )
        self._set_interaction_status("Enclosure CFD · complete")
        self._update_cfd_results_ui(mesh, result, rendered_plots)

    def _fail_cfd_analysis(self, exc):
        if self._closing:
            return
        self.btn_run_cfd.Enable()
        self.btn_run_cfd.SetLabel("Run Enclosure CFD")
        message = str(exc)
        if isinstance(exc, CFDAnalysisCancelled):
            self.log("Enclosure CFD analysis cancelled.")
            self._set_interaction_status("Enclosure CFD · cancelled")
            return
        self.log(f"Enclosure CFD error: {message}")
        self._set_interaction_status("Enclosure CFD · failed")
        if "cancelled" not in message.lower():
            wx.MessageBox(message, "Enclosure CFD Error", wx.OK | wx.ICON_ERROR)

    def _update_cfd_results_ui(self, mesh, result, rendered_plots=None):
        self._result_generation += 1
        if rendered_plots is None:
            plotter = Plotter(debug=self.chk_debug.GetValue())
            plots = [
                ("CFD 3D", plotter.plot_cfd_3d(mesh, result)),
                ("Temperature XY", plotter.plot_cfd_slice(mesh, result, "TEMPERATURE", "XY")),
                ("Temperature XZ", plotter.plot_cfd_slice(mesh, result, "TEMPERATURE", "XZ")),
                ("Velocity XY", plotter.plot_cfd_slice(mesh, result, "VELOCITY", "XY")),
                ("Pressure XY", plotter.plot_cfd_slice(mesh, result, "PRESSURE", "XY")),
                ("Residuals", plotter.plot_cfd_residuals(result)),
            ]
        else:
            plots = [
                (title, Plotter.bitmap_from_png(png_bytes))
                for title, png_bytes in rendered_plots if png_bytes
            ]
        self._publish_results(
            "CFD", format_cfd_report(mesh, result),
            [(title, bitmap) for title, bitmap in plots if bitmap],
            structured_result=adapt_cfd_result(mesh, result),
        )

    def _debug_plot_geo(self, extractor, geo):
        try:
            buf = extractor.plot_geometry(geo)
            if buf:
                img = wx.Image(buf, wx.BITMAP_TYPE_PNG)
                self._publish_results("DEBUG", "Geometry debug plot", [("Geometry Debug", wx.Bitmap(img))])
        except: pass

    def _debug_plot_mesh(self, mesher, mesh, stackup):
        try:
            plotter = Plotter(debug=self.chk_debug.GetValue())
            bmp = plotter.plot_3d_mesh(mesh, stackup)
            if bmp:
                self._publish_results("DEBUG", "Mesh debug plot", [("Mesh Debug", bmp)])
        except: pass

    def _update_results_ui(self):
        self._result_generation += 1
        try:
            drop_pct_ui = min(100.0, max(0.0, float(self.txt_drop_pct.GetValue())))
        except (TypeError, ValueError):
            drop_pct_ui = 5.0
        dc_result = adapt_dc_result(self.system_results, drop_pct_ui)
        # Give the voltage-drop findings a sized fix. Without this the report
        # says "No structured remediation was computed" on every action, which
        # is what it did until now: the advisor existed and nothing called it.
        try:
            attached = attach_dc_remediations(
                dc_result, self._board_file_path(), self.power_tree.rails,
                drop_pct_ui, log_callback=self.log,
            )
            if attached:
                self.log(f"Sized a copper fix for {attached} voltage-drop finding(s).")
        except Exception as exc:
            self.log(f"Remediation sizing skipped: {exc}")
        page = self._publish_results(
            "DC", format_dc_report(self.system_results), [],
            structured_result=dc_result,
        )
        if not self.system_results:
            return
        try:
            extractor = GeometryExtractor(self.board)
            stackup = extractor.get_board_stackup()
            board_bounds = extractor.get_board_bounds(board_file_path=self._board_file_path())
        except Exception as exc:
            self.log(f"DC plot geometry warning: {exc}")
            stackup = None
            board_bounds = None
        generation = self._result_generation
        page.show_rendering("Rendering DC rail maps in background...")
        self._dc_plot_thread = threading.Thread(
            target=self._render_dc_plots_worker,
            args=(
                dict(self.system_results), stackup, board_bounds, drop_pct_ui,
                self.chk_debug.GetValue(), generation,
            ),
            name="KiPIDA-DC-Plots", daemon=True,
        )
        self._dc_plot_thread.start()

    def _render_dc_plots_worker(
        self, system_results, stackup, board_bounds, drop_pct, debug, generation,
    ):
        try:
            with self._plot_lock:
                groups = render_dc_plots(
                    system_results, stackup=stackup, board_bounds=board_bounds,
                    drop_pct=drop_pct, debug=debug,
                )
            if not self._closing:
                wx.CallAfter(self._finish_dc_plots, generation, groups)
        except Exception as exc:
            if not self._closing:
                wx.CallAfter(self._fail_dc_plots, generation, exc)

    def _finish_dc_plots(self, generation, groups):
        self._dc_plot_thread = None
        if self._closing or generation != self._result_generation:
            return
        page = self.results_workspace.page_for("DC")
        bitmaps = [
            (title, Plotter.bitmap_from_png(png_bytes))
            for title, png_bytes in flatten_dc_plot_groups(groups)
        ]
        page.set_plots(bitmaps)
        self.results_workspace.update_history_plots("DC", bitmaps)
        self._select_workspace(self.PAGE_RESULTS)
        self.log("DC rail maps ready.")

    def _fail_dc_plots(self, generation, exc):
        self._dc_plot_thread = None
        if self._closing or generation != self._result_generation:
            return
        self.log(f"DC plot rendering error: {exc}")

    def on_close(self, event):
        if self.dc_controller.is_running:
            self.dc_controller.cancel()
            self._set_interaction_status("DC analysis · cancelling")
            self.log("Close requested; cancelling DC analysis first.")
            return
        if self.ac_controller.is_running:
            self.ac_controller.cancel()
            self._set_interaction_status("AC analysis · cancelling")
            self.log("Close requested; cancelling AC analysis first.")
            return
        if self.differential_controller.is_running:
            self.differential_controller.cancel()
            self._set_interaction_status("Differential analysis · cancelling")
            self.log("Close requested; cancelling differential analysis first.")
            return
        if self.thermal_controller.is_running:
            self.thermal_controller.cancel()
            self._set_interaction_status("Thermal analysis · cancelling")
            self.log("Close requested; cancelling thermal analysis first.")
            return
        if self.emc_controller.is_running:
            self.emc_controller.cancel()
            self.btn_cancel_emc.Disable()
            self.btn_cancel_emc.SetLabel("Cancelling EMI/EMC...")
            self.log("Close requested; cancelling EMI/EMC first.")
            return
        if self.cfd_controller.is_running:
            self.cfd_controller.cancel()
            self.btn_run_cfd.Disable()
            self.btn_run_cfd.SetLabel("Cancelling CFD...")
            self.log("Close requested; cancelling enclosure CFD first.")
            return
        self._closing = True
        self._result_generation += 1
        self._thermal_result_generation += 1
        self._stream_capture.restore()
        self.EndModal(wx.ID_CANCEL)
