"""Explain why HybridMesher separates copper the rasterising mesher joins.

Four of seven rails on the reference board strand load nodes in the advisor's
mesh while the production DC mesher reports no exclusions at all, on the same
board at the same grid. One of the two is wrong about the copper.

This reports the connected components of a HybridMesher net mesh, which
component holds the source and which holds each load, and how far apart the
nearest nodes of two components are. A gap of one grid step means the mesher
failed to join adjacent copper; a gap of millimetres means the copper really is
separate and the load is genuinely unreachable.

    python validation/mesh_connectivity.py <board.kicad_pcb> <net>

Read-only.
"""

import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advisor.dc_advisor import build_net_mesh  # noqa: E402
from advisor.rail_sources import (  # noqa: E402
    map_pads_to_nodes, resolve_rail_source, solver_loads,
)
from config_manager import load_config  # noqa: E402
from ingest.board_reader import read_board  # noqa: E402

GRID_STEP_MM = 0.1


def components(mesh):
    """Label every node with the id of the component it belongs to."""
    adjacency = defaultdict(list)
    for branch in mesh.branches:
        adjacency[branch.node_a].append(branch.node_b)
        adjacency[branch.node_b].append(branch.node_a)
    label = {}
    for start in mesh.node_coords:
        if start in label:
            continue
        current = len(set(label.values()))
        stack, label[start] = [start], current
        while stack:
            node = stack.pop()
            for neighbour in adjacency[node]:
                if neighbour not in label:
                    label[neighbour] = current
                    stack.append(neighbour)
    return label


def nearest_gap(mesh, label, first, second, limit=4000):
    """Smallest planar distance between two components, and the pair of nodes.

    Bounded: comparing two large components exhaustively is quadratic, so each
    side is sampled. The number reported is therefore an upper bound on the
    true gap -- which is the safe direction, since a small value proves
    adjacency while a large one only suggests separation.
    """
    left = [n for n, c in label.items() if c == first][:limit]
    right = [n for n, c in label.items() if c == second][:limit]
    best = None
    for a in left:
        ax, ay, al = mesh.node_coords[a]
        for b in right:
            bx, by, bl = mesh.node_coords[b]
            distance = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
            if best is None or distance < best[0]:
                best = (distance, a, b, al, bl)
    return best


def _same_layer_gap(mesh, label, members, target_component, limit=6000):
    """Closest node of *target_component* lying on the same layer as a member."""
    layers = {mesh.node_coords[n][2] for n in members}
    candidates = [
        n for n, c in label.items()
        if c == target_component and mesh.node_coords[n][2] in layers
    ][:limit]
    if not candidates:
        return None
    best = None
    for a in members:
        ax, ay, al = mesh.node_coords[a]
        for b in candidates:
            bx, by, bl = mesh.node_coords[b]
            if bl != al:
                continue
            distance = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
            if best is None or distance < best[0]:
                best = (distance, a, b, al)
    return best


def _report_nearby_vias(board, net_name, mesh, members, radius_mm=0.5):
    """Vias of this net near the stranded copper.

    If a via sits right there and the node is still isolated, the mesher saw
    the via and failed to bind it; if there is no via, the board itself has no
    vertical connection at that point and the load really is on its own island.
    """
    found = []
    for via in board.vias:
        if via.net_name != net_name:
            continue
        vx, vy = via.position
        for node in members:
            nx, ny, _layer = mesh.node_coords[node]
            distance = ((vx - nx) ** 2 + (vy - ny) ** 2) ** 0.5
            if distance <= radius_mm:
                found.append((distance, vx, vy, tuple(via.layers)))
                break
    found.sort()
    if not found:
        print(f"      no via of this net within {radius_mm} mm of the stranded copper")
        return
    for distance, vx, vy, layers in found[:4]:
        print(f"      via at ({vx:.3f}, {vy:.3f}) spanning {layers} "
              f"is {distance:.4f} mm away")


def main(argv):
    if len(argv) < 3:
        raise SystemExit(__doc__)
    board_path, net_name = argv[1], argv[2]

    board = read_board(board_path)
    rails = load_config(os.path.splitext(board_path)[0] + ".kipida.json")
    rail = next(item for item in rails if item.net_name == net_name)

    mesh = build_net_mesh(board, net_name, "", grid_step_mm=GRID_STEP_MM)
    label = components(mesh)
    sizes = defaultdict(int)
    for component in label.values():
        sizes[component] += 1
    print(f"{net_name}: {len(mesh.node_coords):,} nodes, {len(mesh.branches):,} branches, "
          f"{len(sizes):,} connected components")
    ranked = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)
    print("  largest components: " + ", ".join(f"{size:,}" for _c, size in ranked[:6]))
    singletons = sum(1 for _c, size in sizes.items() if size == 1)
    print(f"  isolated single nodes: {singletons:,}")

    source_ref, source_pads, rule = resolve_rail_source(board, rail, rails)
    source_nodes, _unlocated = map_pads_to_nodes(
        mesh, board, source_ref, source_pads,
        net_name=net_name, grid_step_mm=GRID_STEP_MM,
    )
    loads = solver_loads(mesh, board, rail, grid_step_mm=GRID_STEP_MM)
    source_components = {label[n] for n in source_nodes}
    print(f"  source {source_ref} ({rule}) sits in component(s) {sorted(source_components)}")

    for item in loads:
        node = item["node_id"]
        component = label[node]
        x, y, layer = mesh.node_coords[node]
        status = "reachable" if component in source_components else "STRANDED"
        print(f"  load node {node} at ({x:.3f}, {y:.3f}) layer {layer} "
              f"-> component {component} (size {sizes[component]:,}) {status}")
        if component not in source_components:
            members = [n for n, c in label.items() if c == component]
            print(f"      component members: " + ", ".join(
                f"{n}@({mesh.node_coords[n][0]:.3f},{mesh.node_coords[n][1]:.3f})"
                f"L{mesh.node_coords[n][2]}" for n in members[:8]
            ))
            gap = nearest_gap(mesh, label, component, min(source_components))
            if gap:
                distance, a, b, layer_a, layer_b = gap
                print(f"      nearest approach (any layer): {distance:.4f} mm "
                      f"(node {a} L{layer_a} <-> node {b} L{layer_b})")
            # Same-layer proximity is the one a planar join could fix; a
            # cross-layer gap needs a via instead, and the two call for
            # different repairs. Reporting only the closest pair would hide
            # which of the two this actually is.
            same = _same_layer_gap(mesh, label, members, min(source_components))
            if same:
                distance, a, b, layer = same
                print(f"      nearest approach on the SAME layer: {distance:.4f} mm "
                      f"(node {a} <-> node {b} on L{layer})")
                print("      -> " + ("planar join failed: same layer, under one grid step"
                                     if distance <= GRID_STEP_MM
                                     else "no same-layer neighbour within a grid step"))
            else:
                print("      -> the source component has no node on these layers at all;"
                      " the missing link is vertical (a via), not planar")
            _report_nearby_vias(board, net_name, mesh, members)


if __name__ == "__main__":
    main(sys.argv)
