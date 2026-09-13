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
    unit: int = 0         # 0: drawn in every unit
    body_style: int = 0   # 0: drawn in every body style; 2 is the De Morgan alternate
    x: float = 0.0        # connection point, library coordinates (Y up)
    y: float = 0.0


@dataclass
class SymbolDef:
    lib_id: str
    pins: list[PinDef] = field(default_factory=list)
    power: str = ""       # "global" or "local" for a power symbol, "" otherwise


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
    pins: list[PinDef]    # from lib definition, every unit and body style
    # The path a PCB footprint records for this symbol: the sheet instance path
    # without the root sheet, then the symbol's own uuid ("/sheet_uuid/symbol_uuid").
    footprint_path: str = ""
    unit: int = 1
    body_style: int = 1
    lib_name: str = ""    # the embedded definition, when it differs from the library's
    power: str = ""       # from the definition: "global", "local" or ""
    # Attributes as KiCad applies them: a symbol inside a sheet excluded from
    # the board or the BOM, or marked DNP, carries its sheet's exclusion.
    in_bom: bool = True
    on_board: bool = True
    dnp: bool = False
    no_connect_pins: set[str] = field(default_factory=set)  # pin numbers under a no-connect marker

    def unit_pins(self) -> list[PinDef]:
        """The pins this placement draws: its unit's and body style's, and the shared ones."""
        return [pin for pin in self.pins
                if pin.unit in (0, self.unit) and pin.body_style in (0, self.body_style)]


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
    parsed_files: dict[Path, Optional[_SheetFile]] = {}
    listings: dict[Path, list[str]] = {}

    def _load(sch_path: Path) -> Optional[_SheetFile]:
        """Parse a file once, however many instances use it."""
        if sch_path not in parsed_files:
            roots = parse(sch_path.read_text(encoding="utf-8"))
            root = roots[0] if roots and isinstance(roots[0], list) else None
            parsed_files[sch_path] = None if root is None else _SheetFile(
                root=root,
                definitions=_collect_file_data(root, symbol_defs, global_nets),
                no_connects={_point_key(x, y) for x, y in _no_connect_points(root)},
            )
        return parsed_files[sch_path]

    def _walk(sch_path: Path, segments: tuple[str, ...], ancestry: tuple[Path, ...],
              attributes: _Attributes) -> None:
        sch_path = sch_path.resolve()
        sheet_file = _load(sch_path)
        if sheet_file is None:
            return
        root = sheet_file.root
        if not segments:
            segments = (_uuid_of(root),)
        sheet_path = "/" + "/".join(segments)

        for sym_node in find_all(root, "symbol"):
            instance = _symbol_instance(sym_node, sheet_path, segments, sheet_file, symbol_defs, attributes)
            if instance is not None:
                instances.append(instance)

        for sheet_node in find_all(root, "sheet"):
            written = _sheet_filename(sheet_node)
            if written:
                _follow(sheet_node, written, sch_path, segments + (_uuid_of(sheet_node),),
                        ancestry + (sch_path,), attributes.within(sheet_node))

    def _follow(sheet_node, written, parent_path, segments, ancestry, attributes) -> None:
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
            _walk(child, segments, ancestry, attributes)
        except (OSError, UnicodeDecodeError) as exc:
            issue(UNREADABLE_SHEET, f"{written} could not be read ({exc}); "
                                    "the symbols of this sheet are not read.")

    _walk(root_sch_path, (), (), _Attributes())
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


@dataclass
class _SheetFile:
    root: list
    definitions: dict[str, SymbolDef]   # this file's own lib_symbols
    no_connects: set[tuple[int, int]]   # marker positions, see _point_key


@dataclass(frozen=True)
class _Attributes:
    """Exclusions a sheet passes to everything placed inside it."""
    in_bom: bool = True
    on_board: bool = True
    dnp: bool = False

    def within(self, node: list) -> "_Attributes":
        return _Attributes(
            in_bom=self.in_bom and _yes_no(node, "in_bom", True),
            on_board=self.on_board and _yes_no(node, "on_board", True),
            dnp=self.dnp or _yes_no(node, "dnp", False),
        )


def _collect_file_data(root: list, symbol_defs: dict[str, SymbolDef],
                       global_nets: list[str]) -> dict[str, SymbolDef]:
    """Library definitions and global labels, which do not depend on the instance.

    Returns the file's own definitions. Each sheet embeds the definitions it
    uses, and two sheets can embed different symbols under one name; a placed
    symbol is resolved against its own file first.
    """
    definitions: dict[str, SymbolDef] = {}
    lib_syms_node = find(root, "lib_symbols")
    if lib_syms_node:
        for sym_node in find_all(lib_syms_node, "symbol"):
            if not sym_node or len(sym_node) < 2:
                continue
            lib_id = sym_node[1] if isinstance(sym_node[1], str) else ""
            if not lib_id or lib_id in definitions:
                continue
            definitions[lib_id] = _parse_sym_def(lib_id, sym_node)
            symbol_defs.setdefault(lib_id, definitions[lib_id])

    for gl_node in find_all(root, "global_label"):
        if len(gl_node) >= 2 and isinstance(gl_node[1], str):
            name = gl_node[1]
            if name and name not in global_nets:
                global_nets.append(name)
    return definitions


def _symbol_instance(sym_node: list, sheet_path: str, segments: tuple[str, ...],
                     sheet_file: _SheetFile, symbol_defs: dict[str, SymbolDef],
                     attributes: _Attributes):
    """One placed symbol under one sheet instance, or None for a non-symbol node."""
    if not sym_node or len(sym_node) < 2:
        return None
    lib_id_node = find(sym_node, "lib_id")
    if lib_id_node is None:
        return None  # a bare library definition, not a placed symbol
    lib_id = lib_id_node[1] if len(lib_id_node) > 1 else ""
    lib_name = _str_atom(find(sym_node, "lib_name"), "")

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
    # KiCad 9 renamed (convert N) to (body_style N).
    body_style = _int_atom(find(sym_node, "body_style") or find(sym_node, "convert"), 1)

    definition = _resolve_definition(lib_name or lib_id, sheet_file.definitions, symbol_defs)
    pins = list(definition.pins) if definition else []
    placed = attributes.within(sym_node)
    instance = SymbolInstance(
        lib_id=lib_id,
        uuid=uuid,
        reference=reference,
        value=props.get("Value", ""),
        footprint=props.get("Footprint", ""),
        datasheet=props.get("Datasheet", ""),
        extra_fields=extra,
        sheet_path=sheet_path,
        pins=pins,
        footprint_path="/" + "/".join(segments[1:] + (uuid,)),
        unit=unit,
        body_style=body_style,
        lib_name=lib_name,
        power=definition.power if definition else "",
        in_bom=placed.in_bom,
        on_board=placed.on_board,
        dnp=placed.dnp,
    )
    if sheet_file.no_connects:
        placement = _placement(sym_node)
        instance.no_connect_pins = {
            pin.number for pin in instance.unit_pins()
            if _point_key(*_place(pin.x, pin.y, placement)) in sheet_file.no_connects
        }
    return instance


def _no_connect_points(root: list) -> list[tuple[float, float]]:
    points = []
    for marker in find_all(root, "no_connect"):
        at = find(marker, "at")
        if at is not None and len(at) >= 3:
            try:
                points.append((float(at[1]), float(at[2])))
            except ValueError:
                continue
    return points


def _placement(sym_node: list) -> tuple[float, float, int, str]:
    """Position, rotation in degrees and mirror axis of a placed symbol."""
    at = find(sym_node, "at")
    values = [float(v) for v in at[1:4]] if at is not None else []
    x, y, angle = (values + [0.0, 0.0, 0.0])[:3]
    return x, y, int(round(angle)) % 360, _str_atom(find(sym_node, "mirror"), "")


_QUARTER_TURNS = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}  # (cos, sin)


def _place(px: float, py: float, placement: tuple[float, float, int, str]) -> tuple[float, float]:
    """A library point on the sheet.

    Library Y points up and the sheet's down; the symbol turns counter-clockwise
    as seen on the sheet, then ``(mirror x)`` flips it top to bottom and
    ``(mirror y)`` left to right. Chosen among the eight readings of rotation
    direction, mirror axis and order by where pins land on the KiCad 10 demos:
    this one puts 6,756 of the 7,368 pins of turned or mirrored symbols on a
    wire end, label, junction or no-connect marker, and wins in every
    turn-and-mirror combination at once; the next best reaches 5,629.
    """
    x0, y0, angle, mirror = placement
    cos, sin = _QUARTER_TURNS.get(angle, (1, 0))
    x, y = px, -py
    x, y = x * cos + y * sin, -x * sin + y * cos
    if mirror == "x":
        y = -y
    elif mirror == "y":
        x = -x
    return x0 + x, y0 + y


def _point_key(x: float, y: float) -> tuple[int, int]:
    # Schematic coordinates are written to 0.1 um; compare to the micrometre so
    # that rounding in the placement arithmetic cannot separate a pin from its marker.
    return round(x * 1000), round(y * 1000)


def _yes_no(node: list, tag: str, default: bool) -> bool:
    value = _str_atom(find(node, tag), "")
    return default if value not in ("yes", "no") else value == "yes"


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


_UNIT_SUFFIX_RE = re.compile(r"_(\d+)_(\d+)$")


def _parse_sym_def(lib_id: str, sym_node: list) -> SymbolDef:
    pins: list[PinDef] = []
    _collect_pins_recursive(sym_node, pins, 0, 0)
    power_node = find(sym_node, "power")
    power = ""
    if power_node is not None:
        # (power) before KiCad 9; (power global) or (power local) since.
        power = power_node[1] if len(power_node) > 1 and power_node[1] in ("global", "local") else "global"
    return SymbolDef(lib_id=lib_id, pins=pins, power=power)


def _collect_pins_recursive(node: list, pins: list[PinDef], unit: int, body_style: int) -> None:
    """Walk all sub-nodes collecting (pin <type> ...) definitions.

    A definition draws each unit and body style in a sub-symbol named
    ``<name>_<unit>_<body style>``, 0 standing for all of them.
    """
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
            at = find(child, "at")
            try:
                x, y = (float(at[1]), float(at[2])) if at is not None and len(at) >= 3 else (0.0, 0.0)
            except ValueError:
                x, y = 0.0, 0.0
            pins.append(PinDef(number=pin_num, name=pin_name, electrical_type=etype,
                               unit=unit, body_style=body_style, x=x, y=y))
        elif child[0] == "symbol" and len(child) >= 2 and isinstance(child[1], str):
            match = _UNIT_SUFFIX_RE.search(child[1])
            if match:
                _collect_pins_recursive(child, pins, int(match.group(1)), int(match.group(2)))
            else:
                _collect_pins_recursive(child, pins, unit, body_style)
        else:
            _collect_pins_recursive(child, pins, unit, body_style)


def _collect_properties(sym_node: list) -> dict[str, str]:
    props: dict[str, str] = {}
    for prop_node in find_all(sym_node, "property"):
        if len(prop_node) >= 3:
            key = prop_node[1]
            val = prop_node[2]
            if isinstance(key, str) and isinstance(val, str):
                props[key] = val
    return props


def _resolve_definition(name: str, own: dict[str, SymbolDef],
                        symbol_defs: dict[str, SymbolDef]) -> Optional[SymbolDef]:
    """The definition a placed symbol draws: its ``lib_name`` if it has one, else its ``lib_id``.

    KiCad writes a ``lib_name`` when the embedded symbol no longer matches the
    library's (261 placements across the KiCad 10 demos); the definition is
    embedded under that name, and looking it up by ``lib_id`` found nothing.
    """
    for candidate in (name, _UNIT_SUFFIX_RE.sub("", name)):
        definition = own.get(candidate) or symbol_defs.get(candidate)
        if definition is not None:
            return definition
    return None
