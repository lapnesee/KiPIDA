"""Tests for CSV export of campaign findings and actions."""

import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from analysis_contract import (  # noqa: E402
    AnalysisFinding,
    AnalysisResult,
    AnalysisStatus,
    FindingSeverity,
    Remediation,
)
from campaign import CampaignResult  # noqa: E402
from report.csv_export import (  # noqa: E402
    ACTION_COLUMNS,
    FINDING_COLUMNS,
    write_actions_csv,
    write_domains_csv,
    write_findings_csv,
)


def read_csv(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


class FindingsCsvTests(unittest.TestCase):
    def setUp(self):
        self.campaign = CampaignResult.from_results([AnalysisResult(
            analysis_type="DC", title="DC Power", status=AnalysisStatus.WARN,
            findings=[
                AnalysisFinding(
                    rule_id="DC-1", category="DC", severity=FindingSeverity.HIGH,
                    title="Two fixes", description="d", nets=["VCC", "VDD"],
                    remediations=[
                        Remediation(action="WIDEN_TRACK", target="seg-1",
                                    current_value=0.25, proposed_value=0.6,
                                    unit="mm", verified=True),
                        Remediation(action="ADD_STITCHING_VIAS", target="seg-2"),
                    ],
                ),
                AnalysisFinding(
                    rule_id="DC-2", category="DC", severity=FindingSeverity.LOW,
                    title="No fix", description="d", nets=["GND"],
                ),
            ],
        )])

    def test_row_count_follows_remediation_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = read_csv(write_findings_csv(self.campaign, Path(tmp) / "f.csv"))
        # 2 remediations + 1 finding without any = 3 rows.
        self.assertEqual(len(rows), 3)
        by_rule = [r["rule_id"] for r in rows]
        self.assertEqual(by_rule.count("DC-1"), 2)
        self.assertEqual(by_rule.count("DC-2"), 1)

    def test_columns_and_multivalue_joining(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = read_csv(write_findings_csv(self.campaign, Path(tmp) / "f.csv"))
        self.assertEqual(list(rows[0].keys()), FINDING_COLUMNS)
        self.assertEqual(rows[0]["nets"], "VCC;VDD")
        self.assertEqual(rows[0]["verified"], "true")
        empty = next(r for r in rows if r["rule_id"] == "DC-2")
        self.assertEqual(empty["remediation_action"], "")


class ActionsCsvTests(unittest.TestCase):
    def test_actions_are_ranked_and_columns_present(self):
        campaign = CampaignResult.from_results([
            AnalysisResult(
                analysis_type="DC", title="DC", status=AnalysisStatus.WARN,
                findings=[AnalysisFinding(
                    rule_id="DC-1", category="DC", severity=FindingSeverity.LOW,
                    title="Minor", description="d", nets=["A"],
                )],
            ),
            AnalysisResult(
                analysis_type="EMC", title="EMC", status=AnalysisStatus.FAIL,
                findings=[AnalysisFinding(
                    rule_id="GP-1", category="EMC", severity=FindingSeverity.CRITICAL,
                    title="Severe", description="d", nets=["B"],
                )],
            ),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            rows = read_csv(write_actions_csv(campaign, Path(tmp) / "a.csv"))
        self.assertEqual(list(rows[0].keys()), ACTION_COLUMNS)
        self.assertEqual(rows[0]["rank"], "1")
        self.assertEqual(rows[0]["severity"], "CRITICAL")

    def test_recommendation_text_reaches_the_csv_without_structured_remediation(self):
        # COH-1: actions.csv (report/csv_export.py:98-106) showed nothing for
        # an action whose only advice was AnalysisFinding.recommendation.
        campaign = CampaignResult.from_results([AnalysisResult(
            analysis_type="DIFFERENTIAL", title="D", status=AnalysisStatus.FAIL,
            findings=[AnalysisFinding(
                rule_id="SI-DIFF-001", category="DIFFERENTIAL", severity=FindingSeverity.HIGH,
                title="/USB_DEVICE_D: Fail High", description="d",
                recommendation="Route 0.155 mm / 0.250 mm gap -> 90.6 ohm.",
            )],
        )])
        with tempfile.TemporaryDirectory() as tmp:
            rows = read_csv(write_actions_csv(campaign, Path(tmp) / "a.csv"))
        self.assertEqual(rows[0]["remediation_actions"], "")
        self.assertEqual(rows[0]["recommendations"], "Route 0.155 mm / 0.250 mm gap -> 90.6 ohm.")


class IdentityColumnsTests(unittest.TestCase):
    def test_rows_name_their_campaign_run_and_decision(self):
        # P02 Rev E's 39 findings-*.csv named neither campaign nor run, and
        # actions-*.csv left out which list of the report each action is in.
        dc = AnalysisResult("DC", "DC Power", status=AnalysisStatus.WARN, findings=[AnalysisFinding(
            rule_id="DC-008", category="CONNECTIVITY", severity=FindingSeverity.HIGH,
            title="GND pads are cut off from the rest of the net", description="C88.2, R10.2",
            nets=["GND"], components=["C88", "R10"],
        )], tool_version="0.19.0+38bcd630 (19242f5 2026-10-02)")
        campaign = CampaignResult.from_results([dc], coverage=[
            {"domain": "THERMAL", "state": "FAILED", "reason": "RuntimeError: boom"},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            finding = read_csv(write_findings_csv(campaign, Path(tmp) / "f.csv"))[0]
            action = read_csv(write_actions_csv(campaign, Path(tmp) / "a.csv"))[0]
            domains = read_csv(write_domains_csv(campaign, Path(tmp) / "d.csv"))
        self.assertEqual((finding["campaign_id"], finding["run_id"]), (campaign.campaign_id, dc.run_id))
        self.assertEqual(finding["tool_version"], "0.19.0+38bcd630 (19242f5 2026-10-02)")
        self.assertEqual((action["campaign_id"], action["decision"]), (campaign.campaign_id, "fix"))
        self.assertEqual([(row["domain"], row["status"], row["campaign_status"]) for row in domains],
                         [("DC", "WARN", "FAIL"), ("THERMAL", "FAILED", "FAIL")])
        self.assertEqual(domains[0]["finding_counts"], "HIGH=1")
        self.assertEqual(domains[1]["reason"], "RuntimeError: boom")


if __name__ == "__main__":
    unittest.main()
