"""Before/after campaign comparison.

Actions are matched across campaigns by their physical identity -- which
rules fired on which nets and components -- not by ``action_id``, which is
regenerated on every aggregation and would report every action as both
resolved and reintroduced.
"""

from dataclasses import dataclass, field
from typing import Dict, List

from campaign import CampaignAction, CampaignResult


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
    baseline_by_identity = {action.identity: action for action in baseline.actions}
    current_by_identity = {action.identity: action for action in current.actions}

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
