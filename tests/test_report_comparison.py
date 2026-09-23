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


def campaign(rule_net_pairs, fingerprint="fp-1", domain="DC"):
    findings = [
        AnalysisFinding(
            rule_id=rule, category=domain, severity=FindingSeverity.HIGH,
            title=f"Problem on {net}", description="d", nets=[net],
        )
        for rule, net in rule_net_pairs
    ]
    return CampaignResult.from_results(
        [AnalysisResult(
            analysis_type=domain, title=domain, status=AnalysisStatus.WARN,
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

    def test_identity_collisions_are_matched_by_count_not_overwritten(self):
        # COH-7 (docs/audit-2026-09-23.md scratchpad/audit/coherence/agg.py):
        # SI-DIFF-001 findings carry no net/component before the SI adapter
        # adds the pair's P/N nets, so all 5 differential-pair findings share
        # one identity. A dict keyed on identity kept only the last action
        # written on each side and reported "resolved 0, persisting 6" for
        # 10 actions across 6 distinct identities; fixing 4 of 5 pairs must
        # report resolved 4.
        def diff_campaign(pair_titles):
            findings = [
                AnalysisFinding(
                    rule_id="SI-DIFF-001", category="DIFFERENTIAL",
                    severity=FindingSeverity.HIGH, title=f"{title}: Fail High",
                    description="d",
                )
                for title in pair_titles
            ]
            return CampaignResult.from_results([AnalysisResult(
                analysis_type="DIFFERENTIAL", title="D", status=AnalysisStatus.FAIL,
                findings=findings,
            )], board_fingerprint="fp-1")

        baseline = diff_campaign([
            "/USB_DEVICE_D", "/USB_J2_MUX_D", "/USB_HOST_D", "/USB_J1_D", "/USB_J2_D",
        ])
        current = diff_campaign(["/USB_DEVICE_D"])
        delta = compare_campaigns(baseline, current)
        self.assertEqual(len(delta.resolved), 4)
        self.assertEqual(len(delta.persisting), 1)
        self.assertEqual(len(delta.introduced), 0)

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
        # Baseline lost 3 points to one HIGH finding of heuristic confidence
        # (12 x 0.25); current is clean.
        self.assertAlmostEqual(delta.score_deltas["DC"], 3.0)
        self.assertEqual(delta.net_improvement, 1)

    def test_pre_rename_emc_rule_ids_still_match(self):
        # EMC decoupling was DC-001 before it collided with the DC analysis's
        # island finding; a campaign saved then must still pair with one run now.
        baseline = campaign([("DC-001", "+3V3")], domain="EMC")
        current = campaign([("DE-001", "+3V3")], domain="EMC")
        delta = compare_campaigns(baseline, current)
        self.assertEqual(len(delta.persisting), 1)
        self.assertEqual(delta.resolved, [])
        self.assertEqual(delta.introduced, [])

    def test_dc_domain_rule_ids_are_not_translated(self):
        baseline = campaign([("DC-001", "+3V3")], domain="DC")
        current = campaign([("DE-001", "+3V3")], domain="DC")
        delta = compare_campaigns(baseline, current)
        self.assertEqual(len(delta.resolved), 1)
        self.assertEqual(len(delta.introduced), 1)

    def test_same_fingerprint_is_not_reported_as_board_change(self):
        delta = compare_campaigns(campaign([]), campaign([]))
        self.assertFalse(delta.board_changed)


if __name__ == "__main__":
    unittest.main()
