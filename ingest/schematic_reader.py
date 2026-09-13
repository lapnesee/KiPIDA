"""Read KiCad schematic files (.kicad_sch) offline.

The hierarchy is walked by sheet *instance*, not by file. A sheet file placed
twice is one file and two instances: its symbols exist twice, once under each
instance path, and each copy carries the reference KiCad recorded for that
path. Reading each file once kept one copy -- 73 of complex_hierarchy's 119
symbols, 1,606 of vme-wren's 2,876 -- and gave it whichever reference the
symbol's own property held.

A symbol's instance block lists every path it is placed under:

    (instances (project "name" (path "/root-uuid/sheet-uuid" (reference "R201") (unit 1))))

The entry is chosen by its path alone. The project name written there is the
one the sheet was annotated under, which is not the file's name once a project
has been renamed or copied (nine of the KiCad 10 demos).

A sheet that cannot be followed is an :class:`SchematicIssue`, never a silent
gap: its symbols are missing from every count and rule downstream, and whoever
reads the result has to be told which ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .sexpr import parse, find, find_all

# Issue kinds. The first three leave the sheet's symbols out; the last two read
# it, but name a reference that only resolves on some filesystems.
MISSING_SHEET = "missing_sheet"
UNREADABLE_SHEET = "unreadable_sheet"
RECURSIVE_SHEET = "recursive_sheet"
SHEET_FILE_CASE = "sheet_file_case"
SHEET_FILE_SEPARATOR = "sheet_file_separator"


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
    sheet_path: str       # instance path of the sheet holding it: "/root_uuid/sheet_uuid"
    pins: list[PinDef]    # from lib definition
    # The path a PCB footprint records for this symbol: the sheet instance path
    # without the root sheet, then the symbol's own uuid ("/sheet_uuid/symbol_uuid").
    footprint_path: str = ""
    unit: int = 1


@dataclass
class SchematicIssue:
    kind: str          # one of the module's issue kinds
    sheet_path: str    # instance path the sheet would have had
    sheet_name: str
    sheet_file: str    # as written in the parent sheet
    detail: str


@dataclass
class ParsedSchematic:
    root_sch_path: Path
    symbol_defs: dict[str, SymbolDef]
    instances: list[SymbolInstance]
    global_nets: list[str]
    issues: list[SchematicIssue] = field(default_factory=list)


def read_schematic(root_sch_path: Path) -> ParsedSchematic:
    """Parse the root schematic and every sheet instance below it."""
    root_sch_path = Path(root_sch_path)
    symbol_defs: dict[str, SymbolDef] = {}
    instances: list[SymbolInstance] = []
    global_nets: list[str] = []
    issues: list[SchematicIssue] = []
    parsed_files: dict[Path, list] = {}
    listings: dict[Path, list[str]] = {}

    def _load(sch_path: Path):
        """Parse a file once, however many instances use it."""
        if sch_path not in parsed_files:
            roots = parse(sch_path.read_text(encoding="utf-8"))
            root = roots[0] if roots and isinstance(roots[0], list) else None
            parsed_files[sch_path] = root
            if root is not None:
                _collect_file_data(root, symbol_defs, global_nets)
        return parsed_files[sch_path]

    def _walk(sch_path: Path, segments: tuple[str, ...], ancestry: tuple[Path, ...]) -> None:
        sch_path = sch_path.resolve()
        root = _load(sch_path)
        if root is None:
            return
        if not segments:
            segments = (_uuid_of(root),)
        sheet_path = "/" + "/".join(segments)

        for sym_node in find_all(root, "symbol"):
            instance = _symbol_instance(sym_node, sheet_path, segments, symbol_defs)
            if instance is not None:
                instances.append(instance)

        for sheet_node in find_all(root, "sheet"):
            written = _sheet_filename(sheet_node)
            if written:
                _follow(sheet_node, written, sch_path, segments + (_uuid_of(sheet_node),),
                        ancestry + (sch_path,))

    def _follow(sheet_node, written, parent_path, segments, ancestry) -> None:
        def issue(kind, detail):
            issues.append(SchematicIssue(
                kind=kind, sheet_path="/" + "/".join(segments),
                sheet_name=_sheet_name(sheet_node), sheet_file=written, detail=detail,
            ))

        child, notes = _resolve_sheet_file(parent_path.parent, written, listings)
        for kind, detail in notes:
            issue(kind, detail)
        if child is None:
            issue(MISSING_SHEET, f"{written} does not exist beside {parent_path.name}; "
                                 "the symbols of this sheet are not read.")
            return
        if child.resolve() in ancestry:
            # KiCad refuses to load a sheet that contains itself.
            issue(RECURSIVE_SHEET, f"{written} is already one of this sheet's parents; not followed.")
            return
        try:
            _walk(child, segments, ancestry)
        except (OSError, UnicodeDecodeError) as exc:
            issue(UNREADABLE_SHEET, f"{written} could not be read ({exc}); "
                                    "the symbols of this sheet are not read.")

    _walk(root_sch_path, (), ())
    return ParsedSchematic(
        root_sch_path=root_sch_path,
        symbol_defs=symbol_defs,
        instances=instances,
        global_nets=global_nets,
        issues=issues,
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


def _collect_file_data(root: list, symbol_defs: dict[str, SymbolDef], global_nets: list[str]) -> None:
    """Library definitions and global labels, which do not depend on the instance."""
    lib_syms_node = find(root, "lib_symbols")
    if lib_syms_node:
        for sym_node in find_all(lib_syms_node, "symbol"):
            if not sym_node or len(sym_node) < 2:
                continue
            lib_id = sym_node[1] if isinstance(sym_node[1], str) else ""
            if not lib_id or lib_id in symbol_defs:
                continue
            symbol_defs[lib_id] = _parse_sym_def(lib_id, sym_node)

    for gl_node in find_all(root, "global_label"):
        if len(gl_node) >= 2 and isinstance(gl_node[1], str):
            name = gl_node[1]
            if name and name not in global_nets:
                global_nets.append(name)


def _symbol_instance(sym_node: list, sheet_path: str, segments: tuple[str, ...],
                     symbol_defs: dict[str, SymbolDef]):
    """One placed symbol under one sheet instance, or None for a non-symbol node."""
    if not sym_node or len(sym_node) < 2:
        return None
    lib_id_node = find(sym_node, "lib_id")
    if lib_id_node is None:
        return None  # a bare library definition, not a placed symbol
    lib_id = lib_id_node[1] if len(lib_id_node) > 1 else ""

    uuid = _uuid_of(sym_node)
    props = _collect_properties(sym_node)
    extra = {k: v for k, v in props.items()
             if k not in ("Reference", "Value", "Footprint", "Datasheet",
                          "ki_keywords", "ki_fp_filters", "Description",
                          "Description_1", "ki_description")}

    reference = props.get("Reference", "")
    unit = _int_atom(find(sym_node, "unit"), 1)
    entry = _instance_entry(sym_node, sheet_path)
    if entry is not None:
        reference = _str_atom(find(entry, "reference"), reference)
        unit = _int_atom(find(entry, "unit"), unit)

    return SymbolInstance(
        lib_id=lib_id,
        uuid=uuid,
        reference=reference,
        value=props.get("Value", ""),
        footprint=props.get("Footprint", ""),
        datasheet=props.get("Datasheet", ""),
        extra_fields=extra,
        sheet_path=sheet_path,
        pins=_resolve_pins(lib_id, symbol_defs),
        footprint_path="/" + "/".join(segments[1:] + (uuid,)),
        unit=unit,
    )


def _instance_entry(sym_node: list, sheet_path: str):
    """The (path ...) entry recorded for this sheet instance, whatever its project name."""
    instances_node = find(sym_node, "instances")
    if not instances_node:
        return None
    for project_node in find_all(instances_node, "project"):
        for path_node in find_all(project_node, "path"):
            if len(path_node) >= 2 and path_node[1] == sheet_path:
                return path_node
    return None


def _sheet_filename(sheet_node: list) -> str:
    # KiCad 6+ names the child file in a property, not a node of its own:
    #     (sheet ... (property "Sheetfile" "child.kicad_sch" ...) ...)
    # Looking only for a bare (sheetfile ...) matched nothing, so no hierarchy
    # was ever followed. The node form is still accepted.
    sheetfile_node = find(sheet_node, "sheetfile")
    if sheetfile_node and len(sheetfile_node) > 1:
        return str(sheetfile_node[1])
    for prop in find_all(sheet_node, "property"):
        if len(prop) >= 3 and str(prop[1]).lower() == "sheetfile":
            return str(prop[2])
    return ""


def _sheet_name(sheet_node: list) -> str:
    for prop in find_all(sheet_node, "property"):
        if len(prop) >= 3 and str(prop[1]).lower() == "sheetname":
            return str(prop[2])
    return ""


def _resolve_sheet_file(parent_dir: Path, written: str, listings: dict[Path, list[str]]):
    """The file a Sheetfile names, and what about the name would not travel.

    KiCad resolves a sheet file against the directory of the sheet naming it.
    Whether that works can depend on the machine: a backslash is a separator
    only on Windows, and a name differing from the file in case only resolves
    on a case-insensitive filesystem -- which Windows answers as a match, so
    the case is compared with the directory listing rather than trusted to
    ``exists()``.
    """
    notes = []
    relative = written
    if "\\" in written:
        relative = written.replace("\\", "/")
        notes.append((SHEET_FILE_SEPARATOR,
                      f"{written} uses backslashes, which only Windows reads as separators."))
    if Path(relative).is_absolute():
        target = Path(relative)
        return (target if target.is_file() else None), notes

    current = parent_dir
    for part in Path(relative).parts:
        if part in (".", ".."):
            current = current / part
            continue
        if current not in listings:
            try:
                listings[current] = [entry.name for entry in current.iterdir()]
            except OSError:
                listings[current] = []
        names = listings[current]
        if part in names:
            current = current / part
            continue
        folded = [name for name in names if name.casefold() == part.casefold()]
        if not folded:
            return None, notes
        notes.append((SHEET_FILE_CASE,
                      f"{written} names {part} but the file on disk is {folded[0]}; "
                      "a case-sensitive filesystem would not find it."))
        current = current / folded[0]
    return (current if current.is_file() else None), notes


def _uuid_of(node: list) -> str:
    return _str_atom(find(node, "uuid"), "")


def _str_atom(node, default: str) -> str:
    if node is not None and len(node) > 1 and isinstance(node[1], str):
        return node[1]
    return default


def _int_atom(node, default: int) -> int:
    try:
        return int(_str_atom(node, ""))
    except ValueError:
        return default


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


def _resolve_pins(lib_id: str, symbol_defs: dict[str, SymbolDef]) -> list[PinDef]:
    """Return pin defs for lib_id, handling unit suffixes like "Device:R_0_1"."""
    if lib_id in symbol_defs:
        return list(symbol_defs[lib_id].pins)
    # Try stripping unit suffix (e.g. "Device:R" from "Device:R_0_1")
    base = re.sub(r"_\d+_\d+$", "", lib_id)
    if base in symbol_defs:
        return list(symbol_defs[base].pins)
    return []
