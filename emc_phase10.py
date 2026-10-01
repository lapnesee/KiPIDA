"""Phase 10 multi-fidelity EMC orchestration.

The module deliberately keeps external solvers outside KiCad's Python process.
Every output records whether it is measured, circuit-derived, full-wave, or an
engineering estimate so that unavailable inputs never become silent defaults.
"""

from dataclasses import asdict, replace
from datetime import datetime
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

try:
    from shapely.geometry import LineString, Point, box as geometry_box
except ImportError:  # pragma: no cover - normal Ki-PIDA runtime includes Shapely
    LineString = Point = geometry_box = None

try:
    from .models import (
        EMCBoardCircuitResult, EMCPhase10ExcitationResult, EMCPhase10RegionResult,
        EMCPhase10Result, EMCPalaceRemoteRunResult,
        EMCPhase10ToolStatus, EMCSpiceModelAudit, EMCVirtualReceiverPoint,
    )
    from .emc_analyzer import hot_loop_components
    from .ingest.board_reader import read_board
    from .ingest.netlist_builder import is_ground_name
    from .palace_remote import PalaceRemoteClient, PalaceRemoteConnection
except (ImportError, ValueError):
    from models import (
        EMCBoardCircuitResult, EMCPhase10ExcitationResult, EMCPhase10RegionResult,
        EMCPhase10Result, EMCPalaceRemoteRunResult,
        EMCPhase10ToolStatus, EMCSpiceModelAudit, EMCVirtualReceiverPoint,
    )
    from emc_analyzer import hot_loop_components
    from ingest.board_reader import read_board
    from ingest.netlist_builder import is_ground_name
    from palace_remote import PalaceRemoteClient, PalaceRemoteConnection


WINDOWS_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
WINDOWS_NEW_PROCESS_GROUP = 0x00000200 if os.name == "nt" else 0


def _run(command, timeout=20.0, cwd=None, environment=None):
    return subprocess.run(
        [str(item) for item in command], cwd=str(cwd) if cwd else None,
        env=environment, text=True, capture_output=True, timeout=timeout,
        creationflags=WINDOWS_NO_WINDOW,
    )


def _format_bytes(value):
    value = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB"):
        if value < 1024.0 or suffix == "GiB":
            return f"{value:.1f} {suffix}"
        value /= 1024.0


def parse_openems_log(path, maximum_timesteps=0):
    """Extract convergence evidence from openEMS stdout without guessing."""
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    iteration_matches = re.findall(
        r"Time for\s+(\d+)\s+iterations with\s+([0-9.]+)\s+cells", text, re.I,
    )
    iterations = int(iteration_matches[-1][0]) if iteration_matches else 0
    cells = int(float(iteration_matches[-1][1])) if iteration_matches else 0
    energy_matches = re.findall(r"Energy:\s*~[^\(]+\(-\s*([0-9.]+)dB\)", text, re.I)
    energy_decay_db = float(energy_matches[-1]) if energy_matches else None
    hit_limit = "max. number of timesteps was reached before the end-criteria" in text.lower()
    if hit_limit:
        converged = False
    elif iterations and maximum_timesteps and iterations < maximum_timesteps:
        converged = True
    else:
        converged = None
    pulse_match = re.search(
        r"Requested excitation pu(?:ls|sl)e would be\s+(\d+)\s+timesteps.*?Cutting",
        text, re.I | re.S,
    )
    unused = len(re.findall(r"Warning:\s+Unused primitive", text, re.I))
    warnings = []
    if hit_limit:
        warnings.append(
            "openEMS reached the maximum timestep count before its end criterion; "
            "field values are retained only as non-converged diagnostics."
        )
    if pulse_match:
        warnings.append(
            f"The requested excitation required {int(pulse_match.group(1)):,} timesteps "
            "and was truncated by the configured limit."
        )
    if unused:
        warnings.append(
            f"openEMS reported {unused} unused copper primitive(s); inspect overlaps and mesh alignment."
        )
    if "h5py is running against hdf5" in text.lower():
        warnings.append(
            "The openEMS Python environment reports an h5py/HDF5 build-version mismatch."
        )
    return {
        "iterations": iterations, "cells": cells, "converged": converged,
        "energy_decay_db": energy_decay_db, "unused_primitives": unused,
        "warnings": warnings,
    }


def _supports_openems_port(source):
    """True when the worker can construct every conductor required by the source."""
    kind = str(source.kind).upper()
    return kind == "SWITCHING" or (
        kind == "DIFFERENTIAL" and bool(source.net_name and source.negative_net_name)
    )


def _terminate_owned_process(process):
    """Stop the exact worker tree without affecting unrelated solver jobs."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            text=True, capture_output=True, timeout=10,
            creationflags=WINDOWS_NO_WINDOW,
        )
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    else:  # pragma: no cover - Windows is the supported KiCad host here
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def _run_monitored(
    command, timeout, cwd, environment, log_callback=None,
    cancellation_callback=None, progress_interval_s=5.0,
):
    """Run an external solver with bounded execution and visible heartbeats."""
    cwd = Path(cwd)
    log_path = cwd / "solver.log"
    started = time.monotonic()
    last_report = -float(progress_interval_s)
    creationflags = WINDOWS_NO_WINDOW | WINDOWS_NEW_PROCESS_GROUP
    with log_path.open("w", encoding="utf-8", errors="replace") as stream:
        process = subprocess.Popen(
            [str(item) for item in command], cwd=str(cwd), env=environment,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        def drain_output():
            try:
                for line in process.stdout or ():
                    stream.write(line)
                    stream.flush()
            except (OSError, ValueError):
                pass

        output_thread = threading.Thread(target=drain_output, daemon=True)
        output_thread.start()
        interrupted_status = ""
        while process.poll() is None:
            elapsed = time.monotonic() - started
            if cancellation_callback and cancellation_callback():
                _terminate_owned_process(process)
                interrupted_status = "CANCELLED"
                break
            if elapsed >= timeout:
                _terminate_owned_process(process)
                interrupted_status = "TIMEOUT"
                break
            if log_callback and elapsed - last_report >= progress_interval_s:
                last_report = elapsed
                field_files = list(cwd.glob("openems/*.h5"))
                field_bytes = sum(path.stat().st_size for path in field_files if path.is_file())
                log_callback(
                    f"running {elapsed:.0f} s / {timeout:.0f} s; "
                    f"field output {_format_bytes(field_bytes)}"
                )
            time.sleep(0.25)
        output_thread.join(timeout=2.0)
        if process.stdout is not None:
            process.stdout.close()
        if interrupted_status:
            return None, interrupted_status, log_path
    return process.returncode, "COMPLETED", log_path


def _existing_file(*candidates):
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file():
            return path
        located = shutil.which(str(candidate))
        if located:
            return Path(located)
    return None


class Phase10Toolchain:
    """Detect external tools, including known Windows installs outside PATH."""

    def __init__(self, settings, module_directory=None):
        self.settings = settings
        self.module_directory = Path(module_directory or Path(__file__).parent)

    def _phase10_python(self):
        configured = self.settings.openems_python_path
        openems_root = Path(self.settings.openems_root or r"C:\openEMS")
        candidates = [
            configured,
            os.environ.get("KIPIDA_OPENEMS_PYTHON", ""),
            openems_root / "phase10-venv" / "Scripts" / "python.exe",
        ]
        runtime = self.module_directory / ".runtime" / "phase10-venv" / "Scripts" / "python.exe"
        candidates.append(runtime)
        candidates.extend(
            self.module_directory.glob(
                ".runtime/python/cpython-3.13*-windows-x86_64-none/python.exe"
            )
        )
        return _existing_file(*candidates)

    @staticmethod
    def _status(name, path, version_args=()):
        if path is None:
            return EMCPhase10ToolStatus(name, False, detail="Not found")
        version = ""
        detail = "Detected"
        if version_args:
            try:
                completed = _run([path, *version_args], timeout=15.0)
                output = (completed.stdout + "\n" + completed.stderr).strip()
                lines = [line.strip() for line in output.splitlines() if line.strip()]
                version = next(
                    (line for line in lines
                     if name.lower() in line.lower() or "version" in line.lower()),
                    lines[0] if lines else "",
                )
                if completed.returncode != 0:
                    detail = f"Detected; version probe returned {completed.returncode}"
            except Exception as exc:  # pragma: no cover - host-specific process failure
                detail = f"Detected; version probe failed: {exc}"
        return EMCPhase10ToolStatus(name, True, str(path), version, detail)

    def detect(self):
        openems_root = Path(self.settings.openems_root or r"C:\openEMS")
        ngspice = _existing_file(
            self.settings.ngspice_path, r"C:\Spice64\bin\ngspice_con.exe",
            "ngspice_con", "ngspice",
        )
        openems = _existing_file(openems_root / "openEMS.exe", "openEMS")
        gmsh = _existing_file(
            self.settings.gmsh_path,
            openems_root / "phase10-venv" / "Scripts" / "gmsh.bat",
            openems_root / "phase10-venv" / "Scripts" / "gmsh",
            "gmsh",
        )
        palace = _existing_file(self.settings.palace_path, "palace")
        pspice = _existing_file(getattr(self.settings, "pspice_path", ""), PSPICE_DEFAULT_PATH)
        python = self._phase10_python()
        statuses = [
            self._status("NGSPICE", ngspice, ("--version",)),
            # No version probe: pspice.exe opens its window and stays open.
            self._status("PSPICE", pspice),
            # The Windows openEMS executable prints a banner but some builds do
            # not terminate for --version; the Python API probe below is the
            # bounded readiness check.
            self._status("OPENEMS", openems),
            self._status("OPENEMS_PYTHON", python, ("--version",)),
            self._status("GMSH", gmsh),
            self._status("PALACE", palace, ("--version",)),
        ]
        if python is not None:
            env = os.environ.copy()
            env["CSXCAD_INSTALL_PATH"] = str(openems_root)
            env["PATH"] = str(openems_root) + os.pathsep + env.get("PATH", "")
            try:
                probe = _run(
                    [python, "-c", "import CSXCAD, openEMS; print('API_OK')"],
                    environment=env,
                )
                target = next(item for item in statuses if item.name == "OPENEMS_PYTHON")
                target.available = probe.returncode == 0 and "API_OK" in probe.stdout
                target.detail = "Python API ready" if target.available else (
                    "Python found but openEMS API unavailable: "
                    + (probe.stderr.strip() or probe.stdout.strip())[-400:]
                )
            except Exception as exc:
                target = next(item for item in statuses if item.name == "OPENEMS_PYTHON")
                target.available = False
                target.detail = f"Python API probe failed: {exc}"
            try:
                probe = _run(
                    [python, "-c", "import gmsh; print(gmsh.__version__)"],
                    environment=env,
                )
                target = next(item for item in statuses if item.name == "GMSH")
                if probe.returncode == 0:
                    target.available = True
                    target.path = target.path or str(python)
                    target.version = probe.stdout.strip()
                    target.detail = "Gmsh Python API ready"
            except Exception:
                pass
        return statuses


class SpiceExcitationRunner:
    """Create traceable parametric transient sources and validate them in ngspice."""

    def __init__(self, executable, timeout_s=60.0):
        self.executable = Path(executable) if executable else None
        self.timeout_s = timeout_s

    def run(self, source, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(char if char.isalnum() or char == "_" else "_" for char in source.name)
        netlist = directory / f"{safe_name}.cir"
        waveform = directory / f"{safe_name}.dat"
        frequency = max(float(source.frequency_hz), 1.0)
        period = 1.0 / frequency
        rise = max(float(source.rise_time_ns) * 1e-9, period / 100000.0)
        fall = rise
        high = max(period * 0.5 - rise, period * 0.05)
        resistance = max(abs(float(source.voltage_swing_v)) / max(abs(float(source.current_a)), 1e-6), 1e-3)
        step = min(rise / 10.0, period / 250.0)
        text = "\n".join([
            f"* Ki-PIDA Phase 10 parametric excitation for {source.name}",
            f"VDRIVE out 0 PULSE(0 {source.voltage_swing_v:.12g} 0 {rise:.12g} {fall:.12g} {high:.12g} {period:.12g})",
            f"RLOAD out 0 {resistance:.12g}",
            ".control", "set wr_vecnames", "set wr_singlescale",
            f"tran {step:.12g} {4.0 * period:.12g}",
            # ngspice tokenizes absolute Windows paths and punctuation.  The
            # process already runs here and the leaf is sanitized above.
            f"wrdata {waveform.name} time v(out) i(vdrive)",
            "quit", ".endc", ".end", "",
        ])
        netlist.write_text(text, encoding="utf-8")
        dv_dt = abs(float(source.voltage_swing_v)) / rise
        di_dt = abs(float(source.current_a)) / rise
        result = EMCPhase10ExcitationResult(
            source_name=source.name, status="PARAMETRIC_ONLY",
            provenance="PARAMETRIC_SOURCE_SETTINGS",
            peak_voltage_v=abs(float(source.voltage_swing_v)),
            peak_current_a=abs(float(source.current_a)),
            maximum_dv_dt_v_s=dv_dt, maximum_di_dt_a_s=di_dt,
            waveform_path=str(waveform),
            notes=[
                "Ideal pulse and resistive load; replace with a manufacturer or measured model for sign-off.",
            ],
        )
        if not self.executable or not self.executable.is_file():
            result.status = "SKIPPED_TOOL_MISSING"
            return result
        try:
            completed = _run(
                [self.executable, "-b", str(netlist)], timeout=self.timeout_s, cwd=directory,
            )
            if completed.returncode == 0 and waveform.is_file():
                result.status = "SIMULATED"
                # EMC-6: ngspice only numerically renders an ideal pulse
                # into a resistive load here (no real device model is
                # involved), so "PARAMETRIC_NGSPICE" overstated it as a
                # circuit simulation.
                result.provenance = "IDEAL_PULSE"
            else:
                result.status = "FAILED"
                result.notes.append((completed.stderr or completed.stdout)[-600:])
        except Exception as exc:
            result.status = "FAILED"
            result.notes.append(str(exc))
        return result


def _normalized_part(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _source_component_reference(source):
    text = f"{source.name} {source.net_name} {source.negative_net_name}"
    match = re.search(r"(?<![A-Z0-9])(U\d+)(?![A-Z0-9])", text, re.I)
    return match.group(1).upper() if match else ""


VERIFIED_SPICE_MAPPINGS = {
    "TPS568236RJNR": {
        "model_name": "TPS568236_TRANS",
        "model_order": "VIN VBST EN SW PG FB VCC SS MODE AGND PGND",
        "symbol_order": "1:VIN 2:PGND 3:PG 4:FB 5:SS 6:NC 7:SW 8:VBST 9:VCC 10:AGND 11:EN 12:MODE",
        "wrapper_name": "TPS568236_RJN",
        "wrapper": (
            ".subckt TPS568236_RJN VIN PGND PG FB SS NC SW VBST VCC AGND EN MODE\n"
            "XCORE VIN VBST EN SW PG FB VCC SS MODE AGND PGND TPS568236_TRANS\n"
            ".ends TPS568236_RJN\n"
        ),
        "source": "TI TPS568236 datasheet; KiCad U4 symbol; TPS568236_TRANS .SUBCKT",
    },
    "TPS562200DDCT": {
        "model_name": "TPS562200_TRANS",
        "model_order": "EN GND SW VBST VFB VIN",
        "symbol_order": "1:GND 2:SW 3:VIN 4:VFB 5:EN 6:VBST",
        "wrapper_name": "TPS562200_DDC",
        "wrapper": (
            ".subckt TPS562200_DDC GND SW VIN VFB EN VBST\n"
            "XCORE EN GND SW VBST VFB VIN TPS562200_TRANS\n"
            ".ends TPS562200_DDC\n"
        ),
        "source": "TI TPS562200 datasheet; KiCad U5 symbol; TPS562200_TRANS .SUBCKT",
    },
}


# Markers of a library only PSpice can run: TI's own and Cadence's.
_ENCRYPTION_MARKERS = ("$ENCRYPTED_LIB", "$CDNENCSTART")

PSPICE_DEFAULT_PATH = r"C:\Cadence\PSpiceTI\tools\bin\pspice.exe"


def read_pspice_print(path):
    """``{column: [values]}`` of a PSpice .out's .PRINT TRAN tables.

    PSpice splits a table wider than its line into several, each with its own
    TIME column; the columns are joined by name, TIME taken from the first.
    Its header columns are 16 characters wide, and a name that fills one
    runs into the next: "V(N_MAIN_3V3_SW)I(L_L2)".
    """
    text = Path(path).read_text(encoding="latin-1", errors="replace")
    tables, header, rows = [], None, []
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == "TIME":
            if header:
                tables.append((header, rows))
            header, rows = ["TIME"] + re.findall(r"[^\s()]+\([^()]*\)", line), []
            continue
        if header and len(parts) == len(header):
            try:
                rows.append([float(item.replace("D", "E")) for item in parts])
            except ValueError:
                continue
    if header:
        tables.append((header, rows))
    columns = {}
    for index, (names, values) in enumerate(tables):
        for position, name in enumerate(names):
            if name == "TIME" and index:
                continue
            columns.setdefault(name, []).extend(row[position] for row in values)
    return columns


class PSpiceRunner:
    """Run one circuit file in PSpice for TI and close it again.

    PSpice for TI has no command-line simulator: ``pspice.exe -r`` opens its
    window, runs the file through its simulation manager and stays open. The
    runner watches the .out for JOB CONCLUDED or an abort, then closes the
    window, which ends the manager too; a first close while PSpice still
    writes a large .out is ignored, so it is repeated. Killing PSpice instead
    left the manager behind, and every later run of 2026-09-30 stopped at
    "Le serveur RPC n'est pas disponible". A .PRINT of five columns stopped
    the same way; the benches here print at most four.
    """

    def __init__(self, executable, timeout_s=300.0, poll_s=2.0):
        self.executable = Path(executable) if executable else None
        self.timeout_s = float(timeout_s)
        self.poll_s = float(poll_s)

    @property
    def available(self):
        return bool(self.executable and self.executable.is_file())

    def run(self, netlist):
        """``(state, out_path, seconds)``: state is concluded, error, exited or timeout."""
        netlist = Path(netlist)
        out_path = netlist.with_suffix(".out")
        started = time.time()
        startup = None
        if os.name == "nt":
            # Minimised and not activated: the window must exist for the
            # close request, but not take the focus during a campaign.
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 7  # SW_SHOWMINNOACTIVE
        process = subprocess.Popen(
            [str(self.executable), "-r", netlist.name], cwd=str(netlist.parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startup,
        )
        state = "timeout"
        while time.time() - started < self.timeout_s:
            time.sleep(self.poll_s)
            if out_path.is_file() and out_path.stat().st_mtime >= started - 1.0:
                text = out_path.read_text(encoding="latin-1", errors="replace")
                if "JOB CONCLUDED" in text:
                    state = "concluded"
                    break
                if "JOB ABORTED" in text or re.search(r"(?m)^ERROR", text):
                    state = "error"
                    break
            if process.poll() is not None:
                state = "exited"
                break
        if state == "concluded":
            self._close(process)
        else:
            # A convergence failure pauses PSpice on its "Runtime Settings"
            # dialog, and closing a paused run asks "Stop and close the
            # simulation?": both wait for a click (2026-09-30). Only a run
            # that concluded is closed through its window.
            self._stop(process)
        return state, out_path, time.time() - started

    @staticmethod
    def _close_windows(pid):
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        handles = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def collect(hwnd, _param):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                handles.append(hwnd)
            return True

        user32.EnumWindows(collect, 0)
        for hwnd in handles:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE

    def _close(self, process):
        deadline = time.time() + 60.0
        while process.poll() is None and time.time() < deadline:
            self._close_windows(process.pid)
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                continue
        if process.poll() is None:
            self._stop(process)

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=15.0)
            except subprocess.TimeoutExpired:
                pass
        # The simulation manager and server are COM servers, not children of
        # pspice.exe, and one left behind made every later run stop at "RPC
        # server unavailable"; they go only when no other PSpice is running.
        if os.name == "nt":
            listing = _run(["tasklist", "/FI", "IMAGENAME eq pspice.exe", "/NH"], timeout=15.0)
            if "pspice.exe" not in listing.stdout.lower():
                for image in ("simmgr.exe", "SimSrvr.exe"):
                    _run(["taskkill", "/IM", image, "/F"], timeout=15.0)


def _logical_lines(text):
    """SPICE lines with their ``+`` continuations joined."""
    lines = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("+") and lines:
            lines[-1] += " " + stripped[1:]
        elif stripped and not stripped.startswith("*"):
            lines.append(stripped)
    return lines


def subckt_pins(model_path, name):
    """The pin names of .SUBCKT *name* in *model_path*, in order."""
    text = Path(model_path).read_text(encoding="latin-1", errors="replace")
    for line in _logical_lines(text):
        tokens = line.split()
        if len(tokens) > 2 and tokens[0].upper() == ".SUBCKT" and tokens[1].upper() == name.upper():
            pins = []
            for token in tokens[2:]:
                if token.upper().startswith("PARAMS:") or "=" in token:
                    break
                pins.append(token)
            return pins
    return []


def manufacturer_bench(model_path, name):
    """``(bench_path, nodes)`` of a test bench shipped beside the model.

    A manufacturer's archive carries the circuit its model was checked in:
    TI's LMR51450 (SNVMCB2) ships STEADY_STATE.net and START_UP.net, its
    TPS55288 (SLVMDD9) "TPS55288 Startup.net". The bench whose name says
    steady state is preferred; *nodes* are the instance's nets, pin order.
    """
    found = []
    for path in sorted(Path(model_path).parent.rglob("*.net")):
        try:
            text = path.read_text(encoding="latin-1", errors="replace")
        except OSError:
            continue
        for line in _logical_lines(text):
            tokens = line.split()
            if tokens and tokens[0].upper().startswith("X"):
                upper = [token.upper() for token in tokens]
                if name.upper() in upper[1:]:
                    nodes = tokens[1:upper.index(name.upper(), 1)]
                    found.append((0 if "STEADY" in path.name.upper() else 1, path, nodes))
                    break
    if not found:
        return None, []
    _rank, path, nodes = min(found, key=lambda item: (item[0], str(item[1])))
    return path, nodes


_SPICE_SCALES = {"T": 1e12, "G": 1e9, "MEG": 1e6, "K": 1e3, "M": 1e-3, "U": 1e-6, "N": 1e-9,
                 "P": 1e-12, "F": 1e-15}


def _spice_value(text):
    """A SPICE number with its scale suffix (``1.5m``, ``20n``, ``2MEG``), or None."""
    match = re.match(r"^([-+]?[0-9.]+(?:E[-+]?\d+)?)(MEG|[TGKMUNPF])?", str(text).strip().upper())
    if not match:
        return None
    return float(match.group(1)) * _SPICE_SCALES.get(match.group(2) or "", 1.0)


def bench_profile(bench_path):
    """``(stop_s, max_step_s)`` of the manufacturer's simulation profile of a bench.

    TI's profile circuit files .INC the bench and carry its analysis: the
    LMR51450's STEADY_STATE runs 1.5 ms at 20 ns, the TPS55288's Startup 5 ms
    at 5 ns, whose instance sets FAST_STARTUP=0 and does not switch in its
    first 100 us.
    """
    bench_path = Path(bench_path)
    for path in sorted(bench_path.parent.rglob("*.cir")):
        try:
            text = path.read_text(encoding="latin-1", errors="replace")
        except OSError:
            continue
        if bench_path.name.lower() not in text.lower():
            continue
        for line in _logical_lines(text):
            tokens = line.split()
            if tokens and tokens[0].upper() == ".TRAN" and len(tokens) >= 3:
                values = [_spice_value(token) for token in tokens[1:5]]
                stop = values[1]
                step = values[3] if len(values) > 3 else None
                if stop:
                    return stop, step
    return None


# Benches for models whose archive ships none; PSpice syntax.
PSPICE_PROBE_BENCHES = {
    "TPS562200_TRANS": (
        "VIN VIN 0 PWL(0 0 10u 5)\n"
        "VEN EN 0 PWL(0 0 20u 0 21u 5)\n"
        "CBST VBST SW 100n\n"
        "L1 SW OUT 3.3u\n"
        "COUT OUT 0 22u\n"
        "RLOAD OUT 0 11.2\n"
        "RFBT OUT VFB 34k\n"
        "RFBB VFB 0 10k\n"
        "XCORE EN 0 SW VBST VFB VIN TPS562200_TRANS\n",
        "SW",
    ),
}


def switching_summary(time_s, volts):
    """``(frequency_hz, low_v, high_v)`` of a switching waveform, or None."""
    if len(time_s) < 3:
        return None
    ordered = sorted(volts)
    low = ordered[int(0.02 * (len(ordered) - 1))]
    high = ordered[int(0.98 * (len(ordered) - 1))]
    if high - low < 1.0:
        return None
    middle = (low + high) / 2.0
    crossings = [
        time_s[index] for index in range(1, len(volts))
        if volts[index - 1] < middle <= volts[index]
    ]
    if len(crossings) < 3:
        return None
    periods = sorted(b - a for a, b in zip(crossings, crossings[1:]) if b > a)
    if not periods:
        return None
    return 1.0 / periods[len(periods) // 2], low, high


def subckt_parameters(model_path, name):
    """The PARAMS: names .SUBCKT *name* in *model_path* declares, as written."""
    text = Path(model_path).read_text(encoding="latin-1", errors="replace")
    for line in _logical_lines(text):
        tokens = line.split()
        if len(tokens) > 2 and tokens[0].upper() == ".SUBCKT" and tokens[1].upper() == name.upper():
            return [token.split("=", 1)[0] for token in tokens[2:] if "=" in token]
    return []


# Footprint values are case-sensitive, unlike SPICE numbers: "7.5m" is milli
# and "1M" mega; "2R2" and "4k7" put the scale where the point goes.
_VALUE_SCALES = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3,
                 "M": 1e6, "MEG": 1e6, "G": 1e9, "R": 1.0, "r": 1.0}


def footprint_value(text):
    """The value a footprint's Value field starts with, or None.

    P02 Rev E writes "10u 35V X6S", "4.7uH / Isat25.4A", "7.5m 1% / 3W",
    "2R2 DNP" and "249 k / 1%".
    """
    text = str(text or "").strip()
    match = re.match(r"^(\d+)([pnuµmkKMGRr])(\d+)(?![\w.])", text)
    if match:
        return float(f"{match.group(1)}.{match.group(3)}") * _VALUE_SCALES[match.group(2)]
    match = re.match(
        r"^(\d+(?:\.\d+)?|\.\d+)(?:\s*(MEG|Meg|meg|[pnuµmkKMGRr])(?=[FHΩ]|[Oo]hm|[^A-Za-z]|$))?", text,
    )
    if not match:
        return None
    scale = match.group(2) or "R"
    return float(match.group(1)) * _VALUE_SCALES["MEG" if scale.upper() == "MEG" else scale]


def _pin_base(function):
    # KiCad suffixes the pad number to a repeated pin's name: "SW_1".
    return re.sub(r"_\d+$", "", str(function or "")).upper()


_PIN_ALIASES = {"PGOOD": "PG", "PG": "PGOOD"}


def map_model_pins(model_pins, pads, node_of):
    """``({model_pin: node}, unmatched, conflicts)``, the model's pins by name.

    A pad's pin function is its symbol pin's name. A model pin takes the
    pads of the same name, then of its name less one trailing digit (TI's
    LMR51450 calls SW1 to SW3 what the P02 symbol calls SW, its TPS55288 SW22
    the second SW2 pin), then of an alias, then of a name whose first word it
    is (EN for the TPS552882's EN_UVLO). A ground pin nothing matches is
    grounded; *conflicts* names a pin whose pads sit on several nets.
    """
    bases = {}
    for pad in pads:
        base = _pin_base(getattr(pad, "pinfunction", ""))
        if base and pad.net_name:
            bases.setdefault(base, set()).add(node_of(pad.net_name))
    mapping, unmatched, conflicts = {}, [], []
    for pin in model_pins:
        name = pin.upper()
        tries = [name] + ([name[:-1]] if name[-1:].isdigit() else [])
        tries += [_PIN_ALIASES[item] for item in list(tries) if item in _PIN_ALIASES]
        nodes = next((bases[item] for item in tries if item in bases), None)
        if nodes is None:
            words = [base for base in bases if base.split("_")[0] in tries]
            nodes = set().union(*(bases[base] for base in words)) if words else None
        if not nodes:
            if is_ground_name(pin):
                mapping[pin] = "0"
            else:
                unmatched.append(pin)
        elif len(nodes) > 1:
            conflicts.append(f"{pin} matches pads on {', '.join(sorted(nodes))}")
        else:
            mapping[pin] = next(iter(nodes))
    return mapping, unmatched, conflicts


# Model parameters that start a TI transient model at its operating point
# rather than at power-up, set only when the model declares them. MODE=1
# with IC= on the inductor and capacitors (TI's LMR51450 model notes, C.1);
# the TPS562200 model also wants VOUT_SS at the output voltage: with MODE=1
# alone P02 Rev E's U5 was still ramping at 1.88 V after 300 us, with both
# at 3.304 V. TI's TPS55288 bench declares fast_startup=1000: at 0 U4 did
# not switch in 300 us, at 1000 it settled by 1.1 ms.
_STEADY_STATE_PARAMETERS = {
    "MODE": lambda vout: 1,
    "VOUT_SS": lambda vout: vout,
    "FAST_STARTUP": lambda vout: 1000,
}

# A pin the part defines open but the model does not, and the resistor to
# ground that the datasheet gives the same behaviour. TI's LMR51450 model
# never switched with RT open (P02 Rev E's U14); SLUSEP7 says "RT open
# defaults to 500 kHz" and gives 495 kHz typical at 31.6 kOhm.
_OPEN_PIN_EQUIVALENTS = {
    ("LMR51450_TRANS", "RT"): (31.6e3, "RT is open on the board; SLUSEP7 gives open 500 kHz, "
                                       "31.6 kOhm 495 kHz typical, so the bench fits 31.6 kOhm"),
}


def _bench_node(node):
    return "0" if node == "0" else "N_" + re.sub(r"[^A-Za-z0-9_]", "_", node.strip("/"))


def _board_parts(board, reference, node_of):
    """``(parts, foreign)``: the board's two-pin R, C and L and its G/D/S MOSFETs.

    A part is ``(kind, reference, nodes, value)``, nodes in pin order, D S G
    for a MOSFET. *foreign* holds every node another kind of part touches: a
    net reaching an IC or connector is not the converter's own.
    """
    parts, foreign = [], set()
    for footprint in board.footprints:
        if getattr(footprint, "dnp", False) or footprint.reference == reference:
            continue
        prefix = (re.match(r"[A-Za-z]+", footprint.reference) or [""])[0].upper()
        by_number = {}
        for pad in footprint.pads:
            if pad.number and pad.net_name:
                by_number.setdefault(pad.number, node_of(pad.net_name))
        part = None
        if prefix in {"R", "C", "L"} and len(by_number) == 2:
            nodes = tuple(by_number.values())
            value = footprint_value(footprint.value)
            if nodes[0] != nodes[1] and value is not None and (value > 0 or prefix == "R"):
                part = (prefix, footprint.reference, nodes, value)
        elif prefix == "Q":
            roles = {}
            for pad in footprint.pads:
                role = str(getattr(pad, "pinfunction", "") or "").split("_")[0].upper()
                if role in {"G", "D", "S"} and pad.net_name:
                    roles.setdefault(role, set()).add(node_of(pad.net_name))
            if all(len(roles.get(role, ())) == 1 for role in "DSG"):
                part = ("Q", footprint.reference, tuple(next(iter(roles[role])) for role in "DSG"), 0.0)
        if part is not None:
            parts.append(part)
        else:
            foreign.update(by_number.values())
    return parts, foreign


def board_converter_bench(board, reference, model_path, model_name, switch_net, rail_voltages,
                          load_a, input_fallback_v):
    """The converter around *reference* on the saved *board*, as a PSpice bench.

    Returns a dict: ``text`` (the bench), ``probes`` (switch node, inductor
    current, load), the input, output and load nodes and voltages, the pin
    mapping and the parts used. Raises ValueError with the reason when the
    circuit cannot be read.

    The model's pins take the IC's nets by pin name; the IC's nets, the
    inductor's far end and every net only two-pin R, C, L and G/D/S MOSFETs
    reach make the circuit, DNP parts left out. A MOSFET is a switch with a
    body diode, as TI's own TPS55288 bench models the TPS552882's external
    FETs (VSWITCH, 18 mOhm on above 1.5 V). The input is its rail's voltage
    behind 5 mOhm, as TI's LMR51450 bench; the load a resistor drawing the
    source's current at the output rail's voltage, placed past any series
    shunt under 0.1 ohm. Capacitors on the input and output and the inductor
    start at their operating point.
    """
    footprint = next((item for item in board.footprints if item.reference == reference), None)
    if footprint is None:
        raise ValueError(f"{reference} is not on the saved board")

    def node_of(net):
        return "0" if is_ground_name(net) else str(net)

    model_pins = subckt_pins(model_path, model_name)
    mapping, unmatched, conflicts = map_model_pins(model_pins, footprint.pads, node_of)
    if conflicts:
        raise ValueError("pin names disagree with the model: " + "; ".join(conflicts))
    core = {node for node in mapping.values() if node != "0" and not node.startswith("unconnected-")}
    switch_node = node_of(switch_net)
    if switch_node not in core:
        raise ValueError(f"{switch_net} is on no pin of {model_name}")
    parts, foreign = _board_parts(board, reference, node_of)
    inductor = next((part for part in parts if part[0] == "L" and switch_node in part[2]), None)
    if inductor is None:
        raise ValueError(f"no inductor on {switch_net}")
    far = inductor[2][1] if inductor[2][0] == switch_node else inductor[2][0]
    if far not in core:
        output_node = far
        core.add(far)
    else:
        # A buck-boost's inductor joins two switch pins; its output is a pin.
        output_pin = next((pin for pin in model_pins if pin.upper().startswith("VOUT") and pin in mapping), None)
        if output_pin is None:
            raise ValueError(f"{model_name} has no VOUT pin and its inductor no free end")
        output_node = mapping[output_pin]
    touching = {}
    for part in parts:
        for node in part[2]:
            touching.setdefault(node, []).append(part)
    accepted = core | {"0"}
    private = {node for part in parts if set(part[2]) & core for node in part[2]} - accepted - foreign
    while True:
        allowed = accepted | private
        rejected = {node for node in private
                    if any(not set(part[2]) <= allowed for part in touching.get(node, ()))}
        if not rejected:
            break
        private -= rejected
    allowed = accepted | private
    used = [part for part in parts if set(part[2]) <= allowed and set(part[2]) & (core | private)]
    load_node, seen = output_node, {output_node}
    while True:
        shunt = next((part for part in used if part[0] == "R" and part[3] < 0.1
                      and load_node in part[2] and not set(part[2]) <= seen), None)
        if shunt is None:
            break
        load_node = next(node for node in shunt[2] if node not in seen)
        seen.add(load_node)
    input_pin = next((pin for pin in model_pins if pin.upper().startswith("VIN") and pin in mapping), None)
    if input_pin is None:
        raise ValueError(f"no VIN pin of {model_name} is on the board")
    input_node = mapping[input_pin]
    input_v = float(rail_voltages.get(input_node) or input_fallback_v or 0.0)
    output_v = float(rail_voltages.get(load_node) or rail_voltages.get(output_node) or 0.0)
    if input_v <= 0.0 or output_v <= 0.0 or load_a <= 0.0:
        raise ValueError(
            f"input {input_node} {input_v:g} V, output {load_node} {output_v:g} V and load "
            f"{load_a:g} A must all be known"
        )
    node = _bench_node
    starts = {input_node: input_v, output_node: output_v, load_node: output_v}
    lines = [f"* Ki-PIDA board circuit of {reference} ({model_name})",
             f"V_KIPIDA_IN KIPIDA_SUPPLY 0 {input_v:.6g}",
             f"R_KIPIDA_IN KIPIDA_SUPPLY {node(input_node)} 5m"]
    connections = {input_node: 1, load_node: 1}
    for kind, ref, nodes, value in used:
        for item in set(nodes):
            connections[item] = connections.get(item, 0) + 1
        if kind == "R":
            lines.append(f"R_{ref} {node(nodes[0])} {node(nodes[1])} {max(value, 1e-6):.6g}")
        elif kind == "C":
            other = nodes[1] if nodes[0] == "0" else nodes[0] if nodes[1] == "0" else None
            start = f" IC={starts[other]:.6g}" if other in starts else ""
            lines.append(f"C_{ref} {node(nodes[0])} {node(nodes[1])} {value:.6g}{start}")
        elif kind == "L":
            start = ""
            if ref == inductor[1]:
                start = f" IC={load_a if nodes[0] == switch_node else -load_a:.6g}"
            lines.append(f"L_{ref} {node(nodes[0])} {node(nodes[1])} {value:.6g}{start}")
        else:
            drain, source, gate = (node(item) for item in nodes)
            lines += [f"S_{ref} {drain} {source} {gate} {source} KIPIDA_FET",
                      f"R_{ref}_GS {gate} {source} 1G",
                      f"D_{ref}_BODY {source} {drain} KIPIDA_BODY"]
    if any(part[0] == "Q" for part in used):
        lines += [".MODEL KIPIDA_FET VSWITCH Roff=1e9 Ron=18m Voff=0.7 Von=1.5",
                  ".MODEL KIPIDA_BODY D(IS=1e-12 RS=10m)"]
    lines.append(f"R_KIPIDA_LOAD {node(load_node)} 0 {output_v / load_a:.6g}")
    notes, pins = [], []
    for pin in model_pins:
        target = mapping.get(pin)
        if target is None or target.startswith("unconnected-"):
            target = f"OPEN_{reference}_{pin}"
        pins.append(node(target))
        if target == "0" or connections.get(target, 0):
            continue
        connections[target] = 1
        equivalent = _OPEN_PIN_EQUIVALENTS.get((model_name.upper(), pin.upper()))
        if pin.upper().startswith("EN"):
            level = min(3.3, input_v)
            lines.append(f"V_KIPIDA_{pin} {node(target)} 0 {level:.6g}")
            notes.append(f"{pin} is driven by nothing on the bench; it is held at {level:.3g} V.")
        elif equivalent is not None:
            lines.append(f"R_KIPIDA_{pin} {node(target)} 0 {equivalent[0]:.6g}")
            notes.append(equivalent[1] + ".")
        else:
            # Two, since PSpice does not count a pin its sub-circuit leaves
            # unconnected inside (TI's TPS55288 SCL_NULL).
            lines += [f"R_KIPIDA_{pin}_OPEN {node(target)} 0 2G",
                      f"R_KIPIDA_{pin}_OPEN2 {node(target)} 0 2G"]
    declared = subckt_parameters(model_path, model_name)
    parameters = [f"{name}={_STEADY_STATE_PARAMETERS[name.upper()](output_v):.6g}"
                  for name in declared if name.upper() in _STEADY_STATE_PARAMETERS]
    tokens = [f"X_{reference}", *pins, model_name] + (["PARAMS:", *parameters] if parameters else [])
    # PSpice ignored the whole 26-pin TPS55288 instance on one line.
    line = tokens[0]
    for token in tokens[1:]:
        if len(line) + len(token) > 78:
            lines.append(line)
            line = "+"
        line += " " + token
    lines.append(line)
    return {
        "text": "\n".join(lines) + "\n",
        "probes": [f"V([{node(switch_node)}])", f"I(L_{inductor[1]})", f"V([{node(load_node)}])"],
        "input_node": input_node, "input_v": input_v,
        "output_node": output_node, "load_node": load_node, "output_v": output_v,
        "inductor": inductor[1],
        "pin_mapping": "; ".join(
            [f"{pin}={mapping[pin]}" for pin in model_pins if pin in mapping]
            + [f"{pin} open" for pin in unmatched]
        ),
        "parts": sorted(part[1] for part in used),
        "notes": notes,
    }


def board_circuit_measurements(columns, probes):
    """What the printed window of a board bench shows, as a dict.

    The window is split in halves: the circuit is steady when its output
    and mean inductor current agree between them within 0.5 % and 3 %. The
    inductor's peak and valley are those of the second half.
    """
    by_name = {name.replace("[", "").replace("]", "").upper(): values for name, values in columns.items()}
    time_s = by_name.get("TIME", [])
    switch, inductor, output = (by_name.get(name.replace("[", "").replace("]", "").upper(), [])
                                for name in probes)
    if len(time_s) < 4 or not (len(switch) == len(inductor) == len(output) == len(time_s)):
        return {"status": "FAILED", "note": "PSpice printed no usable table."}
    summary = switching_summary(time_s, switch)
    if summary is None:
        return {"status": "NOT_SWITCHING", "output_v": output[-1]}
    middle = time_s[0] + (time_s[-1] - time_s[0]) / 2.0
    split = next(index for index, value in enumerate(time_s) if value >= middle)

    def mean(values):
        return sum(values) / max(1, len(values))

    first_v, second_v = mean(output[:split]), mean(output[split:])
    first_i, second_i = mean(inductor[:split]), mean(inductor[split:])
    steady = (abs(second_v - first_v) <= 0.005 * abs(second_v) + 1e-3
              and abs(second_i - first_i) <= 0.03 * abs(second_i) + 0.02)
    return {
        "status": "SWITCHING" if steady else "NOT_STEADY",
        "frequency_hz": summary[0], "switch_low_v": summary[1], "switch_high_v": summary[2],
        "output_v": second_v, "inductor_mean_a": second_i,
        "inductor_peak_a": max(inductor[split:]), "inductor_valley_a": min(inductor[split:]),
    }


def board_circuit_disagreements(source, result):
    """The configured *source* values the board circuit *result* contradicts.

    Not its current: a source's current_a is its converter's load (the
    power tree's downstream sum), which the bench draws, and the inductor
    peaks above it by half its ripple on every buck.
    """
    if result.status != "SWITCHING":
        return []
    found = []
    if source.frequency_hz > 0 and abs(result.frequency_hz - source.frequency_hz) > 0.1 * source.frequency_hz:
        found.append(f"it switches at {result.frequency_hz / 1e3:.0f} kHz, not the configured "
                     f"{source.frequency_hz / 1e3:.0f} kHz")
    swing = result.switch_high_v - result.switch_low_v
    if abs(swing - source.voltage_swing_v) > 0.1 * abs(source.voltage_swing_v):
        found.append(f"its switch node swings {swing:.2f} V, not the configured {source.voltage_swing_v:.2f} V")
    if result.output_nominal_v and abs(result.output_v - result.output_nominal_v) > 0.05 * result.output_nominal_v:
        found.append(f"its output settles at {result.output_v:.3f} V, not the rail's "
                     f"{result.output_nominal_v:.3f} V")
    return found


class BoardCircuitSimulator:
    """Run a switching source's converter in the board's own circuit.

    Each run prints its last 60 us (or 20 periods) and must be steady there;
    a circuit still settling runs again longer, up to 3 ms: P02 Rev E's U14
    and U5 settled within 300 us, U4 by 1.2 ms. Each run is cached on the
    model, the bench and the circuit, a timeout excepted.
    """

    STOP_TIMES_S = (300e-6, 1.2e-3, 3e-3)

    def __init__(self, pspice_runner, cache_directory=None):
        self.pspice = pspice_runner
        self.cache = Path(cache_directory) if cache_directory else (
            Path(tempfile.gettempdir()) / "kipida-pspice-probes"
        )

    def run(self, board, source, reference, model_path, model_name, rail_voltages, directory):
        result = EMCBoardCircuitResult(source.name, reference, "NOT_BUILT", model_name)
        try:
            bench = board_converter_bench(
                board, reference, model_path, model_name, source.net_name, rail_voltages,
                float(source.current_a), float(source.voltage_swing_v),
            )
        except (ValueError, OSError) as exc:
            result.notes.append(str(exc))
            return result
        result.input_v, result.load_a = bench["input_v"], float(source.current_a)
        result.output_nominal_v = bench["output_v"]
        result.pin_mapping, result.parts = bench["pin_mapping"], bench["parts"]
        result.notes.extend(bench["notes"])
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "bench.net").write_text(bench["text"], encoding="latin-1")
        period = 1.0 / (source.frequency_hz if source.frequency_hz > 0 else 500e3)
        step = min(5e-9, period / 400.0)
        window = max(60e-6, 20.0 * period)
        stat = Path(model_path).stat()
        started = time.time()
        for stop in self.STOP_TIMES_S:
            stop = max(stop, 2.0 * window)
            circuit = "\n".join([
                f"* Ki-PIDA board circuit of {reference} for {source.name}",
                f'.LIB "{Path(model_path)}"',
                '.lib "nom.lib"',
                f".TRAN {step:.6g} {stop:.6g} {stop - window:.6g} {step:.6g} SKIPBP",
                ".OPTIONS ADVCONV NUMDGT=8",
                ".OPTIONS ITL4=40",
                # After the bench: PSpice refused I(L_L3) before its inductor.
                '.INC "bench.net"',
                ".PRINT TRAN " + " ".join(bench["probes"]),
                ".END", "",
            ])
            netlist = directory / "board.cir"
            netlist.write_text(circuit, encoding="ascii", errors="replace")
            result.netlist_path = str(netlist)
            key = hashlib.sha1(
                f"{Path(model_path).resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{bench['text']}|{circuit}"
                .encode("utf-8", "replace")
            ).hexdigest()
            cache = self.cache / f"board-{key}.json"
            if cache.is_file():
                measured = json.loads(cache.read_text(encoding="utf-8"))
                result.notes.append(f"{stop * 1e3:.3g} ms run read from the cache.")
            else:
                state, out_path, seconds = self.pspice.run(netlist)
                if state == "timeout":
                    result.status = "TIMEOUT"
                    result.notes.append(
                        f"The {stop * 1e3:.3g} ms run passed the {seconds:.0f} s solver timeout; raise it "
                        "to let it finish, the result is then cached."
                    )
                    break
                if state == "concluded":
                    measured = board_circuit_measurements(read_pspice_print(out_path), bench["probes"])
                    result.output_path = str(out_path)
                else:
                    measured = {"status": "FAILED", "note": f"PSpice ended {state}; see {out_path}."}
                measured["seconds"] = seconds
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(measured), encoding="utf-8")
            result.status = measured["status"]
            result.simulated_s = stop
            for name in ("frequency_hz", "switch_low_v", "switch_high_v", "output_v",
                         "inductor_mean_a", "inductor_peak_a", "inductor_valley_a"):
                setattr(result, name, measured.get(name))
            if measured.get("note"):
                result.notes.append(measured["note"])
            if result.status in {"SWITCHING", "FAILED"}:
                break
        result.elapsed_seconds = time.time() - started
        result.disagreements = board_circuit_disagreements(source, result)
        return result


def board_circuit_excitation(source, circuit):
    """The excitation of *source* from its converter's board circuit.

    Swing and switched current are the circuit's; the edges keep the
    configured rise time, the model's switch edges being behavioural (TI's
    LMR51450 rises in 0.3 ns whatever its step).
    """
    rise_s = max(float(source.rise_time_ns) * 1e-9, 1e-12)
    swing = circuit.switch_high_v - circuit.switch_low_v
    return EMCPhase10ExcitationResult(
        source_name=source.name, status="SIMULATED", provenance="BOARD_CIRCUIT_PSPICE",
        peak_voltage_v=swing, peak_current_a=circuit.inductor_peak_a,
        maximum_dv_dt_v_s=swing / rise_s, maximum_di_dt_a_s=circuit.inductor_peak_a / rise_s,
        waveform_path=circuit.output_path,
        notes=[
            f"{circuit.component_ref} ({circuit.model_name}) in the board's circuit switched at "
            f"{circuit.frequency_hz / 1e3:.0f} kHz from {circuit.input_v:g} V into a {circuit.load_a:g} A "
            f"load, the inductor peaking at {circuit.inductor_peak_a:.2f} A; edges keep the configured "
            f"{source.rise_time_ns:g} ns rise time.",
        ],
    )


def board_circuit_sources(settings, circuits):
    """``(settings, revalued)``: *settings* with the sources whose board circuit settled re-valued.

    Such a source takes its circuit's frequency, swing and switched current,
    the inductor's peak that the switches chop, and keeps its configured
    rise time; *revalued* describes each. The configured settings are left
    alone: a source's current_a is the load its next board circuit draws.
    """
    settled = {item.source_name: item for item in circuits if item.status == "SWITCHING"}
    sources, revalued = [], []
    for source in settings.sources:
        circuit = settled.get(source.name)
        if circuit is None:
            sources.append(source)
            continue
        swing = circuit.switch_high_v - circuit.switch_low_v
        sources.append(replace(
            source, frequency_hz=circuit.frequency_hz, voltage_swing_v=swing,
            current_a=circuit.inductor_peak_a, source="board-circuit", parameter_confidence="HIGH",
            parameter_notes=(
                f"{circuit.component_ref} ({circuit.model_name}) in the board's circuit under PSpice for TI: "
                f"{circuit.frequency_hz / 1e3:.1f} kHz, switch {circuit.switch_low_v:.2f} to "
                f"{circuit.switch_high_v:.2f} V, inductor peak {circuit.inductor_peak_a:.2f} A under the "
                f"{circuit.load_a or source.current_a:.3g} A load; configured {source.frequency_hz / 1e3:g} kHz, "
                f"{source.voltage_swing_v:g} V, {source.current_a:g} A; rise time "
                f"{source.rise_time_ns:g} ns as configured."
            ),
        ))
        revalued.append(
            f"{source.name}: {circuit.frequency_hz / 1e3:.0f} kHz, {swing:.2f} V, "
            f"{circuit.inductor_peak_a:.2f} A (configured {source.frequency_hz / 1e3:g} kHz, "
            f"{source.voltage_swing_v:g} V, {source.current_a:g} A)"
        )
    if not revalued:
        return settings, []
    return replace(settings, sources=sources), revalued


def _encrypted_library(path):
    """Whether the model file at *path* is encrypted, so ngspice cannot run it."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace").upper()
    except OSError:
        return False
    return any(marker in text for marker in _ENCRYPTION_MARKERS)


class SpiceModelInventory:
    """Resolve model coverage without silently trusting unverified pin order."""

    def __init__(self, library_path, ngspice_path="", audit_directory=None, pspice_runner=None,
                 probe_cache_directory=None):
        self.root = Path(library_path) if library_path else None
        self.ngspice_path = Path(ngspice_path) if ngspice_path else None
        self.audit_directory = Path(audit_directory) if audit_directory else None
        self.pspice = pspice_runner if pspice_runner is not None and pspice_runner.available else None
        self.probe_cache = Path(probe_cache_directory) if probe_cache_directory else (
            Path(tempfile.gettempdir()) / "kipida-pspice-probes"
        )
        self.entries = self._read_catalog()

    def _probe_pspice(self, reference, model_path, model_name):
        """``(passed, log_path, note)``: the model switching in PSpice for TI.

        It runs in the manufacturer's own bench when the archive ships one,
        else in PSPICE_PROBE_BENCHES, and one of its switch nodes must switch
        in the last 20 us: a buck-boost has two, and TI's TPS55288 bench (5 V
        in, 9 V out) switches its boost side, SW2, not SW1. A first run lasts
        100 us, enough for TI's LMR51450 STEADY_STATE bench, which starts from
        its operating point (41 s); only a bench that has not switched by then
        runs its manufacturer's own duration (bench_profile, at most 5 ms):
        TI's TPS55288 Startup sets FAST_STARTUP=0 and took 1095 s. Each run is
        cached on the model, bench and circuit.
        """
        bench_path, nodes = manufacturer_bench(model_path, model_name)
        attempts = [(100e-6, 5e-9)]
        if bench_path is not None:
            profile = bench_profile(bench_path)
            if profile and profile[0] > 100e-6:
                stop_s = min(profile[0], 5e-3)
                # At most 200,000 steps: 25 ns still puts 100 steps in a
                # 400 kHz period.
                attempts.append((stop_s, max(profile[1] or 5e-9, stop_s / 200_000)))
            pins = subckt_pins(model_path, model_name)
            probes = []
            for pin, node in zip(pins, nodes):
                if pin.upper().startswith("SW") and node != "0" and node not in probes:
                    probes.append(node)
            bench_text = bench_path.read_text(encoding="latin-1", errors="replace")
            origin = f"the manufacturer's bench {bench_path.name}"
        elif model_name.upper() in PSPICE_PROBE_BENCHES:
            bench_text, probe = PSPICE_PROBE_BENCHES[model_name.upper()]
            probes = [probe]
            origin = "Ki-PIDA's minimal bench"
        else:
            return False, None, "No bench is available to run this model in PSpice."
        # PSpice for TI stopped at "RPC server unavailable" on five columns.
        probes = probes[:4]
        if not probes or self.audit_directory is None:
            return False, None, "The bench has no switch-node pin to probe."
        directory = self.audit_directory / "model-probes" / f"{reference}-pspice"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "bench.net").write_text(bench_text, encoding="latin-1")
        stat = Path(model_path).stat()
        passed, out_path, note = False, None, ""
        for stop_s, step_s in attempts:
            circuit = "\n".join([
                f"* Ki-PIDA PSpice probe of {model_name} in {origin}",
                f'.LIB "{Path(model_path)}"',
                '.lib "nom.lib"',
                f".TRAN 1n {stop_s:.6g} {max(0.0, stop_s - 20e-6):.6g} {step_s:.6g} SKIPBP",
                ".OPTIONS ADVCONV NUMDGT=8",
                ".OPTIONS ITL4=40",
                ".PRINT TRAN " + " ".join(f"V([{probe}])" for probe in probes),
                '.INC "bench.net"',
                ".END", "",
            ])
            netlist = directory / "probe.cir"
            netlist.write_text(circuit, encoding="ascii", errors="replace")
            key = hashlib.sha1(
                f"{Path(model_path).resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{bench_text}|{circuit}".encode(
                    "utf-8", "replace")
            ).hexdigest()
            cache = self.probe_cache / f"{key}.json"
            if cache.is_file():
                cached = json.loads(cache.read_text(encoding="utf-8"))
                passed, out_path, note = bool(cached["passed"]), None, cached["note"] + " (cached)"
                if passed:
                    break
                continue
            state, out_path, seconds = self.pspice.run(netlist)
            summaries = {}
            if state == "concluded":
                columns = read_pspice_print(out_path)
                for name, values in columns.items():
                    summary = switching_summary(columns.get("TIME", []), values) if name != "TIME" else None
                    if summary is not None:
                        summaries[name] = summary
            if not summaries:
                note = (f"PSpice for TI ran {origin} ({state}, {seconds:.0f} s) but the model did "
                        f"not switch on {', '.join(probes)} in the last 20 us of {stop_s * 1e3:.3g} ms.")
                passed = False
            else:
                column = max(summaries, key=lambda name: summaries[name][2] - summaries[name][1])
                frequency, low, high = summaries[column]
                note = (f"PSpice for TI ran {origin} in {seconds:.0f} s: {column} switched at "
                        f"{frequency / 1e3:.0f} kHz between {low:.2f} and {high:.2f} V. The bench is "
                        "the model's, not this board's circuit; the excitation stays parametric.")
                passed = True
            if state in ("concluded", "error"):
                # A timeout is the budget's, not the model's.
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({"passed": passed, "note": note}), encoding="utf-8")
            elif state == "timeout":
                note += " Raise the Phase 10 solver timeout to let it finish; the result is then cached."
                break
            if passed:
                break
        return passed, out_path, note

    @staticmethod
    def _verified_mapping(mpn):
        normalized = _normalized_part(mpn)
        return next((data for key, data in VERIFIED_SPICE_MAPPINGS.items()
                     if normalized.startswith(key) or key.startswith(normalized)), None)

    def _write_wrapper(self, reference, mapping):
        if self.audit_directory is None:
            return None
        directory = self.audit_directory / "model-wrappers"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{reference}_{mapping['wrapper_name']}.lib"
        path.write_text(mapping["wrapper"], encoding="utf-8")
        return path

    def _probe_ngspice(self, reference, model_path, mapping):
        if not self.ngspice_path or not self.ngspice_path.is_file():
            return "MAPPING_VERIFIED_NGSPICE_NOT_TESTED", None, "ngspice is unavailable."
        if self.audit_directory is None:
            return "MAPPING_VERIFIED_NGSPICE_NOT_TESTED", None, "No probe output directory is available."
        directory = self.audit_directory / "model-probes" / reference
        directory.mkdir(parents=True, exist_ok=True)
        netlist = directory / "compatibility.cir"
        log_path = directory / "compatibility.log"
        # TI transient libraries are authored for PSpice.  This local file is
        # confined to the probe directory and does not alter the user's global
        # ngspice configuration.
        (directory / ".spiceinit").write_text("set ngbehavior=ps\n", encoding="utf-8")
        model_name = mapping["model_name"]
        if model_name == "TPS562200_TRANS":
            circuit = [
                f'.include "{model_path}"',
                "VIN VIN 0 PWL(0 0 10u 5)",
                "VEN EN 0 PWL(0 0 20u 0 21u 5)",
                "CBST VBST SW 100n",
                "L1 SW OUT 3.3u", "COUT OUT 0 22u", "RLOAD OUT 0 11.2",
                "RFBT OUT VFB 34k", "RFBB VFB 0 10k",
                f"XCORE EN 0 SW VBST VFB VIN {model_name}",
                ".options method=gear reltol=0.01 abstol=1u vntol=1m",
                ".tran 100n 100u uic", ".print tran v(out) v(sw)", ".end",
            ]
        else:
            return "MAPPING_VERIFIED_NGSPICE_NOT_TESTED", None, "No safe minimal bench is defined."
        netlist.write_text("\n".join(["* Ki-PIDA minimal compatibility probe", *circuit, ""]), encoding="utf-8")
        try:
            completed = _run(
                [self.ngspice_path, "-b", "-o", log_path.name, netlist.name],
                timeout=60.0, cwd=directory,
            )
            log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
            combined = (completed.stdout + "\n" + completed.stderr + "\n" + log_text).lower()
            failed = completed.returncode != 0 or any(token in combined for token in (
                "fatal error", "unknown device type", "unknown parameter", "encrypted library",
            ))
            if failed:
                if "timestep too small" in combined or "transient op failed" in combined:
                    return "MAPPING_VERIFIED_NGSPICE_TRANSIENT_UNSTABLE", log_path, (
                        "The PSpice-compatibility parser accepted the model, but the minimal "
                        "startup transient did not converge; the model is not ready for analysis."
                    )
                return "MAPPING_VERIFIED_NGSPICE_PROBE_FAILED", log_path, (
                    "The minimal transient compatibility probe failed; see its log."
                )
            return "MAPPING_VERIFIED_NGSPICE_MINIMAL_PASS", log_path, (
                "A minimal 100 us startup transient parsed and ran; full converter accuracy/stability is not validated."
            )
        except Exception as exc:
            return "MAPPING_VERIFIED_NGSPICE_PROBE_FAILED", log_path, str(exc)

    def _read_catalog(self):
        catalog = self.root / "MODEL_CATALOG.csv" if self.root else None
        if not catalog or not catalog.is_file():
            return []
        try:
            with catalog.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
                return list(csv.DictReader(stream, delimiter=";"))
        except OSError:
            return []

    def _catalog_model_path(self, raw_path):
        if not self.root or not raw_path:
            return None
        cleaned = str(raw_path).replace("\\\\", "\\")
        candidate = Path(cleaned)
        candidates = [candidate] if candidate.is_absolute() else [
            self.root / candidate,
            self.root.parent / candidate,
        ]
        if candidate.parts and candidate.parts[0].upper() == "SPICE":
            candidates.insert(0, self.root.joinpath(*candidate.parts[1:]))
        return next((path for path in candidates if path.is_file()), None)

    def _matching_entry(self, mpn):
        target = _normalized_part(mpn)
        candidates = []
        for entry in self.entries:
            component = _normalized_part(entry.get("Component", ""))
            if component and (target.startswith(component) or component.startswith(target)):
                candidates.append((min(len(target), len(component)), entry))
        return max(candidates, default=(0, None), key=lambda item: item[0])[1]

    def _scan_model(self, mpn):
        if not self.root or not self.root.is_dir():
            return None, ""
        normalized = _normalized_part(mpn)
        family = re.match(r"[A-Z]+\d+", normalized)
        token = family.group(0) if family else normalized
        if len(token) < 5:
            return None, ""
        for path in sorted(self.root.rglob("*")):
            if path.suffix.lower() not in {".lib", ".mod", ".cir", ".spice"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in re.finditer(r"(?im)^\s*\.subckt\s+(\S+)", text):
                model_name = match.group(1)
                if token in _normalized_part(model_name):
                    return path, model_name
        return None, ""

    def audit(self, snapshot, sources):
        footprints = {item.reference.upper(): item for item in snapshot.footprints}
        results = []
        for source in sources:
            if not source.enabled or str(source.kind).upper() != "SWITCHING":
                continue
            reference = _source_component_reference(source)
            footprint = footprints.get(reference)
            mpn = str(getattr(footprint, "value", "") or "")
            if not reference or footprint is None:
                results.append(EMCSpiceModelAudit(
                    reference, mpn, source.name, "COMPONENT_NOT_IDENTIFIED",
                    notes="The switching source could not be linked to a populated PCB reference.",
                ))
                continue
            if not self.root or not self.root.is_dir():
                results.append(EMCSpiceModelAudit(
                    reference, mpn, source.name, "LIBRARY_UNAVAILABLE",
                    notes=f"Configured SPICE library is unavailable: {self.root or 'not configured'}",
                ))
                continue
            entry = self._matching_entry(mpn)
            model_path = self._catalog_model_path(entry.get("Location_or_source", "")) if entry else None
            model_name = str(entry.get("Model_or_action", "")) if entry else ""
            catalog_status = str(entry.get("SPICE_status", "")) if entry else ""
            notes = str(entry.get("Notes", "")) if entry else ""
            mapping = self._verified_mapping(mpn)
            wrapper_path = self._write_wrapper(reference, mapping) if mapping else None
            compatibility = "NOT_TESTED"
            probe_log = None
            if mapping and model_path is None:
                scanned_path, scanned_name = self._scan_model(mpn)
                if scanned_path is not None:
                    model_path, model_name = scanned_path, scanned_name
            if mapping and model_path is not None:
                if _encrypted_library(model_path):
                    status = compatibility = "MAPPING_VERIFIED_PSPICE_ONLY"
                    notes = (
                        "Pin mapping is verified, but the TI library contains $ENCRYPTED_LIB "
                        "and cannot be executed by ngspice."
                    )
                else:
                    status, probe_log, probe_note = self._probe_ngspice(reference, model_path, mapping)
                    compatibility = status
                    notes = (notes + " " + probe_note).strip()
            elif entry and catalog_status.upper() == "NON_APPLICABLE":
                status = "NOT_APPLICABLE"
            elif entry and "INCOMPATIBLE" in catalog_status.upper():
                status = "INCOMPATIBLE"
            elif model_path is not None:
                status = (
                    "AVAILABLE_VERIFIED_MAPPING"
                    if "ASSOCIE_VERIFIE" in catalog_status.upper()
                    else "AVAILABLE_REQUIRES_PIN_MAPPING"
                )
            else:
                scanned_path, scanned_name = self._scan_model(mpn)
                if scanned_path is not None:
                    model_path, model_name = scanned_path, scanned_name
                    status = "AVAILABLE_REQUIRES_PIN_MAPPING"
                    notes = notes or "Model found by .SUBCKT name; PCB-symbol pin mapping is not audited."
                else:
                    status = "MODEL_FILE_MISSING"
                    if entry:
                        notes = notes or "Catalog entry exists but its model file cannot be resolved."
            # A model found without a verified mapping read "available" even
            # encrypted: TI's LMR51450 (SNVMCB2) and TPS55288 (SLVMDD9, the
            # model TI gives for the TPS552882) run in PSpice 17.2+ only.
            if status.startswith("AVAILABLE_") and model_path is not None and _encrypted_library(model_path):
                status = compatibility = "AVAILABLE_PSPICE_ONLY"
                notes = (notes + " The library is encrypted and runs in PSpice only, "
                         "not in ngspice.").strip()
            # What ngspice cannot run, PSpice for TI may: an encrypted TI
            # library, or P02 Rev E's TPS562200 model, whose startup
            # transient did not converge in ngspice.
            if self.pspice is not None and model_path is not None and model_name and (
                status.endswith("PSPICE_ONLY") or "NGSPICE_TRANSIENT_UNSTABLE" in status
                or "NGSPICE_PROBE_FAILED" in status
            ):
                passed, pspice_log, pspice_note = self._probe_pspice(reference, model_path, model_name)
                status = ("MAPPING_VERIFIED_" if mapping else "") + (
                    "PSPICE_BENCH_PASS" if passed else "PSPICE_BENCH_FAILED"
                )
                probe_log = pspice_log or probe_log
                notes = (notes + " " + pspice_note).strip()
            results.append(EMCSpiceModelAudit(
                reference, mpn, source.name, status, model_name,
                str(model_path or ""), catalog_status, False,
                "PARAMETRIC_SOURCE",
                mapping["wrapper_name"] if mapping else "",
                str(wrapper_path or ""),
                ((mapping["symbol_order"] + " -> " + mapping["model_order"])
                 if mapping else ""),
                compatibility, str(probe_log or ""),
                ((notes + "; mapping source: " + mapping["source"]).strip("; ")
                 if mapping else notes),
            ))
        return results


def _evidence_position(finding):
    for evidence in finding.evidence:
        if evidence.x_mm is not None and evidence.y_mm is not None:
            return float(evidence.x_mm), float(evidence.y_mm)
    return None


# Coarsest grid step a full-wave region is meshed at to fit the cell limit;
# past it the region is skipped rather than meshed too coarsely to resolve
# its tracks.
PHASE10_COARSEST_RESOLUTION_MM = 1.0


def select_target_regions(snapshot, findings, settings):
    """Select source-linked emission regions before passive checklist findings."""
    severity_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
    full_wave_rank = {
        "SWITCHING": 0, "GROUND": 1, "RETURN_PATH": 1,
        "DIFFERENTIAL": 2, "CROSSTALK": 2, "BOARD_EDGE": 3,
        "SHIELDING": 3, "PDN": 4,
    }

    def linked_sources(finding):
        if not hasattr(settings, "_parent_sources"):
            return []
        finding_nets = set(finding.nets)
        return sorted({
            source.name for source in settings._parent_sources
            if source.enabled and (
                source.net_name in finding_nets
                or source.negative_net_name in finding_nets
            )
        })

    def hot_loop_bounds_mm(finding):
        """Bounding box of a SWITCHING finding's net copper, FETs and inductor.

        EMC-4, P02 Rev E: region_1 (SW-002) was the finding's evidence point
        +/- region_margin_mm, x 266.8-276.8 / y 74.3-84.3 -- it held L1 but
        neither Q10 (274.5, 85.6) nor Q11 (269.8, 66.1), 20.1 mm apart, so no
        hot-loop rule could see them together. The region is now the union
        of the switch node's own copper and every hot-loop footprint
        (SW-003's ``hot_loop_components``), not one point and a fixed pad.
        """
        if finding.category != "SWITCHING" or not hasattr(settings, "_parent_sources"):
            return None
        finding_nets = set(finding.nets)
        xs, ys = [], []
        for source in settings._parent_sources:
            if not source.enabled or source.net_name not in finding_nets:
                continue
            for x, y in hot_loop_components(snapshot, source.name, source.net_name).values():
                xs.append(x)
                ys.append(y)
            for track in snapshot.tracks_by_net.get(source.net_name, []):
                xs.extend((track.start[0], track.end[0]))
                ys.extend((track.start[1], track.end[1]))
            for geometry in snapshot.zones_by_net.get(source.net_name, {}).values():
                if geometry is not None and not geometry.is_empty:
                    minx, miny, maxx, maxy = geometry.bounds
                    xs.extend((minx, maxx))
                    ys.extend((miny, maxy))
        if len(xs) < 2:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    candidates = sorted(
        (finding for finding in findings if _evidence_position(finding) is not None),
        key=lambda item: (
            0 if linked_sources(item) else 1,
            full_wave_rank.get(item.category, 9),
            severity_rank.get(item.severity, 5),
            item.rule_id,
        ),
    )
    regions = []
    board = snapshot.bounds_mm
    margin = max(0.1, float(settings.region_margin_mm))
    for finding in candidates:
        x, y = _evidence_position(finding)
        if any(region.bounds_mm[0] <= x <= region.bounds_mm[2]
               and region.bounds_mm[1] <= y <= region.bounds_mm[3] for region in regions):
            continue
        bounds = (
            max(board[0], x - margin), max(board[1], y - margin),
            min(board[2], x + margin), min(board[3], y + margin),
        )
        loop_bounds = hot_loop_bounds_mm(finding)
        if loop_bounds is not None:
            bounds = (
                max(board[0], min(bounds[0], loop_bounds[0]) - margin),
                max(board[1], min(bounds[1], loop_bounds[1]) - margin),
                min(board[2], max(bounds[2], loop_bounds[2]) + margin),
                min(board[3], max(bounds[3], loop_bounds[3]) + margin),
            )
        width = max(bounds[2] - bounds[0], settings.mesh_resolution_mm)
        depth = max(bounds[3] - bounds[1], settings.mesh_resolution_mm)
        air_margin = max(3.0, 0.5 * max(width, depth))

        def cell_count(step):
            # Air on every side plus a conservative 2 mm PCB stack.
            return math.ceil((width + 2.0 * air_margin) / step) * math.ceil(
                (depth + 2.0 * air_margin) / step
            ) * math.ceil((2.0 + 2.0 * air_margin) / step)

        resolution = float(settings.mesh_resolution_mm)
        cells = cell_count(resolution)
        warnings = []
        limit = int(getattr(settings, "maximum_cells", 0) or 0)
        if limit > 0 and cells > limit:
            # Every region of P02 Rev E exceeded the 2,000,000-cell limit at
            # 0.25 mm (9,241,120 and 2,112,000 cells) and was skipped, so the
            # full-wave never ran on it. The region is meshed coarser instead,
            # up to PHASE10_COARSEST_RESOLUTION_MM, and says so.
            coarse = resolution * (cells / limit) ** (1.0 / 3.0)
            while cell_count(coarse) > limit and coarse <= PHASE10_COARSEST_RESOLUTION_MM:
                coarse *= 1.02
            if coarse <= PHASE10_COARSEST_RESOLUTION_MM:
                warnings.append(
                    f"Meshed at {coarse:.3g} mm instead of {resolution:g} mm to fit the "
                    f"{limit:,}-cell limit ({cells:,} cells at {resolution:g} mm)."
                )
                resolution, cells = coarse, cell_count(coarse)
        sources = linked_sources(finding)
        regions.append(EMCPhase10RegionResult(
            name=f"region_{len(regions) + 1}_{finding.rule_id.lower()}",
            status="SELECTED", bounds_mm=bounds, source_names=sources,
            finding_ids=[finding.rule_id], estimated_cells=cells,
            warnings=warnings, mesh_resolution_mm=resolution,
        ))
        if len(regions) >= max(0, int(settings.maximum_regions)):
            break
    return regions


def _polygon_coordinates(geometry):
    if geometry is None or getattr(geometry, "is_empty", True):
        return []
    polygons = list(getattr(geometry, "geoms", [geometry]))
    result = []
    for polygon in polygons:
        exterior = getattr(polygon, "exterior", None)
        if exterior is not None:
            result.append([[float(x), float(y)] for x, y in exterior.coords])
    return result


def serialize_region(snapshot, region, settings, sources, path, run_solver=False):
    xmin, ymin, xmax, ymax = region.bounds_mm
    def intersects_track(track):
        return not (max(track.start[0], track.end[0]) < xmin
                    or min(track.start[0], track.end[0]) > xmax
                    or max(track.start[1], track.end[1]) < ymin
                    or min(track.start[1], track.end[1]) > ymax)
    tracks = [track for track in snapshot.tracks if intersects_track(track)]
    track_payloads = []
    clipping_box = geometry_box(xmin, ymin, xmax, ymax) if geometry_box else None
    for track in tracks:
        if clipping_box is None or LineString is None:
            track_payloads.append(asdict(track))
            continue
        clipped = LineString([track.start, track.end]).intersection(clipping_box)
        pieces = list(getattr(clipped, "geoms", [clipped]))
        for piece in pieces:
            coordinates = list(getattr(piece, "coords", []))
            if len(coordinates) < 2:
                continue
            start, end = coordinates[0], coordinates[-1]
            track_payloads.append({
                "net_name": track.net_name,
                "start": [float(start[0]), float(start[1])],
                "end": [float(end[0]), float(end[1])],
                "width_mm": track.width_mm, "layer_id": track.layer_id,
                "length_mm": float(piece.length),
            })
    vias = [via for via in snapshot.vias if xmin <= via.position[0] <= xmax
            and ymin <= via.position[1] <= ymax]
    zones = []
    zone_geometries = []
    for net_name, layer_geometries in snapshot.zones_by_net.items():
        for layer_id, geometry in layer_geometries.items():
            clipped_geometry = (
                geometry.intersection(clipping_box)
                if clipping_box is not None and geometry is not None else geometry
            )
            for polygon in _polygon_coordinates(clipped_geometry):
                zones.append({"net_name": net_name, "layer_id": int(layer_id), "polygon": polygon})
            if clipped_geometry is not None and not getattr(clipped_geometry, "is_empty", True):
                zone_geometries.append((net_name, int(layer_id), clipped_geometry))

    source_ports = []
    for source in (source for source in sources if source.enabled):
        conductors = [(source.net_name, "SINGLE")]
        if str(source.kind).upper() == "DIFFERENTIAL" and source.negative_net_name:
            conductors = [
                (source.net_name, "POSITIVE"),
                (source.negative_net_name, "NEGATIVE"),
            ]
        for conductor_net, conductor_role in conductors:
            for track in track_payloads:
                if track["net_name"] != conductor_net:
                    continue
                x = (float(track["start"][0]) + float(track["end"][0])) / 2.0
                y = (float(track["start"][1]) + float(track["end"][1])) / 2.0
                source_ports.append({
                    "source_name": source.name, "net_name": conductor_net,
                    "conductor_role": conductor_role,
                    "x_mm": x, "y_mm": y, "layer_id": int(track["layer_id"]),
                    "geometry_source": "ROUTED_TRACK", "confidence": "HIGH",
                })
            for net_name, layer_id, geometry in zone_geometries:
                if net_name != conductor_net:
                    continue
                anchored = None
                if Point is not None:
                    for footprint in snapshot.footprints:
                        for pad_net, pad_x, pad_y in footprint.net_positions:
                            point = Point(float(pad_x), float(pad_y))
                            if pad_net == conductor_net and geometry.covers(point):
                                anchored = (float(pad_x), float(pad_y))
                                break
                        if anchored is not None:
                            break
                if anchored is None:
                    representative = geometry.representative_point()
                    anchored = (float(representative.x), float(representative.y))
                    geometry_source, confidence = "ZONE_REPRESENTATIVE", "MEDIUM"
                else:
                    geometry_source, confidence = "ZONE_PAD_ANCHORED", "HIGH"
                source_ports.append({
                    "source_name": source.name, "net_name": conductor_net,
                    "conductor_role": conductor_role,
                    "x_mm": anchored[0], "y_mm": anchored[1], "layer_id": layer_id,
                    "geometry_source": geometry_source, "confidence": confidence,
                })
    payload = {
        "schema": "KIPIDA_PHASE10_OPENEMS_3",
        "region": asdict(region),
        "frequency_start_hz": settings.frequency_start_hz,
        "frequency_stop_hz": settings.frequency_stop_hz,
        "mesh_resolution_mm": region.mesh_resolution_mm or settings.phase10.mesh_resolution_mm,
        "maximum_cells": settings.phase10.maximum_cells,
        "openems_max_timesteps": settings.phase10.openems_max_timesteps,
        "openems_end_criteria": settings.phase10.openems_end_criteria,
        "differential_excitation_mode": settings.phase10.differential_excitation_mode,
        "differential_leg_impedance_ohm": settings.phase10.differential_leg_impedance_ohm,
        "run_solver": bool(run_solver),
        "stackup": [asdict(layer) for layer in snapshot.stackup.layers],
        "tracks": track_payloads,
        "vias": [asdict(via) for via in vias],
        "zones": zones,
        "source_ports": source_ports,
        "reference_nets": list(settings.reference_net_names),
        "sources": [asdict(source) for source in sources if source.enabled],
    }
    path = Path(path)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


class VirtualEMIReceiver:
    """RBW/detector postprocessor that refuses limits for relative-only spectra."""

    def __init__(self, settings):
        self.settings = settings

    def process_relative(self, frequency_risks):
        points = []
        requested = self.settings.receiver_detector.upper()
        # No quasi-peak weighting is applied: the level is the bin's peak (or
        # its average), and the label says so rather than the requested
        # detector's name (EMC-8).
        detector = "AVERAGE_RELATIVE" if requested == "AVERAGE" else "PEAK_RELATIVE"
        rbw = max(float(self.settings.receiver_rbw_hz), 1.0)
        bins = {}
        for risk in frequency_risks:
            key = int(float(risk.frequency_hz) // rbw)
            bins.setdefault(key, []).append(risk)
        for key in sorted(bins):
            risks = bins[key]
            dominant = max(risks, key=lambda item: item.level_db)
            if requested == "AVERAGE":
                linear = sum(10.0 ** (item.level_db / 20.0) for item in risks) / len(risks)
                level = 20.0 * math.log10(max(linear, 1e-30))
            else:
                level = dominant.level_db
            points.append(EMCVirtualReceiverPoint(
                frequency_hz=dominant.frequency_hz, detector=detector,
                level_rel_db=level, limit_dbuv_m=None, margin_db=None,
                source_name=dominant.source_name,
                provenance="RELATIVE_RISK_ONLY_NO_COMPLIANCE_MARGIN",
            ))
        return points


class EMCPhase10Pipeline:
    """Orchestrate dependency checks, SPICE excitations and targeted openEMS jobs."""

    def __init__(
        self, snapshot, settings, analysis_result, board_file_path="",
        log_callback=None, cancellation_callback=None,
    ):
        self.snapshot = snapshot
        self.settings = settings
        self.analysis_result = analysis_result
        self.board_file_path = Path(board_file_path) if board_file_path else None
        self.log_callback = log_callback
        self.cancellation_callback = cancellation_callback
        self._circuits = None
        self._output = None

    def log(self, message):
        if self.log_callback:
            self.log_callback(message)

    def _output_directory(self):
        configured = self.settings.phase10.output_directory
        if configured:
            base = Path(configured)
        elif self.board_file_path:
            base = self.board_file_path.parent / "KiPIDA-results"
        else:
            base = Path.cwd() / "KiPIDA-results"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        directory = base / f"{stamp}-EMC-PHASE10"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _run_board_circuits(self, result, pspice_runner, output):
        """Run each switching source whose model PSpice runs in the saved board's circuit."""
        audits = {audit.source_name: audit for audit in result.spice_model_audit}
        runnable = [
            (source, audits[source.name]) for source in self.settings.sources
            if source.enabled and str(source.kind).upper() == "SWITCHING" and source.name in audits
            and audits[source.name].model_path and audits[source.name].model_name
            and str(audits[source.name].status).endswith(("PSPICE_BENCH_PASS", "NGSPICE_MINIMAL_PASS"))
        ]
        if not runnable:
            return
        if not (self.board_file_path and self.board_file_path.is_file()):
            self.log("[PHASE 10] Board circuits skipped: the board has not been saved to a file.")
            return
        try:
            board = read_board(self.board_file_path)
        except Exception as exc:
            self.log(f"[PHASE 10] Board circuits skipped: {exc}")
            return
        simulator = BoardCircuitSimulator(pspice_runner)
        rail_voltages = dict(getattr(self.snapshot, "rail_voltages", None) or {})
        tree = dict(getattr(self.snapshot, "converter_outputs", None) or {})
        for source, audit in runnable:
            if self.cancellation_callback and self.cancellation_callback():
                break
            self.log(f"[PHASE 10] Board circuit: {source.name} ({audit.component_ref}, {audit.model_name})")
            # The circuit draws the converter's output current as the power
            # tree sums it, not the entered source's: on P02 Rev E, U4's
            # circuit drew 4.36 A where the tree's loads, which the AC target
            # used, give 4.726 A.
            loaded = source
            converter = tree.get(str(audit.component_ref).upper())
            if converter is not None and converter.current_a > 0.0:
                loaded = replace(source, current_a=converter.current_a)
                if abs(converter.current_a - float(source.current_a)) > 1e-9:
                    self.log(
                        f"[PHASE 10] Board circuit {audit.component_ref} draws {converter.current_a:.3g} A, "
                        f"{converter.name}'s output in the power tree; the source is entered at "
                        f"{float(source.current_a):g} A."
                    )
            safe_name = "".join(char if char.isalnum() else "_" for char in source.name)
            circuit = simulator.run(
                board, loaded, audit.component_ref, audit.model_path, audit.model_name,
                rail_voltages, output / "board-circuits" / safe_name,
            )
            result.board_circuits.append(circuit)
            if circuit.status == "SWITCHING":
                # PSpice wrote an output only if it ran in this run; P02 Rev E's
                # cached circuits logged "(0 s)" as if they had run in no time.
                timing = f"{circuit.elapsed_seconds:.0f} s" if circuit.output_path else "read from the cache"
                self.log(
                    f"[PHASE 10] Board circuit {circuit.component_ref}: {circuit.frequency_hz / 1e3:.0f} kHz, "
                    f"switch {circuit.switch_low_v:.2f} to {circuit.switch_high_v:.2f} V, output "
                    f"{circuit.output_v:.3f} V, inductor {circuit.inductor_valley_a:.2f} to "
                    f"{circuit.inductor_peak_a:.2f} A after {circuit.simulated_s * 1e3:.3g} ms "
                    f"({timing})"
                    + "".join(f"; {item}" for item in circuit.disagreements)
                )
            else:
                self.log(
                    f"[PHASE 10] Board circuit {circuit.component_ref}: {circuit.status}"
                    + (f"; {circuit.notes[-1]}" if circuit.notes else "")
                )

    def simulate_circuits(self):
        """The tools, SPICE model audit and board circuits, as a partial result.

        EMCSolverEngine runs this before the EMC analysis, whose spectrum then
        takes each converter's frequency, swing and switched current from its
        board circuit; run() carries on from it, or starts with it.
        """
        if self._circuits is not None:
            return self._circuits
        started = time.perf_counter()
        phase = self.settings.phase10
        result = EMCPhase10Result(status="RUNNING")
        if not phase.enabled:
            result.status = "DISABLED"
            self._circuits = result
            return result
        self._output = self._output_directory()
        result.output_directory = str(self._output)
        result.tools = Phase10Toolchain(phase).detect()
        status_by_name = {item.name: item for item in result.tools}
        if phase.spice_enabled:
            ngspice = status_by_name["NGSPICE"].path if status_by_name["NGSPICE"].available else ""
            pspice = status_by_name["PSPICE"].path if status_by_name["PSPICE"].available else ""
            result.spice_model_audit = SpiceModelInventory(
                phase.spice_library_path, ngspice, self._output / "spice-model-audit",
                pspice_runner=PSpiceRunner(pspice, phase.solver_timeout_s) if pspice else None,
            ).audit(self.snapshot, self.settings.sources)
            for audit in result.spice_model_audit:
                self.log(
                    f"[PHASE 10] SPICE model {audit.component_ref or audit.source_name}: "
                    f"{audit.status}"
                )
            if pspice:
                self._run_board_circuits(
                    result, PSpiceRunner(pspice, phase.solver_timeout_s), self._output,
                )
        result.elapsed_seconds = time.perf_counter() - started
        self._circuits = result
        return result

    def run(self):
        phase = self.settings.phase10
        result = self.simulate_circuits()
        if result.status == "DISABLED":
            return result
        # The circuits' own time, whether they ran just now or before the
        # analysis, plus what follows.
        started = time.perf_counter() - result.elapsed_seconds
        output = self._output
        status_by_name = {item.name: item for item in result.tools}

        if phase.spice_enabled:
            ngspice = status_by_name["NGSPICE"].path if status_by_name["NGSPICE"].available else ""
            board_circuits = {item.source_name: item for item in result.board_circuits}
            runner = SpiceExcitationRunner(ngspice, phase.solver_timeout_s)
            spice_dir = output / "excitations"
            for source in self.settings.sources:
                # An ideal pulse does not improve a differential serial-link
                # model; those require IBIS/Touchstone or a measured waveform.
                if (source.enabled and source.frequency_hz > 0.0
                        and str(source.kind).upper() == "SWITCHING"):
                    circuit = board_circuits.get(source.name)
                    if circuit is not None and circuit.status == "SWITCHING":
                        result.excitations.append(board_circuit_excitation(source, circuit))
                        continue
                    self.log(f"[PHASE 10] SPICE excitation: {source.name}")
                    result.excitations.append(runner.run(source, spice_dir))

        # Attach sources transiently without changing persisted settings.
        phase._parent_sources = self.settings.sources
        try:
            result.regions = select_target_regions(
                self.snapshot, self.analysis_result.findings, phase,
            )
        finally:
            del phase._parent_sources

        backend = str(phase.full_wave_backend or "OPENEMS_LOCAL").upper()
        if backend == "OPENEMS":
            backend = "OPENEMS_LOCAL"
        if phase.full_wave_enabled and backend == "PALACE_REMOTE":
            if phase.auto_run_full_wave:
                palace_dir = output / "palace-remote"
                try:
                    connection = PalaceRemoteConnection.from_settings(phase)
                    palace_run = PalaceRemoteClient(
                        connection,
                        log_callback=lambda detail: self.log(f"[PALACE REMOTE] {detail}"),
                        cancellation_callback=self.cancellation_callback,
                    ).run_project(phase.palace_remote_config_path, palace_dir)
                    result.palace_runs.append(palace_run)
                    result.tools.append(EMCPhase10ToolStatus(
                        "PALACE_REMOTE",
                        palace_run.status not in {"FAILED", "VALIDATION_FAILED"},
                        palace_run.server,
                        palace_run.palace_version,
                        f"{palace_run.status}; artifacts: {palace_run.local_artifact_directory}",
                    ))
                except Exception as exc:
                    result.palace_runs.append(EMCPalaceRemoteRunResult(
                        status="FAILED",
                        server=(
                            f"{phase.palace_remote_username}@{phase.palace_remote_host}"
                            if phase.palace_remote_username else phase.palace_remote_host
                        ),
                        config_path=phase.palace_remote_config_path,
                        local_artifact_directory=str(palace_dir),
                        warnings=[str(exc)],
                    ))
                    result.tools.append(EMCPhase10ToolStatus(
                        "PALACE_REMOTE", False, phase.palace_remote_host,
                        detail=str(exc),
                    ))
                    self.log(f"[PALACE REMOTE] Failed: {exc}")
            else:
                result.tools.append(EMCPhase10ToolStatus(
                    "PALACE_REMOTE", False, phase.palace_remote_host,
                    detail="Configured; execution disabled for this run",
                ))
        elif phase.full_wave_enabled:
            openems = status_by_name["OPENEMS"]
            python_api = status_by_name["OPENEMS_PYTHON"]
            for region in result.regions:
                if self.cancellation_callback and self.cancellation_callback():
                    region.status = "CANCELLED"
                    break
                region_dir = output / region.name
                region_dir.mkdir(parents=True, exist_ok=True)
                region_sources = [
                    source for source in self.settings.sources
                    if source.name in set(region.source_names)
                ]
                solver_sources = [source for source in region_sources if _supports_openems_port(source)]
                solve_region = bool(phase.auto_run_full_wave and solver_sources)
                if phase.auto_run_full_wave and region_sources and not solver_sources:
                    region.warnings.append(
                        "Automatic solve skipped: the source does not provide the complete conductor "
                        "definition required by an implemented openEMS port."
                    )
                input_path = serialize_region(
                    self.snapshot, region, self.settings,
                    solver_sources if solve_region else region_sources,
                    region_dir / "input.json", run_solver=solve_region,
                )
                region.geometry_path = str(input_path)
                if region.estimated_cells > phase.maximum_cells:
                    region.status = "SKIPPED_CELL_LIMIT"
                    region.warnings.append(
                        f"Estimated {region.estimated_cells:,} cells exceeds limit {phase.maximum_cells:,}."
                    )
                    continue
                if not (openems.available and python_api.available):
                    region.status = "SKIPPED_TOOL_MISSING"
                    continue
                worker = Path(__file__).with_name("phase10_openems_worker.py")
                result_path = region_dir / "result.json"
                env = os.environ.copy()
                env["CSXCAD_INSTALL_PATH"] = phase.openems_root
                env["OPENEMS_INSTALL_PATH"] = phase.openems_root
                env["PATH"] = phase.openems_root + os.pathsep + env.get("PATH", "")
                self.log(f"[PHASE 10] openEMS {'solve' if solve_region else 'export'}: {region.name}")
                try:
                    returncode, execution_status, log_path = _run_monitored(
                        [python_api.path, worker, input_path, result_path],
                        timeout=phase.solver_timeout_s, cwd=region_dir, environment=env,
                        cancellation_callback=self.cancellation_callback,
                        progress_interval_s=phase.progress_interval_s,
                        # EMC-6: a "running 0 s / 600 s" line in export-only
                        # mode (auto_run_full_wave off) claimed a solve was
                        # under way when the worker only ever writes geometry
                        # and exits; only report progress while actually
                        # solving.
                        log_callback=(
                            (lambda detail, name=region.name: self.log(
                                f"[PHASE 10] {name}: {detail}"
                            )) if solve_region else None
                        ),
                    )
                    if execution_status == "CANCELLED":
                        region.status = "CANCELLED"
                        region.warnings.append("Solver cancelled by user; partial files were retained.")
                        self.log(f"[PHASE 10] Cancelled: {region.name}")
                        break
                    if execution_status == "TIMEOUT":
                        region.status = "TIMEOUT"
                        region.warnings.append(
                            f"Solver stopped after the configured {phase.solver_timeout_s:g} s timeout; "
                            f"diagnostic log: {log_path}"
                        )
                        self.log(f"[PHASE 10] Timeout: {region.name}")
                        continue
                    if result_path.is_file():
                        payload = json.loads(result_path.read_text(encoding="utf-8"))
                        region.status = payload.get("status", "FAILED")
                        region.solver_output_path = payload.get("solver_output_path", "")
                        region.maximum_e_v_m = payload.get("maximum_e_v_m")
                        region.maximum_h_a_m = payload.get("maximum_h_a_m")
                        region.elapsed_seconds = float(payload.get("elapsed_seconds", 0.0))
                        region.fields_extracted = bool(payload.get("fields_extracted", False))
                        region.port_net_name = payload.get("port_net_name", "")
                        region.port_net_names = list(payload.get("port_net_names", []))
                        region.port_count = int(payload.get("port_count", 0))
                        region.port_mode = payload.get("port_mode", "")
                        region.port_leg_impedance_ohm = float(
                            payload.get("port_leg_impedance_ohm", 0.0)
                        )
                        region.port_excitations = [
                            float(item) for item in payload.get("port_excitations", [])
                        ]
                        region.port_reference_layer_ids = [
                            int(item) for item in payload.get("port_reference_layer_ids", [])
                        ]
                        region.port_geometry_source = payload.get("port_geometry_source", "")
                        region.port_confidence = payload.get("port_confidence", "")
                        region.warnings.extend(payload.get("warnings", []))
                        if solve_region:
                            diagnostics = parse_openems_log(
                                log_path, phase.openems_max_timesteps,
                            )
                            region.solver_cells = diagnostics["cells"]
                            region.solver_iterations = diagnostics["iterations"]
                            region.solver_converged = diagnostics["converged"]
                            region.solver_energy_decay_db = diagnostics["energy_decay_db"]
                            region.unused_primitive_count = diagnostics["unused_primitives"]
                            region.warnings.extend(diagnostics["warnings"])
                            if diagnostics["converged"] is False:
                                region.status = "SOLVED_FIELDS_UNCALIBRATED_NOT_CONVERGED"
                            elif diagnostics["converged"] is None:
                                region.status = "SOLVED_FIELDS_UNCALIBRATED_CONVERGENCE_UNKNOWN"
                    else:
                        region.status = "FAILED"
                        region.warnings.append(
                            f"Worker returned {returncode} without a result; diagnostic log: {log_path}"
                        )
                except Exception as exc:
                    region.status = "FAILED"
                    region.warnings.append(str(exc))

        result.receiver_points = VirtualEMIReceiver(phase).process_relative(
            self.analysis_result.frequency_risks
        )
        result.limitations.extend([
            "Virtual-receiver levels derived from the risk spectrum remain relative; no regulatory margin is reported.",
            "Generated openEMS ports are geometry-derived approximations until a measured, IBIS, Touchstone, or manufacturer source model is supplied.",
            "Differential/common-mode openEMS excitation uses two lumped legs to a shared reference plane; it is not a de-embedded wave port.",
            "Ideal ngspice pulse validation is limited to switching converters; differential serial links are excluded until IBIS, Touchstone, or measured waveforms are available.",
            "Cables and enclosure require explicit geometry/material data; enabled flags do not invent missing objects.",
            "Accredited measurements remain required for compliance sign-off.",
        ])
        if result.board_circuits:
            result.limitations.append(
                "Board circuits take the converter's parts from the saved board with ideal capacitors "
                "(no ESR, ESL or DC-bias derating), external MOSFETs as switches, and a resistive load at "
                "the source's current; their switch edges are the model's behavioural ones, so the "
                "excitation keeps the configured rise time."
            )
        if backend == "PALACE_REMOTE":
            result.limitations.extend([
                "The selected Palace project directory is disclosed to the configured LAN server.",
                "Palace mesh attributes, materials, ports, and boundary conditions remain user-supplied and are not inferred by Ki-PIDA.",
                "Remote Palace outputs are engineering simulation evidence, not accredited EMC compliance measurements.",
            ])
        cancelled = any(item.status == "CANCELLED" for item in result.regions)
        cancelled = cancelled or any(
            item.status == "CANCELLED" for item in result.palace_runs
        )
        failed = any(
            item.status in {"FAILED", "TIMEOUT", "VALIDATION_FAILED"}
            for item in result.excitations + result.regions + result.palace_runs
        )
        # EMC-6: MODEL_FILE_MISSING and an unstable ngspice transient never
        # reached this check before -- a run reporting them (P02 Rev E:
        # U4/U14 MODEL_FILE_MISSING, U5 TRANSIENT_UNSTABLE) still said
        # COMPLETED.
        spice_issue = any(
            audit.status == "MODEL_FILE_MISSING" or "UNSTABLE" in str(audit.status or "").upper()
            for audit in result.spice_model_audit
        )
        warned = spice_issue or any(
            "NOT_CONVERGED" in item.status or "CONVERGENCE_UNKNOWN" in item.status
            for item in result.regions
        ) or any(
            item.status in {"NOT_SWITCHING", "NOT_STEADY", "FAILED", "TIMEOUT"} or item.disagreements
            for item in result.board_circuits
        )
        # A run whose regions only ever had geometry exported (every region
        # GEOMETRY_EXPORTED, no Palace run) never actually solved anything,
        # by the user's own auto_run_full_wave=False choice or because no
        # region had a supported source -- COMPLETED implies a finished
        # solve that did not happen.
        export_only = (
            bool(result.regions)
            and all(item.status == "GEOMETRY_EXPORTED" for item in result.regions)
            and not result.palace_runs
        )
        result.status = "CANCELLED" if cancelled else (
            "COMPLETED_WITH_ERRORS" if failed else (
                "EXPORT_ONLY" if export_only else (
                    "COMPLETED_WITH_WARNINGS" if warned else "COMPLETED"
                )
            )
        )
        result.elapsed_seconds = time.perf_counter() - started
        manifest = output / "phase10-result.json"
        manifest.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
        return result
