"""What a project's saved files declare, for analyses that otherwise guess.

docs/plan-universalite.md, P4: the designer's declarations come before
heuristics, with their source. Net names are the last resort.
"""

from __future__ import annotations

from .board_reader import read_board
from .netlist_builder import build_netlist
from .project_resolver import BoardProject
from .schematic_reader import read_schematic


def design_netlist(project: BoardProject):
    """The saved board fused with its schematic. Reading errors propagate."""
    board = read_board(project.pcb_path)
    schematic = read_schematic(project.sch_root_path) if project.sch_root_path.is_file() else None
    return build_netlist(board, schematic, board_uuid=project.sub_project_uuid or project.pro_path.stem)


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
