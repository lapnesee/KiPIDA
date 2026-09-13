"""Which application launched Ki-PIDA, and what that makes of the project.

docs/plan-universalite.md, section 3. The Zeo multi-board layer applies only
when Zeo launched the plugin: a project Zeo saved carries a ``multi_board``
block wherever it is opened, so the file cannot say it. Everything else is
single-board, and the context says why.

| host    | project named by the host                          | mode           |
| ------- | -------------------------------------------------- | -------------- |
| KiCad   | any, a container or a member included              | single-board   |
| Zeo     | a container (``multi_board.container``)            | Zeo container  |
| Zeo     | a board its container lists in ``sub_projects``     | Zeo member     |
| Zeo     | a board naming no container                        | single-board   |
| Zeo     | a board whose container is missing or omits it     | single-board   |
| unknown | any                                                | single-board   |

In a KiCad or unknown host neither the project's ``multi_board`` block nor any
container or ``.kicad_mbs`` is read.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .project_resolver import BoardProject, resolve_project

KICAD = "kicad"
ZEO = "zeo"
UNKNOWN = "unknown"

SINGLE_BOARD = "single_board"
ZEO_CONTAINER = "zeo_container"
ZEO_MEMBER = "zeo_member"

_ZEO_EXECUTABLES = ("Zeo.exe", "Zeo", "zeo")


@dataclass(frozen=True)
class HostDetection:
    host: str
    evidence: tuple[str, ...] = ()


def detect_host(client=None, environ=None, ancestry=None) -> HostDetection:
    """What the launching application says about itself, and the verdict drawn from it.

    No rule is chosen yet. The candidates (plan section 3.2) are the host
    binary directory reported over IPC, the chain of parent processes and the
    environment the host sets; which of them separates Zeo from KiCad is to be
    read off ``kipida_host_probe.log`` after the four launches phase 0 asks
    for. Until then the verdict is UNKNOWN, which the table maps to
    single-board mode: a Zeo launch loses the multi-board layer, and no KiCad
    launch can gain it.
    """
    try:
        from ..host_probe import host_environment_variable_names, process_ancestry
    except (ImportError, ValueError):
        from host_probe import host_environment_variable_names, process_ancestry

    evidence = []
    cli = None
    getter = getattr(client, "get_kicad_binary_path", None)
    if callable(getter):
        try:
            cli = getter("kicad-cli")
        except Exception as exc:  # the IPC call is evidence, not a requirement
            evidence.append(f"get_kicad_binary_path failed: {type(exc).__name__}")
    if cli:
        directory = Path(str(cli)).parent
        beside = [name for name in _ZEO_EXECUTABLES if (directory / name).is_file()]
        evidence.append(f"host kicad-cli: {cli}; Zeo executable beside it: {', '.join(beside) or 'none'}")
    chain = process_ancestry() if ancestry is None else ancestry
    evidence.append("process ancestry: " + (" <- ".join(chain) if chain else "unavailable"))
    names = host_environment_variable_names(environ)
    evidence.append("environment variables (names only): " + (", ".join(names) or "none"))
    evidence.append("no detection rule is chosen yet: it awaits the host probe log (plan phase 0.1)")
    return HostDetection(UNKNOWN, tuple(evidence))


@dataclass
class ProjectContext:
    host: HostDetection
    mode: str
    project: BoardProject            # the project the host named
    boards: list[BoardProject]       # the boards analysed: one, except in container mode
    container_path: Optional[Path] = None
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        label = {SINGLE_BOARD: "single-board", ZEO_CONTAINER: "Zeo container", ZEO_MEMBER: "Zeo member"}[self.mode]
        text = (f"host {self.host.host}, {label} mode, project {self.project.pro_path.name} "
                f"({self.project.status})")
        return text + ("; " + "; ".join(self.notes) if self.notes else "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host.host,
            "host_evidence": list(self.host.evidence),
            "mode": self.mode,
            "project": str(self.project.pro_path),
            "status": self.project.status,
            "boards": [str(board.pro_path) for board in self.boards],
            "container": str(self.container_path) if self.container_path else "",
            "notes": list(self.notes),
        }


def build_project_context(project_path, detection: HostDetection) -> ProjectContext:
    """Apply the table above to the .kicad_pro the host named."""
    project = resolve_project(Path(project_path))
    if not isinstance(project, BoardProject):
        raise ValueError(f"{project_path} is not a .kicad_pro: the host names a project file")

    def single(*notes):
        return ProjectContext(detection, SINGLE_BOARD, project, [project], notes=list(notes))

    if detection.host == KICAD:
        return single()
    if detection.host != ZEO:
        return single("host detection inconclusive, so the Zeo multi-board layer is not applied")

    settings = _multi_board(project.pro_path)
    if settings.get("container"):
        return _container_context(detection, project, project.pro_path, settings)
    relative = str(settings.get("container_project_relative_path") or "")
    if not relative:
        return single("Zeo, standalone project")

    container_path = (project.pro_path.parent / relative).resolve()
    container_settings = _multi_board(container_path) if container_path.is_file() else {}
    if not container_settings.get("container"):
        reason = "does not exist" if not container_path.is_file() else "is not a Zeo container"
        return single(f"orphan board: its container {relative} {reason}")
    entry = _sub_project_entry(container_path, container_settings, project.pro_path)
    if entry is None:
        return single(f"orphan board: its container {container_path.name} does not list it")
    project.sub_project_uuid = str(entry.get("uuid") or project.sub_project_uuid)
    return ProjectContext(detection, ZEO_MEMBER, project, [project], container_path=container_path)


def _container_context(detection, project, container_path, settings) -> ProjectContext:
    boards, notes = [], []
    for entry in settings.get("sub_projects") or ():
        path = (container_path.parent / str(entry.get("path") or "")).resolve()
        if not path.is_file():
            notes.append(f"board {entry.get('name') or entry.get('path')} listed by the container does not exist")
            continue
        board = resolve_project(path)
        board.sub_project_uuid = str(entry.get("uuid") or board.sub_project_uuid)
        boards.append(board)
    return ProjectContext(detection, ZEO_CONTAINER, project, boards, container_path=container_path, notes=notes)


def _multi_board(pro_path: Path) -> dict:
    try:
        data = json.loads(Path(pro_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    settings = data.get("multi_board") if isinstance(data, dict) else None
    return settings if isinstance(settings, dict) else {}


def _sub_project_entry(container_path: Path, settings: dict, pro_path: Path):
    wanted = os.path.normcase(str(Path(pro_path).resolve()))
    for entry in settings.get("sub_projects") or ():
        candidate = (container_path.parent / str(entry.get("path") or "")).resolve()
        if os.path.normcase(str(candidate)) == wanted:
            return entry
    return None
