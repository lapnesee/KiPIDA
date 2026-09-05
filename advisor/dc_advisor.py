"""Quantified DC remediation: rank loss contributors, size the fix, re-simulate.

A finding that says "increase copper cross-section" is not actionable. This
module answers: which segments, from what width to what width, and what the
resulting drop actually becomes once re-solved -- not estimated, re-solved.

Voltage-drop convention
-----------------------
``max(V) - min(V)`` over a whole net is the statistic the refactor audit
criticises: it reports the worst pair of points anywhere on the copper, which
may be two unrelated stubs. Everything here instead measures **the drop each
load actually sees**: ``V_rail - V(load_node)``, maximised over the load
nodes. ``V_rail`` is the highest source voltage in the system, i.e. the rail
being studied rather than its 0 V return.

Node-id stability
-----------------
:meth:`~mesh_hybrid.HybridMesher.build_mesh` assigns node ids in geometric
discovery order, keyed by snapped ``(x, y, layer_id)``. Changing only segment
*widths* changes conductances, never coordinates or iteration order, so node
ids and branch ordering are stable across a what-if width change. That is what
makes it valid to reuse a caller's ``sources``/``loads`` node ids after
re-meshing, and to zip old and new branch lists together for the first-order
prediction. :func:`find_node_at` is provided so callers need not guess ids.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, replace
from typing import Callable, Optional

try:
    from ..analysis_contract import Remediation, RemediationEffort
except (ImportError, ValueError):
    from analysis_contract import Remediation, RemediationEffort

try:
    from ..ingest.board_reader import ParsedBoard, Segment
    from ..ingest.track_resistance import RHO_COPPER
    from ..mesh import Mesh
    from ..mesh_hybrid import HybridMesher
    from ..solver import Solver
except (ImportError, ValueError):
    from ingest.board_reader import ParsedBoard, Segment
    from ingest.track_resistance import RHO_COPPER
    from mesh import Mesh
    from mesh_hybrid import HybridMesher
    from solver import Solver


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class BranchLoss:
    """One mesh branch's share of the dissipated power."""
    branch_index: int
    resistance_ohm: float
    current_a: float
    power_w: float                # R * I^2
    geometry_source: str          # "seg:F.Cu", "via:F.Cu-B.Cu", "zone:In1.Cu:h"
    node_a: int
    node_b: int


@dataclass
class WhatIfOutcome:
    """Baseline, first-order prediction, and re-simulated result of a change."""
    baseline_drop_v: float
    predicted_drop_v: float       # first-order estimate, before re-solve
    resimulated_drop_v: float     # after re-solve with the change applied
    prediction_error_pct: float   # |predicted - resimulated| / resimulated * 100
    converged: bool


# ---------------------------------------------------------------------------
# Mesh helpers
# ---------------------------------------------------------------------------


def find_node_at(
    mesh: Mesh, x_mm: float, y_mm: float,
    layer_id: Optional[int] = None, tolerance_mm: float = 0.005,
) -> Optional[int]:
    """Return the node id nearest ``(x_mm, y_mm)`` within *tolerance_mm*.

    Callers use this to turn a pad/probe coordinate into the node id that
    :class:`~solver.Solver` expects, without depending on mesh build order.
    Returns ``None`` when no node lies within the tolerance.
    """
    best_id, best_d2 = None, tolerance_mm * tolerance_mm
    for node_id, coord in mesh.node_coords.items():
        if layer_id is not None and len(coord) > 2 and coord[2] != layer_id:
            continue
        dx, dy = coord[0] - x_mm, coord[1] - y_mm
        d2 = dx * dx + dy * dy
        if d2 <= best_d2:
            best_id, best_d2 = node_id, d2
    return best_id


def merge_meshes(mesh_a: Mesh, mesh_b: Mesh) -> Mesh:
    """Combine two independently built meshes into one system.

    Node ids of *mesh_b* are offset past those of *mesh_a*. The two copper
    graphs stay galvanically separate: the caller couples them by injecting
    ``+I`` at a rail node and ``-I`` at the matching return node, which is the
    rail+return loop formulation the audit asks for in §3.2.

    ``mesh_b``'s node ids in the merged mesh are ``original_id + offset``,
    where ``offset = max(mesh_a.nodes) + 1`` (0 when *mesh_a* is empty).
    """
    merged = Mesh()
    offset = (max(mesh_a.nodes) + 1) if mesh_a.nodes else 0

    merged.nodes = list(mesh_a.nodes) + [n + offset for n in mesh_b.nodes]
    merged.node_coords = dict(mesh_a.node_coords)
    for node_id, coord in mesh_b.node_coords.items():
        merged.node_coords[node_id + offset] = coord

    merged.branches = list(mesh_a.branches)
    for branch in mesh_b.branches:
        merged.branches.append(replace(
            branch,
            node_a=branch.node_a + offset,
            node_b=branch.node_b + offset,
        ))

    merged.G_coo_data = list(mesh_a.G_coo_data) + list(mesh_b.G_coo_data)
    merged.G_coo_row = list(mesh_a.G_coo_row) + [r + offset for r in mesh_b.G_coo_row]
    merged.G_coo_col = list(mesh_a.G_coo_col) + [c + offset for c in mesh_b.G_coo_col]
    return merged


def build_net_mesh(
    parsed_board: ParsedBoard, net_name: str,
    ground_net_name: str = "", *,
    grid_step_mm: float = 0.1, rho: float = RHO_COPPER,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Mesh:
    """Mesh *net_name*, optionally merged with its return net.

    When *ground_net_name* is empty or has no copper, only the rail net is
    meshed and the returned mesh is exactly ``HybridMesher.build_mesh(net)``.
    """
    mesher = HybridMesher(
        parsed_board, grid_step_mm=grid_step_mm,
        log_callback=log_callback, rho=rho,
    )
    rail = mesher.build_mesh(net_name)
    if not ground_net_name:
        return rail
    ground = mesher.build_mesh(ground_net_name)
    if not ground.nodes:
        return rail
    return merge_meshes(rail, ground)


# ---------------------------------------------------------------------------
# Loss ranking
# ---------------------------------------------------------------------------


def rank_branch_losses(
    mesh: Mesh, node_voltages: dict, *, limit: int = 0,
) -> list[BranchLoss]:
    """Per-branch ``I = (V_a - V_b) / R`` and ``P = R*I^2``, worst first.

    ``limit=0`` returns every branch. Branches whose endpoints are absent from
    *node_voltages* (floating copper the solver excluded) are skipped, as are
    branches with non-positive resistance.
    """
    losses: list[BranchLoss] = []
    for index, branch in enumerate(getattr(mesh, "branches", [])):
        v_a = node_voltages.get(branch.node_a)
        v_b = node_voltages.get(branch.node_b)
        if v_a is None or v_b is None:
            continue
        resistance = float(branch.resistance_ohm)
        if resistance <= 0.0:
            continue
        current = (v_a - v_b) / resistance
        losses.append(BranchLoss(
            branch_index=index,
            resistance_ohm=resistance,
            current_a=current,
            power_w=resistance * current * current,
            geometry_source=str(branch.geometry_source or ""),
            node_a=branch.node_a,
            node_b=branch.node_b,
        ))
    losses.sort(key=lambda item: item.power_w, reverse=True)
    return losses[:limit] if limit and limit > 0 else losses


def dominant_path_share(
    losses: list[BranchLoss], fraction: float = 0.8,
) -> list[BranchLoss]:
    """Smallest prefix of *losses* carrying at least *fraction* of the power.

    Turns "increase copper cross-section" into "62 % of the loss is on these
    8 branches". *losses* is assumed already sorted worst-first, as returned
    by :func:`rank_branch_losses`.
    """
    total = sum(item.power_w for item in losses)
    if total <= 0.0:
        return []
    target = fraction * total
    running, prefix = 0.0, []
    for item in losses:
        prefix.append(item)
        running += item.power_w
        if running >= target:
            break
    return prefix


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------


def required_width_for_target_drop(
    current_width_mm: float, actual_drop_v: float, target_drop_v: float,
) -> float:
    """First-order sizing: ``R ∝ 1/w``, so ``w' = w · (drop_actual / drop_target)``.

    Raises:
        ValueError: for non-positive inputs, or when *target_drop_v* is not
            below *actual_drop_v* (there is nothing to fix).
    """
    if current_width_mm <= 0:
        raise ValueError(f"current_width_mm must be > 0, got {current_width_mm}")
    if actual_drop_v <= 0:
        raise ValueError(f"actual_drop_v must be > 0, got {actual_drop_v}")
    if target_drop_v <= 0:
        raise ValueError(f"target_drop_v must be > 0, got {target_drop_v}")
    if target_drop_v >= actual_drop_v:
        raise ValueError(
            f"target_drop_v ({target_drop_v}) must be below actual_drop_v "
            f"({actual_drop_v}); nothing to fix"
        )
    return current_width_mm * (actual_drop_v / target_drop_v)


# ---------------------------------------------------------------------------
# Drop measurement
# ---------------------------------------------------------------------------


def load_drop_v(node_voltages: dict, sources: list, loads: list) -> float:
    """Worst drop actually seen by a load: ``max(V_rail - V(load_node))``.

    ``V_rail`` is the highest source voltage present in the solution, so a
    rail+return system referenced to 0 V still measures against the rail.
    Returns 0.0 when the solution has no usable source or load node.
    """
    source_voltages = [
        node_voltages[s["node_id"]] for s in sources
        if s.get("node_id") in node_voltages
    ]
    if not source_voltages:
        return 0.0
    v_rail = max(source_voltages)
    drops = [
        v_rail - node_voltages[l["node_id"]] for l in loads
        if l.get("node_id") in node_voltages
        and abs(float(l.get("current", 0.0))) > 0.0
    ]
    return max(drops) if drops else 0.0


# ---------------------------------------------------------------------------
# What-if
# ---------------------------------------------------------------------------


def _board_with_widths(
    parsed_board: ParsedBoard,
    segment_predicate: Callable[[Segment], bool],
    new_width_mm: float,
) -> ParsedBoard:
    """Shallow-copy the board with matching segments re-widened.

    The caller's board is never mutated: matching ``Segment`` objects are
    rebuilt with :func:`dataclasses.replace` into a fresh list.
    """
    new_segments = [
        replace(seg, width_mm=new_width_mm) if segment_predicate(seg) else seg
        for seg in parsed_board.segments
    ]
    modified = copy.copy(parsed_board)
    modified.segments = new_segments
    return modified


def simulate_width_change(
    parsed_board: ParsedBoard, net_name: str, ground_net_name: str,
    sources: list, loads: list,
    segment_predicate: Callable[[Segment], bool], new_width_mm: float,
    *, grid_step_mm: float = 0.1, solver=None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> WhatIfOutcome:
    """Re-mesh and re-solve with matching segments widened, and report the gain.

    *segment_predicate* selects which physical segments the proposed change
    applies to. The board is never mutated (see :func:`_board_with_widths`);
    the caller's ``ParsedBoard`` is byte-identical afterwards.

    Both a first-order prediction and the re-simulated result are returned, so
    the caller can see how good the estimate was. The prediction holds branch
    currents fixed at their baseline values and subtracts the resistance saved
    on each changed branch — exact for a series path, an approximation once
    current redistributes.
    """
    solver = solver or Solver()

    baseline_mesh = build_net_mesh(
        parsed_board, net_name, ground_net_name,
        grid_step_mm=grid_step_mm, log_callback=log_callback,
    )
    baseline_v = solver.solve(baseline_mesh, sources, loads)
    if not baseline_v:
        return WhatIfOutcome(0.0, 0.0, 0.0, 0.0, converged=False)
    baseline_drop = load_drop_v(baseline_v, sources, loads)

    modified_board = _board_with_widths(parsed_board, segment_predicate, new_width_mm)
    modified_mesh = build_net_mesh(
        modified_board, net_name, ground_net_name,
        grid_step_mm=grid_step_mm, log_callback=log_callback,
    )

    # First-order prediction: hold baseline currents, credit the saved
    # resistance on every branch whose resistance actually changed.
    predicted_drop = baseline_drop
    old_branches = baseline_mesh.branches
    new_branches = modified_mesh.branches
    if len(old_branches) == len(new_branches):
        for index, (old, new) in enumerate(zip(old_branches, new_branches)):
            delta_r = float(old.resistance_ohm) - float(new.resistance_ohm)
            if abs(delta_r) <= 0.0:
                continue
            v_a = baseline_v.get(old.node_a)
            v_b = baseline_v.get(old.node_b)
            if v_a is None or v_b is None or old.resistance_ohm <= 0:
                continue
            current = (v_a - v_b) / float(old.resistance_ohm)
            predicted_drop -= delta_r * abs(current)
    predicted_drop = max(0.0, predicted_drop)

    resimulated_v = solver.solve(modified_mesh, sources, loads)
    if not resimulated_v:
        return WhatIfOutcome(baseline_drop, predicted_drop, 0.0, 0.0, converged=False)
    resimulated_drop = load_drop_v(resimulated_v, sources, loads)

    error_pct = (
        abs(predicted_drop - resimulated_drop) / resimulated_drop * 100.0
        if resimulated_drop > 0.0 else 0.0
    )
    return WhatIfOutcome(
        baseline_drop_v=baseline_drop,
        predicted_drop_v=predicted_drop,
        resimulated_drop_v=resimulated_drop,
        prediction_error_pct=error_pct,
        converged=True,
    )


# ---------------------------------------------------------------------------
# End-to-end remediation building
# ---------------------------------------------------------------------------


def _effort_for_ratio(ratio: float) -> RemediationEffort:
    """Widening under 2x is routine; past 4x it usually means a reroute."""
    if ratio < 2.0:
        return RemediationEffort.LOW
    if ratio < 4.0:
        return RemediationEffort.MEDIUM
    return RemediationEffort.HIGH


def _layer_of(geometry_source: str) -> str:
    """``"seg:F.Cu"`` -> ``"F.Cu"``; empty for via/zone/unknown sources."""
    if geometry_source.startswith("seg:"):
        return geometry_source[4:]
    return ""


def _via_pair_of(geometry_source: str) -> str:
    """``"via:F.Cu-In2.Cu"`` -> ``"F.Cu-In2.Cu"``; empty for anything else."""
    if geometry_source.startswith("via:"):
        return geometry_source[4:]
    return ""


def _stitching_via_actions(
    net_name: str, dominant, baseline_drop: float, target_drop_v: float,
    log,
) -> list["Remediation"]:
    """Propose parallel vias when the dominant loss sits in via barrels.

    Until now the advisor could size exactly one thing -- track width -- so a
    rail whose loss lives in its vias got no advice at all. That is the common
    case on a power board: the reference rail's loss was in vias and plane
    copper, and the correct-but-useless answer was silence.

    Sizing is first order and says so. N vias of equal geometry in parallel
    carry R/N, so cutting the via contribution by a factor f needs f times as
    many. That assumes the added vias sit beside the existing ones, span the
    same layer pair, and split the current evenly -- true enough for stitching
    a pour, and wrong if a single via is a bottleneck feeding a long spur.

    Deliberately NOT re-simulated. simulate_width_change rebuilds the board with
    modified segment widths; there is no equivalent for "add a via near this
    one" that does not require inventing a position. Reporting verified=False is
    honest; inventing coordinates so a re-simulation could run would dress a
    guess up as a measurement.
    """
    power_by_pair: dict[str, float] = {}
    for loss in dominant:
        pair = _via_pair_of(loss.geometry_source)
        if pair:
            power_by_pair[pair] = power_by_pair.get(pair, 0.0) + loss.power_w
    if not power_by_pair:
        return []

    total_power = sum(loss.power_w for loss in dominant)
    if total_power <= 0.0:
        return []
    excess = baseline_drop - target_drop_v
    if excess <= 0.0:
        return []

    remediations = []
    for pair, power in sorted(power_by_pair.items(), key=lambda kv: kv[1], reverse=True):
        # Attribute drop in proportion to dissipated power along the dominant
        # path, which is exact for a series path and approximate once current
        # redistributes -- the same assumption simulate_width_change documents.
        via_drop = baseline_drop * (power / total_power)
        if via_drop <= excess:
            log(
                f"Vias {pair} on '{net_name}' carry {via_drop:.3f} V of the "
                f"{baseline_drop:.3f} V drop, less than the {excess:.3f} V that "
                "must be removed; adding vias alone cannot reach the target."
            )
            continue
        factor = via_drop / (via_drop - excess)
        count = len([loss for loss in dominant if _via_pair_of(loss.geometry_source) == pair])
        proposed = max(count + 1, int(math.ceil(count * factor)))
        remediations.append(Remediation(
            action="ADD_STITCHING_VIAS",
            target=f"{net_name} / {pair} / {count} via(s) on the dominant path",
            current_value=float(count),
            proposed_value=float(proposed),
            unit="vias",
            predicted_gain=(
                f"drop {baseline_drop:.3f} V -> ~{target_drop_v:.3f} V "
                "(first-order, parallel-resistance estimate, not re-simulated)"
            ),
            effort="LOW" if proposed <= 2 * count else "MEDIUM",
            verified=False,
            layer=pair,
            alternatives=[
                "Increase the finished via diameter instead of the count",
                "Widen the pour feeding these vias so current reaches them evenly",
            ],
        ))
    return remediations


def _zone_layer_of(geometry_source: str) -> str:
    """``"zone:In2.Cu:h"`` -> ``"In2.Cu"``; empty for anything else.

    The mesher tags zone edges with their direction, so the raw suffix is
    ``In2.Cu:h`` or ``In2.Cu:v``. Keeping it would split one pour into two
    half-strength candidates and then fail the stackup thickness lookup, since
    no layer is named "In2.Cu:h".
    """
    if not geometry_source.startswith("zone:"):
        return ""
    layer = geometry_source[5:]
    if layer.endswith(":h") or layer.endswith(":v"):
        layer = layer[:-2]
    return layer


# Finished copper weights a fabricator will quote, in ounces. Sizing is snapped
# to these rather than reporting an arbitrary thickness: "use 1.4 oz" is not a
# manufacturable instruction, and rounding down would under-deliver the target.
COPPER_WEIGHTS_OZ = (0.5, 1.0, 2.0, 3.0, 4.0)

# Nominal finished thickness of one ounce of copper, mm (IPC-2221).
OUNCE_MM = 0.0348


def _plane_copper_actions(
    parsed_board: ParsedBoard, net_name: str, dominant,
    baseline_drop: float, target_drop_v: float, log,
) -> list["Remediation"]:
    """Propose heavier copper when the dominant loss is spread through a pour.

    This is the case the reference board actually presents: on +5V_RAIL the
    loss sits in 228,153 zone edges against 26 via branches, so neither
    widening a track nor adding vias touches it, and the advisor previously had
    nothing to say at all.

    A pour is a sheet conductor, so its resistance scales as 1/thickness with
    the geometry untouched. Cutting the zone's share of the drop by a factor f
    therefore needs f times the copper weight -- snapped up to a weight a
    fabricator will actually quote.

    The proposal is declined outright when the zone carries less than the
    excess that must be removed, or when the required weight exceeds 4 oz.
    Beyond that the honest answer is that copper weight is the wrong lever and
    the plane needs re-planning, not thickening.

    Not re-simulated. simulate_width_change re-meshes with modified segment
    widths; there is no equivalent for "the same pour, thicker", and the
    stackup change also alters etch compensation and trace geometry the mesher
    does not model. Reporting an unverified first-order estimate is honest;
    re-simulating only the resistance change would look like verification while
    ignoring what else moves.
    """
    power_by_layer: dict[str, float] = {}
    for loss in dominant:
        layer = _zone_layer_of(loss.geometry_source)
        if layer:
            power_by_layer[layer] = power_by_layer.get(layer, 0.0) + loss.power_w
    if not power_by_layer:
        return []

    total_power = sum(loss.power_w for loss in dominant)
    excess = baseline_drop - target_drop_v
    if total_power <= 0.0 or excess <= 0.0:
        return []

    thickness_by_layer = {
        layer.name: layer.thickness_mm
        for layer in parsed_board.stackup.layers
    }

    remediations = []
    for layer, power in sorted(power_by_layer.items(), key=lambda kv: kv[1], reverse=True):
        zone_drop = baseline_drop * (power / total_power)
        if zone_drop <= excess:
            log(
                f"Pour on {layer} carries {zone_drop:.4f} V of the "
                f"{baseline_drop:.4f} V drop, less than the {excess:.4f} V that "
                "must be removed; thicker copper alone cannot reach the target."
            )
            continue
        factor = zone_drop / (zone_drop - excess)
        current_mm = float(thickness_by_layer.get(layer, 0.0) or 0.0)
        if current_mm <= 0.0:
            log(f"Pour on {layer} has no stackup thickness; cannot size copper weight.")
            continue
        current_oz = current_mm / OUNCE_MM
        needed_oz = current_oz * factor
        quotable = next(
            (weight for weight in COPPER_WEIGHTS_OZ if weight >= needed_oz - 1e-9), None,
        )
        if quotable is None:
            log(
                f"Pour on {layer} would need {needed_oz:.2f} oz copper to meet "
                f"the target, beyond the {COPPER_WEIGHTS_OZ[-1]:.0f} oz a "
                "fabricator will normally quote; the plane needs re-planning "
                "rather than thickening."
            )
            continue
        remediations.append(Remediation(
            action="INCREASE_COPPER_WEIGHT",
            target=f"{net_name} / {layer} pour",
            current_value=round(current_oz, 3),
            proposed_value=quotable,
            unit="oz",
            predicted_gain=(
                f"drop {baseline_drop:.4f} V -> ~{target_drop_v:.4f} V "
                "(first-order, sheet-resistance estimate, not re-simulated)"
            ),
            effort="MEDIUM" if quotable <= 2.0 else "HIGH",
            verified=False,
            layer=layer,
            alternatives=[
                "Move the load closer to the regulator",
                "Add a second plane layer for this rail and stitch the two",
            ],
        ))
    return remediations


def build_dc_remediations(
    parsed_board: ParsedBoard, net_name: str, ground_net_name: str,
    sources: list, loads: list, *,
    target_drop_v: float, grid_step_mm: float = 0.1,
    verify: bool = True, max_actions: int = 5,
    log_callback: Optional[Callable[[str], None]] = None,
) -> list[Remediation]:
    """Solve, rank losses, size the fix, optionally re-simulate, and advise.

    With ``verify=True`` each returned :class:`~analysis_contract.Remediation`
    carries ``verified=True`` and a ``predicted_gain`` quoting the
    **re-simulated** drop. With ``verify=False`` (fast path) it carries
    ``verified=False`` and wording that marks the number as a first-order
    estimate. The distinction is deliberate: the project never presents an
    estimate as a measurement.

    Returns an empty list — never raises — when the net has no copper, the
    solve fails, or the drop is already within *target_drop_v*.
    """
    def _log(message: str) -> None:
        if log_callback is not None:
            log_callback(f"[dc_advisor] {message}")

    if target_drop_v <= 0:
        _log(f"target_drop_v must be > 0, got {target_drop_v}; no advice produced.")
        return []

    solver = Solver()
    mesh = build_net_mesh(
        parsed_board, net_name, ground_net_name,
        grid_step_mm=grid_step_mm, log_callback=log_callback,
    )
    if not mesh.nodes or not mesh.branches:
        _log(f"Net '{net_name}' has no meshable copper; no advice produced.")
        return []

    node_voltages = solver.solve(mesh, sources, loads)
    if not node_voltages:
        _log(f"Solver did not converge for net '{net_name}'; no advice produced.")
        return []

    baseline_drop = load_drop_v(node_voltages, sources, loads)
    if baseline_drop <= target_drop_v:
        _log(
            f"Net '{net_name}' drop {baseline_drop:.4f} V already meets the "
            f"{target_drop_v:.4f} V target; no advice produced."
        )
        return []

    dominant = dominant_path_share(rank_branch_losses(mesh, node_voltages))
    if not dominant:
        _log(f"Net '{net_name}' dissipates no power; no advice produced.")
        return []

    # Group the dominant loss by copper layer: one action per layer, worst first.
    power_by_layer: dict[str, float] = {}
    for loss in dominant:
        layer = _layer_of(loss.geometry_source)
        if layer:
            power_by_layer[layer] = power_by_layer.get(layer, 0.0) + loss.power_w
    if not power_by_layer:
        via_actions = _stitching_via_actions(
            net_name, dominant, baseline_drop, target_drop_v, _log,
        )
        if via_actions:
            return via_actions[:max_actions]
        zone_actions = _plane_copper_actions(
            parsed_board, net_name, dominant, baseline_drop, target_drop_v, _log,
        )
        if zone_actions:
            return zone_actions[:max_actions]
        _log(
            f"Dominant loss on net '{net_name}' is not on track segments, not "
            "concentrated in vias, and not on meshable zone copper either; no "
            "action this advisor can size would help."
        )
        return []

    remediations: list[Remediation] = []
    for layer, _power in sorted(
        power_by_layer.items(), key=lambda kv: kv[1], reverse=True
    )[:max_actions]:
        layer_segments = [
            seg for seg in parsed_board.segments
            if seg.net_name == net_name and seg.layer == layer and seg.width_mm > 0
        ]
        if not layer_segments:
            continue
        # The narrowest segment governs the drop, so size against it.
        current_width = min(seg.width_mm for seg in layer_segments)
        try:
            proposed_width = required_width_for_target_drop(
                current_width, baseline_drop, target_drop_v,
            )
        except ValueError as exc:
            _log(f"Cannot size layer {layer}: {exc}")
            continue

        ratio = proposed_width / current_width
        alternatives: list[str] = []
        if ratio > 2.0:
            alternatives = [
                "Add a parallel copper pour on an adjacent layer",
                "Move the load closer to the regulator",
            ]

        def _predicate(seg, _layer=layer, _net=net_name):
            return seg.net_name == _net and seg.layer == _layer and seg.width_mm > 0

        if verify:
            outcome = simulate_width_change(
                parsed_board, net_name, ground_net_name, sources, loads,
                _predicate, proposed_width,
                grid_step_mm=grid_step_mm, solver=solver,
                log_callback=log_callback,
            )
            if not outcome.converged:
                _log(
                    f"What-if re-simulation for layer {layer} did not converge; "
                    "falling back to an unverified first-order estimate."
                )
                gain = (
                    f"drop {baseline_drop:.3f} V -> ~{target_drop_v:.3f} V "
                    "(first-order estimate, not re-simulated)"
                )
                verified = False
            else:
                gain = (
                    f"drop {outcome.baseline_drop_v:.3f} V -> "
                    f"{outcome.resimulated_drop_v:.3f} V (re-simulated)"
                )
                verified = True
        else:
            gain = (
                f"drop {baseline_drop:.3f} V -> ~{target_drop_v:.3f} V "
                "(first-order estimate, not re-simulated)"
            )
            verified = False

        remediations.append(Remediation(
            action="WIDEN_TRACK",
            target=f"{net_name} / {layer} / {len(layer_segments)} segment(s)",
            current_value=current_width,
            proposed_value=proposed_width,
            unit="mm",
            predicted_gain=gain,
            effort=_effort_for_ratio(ratio),
            verified=verified,
            layer=layer,
            alternatives=alternatives,
        ))

    return remediations
