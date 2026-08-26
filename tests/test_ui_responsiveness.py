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

    def test_coupled_analysis_uses_background_pipeline(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertIn("_start_thermal_pipeline", method_calls(tree, "on_run_coupled_thermal"))
        self.assertIn("start", method_calls(tree, "_start_thermal_pipeline"))
        self.assertIn("_solve_system", method_calls(tree, "_thermal_pipeline_worker"))

    def test_worker_logs_are_marshaled_to_wx_thread(self):
        source = (PLUGIN_ROOT / "ui" / "main_dialog.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = method_calls(tree, "log")
        self.assertIn("IsMainThread", calls)
        self.assertIn("CallAfter", calls)


if __name__ == "__main__":
    unittest.main()
