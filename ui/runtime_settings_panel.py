"""Machine-local CPU/CUDA configuration panel."""

from dataclasses import replace
import os
from pathlib import Path
import threading
import time

import numpy as np
import scipy.sparse
import wx

from i18n import _, SYSTEM_LANGUAGE, available_languages

try:
    from compute_backend import SparseComputeBackend, cuda_diagnostics
    from runtime_config import load_runtime_settings, save_runtime_settings, system_memory_info
    from runtime_environment import install_cuda_environment, plugin_version, runtime_summary
except (ImportError, ValueError):
    from ..compute_backend import SparseComputeBackend, cuda_diagnostics
    from ..runtime_config import load_runtime_settings, save_runtime_settings, system_memory_info
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

        language_box = wx.StaticBoxSizer(wx.VERTICAL, self, "Language")
        language_parent = language_box.GetStaticBox()
        language_row = wx.BoxSizer(wx.HORIZONTAL)
        language_row.Add(wx.StaticText(language_parent, label="Interface language:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.choice_language = wx.Choice(language_parent)
        self._language_codes = [SYSTEM_LANGUAGE] + list(available_languages())
        self.choice_language.Append(_("System default"))
        for code, label in available_languages().items():
            self.choice_language.Append(label)
        language_row.Add(self.choice_language, 1, wx.EXPAND)
        language_box.Add(language_row, 0, wx.EXPAND | wx.ALL, 8)
        language_box.Add(wx.StaticText(
            language_parent,
            label="The selected language is applied the next time the Ki-PIDA window is opened.",
        ), 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        main.Add(language_box, 0, wx.EXPAND | wx.ALL, 6)

        identity = wx.StaticBoxSizer(wx.VERTICAL, self, "Plugin and Python Runtime")
        root = Path(__file__).resolve().parent.parent
        self.lbl_version = wx.StaticText(
            identity.GetStaticBox(),
            label=_("Ki-PIDA version: {version}").format(version=plugin_version(root)),
        )
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
        self.spin_memory_limit = wx.SpinCtrlDouble(parent, min=0.0, max=256.0, initial=0.0, inc=1.0)
        self.spin_memory_limit.SetDigits(1)
        for label, control in (
            ("Preferred backend:", self.choice_backend),
            ("CPU multithreading:", self.chk_cpu_threads),
            ("CPU threads (0 = Auto):", self.spin_cpu_threads),
            ("CUDA:", self.chk_cuda),
            ("CUDA device:", self.choice_device),
            ("AUTO CUDA threshold (nodes):", self.spin_cuda_threshold),
            ("Thermal RAM limit GiB (0 = conservative default):", self.spin_memory_limit),
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
        try:
            language_index = self._language_codes.index(settings.ui_language)
        except ValueError:
            language_index = 0
        self.choice_language.SetSelection(language_index)
        index = self.choice_backend.FindString(settings.backend)
        self.choice_backend.SetSelection(index if index != wx.NOT_FOUND else 0)
        self.chk_cpu_threads.SetValue(settings.cpu_multithread)
        self.spin_cpu_threads.SetValue(settings.cpu_threads)
        self.chk_cuda.SetValue(settings.cuda_enabled)
        self.spin_cuda_threshold.SetValue(settings.cuda_min_nodes)
        self.spin_memory_limit.SetValue(settings.memory_limit_gib)

    def get_settings(self, persist=False):
        language_index = self.choice_language.GetSelection()
        self.settings.ui_language = (
            self._language_codes[language_index]
            if 0 <= language_index < len(self._language_codes) else SYSTEM_LANGUAGE
        )
        self.settings.backend = self.choice_backend.GetStringSelection() or "AUTO"
        self.settings.cpu_multithread = self.chk_cpu_threads.GetValue()
        self.settings.cpu_threads = self.spin_cpu_threads.GetValue()
        self.settings.cuda_enabled = self.chk_cuda.GetValue()
        self.settings.cuda_min_nodes = self.spin_cuda_threshold.GetValue()
        self.settings.memory_limit_gib = self.spin_memory_limit.GetValue()
        if self.choice_device.GetSelection() != wx.NOT_FOUND and self._devices:
            self.settings.cuda_device = self._devices[self.choice_device.GetSelection()]["index"]
        self.settings.normalized()
        if persist:
            save_runtime_settings(self.settings)
        return replace(self.settings)

    def _on_save(self, event):
        previous_language = self.settings.ui_language
        path = save_runtime_settings(self.get_settings())
        self._log(_("Runtime settings saved to {path}").format(path=path))
        if self.settings.ui_language != previous_language:
            wx.MessageBox(
                _("The language change will be applied the next time the Ki-PIDA window is opened."),
                _("Language change"), wx.OK | wx.ICON_INFORMATION,
            )

    def refresh_status(self):
        summary = runtime_summary()
        memory = system_memory_info()
        diagnostics = summary["cuda"]
        self._devices = diagnostics["devices"]
        self.choice_device.Clear()
        for device in self._devices:
            gib = device["total_bytes"] / (1024 ** 3)
            self.choice_device.Append(f"{device['index']}: {device['name']} ({gib:.1f} GiB)")
        if self._devices:
            selected = min(self.settings.cuda_device, len(self._devices) - 1)
            self.choice_device.SetSelection(selected)
        self.lbl_python.SetLabel(
            _("Python: {version} — {executable}").format(
                version=summary['python'], executable=summary['executable'],
            )
        )
        lines = [
            _("CPU logical processors: {count}").format(count=os.cpu_count() or 1),
            _("CPU sparse backend: {backend}").format(backend='PARDISO' if summary['pypardiso'] else 'SciPy SuperLU'),
            _("Thread control: {state}").format(state=_("available") if summary['threadpoolctl'] else _("not installed")),
            _("CuPy: {version}").format(version=diagnostics['cupy_version']),
            _("CUDA available: {state}").format(state=_("Yes") if diagnostics['available'] else _("No")),
        ]
        if memory["total_bytes"]:
            lines.append(
                _("System RAM: {available:.1f}/{total:.1f} GiB available").format(
                    available=memory['available_bytes'] / (1024 ** 3),
                    total=memory['total_bytes'] / (1024 ** 3),
                )
            )
        ceiling = self.settings.memory_limit_gib
        lines.append(
            _("Thermal RAM ceiling: {ceiling}").format(ceiling=(
                _("{value:g} GiB (expert override)").format(value=ceiling)
                if ceiling > 0 else _("conservative default")
            ))
        )
        if diagnostics["driver_version"] is not None:
            lines.append(_("CUDA driver/runtime: {driver} / {runtime}").format(
                driver=diagnostics['driver_version'], runtime=diagnostics['runtime_version'],
            ))
        for device in self._devices:
            lines.append(
                _("GPU {index}: {name} — {free:.1f}/{total:.1f} GiB free").format(
                    index=device['index'], name=device['name'],
                    free=device['free_bytes'] / (1024 ** 3), total=device['total_bytes'] / (1024 ** 3),
                )
            )
        if diagnostics["error"]:
            lines.append(_("CUDA diagnostic: {error}").format(error=diagnostics['error']))
        self.txt_status.SetValue("\n".join(lines))

    def _start_test(self, backend_name):
        settings = self.get_settings()
        settings.backend = backend_name
        if backend_name == "CUDA":
            settings.cuda_enabled = True
        self.txt_status.AppendText(_("\nTesting {backend} backend...\n").format(backend=backend_name))
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
            message = _("Backend test failed: {error}").format(error=exc)
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
