import ast
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def method_calls(tree, method_name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name:
            return {
                child.func.attr
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
            }
    raise AssertionError(f"Method {method_name!r} was not found")


class UICompatibilityTests(unittest.TestCase):
    def test_font_zoom_does_not_require_font_copy(self):
        source = (PLUGIN_ROOT / "ui" / "interactive_views.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertNotIn(".Copy(", source)
        self.assertIn("_copy_font", {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)})

    def test_emc_popup_supports_toggle_and_clipboard_copy(self):
        source = (PLUGIN_ROOT / "ui" / "interactive_views.py").read_text(encoding="utf-8")
        self.assertIn("class ProbePopup", source)
        self.assertIn("EVT_LEFT_DCLICK", source)
        self.assertIn("TheClipboard", source)
        self.assertIn("same_observation", source)

    def test_coupled_analysis_uses_background_pipeline(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertIn("_start_thermal_pipeline", method_calls(tree, "on_run_coupled_thermal"))
        self.assertIn("start", method_calls(tree, "_start_thermal_pipeline"))
        self.assertIn("prepare_dc_request", source)
        self.assertIn("ThermalAnalysisController", source)
        self.assertNotIn("_thermal_pipeline_worker", source)

    def test_thermal_worker_does_not_reference_live_board(self):
        source = (PLUGIN_ROOT / "application" / "thermal_controller.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("self.board", source)
        self.assertNotIn("GeometryExtractor", source)

    def test_thermal_plot_generation_is_outside_wx_dialog(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        presenter = (PLUGIN_ROOT / "application" / "thermal_plot_presenter.py").read_text(
            encoding="utf-8",
        )
        self.assertIn("render_thermal_plots(", source)
        self.assertIn("def internal_copper_slices", presenter)
        self.assertNotIn("plotter.plot_thermal_3d", source)

    def test_differential_analysis_uses_controller_after_ui_capture(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        self.assertIn("DifferentialAnalysisController", source)
        self.assertIn("DifferentialGeometrySnapshot.capture", source)
        self.assertNotIn("_run_differential_worker", source)

    def test_emc_analysis_uses_controller_after_ui_capture(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        self.assertIn("EMCAnalysisController", source)
        self.assertIn("EMCGeometrySnapshot.capture", source)
        self.assertNotIn("_run_emc_worker", source)

    def test_cfd_analysis_uses_controller_after_ui_model_capture(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        self.assertIn("CFDAnalysisController", source)
        self.assertIn("ThermalModelBuilder", source)
        self.assertNotIn("_run_cfd_worker", source)

    def test_simple_reports_are_presented_outside_wx_dialog(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        self.assertIn("format_ac_report(result, optimization, settings)", source)
        self.assertIn("adapt_ac_result(result, optimization, settings)", source)
        self.assertIn("format_dc_report(self.system_results)", source)
        self.assertIn("format_cfd_report(mesh, result)", source)
        self.assertIn("format_thermal_report(", source)
        self.assertIn(
            "format_differential_report(results, stackup, target_tolerance_pct)", source,
        )
        self.assertIn("format_emc_report(settings, result)", source)
        self.assertNotIn("def _format_emc_report", source)

    def test_dc_live_board_capture_precedes_background_controller(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = method_calls(tree, "on_run")
        self.assertIn("prepare_dc_request", source)
        self.assertIn("start", calls)
        self.assertNotIn("_solve_system", calls)
        self.assertNotIn("def _solve_system", source)
        self.assertNotIn("from mesh import Mesher", source)
        self.assertNotIn("from solver import Solver", source)

    def test_dc_plot_rendering_uses_png_worker(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        presenter = (PLUGIN_ROOT / "application" / "dc_plot_presenter.py").read_text(
            encoding="utf-8",
        )
        self.assertIn('name="KiPIDA-DC-Plots"', source)
        self.assertIn("render_dc_plots(", source)
        self.assertIn("as_png=True", presenter)
        self.assertNotIn("plotter.plot_layer_2d", source)
        self.assertIn('update_history_plots("DC", bitmaps)', source)
        self.assertNotIn("wx.Notebook(results_notebook)", source)

    def test_worker_logs_are_marshaled_to_wx_thread(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = method_calls(tree, "log")
        self.assertIn("IsMainThread", calls)
        self.assertIn("CallAfter", calls)

    def test_dialog_restores_process_streams_on_close(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        self.assertIn("DialogStreamCapture(self.log)", source)
        self.assertIn("self._stream_capture.restore()", source)
        self.assertNotIn("class LogRedirector", source)

    def test_live_board_refresh_propagates_connection_failure(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        method = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_refresh_live_board_state"
        )
        self.assertTrue(any(isinstance(node, ast.Raise) for node in ast.walk(method)))
        self.assertIn("No live KiCad PCB is connected", source)

    def test_action_bar_is_contextual_to_active_workspace(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        action_source = (PLUGIN_ROOT / "ui" / "dialog_action_bar.py").read_text(
            encoding="utf-8",
        )
        tree = ast.parse(source)
        methods = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        self.assertIn("_update_contextual_actions", methods)
        self.assertIn("DialogActionBar(self, handlers, actions_by_page)", source)
        self.assertIn("button.Show(button in visible)", action_source)
        self.assertIn("def set_active_page", action_source)
        self.assertNotIn("self.btn_run = wx.Button", source)

    def test_results_workspace_renders_structured_findings(self):
        source = (PLUGIN_ROOT / "ui" / "results_workspace.py").read_text(encoding="utf-8")
        presenter = (PLUGIN_ROOT / "application" / "result_detail_presenter.py").read_text(
            encoding="utf-8",
        )
        self.assertIn("self.findings = wx.ListCtrl", source)
        self.assertIn("finding.rule_id", source)
        self.assertIn("finding.recommendation", presenter)
        self.assertNotIn("_LegacyProjectResultsHistory", source)
        self.assertIn("ProjectResultsHistory(history_directory)", source)
        self.assertIn("self.notebook = wx.Simplebook(self)", source)
        self.assertIn("self.analysis_choice = wx.Choice", source)
        self.assertIn("def _sync_history_to_analysis", source)
        self.assertNotIn("EVT_NOTEBOOK_PAGE_CHANGED", source)
        self.assertIn("_displayed_entries", source)
        self.assertIn('"Actionable (Critical–Medium)"', source)
        self.assertIn("self.finding_search", source)
        self.assertIn('for label in ("Report", "Selected finding", "Evidence and limits")', source)
        self.assertIn("self.detail_view = wx.Choice", source)
        self.assertIn("def _show_selected_detail", source)
        self.assertIn("self.text._kipida_text_zoom = self.text_zoom", source)
        self.assertNotIn("self.details_notebook", source)
        self.assertIn("format_finding_detail", source)

    def test_plot_auto_fit_tracks_final_viewport_until_manual_zoom(self):
        source = (PLUGIN_ROOT / "ui" / "interactive_views.py").read_text(encoding="utf-8")
        self.assertIn("self._auto_fit_width = True", source)
        self.assertIn("wx.CallAfter(self._fit_to_width_if_alive)", source)
        self.assertIn("self._is_destroyed = True", source)
        self.assertIn("self._auto_fit_width = False", source)
        self.assertIn('label="Fit page"', source)
        self.assertIn('label="Fit width"', source)
        self.assertIn('label="100%"', source)
        self.assertIn('self._fit_mode == "PAGE"', source)
        self.assertIn("wx.ALIGN_CENTER_HORIZONTAL", source)

    def test_result_plots_use_safe_selector_instead_of_nested_notebook(self):
        source = (PLUGIN_ROOT / "ui" / "results_workspace.py").read_text(encoding="utf-8")
        self.assertIn("self.plot_view = wx.Choice", source)
        self.assertIn("def _show_selected_plot", source)
        self.assertIn("view.Show(index == selection)", source)
        self.assertIn("_fit_to_width_if_alive", source)
        self.assertNotIn("self.plots = wx.Notebook", source)

    def test_emc_page_scrolls_and_reflows_phase10_without_overlap(self):
        source = (PLUGIN_ROOT / "ui" / "emc_analysis_panel.py").read_text(encoding="utf-8")
        self.assertIn("class EMCAnalysisPanel(wx.ScrolledWindow)", source)
        self.assertIn("EVT_COLLAPSIBLEPANE_CHANGED", source)
        self.assertIn("self.FitInside()", source)
        self.assertIn("self.source_list.SetMinSize((-1, 190))", source)

    def test_top_level_navigation_uses_grouped_sidebar_and_simplebook(self):
        main_source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        nav_source = (PLUGIN_ROOT / "ui" / "workspace_navigation.py").read_text(encoding="utf-8")
        pages_source = (PLUGIN_ROOT / "ui" / "dialog_pages.py").read_text(encoding="utf-8")
        self.assertIn("wx.Simplebook(content_panel)", main_source)
        self.assertIn("DialogPages(", main_source)
        self.assertIn("class DialogPages", pages_source)
        self.assertIn('notebook.AddPage(self.tab_results, "Results")', pages_source)
        self.assertIn('notebook.AddPage(self.tab_log, "Log")', pages_source)
        self.assertIn("WorkspaceNavigator", main_source)
        self.assertIn("build_workspace_entries", main_source)
        self.assertIn('WorkspaceEntry("Power Integrity"', nav_source)
        self.assertIn('WorkspaceEntry("Signal Integrity"', nav_source)
        self.assertIn('WorkspaceEntry("Results"', nav_source)
        self.assertNotIn("wx.EVT_NOTEBOOK_PAGE_CHANGED", main_source)
        self.assertIn("wx.TreeCtrl", nav_source)

    def test_programmatic_navigation_uses_one_synchronization_path(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        direct_calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "SetSelection" and isinstance(node.func.value, ast.Attribute):
                if node.func.value.attr == "notebook":
                    direct_calls.append(node.lineno)
        select_method = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_select_workspace"
        )
        self.assertEqual(len(direct_calls), 1)
        self.assertGreaterEqual(direct_calls[0], select_method.lineno)
        self.assertLessEqual(direct_calls[0], select_method.end_lineno)
        self.assertIn("self._sync_workspace_chrome(page_index)", source)
        self.assertIn("def _sync_workspace_chrome", source)

    def test_heavy_analyses_share_one_dialog_level_execution_slot(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        methods = {
            node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        self.assertIn("_running_analysis_label", methods)
        self.assertIn("_ensure_analysis_slot", methods)
        guarded = (
            "on_run", "_start_ac_job", "on_run_differential", "on_run_emc",
            "on_run_thermal", "on_run_coupled_thermal", "on_run_cfd",
        )
        for name in guarded:
            self.assertIn("_ensure_analysis_slot", method_calls(tree, name), name)

    def test_ac_mesh_is_independent_from_dc_mesh_and_cost_guarded(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        panel_source = (PLUGIN_ROOT / "ui" / "ac_analysis_panel.py").read_text(
            encoding="utf-8",
        )
        self.assertIn("settings.mesh_resolution_mm", source)
        self.assertIn("AC_MAX_NETWORK_NODES", source)
        self.assertIn("AC mesh contains", source)
        self.assertIn("AC mesh resolution (mm):", panel_source)

    def test_main_window_can_be_maximized(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        self.assertIn("wx.MAXIMIZE_BOX", source)
        self.assertIn("wx.MINIMIZE_BOX", source)

    def test_ac_ui_has_presets_cost_hint_cancellation_and_smart_auto_backend(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        panel_source = (PLUGIN_ROOT / "ui" / "ac_analysis_panel.py").read_text(
            encoding="utf-8",
        )
        self.assertIn("ANALYSIS_PRESETS", panel_source)
        self.assertIn("Balanced (recommended)", panel_source)
        self.assertIn("Estimated relative solve load", panel_source)
        self.assertIn("Frequency points must be between 11 and 401", panel_source)
        self.assertIn("Cancel AC Analysis", source)
        # AUTO used to be rewritten to CPU here before the solver was ever
        # consulted, which put the GPU out of reach for AC whatever the user
        # selected. It now stays AUTO, guarded by the solver's first-point
        # accuracy audit, so the rewrite must be gone rather than merely
        # bypassed.
        self.assertNotIn('replace(compute_settings, backend="CPU")', source)
        self.assertIn("verified", source)
        # The message must report the decision the backend will actually make.
        # It once announced a CUDA attempt unconditionally, while AUTO silently
        # stayed on CPU because the network sat below cuda_min_nodes -- a log
        # asserting something the code was not doing.
        self.assertIn("cuda_min_nodes", source)
        self.assertIn("below the", source)

    def test_result_plots_fit_the_available_width_on_first_layout(self):
        source = (PLUGIN_ROOT / "ui" / "interactive_views.py").read_text(
            encoding="utf-8",
        )
        self.assertIn("self._auto_fit_width = True", source)
        self.assertIn("wx.CallAfter(self._fit_to_width_if_alive)", source)
        self.assertIn("available / max(1, self._bitmap.GetWidth())", source)
        self.assertIn("self.Bind(wx.EVT_SIZE, self._on_size)", source)

    def test_ac_solver_is_owned_by_background_controller(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        start_calls = method_calls(tree, "_start_ac_job")
        self.assertIn("start", start_calls)
        self.assertNotIn("solve_sweep", method_calls(tree, "on_run_ac"))
        self.assertNotIn("optimize", method_calls(tree, "on_optimize_decoupling"))
        ac_progress = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_ac_progress"
        )
        self.assertFalse(any(
            isinstance(node, ast.Attribute) and node.attr == "SafeYield"
            for node in ast.walk(ac_progress)
        ))


if __name__ == "__main__":
    unittest.main()
