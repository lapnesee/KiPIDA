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

    def test_thermal_worker_does_not_reference_live_board(self):
        source = (PLUGIN_ROOT / "application" / "thermal_controller.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("self.board", source)
        self.assertNotIn("GeometryExtractor", source)

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

    def test_worker_logs_are_marshaled_to_wx_thread(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = method_calls(tree, "log")
        self.assertIn("IsMainThread", calls)
        self.assertIn("CallAfter", calls)

    def test_live_board_refresh_propagates_connection_failure(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        method = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_refresh_live_board_state"
        )
        self.assertTrue(any(isinstance(node, ast.Raise) for node in ast.walk(method)))
        self.assertIn("No live KiCad PCB is connected", source)

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
        # selected. The solver now makes that choice and forcing CUDA still
        # reaches the GPU, guarded by its first-point accuracy audit, so the
        # rewrite must stay gone.
        self.assertNotIn('replace(compute_settings, backend="CPU")', source)
        self.assertIn("verified", source)
        # The message must report the decision the solver will actually make.
        # It once announced a CUDA attempt for any network above a node
        # threshold; AUTO now sweeps on the recycled CPU factorisation.
        self.assertIn("reused across neighbouring frequencies", source)
        self.assertNotIn("cuda_min_nodes_sweep", source)

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
