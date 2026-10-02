"""Runtime inspection and optional CUDA environment installation helpers."""

from pathlib import Path
import re
import subprocess
import sys
import importlib.util

try:
    from .compute_backend import cuda_diagnostics
except (ImportError, ValueError):
    from compute_backend import cuda_diagnostics


# The build's identity lives in build_info, which a result imports without
# pulling compute_backend in; these names stay importable from here.
try:
    from .build_info import build_commit, plugin_version, source_fingerprint  # noqa: F401
except (ImportError, ValueError):
    from build_info import build_commit, plugin_version, source_fingerprint  # noqa: F401


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
