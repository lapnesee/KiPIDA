"""Tests for cross-domain campaign aggregation (campaign.py).

Scope: the aggregation rules that carry the value -- scoring, verdict,
deduplication, gain ranking -- plus one serialization round trip.
"""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from analysis_contract import (  # noqa: E402
    AnalysisEvidence,
    AnalysisFinding,
    AnalysisResult,
    AnalysisStatus,
    EvidenceConfidence,
    FindingSeverity,
    Remediation,
    RemediationEffort,
)
from campaign import CampaignResult  # noqa: E402


def finding(
    rule_id="R-1", severity=FindingSeverity.MEDIUM, title="Problem",
    nets=None, components=None, evidence=None, remediations=None,
):
    return AnalysisFinding(
        rule_id=rule_id, category="TEST", severity=severity,
        title=title, description="d",
        confidence=EvidenceConfidence.DETERMINISTIC,
        nets=list(nets or []), components=list(components or []),
        evidence=list(evidence or []), remediations=list(remediations or []),
    )


def result(domain="DC", status=AnalysisStatus.WARN, findings=None):
    return AnalysisResult(
        analysis_type=domain, title=f"{domain} analysis", status=status,
        findings=list(findings or []),
    )


class DomainScoreTests(unittest.TestCase):
    def test_penalty_formula(self):
        # 1 CRITICAL (25) + 2 HIGH (20) + 1 LOW (1) = 46 -> score 54
        campaign = CampaignResult.from_results([result("DC", findings=[
            finding("A", FindingSeverity.CRITICAL, nets=["N1"]),
            finding("B", FindingSeverity.HIGH, nets=["N2"]),
            finding("C", FindingSeverity.HIGH, nets=["N3"]),
            finding("D", FindingSeverity.LOW, nets=["N4"]),
        ])])
        self.assertAlmostEqual(campaign.domain_scores[0].score, 54.0)

    def test_info_findings_do_not_reduce_score(self):
        campaign = CampaignResult.from_results([result("DC", AnalysisStatus.PASS, [
            finding("A", FindingSeverity.INFO, nets=["N1"]),
        ])])
        self.assertAlmostEqual(campaign.domain_scores[0].score, 100.0)

    def test_no_data_domain_is_not_scored_as_clean(self):
        campaign = CampaignResult.from_results([result("CFD", AnalysisStatus.NO_DATA)])
        score = campaign.domain_scores[0]
        self.assertEqual(score.status, AnalysisStatus.NO_DATA)
        self.assertEqual(score.score, 0.0)
        # And it must not make the campaign look failed either.
        self.assertEqual(campaign.overall_status, AnalysisStatus.NO_DATA)


class OverallStatusTests(unittest.TestCase):
    def test_critical_finding_forces_fail(self):
        campaign = CampaignResult.from_results([result("DC", AnalysisStatus.WARN, [
            finding("A", FindingSeverity.CRITICAL, nets=["N1"]),
        ])])
        self.assertEqual(campaign.overall_status, AnalysisStatus.FAIL)

    def test_high_finding_gives_warn(self):
        campaign = CampaignResult.from_results([result("DC", AnalysisStatus.PASS, [
            finding("A", FindingSeverity.HIGH, nets=["N1"]),
        ])])
        self.assertEqual(campaign.overall_status, AnalysisStatus.WARN)

    def test_clean_domains_give_pass(self):
        campaign = CampaignResult.from_results([result("DC", AnalysisStatus.PASS, [
            finding("A", FindingSeverity.INFO, nets=["N1"]),
        ])])
        self.assertEqual(campaign.overall_status, AnalysisStatus.PASS)

    def test_no_results_is_no_data(self):
        self.assertEqual(
            CampaignResult.from_results([]).overall_status, AnalysisStatus.NO_DATA,
        )


class DeduplicationTests(unittest.TestCase):
    def test_shared_net_merges_across_domains(self):
        campaign = CampaignResult.from_results([
            result("DC", findings=[finding("DC-1", title="Drop on GND", nets=["GND"])]),
            result("EMC", findings=[finding("GP-3", title="Return path", nets=["GND"])]),
        ])
        self.assertEqual(len(campaign.actions), 1)
        self.assertEqual(sorted(campaign.actions[0].domains), ["DC", "EMC"])
        self.assertEqual(len(campaign.actions[0].consequences), 2)

    def test_different_nets_stay_separate(self):
        campaign = CampaignResult.from_results([
            result("DC", findings=[finding("DC-1", nets=["GND"])]),
            result("EMC", findings=[finding("GP-3", nets=["VCC"])]),
        ])
        self.assertEqual(len(campaign.actions), 2)

    def test_nearby_evidence_merges(self):
        near = [AnalysisEvidence("PCB", "d", x_mm=10.0, y_mm=10.0, layer="F.Cu")]
        also_near = [AnalysisEvidence("PCB", "d", x_mm=11.0, y_mm=10.0, layer="F.Cu")]
        campaign = CampaignResult.from_results([
            result("DC", findings=[finding("DC-1", evidence=near)]),
            result("EMC", findings=[finding("GP-3", evidence=also_near)]),
        ])
        self.assertEqual(len(campaign.actions), 1)

    def test_distant_evidence_does_not_merge(self):
        here = [AnalysisEvidence("PCB", "d", x_mm=10.0, y_mm=10.0, layer="F.Cu")]
        far = [AnalysisEvidence("PCB", "d", x_mm=30.0, y_mm=10.0, layer="F.Cu")]
        campaign = CampaignResult.from_results([
            result("DC", findings=[finding("DC-1", evidence=here)]),
            result("EMC", findings=[finding("GP-3", evidence=far)]),
        ])
        self.assertEqual(len(campaign.actions), 2)

    def test_same_coordinates_on_different_layers_do_not_merge(self):
        top = [AnalysisEvidence("PCB", "d", x_mm=10.0, y_mm=10.0, layer="F.Cu")]
        bottom = [AnalysisEvidence("PCB", "d", x_mm=10.0, y_mm=10.0, layer="B.Cu")]
        campaign = CampaignResult.from_results([
            result("DC", findings=[finding("DC-1", evidence=top)]),
            result("EMC", findings=[finding("GP-3", evidence=bottom)]),
        ])
        self.assertEqual(len(campaign.actions), 2)


class GainRankTests(unittest.TestCase):
    def test_multi_domain_outranks_single_domain_of_same_severity(self):
        campaign = CampaignResult.from_results([
            result("DC", findings=[finding("DC-1", FindingSeverity.HIGH, nets=["GND"])]),
            result("EMC", findings=[finding("GP-3", FindingSeverity.HIGH, nets=["GND"])]),
            result("THERMAL", findings=[finding("TH-1", FindingSeverity.HIGH, nets=["VCC"])]),
        ])
        ranked = campaign.top_actions()
        self.assertEqual(ranked[0].nets, ["GND"])
        self.assertGreater(ranked[0].gain_rank, ranked[1].gain_rank)

    def test_low_effort_outranks_high_effort_at_equal_severity(self):
        cheap = Remediation(action="WIDEN_TRACK", target="T1", effort=RemediationEffort.LOW)
        costly = Remediation(action="RESPIN_STACKUP", target="T2", effort=RemediationEffort.HIGH)
        campaign = CampaignResult.from_results([result("DC", findings=[
            finding("A", FindingSeverity.HIGH, nets=["N1"], remediations=[cheap]),
            finding("B", FindingSeverity.HIGH, nets=["N2"], remediations=[costly]),
        ])])
        ranked = campaign.top_actions()
        self.assertEqual(ranked[0].effort, RemediationEffort.LOW)
        self.assertEqual(ranked[1].effort, RemediationEffort.HIGH)

    def test_action_effort_is_the_most_constraining_remediation(self):
        campaign = CampaignResult.from_results([result("DC", findings=[
            finding("A", nets=["N1"], remediations=[
                Remediation(action="X", target="a", effort=RemediationEffort.LOW),
                Remediation(action="Y", target="b", effort=RemediationEffort.HIGH),
            ]),
        ])])
        self.assertEqual(campaign.actions[0].effort, RemediationEffort.HIGH)


class SerializationTests(unittest.TestCase):
    def test_round_trip_preserves_aggregation(self):
        campaign = CampaignResult.from_results(
            [
                result("DC", findings=[finding(
                    "DC-1", FindingSeverity.CRITICAL, nets=["GND"],
                    remediations=[Remediation(
                        action="WIDEN_TRACK", target="seg-1",
                        current_value=0.25, proposed_value=0.6, unit="mm",
                        predicted_gain="drop 3.1% -> 1.8%", verified=True,
                    )],
                )]),
                result("EMC", findings=[finding("GP-3", nets=["GND"])]),
            ],
            project_name="demo", board_fingerprint="abc123",
        )
        restored = CampaignResult.from_json(campaign.to_json())
        self.assertEqual(restored.project_name, "demo")
        self.assertEqual(restored.board_fingerprint, "abc123")
        self.assertEqual(restored.overall_status, AnalysisStatus.FAIL)
        self.assertEqual(len(restored.actions), 1)
        self.assertEqual(sorted(restored.actions[0].domains), ["DC", "EMC"])
        remediation = restored.actions[0].remediations[0]
        self.assertTrue(remediation.verified)
        self.assertEqual(remediation.proposed_value, 0.6)


if __name__ == "__main__":
    unittest.main()
