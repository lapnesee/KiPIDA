"""The content of a Zeo multi-board container, once the project context has activated it.

A container's .kicad_pro holds, under ``multi_board``, the boards
(``sub_projects``: name, path, uuid) and the links Zeo has already resolved
between them (``cross_board_nets``: a name, and endpoints naming a board, a
component, a pin and the pin's net). These are authorities: links are not
re-derived from the ``.kicad_mbs`` drawing, and boards are not paired by net
name (docs/plan-universalite.md, decisions 5 and section 3.3).

What Zeo resolved can still be out of date with a board edited since, so each
endpoint is checked against its board's PCB:

* ``ok`` -- the pad exists and carries the net the link recorded;
* ``stale`` -- the pad exists on another net;
* ``missing`` -- the board, its PCB, the component or the pad is not there.

A net is qualified by its board, ``(board uuid, net name)``: the same name on
two boards is two nets until a link joins them, and two names can be one
system net. System nets are the unions of qualified nets through links.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .multiboard import CrossBoardNet, MbsModuleBlock, parse_mbs
from .project_resolver import BoardProject, resolve_project

OK = "ok"
STALE = "stale"
MISSING = "missing"

# multi_board keys this module interprets; the rest is kept as written.
_INTERPRETED = {"container", "container_project_relative_path", "sub_projects", "cross_board_nets",
                "cross_board_diff_pairs", "mbs_file"}


@dataclass
class LinkEndpoint:
    board_uuid: str
    component: str
    pin: str
    pin_name: str
    state: str = ""
    net: str = ""        # the pad's net on the board's PCB, when the pad was found
    detail: str = ""


@dataclass
class CrossBoardLink:
    name: str
    uuid: str
    endpoints: list[LinkEndpoint]

    @property
    def state(self) -> str:
        states = {endpoint.state for endpoint in self.endpoints}
        return MISSING if MISSING in states else STALE if STALE in states else OK if states == {OK} else ""


@dataclass
class ZeoContainer:
    path: Path
    boards: dict[str, BoardProject]                   # by sub-project uuid
    links: list[CrossBoardLink]
    diff_pairs: list[tuple[str, str]]
    module_blocks: dict[str, list[MbsModuleBlock]] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)          # multi_board fields not interpreted
    notes: list[str] = field(default_factory=list)
    system_nets: list[set[tuple[str, str]]] = field(default_factory=list)  # after validate_links
    diff_pair_issues: list[str] = field(default_factory=list)              # after validate_links


def listed_boards(container_path: Path, settings: dict):
    """The boards a container lists in sub_projects, with their uuids, and notes on those that do not exist."""
    boards, notes = [], []
    for entry in settings.get("sub_projects") or ():
        path = (Path(container_path).parent / str(entry.get("path") or "")).resolve()
        if not path.is_file():
            notes.append(f"board {entry.get('name') or entry.get('path')} listed by the container does not exist")
            continue
        board = resolve_project(path)
        board.sub_project_uuid = str(entry.get("uuid") or board.sub_project_uuid)
        boards.append(board)
    return boards, notes


def cross_board_nets(container: ZeoContainer) -> list[CrossBoardNet]:
    """The system nets of a validated container, each named by the links that join it."""
    names_by_member: dict[tuple[str, str], list[str]] = {}
    for link in container.links:
        for endpoint in link.endpoints:
            if endpoint.net:
                names_by_member.setdefault((endpoint.board_uuid, endpoint.net), []).append(link.name)
    nets = []
    for members in container.system_nets:
        ordered = sorted(members)
        names = sorted({name for member in ordered for name in names_by_member.get(member, ())})
        nets.append(CrossBoardNet(
            net_name=" / ".join(names) or ordered[0][1],
            boards=sorted({uuid for uuid, _net in ordered}),
            members=ordered,
        ))
    return nets


def read_zeo_container(container_path, boards: list[BoardProject]) -> ZeoContainer:
    """Read the container's declarations. Nothing is checked against a PCB yet."""
    container_path = Path(container_path)
    notes: list[str] = []
    settings = json.loads(container_path.read_text(encoding="utf-8")).get("multi_board") or {}

    links = []
    for entry in settings.get("cross_board_nets") or ():
        endpoints = [
            LinkEndpoint(str(point.get("sub_project_uuid", "")), str(point.get("component", "")),
                         str(point.get("pin", "")), str(point.get("pin_name", "")))
            for point in entry.get("endpoints") or ()
        ]
        links.append(CrossBoardLink(str(entry.get("name", "")), str(entry.get("uuid", "")), endpoints))
    diff_pairs = [(str(pair.get("a", "")), str(pair.get("b", ""))) for pair in settings.get("cross_board_diff_pairs") or ()]

    blocks: dict[str, list[MbsModuleBlock]] = {}
    mbs_name = str(settings.get("mbs_file") or "")
    if mbs_name:
        mbs_path = container_path.parent / mbs_name
        try:
            for block in parse_mbs(mbs_path):
                blocks.setdefault(block.sub_project_uuid, []).append(block)
        except (OSError, UnicodeDecodeError) as exc:
            notes.append(f"{mbs_name} could not be read ({type(exc).__name__}); its module blocks are not attached")

    return ZeoContainer(
        path=container_path,
        boards={board.sub_project_uuid: board for board in boards if board.sub_project_uuid},
        links=links,
        diff_pairs=diff_pairs,
        module_blocks=blocks,
        raw={key: value for key, value in settings.items() if key not in _INTERPRETED},
        notes=notes,
    )


def validate_links(container: ZeoContainer, read_board: Optional[Callable] = None) -> ZeoContainer:
    """Check every endpoint against its board's PCB, then join qualified nets into system nets."""
    if read_board is None:
        from .board_reader import read_board
    parsed: dict[str, object] = {}

    def board_of(uuid):
        if uuid not in parsed:
            project = container.boards.get(uuid)
            if project is None:
                parsed[uuid] = "is not a board of the container"
            elif not project.pcb_path.is_file():
                parsed[uuid] = f"has no PCB ({project.pcb_path.name})"
            else:
                try:
                    parsed[uuid] = read_board(project.pcb_path)
                except (OSError, UnicodeDecodeError, ValueError) as exc:
                    parsed[uuid] = f"PCB could not be read ({type(exc).__name__}: {exc})"
        return parsed[uuid]

    for link in container.links:
        for endpoint in link.endpoints:
            board = board_of(endpoint.board_uuid)
            if isinstance(board, str):
                endpoint.state, endpoint.detail = MISSING, f"board {endpoint.board_uuid} {board}"
                continue
            footprints = [fp for fp in board.footprints if fp.reference == endpoint.component]
            pads = [pad for fp in footprints for pad in fp.pads if str(pad.number) == endpoint.pin]
            if not footprints:
                endpoint.state, endpoint.detail = MISSING, f"{endpoint.component} is not on the board"
            elif not pads:
                endpoint.state, endpoint.detail = MISSING, f"{endpoint.component} has no pad {endpoint.pin}"
            else:
                endpoint.net = pads[0].net_name
                if endpoint.net == endpoint.pin_name:
                    endpoint.state = OK
                else:
                    endpoint.state = STALE
                    endpoint.detail = (f"{endpoint.component} pad {endpoint.pin} is on {endpoint.net or 'no net'}, "
                                       f"not {endpoint.pin_name}")

    container.system_nets = _system_nets(container.links)
    link_names = {link.name for link in container.links}
    board_nets = {net for board in parsed.values() if not isinstance(board, str) for net in board.all_net_names}
    container.diff_pair_issues = []
    for a, b in container.diff_pairs:
        unknown = [name for name in (a, b) if name not in link_names]
        if unknown:
            nowhere = [name for name in unknown if name not in board_nets]
            container.diff_pair_issues.append(
                f"differential pair {a} / {b}: {' and '.join(unknown)} "
                + ("is no cross-board link" if len(unknown) == 1 else "are no cross-board links")
                + (f", nor a net of any board" if len(nowhere) == len(unknown) else
                   f"; {' and '.join(nowhere)} is no net of any board" if nowhere else "")
            )
    return container


def _system_nets(links: list[CrossBoardLink]) -> list[set[tuple[str, str]]]:
    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(item):
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for link in links:
        members = [(endpoint.board_uuid, endpoint.net) for endpoint in link.endpoints if endpoint.net]
        for member in members:
            find(member)
        for other in members[1:]:
            parent[find(other)] = find(members[0])
    groups: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for item in parent:
        groups.setdefault(find(item), set()).add(item)
    return sorted(groups.values(), key=lambda group: sorted(group))
