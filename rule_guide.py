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
        "Rerun AC after the change: the curve should stay under the target at every "
        "frequency.",
        _B, _MEDIUM),
    "AC-002": RuleGuide(
        "Rail target impedance unknown",
        "Without a load current or an explicit target, the sweep has nothing to be judged "
        "against.",
        "Rerun AC: the rail should get a target and a PASS or FAIL.",
        _D, _LOW),
    "AC-003": RuleGuide(
        "Rail verdict open at the sweep edge",
        "The worst point is still rising where the sweep stops, so the real peak may lie "
        "outside the swept band.",
        "Rerun AC with a higher stop frequency: the worst point should fall inside the "
        "sweep.",
        _C, _LOW),
    "AC-004": RuleGuide(
        "AC profiles left out of the campaign",
        "A rail that was not swept is neither checked nor scored.",
        "Rerun the campaign: every configured profile should appear in the AC results.",
        _C, _LOW),
    "AC-005": RuleGuide(
        "AC result changes with the grid",
        "A margin that moves when the mesh is refined is set by the mesh as much as by the "
        "copper.",
        "Rerun on the finer grid the finding names: the verdict should be the same on both "
        "grids.",
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
        "Rerun with more iterations: the residuals should fall under the tolerance.",
        _C, _LOW),
    "CFD-002": RuleGuide(
        "Airflow solve loses mass or heat",
        "A solve that does not conserve what flows in and out misstates the air "
        "temperature.",
        "Rerun on a finer mesh: the mass and heat balances should close.",
        _C, _LOW),
    "CK-001": RuleGuide(
        "Clock on outer copper",
        "A fast edge on an outer layer radiates with no plane on one side to contain its "
        "field.",
        "Scan the clock's harmonics with a near-field probe on the prototype.",
        _B, _MEDIUM),
    "CK-002": RuleGuide(
        "Long clock route",
        "A long clock trace is a longer antenna for its harmonics.",
        "Scan the clock's harmonics with a near-field probe on the prototype.",
        _B, _MEDIUM),
    "CK-003": RuleGuide(
        "Clock near a connector",
        "Noise coupled into a connector's lines leaves the board on the cable, which "
        "radiates.",
        "Measure the common-mode current on the connector's cable with a current probe on "
        "the prototype.",
        _B, _MEDIUM),
    "DC-001": RuleGuide(
        "Loads the source cannot reach",
        "A load with no copper path to its source is either unconnected on the board or "
        "left out of the solve, so the rail's figures leave it out.",
        "Run KiCad's DRC: it lists the unconnected items on that net.",
        _B, _LOW),
    "DC-002": RuleGuide(
        "DC solve did not converge",
        "The voltages and currents of an unconverged solve cannot be relied on.",
        "Rerun DC: the solve should report convergence.",
        _C, _LOW),
    "DC-003": RuleGuide(
        "Voltage drop above the limit",
        "Too much drop in the copper leaves the load below its supply tolerance.",
        "Rerun DC after the change; on the prototype, measure the voltage at the load "
        "under full current.",
        _B, _MEDIUM),
    "DC-004": RuleGuide(
        "DC solved on an older save",
        "The figures describe the saved board file, which differs from the open board.",
        "Rerun DC after saving: this finding should not return.",
        _C, _LOW),
    "DC-005": RuleGuide(
        "DC result changes with the grid",
        "A drop that moves when the mesh is refined is set by the mesh as much as by the "
        "copper.",
        "Rerun on a finer DC grid: the drop should move by less than its margin.",
        _C, _LOW),
    "DC-006": RuleGuide(
        "Drop through fuses, switches and shunts",
        "Each part in series between the source and the regulator takes its own share of "
        "the voltage before the copper does.",
        "On the prototype, measure the voltage on both sides of each pass element under "
        "load.",
        _B, _LOW),
    "DC-007": RuleGuide(
        "Load not found on the board",
        "A load that the power tree names but the board lacks is left out of the solve.",
        "Rerun DC: the load should appear in the rail's results.",
        _D, _LOW),
    "DC-008": RuleGuide(
        "Pads cut off from their net",
        "A pad isolated from the rest of its net is an open circuit on the assembled board.",
        "Rerun DC: the pads should be on the net's copper, and KiCad's DRC should list no "
        "unconnected item.",
        _B, _LOW),
    "DE-001": RuleGuide(
        "Decoupling capacitor too far",
        "The trace between a capacitor and its pin adds inductance, which leaves high-"
        "frequency current to the rest of the board.",
        "Measure the capacitor-to-pin distance after the move; on the prototype, look at "
        "the pin's ripple with a short ground spring.",
        _B, _LOW),
    "DE-002": RuleGuide(
        "No decoupling near a power pin",
        "With no local capacitor, a pin draws its fast current through the whole power "
        "network.",
        "On the prototype, look at the power pin's ripple with a short ground spring.",
        _B, _LOW),
    "DP-001": RuleGuide(
        "Differential pair skew",
        "When one line of a pair is longer, part of the signal turns into common mode, which "
        "radiates.",
        "Read the pair's length difference in KiCad's length tuner.",
        _B, _MEDIUM),
    "DP-003": RuleGuide(
        "Differential pair changes reference",
        "Changing reference layer forces the return current to find a new path, often a long "
        "one.",
        "Check for a ground via within a few millimetres of each layer change of the pair.",
        _B, _LOW),
    "DP-004": RuleGuide(
        "Gaps under a differential pair",
        "A gap in the reference under the pair breaks the return path at that spot.",
        "Check the reference plane at the coordinates given, layer by layer.",
        _B, _LOW),
    "EE-001": RuleGuide(
        "Board cavity resonances",
        "Between two planes the board resonates at frequencies set by its size.",
        "Compare these frequencies with the peaks of a near-field scan.",
        _N, _LOW),
    "EE-002": RuleGuide(
        "Source harmonic envelopes",
        "Each switching source has a spectrum that other findings compare with the band.",
        "Nothing to check.",
        _N, _LOW),
    "EE-003": RuleGuide(
        "Switching frequency unknown",
        "A source without a frequency cannot be placed in the emissions band.",
        "Rerun EMC: the source should be placed in the band.",
        _D, _LOW),
    "ES-001": RuleGuide(
        "ESD path missing or long",
        "A discharge that travels far before its protection reaches the parts on the way.",
        "Test the connector with an ESD gun on the prototype (IEC 61000-4-2).",
        _B, _MEDIUM),
    "ES-002": RuleGuide(
        "TVS short of ground vias",
        "A TVS clamps only as well as its path to ground is short.",
        "Count the ground vias within 3 mm of the TVS's ground pad; test with an ESD gun "
        "on the prototype.",
        _B, _LOW),
    "GE-001": RuleGuide(
        "Copper outside the board ignored",
        "Geometry off the outline was left out of the analysis.",
        "Nothing to check if that copper is intentional, such as a test coupon.",
        _N, _LOW),
    "GP-001": RuleGuide(
        "Signal over a gap in its plane",
        "Over a gap, the return current detours around it and the loop it forms radiates.",
        "Check the plane under the segment, layer by layer; a near-field scan on the "
        "prototype shows whether it radiates.",
        _B, _MEDIUM),
    "GP-002": RuleGuide(
        "No ground plane",
        "Without a plane, every return current takes its own path and no reference is quiet.",
        "Check the stackup in KiCad's board setup.",
        _B, _HIGH),
    "GP-003": RuleGuide(
        "Ground plane in pieces",
        "Pieces of plane joined by narrow necks raise the return-path impedance.",
        "Highlight the ground net in KiCad after refilling the zones and look for narrow "
        "necks.",
        _B, _MEDIUM),
    "GP-004": RuleGuide(
        "Little ground copper",
        "A sparsely filled ground leaves signals without a reference under them.",
        "Highlight the ground net in KiCad and look at the fill under fast signals.",
        _B, _MEDIUM),
    "GP-005": RuleGuide(
        "Several ground domains",
        "Separate grounds are a choice; a signal crossing between them loses its return "
        "path.",
        "Highlight each ground net in KiCad and check where they join.",
        _B, _MEDIUM),
    "IN-001": RuleGuide(
        "Inductor shielding not calibrated",
        "Shielding is recognised from the part, but its effect on the field is not "
        "quantified.",
        "Nothing to check.",
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
        "Test the connector with an ESD gun and its cable with a current probe on the "
        "prototype.",
        _B, _MEDIUM),
    "IO-002": RuleGuide(
        "Interface on a cable connector",
        "The tool assumed the cable stays inside the enclosure.",
        "Check where the cable runs in the enclosure.",
        _B, _LOW),
    "PD-001": RuleGuide(
        "Rail impedance estimate above target",
        "The EMC estimate of the rail exceeds its target; the AC domain judges the rail.",
        "Read the AC result for this rail.",
        _N, _LOW),
    "PD-002": RuleGuide(
        "Rail impedance within target",
        "The rail holds its target over the swept band.",
        "Nothing to check.",
        _N, _LOW),
    "PH-001": RuleGuide(
        "No SPICE model for a converter",
        "Without a model the converter's waveforms are not simulated, and the EMC estimate "
        "keeps the entered values.",
        "Rerun EMC: the converter should appear among the simulated circuits.",
        _C, _LOW),
    "PH-002": RuleGuide(
        "Converter model unstable",
        "The model did not settle in simulation, so its waveforms are not used.",
        "Rerun EMC: the circuit should settle.",
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
        "Compare the entered value with the datasheet and with the simulated circuit.",
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
        "Check for a ground via within a few millimetres of each signal via.",
        _B, _LOW),
    "SCH-001": RuleGuide(
        "Power pin without decoupling",
        "A power input with no capacitor draws its fast current through the whole network.",
        "Run the schematic rules again: the pin should have a capacitor.",
        _B, _LOW),
    "SCH-002": RuleGuide(
        "Unconnected pin",
        "A pin left open by mistake can float or leave a function unpowered.",
        "Run KiCad's ERC: the pin should be connected or marked no-connect.",
        _B, _LOW),
    "SCH-003": RuleGuide(
        "Resistor without a power rating",
        "Without a rating, the dissipation of a resistor cannot be checked.",
        "Run the schematic rules again: the resistor should have a rating.",
        _D, _LOW),
    "SCH-004": RuleGuide(
        "Cross-board link out of date",
        "A link the project declares no longer matches the boards it joins.",
        "Run the schematic rules again: the link should match the boards.",
        _D, _LOW),
    "SCH-005": RuleGuide(
        "Footprint whose symbol is gone",
        "The board holds a part the schematic no longer has.",
        "Update the PCB from the schematic in KiCad and read the changes it lists.",
        _D, _LOW),
    "SCH-006": RuleGuide(
        "Footprints added on the board only",
        "Parts placed only on the board are not checked by schematic rules.",
        "List the footprints with no symbol and check that each is mechanical.",
        _D, _LOW),
    "SH-001": RuleGuide(
        "Connector shield return not found",
        "A connector shell with no short path to chassis lets cable noise onto the board.",
        "Check the connector's shell pads on the footprint and the net they are on.",
        _B, _MEDIUM),
    "SI-DIFF-001": RuleGuide(
        "Differential impedance off target",
        "An impedance step reflects part of the signal and closes the eye.",
        "Rerun the differential analysis after the change; ask the fabricator for an "
        "impedance coupon.",
        _B, _MEDIUM),
    "SI-STACKUP-001": RuleGuide(
        "Stackup not confirmed",
        "Impedances computed from a guessed stackup carry the guess's error.",
        "Rerun the differential analysis after the import.",
        _D, _LOW),
    "SU-001": RuleGuide(
        "Two signal layers side by side",
        "Without a plane between them, two signal layers couple to each other.",
        "Check the stackup in KiCad's board setup.",
        _B, _HIGH),
    "SU-002": RuleGuide(
        "Signal layer far from its plane",
        "The farther a trace is from its plane, the larger its return loop.",
        "Check the stackup in KiCad's board setup.",
        _B, _HIGH),
    "SW-001": RuleGuide(
        "Switching harmonics in the emissions band",
        "A converter's sharp edges spread energy over many harmonics, some inside the band "
        "the standard limits.",
        "Scan this band with a near-field probe on the prototype.",
        _B, _MEDIUM),
    "SW-002": RuleGuide(
        "Large switching-node copper",
        "The switching node swings at full voltage; each square millimetre of it is antenna "
        "area.",
        "Read the node's copper area again after the change; scan it with a near-field "
        "probe on the prototype.",
        _B, _MEDIUM),
    "SW-003": RuleGuide(
        "Spread-out switching hot loop",
        "The loop through the input capacitor and the switches carries the fastest current "
        "edges; it radiates in proportion to its area.",
        "Measure the loop's widest gap after the move; scan the switching harmonics with a "
        "near-field probe on the prototype.",
        _B, _MEDIUM),
    "SW-004": RuleGuide(
        "Converter values differ from the power tree",
        "An entered current or frequency that differs from the power tree's makes each "
        "analysis judge a different converter.",
        "Rerun EMC: the finding should not return once the values agree.",
        _D, _LOW),
    "TE-001": RuleGuide(
        "Hot parts change their EMC behaviour",
        "Heat lowers a ceramic capacitor's capacitance and a ferrite's impedance.",
        "Read those parts' ratings at the temperature found.",
        _N, _LOW),
    "TE-002": RuleGuide(
        "Capacitor above its rated temperature",
        "Beyond the temperature its dielectric is rated to (X5R 85 °C, X7R 125 °C), a "
        "ceramic capacitor's capacitance and life are no longer specified.",
        "Read the capacitor's temperature range in its datasheet; on the prototype, measure "
        "its temperature at full load.",
        _B, _LOW),
    "TH-001": RuleGuide(
        "Junction above its limit",
        "Above its maximum junction temperature, a part ages fast or shuts down.",
        "Rerun thermal after the change; on the prototype, measure the case with a "
        "thermocouple or a thermal camera.",
        _B, _MEDIUM),
    "TH-002": RuleGuide(
        "Thermal solve did not converge",
        "The temperatures of an unconverged solve cannot be relied on.",
        "Rerun thermal: the solve should report convergence.",
        _C, _LOW),
    "TH-003": RuleGuide(
        "Thermal energy balance off",
        "The heat leaving the model does not match the heat put in.",
        "Rerun thermal: the energy balance error should fall under its limit.",
        _C, _LOW),
    "TH-004": RuleGuide(
        "Thermal result changes with the grid",
        "A temperature that moves when the mesh is refined is set by the mesh as much as "
        "by the copper.",
        "Rerun on the finer grid the finding names: the temperatures should move by less "
        "than their margin.",
        _C, _LOW),
    "TH-005": RuleGuide(
        "Heat left out of the thermal model",
        "Losses that are not placed on the board are missing from the temperatures.",
        "Rerun thermal: the input heat should match the losses.",
        _D, _LOW),
    "TH-006": RuleGuide(
        "Thin temperature margin",
        "Little margin to the limit leaves no room for a warmer enclosure or higher losses.",
        "Read the part's rating at this temperature in its datasheet; on the prototype, "
        "measure its temperature at full load.",
        _B, _MEDIUM),
    "TH-007": RuleGuide(
        "Rating holds at temperature",
        "The part keeps enough of its rating at the temperature found.",
        "Nothing to check while its current and temperature stay as estimated.",
        _N, _LOW),
    "TH-008": RuleGuide(
        "Converter loss from an estimated efficiency",
        "The heat a converter puts on the board rests on an efficiency nobody read off a "
        "curve or measured; the temperatures of its parts move with it.",
        "Rerun thermal after entering the efficiency: the finding should not return.",
        _D, _LOW),
    "VS-001": RuleGuide(
        "Sparse ground stitching",
        "Planes stitched far apart resonate at frequencies inside the band.",
        "Scan the board edges with a near-field probe on the prototype.",
        _B, _LOW),
    "XT-001": RuleGuide(
        "Crosstalk between parallel traces",
        "Traces running side by side couple part of one signal into the other.",
        "On the prototype, look at the victim line with an oscilloscope while the "
        "aggressor switches.",
        _B, _MEDIUM),
}


# Rules applying a layout rule of thumb whose consequence (emissions, ESD
# immunity, noise) only a measurement on the prototype settles. Short of
# CRITICAL, the report lists their findings under "check on the prototype",
# not "fix before manufacture". The audit of 2026-10-01 (LE-10, LE-11)
# found SW-003, a 20 mm threshold, shown at the same HIGH as DC-008, C88
# and R10 cut off from GND.
PROTOTYPE_CHECK = frozenset((
    "CK-001", "CK-002", "CK-003", "DE-001", "DE-002", "DP-003", "DP-004", "ES-001",
    "ES-002", "GP-001", "GP-003", "GP-004", "GP-005", "IO-001", "IO-002", "RP-001",
    "SH-001", "SW-001", "SW-002", "SW-003", "VS-001", "XT-001",
))

# INFO findings that say a check holds, listed under "what holds" rather
# than lost among the other notes (LE-9): PD-002 on three P02 Rev E rails.
CONFIRMATIONS = frozenset(("PD-002", "TH-007"))


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
