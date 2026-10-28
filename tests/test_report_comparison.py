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


def finding(rule, nets=(), components=(), severity=FindingSeverity.HIGH, title=None):
    return AnalysisFinding(
        rule_id=rule, category="TEST", severity=severity,
        title=title or f"Problem {rule}", description="d", nets=list(nets),
        components=list(components),
    )


def campaign(findings_by_domain, fingerprint="fp-1", tool="0.19.0+aaaaaaaa"):
    results = [
        AnalysisResult(analysis_type=domain, title=domain, status=AnalysisStatus.WARN,
                       findings=list(findings))
        for domain, findings in findings_by_domain.items()
    ]
    result = CampaignResult.from_results(results, board_fingerprint=fingerprint)
    result.tool_version = tool
    return result


def nets_campaign(rule_net_pairs, domain="DC", **kwargs):
    return campaign({domain: [finding(rule, nets=[net]) for rule, net in rule_net_pairs]}, **kwargs)


class ComparisonTests(unittest.TestCase):
    def test_resolved_introduced_and_persisting_are_classified(self):
        delta = compare_campaigns(nets_campaign([("DC-1", "VCC"), ("DC-2", "GND")]),
                                  nets_campaign([("DC-2", "GND"), ("DC-3", "VDD")]))
        self.assertEqual([item.subject for item in delta.resolved], ["VCC"])
        self.assertEqual([item.subject for item in delta.introduced], ["VDD"])
        self.assertEqual([item.subject for item in delta.persisting], ["GND"])
        self.assertEqual(delta.net_improvement, 0)

    def test_one_capacitor_fixed_is_one_resolved_not_a_new_finding(self):
        # P02 Rev E, 2026-10-01 21:52 -> 22:38: comparing actions read
        # "resolved SW-004,TH-008 / introduced TH-008" when only SW-004 had
        # gone; TE-002 lists thirteen capacitors, and dropping one changed the
        # whole action's identity.
        x5r = ["C37", "C36", "C49"]
        before = campaign({
            "EMC": [finding("TE-002", components=x5r), finding("SW-004", components=["U4", "L1"],
                                                              severity=FindingSeverity.LOW)],
            "THERMAL": [finding("TH-008", components=["U4"], severity=FindingSeverity.LOW)],
        })
        after = campaign({
            "EMC": [finding("TE-002", components=x5r[:2])],
            "THERMAL": [finding("TH-008", components=["U4"], severity=FindingSeverity.LOW)],
        })
        delta = compare_campaigns(before, after)
        self.assertEqual(sorted((item.rule_id, item.subject) for item in delta.resolved),
                         [("SW-004", "L1"), ("SW-004", "U4"), ("TE-002", "C49")])
        self.assertEqual(delta.introduced, [])
        self.assertIn(("TH-008", "U4"), [(item.rule_id, item.subject) for item in delta.persisting])

    def test_identity_collisions_are_matched_by_count_not_overwritten(self):
        # COH-7: five differential pairs, four fixed, read four resolved.
        def pairs(titles):
            return campaign({"DIFFERENTIAL": [
                finding("SI-DIFF-001", title=f"{title}: Fail High") for title in titles
            ]})

        delta = compare_campaigns(
            pairs(["/USB_DEVICE_D", "/USB_J2_MUX_D", "/USB_HOST_D", "/USB_J1_D", "/USB_J2_D"]),
            pairs(["/USB_DEVICE_D"]),
        )
        self.assertEqual((len(delta.resolved), len(delta.persisting), len(delta.introduced)),
                         (4, 1, 0))

    def test_a_domain_run_once_is_not_compared_and_a_new_build_is_said(self):
        # A DC-only campaign against a full one read "10 resolved" and -100
        # per missing domain; P02's EMC score moved with the tool alone.
        before = campaign({"DC": [finding("DC-008", components=["C88"])],
                           "EMC": [finding("TE-002", components=["C37"])]})
        after = campaign({"DC": [finding("DC-008", components=["C88"])]}, tool="0.19.0+bbbbbbbb")
        delta = compare_campaigns(before, after)
        self.assertEqual((delta.resolved, delta.not_compared), ([], ["EMC"]))
        self.assertEqual(list(delta.score_deltas), ["DC"])
        self.assertTrue(delta.tool_changed)
        self.assertFalse(delta.board_changed)

    def test_a_finding_regraded_is_a_severity_change(self):
        # TE-002 now grades C25 (+2.3 K) as MEDIUM, where it was HIGH.
        delta = compare_campaigns(
            campaign({"EMC": [finding("TE-002", components=["C25"])]}),
            campaign({"EMC": [finding("TE-002", components=["C25"],
                                      severity=FindingSeverity.MEDIUM)]}),
        )
        self.assertEqual([(before.severity, after.severity) for before, after in delta.changed],
                         [("HIGH", "MEDIUM")])
        self.assertEqual((delta.resolved, delta.introduced), ([], []))

    def test_board_changed_flag_and_score_deltas(self):
        delta = compare_campaigns(nets_campaign([("DC-1", "VCC")], fingerprint="fp-old"),
                                  nets_campaign([], fingerprint="fp-new"))
        self.assertTrue(delta.board_changed)
        # One HIGH finding of heuristic confidence costs 3 points (12 x 0.25).
        self.assertAlmostEqual(delta.score_deltas["DC"], 3.0)
        self.assertEqual(delta.net_improvement, 1)

    def test_pre_rename_emc_rule_ids_still_match(self):
        delta = compare_campaigns(nets_campaign([("DC-001", "+3V3")], domain="EMC"),
                                  nets_campaign([("DE-001", "+3V3")], domain="EMC"))
        self.assertEqual((len(delta.persisting), delta.resolved, delta.introduced), (1, [], []))

    def test_dc_domain_rule_ids_are_not_translated(self):
        delta = compare_campaigns(nets_campaign([("DC-001", "+3V3")]),
                                  nets_campaign([("DE-001", "+3V3")]))
        self.assertEqual((len(delta.resolved), len(delta.introduced)), (1, 1))


if __name__ == "__main__":
    unittest.main()
