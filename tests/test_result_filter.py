import unittest

from analysis_contract import AnalysisFinding, EvidenceConfidence, FindingSeverity
from application.result_filter import filter_findings


def finding(rule_id, severity, *, title="Finding", recommendation="", nets=None, components=None):
    return AnalysisFinding(
        rule_id=rule_id, category="TEST", severity=severity,
        title=title, description="Test description", recommendation=recommendation,
        confidence=EvidenceConfidence.ESTIMATED,
        nets=nets or [], components=components or [],
    )


class ResultFilterTests(unittest.TestCase):
    def setUp(self):
        self.findings = [
            finding("AC-001", FindingSeverity.HIGH, recommendation="Validate capacitor ESL", components=["C1"]),
            finding("EMI-002", FindingSeverity.MEDIUM, title="Return path gap", nets=["USB_D+"]),
            finding("INFO-001", FindingSeverity.INFO, title="Model note"),
        ]

    def test_severity_groups_are_consistent(self):
        self.assertEqual(
            [item.rule_id for item in filter_findings(self.findings, "Critical / High")],
            ["AC-001"],
        )
        self.assertEqual(
            [item.rule_id for item in filter_findings(self.findings, "Actionable (Critical–Medium)")],
            ["AC-001", "EMI-002"],
        )
        self.assertEqual(
            [item.rule_id for item in filter_findings(self.findings, "Low / Info")],
            ["INFO-001"],
        )

    def test_search_covers_rule_text_recommendation_net_and_component(self):
        for query, expected in (
            ("emi-002", "EMI-002"),
            ("return path", "EMI-002"),
            ("esl", "AC-001"),
            ("usb_d+", "EMI-002"),
            ("c1", "AC-001"),
        ):
            with self.subTest(query=query):
                self.assertEqual(filter_findings(self.findings, query=query)[0].rule_id, expected)

    def test_search_is_case_insensitive_and_combines_with_severity(self):
        self.assertEqual(
            filter_findings(self.findings, "Critical / High", "VALIDATE"),
            [self.findings[0]],
        )
        self.assertEqual(filter_findings(self.findings, "Low / Info", "validate"), [])


if __name__ == "__main__":
    unittest.main()
