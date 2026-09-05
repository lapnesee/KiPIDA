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
    all_board_netlists: Optional[list] = None  # list[BoardNetlist], for SCH-004


_CAPACITOR_VALUE_RE = re.compile(r"\d+\.?\d*\s*[pnuµ]f", re.IGNORECASE)
_GND_NET_RE = re.compile(r"gnd", re.IGNORECASE)
_RESISTOR_REF_RE = re.compile(r"^R\d", re.IGNORECASE)
_RESISTOR_VALUE_RE = re.compile(r"\d+\.?\d*\s*[kKmM]?\s*(ohm|Ω|r)\b", re.IGNORECASE)
_POWER_FIELD_RE = re.compile(r"power|watt", re.IGNORECASE)


def _is_capacitor_value(value: str) -> bool:
    return bool(_CAPACITOR_VALUE_RE.search(value or ""))


def _is_gnd_net(name: str) -> bool:
    return bool(_GND_NET_RE.search(name or ""))


def _is_resistor(component: ComponentInfo) -> bool:
    if _RESISTOR_REF_RE.match(component.reference or ""):
        return True
    return bool(_RESISTOR_VALUE_RE.search(component.value or ""))


def _has_power_field(component: ComponentInfo) -> bool:
    return any(_POWER_FIELD_RE.search(key) for key in component.extra_fields)


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
            if _is_gnd_net(net_name):
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
                if other is None or not _is_capacitor_value(other.value):
                    continue
                if any(_is_gnd_net(n) for n in other.nets_by_pad.values()):
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
                    confidence=EvidenceConfidence.DETERMINISTIC,
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
    "A footprint pad is on a KiCad-synthesized 'unconnected-(...)' net.",
    default_severity="LOW",
    reference="",
    remediation_template="Verify {ref} pad {pad} is intentionally unconnected.",
)
def _check_unconnected_pins(context: SchematicRuleContext) -> list:
    findings = []
    for component in context.netlist.components:
        for pad_number, net_name in component.nets_by_pad.items():
            if not net_name.startswith("unconnected-"):
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
    netlist = context.netlist
    voltage_by_net = {n.name: n.voltage_hint for n in netlist.nets}

    findings = []
    for component in netlist.components:
        if not _is_resistor(component):
            continue
        if _has_power_field(component):
            continue
        for net_name in component.nets_by_pad.values():
            voltage_hint = voltage_by_net.get(net_name)
            if voltage_hint is None:
                continue
            findings.append(AnalysisFinding(
                rule_id="SCH-003",
                category="SCHEMATIC",
                severity=FindingSeverity.INFO,
                title="Missing power-rating data",
                description=(
                    f"{component.reference} is on net '{net_name}' (~{voltage_hint}V) "
                    "with no Power field on the schematic symbol."
                ),
                recommendation=(
                    f"Add a Power field to {component.reference} or verify manually that "
                    f"dissipation stays under the package rating for {voltage_hint}V across it."
                ),
                confidence=EvidenceConfidence.HEURISTIC,
                nets=[net_name],
                components=[component.reference],
            ))
            break  # one finding per component is enough
    return findings


# ---------------------------------------------------------------------------
# SCH-004 — same-named nets across boards without a declared cross-board link
# ---------------------------------------------------------------------------

@rule(
    "SCH-004", "SCHEMATIC",
    "Undeclared cross-board net name collision",
    "Nets with the same name exist on multiple boards of a multi-board "
    "project without being declared as a cross-board net.",
    default_severity="HIGH",
    reference="",
    remediation_template="Confirm nets named {net} across boards are intentionally independent.",
)
def _check_cross_board_net_collisions(context: SchematicRuleContext) -> list:
    netlists = context.all_board_netlists
    if not netlists or len(netlists) < 2:
        return []

    # name -> {board_uuid: NetInfo}
    by_name: dict[str, dict] = {}
    for nl in netlists:
        for net in nl.nets:
            by_name.setdefault(net.name, {})[nl.board_uuid] = net

    findings = []
    for name, per_board in by_name.items():
        if len(per_board) < 2:
            continue
        board_uuids = list(per_board.keys())
        declared = True
        for i, uuid_a in enumerate(board_uuids):
            net_a = per_board[uuid_a]
            for uuid_b in board_uuids[i + 1:]:
                if f"{uuid_b}:{name}" not in net_a.aliases:
                    declared = False
                    break
            if not declared:
                break
        if declared:
            continue
        findings.append(AnalysisFinding(
            rule_id="SCH-004",
            category="SCHEMATIC",
            severity=FindingSeverity.HIGH,
            title="Undeclared cross-board net name collision",
            description=(
                f"Nets named '{name}' exist on {len(per_board)} boards "
                "without being declared as a cross-board net — verify they are not "
                "accidentally isolated or unintentionally merged."
            ),
            recommendation=f"Confirm nets named {name} across boards are intentionally independent.",
            confidence=EvidenceConfidence.DETERMINISTIC,
            nets=[name],
        ))
    return findings


# ---------------------------------------------------------------------------
# SCH-005 — PCB footprint with no matching schematic symbol
# ---------------------------------------------------------------------------

@rule(
    "SCH-005", "SCHEMATIC",
    "PCB/schematic reference mismatch",
    "A PCB footprint reference has no matching symbol in the schematic.",
    default_severity="MEDIUM",
    reference="",
    remediation_template="Confirm {ref} is DNP-consistent between PCB and schematic.",
)
def _check_pcb_schematic_consistency(context: SchematicRuleContext) -> list:
    if context.schematic is None:
        return []
    schematic_refs = {inst.reference for inst in context.schematic.instances if inst.reference}

    findings = []
    for component in context.netlist.components:
        if component.reference and component.reference not in schematic_refs:
            findings.append(AnalysisFinding(
                rule_id="SCH-005",
                category="SCHEMATIC",
                severity=FindingSeverity.MEDIUM,
                title="PCB/schematic reference mismatch",
                description=f"Footprint {component.reference} on the PCB has no matching schematic symbol.",
                recommendation=f"Confirm {component.reference} is DNP-consistent between PCB and schematic.",
                confidence=EvidenceConfidence.DETERMINISTIC,
                components=[component.reference],
            ))
    return findings
