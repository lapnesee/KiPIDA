"""Machine-local compute preferences for Ki-PIDA.

Project files keep electrical and physical analysis settings.  Hardware
selection belongs to the workstation, so it is persisted separately.
"""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile

try:
    from .i18n import SYSTEM_LANGUAGE, normalize_language
except (ImportError, ValueError):
    from i18n import SYSTEM_LANGUAGE, normalize_language


RUNTIME_CONFIG_VERSION = "1.2"


@dataclass
class RuntimeComputeSettings:
    ui_language: str = SYSTEM_LANGUAGE
    backend: str = "AUTO"  # AUTO, CPU, CUDA
    cpu_multithread: bool = True
    cpu_threads: int = 0  # 0 = automatic
    cuda_enabled: bool = False
    cuda_device: int = 0
    cuda_min_nodes: int = 100000
    # Threshold for an analysis that solves the same-sized system many times
    # over -- an AC frequency sweep, above all. cuda_min_nodes is calibrated
    # for a single solve, where the upload and preconditioner setup must pay
    # for themselves once; a sweep amortises that fixed cost across every
    # frequency against a resident matrix, so the break-even node count is far
    # lower. Judging a sweep by the single-solve bar kept a 39,569-node AC
    # network on the CPU against a 100,000-node default.
    cuda_min_nodes_sweep: int = 10000
    memory_limit_gib: float = 0.0  # 0 = built-in conservative mesh limit
    solver_rtol: float = 1.0e-8
    solver_max_iterations: int = 5000
    # The GPU path is iterative where the CPU path is a direct factorisation,
    # so the two have different error profiles. Before trusting a GPU sweep,
    # solve its first frequency both ways and compare. One extra point out of
    # a hundred-odd costs nothing measurable and turns "as accurate as CPU"
    # from an assertion into a checked property.
    verify_gpu_accuracy: bool = True

    def normalized(self):
        requested_language = str(self.ui_language or SYSTEM_LANGUAGE).strip()
        self.ui_language = (
            SYSTEM_LANGUAGE if requested_language.upper() == SYSTEM_LANGUAGE
            else (normalize_language(requested_language) or SYSTEM_LANGUAGE)
        )
        self.backend = str(self.backend or "AUTO").upper()
        if self.backend not in {"AUTO", "CPU", "CUDA"}:
            self.backend = "AUTO"
        self.cpu_threads = max(0, int(self.cpu_threads))
        self.cuda_device = max(0, int(self.cuda_device))
        self.cuda_min_nodes = max(1, int(self.cuda_min_nodes))
        self.cuda_min_nodes_sweep = max(1, int(self.cuda_min_nodes_sweep))
        self.memory_limit_gib = min(256.0, max(0.0, float(self.memory_limit_gib)))
        self.solver_rtol = min(1.0e-2, max(1.0e-12, float(self.solver_rtol)))
        self.solver_max_iterations = max(10, int(self.solver_max_iterations))
        self.verify_gpu_accuracy = bool(self.verify_gpu_accuracy)
        return self


def system_memory_info():
    """Return physical RAM totals without adding a runtime dependency."""
    try:
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {"total_bytes": int(status.ullTotalPhys), "available_bytes": int(status.ullAvailPhys)}
    except (AttributeError, OSError):
        pass
    return {"total_bytes": 0, "available_bytes": 0}


def runtime_config_path():
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / ".config")
    return Path(base) / "KiPIDA" / "runtime.json"


def load_runtime_settings(path=None):
    target = Path(path) if path else runtime_config_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return RuntimeComputeSettings()
    values = data.get("compute", data) if isinstance(data, dict) else {}
    allowed = RuntimeComputeSettings.__dataclass_fields__
    return RuntimeComputeSettings(**{
        key: value for key, value in values.items() if key in allowed
    }).normalized()


def save_runtime_settings(settings, path=None):
    target = Path(path) if path else runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": RUNTIME_CONFIG_VERSION,
        "compute": asdict(settings.normalized()),
    }
    handle, temporary = tempfile.mkstemp(
        prefix="runtime-", suffix=".json", dir=str(target.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target
