"""Resolve KiCad project paths for single-board and Zeo multi-board projects.

Supports three entry points:
  - a .kicad_pro file, resolved as that project and nothing else;
  - a .kicad_mbs file;
  - a directory, searched for one .kicad_mbs, then one .kicad_pro.

Inside the plugin the project comes from the host (``Project.path``), never
from a search (docs/plan-universalite.md, P1). A directory is for validation
and tests, and a directory holding several candidates is an error naming them:
the KiCad 10 demos ship ``ecc83`` with two projects, and picking the first one
the filesystem listed analysed whichever that happened to be.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .multiboard import CrossBoardNet, MbsModuleBlock, extract_cross_board_nets, parse_mbs


@dataclass
class BoardProject:
    pro_path: Path
    pcb_path: Path
    sch_root_path: Path
    sch_sheets: list[tuple[str, str]]  # [(uuid, name), ...]
    is_part_of_multiboard: bool
    sub_project_uuid: str
    notes: list[str] = field(default_factory=list)  # what could not be read or resolved, and why

    @property
    def status(self) -> str:
        """Which design files exist: COMPLETE, SCHEMATIC_ONLY, PCB_ONLY or NO_DESIGN_FILES."""
        has_pcb, has_sch = self.pcb_path.is_file(), self.sch_root_path.is_file()
        if has_pcb and has_sch:
            return COMPLETE
        if has_sch:
            return SCHEMATIC_ONLY
        return PCB_ONLY if has_pcb else NO_DESIGN_FILES


COMPLETE = "complete"
SCHEMATIC_ONLY = "schematic_only"
PCB_ONLY = "pcb_only"
NO_DESIGN_FILES = "no_design_files"


@dataclass
class MultiboardProject:
    mbs_path: Path
    container_pro_path: Path | None
    boards: list[BoardProject]
    cross_board_nets: list[CrossBoardNet]
    notes: list[str] = field(default_factory=list)


def resolve_project(path: Path) -> MultiboardProject | BoardProject:
    """Resolve *path* into a project descriptor.

    *path* may be a .kicad_pro, .kicad_mbs, or a directory.
    """
    path = Path(path).resolve()

    if path.is_dir():
        # Prefer .kicad_mbs (Zeo multi-board container)
        for pattern, resolve in (("*.kicad_mbs", _resolve_mbs), ("*.kicad_pro", _resolve_pro)):
            candidates = sorted(path.glob(pattern))
            if len(candidates) > 1:
                raise ValueError(
                    f"{path} holds {len(candidates)} {pattern[1:]} files; name the project to "
                    "analyse: " + ", ".join(candidate.name for candidate in candidates)
                )
            if candidates:
                return resolve(candidates[0])
        raise FileNotFoundError(f"No .kicad_pro or .kicad_mbs found in {path}")

    if path.suffix == ".kicad_mbs":
        return _resolve_mbs(path)

    if path.suffix == ".kicad_pro":
        return _resolve_pro(path)

    raise ValueError(f"Unsupported file type: {path.suffix}")


# ---------------------------------------------------------------------------
# Internal resolvers
# ---------------------------------------------------------------------------

def _load_pro(pro_path: Path, notes: list[str]) -> dict:
    """The project file's settings, or {} with the reason added to *notes*."""
    try:
        data = json.loads(pro_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        notes.append(f"{pro_path.name} could not be read ({type(exc).__name__}: {exc}); "
                     "its sheet list and multi-board settings are unknown.")
        return {}
    if not isinstance(data, dict):
        notes.append(f"{pro_path.name} is not a KiCad project file (no settings object).")
        return {}
    return data


def _resolve_pro(pro_path: Path) -> BoardProject:
    notes: list[str] = []
    pro_data = _load_pro(pro_path, notes)

    # PCB: same stem, same directory
    pcb_path = pro_path.with_suffix(".kicad_pcb")
    sch_path = pro_path.with_suffix(".kicad_sch")

    # Sheets from pro
    raw_sheets = pro_data.get("sheets", [])
    sch_sheets: list[tuple[str, str]] = []
    for entry in raw_sheets:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            sch_sheets.append((str(entry[0]), str(entry[1])))

    # Whether the file says it belongs to a Zeo container. Descriptive only:
    # Zeo writes a multi_board block into every project it saves, standalone
    # ones included, so its presence means nothing; and whether the multi-board
    # layer applies depends on the host, which the file cannot tell.
    multi_board_data = pro_data.get("multi_board", {})
    is_multi = isinstance(multi_board_data, dict) and bool(
        multi_board_data.get("container") or multi_board_data.get("container_project_relative_path")
    )

    # sub_project_uuid — look in meta or multi_board
    sub_uuid = ""
    meta = pro_data.get("meta", {})
    if isinstance(multi_board_data, dict):
        sub_uuid = multi_board_data.get("uuid", "")
    if not sub_uuid:
        sub_uuid = meta.get("uuid", "")

    # The project is the one named. It used to be swapped for its container
    # when multi_board held a "filename" key -- a key Zeo does not write, so it
    # never happened, and it must not: in KiCad the container is not opened at
    # all, and in Zeo the project context decides (plan section 3.1).
    return BoardProject(
        pro_path=pro_path,
        pcb_path=pcb_path,
        sch_root_path=sch_path,
        sch_sheets=sch_sheets,
        is_part_of_multiboard=is_multi,
        sub_project_uuid=sub_uuid,
        notes=notes,
    )


def _resolve_mbs(mbs_path: Path) -> MultiboardProject:
    mbs_path = mbs_path.resolve()
    mbs_dir = mbs_path.parent

    # Try to find a companion .kicad_pro
    container_pro: Path | None = None
    pro_stem = mbs_path.stem
    candidate = mbs_dir / f"{pro_stem}.kicad_pro"
    if candidate.exists():
        container_pro = candidate
    else:
        # Some Zeo projects use a different name
        pros = list(mbs_dir.glob("*.kicad_pro"))
        if pros:
            container_pro = pros[0]

    blocks: list[MbsModuleBlock] = parse_mbs(mbs_path)
    cross_nets: list[CrossBoardNet] = extract_cross_board_nets(blocks)

    boards: list[BoardProject] = []
    container_notes: list[str] = []
    seen_sub_paths: set[str] = set()
    for block in blocks:
        sub_rel = block.sub_project_path
        if sub_rel in seen_sub_paths:
            continue
        seen_sub_paths.add(sub_rel)
        sub_pro_path = (mbs_dir / sub_rel).resolve()
        if not sub_pro_path.exists():
            container_notes.append(f"Board project {sub_rel} named by {mbs_path.name} does not exist.")
            continue
        board_notes: list[str] = []
        pro_data = _load_pro(sub_pro_path, board_notes)
        stem = sub_pro_path.stem
        pcb_path = sub_pro_path.with_suffix(".kicad_pcb")
        sch_path = sub_pro_path.with_suffix(".kicad_sch")
        raw_sheets = pro_data.get("sheets", [])
        sch_sheets: list[tuple[str, str]] = [
            (str(e[0]), str(e[1]))
            for e in raw_sheets
            if isinstance(e, (list, tuple)) and len(e) >= 2
        ]
        boards.append(BoardProject(
            pro_path=sub_pro_path,
            pcb_path=pcb_path,
            sch_root_path=sch_path,
            sch_sheets=sch_sheets,
            is_part_of_multiboard=True,
            sub_project_uuid=block.sub_project_uuid,
            notes=board_notes,
        ))

    return MultiboardProject(
        mbs_path=mbs_path,
        container_pro_path=container_pro,
        boards=boards,
        cross_board_nets=cross_nets,
        notes=container_notes,
    )
