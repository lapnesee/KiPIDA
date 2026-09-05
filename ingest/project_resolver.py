"""Resolve KiCad project paths for single-board and Zeo multi-board projects.

Supports three entry points:
  - a .kicad_pro file
  - a .kicad_mbs file
  - a directory (searches for .kicad_mbs first, then .kicad_pro)
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


@dataclass
class MultiboardProject:
    mbs_path: Path
    container_pro_path: Path | None
    boards: list[BoardProject]
    cross_board_nets: list[CrossBoardNet]


def resolve_project(path: Path) -> MultiboardProject | BoardProject:
    """Resolve *path* into a project descriptor.

    *path* may be a .kicad_pro, .kicad_mbs, or a directory.
    """
    path = Path(path).resolve()

    if path.is_dir():
        # Prefer .kicad_mbs (Zeo multi-board container)
        mbs_files = list(path.glob("*.kicad_mbs"))
        if mbs_files:
            return _resolve_mbs(mbs_files[0])
        pro_files = list(path.glob("*.kicad_pro"))
        if pro_files:
            return _resolve_pro(pro_files[0])
        raise FileNotFoundError(f"No .kicad_pro or .kicad_mbs found in {path}")

    if path.suffix == ".kicad_mbs":
        return _resolve_mbs(path)

    if path.suffix == ".kicad_pro":
        return _resolve_pro(path)

    raise ValueError(f"Unsupported file type: {path.suffix}")


# ---------------------------------------------------------------------------
# Internal resolvers
# ---------------------------------------------------------------------------

def _load_pro(pro_path: Path) -> dict:
    try:
        return json.loads(pro_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _resolve_pro(pro_path: Path) -> MultiboardProject | BoardProject:
    pro_data = _load_pro(pro_path)
    stem = pro_path.stem

    # PCB: same stem, same directory
    pcb_path = pro_path.with_suffix(".kicad_pcb")
    sch_path = pro_path.with_suffix(".kicad_sch")

    # Sheets from pro
    raw_sheets = pro_data.get("sheets", [])
    sch_sheets: list[tuple[str, str]] = []
    for entry in raw_sheets:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            sch_sheets.append((str(entry[0]), str(entry[1])))

    # Is this part of a multi-board project?
    multi_board_data = pro_data.get("multi_board", {})
    is_multi = bool(multi_board_data)

    # sub_project_uuid — look in meta or multi_board
    sub_uuid = ""
    meta = pro_data.get("meta", {})
    if isinstance(multi_board_data, dict):
        sub_uuid = multi_board_data.get("uuid", "")
    if not sub_uuid:
        sub_uuid = meta.get("uuid", "")

    bp = BoardProject(
        pro_path=pro_path,
        pcb_path=pcb_path,
        sch_root_path=sch_path,
        sch_sheets=sch_sheets,
        is_part_of_multiboard=is_multi,
        sub_project_uuid=sub_uuid,
    )

    # If it references a multi_board parent, try to resolve the full MBS
    if is_multi and isinstance(multi_board_data, dict):
        parent_rel = multi_board_data.get("filename", "")
        if parent_rel:
            parent_path = (pro_path.parent / parent_rel).resolve()
            if parent_path.exists() and parent_path.suffix == ".kicad_mbs":
                try:
                    return _resolve_mbs(parent_path)
                except Exception:
                    pass

    return bp


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
    seen_sub_paths: set[str] = set()
    for block in blocks:
        sub_rel = block.sub_project_path
        if sub_rel in seen_sub_paths:
            continue
        seen_sub_paths.add(sub_rel)
        sub_pro_path = (mbs_dir / sub_rel).resolve()
        if not sub_pro_path.exists():
            continue
        pro_data = _load_pro(sub_pro_path)
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
        ))

    return MultiboardProject(
        mbs_path=mbs_path,
        container_pro_path=container_pro,
        boards=boards,
        cross_board_nets=cross_nets,
    )
