"""What the power tree says each converter delivers, read once for every domain.

The tree's loads are the project's own statement of the current each rail
carries. Domains that need a converter's output current used to take it
from wherever they kept a copy. P02 Rev E's 2026-10-01 campaign used four
values for U4: 4.36 A entered for the EMC source and L1's model, 4.73 A for
the AC target, 4.746 A from the DC solve, and the Phase 10 board circuit
loaded with the entered 4.36 A (audit CO-1, CO-3).
"""

from dataclasses import dataclass
from typing import Dict, Iterable


def converter_input_current(rail, all_rails):
    """Current the regulators fed by *rail* draw from it, through the whole tree.

    A regulator draws what its output rail supplies -- that rail's loads and,
    in turn, its own regulators' inputs -- scaled by Vout / (Vin x efficiency)
    when it switches and passed through unchanged when it is linear, as the DC
    solve and the EMC source scan count it.
    """
    by_name = {item.net_name: item for item in (all_rails or ())}

    def supplied(name, visiting):
        item = by_name.get(name)
        if item is None or name in visiting:
            return 0.0
        own = sum(float(getattr(load, "total_current", 0.0) or 0.0) for load in (item.loads or ()))
        return own + drawn(item, visiting | {name})

    def drawn(item, visiting):
        total = 0.0
        for regulator in getattr(item, "child_regulators", ()) or ():
            downstream = supplied(regulator.output_rail_name, visiting)
            if str(regulator.reg_type).upper() == "SWITCHING":
                output = by_name.get(regulator.output_rail_name)
                vout = float(getattr(output, "nominal_voltage", 0.0) or 0.0)
                vin = float(getattr(item, "nominal_voltage", 0.0) or 0.0)
                efficiency = max(float(getattr(regulator, "efficiency", 1.0) or 1.0), 1.0e-6)
                total += downstream * vout / (vin * efficiency) if vin > 0.0 else downstream
            else:
                total += downstream
        return total

    return drawn(rail, {rail.net_name})


def rail_supplied_current(rail_name, all_rails):
    """What feeding *rail_name* takes: its loads, plus what the regulators it
    feeds draw from it -- a converter's output current when a converter
    feeds it. 0 for a rail the tree does not hold."""
    rail = next((item for item in (all_rails or ()) if item.net_name == rail_name), None)
    if rail is None:
        return 0.0
    own = sum(float(getattr(load, "total_current", 0.0) or 0.0) for load in (rail.loads or ()))
    return own + converter_input_current(rail, all_rails)


@dataclass(frozen=True)
class ConverterOutput:
    name: str
    output_rail: str
    current_a: float
    # From the converter's loss model; 0 when it states none.
    frequency_hz: float = 0.0


def _loss_model_frequency(regulator) -> float:
    loss_model = getattr(regulator, "loss_model", None) or {}
    raw = loss_model.get("switching_frequency_hz", 0.0) if isinstance(loss_model, dict) else 0.0
    value = raw.get("value", 0.0) if isinstance(raw, dict) else raw
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _references(regulator) -> Iterable[str]:
    """The part references a converter goes by: its name's first word ("U4
    buck-boost 5 V machine"), and the parts it names."""
    loss_model = getattr(regulator, "loss_model", None) or {}
    candidates = (
        str(getattr(regulator, "name", "") or "").split(" ", 1)[0],
        getattr(regulator, "thermal_ref_des", ""),
        loss_model.get("controller_ref_des", "") if isinstance(loss_model, dict) else "",
        getattr(regulator, "input_ref_des", ""),
        getattr(regulator, "output_ref_des", ""),
    )
    for candidate in candidates:
        text = str(candidate or "").strip().upper()
        if text:
            yield text


def converter_outputs(all_rails) -> Dict[str, ConverterOutput]:
    """``{reference: ConverterOutput}`` for every switching converter of the
    tree, under each reference it goes by. The first converter to claim a
    reference keeps it."""
    found: Dict[str, ConverterOutput] = {}
    for rail in all_rails or ():
        for regulator in getattr(rail, "child_regulators", ()) or ():
            if str(getattr(regulator, "reg_type", "")).upper() != "SWITCHING":
                continue
            output = ConverterOutput(
                str(getattr(regulator, "name", "") or regulator.output_rail_name),
                regulator.output_rail_name,
                rail_supplied_current(regulator.output_rail_name, all_rails),
                _loss_model_frequency(regulator),
            )
            for reference in _references(regulator):
                found.setdefault(reference, output)
    return found


def converter_output_for(name: str, outputs: Dict[str, ConverterOutput]):
    """The converter an EMC source or an inductor model belongs to, by the
    part reference that opens its name ("U4 SW1 buck leg (Q10/Q11/L1)")."""
    head = str(name or "").strip().split(" ", 1)[0].upper()
    return outputs.get(head) if head else None

