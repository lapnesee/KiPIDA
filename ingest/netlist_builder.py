"""Merge PCB and schematic data into a unified net list.

Resolves pin electrical types from schematic lib_symbols, classifies power
rails deterministically (broche power_out = source), and populates cross-board
net aliases from the MBS module_block topology.

A footprint meets its symbol by the path it records, and by nothing else. A
reference is a label: two sheet instances can hold the same one until they are
annotated, a board not updated since the last annotation holds old ones, and a
logo placed on the board carries whatever it was typed with. Joining on it
attached symbols to footprints they have nothing to do with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .board_reader import ParsedBoard, Stackup, BoardBounds
from .multiboard import CrossBoardNet
from .schematic_reader import ParsedSchematic, SymbolInstance


@dataclass
class NetPin:
    footprint_ref: str
    pad_number: str
    net_name: str
    electrical_type: str  # from schematic lib, or "passive" if unknown


@dataclass
class NetInfo:
    name: str
    board_uuid: str
    is_power_rail: bool
    has_power_out_source: bool
    voltage_hint: Optional[float]
    pins: list[NetPin] = field(default_factory=list)
    aliases: set[str] = field(default_factory=set)


# How a footprint relates to the schematic. Absence is a state, not a gap.
LINKED = "linked"                  # its path names a symbol of the schematic
PCB_ONLY = "pcb_only"              # no path: placed on the board, never from a symbol
SYMBOL_MISSING = "symbol_missing"  # its path names a symbol the schematic no longer holds
NO_SCHEMATIC = "no_schematic"      # no schematic was read

# Link issue kinds.
DUPLICATE_REFERENCE = "duplicate_reference"  # several footprints carry one reference
REFERENCE_MISMATCH = "reference_mismatch"    # a linked footprint and its symbol disagree


@dataclass
class ComponentInfo:
    reference: str
    value: str
    lib_id: str
    datasheet: str
    extra_fields: dict[str, str]
    position: tuple[float, float]
    nets_by_pad: dict[str, str]
    pin_types_by_pad: dict[str, str]
    schematic_link: str = NO_SCHEMATIC


@dataclass
class LinkIssue:
    kind: str
    references: list[str]
    detail: str


@dataclass
class BoardNetlist:
    board_uuid: str
    pcb_path: Path
    components: list[ComponentInfo]
    nets: list[NetInfo]
    stackup: Stackup
    bounds: BoardBounds
    link_issues: list[LinkIssue] = field(default_factory=list)


# Power-bearing pin types — a net with any of these carries power.
_POWER_TYPES = frozenset({"power_in", "power_out", "open_collector", "open_emitter"})
_SOURCE_TYPES = frozenset({"power_out"})


def build_netlist(
    board: ParsedBoard,
    schematic: Optional[ParsedSchematic],
    board_uuid: str,
    cross_board_nets: Optional[list[CrossBoardNet]] = None,
) -> BoardNetlist:
    """Fuse PCB and schematic data into a BoardNetlist."""

    # The path a footprint records -> the symbol placement it came from. Every
    # unit of a multi-unit symbol has its own uuid, so each is indexed.
    sch_by_path: dict[str, SymbolInstance] = {}
    if schematic:
        for inst in schematic.instances:
            if inst.footprint_path:
                sch_by_path.setdefault(inst.footprint_path, inst)
    link_issues: list[LinkIssue] = []

    # Accumulate nets: net_name → NetInfo
    nets_map: dict[str, NetInfo] = {}

    def _ensure_net(net_name: str) -> NetInfo:
        if net_name not in nets_map:
            nets_map[net_name] = NetInfo(
                name=net_name,
                board_uuid=board_uuid,
                is_power_rail=False,
                has_power_out_source=False,
                voltage_hint=_estimate_voltage(net_name),
            )
        return nets_map[net_name]

    components: list[ComponentInfo] = []

    for fp in board.footprints:
        sch_inst = sch_by_path.get(fp.sch_path) if schematic and fp.sch_path else None
        if schematic is None:
            link = NO_SCHEMATIC
        elif not fp.sch_path:
            link = PCB_ONLY
        else:
            link = LINKED if sch_inst else SYMBOL_MISSING
        if sch_inst and sch_inst.reference and fp.reference and sch_inst.reference != fp.reference:
            link_issues.append(LinkIssue(
                REFERENCE_MISMATCH, [fp.reference, sch_inst.reference],
                f"Footprint {fp.reference} comes from symbol {sch_inst.reference}: "
                "the board was not updated since the schematic was annotated.",
            ))
        pin_type_map: dict[str, str] = {}
        if sch_inst:
            for pin_def in sch_inst.pins:
                pin_type_map[pin_def.number] = pin_def.electrical_type

        nets_by_pad: dict[str, str] = {}
        for pad in fp.pads:
            if not pad.net_name:
                continue
            nets_by_pad[pad.number] = pad.net_name
            etype = pin_type_map.get(pad.number, pad.pintype or "passive")
            pin_type_map.setdefault(pad.number, etype)

            net = _ensure_net(pad.net_name)
            net.pins.append(NetPin(
                footprint_ref=fp.reference,
                pad_number=pad.number,
                net_name=pad.net_name,
                electrical_type=etype,
            ))
            if etype in _POWER_TYPES:
                net.is_power_rail = True
            if etype in _SOURCE_TYPES:
                net.has_power_out_source = True

        extra_fields: dict[str, str] = {}
        if sch_inst:
            extra_fields = dict(sch_inst.extra_fields)

        components.append(ComponentInfo(
            reference=fp.reference,
            value=fp.value,
            lib_id=fp.lib_id,
            datasheet=sch_inst.datasheet if sch_inst else "",
            extra_fields=extra_fields,
            position=fp.position,
            nets_by_pad=nets_by_pad,
            pin_types_by_pad=dict(pin_type_map),
            schematic_link=link,
        ))

    footprints_by_reference: dict[str, int] = {}
    for fp in board.footprints:
        if fp.reference:
            footprints_by_reference[fp.reference] = footprints_by_reference.get(fp.reference, 0) + 1
    for reference, count in sorted(footprints_by_reference.items()):
        if count > 1:
            link_issues.append(LinkIssue(
                DUPLICATE_REFERENCE, [reference],
                f"{count} footprints carry the reference {reference}.",
            ))

    # Also capture nets from segments and zones (no pin type info available)
    for seg in board.segments:
        if seg.net_name:
            _ensure_net(seg.net_name)
    for via in board.vias:
        if via.net_name:
            _ensure_net(via.net_name)
    for zone in board.zones:
        if zone.net_name:
            _ensure_net(zone.net_name)

    # Apply cross-board aliases
    if cross_board_nets:
        cb_lookup: dict[str, list[str]] = {}  # net_name → [uuid1, uuid2]
        for cbn in cross_board_nets:
            cb_lookup[cbn.net_name] = cbn.boards
        for net_name, net_info in nets_map.items():
            if net_name in cb_lookup:
                for uuid in cb_lookup[net_name]:
                    if uuid != board_uuid:
                        net_info.aliases.add(f"{uuid}:{net_name}")

    # Global nets from schematic (mark as known even if no pad on this board)
    if schematic:
        for gn in schematic.global_nets:
            _ensure_net(gn)

    nets = sorted(nets_map.values(), key=lambda n: n.name)
    return BoardNetlist(
        board_uuid=board_uuid,
        pcb_path=board.pcb_path,
        components=components,
        nets=nets,
        stackup=board.stackup,
        bounds=board.bounds,
        link_issues=link_issues,
    )


# ---------------------------------------------------------------------------
# Voltage heuristic (name-only, used only as a hint when schematic is absent)
# ---------------------------------------------------------------------------

def _estimate_voltage(name: str) -> Optional[float]:
    # "+3V3" style
    m = re.search(r"(\d+)V(\d+)", name, re.IGNORECASE)
    if m:
        try:
            return float(f"{m.group(1)}.{m.group(2)}")
        except ValueError:
            pass
    # "+5V" style
    m = re.search(r"[+](\d+\.?\d*)V", name, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    # "5V" word-boundary style
    m = re.search(r"\b(\d+\.?\d*)V\b", name, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None
