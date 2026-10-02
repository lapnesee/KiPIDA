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
from rule_guide import PROTOTYPE_CHECK, FindingNature, finding_nature, typical_effort


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

    A finding about the calculation (rule_guide CALCULATION) says how far to
    trust the figures, not that the board fails: it can make a domain WARN,
    never FAIL. A non-converged DC solve (DC-002, HIGH and DETERMINISTIC)
    used to fail its domain and the campaign with no action to take.
    """
    board = [finding for finding in findings
             if finding_nature(finding) != FindingNature.CALCULATION]
    if any(finding.severity == FindingSeverity.CRITICAL for finding in board):
        return AnalysisStatus.FAIL
    if any(
        finding.severity == FindingSeverity.HIGH and finding.confidence in _HARD_CONFIDENCE
        for finding in board
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

# DC-001 marks a rail's DC model incomplete (some loads left unsolved). Any
# other finding on that net is judging the same incomplete model -- a shared
# net alone, without colocated evidence, is enough to join it there, unlike
# the general net-sharing rule (COH-5).
INCOMPLETE_RAIL_RULE_ID = "DC-001"

# A switch node's harmonics in the band (SW-001), its copper (SW-002) and its
# hot loop (SW-003) are one layout around one node: findings of these rules
# on the same net are one action. P02 Rev E's /SYS_5V_SW1 had two actions on
# 2026-10-01, SW-003 ranked second and SW-001 with SW-002 eighth (audit CO-7).
SWITCHING_STAGE_RULES = frozenset(("SW-001", "SW-002", "SW-003"))

# The thermal analysis solves at an internal air taken from the enclosure's
# heat balance (Q / (h x A)); the CFD resolves the same air. Read twice, they
# agree within this: a fifth of P02 Rev E's 16 K enclosure rise.
AIR_AGREEMENT_K = 3.0


@dataclass
class DomainScore:
    domain: str
    status: AnalysisStatus
    score: float
    finding_counts: Dict[str, int] = field(default_factory=dict)
    headline: str = ""
    # COH-10: board_fingerprint = SHA of the *saved* file while AC,
    # differential, EMC and thermal geometry reads the *open* board (DC-004
    # proves they can differ on the same report), and the consolidate button
    # aggregated session results with no per-domain timestamp or fingerprint
    # shown at all. Carried per domain, from the result it was computed on,
    # so a reader can see when two domains describe different moments.
    completed_at: str = ""
    board_fingerprint: str = ""

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
    # Free-text recommendations from findings that carry no structured
    # Remediation. Only DC-003 built one on P02 Rev E (analysis_adapters.py:
    # 567-569); every other action's advice -- including the differential
    # recommender's own geometry ("0.155 mm / 0.250 mm -> 90.6 ohm") -- lived
    # only in AnalysisFinding.recommendation, which the report never showed,
    # so 26/26 P02 actions read "No structured remediation was computed".
    recommendations: List[str] = field(default_factory=list)
    consequences: List[str] = field(default_factory=list)
    effort: RemediationEffort = RemediationEffort.MEDIUM
    gain_rank: float = 0.0
    # BOARD when any of its findings asks for a change on the board, DATA
    # when all of them ask for an input (rule_guide.FindingNature).
    nature: str = FindingNature.BOARD.value
    # What the ranking weighed: the lead findings' best confidence.
    confidence: str = EvidenceConfidence.HEURISTIC.value
    # Each contributing finding as the report's technical detail shows it:
    # domain, rule_id, title, description and confidence. "Consequences"
    # carried only "domain: title", the action's own title again on 13 of
    # 13 P02 Rev E actions (2026-10-01).
    details: List[Dict[str, str]] = field(default_factory=list)

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


FIX, REVIEW, PROTOTYPE, DATA = "fix", "review", "prototype", "data"


def action_bucket(action: CampaignAction) -> str:
    """Which decision list an action belongs to.

    A CRITICAL finding is always to fix. Otherwise an action asking only for
    data is data to complete; one whose every rule is a layout rule of thumb
    (rule_guide.PROTOTYPE_CHECK) is for the prototype to settle; a HIGH one
    is to fix; the rest is to review. On P02 Rev E's 2026-10-01 campaign this
    puts DC-008 (C88 and R10 cut off from GND) alone under "fix", and SW-003
    (a 20 mm hot-loop threshold) under "check on the prototype".

    The campaign's verdict reads these lists too (_compute_overall_status),
    so the report's banner and its lists cannot disagree.
    """
    if action.severity == FindingSeverity.CRITICAL:
        return FIX
    if action.nature == FindingNature.DATA.value:
        return DATA
    if action.rule_ids and all(rule in PROTOTYPE_CHECK for rule in action.rule_ids):
        return PROTOTYPE
    if action.severity == FindingSeverity.HIGH:
        return FIX
    return REVIEW


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
    """100 minus each rule's worst few findings, weighted by what they rest on.

    The cap of MAX_FINDINGS_PER_RULE_IN_SCORE applies per rule *and per
    distinct net*, not per rule alone: a rule firing once per pair/net --
    SI-DIFF-001 on five different differential pairs, on P02 Rev E -- is five
    distinct physical defects, and capping those at three under-counted two
    of them (score 76.6 instead of 61.0 for 5 failed pairs vs. 3). A rule
    naming only components, not nets, stays under the old per-rule cap: 159
    SCH-005 findings, one per silkscreen reference, are one systematic cause
    ("missing a power rating"), not 159, and a component-per-finding rule is
    the survey case GROUPING_MAX_COMPONENTS already treats as one cause
    rather than 159 problems tied together.

    A finding about the calculation (COH-9: CFD-003/004, DC-004/005 on P02
    Rev E; TH-004 and every other rule rule_guide marks CALCULATION) never
    enters the sum at all: "the tool cannot resolve this at the current
    mesh" is a fact about the run, and scoring it the same as a board defect
    made CFD's 82/100 say nothing about the board it graded.
    """
    by_object: Dict[tuple, List[float]] = {}
    for finding in findings:
        if finding_nature(finding) == FindingNature.CALCULATION:
            continue
        net_key = tuple(sorted(finding.nets))
        key = (finding.rule_id, net_key) if net_key else (finding.rule_id, None)
        by_object.setdefault(key, []).append(_finding_penalty(finding))
    penalty = sum(
        sum(sorted(penalties, reverse=True)[:MAX_FINDINGS_PER_RULE_IN_SCORE])
        for penalties in by_object.values()
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
    for members in by_net.values():
        stage = [index for index in members if tagged[index][1].rule_id in SWITCHING_STAGE_RULES]
        for other in stage[1:]:
            join(stage[0], other)
    for members in by_net.values():
        # An INCOMPLETE DC rail (DC-001) joins everything else on its net
        # unconditionally: DC-001 on /VBUS_SYS (U14 isolated, 81 load nodes
        # excluded, per docs/audit-2026-09-23.md COH-5) and an AC PASS or an
        # EMC "PDN remains within target" on the same net are not two
        # problems, they are one incomplete model judged two different ways.
        incomplete = [i for i in members if tagged[i][1].rule_id == INCOMPLETE_RAIL_RULE_ID]
        for left in incomplete:
            for right in members:
                if right != left and root(left) != root(right):
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


def _propagate_incomplete_rails(results: List[AnalysisResult]) -> None:
    """State an INCOMPLETE DC rail as a limitation everywhere else it is used.

    DC-001 marks a rail's model incomplete when a load the source cannot
    reach was excluded from the solve.  On P02 Rev E, /VBUS_SYS's DC-001 (U14
    isolated, 81 load nodes excluded) says nothing to AC, which passed the
    same net counting U14's current; to EMC, whose PD-002 says "PDN remains
    within target" on it; or to the consolidated report, though thermal's own
    report.txt separately marked the net INCOMPLETE.  Every other domain's
    result that names the net now carries the same limitation, so a reader of
    any one of them sees the DC model was incomplete for it.
    """
    excluded_by_net: Dict[str, List[str]] = {}
    for result in results:
        if result.analysis_type != "DC":
            continue
        for finding in result.findings:
            if finding.rule_id != INCOMPLETE_RAIL_RULE_ID:
                continue
            for net in finding.nets:
                loads = excluded_by_net.setdefault(net, [])
                for component in finding.components:
                    if component not in loads:
                        loads.append(component)
    if not excluded_by_net:
        return
    for result in results:
        if result.analysis_type == "DC":
            continue
        nets_here = {net for finding in result.findings for net in finding.nets}
        for net, loads in excluded_by_net.items():
            if net not in nets_here:
                continue
            loads_text = ", ".join(loads) if loads else "some loads"
            note = f"{net}: DC model incomplete ({loads_text} excluded from the solve)."
            if note not in result.limitations:
                result.limitations.append(note)


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
    # Each domain the user selected that produced no result: {"domain",
    # "state" (NOT_PREPARED, FAILED, SKIPPED, CANCELLED), "reason"}. Only
    # results reached the report, so a THERMAL that raised left no trace in
    # it and the verdict could read PASS.
    coverage: List[Dict[str, str]] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def recompute(self) -> "CampaignResult":
        """Recalculate domain scores, deduplicated actions, and the verdict."""
        _propagate_incomplete_rails(self.results)
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
                completed_at=result.completed_at,
                board_fingerprint=result.board_fingerprint,
            ))
        return scores

    def air_cross_check(self) -> Optional[tuple]:
        """``(thermal C, CFD C)``: the internal air of the thermal analysis's
        enclosure balance and the mean of the CFD's solved air, or None
        unless both exist. The CFD's "internal ambient" is the same
        Q / (h * A) as the thermal one, so setting those two side by side
        (P02 Rev E, 61.2 and 60.65 C) checked only that both put the same
        heat in the box."""
        found = {}
        for domain, key in (("THERMAL", "internal_air_c"), ("CFD", "solved_mean_air")):
            for result in self.results:
                if result.analysis_type != domain:
                    continue
                for metric in result.metrics:
                    if metric.key == key and isinstance(metric.value, (int, float)):
                        found[domain] = float(metric.value)
        if len(found) < 2:
            return None
        return found["THERMAL"], found["CFD"]

    def provenance_warnings(self) -> List[str]:
        """State it when this campaign's domains do not describe one moment.

        The consolidate button (ui/main_dialog.py) can aggregate whatever
        results this session happens to hold, run at different times or even
        against different board saves; a batch stamps every domain with the
        same board_fingerprint, so this is silent for one. Warns once per
        distinct fingerprint beyond the first, naming which domains carry it.
        """
        by_fingerprint: Dict[str, List[str]] = {}
        for score in self.domain_scores:
            if not score.board_fingerprint or score.status == AnalysisStatus.NO_DATA:
                continue
            by_fingerprint.setdefault(score.board_fingerprint, []).append(score.domain)
        if len(by_fingerprint) <= 1:
            return []
        return [
            f"{', '.join(domains)}: board fingerprint {fingerprint[:12]}…, "
            "different from at least one other domain in this report."
            for fingerprint, domains in sorted(by_fingerprint.items())
        ]

    @staticmethod
    def _headline(result: AnalysisResult) -> str:
        worst = _worst_severity([finding.severity for finding in result.findings])
        blocking = [f for f in result.findings if f.severity == worst]
        if not blocking or worst == FindingSeverity.INFO:
            return f"{len(result.findings)} finding(s), none above informational."
        return f"{len(blocking)} {worst.value} finding(s); worst: {blocking[0].title}"

    def _compute_actions(self) -> List[CampaignAction]:
        # (domain, finding) pairs across every result, then union-find style
        # grouping by physical identity. INFO findings are notes, not
        # actions ("PDN remains within target", "cavity resonances
        # estimated", FID "Nothing to do" -- COH-9, 8 of them on P02 Rev E):
        # they stay out of the action list entirely, though they are still
        # listed in their own domain's section (result.findings is untouched).
        # So do findings about the calculation: TH-004 ranked third of 13
        # actions on P02 Rev E's 2026-10-01 campaign, ahead of real defects,
        # though rerunning on a finer grid changes nothing on the board.
        tagged: List[tuple] = [
            (result.analysis_type, finding)
            for result in self.results
            for finding in result.findings
            if finding.severity != FindingSeverity.INFO
            and finding_nature(finding) != FindingNature.CALCULATION
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
            recommendations: List[str] = []
            details: List[Dict[str, str]] = []
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
                details.append({
                    "domain": domain,
                    "rule_id": finding.rule_id,
                    "title": finding.title,
                    "description": finding.description,
                    "confidence": finding.confidence.value,
                })
                remediations.extend(finding.remediations)
                text = (finding.recommendation or "").strip()
                if text and text not in recommendations:
                    recommendations.append(text)

            remediations = _dedupe_remediations(remediations)
            # Without a computed remediation, the rule's typical fix: all 13
            # actions of P02 Rev E's 2026-10-01 campaign read MEDIUM, the old
            # default, whether the fix was a via (DC-008) or a re-placement
            # of a power stage (SW-003).
            effort = (
                _worst_effort([item.effort for item in remediations])
                if remediations else _worst_effort([typical_effort(rule) for rule in rule_ids])
            )
            natures = {finding_nature(finding) for _d, finding in group}
            nature = (
                FindingNature.DATA if natures == {FindingNature.DATA} else FindingNature.BOARD
            )
            # Title comes from the most severe contributing finding.
            lead_findings = [finding for _d, finding in group if finding.severity == severity]
            lead = lead_findings[0]
            # Ranking tie-break: fold confidence into the score itself
            # (severity x confidence, one of the audit's own suggestions)
            # rather than leaving equal-severity/effort/domain-count actions
            # ordered by domain iteration order. On P02 Rev E six actions all
            # scored 25.0 (HIGH, no remediation, one domain) and their order
            # was purely which domain the campaign happened to run first.
            lead_confidence = max(
                (f.confidence for f in lead_findings),
                key=lambda item: CONFIDENCE_WEIGHT.get(
                    item, CONFIDENCE_WEIGHT[EvidenceConfidence.HEURISTIC]),
            )
            confidence_weight = CONFIDENCE_WEIGHT.get(
                lead_confidence, CONFIDENCE_WEIGHT[EvidenceConfidence.HEURISTIC])
            action = CampaignAction(
                action_id=f"action-{index:03d}",
                title=lead.title,
                severity=severity,
                domains=domains,
                rule_ids=rule_ids,
                nets=nets,
                components=components,
                remediations=remediations,
                recommendations=recommendations,
                consequences=consequences,
                effort=effort,
                nature=nature.value,
                confidence=EvidenceConfidence(lead_confidence).value,
                details=details,
            )
            action.gain_rank = (
                SEVERITY_WEIGHT[severity]
                * confidence_weight
                * (1.0 + 0.5 * (len(domains) - 1))
                / EFFORT_DIVISOR[effort]
            )
            actions.append(action)
        # Any residual tie (identical severity/confidence/domain-count/effort)
        # breaks on the title, so ordering no longer depends on which domain
        # the campaign happened to run first.
        actions.sort(key=lambda item: (-item.gain_rank, item.title))
        return actions

    def _compute_overall_status(self) -> AnalysisStatus:
        scored = [
            score for score in self.domain_scores
            if score.status != AnalysisStatus.NO_DATA
        ]
        if not scored:
            return AnalysisStatus.NO_DATA
        # An action under "fix before manufacture" fails the campaign: P02
        # Rev E's 2026-10-01 22:29 report read "WARN -- Review before
        # manufacture" above "Fix before manufacture (2)", one of them C88
        # and R10 cut off from GND, because the banner followed the domains'
        # certainty and the lists the severity.
        to_fix = any(action_bucket(action) == FIX for action in self.actions)
        if to_fix or any(score.status == AnalysisStatus.FAIL for score in scored):
            return AnalysisStatus.FAIL
        has_high = any(action.severity == FindingSeverity.HIGH for action in self.actions)
        if has_high or any(score.status == AnalysisStatus.WARN for score in scored):
            return AnalysisStatus.WARN
        # No defect among the analyses that ran is no PASS when one the user
        # selected did not complete: what it would have said is unknown.
        return AnalysisStatus.WARN if self.coverage else AnalysisStatus.PASS

    def top_actions(self, limit: int = 10) -> List[CampaignAction]:
        return sorted(self.actions, key=lambda item: (-item.gain_rank, item.title))[:limit]

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
            "coverage": [dict(item) for item in self.coverage],
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
            coverage=[dict(item) for item in payload.get("coverage", [])],
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
        coverage: Optional[List[Dict[str, str]]] = None,
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
            coverage=[dict(item) for item in coverage or ()],
        ).recompute()
