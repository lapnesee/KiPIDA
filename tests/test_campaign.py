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
    confidence=EvidenceConfidence.DETERMINISTIC,
):
    return AnalysisFinding(
        rule_id=rule_id, category="TEST", severity=severity,
        title=title, description="d",
        confidence=confidence,
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
        # Deterministic findings cost their full weight:
        # 1 CRITICAL (25) + 2 HIGH (24) + 1 LOW (2) = 51 -> score 49
        campaign = CampaignResult.from_results([result("DC", findings=[
            finding("A", FindingSeverity.CRITICAL, nets=["N1"]),
            finding("B", FindingSeverity.HIGH, nets=["N2"]),
            finding("C", FindingSeverity.HIGH, nets=["N3"]),
            finding("D", FindingSeverity.LOW, nets=["N4"]),
        ])])
        self.assertAlmostEqual(campaign.domain_scores[0].score, 49.0)

    def test_info_findings_do_not_reduce_score(self):
        campaign = CampaignResult.from_results([result("DC", AnalysisStatus.PASS, [
            finding("A", FindingSeverity.INFO, nets=["N1"]),
        ])])
        self.assertAlmostEqual(campaign.domain_scores[0].score, 100.0)

    def test_per_rule_cap_exempts_findings_on_distinct_objects(self):
        # COH-7: 5 failed differential pairs (5 distinct nets, one
        # SI-DIFF-001 rule) are 5 physical defects, not one systematic cause
        # capped at 3. docs/audit-2026-09-23.md cites 76.6 (old, capped) vs.
        # 61.0 (right) for 5 HIGH ESTIMATED findings on one rule; those are
        # exactly SEVERITY_PENALTY[HIGH] (12.0) * CONFIDENCE_WEIGHT[ESTIMATED]
        # (0.65) = 7.8 per finding, capped at 3 (100 - 23.4 = 76.6) vs. all 5
        # (100 - 39.0 = 61.0).
        pairs = CampaignResult.from_results([result("DIFFERENTIAL", findings=[
            finding(
                "SI-DIFF-001", FindingSeverity.HIGH, nets=[f"/PAIR_{i}"],
                confidence=EvidenceConfidence.ESTIMATED,
            )
            for i in range(5)
        ])])
        self.assertAlmostEqual(pairs.domain_scores[0].score, 61.0)

        # Contrast: 5 findings on the same rule with no object to distinguish
        # them (a systemic complaint) still share the old per-rule cap of 3.
        systemic = CampaignResult.from_results([result("DIFFERENTIAL", findings=[
            AnalysisFinding(
                rule_id="SI-DIFF-001", category="DIFFERENTIAL",
                severity=FindingSeverity.HIGH, title=f"instance {i}",
                description="d", confidence=EvidenceConfidence.ESTIMATED,
            )
            for i in range(5)
        ])])
        self.assertAlmostEqual(systemic.domain_scores[0].score, 76.6)

    def test_calculation_quality_finding_does_not_reduce_score(self):
        # COH-9, P02 Rev E: CFD-003/004 and DC-004/005 are about the
        # calculation itself ("the tool cannot resolve this"), not the
        # board, but entered the score like any defect (CFD's 82/100 said
        # nothing about the board it graded).
        campaign = CampaignResult.from_results([result("CFD", findings=[
            AnalysisFinding(
                rule_id="CFD-004", category="NUMERICS", severity=FindingSeverity.HIGH,
                title="Enclosure reduced to one cell", description="d",
                calculation_quality=True,
            ),
        ])])
        self.assertAlmostEqual(campaign.domain_scores[0].score, 100.0)

    def test_info_findings_are_out_of_the_action_list(self):
        # COH-9: 8 INFO "actions" on P02 Rev E are notes, not actions
        # ("PDN remains within target", "cavity resonances estimated", FID
        # "Nothing to do"). They must still show in the domain's own
        # section (result.findings), just not in campaign.actions.
        campaign = CampaignResult.from_results([result("EMC", findings=[
            finding("PD-002", FindingSeverity.INFO, nets=["/VBUS_SYS"], title="PDN within target"),
        ])])
        self.assertEqual(campaign.actions, [])
        self.assertEqual(len(campaign.results[0].findings), 1)

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

    def test_high_estimated_finding_gives_warn(self):
        # P02 Rev E, 2026-09-22 21:58: DC-001 is HIGH ESTIMATED (U14 isolated,
        # 8.04 mV) and only ever produced a WARN. COH-2's central rule keeps
        # that: a HIGH resting on an estimate, not a measured/deterministic
        # fact, warrants a WARN, the same for every domain.
        campaign = CampaignResult.from_results([result("DC", AnalysisStatus.PASS, [
            finding("A", FindingSeverity.HIGH, nets=["N1"], confidence=EvidenceConfidence.ESTIMATED),
        ])])
        self.assertEqual(campaign.overall_status, AnalysisStatus.WARN)

    def test_high_deterministic_finding_forces_fail(self):
        # COH-2: verdict FAIL used to depend on which adapter reported it (the
        # differential adapter FAILed on any failed pair, DC/EMC/CFD/thermal
        # only ever WARNed on a HIGH). The central rule now FAILs any domain
        # on a HIGH finding backed by a fact rather than an estimate, the
        # `finding()` helper's default confidence -- regardless of domain.
        campaign = CampaignResult.from_results([result("DC", AnalysisStatus.PASS, [
            finding("A", FindingSeverity.HIGH, nets=["N1"]),
        ])])
        self.assertEqual(campaign.overall_status, AnalysisStatus.FAIL)

    def test_clean_domains_give_pass(self):
        campaign = CampaignResult.from_results([result("DC", AnalysisStatus.PASS, [
            finding("A", FindingSeverity.INFO, nets=["N1"]),
        ])])
        self.assertEqual(campaign.overall_status, AnalysisStatus.PASS)

    def test_no_results_is_no_data(self):
        self.assertEqual(
            CampaignResult.from_results([]).overall_status, AnalysisStatus.NO_DATA,
        )


class IncompleteRailPropagationTests(unittest.TestCase):
    def test_incomplete_dc_rail_becomes_a_limitation_on_other_domains(self):
        # COH-5, P02 Rev E: DC-001 marks /VBUS_SYS incomplete (U14 isolated,
        # 81 load nodes excluded), while AC passes the same net counting
        # U14's current and EMC's PD-002 says "PDN remains within target" on
        # it -- neither said the model was incomplete.
        dc = result("DC", findings=[AnalysisFinding(
            rule_id="DC-001", category="CONNECTIVITY", severity=FindingSeverity.HIGH,
            title="/VBUS_SYS contains source-free loads", description="d",
            nets=["/VBUS_SYS"], components=["U14"],
        )])
        ac = result("AC", AnalysisStatus.PASS, [
            finding("AC-001", FindingSeverity.INFO, nets=["/VBUS_SYS"], title="Target met"),
        ])
        emc = result("EMC", AnalysisStatus.PASS, [
            finding("PD-002", FindingSeverity.INFO, nets=["/VBUS_SYS"], title="PDN within target"),
        ])
        campaign = CampaignResult.from_results([dc, ac, emc])
        ac_result = next(r for r in campaign.results if r.analysis_type == "AC")
        emc_result = next(r for r in campaign.results if r.analysis_type == "EMC")
        expected = "/VBUS_SYS: DC model incomplete (U14 excluded from the solve)."
        self.assertIn(expected, ac_result.limitations)
        self.assertIn(expected, emc_result.limitations)

    def test_incomplete_rail_findings_group_into_one_action(self):
        dc = result("DC", findings=[AnalysisFinding(
            rule_id="DC-001", category="CONNECTIVITY", severity=FindingSeverity.HIGH,
            title="/VBUS_SYS contains source-free loads", description="d",
            nets=["/VBUS_SYS"], components=["U14"],
        )])
        # MEDIUM, not INFO: COH-9 takes INFO findings out of the action list
        # entirely, so this exercises the net-sharing join on an action-list
        # eligible finding rather than accidentally passing because the
        # INFO one was never in `tagged` to begin with.
        emc = result("EMC", AnalysisStatus.WARN, [
            finding("PD-002", FindingSeverity.MEDIUM, nets=["/VBUS_SYS"], title="PDN margin low"),
        ])
        campaign = CampaignResult.from_results([dc, emc])
        self.assertEqual(len(campaign.actions), 1)
        self.assertEqual(sorted(campaign.actions[0].domains), ["DC", "EMC"])


class DeduplicationTests(unittest.TestCase):
    def test_shared_component_merges_across_domains(self):
        campaign = CampaignResult.from_results([
            result("THERMAL", findings=[finding("TH-1", title="Hot", components=["U7"])]),
            result("EMC", findings=[finding("DE-2", title="Decoupling", components=["U7"])]),
        ])
        self.assertEqual(len(campaign.actions), 1)
        self.assertEqual(sorted(campaign.actions[0].domains), ["EMC", "THERMAL"])
        self.assertEqual(len(campaign.actions[0].consequences), 2)

    def test_different_nets_stay_separate(self):
        campaign = CampaignResult.from_results([
            result("DC", findings=[finding("DC-1", nets=["GND"])]),
            result("EMC", findings=[finding("GP-3", nets=["VCC"])]),
        ])
        self.assertEqual(len(campaign.actions), 2)

    def test_nearby_evidence_on_different_nets_does_not_merge(self):
        near = [AnalysisEvidence("PCB", "d", x_mm=10.0, y_mm=10.0, layer="F.Cu")]
        also_near = [AnalysisEvidence("PCB", "d", x_mm=11.0, y_mm=10.0, layer="F.Cu")]
        campaign = CampaignResult.from_results([
            result("DC", findings=[finding("DC-1", nets=["VCC"], evidence=near)]),
            result("EMC", findings=[finding("GP-3", nets=["CLK"], evidence=also_near)]),
        ])
        self.assertEqual(len(campaign.actions), 2)

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
            result("DC", findings=[finding("DC-1", FindingSeverity.HIGH, components=["Q3"])]),
            result("EMC", findings=[finding("GP-3", FindingSeverity.HIGH, components=["Q3"])]),
            result("THERMAL", findings=[finding("TH-1", FindingSeverity.HIGH, components=["U9"])]),
        ])
        ranked = campaign.top_actions()
        self.assertEqual(ranked[0].components, ["Q3"])
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

    def test_deterministic_outranks_estimated_at_equal_severity(self):
        # COH-1: gain_rank now folds in confidence (severity x confidence),
        # so a fact-backed HIGH outranks an estimated one instead of tying.
        campaign = CampaignResult.from_results([result("DC", findings=[
            finding("A", FindingSeverity.HIGH, nets=["N1"], confidence=EvidenceConfidence.ESTIMATED),
            finding("B", FindingSeverity.HIGH, nets=["N2"], confidence=EvidenceConfidence.DETERMINISTIC),
        ])])
        ranked = campaign.top_actions()
        self.assertEqual([a.rule_ids[0] for a in ranked], ["B", "A"])

    def test_residual_ties_break_on_title_not_domain_order(self):
        # P02 Rev E: six HIGH actions with no remediation all scored 25.0 and
        # ordered by which domain the campaign happened to run first
        # (docs/audit-2026-09-23.md COH-1). With equal severity/confidence/
        # effort/domain-count, order must now be a property of the actions,
        # not of self.results' order.
        campaign = CampaignResult.from_results([
            result("EMC", findings=[finding("A", FindingSeverity.HIGH, nets=["N9"], title="Zebra")]),
            result("DC", findings=[finding("B", FindingSeverity.HIGH, nets=["N1"], title="Apple")]),
        ])
        ranked = campaign.top_actions()
        self.assertEqual([a.title for a in ranked], ["Apple", "Zebra"])

    def test_recommendation_text_reaches_the_action_when_unstructured(self):
        # COH-1: finding.recommendation used to stop at the finding -- the
        # action never carried it, so the report had nothing to fall back on.
        campaign = CampaignResult.from_results([result("DIFFERENTIAL", findings=[
            AnalysisFinding(
                rule_id="SI-DIFF-001", category="DIFFERENTIAL", severity=FindingSeverity.HIGH,
                title="/USB_DEVICE_D: Fail High", description="d",
                confidence=EvidenceConfidence.ESTIMATED,
                recommendation="Route 0.155 mm / 0.250 mm gap -> 90.6 ohm.",
            ),
        ])])
        self.assertEqual(
            campaign.actions[0].recommendations, ["Route 0.155 mm / 0.250 mm gap -> 90.6 ohm."],
        )


class ProvenanceWarningTests(unittest.TestCase):
    def test_matching_fingerprints_warn_nothing(self):
        # COH-10: DC-004 proves AC/differential/EMC/thermal geometry (read
        # from the open board) and board_fingerprint (SHA of the saved file)
        # can differ even for one honest run; a campaign batch stamps every
        # domain with the same fingerprint (CampaignEngine._stamp), so that
        # case must stay silent.
        dc = AnalysisResult("DC", "DC", status=AnalysisStatus.PASS, board_fingerprint="abc123")
        emc = AnalysisResult("EMC", "EMC", status=AnalysisStatus.PASS, board_fingerprint="abc123")
        campaign = CampaignResult.from_results([dc, emc])
        self.assertEqual(campaign.provenance_warnings(), [])

    def test_differing_fingerprints_are_named(self):
        # The consolidate button (ui/main_dialog.py:on_build_campaign_report)
        # aggregates whatever results this session holds, which can be run
        # against a board edited and re-saved in between -- unlike a batch,
        # it never checked whether they still describe the same board.
        dc = AnalysisResult("DC", "DC", status=AnalysisStatus.PASS, board_fingerprint="abc123")
        emc = AnalysisResult("EMC", "EMC", status=AnalysisStatus.PASS, board_fingerprint="def456")
        campaign = CampaignResult.from_results([dc, emc])
        warnings = campaign.provenance_warnings()
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("DC" in line for line in warnings))
        self.assertTrue(any("EMC" in line for line in warnings))

    def test_missing_fingerprints_are_not_flagged_as_a_mismatch(self):
        dc = AnalysisResult("DC", "DC", status=AnalysisStatus.PASS, board_fingerprint="abc123")
        emc = AnalysisResult("EMC", "EMC", status=AnalysisStatus.PASS)
        campaign = CampaignResult.from_results([dc, emc])
        self.assertEqual(campaign.provenance_warnings(), [])


class SerializationTests(unittest.TestCase):
    def test_round_trip_preserves_aggregation(self):
        campaign = CampaignResult.from_results(
            [
                result("DC", findings=[finding(
                    "DC-1", FindingSeverity.CRITICAL, components=["Q3"],
                    remediations=[Remediation(
                        action="WIDEN_TRACK", target="seg-1",
                        current_value=0.25, proposed_value=0.6, unit="mm",
                        predicted_gain="drop 3.1% -> 1.8%", verified=True,
                    )],
                )]),
                result("EMC", findings=[finding("GP-3", components=["Q3"])]),
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
