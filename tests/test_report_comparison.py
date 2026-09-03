"""Tests for before/after campaign comparison (report/comparison.py)."""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from analysis_contract import (  # noqa: E402
    AnalysisFinding,
    AnalysisResult,
    AnalysisStatus,
    FindingSeverity,
)
from campaign import CampaignResult  # noqa: E402
from report.comparison import compare_campaigns  # noqa: E402


def campaign(rule_net_pairs, fingerprint="fp-1"):
    findings = [
        AnalysisFinding(
            rule_id=rule, category="DC", severity=FindingSeverity.HIGH,
            title=f"Problem on {net}", description="d", nets=[net],
        )
        for rule, net in rule_net_pairs
    ]
    return CampaignResult.from_results(
        [AnalysisResult(
            analysis_type="DC", title="DC", status=AnalysisStatus.WARN,
            findings=findings,
        )],
        board_fingerprint=fingerprint,
    )


class ComparisonTests(unittest.TestCase):
    def test_resolved_introduced_and_persisting_are_classified(self):
        baseline = campaign([("DC-1", "VCC"), ("DC-2", "GND")])
        current = campaign([("DC-2", "GND"), ("DC-3", "VDD")])
        delta = compare_campaigns(baseline, current)

        self.assertEqual([a.nets for a in delta.resolved], [["VCC"]])
        self.assertEqual([a.nets for a in delta.introduced], [["VDD"]])
        self.assertEqual([a.nets for a in delta.persisting], [["GND"]])
        self.assertEqual(delta.net_improvement, 0)

    def test_identity_matching_survives_regenerated_action_ids(self):
        # action_id is positional and regenerated per campaign; matching must
        # not depend on it or every action reads as both gone and new.
        baseline = campaign([("DC-1", "VCC"), ("DC-2", "GND")])
        current = campaign([("DC-2", "GND"), ("DC-1", "VCC")])
        delta = compare_campaigns(baseline, current)
        self.assertEqual(len(delta.persisting), 2)
        self.assertEqual(delta.resolved, [])
        self.assertEqual(delta.introduced, [])

    def test_board_changed_flag_and_score_deltas(self):
        baseline = campaign([("DC-1", "VCC")], fingerprint="fp-old")
        current = campaign([], fingerprint="fp-new")
        delta = compare_campaigns(baseline, current)
        self.assertTrue(delta.board_changed)
        # Baseline lost 10 points to one HIGH finding; current is clean.
        self.assertAlmostEqual(delta.score_deltas["DC"], 10.0)
        self.assertEqual(delta.net_improvement, 1)

    def test_same_fingerprint_is_not_reported_as_board_change(self):
        delta = compare_campaigns(campaign([]), campaign([]))
        self.assertFalse(delta.board_changed)


if __name__ == "__main__":
    unittest.main()
