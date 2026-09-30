"""Schematic-derived rules.

These rules consume only the offline ingestion layer (``ingest/``) — a
:class:`~ingest.netlist_builder.BoardNetlist`, optionally paired with the
:class:`~ingest.schematic_reader.ParsedSchematic` it was built from and the
sibling boards of a multi-board project. None of them depend on a numerical
solver result, so they can run as soon as a project is opened.

Every rule is deterministic where the underlying fact is deterministic (pin
electrical type comes from the schematic, not a name guess) and is marked
``HEURISTIC``/``INFO`` where Ki-PIDA cannot actually verify the claim (e.g.
whether a resistor's power rating is adequate) — see the project's epistemic
honesty principle in ``docs/refonte-analyses.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

try:
    from .registry import rule
except (ImportError, ValueError):
    from registry import rule

try:
    from ..analysis_contract import (
        AnalysisEvidence,
        AnalysisFinding,
        EvidenceConfidence,
        FindingSeverity,
    )
except (ImportError, ValueError):
    from analysis_contract import (
        AnalysisEvidence,
        AnalysisFinding,
        EvidenceConfidence,
        FindingSeverity,
    )

try:
    from ..ingest.netlist_builder import BoardNetlist, ComponentInfo
    from ..ingest.schematic_reader import ParsedSchematic
except (ImportError, ValueError):
    from ingest.netlist_builder import BoardNetlist, ComponentInfo
    from ingest.schematic_reader import ParsedSchematic


@dataclass
class SchematicRuleContext:
    netlist: BoardNetlist
    schematic: Optional[ParsedSchematic] = None
    all_board_netlists: Optional[list] = None  # list[BoardNetlist]
    # The Zeo container with its links validated (ingest.zeo_container), in the
    # Zeo modes of the project context only; None everywhere else.
    zeo: Optional[object] = None


# A capacitance as designers write it: digits, then an SI multiplier followed by
# F, by the decimal digits of "4u7", or by nothing -- "100n", "10u", "4u7",
# "100nF", "0.1uF", "CC0603_100NF_50V" -- or a bare farad. The "F" used to be
# required, which left 17 of cm5_minima's 19 capacitors undecoupling anything.
_CAPACITOR_VALUE_RE = re.compile(
    r"(?<![\d.,])\d+(?:[.,]\d+)?\s*(?:[pnuµμ](?:f|\d|(?![a-z]))|mf|f(?![a-z]))", re.IGNORECASE,
)
# The class of the part: KiCad's capacitor annotation prefix, or a footprint
# from a capacitor library. A value alone is not a class: "10u" is also an inductor's.
_CAPACITOR_REF_RE = re.compile(r"^C\d")
_CAPACITOR_FOOTPRINT_RE = re.compile(r"capacitor|(^|:)CP?_", re.IGNORECASE)
_GND_NET_RE = re.compile(r"gnd", re.IGNORECASE)
_RESISTOR_REF_RE = re.compile(r"^R\d", re.IGNORECASE)
# A resistance written as a value: "10k", "4.7 ohm", "10R". The number must
# stand on its own -- "47u 16V X5R" is no 5-ohm part, and on P02 Rev E the
# dielectric code put 22 ceramic capacitors in SCH-003's list of resistors.
_RESISTOR_VALUE_RE = re.compile(r"(?<![A-Za-z0-9.])\d+\.?\d*\s*[kKmM]?\s*(ohm|Ω|r)\b", re.IGNORECASE)
_POWER_FIELD_RE = re.compile(r"power|watt", re.IGNORECASE)
# A rating written into the value or another field: "3W", "0.25 W", "1/4W",
# "250mW". P02 Rev E's shunt R49 reads "7.5m 1% / 3W".
_POWER_RATING_RE = re.compile(r"(?<![\w.,/])(\d+(?:[.,]\d+)?|\d+/\d+)\s*[mk]?W\b")
_UNCONNECTED_PREFIX = "unconnected-"


def _is_capacitor_value(value: str) -> bool:
    return bool(_CAPACITOR_VALUE_RE.search(value or ""))


def _is_capacitor(component: ComponentInfo) -> bool:
    is_capacitor_class = bool(
        _CAPACITOR_REF_RE.match(component.reference or "")
        or _CAPACITOR_FOOTPRINT_RE.search(component.lib_id or "")
    )
    return is_capacitor_class and _is_capacitor_value(component.value)


def _is_gnd_net(name: str) -> bool:
    return bool(_GND_NET_RE.search(name or ""))


def _is_resistor(component: ComponentInfo) -> bool:
    if _RESISTOR_REF_RE.match(component.reference or ""):
        return True
    return bool(_RESISTOR_VALUE_RE.search(component.value or ""))


def _has_power_field(component: ComponentInfo) -> bool:
    if any(_POWER_FIELD_RE.search(key) for key in component.extra_fields):
        return True
    texts = [component.value or ""] + [str(text or "") for text in component.extra_fields.values()]
    return any(_POWER_RATING_RE.search(text) for text in texts)


# ---------------------------------------------------------------------------
# SCH-001 — undecoupled power_in pin
# ---------------------------------------------------------------------------

@rule(
    "SCH-001", "SCHEMATIC",
    "Undecoupled power input pin",
    "A component pin whose schematic electrical type is power_in has no "
    "capacitor connected between its net and a ground net.",
    default_severity="MEDIUM",
    reference="",
    remediation_template="Add a decoupling capacitor between {net} and ground, close to {ref}.",
)
def _check_undecoupled_power_pins(context: SchematicRuleContext) -> list:
    netlist = context.netlist
    components_by_ref = {c.reference: c for c in netlist.components}
    # Grounds as the netlist classified them -- a ground symbol first, the
    # name as a hint -- and by name for a net the netlist does not list.
    grounds = {net.name for net in netlist.nets if getattr(net, "ground_source", "")}

    def _is_ground(net_name):
        return net_name in grounds or _is_gnd_net(net_name)

    # net_name -> set of component references with a pad on that net
    net_to_refs: dict[str, set] = {}
    for component in netlist.components:
        for net_name in component.nets_by_pad.values():
            net_to_refs.setdefault(net_name, set()).add(component.reference)

    findings = []
    seen_nets: set[tuple[str, str]] = set()  # (component_ref, net_name) already reported
    for component in netlist.components:
        for pad_number, net_name in component.nets_by_pad.items():
            etype = component.pin_types_by_pad.get(pad_number)
            if etype != "power_in":
                continue
            if net_name.startswith(_UNCONNECTED_PREFIX):
                # A power input left unconnected has no net to decouple;
                # whether it should be connected is another question.
                continue
            if _is_ground(net_name):
                # A power_in pin tied directly to ground (e.g. a GND pin
                # itself) has nothing to be decoupled against.
                continue
            key = (component.reference, net_name)
            if key in seen_nets:
                continue
            seen_nets.add(key)

            decoupled = False
            for other_ref in net_to_refs.get(net_name, ()):
                other = components_by_ref.get(other_ref)
                if other is None or not _is_capacitor(other):
                    continue
                if any(_is_ground(n) for n in other.nets_by_pad.values()):
                    decoupled = True
                    break

            if not decoupled:
                findings.append(AnalysisFinding(
                    rule_id="SCH-001",
                    category="SCHEMATIC",
                    severity=FindingSeverity.MEDIUM,
                    title="Undecoupled power input pin",
                    description=(
                        f"{component.reference} pad {pad_number} is a power_in pin on net "
                        f"'{net_name}' with no decoupling capacitor to ground found on that net."
                    ),
                    recommendation=f"Add a decoupling capacitor between {net_name} and ground, close to {component.reference}.",
                    # The pin type is a fact; "no capacitor to ground" is a
                    # verdict resting on recognising capacitors by class and
                    # value and grounds by name. DETERMINISTIC is for facts
                    # about the run (docs/audit-coherence-analyses.md, R6).
                    confidence=EvidenceConfidence.ESTIMATED,
                    nets=[net_name],
                    components=[component.reference],
                    evidence=[AnalysisEvidence(
                        source="SCHEMATIC",
                        detail=f"Pin electrical_type=power_in from schematic lib_symbols for {component.reference}.",
                    )],
                ))
    return findings


# ---------------------------------------------------------------------------
# SCH-002 — unconnected pin
# ---------------------------------------------------------------------------

@rule(
    "SCH-002", "SCHEMATIC",
    "Unconnected pin",
    "A footprint pad is on a KiCad-synthesized 'unconnected-(...)' net, its pin "
    "carries no no-connect marker in the schematic, and its library does not "
    "declare it a no-connect pin.",
    default_severity="LOW",
    reference="",
    remediation_template="Verify {ref} pad {pad} is intentionally unconnected.",
)
def _check_unconnected_pins(context: SchematicRuleContext) -> list:
    findings = []
    for component in context.netlist.components:
        for pad_number, net_name in component.nets_by_pad.items():
            if not net_name.startswith(_UNCONNECTED_PREFIX):
                continue
            if pad_number in component.no_connect_pads:
                # The designer marked it: unconnected is what was asked for.
                # Every SCH-002 on pic_programmer (77), kit-dev (69) and
                # interf_u (63) was such a pin.
                continue
            if component.pin_types_by_pad.get(pad_number) == "no_connect":
                # The library declares the pin not connected, and KiCad's ERC
                # leaves such a pin alone when it is left open. The 98 findings
                # video kept after markers were honoured were all of this kind.
                continue
            documented = component.pin_types_by_pad.get(pad_number) is not None
            severity = FindingSeverity.LOW if documented else FindingSeverity.INFO
            confidence = EvidenceConfidence.DETERMINISTIC if documented else EvidenceConfidence.HEURISTIC
            findings.append(AnalysisFinding(
                rule_id="SCH-002",
                category="SCHEMATIC",
                severity=severity,
                title="Unconnected pin",
                description=f"{component.reference} pad {pad_number} is unconnected ({net_name}).",
                recommendation=f"Verify {component.reference} pad {pad_number} is intentionally left unconnected.",
                confidence=confidence,
                nets=[net_name],
                components=[component.reference],
            ))
    return findings


# ---------------------------------------------------------------------------
# SCH-003 — missing power-rating data for a resistor on a known-voltage net
# ---------------------------------------------------------------------------

@rule(
    "SCH-003", "SCHEMATIC",
    "Missing power-rating data",
    "A resistor sits on a net with a known nominal voltage but carries no "
    "Power field, so dissipation cannot be checked against its package rating.",
    default_severity="INFO",
    reference="",
    remediation_template="Add a Power field to {ref} or verify manually.",
)
def _check_missing_power_rating(context: SchematicRuleContext) -> list:
    """One finding for the project, naming every resistor concerned.

    It was one INFO per resistor -- 53 on the reference board main, 29 on
    tiny_tapeout -- for a single observation about how the project records its
    parts, which buried the findings that point at a place on the board.
    """
    netlist = context.netlist
    voltage_by_net = {n.name: n.voltage_hint for n in netlist.nets}

    references: list[str] = []
    nets: list[str] = []
    for component in netlist.components:
        if not _is_resistor(component) or _has_power_field(component):
            continue
        on_known_voltage = [
            net_name for net_name in component.nets_by_pad.values()
            if voltage_by_net.get(net_name) is not None
        ]
        if not on_known_voltage:
            continue
        references.append(component.reference)
        for net_name in on_known_voltage:
            if net_name not in nets:
                nets.append(net_name)
    if not references:
        return []
    shown = ", ".join(references[:10]) + (", ..." if len(references) > 10 else "")
    return [AnalysisFinding(
        rule_id="SCH-003",
        category="SCHEMATIC",
        severity=FindingSeverity.INFO,
        title="Missing power-rating data",
        description=(
            f"{len(references)} resistor(s) sit on nets with a nominal voltage and carry no Power "
            f"field on their schematic symbol: {shown}."
        ),
        recommendation=(
            "Add a Power field to these resistors, or check by hand that each one's dissipation "
            "stays under its package rating."
        ),
        confidence=EvidenceConfidence.HEURISTIC,
        nets=nets,
        components=references,
    )]


# ---------------------------------------------------------------------------
# SCH-004 — same-named nets across boards without a declared cross-board link
# ---------------------------------------------------------------------------

@rule(
    "SCH-004", "SCHEMATIC",
    "Cross-board link out of date",
    "In a Zeo multi-board project, a link the container declares no longer "
    "matches a board: the pin is on another net, or the board, component or "
    "pad is gone; or a declared differential pair names no link.",
    default_severity="HIGH",
    reference="",
    remediation_template="Update the multi-board schematic or the board so the link {net} matches.",
)
def _check_cross_board_links(context: SchematicRuleContext) -> list:
    """Only with a Zeo container, and only on what it declares.

    The rule used to flag nets sharing a name on several boards. A name is a
    label (plan P2): two boards may each have a GND that nothing joins, and a
    link may join nets named differently. What can be wrong is a declaration
    the boards no longer honour, which validate_links has already measured.
    """
    zeo = context.zeo
    if zeo is None:
        return []
    board_uuid = context.netlist.board_uuid
    # Analysing one board of the container: the links that reach it. Otherwise all of them.
    concerned = [
        link for link in zeo.links
        if board_uuid not in zeo.boards or any(e.board_uuid == board_uuid for e in link.endpoints)
    ]
    findings = []
    for link in concerned:
        for endpoint in link.endpoints:
            if endpoint.state not in ("stale", "missing"):
                continue
            board = zeo.boards.get(endpoint.board_uuid)
            where = board.pro_path.stem if board is not None else endpoint.board_uuid
            findings.append(AnalysisFinding(
                rule_id="SCH-004",
                category="SCHEMATIC",
                severity=FindingSeverity.HIGH,
                title="Cross-board link out of date",
                description=(
                    f"Link {link.name}: on board {where}, {endpoint.detail}"
                    + (" -- the boards no longer agree on what this link connects."
                       if endpoint.state == "stale" else " -- this end of the link is not on the board.")
                ),
                recommendation=f"Update the multi-board schematic or board {where} so link {link.name} matches.",
                confidence=EvidenceConfidence.DETERMINISTIC,
                nets=[name for name in (endpoint.net, endpoint.pin_name) if name],
                components=[endpoint.component] if endpoint.component else [],
            ))
    for issue in zeo.diff_pair_issues:
        findings.append(AnalysisFinding(
            rule_id="SCH-004",
            category="SCHEMATIC",
            severity=FindingSeverity.MEDIUM,
            title="Cross-board differential pair names no link",
            description=f"The container declares a {issue}.",
            recommendation="Rename the pair's nets in the multi-board schematic to the links that carry it.",
            confidence=EvidenceConfidence.DETERMINISTIC,
        ))
    return findings


# ---------------------------------------------------------------------------
# SCH-005 — PCB footprint with no matching schematic symbol
# ---------------------------------------------------------------------------

@rule(
    "SCH-005", "SCHEMATIC",
    "Footprint whose symbol is gone",
    "A PCB footprint records a schematic path that names no symbol of the schematic.",
    default_severity="MEDIUM",
    reference="",
    remediation_template="Update the PCB from the schematic, or delete {ref} if it is no longer wanted.",
)
def _check_pcb_schematic_consistency(context: SchematicRuleContext) -> list:
    """Footprints that came from a symbol the schematic no longer holds.

    It compared references and reported every footprint whose reference the
    schematic lacked: 159 on tiny_tapeout and 28 on cm5_minima, all footprints
    placed on the board without ever having a symbol (logos, stitching vias,
    mounting holes), plus every copy of a multi-instance sheet the reader used
    to miss. Only a footprint with a path can have lost its symbol.
    """
    if context.schematic is None:
        return []
    findings = []
    for component in context.netlist.components:
        if component.schematic_link != "symbol_missing":
            continue
        findings.append(AnalysisFinding(
            rule_id="SCH-005",
            category="SCHEMATIC",
            severity=FindingSeverity.MEDIUM,
            title="Footprint whose symbol is gone",
            description=(
                f"Footprint {component.reference} was placed from a schematic symbol that the "
                "schematic no longer contains: the board has not been updated since it was deleted."
            ),
            recommendation=(
                f"Update the PCB from the schematic, or delete {component.reference} if it is no longer wanted."
            ),
            confidence=EvidenceConfidence.DETERMINISTIC,
            components=[component.reference],
        ))
    return findings


@rule(
    "SCH-006", "SCHEMATIC",
    "Footprints placed on the board only",
    "Footprints with no schematic path: placed on the PCB directly, never from a symbol.",
    default_severity="INFO",
    reference="",
    remediation_template="Nothing to do if {ref} are mechanical or graphical parts.",
)
def _report_board_only_footprints(context: SchematicRuleContext) -> list:
    """One note for the project: such footprints are normal, and rules that read the schematic skip them."""
    if context.schematic is None:
        return []
    references = [c.reference for c in context.netlist.components if c.schematic_link == "pcb_only"]
    if not references:
        return []
    shown = ", ".join(references[:10]) + (", ..." if len(references) > 10 else "")
    return [AnalysisFinding(
        rule_id="SCH-006",
        category="SCHEMATIC",
        severity=FindingSeverity.INFO,
        title="Footprints placed on the board only",
        description=(
            f"{len(references)} footprint(s) have no schematic symbol and were placed on the board "
            f"directly: {shown}. Rules that read pin types from the schematic do not cover them."
        ),
        recommendation="Nothing to do if these are mechanical or graphical parts.",
        confidence=EvidenceConfidence.DETERMINISTIC,
        components=references,
    )]
