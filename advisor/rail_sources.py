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

from typing import Callable, Optional

try:
    from .dc_advisor import find_node_at
except (ImportError, ValueError):
    from advisor.dc_advisor import find_node_at


# A pad centre rarely coincides with a mesh node: zone cut-cell nodes sit on
# the mesher's grid, and track nodes at segment endpoints. The nearest grid
# node is at most g/sqrt(2) away, so a couple of grid steps leaves margin for
# a pad slightly off the meshed copper while still refusing a match that would
# attach a pad to unrelated copper millimetres away.
NODE_MATCH_GRID_FACTOR = 2.0
NODE_MATCH_FLOOR_MM = 0.2


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


def pad_positions(parsed_board, ref_des, pad_names, net_name=""):
    """``[(pad_number, (x_mm, y_mm)), ...]`` for the named pads of *ref_des*.

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
        found = []
        for pad in getattr(footprint, "pads", ()) or ():
            if net_name and pad.net_name != net_name:
                continue
            if wanted and str(pad.number) not in wanted:
                continue
            found.append((str(pad.number), pad.position))
        return found
    return []


def map_pads_to_nodes(mesh, parsed_board, ref_des, pad_names, *,
                      net_name="", grid_step_mm=0.1, tolerance_mm=None):
    """Locate a component's pads in *mesh*.

    Returns ``(node_ids, unlocated)``, where *unlocated* lists the pad numbers
    that had no mesh node within tolerance. Those are reported rather than
    snapped to whatever happened to be nearest: attaching a pad to unrelated
    copper would move current somewhere it does not flow, and a quietly wrong
    anchor is worse than a refused one.
    """
    if tolerance_mm is None:
        tolerance_mm = max(NODE_MATCH_GRID_FACTOR * grid_step_mm, NODE_MATCH_FLOOR_MM)
    node_ids, unlocated = [], []
    for number, (x_mm, y_mm) in pad_positions(
        parsed_board, ref_des, pad_names, net_name,
    ):
        node_id = find_node_at(mesh, x_mm, y_mm, tolerance_mm=tolerance_mm)
        if node_id is None:
            unlocated.append(number)
        elif node_id not in node_ids:
            node_ids.append(node_id)
    return node_ids, unlocated


def solver_loads(mesh, parsed_board, rail, *, grid_step_mm=0.1,
                 log_callback: Optional[Callable[[str], None]] = None):
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
            net_name=net_name, grid_step_mm=grid_step_mm,
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
        loads.extend({"node_id": node_id, "current": share} for node_id in node_ids)
    return loads
