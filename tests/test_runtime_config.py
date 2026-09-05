import json
from pathlib import Path
import tempfile
import unittest

from runtime_config import (
    RUNTIME_CONFIG_VERSION, RuntimeComputeSettings, load_runtime_settings,
    save_runtime_settings,
)
from runtime_environment import (
    plugin_version, recommended_cupy_package, source_fingerprint,
)
from unittest.mock import Mock, patch


class RuntimeConfigTests(unittest.TestCase):
    def test_round_trip_machine_local_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            settings = RuntimeComputeSettings(
                ui_language="fr",
                backend="CUDA", cpu_multithread=False, cpu_threads=3,
                cuda_enabled=True, cuda_device=1, cuda_min_nodes=42000,
                memory_limit_gib=48.0,
            )
            save_runtime_settings(settings, path)
            loaded = load_runtime_settings(path)
            self.assertEqual(loaded.backend, "CUDA")
            self.assertEqual(loaded.ui_language, "fr")
            self.assertFalse(loaded.cpu_multithread)
            self.assertEqual(loaded.cpu_threads, 3)
            self.assertTrue(loaded.cuda_enabled)
            self.assertEqual(loaded.cuda_device, 1)
            self.assertEqual(loaded.cuda_min_nodes, 42000)
            self.assertEqual(loaded.memory_limit_gib, 48.0)
            self.assertEqual(json.loads(path.read_text())["version"], RUNTIME_CONFIG_VERSION)

    def test_invalid_config_uses_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(load_runtime_settings(path).backend, "AUTO")

    def test_plugin_version_is_read_from_manifest(self):
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(plugin_version(root), "0.19.0")

    def test_the_build_fingerprint_follows_the_sources(self):
        """The release number cannot tell two builds apart between releases.

        plugin.json was last bumped at the 0.19.0 release, so every change
        since reports the same string -- which is the one question that matters
        when checking whether the copy in the plugin folder is the one just
        built. Establishing that from behaviour instead cost cross-checking a
        solver iteration count, a residual curve and one log line.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "solver.py").write_text("x = 1\n", encoding="utf-8")
            before = source_fingerprint(root)

            (root / "solver.py").write_text("x = 2\n", encoding="utf-8")
            after = source_fingerprint(root)

            self.assertTrue(before)
            self.assertNotEqual(before, after)

    def test_the_fingerprint_ignores_tests_and_vendored_runtimes(self):
        # A deployed copy carries no tests and may carry a bundled venv; those
        # must not change the identity of the code being run.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "solver.py").write_text("x = 1\n", encoding="utf-8")
            baseline = source_fingerprint(root)

            (root / "tests").mkdir()
            (root / "tests" / "test_thing.py").write_text("y = 2\n", encoding="utf-8")
            (root / ".runtime").mkdir()
            (root / ".runtime" / "vendored.py").write_text("z = 3\n", encoding="utf-8")

            self.assertEqual(source_fingerprint(root), baseline)

    @patch("runtime_environment.subprocess.run")
    def test_cuda_wheel_family_follows_driver_runtime(self, run):
        run.return_value = Mock(stdout="NVIDIA-SMI 595.79   CUDA Version: 13.1")
        self.assertEqual(recommended_cupy_package(), "cupy-cuda13x[ctk]")


if __name__ == "__main__":
    unittest.main()
