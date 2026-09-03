"""Read KiCad schematic files (.kicad_sch) offline.

Follows hierarchical sheet references recursively to collect all symbol
instances and their pin electrical types from lib_symbols definitions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .sexpr import parse, find, find_all, get_str


@dataclass
class PinDef:
    number: str
    name: str
    electrical_type: str  # power_in, power_out, bidirectional, passive, input, output…


@dataclass
class SymbolDef:
    lib_id: str
    pins: list[PinDef] = field(default_factory=list)


@dataclass
class SymbolInstance:
    lib_id: str
    uuid: str
    reference: str
    value: str
    footprint: str
    datasheet: str
    extra_fields: dict[str, str]
    sheet_path: str       # hierarchical path "/root_uuid/sheet_uuid"
    pins: list[PinDef]    # from lib definition


@dataclass
class ParsedSchematic:
    root_sch_path: Path
    symbol_defs: dict[str, SymbolDef]
    instances: list[SymbolInstance]
    global_nets: list[str]


def read_schematic(root_sch_path: Path) -> ParsedSchematic:
    """Parse root schematic and all referenced sub-sheets recursively."""
    root_sch_path = Path(root_sch_path)
    symbol_defs: dict[str, SymbolDef] = {}
    instances: list[SymbolInstance] = []
    global_nets: list[str] = []
    visited: set[Path] = set()
    is_root: list[bool] = [True]  # mutable flag to distinguish root from sub-sheets

    def _process_sheet(sch_path: Path) -> None:
        sch_path = sch_path.resolve()
        if sch_path in visited:
            return
        visited.add(sch_path)

        root_call = is_root[0]
        is_root[0] = False

        try:
            text = sch_path.read_text(encoding="utf-8")
        except OSError:
            if root_call:
                raise  # propagate for the root file; silently skip sub-sheets
            return

        roots = parse(text)
        if not roots or not isinstance(roots[0], list):
            return
        root = roots[0]

        # Collect lib_symbols definitions (may be in any sheet)
        lib_syms_node = find(root, "lib_symbols")
        if lib_syms_node:
            for sym_node in find_all(lib_syms_node, "symbol"):
                if not sym_node or len(sym_node) < 2:
                    continue
                lib_id = sym_node[1] if isinstance(sym_node[1], str) else ""
                if not lib_id or lib_id in symbol_defs:
                    continue
                symbol_defs[lib_id] = _parse_sym_def(lib_id, sym_node)

        # Collect global_label names
        for gl_node in find_all(root, "global_label"):
            if len(gl_node) >= 2 and isinstance(gl_node[1], str):
                name = gl_node[1]
                if name and name not in global_nets:
                    global_nets.append(name)

        # Collect symbol instances
        for sym_node in find_all(root, "symbol"):
            if not sym_node or len(sym_node) < 2:
                continue
            # Skip lib_symbols children — they sit inside lib_symbols, not at root
            # but find_all only looks at direct children of root, so this is fine.
            # Detect lib definition vs instance: lib defs have a string as second element
            # that contains ":" (lib:name) and do NOT have an "instances" child.
            second = sym_node[1] if len(sym_node) > 1 else ""
            if isinstance(second, str) and ":" in second:
                # Could be either lib_id atom (instance) or lib def name
                # Instances always have (lib_id "...") as a sub-node
                lib_id_node = find(sym_node, "lib_id")
                if lib_id_node is None:
                    continue  # bare lib def, skip
                lib_id = lib_id_node[1] if len(lib_id_node) > 1 else ""
            else:
                lib_id_node = find(sym_node, "lib_id")
                if lib_id_node is None:
                    continue
                lib_id = lib_id_node[1] if len(lib_id_node) > 1 else ""

            uuid_node = find(sym_node, "uuid")
            uuid = uuid_node[1] if uuid_node and len(uuid_node) > 1 else ""

            props = _collect_properties(sym_node)
            reference = props.get("Reference", "")
            value = props.get("Value", "")
            footprint = props.get("Footprint", "")
            datasheet = props.get("Datasheet", "")
            extra = {k: v for k, v in props.items()
                     if k not in ("Reference", "Value", "Footprint", "Datasheet",
                                  "ki_keywords", "ki_fp_filters", "Description",
                                  "Description_1", "ki_description")}

            # Sheet path from instances block
            sheet_path = _extract_sheet_path(sym_node)

            # Resolve pin defs from lib
            pin_defs = _resolve_pins(lib_id, symbol_defs)

            instances.append(SymbolInstance(
                lib_id=lib_id,
                uuid=uuid,
                reference=reference,
                value=value,
                footprint=footprint,
                datasheet=datasheet,
                extra_fields=extra,
                sheet_path=sheet_path,
                pins=pin_defs,
            ))

        # Recurse into sub-sheets
        for sheet_node in find_all(root, "sheet"):
            sheetfile_node = find(sheet_node, "sheetfile")
            if sheetfile_node and len(sheetfile_node) > 1:
                sub_filename = sheetfile_node[1]
                sub_path = sch_path.parent / sub_filename
                _process_sheet(sub_path)

    _process_sheet(root_sch_path)
    return ParsedSchematic(
        root_sch_path=root_sch_path,
        symbol_defs=symbol_defs,
        instances=instances,
        global_nets=global_nets,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# KiCad pin electrical type as first token after "pin" tag in lib_symbols:
# (pin power_in line (at ...) ...)
_PIN_TYPES = {
    "power_in", "power_out", "bidirectional", "passive", "input", "output",
    "open_collector", "open_emitter", "tri_state", "unspecified", "no_connect",
}


def _parse_sym_def(lib_id: str, sym_node: list) -> SymbolDef:
    pins: list[PinDef] = []
    _collect_pins_recursive(sym_node, pins)
    return SymbolDef(lib_id=lib_id, pins=pins)


def _collect_pins_recursive(node: list, pins: list[PinDef]) -> None:
    """Walk all sub-nodes collecting (pin <type> ...) definitions."""
    for child in node[1:]:
        if not isinstance(child, list) or not child:
            continue
        if child[0] == "pin" and len(child) >= 2 and isinstance(child[1], str):
            etype = child[1] if child[1] in _PIN_TYPES else "passive"
            # (pin etype line (at ...) (length ...) (name "NAME" ...) (number "NUM" ...))
            name_node = find(child, "name")
            number_node = find(child, "number")
            pin_name = name_node[1] if name_node and len(name_node) > 1 else ""
            pin_num = number_node[1] if number_node and len(number_node) > 1 else ""
            pins.append(PinDef(number=pin_num, name=pin_name, electrical_type=etype))
        else:
            _collect_pins_recursive(child, pins)


def _collect_properties(sym_node: list) -> dict[str, str]:
    props: dict[str, str] = {}
    for prop_node in find_all(sym_node, "property"):
        if len(prop_node) >= 3:
            key = prop_node[1]
            val = prop_node[2]
            if isinstance(key, str) and isinstance(val, str):
                props[key] = val
    return props


def _extract_sheet_path(sym_node: list) -> str:
    instances_node = find(sym_node, "instances")
    if not instances_node:
        return ""
    for project_node in find_all(instances_node, "project"):
        for path_node in find_all(project_node, "path"):
            if len(path_node) >= 2 and isinstance(path_node[1], str):
                return path_node[1]
    return ""


def _resolve_pins(lib_id: str, symbol_defs: dict[str, SymbolDef]) -> list[PinDef]:
    """Return pin defs for lib_id, handling unit suffixes like "Device:R_0_1"."""
    if lib_id in symbol_defs:
        return list(symbol_defs[lib_id].pins)
    # Try stripping unit suffix (e.g. "Device:R" from "Device:R_0_1")
    base = re.sub(r"_\d+_\d+$", "", lib_id)
    if base in symbol_defs:
        return list(symbol_defs[base].pins)
    return []
