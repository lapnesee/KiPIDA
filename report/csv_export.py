"""CSV exports for campaign findings, deduplicated actions and domains.

Written with a UTF-8 BOM (``utf-8-sig``) so Excel opens accented net and
component names correctly instead of mojibake.

Every row carries the campaign it came from, and finding rows the run and
build behind them: P02 Rev E's KiPIDA-results held 39 findings-*.csv by
2026-10-02 whose rows named neither, so two files pasted into one sheet
could not be told apart.
"""

from pathlib import Path
from typing import Any, List, Optional
import csv

from campaign import CampaignResult, action_bucket

_MULTI_VALUE_SEPARATOR = ";"

FINDING_COLUMNS: List[str] = [
    "campaign_id", "run_id", "domain", "domain_status", "rule_id", "finding_id", "severity", "confidence",
    "title", "description", "nets", "components", "recommendation",
    "remediation_action", "remediation_target", "current_value",
    "proposed_value", "unit", "predicted_gain", "effort", "verified",
    "tool_version",
]

# decision: the report's list -- fix, review, prototype or data.
ACTION_COLUMNS: List[str] = [
    "campaign_id", "rank", "action_id", "title", "severity", "decision", "domains", "rule_ids",
    "nets", "components", "effort", "gain_rank", "consequences",
    "remediation_actions", "verified_remediations", "recommendations",
]

# One row per selected domain, those that did not complete included, with
# the campaign's verdict beside each.
DOMAIN_COLUMNS: List[str] = [
    "campaign_id", "created_at", "campaign_status", "domain", "status", "score",
    "finding_counts", "run_id", "completed_at", "board_fingerprint",
    "elapsed_seconds", "tool_version", "reason",
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
                "campaign_id": campaign.campaign_id,
                "run_id": result.run_id,
                "domain": result.analysis_type,
                "domain_status": result.status.value,
                "rule_id": finding.rule_id,
                "finding_id": finding.finding_id,
                "severity": finding.severity.value,
                "confidence": finding.confidence.value,
                "title": finding.title,
                "description": finding.description,
                "nets": _join(finding.nets),
                "components": _join(finding.components),
                "recommendation": finding.recommendation,
                "tool_version": result.tool_version,
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
            "campaign_id": campaign.campaign_id,
            "rank": rank,
            "action_id": action.action_id,
            "title": action.title,
            "severity": action.severity.value,
            "decision": action_bucket(action),
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
            # Free-text advice from findings with no structured Remediation
            # (e.g. the differential recommender's geometry suggestion);
            # empty when a structured remediation_actions entry already
            # covers the action.
            "recommendations": _join(action.recommendations),
        })
    return _write(path, ACTION_COLUMNS, rows)


def write_domains_csv(campaign: CampaignResult, path: Path) -> Path:
    """One row per domain scored, then one per selected domain that did not
    complete, with its state as status and why."""
    common = {
        "campaign_id": campaign.campaign_id,
        "created_at": campaign.created_at,
        "campaign_status": campaign.overall_status.value,
    }
    rows = []
    for score, result in zip(campaign.domain_scores, campaign.results):
        rows.append({
            **common,
            "domain": score.domain,
            "status": score.status.value,
            "score": round(score.score, 1),
            "finding_counts": _join(
                f"{severity}={count}" for severity, count in score.finding_counts.items() if count
            ),
            "run_id": result.run_id,
            "completed_at": score.completed_at,
            "board_fingerprint": score.board_fingerprint,
            "elapsed_seconds": round(result.elapsed_seconds, 2),
            "tool_version": result.tool_version,
            "reason": "",
        })
    for item in campaign.coverage:
        rows.append({
            **{column: "" for column in DOMAIN_COLUMNS}, **common,
            "domain": item.get("domain", ""),
            "status": item.get("state", ""),
            "reason": item.get("reason", ""),
        })
    return _write(path, DOMAIN_COLUMNS, rows)
