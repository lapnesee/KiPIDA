"""Machine-local CPU/CUDA configuration panel."""

from dataclasses import replace
import os
from pathlib import Path
import threading
import time

import numpy as np
import scipy.sparse
import wx

try:
    from compute_backend import SparseComputeBackend, cuda_diagnostics
    from runtime_config import load_runtime_settings, save_runtime_settings
    from runtime_environment import install_cuda_environment, plugin_version, runtime_summary
except (ImportError, ValueError):
    from ..compute_backend import SparseComputeBackend, cuda_diagnostics
    from ..runtime_config import load_runtime_settings, save_runtime_settings
    from ..runtime_environment import install_cuda_environment, plugin_version, runtime_summary


class RuntimeSettingsPanel(wx.Panel):
    def __init__(self, parent, log_callback=None):
        super().__init__(parent)
        self.log_callback = log_callback
        self.settings = load_runtime_settings()
        self._devices = []
        self._init_ui()
        self.refresh_status()

    def _log(self, message):
        if self.log_callback:
            self.log_callback(message)

    def _init_ui(self):
        main = wx.BoxSizer(wx.VERTICAL)

        identity = wx.StaticBoxSizer(wx.VERTICAL, self, "Plugin and Python Runtime")
        root = Path(__file__).resolve().parent.parent
        self.lbl_version = wx.StaticText(identity.GetStaticBox(), label=f"Ki-PIDA version: {plugin_version(root)}")
        self.lbl_python = wx.StaticText(identity.GetStaticBox(), label="Python: detecting...")
        identity.Add(self.lbl_version, 0, wx.ALL, 5)
        identity.Add(self.lbl_python, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)
        main.Add(identity, 0, wx.EXPAND | wx.ALL, 6)

        compute = wx.StaticBoxSizer(wx.VERTICAL, self, "Compute Backend")
        parent = compute.GetStaticBox()
        grid = wx.FlexGridSizer(cols=2, hgap=8, vgap=8)
        grid.AddGrowableCol(1, 1)
        self.choice_backend = wx.Choice(parent, choices=["AUTO", "CPU", "CUDA"])
        self.chk_cpu_threads = wx.CheckBox(parent, label="Enable CPU multithreading")
        self.spin_cpu_threads = wx.SpinCtrl(parent, min=0, max=max(1, os.cpu_count() or 1), initial=0)
        self.chk_cuda = wx.CheckBox(parent, label="Enable CUDA acceleration")
        self.choice_device = wx.Choice(parent, choices=[])
        self.spin_cuda_threshold = wx.SpinCtrl(parent, min=1000, max=10000000, initial=100000)
        for label, control in (
            ("Preferred backend:", self.choice_backend),
            ("CPU multithreading:", self.chk_cpu_threads),
            ("CPU threads (0 = Auto):", self.spin_cpu_threads),
            ("CUDA:", self.chk_cuda),
            ("CUDA device:", self.choice_device),
            ("AUTO CUDA threshold (nodes):", self.spin_cuda_threshold),
        ):
            grid.Add(wx.StaticText(parent, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(control, 1, wx.EXPAND)
        compute.Add(grid, 0, wx.EXPAND | wx.ALL, 8)
        main.Add(compute, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        actions = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refresh = wx.Button(self, label="Refresh Diagnostics")
        self.btn_test_cpu = wx.Button(self, label="Test CPU")
        self.btn_test_cuda = wx.Button(self, label="Test CUDA")
        self.btn_install_cuda = wx.Button(self, label="Install / Repair CUDA")
        self.btn_save = wx.Button(self, label="Save Runtime Settings")
        for button in (
            self.btn_refresh, self.btn_test_cpu, self.btn_test_cuda,
            self.btn_install_cuda, self.btn_save,
        ):
            actions.Add(button, 0, wx.RIGHT, 6)
        main.Add(actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.txt_status = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL,
        )
        main.Add(self.txt_status, 1, wx.EXPAND | wx.ALL, 6)
        self.SetSizer(main)

        self.btn_refresh.Bind(wx.EVT_BUTTON, lambda event: self.refresh_status())
        self.btn_save.Bind(wx.EVT_BUTTON, self._on_save)
        self.btn_test_cpu.Bind(wx.EVT_BUTTON, lambda event: self._start_test("CPU"))
        self.btn_test_cuda.Bind(wx.EVT_BUTTON, lambda event: self._start_test("CUDA"))
        self.btn_install_cuda.Bind(wx.EVT_BUTTON, self._on_install_cuda)
        self._set_controls(self.settings)

    def _set_controls(self, settings):
        index = self.choice_backend.FindString(settings.backend)
        self.choice_backend.SetSelection(index if index != wx.NOT_FOUND else 0)
        self.chk_cpu_threads.SetValue(settings.cpu_multithread)
        self.spin_cpu_threads.SetValue(settings.cpu_threads)
        self.chk_cuda.SetValue(settings.cuda_enabled)
        self.spin_cuda_threshold.SetValue(settings.cuda_min_nodes)

    def get_settings(self, persist=False):
        self.settings.backend = self.choice_backend.GetStringSelection() or "AUTO"
        self.settings.cpu_multithread = self.chk_cpu_threads.GetValue()
        self.settings.cpu_threads = self.spin_cpu_threads.GetValue()
        self.settings.cuda_enabled = self.chk_cuda.GetValue()
        self.settings.cuda_min_nodes = self.spin_cuda_threshold.GetValue()
        if self.choice_device.GetSelection() != wx.NOT_FOUND and self._devices:
            self.settings.cuda_device = self._devices[self.choice_device.GetSelection()]["index"]
        self.settings.normalized()
        if persist:
            save_runtime_settings(self.settings)
        return replace(self.settings)

    def _on_save(self, event):
        path = save_runtime_settings(self.get_settings())
        self._log(f"Runtime settings saved to {path}")

    def refresh_status(self):
        summary = runtime_summary()
        diagnostics = summary["cuda"]
        self._devices = diagnostics["devices"]
        self.choice_device.Clear()
        for device in self._devices:
            gib = device["total_bytes"] / (1024 ** 3)
            self.choice_device.Append(f"{device['index']}: {device['name']} ({gib:.1f} GiB)")
        if self._devices:
            selected = min(self.settings.cuda_device, len(self._devices) - 1)
            self.choice_device.SetSelection(selected)
        self.lbl_python.SetLabel(f"Python: {summary['python']} — {summary['executable']}")
        lines = [
            f"CPU logical processors: {os.cpu_count() or 1}",
            f"CPU sparse backend: {'PARDISO' if summary['pypardiso'] else 'SciPy SuperLU'}",
            f"Thread control: {'available' if summary['threadpoolctl'] else 'not installed'}",
            f"CuPy: {diagnostics['cupy_version']}",
            f"CUDA available: {'Yes' if diagnostics['available'] else 'No'}",
        ]
        if diagnostics["driver_version"] is not None:
            lines.append(f"CUDA driver/runtime: {diagnostics['driver_version']} / {diagnostics['runtime_version']}")
        for device in self._devices:
            lines.append(
                f"GPU {device['index']}: {device['name']} — "
                f"{device['free_bytes'] / (1024 ** 3):.1f}/"
                f"{device['total_bytes'] / (1024 ** 3):.1f} GiB free"
            )
        if diagnostics["error"]:
            lines.append(f"CUDA diagnostic: {diagnostics['error']}")
        self.txt_status.SetValue("\n".join(lines))

    def _start_test(self, backend_name):
        settings = self.get_settings()
        settings.backend = backend_name
        if backend_name == "CUDA":
            settings.cuda_enabled = True
        self.txt_status.AppendText(f"\nTesting {backend_name} backend...\n")
        thread = threading.Thread(
            target=self._test_worker, args=(settings,), daemon=True,
            name=f"KiPIDA-{backend_name}-Test",
        )
        thread.start()

    def _test_worker(self, settings):
        try:
            size = 5000
            matrix = scipy.sparse.diags(
                (-np.ones(size - 1), 2.1 * np.ones(size), -np.ones(size - 1)),
                offsets=(-1, 0, 1), format="csr",
            )
            rhs = np.ones(size)
            started = time.perf_counter()
            result = SparseComputeBackend(settings).solve(matrix, rhs, "SPD")
            elapsed = time.perf_counter() - started
            message = (
                f"{result.metadata.backend} / {result.metadata.device}: "
                f"{elapsed:.3f} s, residual={result.metadata.relative_residual:.3e}"
            )
        except Exception as exc:
            message = f"Backend test failed: {exc}"
        wx.CallAfter(self.txt_status.AppendText, message + "\n")

    def _on_install_cuda(self, event):
        answer = wx.MessageBox(
            "Install or update the optional CuPy CUDA environment in the Python runtime used by KiCad?\n\n"
            "A KiCad restart will be required afterwards.",
            "Install CUDA Backend", wx.YES_NO | wx.ICON_QUESTION,
        )
        if answer != wx.YES:
            return
        self.btn_install_cuda.Disable()
        thread = threading.Thread(target=self._install_worker, daemon=True, name="KiPIDA-CUDA-Install")
        thread.start()

    def _install_worker(self):
        def output(line):
            wx.CallAfter(self.txt_status.AppendText, line + "\n")
        try:
            code, command = install_cuda_environment(output)
            message = f"CUDA install command exited with code {code}: {' '.join(command)}\n"
        except Exception as exc:
            message = f"CUDA installation failed: {exc}\n"
        wx.CallAfter(self.txt_status.AppendText, message)
        wx.CallAfter(self.btn_install_cuda.Enable)
