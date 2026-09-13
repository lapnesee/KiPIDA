"""Before/after campaign comparison.

Actions are matched across campaigns by their physical identity -- which
rules fired on which nets and components -- not by ``action_id``, which is
regenerated on every aggregation and would report every action as both
resolved and reintroduced.
"""

from dataclasses import dataclass, field
from typing import Dict, List

from campaign import CampaignAction, CampaignResult

# EMC rules renamed because their identifiers collided with other domains':
# decoupling was DC-001/DC-002 like the DC analysis's island and convergence
# findings, and the hot-component rule was TH-001 like the junction limit. A
# campaign saved before the rename still carries the old identifiers, and
# comparing it with a newer one would report each such EMC action as resolved
# and an identical one as introduced.
_LEGACY_EMC_RULE_IDS = {"DC-001": "DE-001", "DC-002": "DE-002", "TH-001": "TE-001"}


def _identity(action: CampaignAction) -> str:
    """The action's identity, with pre-rename EMC identifiers translated.

    Only an action that EMC reported without the DC or thermal domain is
    translated: when those domains are in the same action, an old ``DC-001``
    could be either rule, and guessing would be worse than a mismatch.
    """
    domains = set(action.domains)
    if "EMC" not in domains or domains & {"DC", "THERMAL"}:
        return action.identity
    rule_ids = [_LEGACY_EMC_RULE_IDS.get(rule_id, rule_id) for rule_id in action.rule_ids]
    return "|".join((
        ",".join(sorted(rule_ids)),
        ",".join(sorted(action.nets)),
        ",".join(sorted(action.components)),
    ))


@dataclass
class CampaignDelta:
    resolved: List[CampaignAction] = field(default_factory=list)
    introduced: List[CampaignAction] = field(default_factory=list)
    persisting: List[CampaignAction] = field(default_factory=list)
    score_deltas: Dict[str, float] = field(default_factory=dict)
    board_changed: bool = False

    @property
    def net_improvement(self) -> int:
        """Resolved minus introduced -- positive means the board improved."""
        return len(self.resolved) - len(self.introduced)


def compare_campaigns(baseline: CampaignResult, current: CampaignResult) -> CampaignDelta:
    """Diff two campaigns.

    A differing ``board_fingerprint`` is the normal case -- comparing a
    board against its corrected revision is the point -- but it is recorded
    in ``board_changed`` so a caller never silently compares two unrelated
    boards while believing it is looking at one board over time.
    """
    baseline_by_identity = {_identity(action): action for action in baseline.actions}
    current_by_identity = {_identity(action): action for action in current.actions}

    resolved = [
        action for identity, action in baseline_by_identity.items()
        if identity not in current_by_identity
    ]
    introduced = [
        action for identity, action in current_by_identity.items()
        if identity not in baseline_by_identity
    ]
    persisting = [
        action for identity, action in current_by_identity.items()
        if identity in baseline_by_identity
    ]

    baseline_scores = {score.domain: score.score for score in baseline.domain_scores}
    current_scores = {score.domain: score.score for score in current.domain_scores}
    score_deltas = {
        domain: current_scores.get(domain, 0.0) - baseline_scores.get(domain, 0.0)
        for domain in sorted(set(baseline_scores) | set(current_scores))
    }

    return CampaignDelta(
        resolved=sorted(resolved, key=lambda a: a.gain_rank, reverse=True),
        introduced=sorted(introduced, key=lambda a: a.gain_rank, reverse=True),
        persisting=sorted(persisting, key=lambda a: a.gain_rank, reverse=True),
        score_deltas=score_deltas,
        board_changed=baseline.board_fingerprint != current.board_fingerprint,
    )
