"""A campaign leaves itself beside its report (review 2026-10-02)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from analysis_contract import (  # noqa: E402
    AnalysisFinding, AnalysisResult, AnalysisStatus, EvidenceConfidence, FindingSeverity,
)
from campaign import CampaignResult  # noqa: E402


class CampaignFilesTests(unittest.TestCase):
    def test_the_campaign_is_saved_with_its_report(self):
        # P02 Rev E's KiPIDA-results held 22 reports and their CSV files, and
        # no campaign a later run could be compared with.
        from report.campaign_files import write_campaign_files

        dc = AnalysisResult("DC", "DC Power", status=AnalysisStatus.WARN, findings=[AnalysisFinding(
            rule_id="DC-008", category="CONNECTIVITY", severity=FindingSeverity.HIGH,
            title="GND pads are cut off from the rest of the net", description="C88.2, R10.2",
            confidence=EvidenceConfidence.ESTIMATED, nets=["GND"], components=["C88", "R10"],
        )])
        campaign = CampaignResult.from_results([dc], project_name="p02_revision_e")
        with tempfile.TemporaryDirectory() as directory:
            paths = write_campaign_files(campaign, directory, "20261002-101500")
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), [
                "actions-20261002-101500.csv", "campaign-20261002-101500.html",
                "campaign-20261002-101500.json", "findings-20261002-101500.csv",
            ])
            saved = CampaignResult.from_json(paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(saved.campaign_id, campaign.campaign_id)
        self.assertEqual(saved.tool_version, campaign.tool_version)
        self.assertEqual(saved.results[0].findings[0].components, ["C88", "R10"])

    def test_the_next_report_says_what_changed_since(self):
        # The comparison existed and nothing called it: whether C88 and R10
        # were reconnected was read off two reports by eye.
        from report.campaign_files import write_campaign_files

        def campaign_of(parts, tool):
            result = AnalysisResult("DC", "DC Power", status=AnalysisStatus.WARN, findings=[
                AnalysisFinding(rule_id="DC-008", category="CONNECTIVITY",
                                severity=FindingSeverity.HIGH, title="GND pads are cut off",
                                description="d", components=list(parts))
            ] if parts else [])
            campaign = CampaignResult.from_results([result], project_name="p02_revision_e")
            campaign.tool_version = tool
            return campaign

        with tempfile.TemporaryDirectory() as directory:
            first = write_campaign_files(campaign_of(["C88", "R10"], "0.19.0+aaaaaaaa"),
                                         directory, "20261002-101500")
            self.assertNotIn('id="since"', first["html"].read_text(encoding="utf-8"))
            second = write_campaign_files(campaign_of(["C88"], "0.19.0+bbbbbbbb"),
                                          directory, "20261002-111500")
            html = second["html"].read_text(encoding="utf-8")
        since = html.split('id="since"')[1].split("</section>")[0]
        self.assertIn("Resolved (1)", since)
        self.assertIn("GND pads are cut off", since)
        self.assertIn("R10", since.split("New")[0])
        self.assertIn("0.19.0+aaaaaaaa to 0.19.0+bbbbbbbb", since)


if __name__ == "__main__":
    unittest.main()
