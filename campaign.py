"""Cross-domain campaign aggregation: many AnalysisResult, one verdict.

A board defect rarely shows up in one domain only.  A fragmented ground
plane raises a DC drop finding, an EMC return-path finding, and a
differential reference-change finding -- three reports of one physical
problem.  CampaignResult merges them into a single ranked action list so
the user fixes the board, not the report.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional
import json
import math
import uuid

from analysis_contract import (
    AnalysisFinding,
    AnalysisResult,
    AnalysisStatus,
    FindingSeverity,
    Remediation,
    RemediationEffort,
    _json_safe,
    _utc_now,
)


CAMPAIGN_SCHEMA_VERSION = "1.0.0"

# Score penalty per finding, by severity.  INFO costs nothing: an
# informational finding is a note, not a defect.
SEVERITY_PENALTY: Dict[FindingSeverity, float] = {
    FindingSeverity.CRITICAL: 25.0,
    FindingSeverity.HIGH: 10.0,
    FindingSeverity.MEDIUM: 4.0,
    FindingSeverity.LOW: 1.0,
    FindingSeverity.INFO: 0.0,
}

# Ranking weights.  Severity sets the base, effort divides it: a cheap fix
# for a moderate problem can legitimately outrank an expensive fix for a
# serious one, which is the whole point of ranking by gain/effort.
SEVERITY_WEIGHT: Dict[FindingSeverity, float] = {
    FindingSeverity.CRITICAL: 100.0,
    FindingSeverity.HIGH: 50.0,
    FindingSeverity.MEDIUM: 20.0,
    FindingSeverity.LOW: 5.0,
    FindingSeverity.INFO: 1.0,
}

EFFORT_DIVISOR: Dict[RemediationEffort, float] = {
    RemediationEffort.LOW: 1.0,
    RemediationEffort.MEDIUM: 2.0,
    RemediationEffort.HIGH: 4.0,
}

# Severity ordering, most severe first, for "highest of the group".
_SEVERITY_ORDER: List[FindingSeverity] = [
    FindingSeverity.CRITICAL,
    FindingSeverity.HIGH,
    FindingSeverity.MEDIUM,
    FindingSeverity.LOW,
    FindingSeverity.INFO,
]

_EFFORT_ORDER: List[RemediationEffort] = [
    RemediationEffort.LOW,
    RemediationEffort.MEDIUM,
    RemediationEffort.HIGH,
]

# Two findings whose evidence sits closer than this on the same layer are
# treated as the same physical location.
COLOCATION_TOLERANCE_MM = 2.0


@dataclass
class DomainScore:
    domain: str
    status: AnalysisStatus
    score: float
    finding_counts: Dict[str, int] = field(default_factory=dict)
    headline: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DomainScore":
        payload = dict(data)
        payload["status"] = AnalysisStatus(str(payload.get("status", "NO_DATA")).upper())
        return cls(**{key: payload[key] for key in cls.__dataclass_fields__ if key in payload})


@dataclass
class CampaignAction:
    """One deduplicated physical problem, with every domain that reports it."""

    action_id: str
    title: str
    severity: FindingSeverity
    domains: List[str] = field(default_factory=list)
    rule_ids: List[str] = field(default_factory=list)
    nets: List[str] = field(default_factory=list)
    components: List[str] = field(default_factory=list)
    remediations: List[Remediation] = field(default_factory=list)
    consequences: List[str] = field(default_factory=list)
    effort: RemediationEffort = RemediationEffort.MEDIUM
    gain_rank: float = 0.0

    @property
    def identity(self) -> str:
        """Stable cross-campaign identity.

        ``action_id`` is generated per campaign and therefore useless for
        before/after matching.  The physical problem is identified by which
        rules fired on which nets/components.
        """
        return "|".join((
            ",".join(sorted(self.rule_ids)),
            ",".join(sorted(self.nets)),
            ",".join(sorted(self.components)),
        ))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CampaignAction":
        payload = dict(data)
        payload.pop("identity", None)
        payload["severity"] = FindingSeverity(str(payload.get("severity", "INFO")).upper())
        payload["effort"] = RemediationEffort(str(payload.get("effort", "MEDIUM")).upper())
        payload["remediations"] = [
            Remediation.from_dict(item) for item in payload.get("remediations", [])
        ]
        return cls(**{key: payload[key] for key in cls.__dataclass_fields__ if key in payload})


def _worst_severity(severities: List[FindingSeverity]) -> FindingSeverity:
    for candidate in _SEVERITY_ORDER:
        if candidate in severities:
            return candidate
    return FindingSeverity.INFO


def _worst_effort(efforts: List[RemediationEffort]) -> RemediationEffort:
    for candidate in reversed(_EFFORT_ORDER):
        if candidate in efforts:
            return candidate
    return RemediationEffort.MEDIUM


def _colocated(left: AnalysisFinding, right: AnalysisFinding) -> bool:
    """True when two findings carry evidence at the same board location."""
    for a in left.evidence:
        if a.x_mm is None or a.y_mm is None:
            continue
        for b in right.evidence:
            if b.x_mm is None or b.y_mm is None:
                continue
            if str(a.layer or "") != str(b.layer or ""):
                continue
            if math.dist((a.x_mm, a.y_mm), (b.x_mm, b.y_mm)) < COLOCATION_TOLERANCE_MM:
                return True
    return False


def _same_problem(left: AnalysisFinding, right: AnalysisFinding) -> bool:
    """Whether two findings describe one physical defect.

    Shared nets or shared components are the strongest signal; failing
    those, evidence pinned to the same spot on the same layer.
    """
    if set(left.nets) & set(right.nets):
        return True
    if set(left.components) & set(right.components):
        return True
    return _colocated(left, right)


def _dedupe_remediations(remediations: List[Remediation]) -> List[Remediation]:
    """Collapse remediations describing the same change to the same target."""
    seen: Dict[tuple, Remediation] = {}
    for remediation in remediations:
        key = (remediation.action, remediation.target)
        # Keep the verified one when the same change arrives twice: a
        # re-simulated prediction supersedes a first-order estimate.
        existing = seen.get(key)
        if existing is None or (remediation.verified and not existing.verified):
            seen[key] = remediation
    return list(seen.values())


@dataclass
class CampaignResult:
    campaign_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: str = CAMPAIGN_SCHEMA_VERSION
    created_at: str = field(default_factory=_utc_now)
    board_fingerprint: str = ""
    project_name: str = ""
    results: List[AnalysisResult] = field(default_factory=list)
    domain_scores: List[DomainScore] = field(default_factory=list)
    actions: List[CampaignAction] = field(default_factory=list)
    overall_status: AnalysisStatus = AnalysisStatus.NO_DATA

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def recompute(self) -> "CampaignResult":
        """Recalculate domain scores, deduplicated actions, and the verdict."""
        self.domain_scores = self._compute_domain_scores()
        self.actions = self._compute_actions()
        self.overall_status = self._compute_overall_status()
        return self

    def _compute_domain_scores(self) -> List[DomainScore]:
        scores: List[DomainScore] = []
        for result in self.results:
            counts = result.severity_counts
            if result.status == AnalysisStatus.NO_DATA:
                # A domain that produced nothing is absent, not clean.  Give
                # it no score rather than a perfect one.
                scores.append(DomainScore(
                    domain=result.analysis_type,
                    status=AnalysisStatus.NO_DATA,
                    score=0.0,
                    finding_counts=counts,
                    headline="No data produced for this domain.",
                ))
                continue
            penalty = sum(
                SEVERITY_PENALTY[severity] * counts.get(severity.value, 0)
                for severity in FindingSeverity
            )
            score = max(0.0, 100.0 - penalty)
            scores.append(DomainScore(
                domain=result.analysis_type,
                status=result.status,
                score=score,
                finding_counts=counts,
                headline=self._headline(result),
            ))
        return scores

    @staticmethod
    def _headline(result: AnalysisResult) -> str:
        worst = _worst_severity([finding.severity for finding in result.findings])
        blocking = [f for f in result.findings if f.severity == worst]
        if not blocking or worst == FindingSeverity.INFO:
            return f"{len(result.findings)} finding(s), none above informational."
        return f"{len(blocking)} {worst.value} finding(s); worst: {blocking[0].title}"

    def _compute_actions(self) -> List[CampaignAction]:
        # (domain, finding) pairs across every result, then union-find style
        # grouping by physical identity.
        tagged: List[tuple] = [
            (result.analysis_type, finding)
            for result in self.results
            for finding in result.findings
        ]
        groups: List[List[tuple]] = []
        for entry in tagged:
            _domain, finding = entry
            for group in groups:
                if any(_same_problem(finding, member[1]) for member in group):
                    group.append(entry)
                    break
            else:
                groups.append([entry])

        actions: List[CampaignAction] = []
        for index, group in enumerate(groups, start=1):
            severity = _worst_severity([finding.severity for _d, finding in group])
            domains: List[str] = []
            rule_ids: List[str] = []
            nets: List[str] = []
            components: List[str] = []
            consequences: List[str] = []
            remediations: List[Remediation] = []
            for domain, finding in group:
                if domain not in domains:
                    domains.append(domain)
                if finding.rule_id not in rule_ids:
                    rule_ids.append(finding.rule_id)
                for net in finding.nets:
                    if net not in nets:
                        nets.append(net)
                for component in finding.components:
                    if component not in components:
                        components.append(component)
                consequences.append(f"{domain}: {finding.title}")
                remediations.extend(finding.remediations)

            remediations = _dedupe_remediations(remediations)
            effort = (
                _worst_effort([item.effort for item in remediations])
                if remediations else RemediationEffort.MEDIUM
            )
            # Title comes from the most severe contributing finding.
            lead = next(finding for _d, finding in group if finding.severity == severity)
            action = CampaignAction(
                action_id=f"action-{index:03d}",
                title=lead.title,
                severity=severity,
                domains=domains,
                rule_ids=rule_ids,
                nets=nets,
                components=components,
                remediations=remediations,
                consequences=consequences,
                effort=effort,
            )
            action.gain_rank = (
                SEVERITY_WEIGHT[severity]
                * (1.0 + 0.5 * (len(domains) - 1))
                / EFFORT_DIVISOR[effort]
            )
            actions.append(action)
        actions.sort(key=lambda item: item.gain_rank, reverse=True)
        return actions

    def _compute_overall_status(self) -> AnalysisStatus:
        scored = [
            score for score in self.domain_scores
            if score.status != AnalysisStatus.NO_DATA
        ]
        if not scored:
            return AnalysisStatus.NO_DATA
        has_critical = any(
            action.severity == FindingSeverity.CRITICAL for action in self.actions
        )
        if has_critical or any(score.status == AnalysisStatus.FAIL for score in scored):
            return AnalysisStatus.FAIL
        has_high = any(action.severity == FindingSeverity.HIGH for action in self.actions)
        if has_high or any(score.status == AnalysisStatus.WARN for score in scored):
            return AnalysisStatus.WARN
        return AnalysisStatus.PASS

    def top_actions(self, limit: int = 10) -> List[CampaignAction]:
        return sorted(self.actions, key=lambda item: item.gain_rank, reverse=True)[:limit]

    @property
    def total_elapsed_seconds(self) -> float:
        return sum(float(result.elapsed_seconds or 0.0) for result in self.results)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "board_fingerprint": self.board_fingerprint,
            "project_name": self.project_name,
            "overall_status": self.overall_status.value,
            "results": [result.to_dict() for result in self.results],
            "domain_scores": [_json_safe(asdict(score)) for score in self.domain_scores],
            "actions": [_json_safe(asdict(action)) for action in self.actions],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False) + "\n"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CampaignResult":
        payload = dict(data)
        schema_version = str(payload.get("schema_version", CAMPAIGN_SCHEMA_VERSION))
        if schema_version != CAMPAIGN_SCHEMA_VERSION:
            raise ValueError(f"Unsupported campaign schema: {schema_version}")
        return cls(
            campaign_id=str(payload.get("campaign_id", uuid.uuid4().hex)),
            schema_version=schema_version,
            created_at=str(payload.get("created_at", "")),
            board_fingerprint=str(payload.get("board_fingerprint", "")),
            project_name=str(payload.get("project_name", "")),
            results=[AnalysisResult.from_dict(item) for item in payload.get("results", [])],
            domain_scores=[DomainScore.from_dict(item) for item in payload.get("domain_scores", [])],
            actions=[CampaignAction.from_dict(item) for item in payload.get("actions", [])],
            overall_status=AnalysisStatus(str(payload.get("overall_status", "NO_DATA")).upper()),
        )

    @classmethod
    def from_json(cls, text: str) -> "CampaignResult":
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_results(
        cls,
        results: List[AnalysisResult],
        *,
        project_name: str = "",
        board_fingerprint: str = "",
    ) -> "CampaignResult":
        """Build and immediately aggregate a campaign from analysis results."""
        fingerprint = board_fingerprint or next(
            (r.board_fingerprint for r in results if r.board_fingerprint), "",
        )
        return cls(
            project_name=project_name,
            board_fingerprint=fingerprint,
            results=list(results),
        ).recompute()
