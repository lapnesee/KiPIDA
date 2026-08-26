"""Machine-local compute preferences for Ki-PIDA.

Project files keep electrical and physical analysis settings.  Hardware
selection belongs to the workstation, so it is persisted separately.
"""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile


RUNTIME_CONFIG_VERSION = "1.0"


@dataclass
class RuntimeComputeSettings:
    backend: str = "AUTO"  # AUTO, CPU, CUDA
    cpu_multithread: bool = True
    cpu_threads: int = 0  # 0 = automatic
    cuda_enabled: bool = False
    cuda_device: int = 0
    cuda_min_nodes: int = 100000
    solver_rtol: float = 1.0e-8
    solver_max_iterations: int = 5000

    def normalized(self):
        self.backend = str(self.backend or "AUTO").upper()
        if self.backend not in {"AUTO", "CPU", "CUDA"}:
            self.backend = "AUTO"
        self.cpu_threads = max(0, int(self.cpu_threads))
        self.cuda_device = max(0, int(self.cuda_device))
        self.cuda_min_nodes = max(1, int(self.cuda_min_nodes))
        self.solver_rtol = min(1.0e-2, max(1.0e-12, float(self.solver_rtol)))
        self.solver_max_iterations = max(10, int(self.solver_max_iterations))
        return self


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
