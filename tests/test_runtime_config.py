import json
from pathlib import Path
import tempfile
import unittest

from runtime_config import (
    RuntimeComputeSettings, load_runtime_settings, save_runtime_settings,
)
from runtime_environment import plugin_version, recommended_cupy_package
from unittest.mock import Mock, patch


class RuntimeConfigTests(unittest.TestCase):
    def test_round_trip_machine_local_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            settings = RuntimeComputeSettings(
                backend="CUDA", cpu_multithread=False, cpu_threads=3,
                cuda_enabled=True, cuda_device=1, cuda_min_nodes=42000,
            )
            save_runtime_settings(settings, path)
            loaded = load_runtime_settings(path)
            self.assertEqual(loaded.backend, "CUDA")
            self.assertFalse(loaded.cpu_multithread)
            self.assertEqual(loaded.cpu_threads, 3)
            self.assertTrue(loaded.cuda_enabled)
            self.assertEqual(loaded.cuda_device, 1)
            self.assertEqual(loaded.cuda_min_nodes, 42000)
            self.assertEqual(json.loads(path.read_text())["version"], "1.0")

    def test_invalid_config_uses_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(load_runtime_settings(path).backend, "AUTO")

    def test_plugin_version_is_read_from_manifest(self):
        root = Path(__file__).resolve().parent.parent
        self.assertEqual(plugin_version(root), "0.11.1")

    @patch("runtime_environment.subprocess.run")
    def test_cuda_wheel_family_follows_driver_runtime(self, run):
        run.return_value = Mock(stdout="NVIDIA-SMI 595.79   CUDA Version: 13.1")
        self.assertEqual(recommended_cupy_package(), "cupy-cuda13x[ctk]")


if __name__ == "__main__":
    unittest.main()
