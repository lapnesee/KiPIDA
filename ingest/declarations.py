"""What a project's saved files declare, for analyses that otherwise guess.

docs/plan-universalite.md, P4: the designer's declarations come before
heuristics, with their source. Net names are the last resort.
"""

from __future__ import annotations

import re

from .board_reader import read_board
from .netlist_builder import build_netlist
from .project_resolver import BoardProject
from .schematic_reader import read_schematic


def design_netlist(project: BoardProject):
    """The saved board fused with its schematic. Reading errors propagate."""
    board = read_board(project.pcb_path)
    schematic = read_schematic(project.sch_root_path) if project.sch_root_path.is_file() else None
    return build_netlist(board, schematic, board_uuid=project.sub_project_uuid or project.pro_path.stem)


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


def rail_declarations(project: BoardProject) -> dict[str, str]:
    """``{net name: rail source}`` for every net the saved files make a rail.

    The source is ``power_symbol`` when a power symbol names the net and
    ``power_pin`` when only a power pin type does (ingest.netlist_builder).
    Reading errors propagate: the caller decides what a guess costs.
    """
    netlist = design_netlist(project)
    return {net.name: net.rail_source for net in netlist.nets if net.is_power_rail}
