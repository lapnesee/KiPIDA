"""Before/after campaign comparison, finding by finding.

Two campaigns are compared on what each finding names: one subject per
component, else per net, else the finding's title, under its domain and
rule. Actions were the unit before, and an action's identity is its whole
group: P02 Rev E's 21:52 and 22:38 campaigns read "resolved
SW-004,TH-008 / introduced TH-008" when only SW-004 had gone, and dropping
one capacitor from TE-002's thirteen would have changed all of it.

Only the domains both campaigns ran are compared. A DC-only campaign
against a full one read "10 resolved" and -100 per missing domain.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from analysis_contract import FindingSeverity
from campaign import CampaignResult
from rule_guide import FindingNature, finding_nature

# EMC rules renamed because their identifiers collided with other domains':
# decoupling was DC-001/DC-002 like the DC analysis's island and convergence
# findings, and the hot-component rule was TH-001 like the junction limit. A
# campaign saved before the rename still carries the old identifiers.
_LEGACY_EMC_RULE_IDS = {"DC-001": "DE-001", "DC-002": "DE-002", "TH-001": "TE-001"}


@dataclass(frozen=True)
class FindingSubject:
    domain: str
    rule_id: str
    subject: str
    severity: str
    title: str

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.domain, self.rule_id, self.subject)


@dataclass
class CampaignDelta:
    resolved: List[FindingSubject] = field(default_factory=list)
    introduced: List[FindingSubject] = field(default_factory=list)
    persisting: List[FindingSubject] = field(default_factory=list)
    # (before, after) for a subject still reported at another severity.
    changed: List[Tuple[FindingSubject, FindingSubject]] = field(default_factory=list)
    score_deltas: Dict[str, float] = field(default_factory=dict)
    # Domains only one of the two campaigns ran, never counted as resolved.
    not_compared: List[str] = field(default_factory=list)
    board_changed: bool = False
    tool_changed: bool = False

    @property
    def net_improvement(self) -> int:
        """Resolved minus introduced -- positive means the board improved."""
        return len(self.resolved) - len(self.introduced)


def _compared(finding) -> bool:
    """A board finding above a note, or an input still to complete; not a
    limit of the calculation."""
    nature = finding_nature(finding)
    if nature == FindingNature.CALCULATION:
        return False
    return finding.severity != FindingSeverity.INFO or nature == FindingNature.DATA


def subjects(campaign: CampaignResult, domains=None) -> List[FindingSubject]:
    found = []
    for result in campaign.results:
        domain = result.analysis_type
        if domains is not None and domain not in domains:
            continue
        for finding in result.findings:
            if not _compared(finding):
                continue
            rule_id = (_LEGACY_EMC_RULE_IDS.get(finding.rule_id, finding.rule_id)
                       if domain == "EMC" else finding.rule_id)
            names = list(finding.components) or list(finding.nets) or [finding.title]
            severity = getattr(finding.severity, "value", str(finding.severity))
            found.extend(FindingSubject(domain, rule_id, str(name), severity, finding.title)
                         for name in names)
    return found


def compare_campaigns(baseline: CampaignResult, current: CampaignResult) -> CampaignDelta:
    """Diff two campaigns on the domains both ran.

    A differing board fingerprint is the normal case -- comparing a board
    against its corrected revision is the point -- and a differing build of
    Ki-PIDA is said too, since P02 Rev E's EMC score moved from 65 to 35 and
    back at one board fingerprint when only the tool changed. Subjects are
    matched by count, so five pairs sharing a rule and fixing four of them
    reads four resolved, one persisting.
    """
    before_domains = {result.analysis_type for result in baseline.results}
    after_domains = {result.analysis_type for result in current.results}
    common = before_domains & after_domains
    before = subjects(baseline, common)
    after = subjects(current, common)
    before_by_key: Dict[tuple, List[FindingSubject]] = {}
    for item in before:
        before_by_key.setdefault(item.key, []).append(item)
    after_by_key: Dict[tuple, List[FindingSubject]] = {}
    for item in after:
        after_by_key.setdefault(item.key, []).append(item)

    delta = CampaignDelta()
    for key in sorted(set(before_by_key) | set(after_by_key)):
        old, new = before_by_key.get(key, []), after_by_key.get(key, [])
        shared = min(len(old), len(new))
        for left, right in zip(old[:shared], new[:shared]):
            if left.severity != right.severity:
                delta.changed.append((left, right))
            else:
                delta.persisting.append(right)
        delta.resolved.extend(old[shared:])
        delta.introduced.extend(new[shared:])

    baseline_scores = {score.domain: score.score for score in baseline.domain_scores}
    current_scores = {score.domain: score.score for score in current.domain_scores}
    delta.score_deltas = {
        domain: current_scores[domain] - baseline_scores[domain]
        for domain in sorted(common) if domain in baseline_scores and domain in current_scores
    }
    delta.not_compared = sorted(before_domains ^ after_domains)
    delta.board_changed = baseline.board_fingerprint != current.board_fingerprint
    delta.tool_changed = (baseline.tool_version or "") != (current.tool_version or "")
    return delta


def grouped(items: List[FindingSubject]) -> List[Tuple[str, str, str, str, List[str]]]:
    """(severity, title, domain, rule_id, subjects) per finding, worst first."""
    order = {severity.value: index for index, severity in enumerate(FindingSeverity)}
    groups: Dict[tuple, List[str]] = {}
    for item in items:
        groups.setdefault((item.severity, item.title, item.domain, item.rule_id), []).append(item.subject)
    return sorted(
        ((severity, title, domain, rule, names) for (severity, title, domain, rule), names
         in groups.items()),
        key=lambda group: (order.get(group[0], 9), group[2], group[3], group[1]),
    )


__all__ = ["CampaignDelta", "FindingSubject", "compare_campaigns", "grouped", "subjects"]
