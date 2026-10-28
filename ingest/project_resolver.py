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

from .multiboard import CrossBoardNet


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
    """The container a .kicad_mbs belongs to: its boards and its validated links.

    Boards come from the container project's sub_projects and links from its
    cross_board_nets, checked against each board's PCB. They used to be
    derived from the drawing, pairing module-block pins by name across boards,
    which joined any two connectors that happened to share a pin label and
    missed any link whose ends were named differently.
    """
    from .zeo_container import cross_board_nets, listed_boards, read_zeo_container, validate_links, OK

    mbs_path = mbs_path.resolve()
    notes: list[str] = []
    container_pro = None
    for candidate in sorted(mbs_path.parent.glob("*.kicad_pro")):
        settings = _load_pro(candidate, notes).get("multi_board") or {}
        if isinstance(settings, dict) and settings.get("container") and (settings.get("mbs_file") or mbs_path.name) == mbs_path.name:
            container_pro = candidate
            break
    if container_pro is None:
        notes.append(f"No container project beside {mbs_path.name} names it; its boards and links are unknown.")
        return MultiboardProject(mbs_path, None, [], [], notes)

    boards, board_notes = listed_boards(container_pro, settings)
    zeo = validate_links(read_zeo_container(container_pro, boards))
    notes.extend(board_notes + zeo.notes + zeo.diff_pair_issues)
    notes.extend(f"link {link.name}: {endpoint.state}, {endpoint.detail}"
                 for link in zeo.links for endpoint in link.endpoints if endpoint.state != OK)
    return MultiboardProject(
        mbs_path=mbs_path,
        container_pro_path=container_pro,
        boards=boards,
        cross_board_nets=cross_board_nets(zeo),
        notes=notes,
    )
