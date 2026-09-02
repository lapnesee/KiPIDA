import unittest

from analysis_contract import (
    AnalysisEvidence, AnalysisFinding, AnalysisResult, EvidenceConfidence,
    FindingSeverity,
)
from application.result_detail_presenter import format_finding_detail, format_result_basis


class ResultDetailPresenterTests(unittest.TestCase):
    def test_finding_detail_separates_recommendation_and_evidence(self):
        finding = AnalysisFinding(
            rule_id="AC-001", category="TARGET", severity=FindingSeverity.HIGH,
            title="Target is not met", description="Worst point is at 100 MHz.",
            recommendation="Validate ESL.", confidence=EvidenceConfidence.ESTIMATED,
            nets=["+3V3", "GND"], components=["C1"],
            evidence=[AnalysisEvidence("AC_SWEEP", "121 solved points", "CPU")],
        )
        text = format_finding_detail(finding)
        self.assertIn("AC-001 — HIGH — ESTIMATED", text)
        self.assertIn("Recommendation\n--------------\nValidate ESL.", text)
        self.assertIn("Nets: +3V3, GND", text)
        self.assertIn("Components: C1", text)
        self.assertIn("AC_SWEEP [CPU]: 121 solved points", text)

    def test_result_basis_separates_provenance_and_limitations(self):
        result = AnalysisResult(
            "AC", "AC Impedance",
            provenance=[AnalysisEvidence("PDN_MODEL", "Uses PCB geometry")],
            limitations=["ESL is estimated."],
        )
        text = format_result_basis(result)
        self.assertIn("Provenance\n----------", text)
        self.assertIn("PDN_MODEL: Uses PCB geometry", text)
        self.assertIn("Model limitations\n-----------------", text)
        self.assertIn("- ESL is estimated.", text)

    def test_empty_states_are_explicit(self):
        self.assertIn("Select a finding", format_finding_detail(None))
        self.assertIn("No structured evidence", format_result_basis(None))
