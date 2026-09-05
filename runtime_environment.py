"""Runtime inspection and optional CUDA environment installation helpers."""

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import importlib.util

try:
    from .compute_backend import cuda_diagnostics
except (ImportError, ValueError):
    from compute_backend import cuda_diagnostics


def plugin_version(plugin_root=None):
    root = Path(plugin_root) if plugin_root else Path(__file__).resolve().parent
    try:
        return str(json.loads((root / "plugin.json").read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"


def source_fingerprint(plugin_root=None, length=8):
    """Short hash of the plugin's Python sources, or "" if it cannot be read.

    plugin.json carries a release number, which is bumped by hand at release
    time. Every change between two releases therefore reports the same string,
    so the version display cannot answer the question that actually comes up
    when testing: is the copy in the plugin folder the one I just built?

    Working that out from behaviour costs real time -- it took cross-checking a
    solver iteration count, a residual curve and the presence of one log line
    to establish that a deployed copy predated five commits.

    Hashing the sources rather than asking git keeps this working for a folder
    that was copied rather than cloned, and rather than stamping mtimes, which
    a copy rewrites.

    Line endings are normalised before hashing. The first version hashed raw
    bytes, so a CRLF checkout could never match an LF one even when every line
    of code was identical -- and on Windows git converts on checkout by
    default. That made the fingerprint report a difference between two copies
    of the same commit, which is precisely the false alarm it exists to
    prevent, and it did mislead once.

    One blind spot remains, and it is not small: this measures the sources on
    disk, not the module that Python actually imported. With flat top-level
    imports, a second plugin directory earlier on sys.path can supply a module
    while this reports the directory the entry point lives in. See
    ``imported_module_origins``.
    """
    root = Path(plugin_root) if plugin_root else Path(__file__).resolve().parent
    skip = {".runtime", ".claude", ".git", "__pycache__", "tests", "validation"}
    digest = hashlib.sha256()
    try:
        for path in sorted(root.rglob("*.py")):
            if skip.intersection(path.relative_to(root).parts):
                continue
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    except (OSError, ValueError):
        return ""
    return digest.hexdigest()[:length]


def imported_module_origins(names, plugin_root=None):
    """Report modules imported from outside *plugin_root*, as "name -> path".

    The fingerprint answers "what is deployed". This answers "what is running",
    which is the question that actually bites: the plugin imports flat
    top-level names, so any other directory earlier on sys.path holding a file
    of the same name supplies that module instead -- while the entry point
    still reports its own folder, because that is all it knows.

    Only strangers are returned. A module living under the plugin root is the
    expected case and saying so on every start would bury the one line that
    matters.
    """
    import sys

    root = Path(plugin_root) if plugin_root else Path(__file__).resolve().parent
    try:
        root = root.resolve()
    except OSError:
        return []
    strangers = []
    for name in names:
        module = sys.modules.get(name)
        origin = getattr(module, "__file__", None)
        if not origin:
            continue
        try:
            resolved = Path(origin).resolve()
            resolved.relative_to(root)
        except ValueError:
            strangers.append(f"{name} -> {origin}")
        except OSError:
            continue
    return strangers


def runtime_summary():
    diagnostics = cuda_diagnostics()
    return {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "pypardiso": importlib.util.find_spec("pypardiso") is not None,
        "threadpoolctl": importlib.util.find_spec("threadpoolctl") is not None,
        "cuda": diagnostics,
    }


def recommended_cupy_package():
    """Choose a pre-built wheel family from the driver-advertised CUDA level."""
    try:
        completed = subprocess.run(
            ["nvidia-smi"],
            capture_output=True, text=True, timeout=10, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        match = re.search(r"CUDA Version:\s*(\d+)", completed.stdout)
        if match and int(match.group(1)) >= 13:
            return "cupy-cuda13x[ctk]"
    except (OSError, subprocess.SubprocessError):
        pass
    return "cupy-cuda12x[ctk]"


def install_cuda_environment(output_callback=None):
    package = recommended_cupy_package()
    command = [sys.executable, "-m", "pip", "install", "--upgrade", package]
    completed = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for line in completed.stdout:
        if output_callback:
            output_callback(line.rstrip())
    return completed.wait(), command
