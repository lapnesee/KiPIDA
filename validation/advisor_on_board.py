"""Exercise the DC advisor against a real board with a chosen drop target.

The stitching-via action could not be reached from a normal run of the
reference board: +5V_RAIL drops 7.7 mV against a 100 mV budget, so no DC-003
finding is raised and the advisor is never called. Removing fifteen vias moved
the drop by 0.6 mV, because a 108,000-node plane carries the current and the
vias are not the bottleneck.

This drives the same code path with the target supplied on the command line,
so the action can be exercised on real copper instead of only on synthetic
BranchLoss objects.

    python validation/advisor_on_board.py <board.kicad_pcb> <net> <target_v>

Nothing is written; the board is opened read-only.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advisor.dc_advisor import build_dc_remediations, build_net_mesh  # noqa: E402
from advisor.rail_sources import (  # noqa: E402
    map_pads_to_nodes, resolve_rail_source, solver_loads, unreachable_nodes,
)
from config_manager import load_config  # noqa: E402
from ingest.board_reader import read_board  # noqa: E402

GRID_STEP_MM = 0.1


def main(argv):
    if len(argv) < 4:
        raise SystemExit(__doc__)
    board_path, net_name, target = argv[1], argv[2], float(argv[3])

    board = read_board(board_path)
    sidecar = os.path.splitext(board_path)[0] + ".kipida.json"
    rails = load_config(sidecar)
    rail = next((item for item in rails if item.net_name == net_name), None)
    if rail is None:
        raise SystemExit(f"{net_name} is not configured in {sidecar}")

    print(f"net={net_name} nominal={rail.nominal_voltage} V target drop={target} V")
    source_ref, source_pads, rule = resolve_rail_source(board, rail, rails)
    print(f"source: {source_ref} via {rule}")
    if source_ref is None:
        raise SystemExit("no source resolved")

    mesh = build_net_mesh(board, net_name, "", grid_step_mm=GRID_STEP_MM)
    print(f"mesh: {len(mesh.nodes):,} nodes, {len(mesh.branches):,} branches")

    source_nodes, unlocated = map_pads_to_nodes(
        mesh, board, source_ref, source_pads,
        net_name=net_name, grid_step_mm=GRID_STEP_MM,
    )
    loads = solver_loads(mesh, board, rail, grid_step_mm=GRID_STEP_MM)
    print(f"source nodes={len(source_nodes)} loads={len(loads)} unlocated={unlocated}")
    stranded = unreachable_nodes(
        mesh, source_nodes, [item["node_id"] for item in loads],
    )
    if stranded:
        raise SystemExit(f"{len(stranded)} load node(s) unreachable; drop would be fictitious")

    # map_pads_to_nodes returns bare node ids; the solver wants dicts carrying
    # the driven voltage, exactly as attach_dc_remediations builds them.
    sources = [
        {"node_id": node_id, "voltage": float(rail.nominal_voltage)}
        for node_id in source_nodes
    ]
    remediations = build_dc_remediations(
        board, net_name, "", sources, loads,
        target_drop_v=target, grid_step_mm=GRID_STEP_MM,
        verify=False, log_callback=lambda message: print(f"  [advisor] {message}"),
    )
    print()
    print(f"{len(remediations)} remediation(s):")
    for item in remediations:
        print(f"  {item.action}: {item.target}")
        print(f"    {item.current_value:g} -> {item.proposed_value:g} {item.unit}")
        print(f"    {item.predicted_gain}")
        print(f"    effort={item.effort} verified={item.verified}")
        for alternative in item.alternatives:
            print(f"    alt: {alternative}")


if __name__ == "__main__":
    main(sys.argv)
