"""Phase 10 multi-fidelity EMC orchestration.

The module deliberately keeps external solvers outside KiCad's Python process.
Every output records whether it is measured, circuit-derived, full-wave, or an
engineering estimate so that unavailable inputs never become silent defaults.
"""

from dataclasses import asdict
from datetime import datetime
import csv
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

try:
    from shapely.geometry import LineString, Point, box as geometry_box
except ImportError:  # pragma: no cover - normal Ki-PIDA runtime includes Shapely
    LineString = Point = geometry_box = None

try:
    from .models import (
        EMCPhase10ExcitationResult, EMCPhase10RegionResult, EMCPhase10Result,
        EMCPalaceRemoteRunResult,
        EMCPhase10ToolStatus, EMCSpiceModelAudit, EMCVirtualReceiverPoint,
    )
    from .palace_remote import PalaceRemoteClient, PalaceRemoteConnection
except (ImportError, ValueError):
    from models import (
        EMCPhase10ExcitationResult, EMCPhase10RegionResult, EMCPhase10Result,
        EMCPalaceRemoteRunResult,
        EMCPhase10ToolStatus, EMCSpiceModelAudit, EMCVirtualReceiverPoint,
    )
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


def parse_palace_log(path):
    """Extract algebraic and mesh-quality evidence from a Palace log."""
    path = Path(path)
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    element_matches = re.findall(
        r"^\s*elements\s+\d+\s+\d+\s+\d+\s+(\d+)\s*$", text, re.I | re.M,
    )
    convergence_matches = re.findall(
        r"(?:GMRES|CG|MINRES) solver converged in\s+(\d+)\s+iterations", text, re.I,
    )
    failed = bool(re.search(r"solver (?:did not converge|failed to converge)", text, re.I))
    kappa_matches = re.findall(
        r"^\s*kappa\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*$", text, re.I | re.M,
    )
    energy_matches = re.findall(
        r"Field energy E \(([-+0-9.eE]+) J\) \+ H \(([-+0-9.eE]+) J\)", text, re.I,
    )
    indicator_matches = re.findall(r"Indicator norm\s*=\s*([-+0-9.eE]+)", text, re.I)
    amr_matches = re.findall(
        r"Completed\s+(\d+)\s+iterations of adaptive mesh refinement", text, re.I,
    )
    h_matches = re.findall(
        r"^\s*h\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*$", text, re.I | re.M,
    )
    unknown_matches = re.findall(r"global unknowns\s*=\s*(\d+)", text, re.I)
    if not unknown_matches:
        unknown_matches = re.findall(
            r"Level 0 \(p = \d+\):\s*(\d+)\s+unknowns", text, re.I,
        )
    memory_matches = re.findall(
        r"Estimated current per-rank memory usage.*?Max\.\s*([0-9.]+)([KMGT])",
        text, re.I,
    )
    memory_gib = None
    if memory_matches:
        value, suffix = memory_matches[-1]
        scale = {"K": 1.0 / (1024.0 ** 2), "M": 1.0 / 1024.0, "G": 1.0, "T": 1024.0}
        memory_gib = float(value) * scale[suffix.upper()]
    return {
        "elements": int(element_matches[-1]) if element_matches else 0,
        "iterations": int(convergence_matches[-1]) if convergence_matches else 0,
        "converged": False if failed else (True if convergence_matches else None),
        "mesh_kappa_maximum": float(kappa_matches[-1][1]) if kappa_matches else None,
        "electric_energy_j": float(energy_matches[-1][0]) if energy_matches else None,
        "magnetic_energy_j": float(energy_matches[-1][1]) if energy_matches else None,
        "error_indicator_norm": float(indicator_matches[-1]) if indicator_matches else None,
        "amr_iterations": int(amr_matches[-1]) if amr_matches else 0,
        "mesh_h_minimum": float(h_matches[-1][0]) if h_matches else None,
        "mesh_h_maximum": float(h_matches[-1][1]) if h_matches else None,
        "unknowns": int(unknown_matches[-1]) if unknown_matches else 0,
        "estimated_memory_gib": memory_gib,
    }


def parse_palace_outputs(project_directory):
    """Read structured Palace CSV/ParaView evidence retained from the remote run."""
    postpro = Path(project_directory) / "postpro"
    result = {
        "frequency_hz": 0.0, "electric_energy_j": None, "magnetic_energy_j": None,
        "error_indicator_norm": None, "error_indicator_maximum": None,
        "field_output_count": 0,
    }

    def rows(path):
        if not path.is_file():
            return []
        with path.open("r", encoding="utf-8", errors="replace", newline="") as stream:
            return [[item.strip() for item in row] for row in csv.reader(stream) if row]

    domain = rows(postpro / "domain-E.csv")
    if len(domain) >= 2 and len(domain[-1]) >= 3:
        result["frequency_hz"] = float(domain[-1][0]) * 1.0e9
        result["electric_energy_j"] = float(domain[-1][1])
        result["magnetic_energy_j"] = float(domain[-1][2])
    indicators = rows(postpro / "error-indicators.csv")
    if len(indicators) >= 2 and len(indicators[-1]) >= 3:
        result["error_indicator_norm"] = float(indicators[-1][0])
        result["error_indicator_maximum"] = float(indicators[-1][2])
    result["field_output_count"] = sum(
        1 for path in postpro.rglob("*")
        if path.is_file() and path.suffix.lower() in {".pvd", ".pvtu", ".vtu"}
    ) if postpro.is_dir() else 0
    return result


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
        python = self._phase10_python()
        statuses = [
            self._status("NGSPICE", ngspice, ("--version",)),
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
                result.provenance = "PARAMETRIC_NGSPICE"
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


class SpiceModelInventory:
    """Resolve model coverage without silently trusting unverified pin order."""

    def __init__(self, library_path, ngspice_path="", audit_directory=None):
        self.root = Path(library_path) if library_path else None
        self.ngspice_path = Path(ngspice_path) if ngspice_path else None
        self.audit_directory = Path(audit_directory) if audit_directory else None
        self.entries = self._read_catalog()

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
                head = model_path.read_text(encoding="utf-8", errors="replace")[:4096]
                if "$ENCRYPTED_LIB" in head.upper():
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
        width = max(bounds[2] - bounds[0], settings.mesh_resolution_mm)
        depth = max(bounds[3] - bounds[1], settings.mesh_resolution_mm)
        air_margin = max(3.0, 0.5 * max(width, depth))
        # Include air on every side plus a conservative 2 mm PCB stack.
        cells = math.ceil((width + 2.0 * air_margin) / settings.mesh_resolution_mm) * math.ceil(
            (depth + 2.0 * air_margin) / settings.mesh_resolution_mm
        ) * math.ceil((2.0 + 2.0 * air_margin) / settings.mesh_resolution_mm)
        sources = linked_sources(finding)
        regions.append(EMCPhase10RegionResult(
            name=f"region_{len(regions) + 1}_{finding.rule_id.lower()}",
            status="SELECTED", bounds_mm=bounds, source_names=sources,
            finding_ids=[finding.rule_id], estimated_cells=cells,
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
        "mesh_resolution_mm": settings.phase10.mesh_resolution_mm,
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
        detector = self.settings.receiver_detector.upper()
        rbw = max(float(self.settings.receiver_rbw_hz), 1.0)
        bins = {}
        for risk in frequency_risks:
            key = int(float(risk.frequency_hz) // rbw)
            bins.setdefault(key, []).append(risk)
        for key in sorted(bins):
            risks = bins[key]
            dominant = max(risks, key=lambda item: item.level_db)
            if detector == "AVERAGE":
                linear = sum(10.0 ** (item.level_db / 20.0) for item in risks) / len(risks)
                level = 20.0 * math.log10(max(linear, 1e-30))
            else:
                level = dominant.level_db
            points.append(EMCVirtualReceiverPoint(
                frequency_hz=dominant.frequency_hz, detector=detector,
                level_dbuv_m=level, limit_dbuv_m=None, margin_db=None,
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

    def run(self):
        started = time.perf_counter()
        phase = self.settings.phase10
        result = EMCPhase10Result(status="RUNNING")
        if not phase.enabled:
            result.status = "DISABLED"
            return result
        output = self._output_directory()
        result.output_directory = str(output)
        toolchain = Phase10Toolchain(phase)
        result.tools = toolchain.detect()
        status_by_name = {item.name: item for item in result.tools}

        if phase.spice_enabled:
            ngspice = status_by_name["NGSPICE"].path if status_by_name["NGSPICE"].available else ""
            result.spice_model_audit = SpiceModelInventory(
                phase.spice_library_path, ngspice, output / "spice-model-audit",
            ).audit(self.snapshot, self.settings.sources)
            for audit in result.spice_model_audit:
                self.log(
                    f"[PHASE 10] SPICE model {audit.component_ref or audit.source_name}: "
                    f"{audit.status}"
                )
            runner = SpiceExcitationRunner(ngspice, phase.solver_timeout_s)
            spice_dir = output / "excitations"
            for source in self.settings.sources:
                # An ideal pulse does not improve a differential serial-link
                # model; those require IBIS/Touchstone or a measured waveform.
                if (source.enabled and source.frequency_hz > 0.0
                        and str(source.kind).upper() == "SWITCHING"):
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
                python_status = status_by_name.get("OPENEMS_PYTHON")
                gmsh_status = status_by_name.get("GMSH")
                python_path = getattr(python_status, "path", "")
                can_mesh = bool(
                    python_path and Path(python_path).is_file()
                    and gmsh_status is not None and gmsh_status.available
                )
                connection = PalaceRemoteConnection.from_settings(phase)
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
                    solver_sources = [
                        source for source in region_sources if _supports_openems_port(source)
                    ]
                    input_path = serialize_region(
                        self.snapshot, region, self.settings, solver_sources,
                        region_dir / "input.json", run_solver=bool(solver_sources),
                    )
                    region.geometry_path = str(input_path)
                    if not solver_sources:
                        region.status = "SKIPPED_SOURCE_INCOMPLETE"
                        region.warnings.append(
                            "Automatic Palace solve skipped: no complete routed switching or "
                            "differential source intersects this region."
                        )
                        continue
                    if region.estimated_cells > phase.maximum_cells:
                        region.status = "SKIPPED_CELL_LIMIT"
                        region.warnings.append(
                            f"Estimated {region.estimated_cells:,} cells exceeds limit "
                            f"{phase.maximum_cells:,}."
                        )
                        continue
                    if not can_mesh:
                        region.status = "SKIPPED_TOOL_MISSING"
                        region.warnings.append(
                            "The isolated Phase 10 Python/Gmsh runtime is required to build "
                            "the Palace region mesh."
                        )
                        continue
                    project_dir = region_dir / "palace-project"
                    build_result_path = region_dir / "palace-build-result.json"
                    worker = Path(__file__).with_name("phase10_palace_worker.py")
                    self.log(f"[PHASE 10] Palace mesh: {region.name}")
                    try:
                        built = _run(
                            [python_path, worker, input_path, project_dir, build_result_path],
                            timeout=min(max(60.0, phase.solver_timeout_s), 600.0),
                            cwd=region_dir,
                        )
                        (region_dir / "palace-mesh.log").write_text(
                            built.stdout + built.stderr, encoding="utf-8", errors="replace",
                        )
                        build_payload = (
                            json.loads(build_result_path.read_text(encoding="utf-8"))
                            if build_result_path.is_file() else {}
                        )
                        region.warnings.extend(build_payload.get("warnings", []))
                        if built.returncode != 0 or build_payload.get("status") != "PROJECT_GENERATED":
                            region.status = "MESH_FAILED"
                            if not build_payload.get("warnings"):
                                region.warnings.append("Gmsh did not generate a Palace project.")
                            continue
                        region.solver_cells = int(build_payload.get("mesh_elements", 0))
                        region.mesh_nodes = int(build_payload.get("mesh_nodes", 0))
                        region.requested_mesh_resolution_mm = float(
                            build_payload.get("mesh_resolution_mm", phase.mesh_resolution_mm)
                        )
                        region.mesh_characteristic_min_mm = float(
                            build_payload.get("mesh_characteristic_min_mm", 0.0)
                        )
                        region.mesh_characteristic_max_mm = float(
                            build_payload.get("mesh_characteristic_max_mm", 0.0)
                        )
                        region.estimated_palace_peak_memory_gib = float(
                            build_payload.get("estimated_palace_peak_memory_gib", 0.0)
                        )
                        region.omitted_short_track_count = int(
                            build_payload.get("omitted_short_track_count", 0)
                        )
                        region.requested_via_count = int(
                            build_payload.get("requested_via_count", 0)
                        )
                        region.modeled_via_count = int(
                            build_payload.get("modeled_via_count", 0)
                        )
                        region.via_model = str(build_payload.get("via_model", ""))
                        region.via_geometry_fallback = bool(
                            build_payload.get("via_geometry_fallback", False)
                        )
                        if region.solver_cells > phase.maximum_cells:
                            region.status = "SKIPPED_CELL_LIMIT"
                            region.warnings.append(
                                f"Generated {region.solver_cells:,} FEM elements exceeds limit "
                                f"{phase.maximum_cells:,}."
                            )
                            continue
                        config_path = Path(build_payload["config_path"])
                        palace_dir = region_dir / "palace-remote"
                        palace_run = PalaceRemoteClient(
                            connection,
                            log_callback=lambda detail, name=region.name: self.log(
                                f"[PALACE REMOTE] {name}: {detail}"
                            ),
                            cancellation_callback=self.cancellation_callback,
                        ).run_project(config_path, palace_dir)
                        result.palace_runs.append(palace_run)
                        region.status = (
                            "SOLVED_PALACE_REMOTE"
                            if palace_run.status == "SOLVED_REMOTE" else palace_run.status
                        )
                        region.solver_output_path = palace_run.local_artifact_directory
                        source = solver_sources[0]
                        differential = str(source.kind).upper() == "DIFFERENTIAL"
                        region.port_mode = (
                            "DIFFERENTIAL_CURRENT_DIPOLES" if differential else "CURRENT_DIPOLE"
                        )
                        region.port_count = int(build_payload.get("dipole_count", 0))
                        region.port_net_name = source.net_name
                        region.port_net_names = [source.net_name] + (
                            [source.negative_net_name] if differential else []
                        )
                        region.port_confidence = "ENGINEERING_APPROXIMATION"
                        region.port_geometry_source = "ROUTED_TRACK"
                        region.frequency_hz = float(build_payload.get("frequency_hz", 0.0))
                        region.harmonic_order = int(build_payload.get("harmonic_order", 0))
                        region.source_moment_a_m = float(
                            build_payload.get("source_moment_a_m", 0.0)
                        )
                        region.elapsed_seconds = (
                            float(build_payload.get("elapsed_seconds", 0.0))
                            + palace_run.elapsed_seconds
                        )
                        diagnostics = parse_palace_log(palace_dir / "palace-run.log")
                        region.solver_cells = diagnostics["elements"] or region.solver_cells
                        region.solver_iterations = diagnostics["iterations"]
                        region.solver_converged = diagnostics["converged"]
                        region.mesh_kappa_maximum = diagnostics["mesh_kappa_maximum"]
                        region.palace_mesh_h_minimum = diagnostics["mesh_h_minimum"]
                        region.palace_mesh_h_maximum = diagnostics["mesh_h_maximum"]
                        region.solver_unknowns = diagnostics["unknowns"]
                        region.solver_estimated_memory_gib = diagnostics[
                            "estimated_memory_gib"
                        ]
                        region.electric_energy_j = diagnostics["electric_energy_j"]
                        region.magnetic_energy_j = diagnostics["magnetic_energy_j"]
                        region.error_indicator_norm = diagnostics["error_indicator_norm"]
                        structured = parse_palace_outputs(palace_dir / "project")
                        region.frequency_hz = structured["frequency_hz"] or region.frequency_hz
                        region.electric_energy_j = (
                            structured["electric_energy_j"]
                            if structured["electric_energy_j"] is not None
                            else region.electric_energy_j
                        )
                        region.magnetic_energy_j = (
                            structured["magnetic_energy_j"]
                            if structured["magnetic_energy_j"] is not None
                            else region.magnetic_energy_j
                        )
                        region.error_indicator_norm = (
                            structured["error_indicator_norm"]
                            if structured["error_indicator_norm"] is not None
                            else region.error_indicator_norm
                        )
                        region.error_indicator_maximum = structured["error_indicator_maximum"]
                        region.field_output_count = structured["field_output_count"]
                        if (
                            region.source_moment_a_m > 0.0
                            and region.electric_energy_j is not None
                            and region.magnetic_energy_j is not None
                        ):
                            region.normalized_energy_j_per_a2_m2 = (
                                region.electric_energy_j + region.magnetic_energy_j
                            ) / (region.source_moment_a_m ** 2)
                        # One successful linear solve proves algebraic convergence only.
                        # Physical discretization remains unverified until an independent
                        # mesh-resolution comparison is available.
                        region.discretization_verified = False
                        if region.solver_converged is True:
                            region.status = (
                                "SOLVED_PALACE_REMOTE_GEOMETRY_APPROXIMATED_"
                                "DISCRETIZATION_UNVERIFIED"
                                if region.via_geometry_fallback else
                                "SOLVED_PALACE_REMOTE_DISCRETIZATION_UNVERIFIED"
                            )
                            region.warnings.append(
                                "Palace's linear solver converged, but this is a single-mesh "
                                "result; discretization convergence has not been verified."
                            )
                        elif region.solver_converged is False:
                            region.status = "SOLVED_PALACE_REMOTE_NOT_CONVERGED"
                        if (
                            region.mesh_kappa_maximum is not None
                            and region.mesh_kappa_maximum > 1000.0
                        ):
                            region.warnings.append(
                                f"Maximum mesh element condition metric kappa="
                                f"{region.mesh_kappa_maximum:.6g} is high; inspect/refine "
                                "sliver elements before quantitative field use."
                            )
                    except subprocess.TimeoutExpired:
                        region.status = "MESH_TIMEOUT"
                        region.warnings.append("Palace region meshing exceeded its bounded timeout.")
                    except Exception as exc:
                        region.status = "FAILED"
                        region.warnings.append(str(exc))
                        self.log(f"[PALACE REMOTE] {region.name} failed: {exc}")
                successful = sum(
                    item.status.startswith("SOLVED_PALACE_REMOTE") for item in result.regions
                )
                result.tools.append(EMCPhase10ToolStatus(
                    "PALACE_REMOTE", successful > 0, phase.palace_remote_host,
                    detail=(
                        f"{successful}/{len(result.regions)} targeted PCB region(s) solved; "
                        "generated FEM projects are retained with each region"
                    ),
                ))
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
                        log_callback=lambda detail, name=region.name: self.log(
                            f"[PHASE 10] {name}: {detail}"
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
            "Ideal ngspice pulse validation is limited to switching converters; differential serial links are excluded until IBIS, Touchstone, or measured waveforms are available.",
            "Cables and enclosure require explicit geometry/material data; enabled flags do not invent missing objects.",
            "Accredited measurements remain required for compliance sign-off.",
        ])
        if backend != "PALACE_REMOTE":
            result.limitations.extend([
                "Generated openEMS ports are geometry-derived approximations until a measured, IBIS, Touchstone, or manufacturer source model is supplied.",
                "Differential/common-mode openEMS excitation uses two lumped legs to a shared reference plane; it is not a de-embedded wave port.",
            ])
        if backend == "PALACE_REMOTE":
            result.limitations.extend([
                "Each generated targeted PCB-region Palace project is disclosed to the configured LAN server.",
                "Palace uses deterministic local routed-current-dipole source approximations; amplitudes are not calibrated emission levels.",
                "Dielectric plies are homogenized while copper elevations retain the configured stackup.",
                "Palace samples one representative harmonic per region; the complete 30 MHz-1 GHz spectrum is not solved.",
                "A single FEM mesh does not establish discretization convergence; compare at least two mesh resolutions before quantitative field use.",
                "Remote Palace outputs are engineering simulation evidence, not accredited EMC compliance measurements.",
            ])
        cancelled = any(item.status == "CANCELLED" for item in result.regions)
        cancelled = cancelled or any(
            item.status == "CANCELLED" for item in result.palace_runs
        )
        failed = any(
            item.status in {"FAILED", "TIMEOUT", "VALIDATION_FAILED"}
            or item.status.endswith("_FAILED")
            for item in result.excitations + result.regions + result.palace_runs
        )
        warned = any(
            "NOT_CONVERGED" in item.status or "CONVERGENCE_UNKNOWN" in item.status
            or "DISCRETIZATION_UNVERIFIED" in item.status
            or item.status.startswith("SKIPPED_")
            for item in result.regions
        )
        result.status = "CANCELLED" if cancelled else (
            "COMPLETED_WITH_ERRORS" if failed else (
                "COMPLETED_WITH_WARNINGS" if warned else "COMPLETED"
            )
        )
        result.elapsed_seconds = time.perf_counter() - started
        manifest = output / "phase10-result.json"
        manifest.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
        return result
