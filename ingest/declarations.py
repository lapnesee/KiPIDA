"""What a project's saved files declare, for analyses that otherwise guess.

docs/plan-universalite.md, P4: the designer's declarations come before
heuristics, with their source. Net names are the last resort.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from .board_reader import read_board
from .netlist_builder import build_netlist
from .project_resolver import BoardProject
from .schematic_reader import read_schematic

# The largest saved board read without being asked. Declarations are read by
# the panels on the GUI thread; measured on the KiCad 10 demos, the 68 MB
# vme-wren board takes 16 s and its schematic 9 s more, the 85 MB Jetson board
# 22 s. The reference board main, about 20 MB, takes 4 s.
AUTOMATIC_READ_LIMIT_MB = 50.0


class DesignReadDeclined(ValueError):
    """A saved board too large to read without the user asking for it."""


_cache: dict = {}
_cache_lock = threading.Lock()


def _stamp(path: Path):
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def design_netlist(project: BoardProject, *, size_limit_mb=AUTOMATIC_READ_LIMIT_MB):
    """The saved board fused with its schematic. Reading errors propagate.

    One project is read once while its files stay as they are: the result is
    kept with the size and modification time of every file read -- the board
    and each sheet -- and read again when any of them changes. A board over
    *size_limit_mb* raises DesignReadDeclined instead (None reads any size).
    """
    pcb_path = Path(project.pcb_path)
    key = (str(pcb_path.resolve()), str(Path(project.sch_root_path).resolve()), project.sub_project_uuid)
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None and all(_stamp(path) == stamp for path, stamp in cached[0]):
        return cached[1]

    size_mb = pcb_path.stat().st_size / 1e6
    if size_limit_mb is not None and size_mb > size_limit_mb:
        raise DesignReadDeclined(
            f"{pcb_path.name} is {size_mb:.0f} MB, over the {size_limit_mb:.0f} MB read "
            "automatically (a board that size takes tens of seconds); its declarations are "
            "not used and names stand in"
        )
    board = read_board(pcb_path)
    schematic = read_schematic(project.sch_root_path) if Path(project.sch_root_path).is_file() else None
    netlist = build_netlist(board, schematic, board_uuid=project.sub_project_uuid or project.pro_path.stem)
    files = [pcb_path] + (list(schematic.files) if schematic is not None else [])
    with _cache_lock:
        _cache.clear()  # one project per session: keep the last design only
        _cache[key] = ([(path, _stamp(path)) for path in files], netlist)
    return netlist


CAPACITOR = "capacitor"
POWERED = "powered"
CONNECTOR = "connector"

# KiCad's capacitor symbols by name (Device:C, C_Small, C_Polarized, CP, CP1...).
_CAPACITOR_SYMBOL_RE = re.compile(r"^(C|CP\d?)(_\w+)?$")
_CAPACITOR_FOOTPRINT_RE = re.compile(r"^Capacitor", re.IGNORECASE)
_CONNECTOR_LIBRARY_RE = re.compile(r"^Connector", re.IGNORECASE)


def component_classes(netlist) -> dict[str, set[str]]:
    """``{reference: classes}`` for the parts the saved files can classify.

    A capacitor by KiCad's capacitor symbols or a capacitor footprint library;
    a connector by a Connector symbol or footprint library; powered when any
    of its pins is power_in, which is what makes a part need decoupling. The
    classes add to a caller's own rule rather than replace it -- a capacitor
    drawn with a part-number symbol from a private library is still one, and a
    vendor symbol typing its supply pins passive still needs decoupling -- but
    a connector is never taken for a part to decouple or for a capacitor.
    Parts with neither a symbol nor a class found are left out.
    """
    classes: dict[str, set[str]] = {}
    for component in netlist.components:
        symbol_library, _, symbol_name = (component.symbol_lib_id or "").partition(":")
        footprint_library = (component.lib_id or "").partition(":")[0]
        found: set[str] = set()
        if _CAPACITOR_SYMBOL_RE.match(symbol_name) or _CAPACITOR_FOOTPRINT_RE.match(footprint_library):
            found.add(CAPACITOR)
        if _CONNECTOR_LIBRARY_RE.match(symbol_library) or _CONNECTOR_LIBRARY_RE.match(footprint_library):
            found.add(CONNECTOR)
        if any(pin_type == "power_in" for pin_type in component.pin_types_by_pad.values()):
            found.add(POWERED)
        if symbol_name or found:
            classes[component.reference] = found
    return classes


def ground_nets(netlist) -> list[str]:
    """The netlist's grounds: those a ground symbol names first, then those only their names suggest."""
    declared = sorted(net.name for net in netlist.nets if net.ground_source == "power_symbol")
    named = sorted(net.name for net in netlist.nets if net.ground_source == "name")
    return declared + named


def with_declared_grounds(reference_net_names, netlist) -> list[str]:
    """*reference_net_names* with the grounds a ground symbol declares added, none removed."""
    names = list(reference_net_names or [])
    for net in netlist.nets:
        if net.ground_source == "power_symbol" and net.name not in names:
            names.append(net.name)
    return names


def supply_rails(netlist) -> dict[str, str]:
    """``{net name: rail source}`` for every net the netlist makes a supply rail.

    A ground is a rail to the netlist -- its symbol is a power symbol, its
    pins power pins -- but not a supply: no DC source feeds it, and meshing
    GND, the widest copper of most boards, bought nothing. On the P02 Rev E
    board GND and two converter analog grounds were proposed as rails.
    """
    return {
        net.name: net.rail_source for net in netlist.nets
        if net.is_power_rail and not net.ground_source
    }


def rail_declarations(project: BoardProject) -> dict[str, str]:
    """``{net name: rail source}`` for every net the saved files make a supply rail.

    The source is ``power_symbol`` when a power symbol names the net and
    ``power_pin`` when only a power pin type does (ingest.netlist_builder).
    Reading errors propagate: the caller decides what a guess costs.
    """
    return supply_rails(design_netlist(project))
