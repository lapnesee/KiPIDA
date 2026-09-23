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
    EvidenceConfidence,
    FindingSeverity,
    Remediation,
    RemediationEffort,
    _json_safe,
    _utc_now,
)


CAMPAIGN_SCHEMA_VERSION = "1.0.0"

# Score penalty per finding, by severity.  INFO costs nothing: an
# informational finding is a note, not a defect.  These are the EMC analyzer's
# weights, so a domain score reads on the same scale whichever domain it is.
SEVERITY_PENALTY: Dict[FindingSeverity, float] = {
    FindingSeverity.CRITICAL: 25.0,
    FindingSeverity.HIGH: 12.0,
    FindingSeverity.MEDIUM: 6.0,
    FindingSeverity.LOW: 2.0,
    FindingSeverity.INFO: 0.0,
}

# How much of its severity a finding costs, by what it rests on.  A heuristic
# that fires can say where to look; it must not be able to sink a domain the
# way an observed fact does.  The EMC analyzer weighs HIGH/MEDIUM/LOW 1/0.65/
# 0.25; these are the same weights on the evidence basis.
CONFIDENCE_WEIGHT: Dict[EvidenceConfidence, float] = {
    EvidenceConfidence.DETERMINISTIC: 1.0,
    EvidenceConfidence.MEASURED: 1.0,
    EvidenceConfidence.DATASHEET_BACKED: 1.0,
    EvidenceConfidence.ESTIMATED: 0.65,
    EvidenceConfidence.HEURISTIC: 0.25,
}

# A rule that fires two hundred times reports one systematic cause, not two
# hundred defects: only its worst few findings are charged to the score.
MAX_FINDINGS_PER_RULE_IN_SCORE = 3

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

# Confidence levels whose weight is 1.0 in CONFIDENCE_WEIGHT: a fact about the
# run or the part, not a model's estimate.  A domain FAILs only on a CRITICAL
# finding or a HIGH one resting on this tier -- the same test for every
# domain, so a differential pair and a DC rail are judged the same way. Before
# this rule each adapter hard-coded its own FAIL/WARN split: the differential
# adapter FAILed its domain on any failed pair while DC/EMC/CFD/thermal only
# ever WARNed on a HIGH finding, so P02 Rev E's global FAIL rested solely on
# two SI-DIFF-001 HIGH ESTIMATED findings (-14.7 % / -15.9 %) while a DC-001
# HIGH ESTIMATED finding on the same campaign only produced a WARN.
_HARD_CONFIDENCE = frozenset((
    EvidenceConfidence.DETERMINISTIC,
    EvidenceConfidence.MEASURED,
    EvidenceConfidence.DATASHEET_BACKED,
))


def domain_status_from_findings(findings: List[AnalysisFinding]) -> AnalysisStatus:
    """Derive a domain's PASS/WARN/FAIL from its findings, the same rule for
    every domain: FAIL on any CRITICAL finding, or a HIGH one whose confidence
    is DETERMINISTIC/MEASURED/DATASHEET_BACKED (a fact, not an estimate); WARN
    on any other HIGH, or a MEDIUM; PASS otherwise.
    """
    if any(finding.severity == FindingSeverity.CRITICAL for finding in findings):
        return AnalysisStatus.FAIL
    if any(
        finding.severity == FindingSeverity.HIGH and finding.confidence in _HARD_CONFIDENCE
        for finding in findings
    ):
        return AnalysisStatus.FAIL
    if any(
        finding.severity in (FindingSeverity.HIGH, FindingSeverity.MEDIUM)
        for finding in findings
    ):
        return AnalysisStatus.WARN
    return AnalysisStatus.PASS

# A finding naming more parts than this surveys the design ("49 resistors lack
# a power rating") rather than blaming a part, so it ties no other finding to
# it through a part they share.
GROUPING_MAX_COMPONENTS = 6


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


def _finding_penalty(finding: AnalysisFinding) -> float:
    return (
        SEVERITY_PENALTY.get(finding.severity, 0.0)
        * CONFIDENCE_WEIGHT.get(finding.confidence, CONFIDENCE_WEIGHT[EvidenceConfidence.HEURISTIC])
    )


def score_findings(findings: List[AnalysisFinding]) -> float:
    """100 minus each rule's worst few findings, weighted by what they rest on."""
    by_rule: Dict[str, List[float]] = {}
    for finding in findings:
        by_rule.setdefault(finding.rule_id, []).append(_finding_penalty(finding))
    penalty = sum(
        sum(sorted(penalties, reverse=True)[:MAX_FINDINGS_PER_RULE_IN_SCORE])
        for penalties in by_rule.values()
    )
    return max(0.0, 100.0 - penalty)


def _group_findings(tagged: List[tuple]) -> List[List[tuple]]:
    """Group findings into physical problems, transitively and order-free.

    Two findings describe one problem when they share a component -- two
    domains blaming the same part point at the same thing to change -- or
    when they share a net *and* carry evidence at the same spot on the same
    layer.  A shared net alone is not enough: nearly every finding names the
    ground or a rail, and "shares a net" merged a PDN impedance miss, a
    fragmented plane and a DC solve that did not converge into one action
    ranked first, because each mentioned GND or +3V3.

    Grouping used to be greedy too: a finding joined the first group it
    matched and stopped, so one linking two existing groups left them apart,
    and reordering the domains changed the actions.  Union-find makes the
    grouping a property of the findings alone.  Groups keep the order of
    their first finding, so action ids stay stable.
    """
    parent = list(range(len(tagged)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def join(left: int, right: int) -> None:
        a, b = root(left), root(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    by_component: Dict[str, List[int]] = {}
    by_net: Dict[str, List[int]] = {}
    for index, (_domain, finding) in enumerate(tagged):
        components = set(finding.components)
        if len(components) > GROUPING_MAX_COMPONENTS:
            # On P02 Rev E SCH-003's 49 parts included C11, which DE-001 named
            # beside U18, and the two made one action ranked first.
            components = set()
        for component in components:
            by_component.setdefault(component, []).append(index)
        for net in set(finding.nets):
            by_net.setdefault(net, []).append(index)
    for members in by_component.values():
        for other in members[1:]:
            join(members[0], other)
    for members in by_net.values():
        for position, left in enumerate(members):
            for right in members[position + 1:]:
                if root(left) != root(right) and _colocated(tagged[left][1], tagged[right][1]):
                    join(left, right)

    groups: Dict[int, List[tuple]] = {}
    for index, entry in enumerate(tagged):
        groups.setdefault(root(index), []).append(entry)
    return [groups[key] for key in sorted(groups)]


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
    # Which host launched the run and the mode it put the project in
    # (ingest.project_context.ProjectContext.to_dict()); empty when unknown.
    project_context: Dict[str, Any] = field(default_factory=dict)
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
            # A domain that grades itself -- the EMC analyzer's risk score --
            # keeps its own figure, so the campaign and the domain's own tab
            # never show two different scores for the same analysis.
            own = (result.summary or {}).get("domain_score")
            score = (
                max(0.0, min(100.0, float(own))) if own is not None
                else score_findings(result.findings)
            )
            # PASS/WARN/FAIL is derived centrally from the findings
            # (domain_status_from_findings), not read off the adapter: an
            # adapter's own status is a hint the rule may downgrade or
            # upgrade, so every domain FAILs and WARNs on the same evidence
            # bar. ERROR/CANCELLED are not about severity at all and pass
            # through unchanged.
            status = (
                domain_status_from_findings(result.findings)
                if result.status in (AnalysisStatus.PASS, AnalysisStatus.WARN, AnalysisStatus.FAIL)
                else result.status
            )
            scores.append(DomainScore(
                domain=result.analysis_type,
                status=status,
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
        groups = _group_findings(tagged)

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
            "project_context": _json_safe(dict(self.project_context)),
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
            project_context=dict(payload.get("project_context") or {}),
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
        project_context: Optional[Dict[str, Any]] = None,
    ) -> "CampaignResult":
        """Build and immediately aggregate a campaign from analysis results."""
        fingerprint = board_fingerprint or next(
            (r.board_fingerprint for r in results if r.board_fingerprint), "",
        )
        return cls(
            project_name=project_name,
            board_fingerprint=fingerprint,
            project_context=dict(project_context or {}),
            results=list(results),
        ).recompute()
