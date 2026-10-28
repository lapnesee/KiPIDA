"""Immutable, JSON-serialisable snapshot of the full design.

DesignModel is the frozen artefact that all Ki-PIDA analyses consume.
It carries a SHA-256 hash of the source files so that analyses can be
cached and invalidated when the design changes.

The hash covers every file the model was built from -- every sheet of the
hierarchy, not only the root, which an edit to a sub-sheet leaves unchanged.
A file that could not be read is an entry of ``issues``, not a silent gap in
the netlist.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .board_reader import ParsedBoard, read_board
from .multiboard import CrossBoardNet
from .netlist_builder import BoardNetlist, build_netlist
from .project_resolver import BoardProject, MultiboardProject, resolve_project
from .schematic_reader import ParsedSchematic, read_schematic


@dataclass
class DesignModel:
    """Frozen snapshot of the full design — PCB + schematic + cross-board topology."""

    project_name: str
    is_multiboard: bool
    # Stored as plain dicts for JSON compatibility and hashability
    board_netlists: tuple        # tuple[dict, ...]
    cross_board_nets: tuple      # tuple[dict, ...]
    source_hash: str             # SHA-256 of all source file contents
    issues: tuple = ()           # tuple[dict, ...]: files that could not be read, and why

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible dict."""
        return {
            "project_name": self.project_name,
            "is_multiboard": self.is_multiboard,
            "board_netlists": list(self.board_netlists),
            "cross_board_nets": list(self.cross_board_nets),
            "source_hash": self.source_hash,
            "issues": list(self.issues),
        }

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), **kwargs)


def build_design_model(resolved_project: MultiboardProject | BoardProject) -> DesignModel:
    """Build a DesignModel from a resolved project descriptor."""

    if isinstance(resolved_project, MultiboardProject):
        return _from_multiboard(resolved_project)
    return _from_single_board(resolved_project)


# ---------------------------------------------------------------------------
# Internal builders
# ---------------------------------------------------------------------------

def _from_multiboard(mp: MultiboardProject) -> DesignModel:
    project_name = mp.mbs_path.stem

    netlists: list[dict] = []
    issues: list[dict] = [{"board": "", "file": str(mp.mbs_path), "error": note} for note in mp.notes]
    source_paths: list[Path] = [mp.mbs_path]
    if mp.container_pro_path:
        source_paths.append(mp.container_pro_path)

    for bp in mp.boards:
        nl = _build_board_netlist(bp, mp.cross_board_nets, source_paths, issues)
        netlists.append(nl)

    source_hash = _hash_files(source_paths)

    cross_dicts = [
        {"net_name": c.net_name, "boards": list(c.boards), "members": [list(member) for member in c.members]}
        for c in mp.cross_board_nets
    ]

    return DesignModel(
        project_name=project_name,
        is_multiboard=True,
        board_netlists=tuple(netlists),
        cross_board_nets=tuple(cross_dicts),
        source_hash=source_hash,
        issues=tuple(issues),
    )


def _from_single_board(bp: BoardProject) -> DesignModel:
    project_name = bp.pro_path.stem
    source_paths: list[Path] = []
    issues: list[dict] = []
    nl = _build_board_netlist(bp, [], source_paths, issues)
    source_hash = _hash_files(source_paths)

    return DesignModel(
        project_name=project_name,
        is_multiboard=False,
        board_netlists=(nl,),
        cross_board_nets=(),
        source_hash=source_hash,
        issues=tuple(issues),
    )


# What a reader raises for a file it cannot use: absent or locked, not text,
# or not a KiCad file it supports (UnsupportedFileFormat is a ValueError).
_READ_ERRORS = (OSError, UnicodeDecodeError, ValueError)


def _build_board_netlist(
    bp: BoardProject,
    cross_board_nets: list[CrossBoardNet],
    source_paths: list[Path],
    issues: list[dict],
) -> dict:
    """The board's netlist as a dict; the files it read join ``source_paths``."""
    board_id = bp.sub_project_uuid or bp.pro_path.stem
    source_paths.append(bp.pro_path)

    def unreadable(path: Path, exc: Exception) -> None:
        issues.append({"board": board_id, "file": str(path), "error": f"{type(exc).__name__}: {exc}"})

    issues.extend({"board": board_id, "file": str(bp.pro_path), "error": note} for note in bp.notes)

    board: Optional[ParsedBoard] = None
    if bp.pcb_path.exists():
        source_paths.append(bp.pcb_path)
        try:
            board = read_board(bp.pcb_path)
        except _READ_ERRORS as exc:
            unreadable(bp.pcb_path, exc)

    schematic: Optional[ParsedSchematic] = None
    if bp.sch_root_path.exists():
        try:
            schematic = read_schematic(bp.sch_root_path)
            source_paths.extend(schematic.files)
        except _READ_ERRORS as exc:
            source_paths.append(bp.sch_root_path)
            unreadable(bp.sch_root_path, exc)

    if board is None:
        # Construct minimal empty board
        from .board_reader import BoardBounds, Stackup
        board = ParsedBoard(
            pcb_path=bp.pcb_path,
            stackup=Stackup(layers=[], total_thickness_mm=0.0, copper_layer_count=0),
            bounds=BoardBounds(0, 0, 0, 0),
            footprints=[],
            segments=[],
            vias=[],
            zones=[],
        )

    netlist = _netlist_to_dict(build_netlist(
        board=board,
        schematic=schematic,
        board_uuid=board_id,
        cross_board_nets=cross_board_nets,
    ))
    netlist["schematic_issues"] = [
        {"kind": issue.kind, "sheet_path": issue.sheet_path, "sheet_name": issue.sheet_name,
         "sheet_file": issue.sheet_file, "detail": issue.detail}
        for issue in (schematic.issues if schematic else [])
    ]
    return netlist


def _hash_files(paths: list[Path]) -> str:
    """SHA-256 over each file's name and bytes; a file that cannot be read still counts.

    The name, not the full path: a project moved or copied elsewhere is the same design.
    """
    h = hashlib.sha256()
    for p in sorted({Path(path).resolve() for path in paths}):
        h.update(p.name.encode("utf-8"))
        try:
            h.update(p.read_bytes())
        except OSError as exc:
            h.update(f"<unreadable: {type(exc).__name__}>".encode("utf-8"))
    return h.hexdigest()


def _netlist_to_dict(nl: BoardNetlist) -> dict:
    """Convert BoardNetlist to a plain dict for JSON serialisation."""
    components = []
    for c in nl.components:
        components.append({
            "reference": c.reference,
            "value": c.value,
            "lib_id": c.lib_id,
            "datasheet": c.datasheet,
            "extra_fields": c.extra_fields,
            "position": list(c.position),
            "nets_by_pad": c.nets_by_pad,
            "pin_types_by_pad": c.pin_types_by_pad,
            "schematic_link": c.schematic_link,
        })

    nets = []
    for n in nl.nets:
        nets.append({
            "name": n.name,
            "board_uuid": n.board_uuid,
            "is_power_rail": n.is_power_rail,
            "rail_source": n.rail_source,
            "rail_confidence": n.rail_confidence,
            "has_power_out_source": n.has_power_out_source,
            "voltage_hint": n.voltage_hint,
            "aliases": sorted(n.aliases),
            "pins": [
                {
                    "footprint_ref": p.footprint_ref,
                    "pad_number": p.pad_number,
                    "net_name": p.net_name,
                    "electrical_type": p.electrical_type,
                }
                for p in n.pins
            ],
        })

    return {
        "board_uuid": nl.board_uuid,
        "pcb_path": str(nl.pcb_path),
        "stackup": {
            "total_thickness_mm": nl.stackup.total_thickness_mm,
            "copper_layer_count": nl.stackup.copper_layer_count,
            "layers": [
                {
                    "layer_id": lyr.layer_id,
                    "name": lyr.name,
                    "layer_type": lyr.layer_type,
                    "thickness_mm": lyr.thickness_mm,
                    "epsilon_r": lyr.epsilon_r,
                    "loss_tangent": lyr.loss_tangent,
                }
                for lyr in nl.stackup.layers
            ],
        },
        "bounds": {
            "x_min": nl.bounds.x_min,
            "y_min": nl.bounds.y_min,
            "x_max": nl.bounds.x_max,
            "y_max": nl.bounds.y_max,
            "width_mm": nl.bounds.width_mm,
            "height_mm": nl.bounds.height_mm,
            "area_mm2": nl.bounds.area_mm2,
        },
        "components": components,
        "nets": nets,
        "link_issues": [
            {"kind": issue.kind, "references": list(issue.references), "detail": issue.detail}
            for issue in nl.link_issues
        ],
    }
