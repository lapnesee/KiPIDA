"""Parse Zeo multi-board schematic (.kicad_mbs) files.

A .kicad_mbs file is a standard KiCad S-expression schematic that adds
``module_block`` elements encoding cross-board connector topology.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .sexpr import parse, find, find_all, get_str


@dataclass
class MbsPin:
    number: str
    name: str
    electrical_type: str


@dataclass
class MbsModuleBlock:
    sub_project_path: str
    sub_project_uuid: str
    component: str
    mbs_reference: str
    name: str
    pins: list[MbsPin] = field(default_factory=list)


@dataclass
class CrossBoardNet:
    """One system net: local nets of several boards that a container's links join.

    The same name on two boards is not enough; only a declared link joins
    nets, and it can join nets named differently on each board.
    """
    net_name: str
    boards: list[str]  # sub_project_uuid list
    members: list[tuple[str, str]] = field(default_factory=list)  # (sub_project_uuid, local net name)


def parse_mbs(mbs_path: Path) -> list[MbsModuleBlock]:
    """Parse a .kicad_mbs file and return all module_block entries."""
    text = Path(mbs_path).read_text(encoding="utf-8")
    roots = parse(text)
    if not roots:
        return []
    # The top-level is (kicad_sch ...) — grab it
    root = roots[0] if isinstance(roots[0], list) else None
    if root is None:
        return []

    blocks: list[MbsModuleBlock] = []
    for node in find_all(root, "module_block"):
        sub_proj = get_str(node, "sub_project")
        sub_uuid = get_str(node, "sub_project_uuid")
        component = get_str(node, "component")
        mbs_ref = get_str(node, "mbs_reference")
        name = get_str(node, "name")

        pins: list[MbsPin] = []
        for pin_node in find_all(node, "pin"):
            p_num = get_str(pin_node, "number")
            p_name = get_str(pin_node, "name")
            p_etype = get_str(pin_node, "electrical_type", "passive")
            pins.append(MbsPin(number=p_num, name=p_name, electrical_type=p_etype))

        blocks.append(MbsModuleBlock(
            sub_project_path=sub_proj,
            sub_project_uuid=sub_uuid,
            component=component,
            mbs_reference=mbs_ref,
            name=name,
            pins=pins,
        ))
    return blocks
