"""Run the schematic-derived rules against a project on disk.

`rules/schematic_rules.py` implements five deterministic checks that need no
solver at all -- only the ingest layer. They were written, tested, and then
never reached from anywhere the user can press, so none of them has ever run.
This module is the missing connection.

It works from files rather than the live IPC snapshot on purpose: the rules
need schematic pin types, component values and MPNs, which the board snapshot
does not carry. Reading the .kicad_pcb and .kicad_sch directly is what the
ingest layer was built for.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

try:
    from ..analysis_contract import (
        AnalysisEvidence, AnalysisMetric, AnalysisResult, AnalysisStatus,
        FindingSeverity,
    )
except (ImportError, ValueError):
    from analysis_contract import (
        AnalysisEvidence, AnalysisMetric, AnalysisResult, AnalysisStatus,
        FindingSeverity,
    )


@dataclass
class SchematicRunRequest:
    """Where the project lives. Everything else is read from it."""
    project_path: str = ""
    board_path: str = ""


@dataclass
class SchematicControllerCallbacks:
    on_log: Callable[[str], None] = lambda message: None
    on_progress: Callable[..., None] = lambda *args: None
    on_complete: Callable[[Any], None] = lambda result: None
    on_error: Callable[[Exception], None] = lambda exc: None


class SchematicRuleEngine:
    """Engine protocol: solve(request, emit_log, emit_progress, cancelled)."""

    DOMAIN = "SCHEMATIC"

    def solve(self, request, emit_log, emit_progress, cancelled) -> AnalysisResult:
        from ingest.board_reader import read_board
        from ingest.netlist_builder import build_netlist
        from ingest.schematic_reader import read_schematic
        from rules.registry import DEFAULT_REGISTRY
        from rules.schematic_rules import SchematicRuleContext

        result = AnalysisResult(analysis_type="SCHEMATIC", title="Schematic Rules")

        board_path = Path(request.board_path) if request.board_path else None
        if board_path is None or not board_path.is_file():
            result.limitations.append(
                "No .kicad_pcb path was supplied, so no schematic rule could run."
            )
            return result.finish(AnalysisStatus.NO_DATA)

        emit_progress(1, 4, "board")
        emit_log(f"Reading board geometry from {board_path.name}.")
        board = read_board(board_path)

        # The schematic sits beside the PCB and shares its stem. A project
        # whose schematic is missing still yields the PCB-only rules rather
        # than failing outright.
        emit_progress(2, 4, "schematic")
        schematic = None
        schematic_path = board_path.with_suffix(".kicad_sch")
        if schematic_path.is_file():
            try:
                schematic = read_schematic(schematic_path)
                emit_log(
                    f"Read {len(schematic.instances)} symbol instance(s) from "
                    f"{schematic_path.name}."
                )
            except OSError as exc:
                emit_log(f"Schematic could not be read ({exc}); PCB-only rules will run.")
        else:
            emit_log(
                f"No schematic found at {schematic_path.name}; PCB-only rules will run."
            )

        if cancelled():
            return result.finish(AnalysisStatus.CANCELLED)

        emit_progress(3, 4, "netlist")
        netlist = build_netlist(board, schematic, board_uuid=board_path.stem)

        emit_progress(4, 4, "rules")
        findings = DEFAULT_REGISTRY.evaluate_domain(
            self.DOMAIN, SchematicRuleContext(netlist=netlist, schematic=schematic),
        )
        result.findings.extend(findings)

        power_rails = [net for net in netlist.nets if net.is_power_rail]
        result.metrics.extend([
            AnalysisMetric("component_count", "Components", len(netlist.components), status="INFO"),
            AnalysisMetric("net_count", "Nets", len(netlist.nets), status="INFO"),
            AnalysisMetric(
                "power_rail_count", "Power rails (by pin type)", len(power_rails),
                status="INFO",
            ),
            AnalysisMetric(
                "rule_count", "Rules evaluated",
                len(DEFAULT_REGISTRY.by_domain(self.DOMAIN)), status="INFO",
            ),
        ])
        result.summary = {
            "component_count": len(netlist.components),
            "net_count": len(netlist.nets),
            "power_rail_count": len(power_rails),
            "schematic_available": schematic is not None,
        }
        result.provenance.append(AnalysisEvidence(
            "SCHEMATIC" if schematic is not None else "PCB_GEOMETRY",
            "Rules read the project files directly; pin electrical types come "
            "from the schematic rather than from net-name patterns."
            if schematic is not None else
            "No schematic was available, so rules needing pin types were skipped.",
            reference=str(schematic_path if schematic is not None else board_path),
        ))
        if schematic is None:
            result.limitations.append(
                "No schematic was read, so rules that depend on pin electrical "
                "types (decoupling, unconnected pins) could not be evaluated."
            )

        emit_log(f"Schematic rules produced {len(result.findings)} finding(s).")
        severities = {finding.severity for finding in result.findings}
        if FindingSeverity.CRITICAL in severities:
            status = AnalysisStatus.FAIL
        elif severities & {FindingSeverity.HIGH, FindingSeverity.MEDIUM}:
            status = AnalysisStatus.WARN
        else:
            status = AnalysisStatus.PASS
        return result.finish(status)
