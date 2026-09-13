"""Campaign actions group physical problems, and scores weigh evidence.

The audit of 2026-09-13 reproduced three unrelated findings -- a PDN impedance
miss on +3V3, a fragmented ground plane, a DC solve that did not converge on
+3V3 -- merging into one action ranked first, because two of them named GND
and two named +3V3. It also found grouping greedy (dependent on domain order,
not transitive) and scores blind to confidence, so 159 heuristic findings of
one rule would sink a domain to zero.
"""

import itertools
import unittest

from analysis_contract import (
    AnalysisEvidence, AnalysisFinding, AnalysisResult, AnalysisStatus,
    EvidenceConfidence, FindingSeverity,
)
from campaign import CampaignResult


def finding(rule_id, nets=(), components=(), evidence=(), severity=FindingSeverity.HIGH,
            confidence=EvidenceConfidence.ESTIMATED):
    return AnalysisFinding(
        rule_id=rule_id, category="TEST", severity=severity, title=rule_id,
        description="d", confidence=confidence, nets=list(nets),
        components=list(components), evidence=list(evidence),
    )


def result(domain, findings, summary=None):
    return AnalysisResult(
        analysis_type=domain, title=domain, status=AnalysisStatus.WARN,
        findings=list(findings), summary=dict(summary or {}),
    )


def groups(campaign):
    return sorted(sorted(action.rule_ids) for action in campaign.actions)


class CampaignGroupingTests(unittest.TestCase):
    def test_a_shared_ground_or_rail_does_not_merge_unrelated_findings(self):
        campaign = CampaignResult.from_results([
            result("AC", [finding("AC-001", ["+3V3", "GND"], ["C12"])]),
            result("EMC", [finding("GP-003", ["GND"])]),
            result("DC", [finding("DC-002", ["+3V3"])]),
        ])
        self.assertEqual(groups(campaign), [["AC-001"], ["DC-002"], ["GP-003"]])

    def test_a_shared_net_at_the_same_spot_merges(self):
        here = [AnalysisEvidence("PCB", "d", x_mm=10.0, y_mm=10.0, layer="F.Cu")]
        near = [AnalysisEvidence("PCB", "d", x_mm=11.0, y_mm=10.5, layer="F.Cu")]
        campaign = CampaignResult.from_results([
            result("EMC", [finding("GP-001", ["USB_D+", "GND"], evidence=here)]),
            result("DIFFERENTIAL", [finding("SI-DIFF-001", ["USB_D+"], evidence=near)]),
        ])
        self.assertEqual(groups(campaign), [["GP-001", "SI-DIFF-001"]])

    def test_grouping_is_transitive_and_independent_of_order(self):
        entries = [
            ("THERMAL", finding("TH-001", components=["U7"])),
            ("EMC", finding("DE-002", components=["U7", "C5"])),
            ("AC", finding("AC-001", components=["C5"])),
            ("DC", finding("DC-003", ["+5V"])),
        ]
        expected = [["AC-001", "DE-002", "TH-001"], ["DC-003"]]
        for order in itertools.permutations(entries):
            campaign = CampaignResult.from_results([result(domain, [item]) for domain, item in order])
            self.assertEqual(groups(campaign), expected)

    def test_a_rule_firing_many_times_costs_its_worst_three(self):
        many = [
            finding("SCH-005", components=[f"LOGO{i}"], severity=FindingSeverity.MEDIUM,
                    confidence=EvidenceConfidence.HEURISTIC)
            for i in range(159)
        ]
        campaign = CampaignResult.from_results([result("SCHEMATIC", many)])
        # 3 findings x MEDIUM 6 x HEURISTIC 0.25
        self.assertAlmostEqual(campaign.domain_scores[0].score, 100.0 - 4.5)

    def test_a_fact_costs_more_than_a_heuristic(self):
        fact = CampaignResult.from_results([result("DC", [
            finding("DC-002", confidence=EvidenceConfidence.DETERMINISTIC),
        ])])
        guess = CampaignResult.from_results([result("DC", [
            finding("DC-002", confidence=EvidenceConfidence.HEURISTIC),
        ])])
        self.assertAlmostEqual(fact.domain_scores[0].score, 88.0)
        self.assertAlmostEqual(guess.domain_scores[0].score, 97.0)

    def test_a_domain_that_grades_itself_keeps_its_score(self):
        campaign = CampaignResult.from_results([result(
            "EMC", [finding("GP-003", ["GND"])], summary={"domain_score": 76},
        )])
        self.assertAlmostEqual(campaign.domain_scores[0].score, 76.0)


if __name__ == "__main__":
    unittest.main()
