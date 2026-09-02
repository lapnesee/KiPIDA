import json
import unittest

from analysis_contract import (
    AnalysisEvidence, AnalysisFinding, AnalysisMetric, AnalysisResult,
    AnalysisStatus, EvidenceConfidence, FindingSeverity, SCHEMA_VERSION,
    normalize_evidence_confidence,
)


class AnalysisResultContractTests(unittest.TestCase):
    def sample(self):
        return AnalysisResult(
            analysis_type="dc",
            title="DC Power",
            status=AnalysisStatus.WARN,
            summary={"rail_count": 2},
            findings=[AnalysisFinding(
                rule_id="DC-001",
                finding_id="DC-001:+3V3",
                category="CONNECTIVITY",
                severity=FindingSeverity.HIGH,
                title="Source-free island",
                description="One load is disconnected from every source.",
                recommendation="Reconnect the island.",
                confidence=EvidenceConfidence.DETERMINISTIC,
                nets=["+3V3"],
                evidence=[AnalysisEvidence("pcb_geometry", "Disconnected copper island")],
            )],
            metrics=[AnalysisMetric("max_drop", "Maximum drop", 2.1, "%", "WARN")],
            limitations=["No connector contact resistance model."],
        ).finish()

    def test_round_trip_preserves_common_fields(self):
        expected = self.sample()
        restored = AnalysisResult.from_json(expected.to_json())
        self.assertEqual(restored.schema_version, SCHEMA_VERSION)
        self.assertEqual(restored.analysis_type, "DC")
        self.assertEqual(restored.status, AnalysisStatus.WARN)
        self.assertEqual(restored.findings[0].severity, FindingSeverity.HIGH)
        self.assertEqual(restored.findings[0].confidence, EvidenceConfidence.DETERMINISTIC)
        self.assertEqual(restored.metrics[0].unit, "%")

    def test_serialized_result_includes_severity_rollup(self):
        payload = json.loads(self.sample().to_json())
        self.assertEqual(payload["severity_counts"]["HIGH"], 1)
        self.assertEqual(payload["severity_counts"]["INFO"], 0)

    def test_duplicate_finding_instances_are_allowed_but_ids_are_unique(self):
        result = self.sample()
        duplicate_rule = AnalysisFinding(
            rule_id="DC-001", finding_id="DC-001:+5V", category="CONNECTIVITY",
            severity=FindingSeverity.MEDIUM, title="Another island", description="Detail",
        )
        result.findings.append(duplicate_rule)
        result.validate()
        duplicate_rule.finding_id = "DC-001:+3V3"
        with self.assertRaisesRegex(ValueError, "finding_id"):
            result.validate()

    def test_legacy_report_is_explicitly_marked(self):
        result = AnalysisResult.legacy_report("AC", "AC Impedance", "report", ["Sweep"])
        self.assertEqual(result.status, AnalysisStatus.NO_DATA)
        self.assertTrue(result.summary["legacy_report"])
        self.assertIn("Legacy result", result.limitations[0])
        self.assertEqual(result.provenance[0].source, "LEGACY_HISTORY")

    def test_metric_keys_must_be_unique(self):
        result = self.sample()
        result.metrics.append(AnalysisMetric("max_drop", "Duplicate", 1.0))
        with self.assertRaisesRegex(ValueError, "metric keys"):
            result.validate()

    def test_legacy_confidence_labels_have_one_shared_mapping(self):
        self.assertEqual(
            normalize_evidence_confidence("HIGH"), EvidenceConfidence.DETERMINISTIC,
        )
        self.assertEqual(
            normalize_evidence_confidence("MEDIUM"), EvidenceConfidence.ESTIMATED,
        )
        self.assertEqual(
            normalize_evidence_confidence("LOW"), EvidenceConfidence.HEURISTIC,
        )
        self.assertEqual(
            normalize_evidence_confidence(EvidenceConfidence.MEASURED),
            EvidenceConfidence.MEASURED,
        )


if __name__ == "__main__":
    unittest.main()
