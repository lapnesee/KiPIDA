"""What each rule means to someone reading its finding for the first time.

A finding's title says what was found. A reader new to the rule also needs
why that is a risk, how to check it, and what kind of problem it is. It may
be a defect on the board. It may be a limit of the calculation that the
board did not cause. It may be an input still missing. Before this guide the
campaign treated all three alike. On P02 Rev E's 2026-10-01 00:59 campaign,
TH-004 ("Thermal result is not converged in mesh") ranked third among 13
actions and cost the thermal score 6 of its 13.8 points. All 13 actions read
"Effort: MEDIUM", the default used when no remediation is computed.

The entries name a rule's typical fix effort, not a computed one. LOW means
moving a part, adding a via or filling in a field. MEDIUM means rerouting or
re-placing a group. HIGH means changing the stackup or a plane.
"""

from dataclasses import dataclass
from typing import Dict, Optional

from analysis_contract import AnalysisFinding, RemediationEffort, _StringEnum


class FindingNature(_StringEnum):
    # Something on the board to change: copper, placement, parts.
    BOARD = "BOARD"
    # A limit of the run, not of the board: mesh, convergence, a solver or
    # model that could not settle the question.
    CALCULATION = "CALCULATION"
    # An input the analysis needed and did not have, or that disagrees with
    # what the tool computed: complete it and the verdict sharpens.
    DATA = "DATA"
    # Nothing to do: a check that holds, or context for another finding.
    NOTE = "NOTE"


@dataclass(frozen=True)
class RuleGuide:
    name: str
    why: str
    verify: str
    nature: FindingNature
    effort: RemediationEffort


_B, _C, _D, _N = (FindingNature.BOARD, FindingNature.CALCULATION,
                  FindingNature.DATA, FindingNature.NOTE)
_LOW, _MEDIUM, _HIGH = RemediationEffort.LOW, RemediationEffort.MEDIUM, RemediationEffort.HIGH

GUIDE: Dict[str, RuleGuide] = {
    "AC-001": RuleGuide(
        "Rail impedance above its target",
        "Above its target impedance, a rail lets the load's current steps move its voltage "
        "more than the load tolerates.",
        "Look at the frequency where the curve crosses the target, then add or move "
        "capacitors that act there and rerun AC.",
        _B, _MEDIUM),
    "AC-002": RuleGuide(
        "Rail target impedance unknown",
        "Without a load current or an explicit target, the sweep has nothing to be judged "
        "against.",
        "Enter the rail's load current step, or its target impedance, and rerun AC.",
        _D, _LOW),
    "AC-003": RuleGuide(
        "Rail verdict open at the sweep edge",
        "The worst point is still rising where the sweep stops, so the real peak may lie "
        "outside the swept band.",
        "Extend the sweep's stop frequency and rerun AC.",
        _C, _LOW),
    "AC-004": RuleGuide(
        "AC profiles left out of the campaign",
        "A rail that was not swept is neither checked nor scored.",
        "Fix what stopped those profiles (the finding names the reason) and rerun.",
        _C, _LOW),
    "AC-005": RuleGuide(
        "AC result changes with the grid",
        "A margin that moves when the mesh is refined is set by the mesh as much as by the "
        "copper.",
        "Rerun on the finer grid the finding proposes and compare the margin.",
        _C, _LOW),
    "BE-001": RuleGuide(
        "Copper near or beyond the board edge",
        "Copper at the edge radiates more easily and can be cut or exposed when the panel is "
        "routed.",
        "Measure the route's distance to the outline in KiCad and compare it with the "
        "fabricator's edge clearance.",
        _B, _LOW),
    "CFD-001": RuleGuide(
        "Airflow solve did not converge",
        "An unconverged flow field can put the hot air in the wrong place.",
        "Rerun with more iterations or a finer mesh and check that the residuals fall.",
        _C, _LOW),
    "CFD-002": RuleGuide(
        "Airflow solve loses mass or heat",
        "A solve that does not conserve what flows in and out misstates the air "
        "temperature.",
        "Refine the mesh or boundary conditions and check the balance again.",
        _C, _LOW),
    "CK-001": RuleGuide(
        "Clock on outer copper",
        "A fast edge on an outer layer radiates with no plane on one side to contain its "
        "field.",
        "Check whether the clock can move to an inner layer between planes.",
        _B, _MEDIUM),
    "CK-002": RuleGuide(
        "Long clock route",
        "A long clock trace is a longer antenna for its harmonics.",
        "Check whether the source can sit closer to its load.",
        _B, _MEDIUM),
    "CK-003": RuleGuide(
        "Clock near a connector",
        "Noise coupled into a connector's lines leaves the board on the cable, which "
        "radiates.",
        "Measure the gap to the connector; increase it or guard the clock with ground.",
        _B, _MEDIUM),
    "DC-001": RuleGuide(
        "Loads the source cannot reach",
        "A load with no copper path to its source is either unconnected on the board or "
        "left out of the solve, so the rail's figures leave it out.",
        "Run KiCad's DRC and look for unconnected items on that net.",
        _B, _LOW),
    "DC-002": RuleGuide(
        "DC solve did not converge",
        "The voltages and currents of an unconverged solve cannot be relied on.",
        "Check the rail's connectivity, then rerun DC.",
        _C, _LOW),
    "DC-003": RuleGuide(
        "Voltage drop above the limit",
        "Too much drop in the copper leaves the load below its supply tolerance.",
        "Widen or shorten the path named in the finding, or add a parallel layer, and "
        "rerun DC.",
        _B, _MEDIUM),
    "DC-004": RuleGuide(
        "DC solved on an older save",
        "The figures describe the saved board file, which differs from the open board.",
        "Save the board and rerun DC.",
        _C, _LOW),
    "DC-005": RuleGuide(
        "DC result changes with the grid",
        "A drop that moves when the mesh is refined is set by the mesh as much as by the "
        "copper.",
        "Rerun on a finer DC grid and compare the drop.",
        _C, _LOW),
    "DC-006": RuleGuide(
        "Drop through fuses, switches and shunts",
        "Each part in series between the source and the regulator takes its own share of "
        "the voltage before the copper does.",
        "Check each pass element's resistance in the sidecar, and budget its drop with the "
        "copper's.",
        _B, _LOW),
    "DC-007": RuleGuide(
        "Load not found on the board",
        "A load that the power tree names but the board lacks is left out of the solve.",
        "Check the load's reference and pad names in the power tree against the board.",
        _D, _LOW),
    "DC-008": RuleGuide(
        "Pads cut off from their net",
        "A pad isolated from the rest of its net is an open circuit on the assembled board.",
        "Refill the zones, run KiCad's DRC and join each pad with a via or a track.",
        _B, _LOW),
    "DE-001": RuleGuide(
        "Decoupling capacitor too far",
        "The trace between a capacitor and its pin adds inductance, which leaves high-"
        "frequency current to the rest of the board.",
        "Measure the capacitor-to-pin distance; move the capacitor next to the pin and keep "
        "its vias short.",
        _B, _LOW),
    "DE-002": RuleGuide(
        "No decoupling near a power pin",
        "With no local capacitor, a pin draws its fast current through the whole power "
        "network.",
        "Check the part's datasheet for its bypass capacitor and place one at the pin.",
        _B, _LOW),
    "DP-001": RuleGuide(
        "Differential pair skew",
        "When one line of a pair is longer, part of the signal turns into common mode, which "
        "radiates.",
        "Length-match the pair, keeping its spacing constant.",
        _B, _MEDIUM),
    "DP-003": RuleGuide(
        "Differential pair changes reference",
        "Changing reference layer forces the return current to find a new path, often a long "
        "one.",
        "Add a ground via next to each layer change of the pair.",
        _B, _LOW),
    "DP-004": RuleGuide(
        "Gaps under a differential pair",
        "A gap in the reference under the pair breaks the return path at that spot.",
        "Restore or stitch the ground at the coordinates given.",
        _B, _LOW),
    "EE-001": RuleGuide(
        "Board cavity resonances",
        "Between two planes the board resonates at frequencies set by its size.",
        "Nothing to do; use these frequencies to read a near-field scan.",
        _N, _LOW),
    "EE-002": RuleGuide(
        "Source harmonic envelopes",
        "Each switching source has a spectrum that other findings compare with the band.",
        "Nothing to do.",
        _N, _LOW),
    "EE-003": RuleGuide(
        "Switching frequency unknown",
        "A source without a frequency cannot be placed in the emissions band.",
        "Enter the source's switching frequency in the EMC settings.",
        _D, _LOW),
    "ES-001": RuleGuide(
        "ESD path missing or long",
        "A discharge that travels far before its protection reaches the parts on the way.",
        "Measure the connector-to-protection distance and move the protection next to the "
        "connector.",
        _B, _MEDIUM),
    "ES-002": RuleGuide(
        "TVS short of ground vias",
        "A TVS clamps only as well as its path to ground is short.",
        "Count the ground vias by the TVS's ground pad and add at least two.",
        _B, _LOW),
    "GE-001": RuleGuide(
        "Copper outside the board ignored",
        "Geometry off the outline was left out of the analysis.",
        "Nothing to do if that copper is intentional, such as a test coupon.",
        _N, _LOW),
    "GP-001": RuleGuide(
        "Signal over a gap in its plane",
        "Over a gap, the return current detours around it and the loop it forms radiates.",
        "Reroute over continuous ground or fill the gap under the segment.",
        _B, _MEDIUM),
    "GP-002": RuleGuide(
        "No ground plane",
        "Without a plane, every return current takes its own path and no reference is quiet.",
        "Add a ground plane in the stackup.",
        _B, _HIGH),
    "GP-003": RuleGuide(
        "Ground plane in pieces",
        "Pieces of plane joined by narrow necks raise the return-path impedance.",
        "Join the pieces with copper or stitching vias.",
        _B, _MEDIUM),
    "GP-004": RuleGuide(
        "Little ground copper",
        "A sparsely filled ground leaves signals without a reference under them.",
        "Increase the ground fill, starting under fast signals.",
        _B, _MEDIUM),
    "GP-005": RuleGuide(
        "Several ground domains",
        "Separate grounds are a choice; a signal crossing between them loses its return "
        "path.",
        "Check that the split is intended and that no signal crosses it.",
        _B, _MEDIUM),
    "IN-001": RuleGuide(
        "Inductor shielding not calibrated",
        "Shielding is recognised from the part, but its effect on the field is not "
        "quantified.",
        "Nothing to do.",
        _N, _LOW),
    "IN-004": RuleGuide(
        "Inductor near saturation",
        "Near saturation, inductance collapses and the current peak rises quickly.",
        "Compare the peak current with the datasheet's saturation current at temperature.",
        _B, _MEDIUM),
    "IO-001": RuleGuide(
        "Connector without EMC protection",
        "An external cable brings in and carries out noise unless it is filtered at the "
        "connector.",
        "Check the interface's datasheet for its protection and place it at the connector.",
        _B, _MEDIUM),
    "IO-002": RuleGuide(
        "Interface on a cable connector",
        "The tool assumed the cable stays inside the enclosure.",
        "If the cable leaves the enclosure, protect the lines where it does.",
        _B, _LOW),
    "PD-001": RuleGuide(
        "Rail impedance estimate above target",
        "The EMC estimate of the rail exceeds its target; the AC domain judges the rail.",
        "Read the AC result for this rail.",
        _N, _LOW),
    "PD-002": RuleGuide(
        "Rail impedance within target",
        "The rail holds its target over the swept band.",
        "Nothing to do.",
        _N, _LOW),
    "PH-001": RuleGuide(
        "No SPICE model for a converter",
        "Without a model the converter's waveforms are not simulated, and the EMC estimate "
        "keeps the entered values.",
        "Add the vendor's model if one exists.",
        _C, _LOW),
    "PH-002": RuleGuide(
        "Converter model unstable",
        "The model did not settle in simulation, so its waveforms are not used.",
        "Check the model's parameters, or try another version of it.",
        _C, _LOW),
    "PH-003": RuleGuide(
        "Converter model unusable",
        "The simulator could not run the model.",
        "Read the solver log in the Phase 10 output directory.",
        _C, _LOW),
    "PH-004": RuleGuide(
        "Simulation failed",
        "The simulation did not complete, so its results are missing.",
        "Read the solver log in the Phase 10 output directory.",
        _C, _LOW),
    "PH-005": RuleGuide(
        "Entered source differs from its board circuit",
        "The value entered for EMC differs from what the simulated board circuit gives.",
        "Check which value is right and correct the entered one.",
        _D, _LOW),
    "PH-006": RuleGuide(
        "Converter not running in its board circuit",
        "The simulated converter did not regulate: a pin may be wired wrong, or the model "
        "may not support how it is wired.",
        "Check the converter's enable, frequency and feedback pins against its datasheet.",
        _C, _LOW),
    "RP-001": RuleGuide(
        "Layer change without a ground via",
        "When a signal changes layer, its return current needs a nearby via to follow it.",
        "Add a ground via next to each signal via.",
        _B, _LOW),
    "SCH-001": RuleGuide(
        "Power pin without decoupling",
        "A power input with no capacitor draws its fast current through the whole network.",
        "Check the part's datasheet and add its bypass capacitor on the schematic.",
        _B, _LOW),
    "SCH-002": RuleGuide(
        "Unconnected pin",
        "A pin left open by mistake can float or leave a function unpowered.",
        "Connect the pin, or mark it no-connect if the datasheet allows it.",
        _B, _LOW),
    "SCH-003": RuleGuide(
        "Resistor without a power rating",
        "Without a rating, the dissipation of a resistor cannot be checked.",
        "Add a Power field to the symbol, such as 0.1W.",
        _D, _LOW),
    "SCH-004": RuleGuide(
        "Cross-board link out of date",
        "A link the project declares no longer matches the boards it joins.",
        "Update the link in the multi-board project.",
        _D, _LOW),
    "SCH-005": RuleGuide(
        "Footprint whose symbol is gone",
        "The board holds a part the schematic no longer has.",
        "Update the PCB from the schematic, or restore the symbol.",
        _D, _LOW),
    "SCH-006": RuleGuide(
        "Footprints added on the board only",
        "Parts placed only on the board are not checked by schematic rules.",
        "Nothing to do for mounting holes and logos; add a symbol for anything electrical.",
        _D, _LOW),
    "SH-001": RuleGuide(
        "Connector shield return not found",
        "A connector shell with no short path to chassis lets cable noise onto the board.",
        "Check how the shell is connected and give it a short return.",
        _B, _MEDIUM),
    "SI-DIFF-001": RuleGuide(
        "Differential impedance off target",
        "An impedance step reflects part of the signal and closes the eye.",
        "Apply the proposed width and spacing, or confirm the stackup with the fabricator.",
        _B, _MEDIUM),
    "SI-STACKUP-001": RuleGuide(
        "Stackup not confirmed",
        "Impedances computed from a guessed stackup carry the guess's error.",
        "Import the fabricator's stackup.",
        _D, _LOW),
    "SU-001": RuleGuide(
        "Two signal layers side by side",
        "Without a plane between them, two signal layers couple to each other.",
        "Route the layers orthogonally, or change the stackup.",
        _B, _HIGH),
    "SU-002": RuleGuide(
        "Signal layer far from its plane",
        "The farther a trace is from its plane, the larger its return loop.",
        "Reduce the dielectric under that layer in the stackup.",
        _B, _HIGH),
    "SW-001": RuleGuide(
        "Switching harmonics in the emissions band",
        "A converter's sharp edges spread energy over many harmonics, some inside the band "
        "the standard limits.",
        "Keep the hot loop small and check this band with a near-field probe on the "
        "prototype.",
        _B, _MEDIUM),
    "SW-002": RuleGuide(
        "Large switching-node copper",
        "The switching node swings at full voltage; each square millimetre of it is antenna "
        "area.",
        "Shrink the node's copper to what the current needs.",
        _B, _MEDIUM),
    "SW-003": RuleGuide(
        "Spread-out switching hot loop",
        "The loop through the input capacitor and the switches carries the fastest current "
        "edges; it radiates in proportion to its area.",
        "Place the input capacitor and both switches as close together as the footprints "
        "allow.",
        _B, _MEDIUM),
    "TE-001": RuleGuide(
        "Hot parts change their EMC behaviour",
        "Heat lowers a ceramic capacitor's capacitance and a ferrite's impedance.",
        "Check those parts' ratings at the temperature found.",
        _N, _LOW),
    "TH-001": RuleGuide(
        "Junction above its limit",
        "Above its maximum junction temperature, a part ages fast or shuts down.",
        "Add copper or thermal vias under the part, or lower its losses, and rerun "
        "thermal.",
        _B, _MEDIUM),
    "TH-002": RuleGuide(
        "Thermal solve did not converge",
        "The temperatures of an unconverged solve cannot be relied on.",
        "Rerun with more iterations.",
        _C, _LOW),
    "TH-003": RuleGuide(
        "Thermal energy balance off",
        "The heat leaving the model does not match the heat put in.",
        "Refine the mesh or the boundary conditions and rerun thermal.",
        _C, _LOW),
    "TH-004": RuleGuide(
        "Thermal result changes with the grid",
        "A temperature that moves when the mesh is refined is set by the mesh as much as "
        "by the copper.",
        "Rerun on the finer grid the finding proposes and compare the temperatures.",
        _C, _LOW),
    "TH-005": RuleGuide(
        "Heat left out of the thermal model",
        "Losses that are not placed on the board are missing from the temperatures.",
        "Check that every dissipating part is placed and enabled.",
        _D, _LOW),
    "TH-006": RuleGuide(
        "Thin temperature margin",
        "Little margin to the limit leaves no room for a warmer enclosure or higher losses.",
        "Check the part's rating at this temperature in its datasheet.",
        _B, _MEDIUM),
    "TH-007": RuleGuide(
        "Rating holds at temperature",
        "The part keeps enough of its rating at the temperature found.",
        "Nothing to do while current and temperature stay as estimated.",
        _N, _LOW),
    "VS-001": RuleGuide(
        "Sparse ground stitching",
        "Planes stitched far apart resonate at frequencies inside the band.",
        "Add ground vias along the edges, around connectors and at layer changes.",
        _B, _LOW),
    "XT-001": RuleGuide(
        "Crosstalk between parallel traces",
        "Traces running side by side couple part of one signal into the other.",
        "Increase the spacing, move one trace to another layer, or add a ground guard.",
        _B, _MEDIUM),
}


def guide(rule_id: str) -> Optional[RuleGuide]:
    return GUIDE.get(str(rule_id or ""))


def rule_nature(rule_id: str) -> FindingNature:
    """A rule's nature; one the guide does not know is read as a board defect."""
    entry = guide(rule_id)
    return entry.nature if entry else FindingNature.BOARD


def finding_nature(finding: AnalysisFinding) -> FindingNature:
    """The flag an adapter set wins, so a result saved before a rule entered
    the guide keeps its nature."""
    if finding.calculation_quality:
        return FindingNature.CALCULATION
    return rule_nature(finding.rule_id)


def typical_effort(rule_id: str) -> RemediationEffort:
    entry = guide(rule_id)
    return entry.effort if entry else RemediationEffort.MEDIUM
