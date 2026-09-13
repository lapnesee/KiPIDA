"""Find what feeds a rail, and turn refdes/pads into solver node ids.

The DC advisor needs two things the power tree does not hand it directly: a
Dirichlet anchor to solve against, and node ids rather than component
references.

On a real power board the rail's feed is a **regulator**, not a
``UnifiedSource``: the reference board declares ``+5V_RAIL`` with zero sources
and one load, while its regulator table says ``+5V_RAIL <- Q3``. Asking only
for a declared source therefore refuses almost every rail, which is what the
advisor did until now.

Resolution is ordered most-factual-first, and every choice carries the rule
that produced it so the log can show the pick was derived rather than guessed.
"""

from __future__ import annotations

import math
from typing import Callable, Optional


# A pad centre rarely coincides with a mesh node: zone cut-cell nodes sit on
# the mesher's grid, and track nodes at segment endpoints. The nearest grid
# node is at most g/sqrt(2) away, so a couple of grid steps leaves margin for
# a pad slightly off the meshed copper while still refusing a match that would
# attach a pad to unrelated copper millimetres away.
NODE_MATCH_GRID_FACTOR = 2.0
NODE_MATCH_FLOOR_MM = 0.2


class NodeIndex:
    """Nearest mesh node to a point, optionally on one layer, without a scan.

    A rail's mesh holds up to 400,000 nodes and a run locates every source,
    load and regulator pad on it; a linear search per pad made placing the
    injections cost more than building the mesh.
    """

    def __init__(self, mesh, cell_mm: float):
        self._cell = max(float(cell_mm), 1e-6)
        self._buckets: dict = {}
        for node_id, coord in getattr(mesh, "node_coords", {}).items():
            key = (math.floor(coord[0] / self._cell), math.floor(coord[1] / self._cell))
            self._buckets.setdefault(key, []).append((node_id, coord))

    def nearest(self, x_mm: float, y_mm: float, tolerance_mm: float,
                layer_id=None) -> Optional[int]:
        reach = int(math.ceil(tolerance_mm / self._cell))
        cx, cy = math.floor(x_mm / self._cell), math.floor(y_mm / self._cell)
        best_id, best_d2 = None, tolerance_mm * tolerance_mm
        for ix in range(cx - reach, cx + reach + 1):
            for iy in range(cy - reach, cy + reach + 1):
                for node_id, coord in self._buckets.get((ix, iy), ()):
                    if layer_id is not None and len(coord) > 2 and coord[2] != layer_id:
                        continue
                    d2 = (coord[0] - x_mm) ** 2 + (coord[1] - y_mm) ** 2
                    if d2 < best_d2 or (d2 == best_d2 and (best_id is None or node_id < best_id)):
                        best_id, best_d2 = node_id, d2
        return best_id


def pad_layer_ids(parsed_board, pad) -> list:
    """Copper layer ids a pad occupies, in the board file's numbering.

    Empty when the board or pad does not say, which callers take as "any
    layer" rather than as "none".
    """
    stackup = getattr(parsed_board, "stackup", None)
    order = [
        (layer.name, layer.layer_id)
        for layer in (getattr(stackup, "layers", ()) or ())
    ]
    declared = {str(name) for name in (getattr(pad, "layers", ()) or ())}
    through = str(getattr(pad, "pad_type", "")).lower() in {"thru_hole", "np_thru_hole"}
    if not order or not (declared or through):
        return []
    if through or "*.Cu" in declared:
        return [layer_id for _name, layer_id in order]
    return [layer_id for name, layer_id in order if name in declared]


def pad_pin_type(pad) -> str:
    """Normalised electrical type of a pad, lowercase.

    KiCad writes compound types in the board file -- ``power_out+no_connect``,
    ``passive+no_connect`` -- so the leading segment is the electrical role and
    the rest is decoration.
    """
    raw = str(getattr(pad, "pintype", "") or "")
    return raw.split("+", 1)[0].strip().lower()


def _declared_source(rail):
    for source in getattr(rail, "sources", ()) or ():
        ref = str(getattr(getattr(source, "component_ref", None), "ref_des", "") or "")
        if ref:
            return ref, [str(name) for name in (getattr(source, "pad_names", ()) or ())]
    return None


def _regulator_output(rail, all_rails):
    """The regulator whose *output* is this rail.

    ``PowerRail.child_regulators`` lists the regulators a rail feeds as their
    input, so the one producing this rail sits on the *upstream* rail's list.
    Scanning only ``rail`` finds nothing; every rail has to be searched.
    """
    net_name = str(getattr(rail, "net_name", ""))
    for candidate in all_rails or ():
        for regulator in getattr(candidate, "child_regulators", ()) or ():
            if str(getattr(regulator, "output_rail_name", "")) != net_name:
                continue
            ref = str(getattr(regulator, "output_ref_des", "") or "")
            if ref:
                return ref, [
                    str(name)
                    for name in (getattr(regulator, "output_pad_names", ()) or ())
                ]
    return None


def _power_output_pad(parsed_board, net_name):
    """A component whose pad on this net is declared a power output."""
    for footprint in getattr(parsed_board, "footprints", ()) or ():
        pads = [
            pad for pad in getattr(footprint, "pads", ()) or ()
            if pad.net_name == net_name and pad_pin_type(pad) == "power_out"
        ]
        if pads:
            return footprint.reference, [str(pad.number) for pad in pads]
    return None


def resolve_rail_source(parsed_board, rail, all_rails=None):
    """Return ``(ref_des, pad_names, rule)`` for the component feeding *rail*.

    Ordered, most factual first:

    1. an explicit ``UnifiedSource`` on the rail       -> ``"declared"``
    2. a regulator whose output is this rail           -> ``"regulator-output"``
    3. a pad on the rail typed ``power_out``           -> ``"pin-type:power_out"``
    4. nothing                                         -> ``(None, [], reason)``

    In case 4 the reason names what was examined, so a refusal explains itself
    instead of the caller falling silent.
    """
    net_name = str(getattr(rail, "net_name", ""))

    found = _declared_source(rail)
    if found:
        return found[0], found[1], "declared"

    found = _regulator_output(rail, all_rails)
    if found:
        return found[0], found[1], "regulator-output"

    found = _power_output_pad(parsed_board, net_name)
    if found:
        return found[0], found[1], "pin-type:power_out"

    regulator_count = sum(
        len(getattr(candidate, "child_regulators", ()) or ())
        for candidate in (all_rails or ())
    )
    return None, [], (
        f"no source found for '{net_name}': the rail declares no source "
        f"component, none of the {regulator_count} known regulator(s) outputs "
        "to it, and no pad on the net is typed power_out"
    )


def component_pads(parsed_board, ref_des, pad_names, net_name=""):
    """The named pads of *ref_des* that sit on *net_name*.

    An empty *pad_names* means every pad of the footprint on *net_name*, which
    is how a regulator entry with no explicit pad list should behave. A
    non-empty list is honoured as given, but pads that are not on *net_name*
    are dropped: a component bridges nets, and only the ones touching this
    rail's copper can anchor a solve on it.
    """
    wanted = {str(name) for name in (pad_names or ())}
    for footprint in getattr(parsed_board, "footprints", ()) or ():
        if footprint.reference != ref_des:
            continue
        return [
            pad for pad in getattr(footprint, "pads", ()) or ()
            if (not net_name or pad.net_name == net_name)
            and (not wanted or str(pad.number) in wanted)
        ]
    return []


def pad_positions(parsed_board, ref_des, pad_names, net_name=""):
    """``[(pad_number, (x_mm, y_mm)), ...]`` for :func:`component_pads`."""
    return [
        (str(pad.number), pad.position)
        for pad in component_pads(parsed_board, ref_des, pad_names, net_name)
    ]


def map_pads_to_nodes(mesh, parsed_board, ref_des, pad_names, *,
                      net_name="", grid_step_mm=0.1, tolerance_mm=None,
                      index=None):
    """Locate a component's pads in *mesh*, on the layers each pad occupies.

    Returns ``(node_ids, unlocated)``, where *unlocated* lists the pad numbers
    that had no mesh node within tolerance. Those are reported rather than
    snapped to whatever happened to be nearest: attaching a pad to unrelated
    copper would move current somewhere it does not flow, and a quietly wrong
    anchor is worse than a refused one.

    The search stays on the pad's own copper layers. Searching every layer let
    a surface pad on F.Cu anchor on the plane node directly beneath it on an
    inner layer, injecting the load past the vias that really carry it. A
    through-hole pad yields one node per layer it reaches: its barrel shorts
    them, and the pin feeds all of them.
    """
    if tolerance_mm is None:
        tolerance_mm = max(NODE_MATCH_GRID_FACTOR * grid_step_mm, NODE_MATCH_FLOOR_MM)
    if index is None:
        index = NodeIndex(mesh, tolerance_mm)
    node_ids, unlocated = [], []
    for pad in component_pads(parsed_board, ref_des, pad_names, net_name):
        x_mm, y_mm = pad.position
        layers = pad_layer_ids(parsed_board, pad) or [None]
        found = [
            node_id for node_id in (
                index.nearest(x_mm, y_mm, tolerance_mm, layer_id) for layer_id in layers
            )
            if node_id is not None
        ]
        if not found:
            unlocated.append(str(pad.number))
        for node_id in found:
            if node_id not in node_ids:
                node_ids.append(node_id)
    return node_ids, unlocated


def unreachable_nodes(mesh, seed_nodes, target_nodes):
    """Which of *target_nodes* no branch path reaches from *seed_nodes*.

    A load the source cannot reach carries no current, and the solver drops it
    from the system. ``load_drop_v`` then finds no load voltage and returns
    0.0 V, which reads exactly like a rail comfortably within target -- a
    fictitious pass. Checking first turns that into something the caller can
    state.
    """
    adjacency = {}
    for branch in getattr(mesh, "branches", ()) or ():
        adjacency.setdefault(branch.node_a, []).append(branch.node_b)
        adjacency.setdefault(branch.node_b, []).append(branch.node_a)

    seen = set(seed_nodes)
    stack = list(seed_nodes)
    while stack:
        node = stack.pop()
        for neighbour in adjacency.get(node, ()):
            if neighbour not in seen:
                seen.add(neighbour)
                stack.append(neighbour)
    return [node for node in target_nodes if node not in seen]


def solver_loads(mesh, parsed_board, rail, *, grid_step_mm=0.1,
                 log_callback: Optional[Callable[[str], None]] = None,
                 index=None):
    """Convert a rail's ``UnifiedLoad`` entries into solver current injections.

    Each load's current is split evenly across the pads that were located,
    which is what ``distribution_mode`` ``"UNIFORM"`` means. A load whose pads
    cannot be located contributes nothing and says so, rather than silently
    dropping current out of the model.
    """
    def _log(message):
        if log_callback is not None:
            log_callback(message)

    net_name = str(getattr(rail, "net_name", ""))
    loads = []
    for load in getattr(rail, "loads", ()) or ():
        ref = str(getattr(getattr(load, "component_ref", None), "ref_des", "") or "")
        current = float(getattr(load, "total_current", 0.0) or 0.0)
        if not ref or current <= 0.0:
            continue
        node_ids, unlocated = map_pads_to_nodes(
            mesh, parsed_board, ref, getattr(load, "pad_names", ()) or (),
            net_name=net_name, grid_step_mm=grid_step_mm, index=index,
        )
        if unlocated:
            _log(
                f"{ref} pad(s) {', '.join(unlocated)} could not be located on "
                f"the {net_name} mesh; their share of {current:.3f} A is not "
                "modelled."
            )
        if not node_ids:
            continue
        share = current / len(node_ids)
        loads.extend(
            {"node_id": node_id, "current": share, "ref_des": ref}
            for node_id in node_ids
        )
    return loads
