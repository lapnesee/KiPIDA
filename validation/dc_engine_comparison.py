"""Compare the raster and hybrid DC engines on the same copper (plan step 5.3).

    python validation/dc_engine_comparison.py --synthetic [--step 0.1]
    python validation/dc_engine_comparison.py BOARD.kicad_pcb SIDECAR.kipida.json [--step 0.1]

Both engines solve copper read from one board file. The hybrid engine reads it
itself. The raster engine is handed what GeometryExtractor captures from a
live board -- tracks buffered to their width with round ends, pad outlines, via
lands and pour fills per copper layer, plus the pad and via snapshot -- built
here from the parsed file, so any difference between the two results is the
meshing, not the input. Copper layers are numbered top to bottom for both, as
the live stackup numbers them, and the raster stackup spreads the board
thickness evenly over its dielectrics so a via barrel is as tall in both.

The gate the plan fixed before any measurement (docs/plan-recommandations-audit.md):

  1. the hybrid engine within 1 % of the analytic drop on synthetic tracks,
     straight and at 45 degrees;
  2. every configured rail of the board solved by the hybrid engine with no
     load isolated from its source;
  3. the hybrid engine's total time at most 3 times the raster engine's;
  4. no rail dominated by pour losses whose worst-load drop differs between
     the engines by more than 10 % without an explanation.

--synthetic measures criterion 1; a board and its sidecar measure 2 to 4.
Nothing is written; the figures and the verdict are printed.
"""

import argparse
import math
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shapely import GeometryCollection, LineString, Point  # noqa: E402

from advisor.rail_sources import (  # noqa: E402
    NODE_MATCH_FLOOR_MM, NODE_MATCH_GRID_FACTOR, NodeIndex, map_pads_to_nodes,
)
from application.dc_controller import (  # noqa: E402
    DCBoardSnapshot, DCFootprintSnapshot, DCNetSnapshot, DCPadSnapshot,
    DCPointSnapshot, DCRunRequest, DCSolverEngine, DCViaSnapshot,
    HybridDCSolverEngine,
)
from ingest.board_reader import (  # noqa: E402
    BoardBounds, CopperLayer, Footprint, Pad, ParsedBoard, Segment, Stackup,
    pad_copper_layers, read_board,
)
from mesh_hybrid import HybridMesher, _pad_outline  # noqa: E402
from models import ComponentRef, PowerRail, UnifiedLoad, UnifiedSource  # noqa: E402
from physical_constants import (  # noqa: E402
    COPPER_RESISTIVITY_OHM_M, COPPER_RESISTIVITY_OHM_MM, DEFAULT_COPPER_THICKNESS_MM,
)

NM = 1e6
SYNTHETIC_LENGTH_MM = 20.0
SYNTHETIC_THICKNESS_MM = 0.035
SYNTHETIC_CURRENT_A = 1.0
GATE_SYNTHETIC_ERROR = 0.01
GATE_TIME_RATIO = 3.0
GATE_PLANE_DIFFERENCE = 0.10


# -- the raster engine's input, built from the file ---------------------------

def layer_numbering(board):
    """Top-to-bottom ids by copper layer name."""
    return {layer.name: index for index, layer in enumerate(board.stackup.layers)}


def live_stackup(board):
    ids = layer_numbering(board)
    layers = board.stackup.layers
    total = board.stackup.total_thickness_mm or 1.6
    dielectric = total / max(1, len(layers) - 1)
    return {
        "copper": {
            ids[layer.name]: {
                "name": layer.name,
                "thickness_mm": layer.thickness_mm or DEFAULT_COPPER_THICKNESS_MM,
            }
            for layer in layers
        },
        "layer_order": [ids[layer.name] for layer in layers],
        "substrate": [
            {"thickness_mm": dielectric, "between": [index, index + 1], "material": "FR4", "epsilon_r": 4.4}
            for index in range(len(layers) - 1)
        ],
        "resistivity": COPPER_RESISTIVITY_OHM_MM,
        "source": "BOARD_FILE",
        "trustworthy": True,
        "warnings": [],
    }


def _via_layers(board, via):
    return [name for name, _layer_id in HybridMesher._layers_spanned(board, via.layers[0], via.layers[-1])]


def raster_geometry(board, net_name):
    ids = layer_numbering(board)
    shapes = {}

    def add(layer_name, shape):
        if layer_name in ids and shape is not None and not shape.is_empty:
            shapes.setdefault(ids[layer_name], []).append(shape)

    for seg in board.segments:
        if seg.net_name == net_name:
            width = seg.width_mm if seg.width_mm > 0 else 0.2
            line = LineString([seg.start, seg.end]) if seg.start != seg.end else Point(seg.start)
            add(seg.layer, line.buffer(width / 2.0))
    for via in board.vias:
        if via.net_name == net_name and len(via.layers) >= 2:
            land = Point(*via.position).buffer((via.size_mm or 0.6) / 2.0)
            for name in _via_layers(board, via):
                add(name, land)
    for footprint in board.footprints:
        for pad in footprint.pads:
            if pad.net_name == net_name:
                outline = _pad_outline(pad)
                for layer in pad_copper_layers(board.stackup, pad):
                    add(layer.name, outline)
    for zone in board.zones:
        if zone.net_name == net_name:
            polygon = HybridMesher(board)._zone_polygon(zone)
            add(zone.layer, polygon)
    return {layer: GeometryCollection(items) for layer, items in shapes.items()}


def raster_snapshot(board):
    ids = layer_numbering(board)
    footprints = []
    for footprint in board.footprints:
        pads = []
        for pad in footprint.pads:
            drill = DCPointSnapshot(pad.drill_mm * NM, pad.drill_mm * NM) if pad.drill_mm > 0 else None
            pads.append(DCPadSnapshot(
                number=str(pad.number), name=str(pad.number),
                position=DCPointSnapshot(pad.position[0] * NM, pad.position[1] * NM),
                net=DCNetSnapshot(pad.net_name),
                pad_type=1 if pad.pad_type == "thru_hole" else 0,
                drill_size=drill,
                layers=tuple(ids[layer.name] for layer in pad_copper_layers(board.stackup, pad)),
            ))
        footprints.append(DCFootprintSnapshot(footprint.reference, tuple(pads)))
    vias = []
    for via in board.vias:
        if len(via.layers) < 2:
            continue
        point = DCPointSnapshot(via.position[0] * NM, via.position[1] * NM)
        vias.append(DCViaSnapshot(
            net=DCNetSnapshot(via.net_name), position=point, start=point,
            width=(via.size_mm or 0.6) * NM,
            drill_size=DCPointSnapshot(via.drill_mm * NM, via.drill_mm * NM) if via.drill_mm > 0 else None,
            layers=tuple(ids[name] for name in _via_layers(board, via)),
        ))
    return DCBoardSnapshot(tuple(footprints), tuple(vias))


def requests_for(board, board_path, rails, step):
    """(raster request, hybrid request, seconds spent building the raster geometry)."""
    stackup = live_stackup(board)
    started = time.perf_counter()
    geometry = {rail.net_name: raster_geometry(board, rail.net_name) for rail in rails}
    snapshot = raster_snapshot(board)
    preparation = time.perf_counter() - started
    common = dict(
        rails=tuple(rails), classification_geometry_by_rail={}, stackup=stackup,
        grid_size_mm=step, board_path=None if board_path is None else str(board_path),
    )
    raster = DCRunRequest(geometry_by_rail=geometry, board=snapshot, **common)
    hybrid = DCRunRequest(geometry_by_rail={}, board=DCBoardSnapshot((), ()), **common)
    return raster, hybrid, preparation


class RasterLocatedLikeHybrid(DCSolverEngine):
    """The raster engine, with the hybrid engine's pad locator.

    The production raster locator takes every node of a 3 x 3 neighbourhood
    around a pad's centre on every copper layer, and, given no pad list, every
    pad of the component whatever its net. The hybrid locator takes the nodes
    inside the pad's outline, on the pad's own layers and the rail's net.
    Solving the raster mesh with the hybrid's locator leaves the meshing as the
    only difference between the two engines.
    """

    def __init__(self, board):
        super().__init__()
        ids = layer_numbering(board)
        # The raster mesh numbers layers top to bottom; so must the pads.
        self._board = replace(board, stackup=replace(board.stackup, layers=[
            replace(layer, layer_id=ids[layer.name]) for layer in board.stackup.layers
        ]))

    def _locator(self, rail, mesh, request, context, emit):
        tolerance = max(NODE_MATCH_GRID_FACTOR * mesh.grid_step, NODE_MATCH_FLOOR_MM)
        index = NodeIndex(mesh, tolerance)

        def locate(ref_des, pad_names):
            nodes, _unlocated = map_pads_to_nodes(
                mesh, self._board, ref_des, pad_names, net_name=rail.net_name,
                tolerance_mm=tolerance, index=index,
            )
            return nodes

        return locate


def run(engine, request):
    logs = []
    started = time.perf_counter()
    results = engine.solve(request, logs.append, lambda *_: None, lambda: False)
    return results, time.perf_counter() - started, logs


def pour_loss_share(data):
    mesh, detailed = data["mesh"], data["detailed_result"]
    total = sum(detailed.branch_losses_w)
    if total <= 0.0:
        return 0.0
    pour = sum(
        loss for branch, loss in zip(mesh.branches, detailed.branch_losses_w)
        if str(branch.geometry_source).startswith("zone:")
    )
    return pour / total


# -- criterion 1: synthetic tracks --------------------------------------------

def synthetic_board(width_mm, angle_deg):
    rad = math.radians(angle_deg)
    end = (10.0 + SYNTHETIC_LENGTH_MM * math.cos(rad), 10.0 - SYNTHETIC_LENGTH_MM * math.sin(rad))
    stackup = Stackup([CopperLayer(0, "F.Cu", "signal", SYNTHETIC_THICKNESS_MM)], 1.6, 1)

    def pad_at(reference, point):
        # No outline: the criterion is the track, not the land at its ends.
        pad = Pad("1", "VCC", "passive", point, "smd", ("F.Cu",))
        return Footprint(reference, "", "", point, 0.0, "F.Cu", "", "", [pad])

    return ParsedBoard(
        pcb_path=None, stackup=stackup, bounds=BoardBounds(0, 0, 40, 40),
        footprints=[pad_at("U1", (10.0, 10.0)), pad_at("R1", end)],
        segments=[Segment("VCC", width_mm, "F.Cu", (10.0, 10.0), end)],
        vias=[], zones=[],
    )


def synthetic(step):
    rail = PowerRail(
        "VCC", 5.0, sources=[UnifiedSource(ComponentRef("U1"), ["1"])],
        loads=[UnifiedLoad(ComponentRef("R1"), SYNTHETIC_CURRENT_A, ["1"])],
    )
    print("Criterion 1 -- worst-load drop / analytic drop on a 20 mm track")
    print(f"{'width':>6} {'angle':>6} {'hybrid':>9} {'raster':>9}")
    worst = 0.0
    for width in (0.15, 0.25, 0.5, 1.0):
        for angle in (0.0, 45.0):
            board = synthetic_board(width, angle)
            exact = SYNTHETIC_CURRENT_A * COPPER_RESISTIVITY_OHM_M * SYNTHETIC_LENGTH_MM * 1e-3 / (
                width * 1e-3 * SYNTHETIC_THICKNESS_MM * 1e-3
            )
            raster_request, hybrid_request, _ = requests_for(board, "synthetic", [rail], step)
            hybrid, _, _ = run(HybridDCSolverEngine(board_reader=lambda _path, b=board: b), hybrid_request)
            raster, _, _ = run(DCSolverEngine(), raster_request)
            h = hybrid["VCC"]["load_drop_v"] / exact if "VCC" in hybrid else float("nan")
            r = raster["VCC"]["load_drop_v"] / exact if "VCC" in raster and raster["VCC"]["detailed_result"].valid else float("nan")
            worst = max(worst, abs(h - 1.0)) if not math.isnan(h) else float("inf")
            print(f"{width:6.2f} {angle:6.0f} {h:9.4f} {r:9.4f}" + ("" if not math.isnan(r) else "  (raster: load not reached)"))
    passed = worst <= GATE_SYNTHETIC_ERROR
    print(f"Criterion 1: worst hybrid error {worst * 100:.3f} % -> {'PASS' if passed else 'FAIL'} (limit {GATE_SYNTHETIC_ERROR * 100:.0f} %)")
    return passed


# -- criteria 2 to 4: a real board --------------------------------------------

def board_gate(board_path, sidecar, step, explain=False):
    from config_manager import load_config

    started = time.perf_counter()
    board = read_board(Path(board_path))
    read_seconds = time.perf_counter() - started
    rails = load_config(sidecar)
    raster_request, hybrid_request, preparation = requests_for(board, board_path, rails, step)
    hybrid, hybrid_seconds, _ = run(HybridDCSolverEngine(), hybrid_request)
    raster, raster_seconds, _ = run(DCSolverEngine(), raster_request)
    control = run(RasterLocatedLikeHybrid(board), raster_request)[0] if explain else {}

    print(f"{Path(board_path).name}: {len(rails)} rails, step {step} mm")
    print(f"{'rail':14s} {'V':>6} {'hybrid mV':>10} {'raster mV':>10} {'diff':>7} {'pour loss':>9} {'hyb nodes':>9} {'ras nodes':>9}  isolated loads (hybrid / raster)")
    isolated, flagged = [], []
    for rail in rails:
        h, r = hybrid.get(rail.net_name), raster.get(rail.net_name)
        if h is None:
            isolated.append(f"{rail.net_name} (not solved)")
            print(f"{rail.net_name:14s} hybrid did not solve")
            continue
        h_detail = h["detailed_result"]
        h_excluded = list(getattr(h_detail, "excluded_load_references", []))
        if not getattr(h_detail, "valid", True):
            isolated.append(f"{rail.net_name} {h_excluded}")
        share = pour_loss_share(h)
        if r is None:
            print(f"{rail.net_name:14s} {h['source_voltage_v']:6.2f} {h['load_drop_v'] * 1e3:10.3f} {'-':>10} {'-':>7} {share:9.0%} {len(h['mesh'].nodes):9d} {'-':>9}  {h_excluded} / raster did not solve")
            continue
        r_detail = r["detailed_result"]
        r_excluded = list(getattr(r_detail, "excluded_load_references", []))
        difference = (h["load_drop_v"] - r["load_drop_v"]) / r["load_drop_v"] if r["load_drop_v"] > 0 else float("nan")
        if share > 0.5 and not math.isnan(difference) and abs(difference) > GATE_PLANE_DIFFERENCE:
            flagged.append((rail.net_name, difference, share))
        print(
            f"{rail.net_name:14s} {h['source_voltage_v']:6.2f} {h['load_drop_v'] * 1e3:10.3f} {r['load_drop_v'] * 1e3:10.3f} "
            f"{difference:+7.1%} {share:9.0%} {len(h['mesh'].nodes):9d} {len(r['mesh'].nodes):9d}  {h_excluded} / {r_excluded}"
        )
        c = control.get(rail.net_name)
        if c is not None and c["load_drop_v"] > 0:
            print(
                f"{'':14s} {'':6s} {'':10s} {c['load_drop_v'] * 1e3:10.3f} "
                f"{(h['load_drop_v'] - c['load_drop_v']) / c['load_drop_v']:+7.1%}  raster mesh, hybrid pad locator"
                + ("" if getattr(c["detailed_result"], "valid", True) else f", isolated {list(c['detailed_result'].excluded_load_references)}")
            )

    raster_total = raster_seconds + preparation
    hybrid_total = hybrid_seconds
    ratio = hybrid_total / raster_total if raster_total > 0 else float("inf")
    print(
        f"Time: hybrid {hybrid_total:.1f} s (board read {read_seconds:.2f} s included); "
        f"raster {raster_seconds:.1f} s + {preparation:.1f} s geometry = {raster_total:.1f} s; ratio {ratio:.2f}"
    )
    passed_2 = not isolated
    passed_3 = ratio <= GATE_TIME_RATIO
    unexplained = []
    lines = []
    for name, diff, share in flagged:
        c, h = control.get(name), hybrid[name]
        if c is not None and c["load_drop_v"] > 0:
            same_locator = (h["load_drop_v"] - c["load_drop_v"]) / c["load_drop_v"]
            explained = abs(same_locator) <= GATE_PLANE_DIFFERENCE
            lines.append(
                f"\n  {name}: {diff:+.1%} with {share:.0%} of the loss in pours; "
                f"{same_locator:+.1%} once the raster mesh locates pads as the hybrid does"
                + (" -- the pad locator, not the meshing" if explained else "")
            )
            if not explained:
                unexplained.append(name)
        else:
            lines.append(f"\n  {name}: {diff:+.1%} with {share:.0%} of the loss in pours (run with --explain)")
            unexplained.append(name)
    passed_4 = not unexplained
    print(f"Criterion 2: {'PASS' if passed_2 else 'FAIL'}" + ("" if passed_2 else f" -- {'; '.join(isolated)}"))
    print(f"Criterion 3: {'PASS' if passed_3 else 'FAIL'} (hybrid / raster {ratio:.2f}, limit {GATE_TIME_RATIO:.0f})")
    print(
        f"Criterion 4: {'PASS' if passed_4 else 'NEEDS AN EXPLANATION'}"
        + (" (differences explained)" if flagged and passed_4 else "") + "".join(lines)
    )
    return passed_2 and passed_3 and passed_4, flagged


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("board", nargs="?")
    parser.add_argument("sidecar", nargs="?")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--step", type=float, default=0.1)
    parser.add_argument(
        "--explain", action="store_true",
        help="also solve the raster mesh with pads injecting on their own layers only",
    )
    args = parser.parse_args()
    if args.synthetic:
        synthetic(args.step)
    if args.board and args.sidecar:
        board_gate(args.board, args.sidecar, args.step, explain=args.explain)
    if not args.synthetic and not (args.board and args.sidecar):
        parser.error("give --synthetic, or a board and its sidecar")


if __name__ == "__main__":
    main()
