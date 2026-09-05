"""Immutable, JSON-serialisable snapshot of the full design.

DesignModel is the frozen artefact that all Ki-PIDA analyses consume.
It carries a SHA-256 hash of the source files so that analyses can be
cached and invalidated when the design changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
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

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible dict."""
        return {
            "project_name": self.project_name,
            "is_multiboard": self.is_multiboard,
            "board_netlists": list(self.board_netlists),
            "cross_board_nets": list(self.cross_board_nets),
            "source_hash": self.source_hash,
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
    source_paths: list[Path] = [mp.mbs_path]
    if mp.container_pro_path:
        source_paths.append(mp.container_pro_path)

    for bp in mp.boards:
        source_paths.extend(_board_source_paths(bp))
        nl = _build_board_netlist(bp, mp.cross_board_nets)
        netlists.append(_netlist_to_dict(nl))

    source_hash = _hash_files(source_paths)

    cross_dicts = [
        {"net_name": c.net_name, "boards": list(c.boards)}
        for c in mp.cross_board_nets
    ]

    return DesignModel(
        project_name=project_name,
        is_multiboard=True,
        board_netlists=tuple(netlists),
        cross_board_nets=tuple(cross_dicts),
        source_hash=source_hash,
    )


def _from_single_board(bp: BoardProject) -> DesignModel:
    project_name = bp.pro_path.stem
    source_paths = _board_source_paths(bp)
    nl = _build_board_netlist(bp, [])
    source_hash = _hash_files(source_paths)

    return DesignModel(
        project_name=project_name,
        is_multiboard=False,
        board_netlists=((_netlist_to_dict(nl)),),
        cross_board_nets=(),
        source_hash=source_hash,
    )


def _build_board_netlist(
    bp: BoardProject,
    cross_board_nets: list[CrossBoardNet],
) -> BoardNetlist:
    board: Optional[ParsedBoard] = None
    if bp.pcb_path.exists():
        try:
            board = read_board(bp.pcb_path)
        except Exception:
            pass

    schematic: Optional[ParsedSchematic] = None
    if bp.sch_root_path.exists():
        try:
            schematic = read_schematic(bp.sch_root_path)
        except Exception:
            pass

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

    return build_netlist(
        board=board,
        schematic=schematic,
        board_uuid=bp.sub_project_uuid or bp.pro_path.stem,
        cross_board_nets=cross_board_nets,
    )


def _board_source_paths(bp: BoardProject) -> list[Path]:
    paths = [bp.pro_path]
    if bp.pcb_path.exists():
        paths.append(bp.pcb_path)
    if bp.sch_root_path.exists():
        paths.append(bp.sch_root_path)
    return paths


def _hash_files(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(set(paths)):
        try:
            h.update(p.read_bytes())
        except OSError:
            pass
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
        })

    nets = []
    for n in nl.nets:
        nets.append({
            "name": n.name,
            "board_uuid": n.board_uuid,
            "is_power_rail": n.is_power_rail,
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
    }
