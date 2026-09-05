"""CSV exports for campaign findings and deduplicated actions.

Written with a UTF-8 BOM (``utf-8-sig``) so Excel opens accented net and
component names correctly instead of mojibake.
"""

from pathlib import Path
from typing import Any, List, Optional
import csv

from campaign import CampaignResult

_MULTI_VALUE_SEPARATOR = ";"

FINDING_COLUMNS: List[str] = [
    "domain", "rule_id", "finding_id", "severity", "confidence",
    "title", "description", "nets", "components", "recommendation",
    "remediation_action", "remediation_target", "current_value",
    "proposed_value", "unit", "predicted_gain", "effort", "verified",
]

ACTION_COLUMNS: List[str] = [
    "rank", "action_id", "title", "severity", "domains", "rule_ids",
    "nets", "components", "effort", "gain_rank", "consequences",
    "remediation_actions", "verified_remediations",
]


def _join(values) -> str:
    return _MULTI_VALUE_SEPARATOR.join(str(item) for item in values)


def _number(value: Optional[float]) -> Any:
    return "" if value is None else value


def _write(path: Path, columns: List[str], rows: List[dict]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_findings_csv(campaign: CampaignResult, path: Path) -> Path:
    """One row per finding; a finding with N remediations yields N rows."""
    rows: List[dict] = []
    for result in campaign.results:
        for finding in result.findings:
            base = {
                "domain": result.analysis_type,
                "rule_id": finding.rule_id,
                "finding_id": finding.finding_id,
                "severity": finding.severity.value,
                "confidence": finding.confidence.value,
                "title": finding.title,
                "description": finding.description,
                "nets": _join(finding.nets),
                "components": _join(finding.components),
                "recommendation": finding.recommendation,
            }
            if not finding.remediations:
                rows.append({
                    **base,
                    "remediation_action": "", "remediation_target": "",
                    "current_value": "", "proposed_value": "", "unit": "",
                    "predicted_gain": "", "effort": "", "verified": "",
                })
                continue
            for remediation in finding.remediations:
                rows.append({
                    **base,
                    "remediation_action": remediation.action,
                    "remediation_target": remediation.target,
                    "current_value": _number(remediation.current_value),
                    "proposed_value": _number(remediation.proposed_value),
                    "unit": remediation.unit,
                    "predicted_gain": remediation.predicted_gain,
                    "effort": remediation.effort.value,
                    "verified": str(remediation.verified).lower(),
                })
    return _write(path, FINDING_COLUMNS, rows)


def write_actions_csv(campaign: CampaignResult, path: Path) -> Path:
    """One row per deduplicated action, ordered by descending gain_rank."""
    rows = []
    for rank, action in enumerate(campaign.top_actions(limit=len(campaign.actions)), start=1):
        rows.append({
            "rank": rank,
            "action_id": action.action_id,
            "title": action.title,
            "severity": action.severity.value,
            "domains": _join(action.domains),
            "rule_ids": _join(action.rule_ids),
            "nets": _join(action.nets),
            "components": _join(action.components),
            "effort": action.effort.value,
            "gain_rank": round(action.gain_rank, 3),
            "consequences": _join(action.consequences),
            "remediation_actions": _join(item.action for item in action.remediations),
            "verified_remediations": sum(
                1 for item in action.remediations if item.verified
            ),
        })
    return _write(path, ACTION_COLUMNS, rows)
