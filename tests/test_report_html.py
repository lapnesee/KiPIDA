"""Tests for the standalone HTML campaign report (report/html_report.py)."""

import base64
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from analysis_contract import (  # noqa: E402
    AnalysisArtifact,
    AnalysisFinding,
    AnalysisResult,
    AnalysisStatus,
    EvidenceConfidence,
    FindingSeverity,
    Remediation,
)
from campaign import CampaignResult  # noqa: E402
from report.html_report import render_campaign_html, write_campaign_html  # noqa: E402


def campaign_with(findings, artifacts=None, **kwargs):
    result = AnalysisResult(
        analysis_type="DC", title="DC Power", status=AnalysisStatus.WARN,
        findings=list(findings), artifacts=list(artifacts or []),
    )
    return CampaignResult.from_results([result], **kwargs)


class StructureTests(unittest.TestCase):
    def test_document_contains_all_four_sections_and_verdict(self):
        campaign = campaign_with([AnalysisFinding(
            rule_id="DC-1", category="DC", severity=FindingSeverity.HIGH,
            title="Excessive drop", description="d", nets=["VCC"],
        )])
        document = render_campaign_html(campaign)
        for anchor in ('id="synthesis"', 'id="actions"', 'id="domains"', 'id="appendices"'):
            self.assertIn(anchor, document)
        self.assertIn("WARN", document)
        self.assertTrue(document.startswith("<!DOCTYPE html>"))

    def test_write_creates_parent_directories(self):
        campaign = campaign_with([])
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "deeper" / "report.html"
            written = write_campaign_html(campaign, target)
            self.assertTrue(written.is_file())
            self.assertIn("<!DOCTYPE html>", written.read_text(encoding="utf-8"))


class EscapingTests(unittest.TestCase):
    def test_hostile_net_name_is_escaped(self):
        # Net names come from user project files; an unescaped one turns the
        # report into an injection vector when opened in a browser.
        campaign = campaign_with([AnalysisFinding(
            rule_id="DC-1", category="DC", severity=FindingSeverity.HIGH,
            title="Drop on <script>alert(1)</script>", description="A & B",
            nets=['<script>alert("x")</script>'],
        )])
        document = render_campaign_html(campaign)
        self.assertNotIn("<script>alert", document)
        self.assertIn("&lt;script&gt;", document)
        self.assertIn("A &amp; B", document)


class ConfidenceRenderingTests(unittest.TestCase):
    def test_finding_confidence_is_visible(self):
        campaign = campaign_with([
            AnalysisFinding(
                rule_id="DC-1", category="DC", severity=FindingSeverity.HIGH,
                title="Measured drop", description="d", nets=["VCC"],
                confidence=EvidenceConfidence.DETERMINISTIC,
            ),
            AnalysisFinding(
                rule_id="DC-2", category="DC", severity=FindingSeverity.LOW,
                title="Guessed drop", description="d", nets=["VDD"],
                confidence=EvidenceConfidence.HEURISTIC,
            ),
        ])
        document = render_campaign_html(campaign)
        self.assertIn("DETERMINISTIC", document)
        self.assertIn("HEURISTIC", document)


class RemediationMarkerTests(unittest.TestCase):
    def test_verified_and_unverified_render_distinctly(self):
        campaign = campaign_with([AnalysisFinding(
            rule_id="DC-1", category="DC", severity=FindingSeverity.HIGH,
            title="Drop", description="d", nets=["VCC"],
            remediations=[
                Remediation(action="WIDEN_TRACK", target="seg-1",
                            current_value=0.25, proposed_value=0.6, unit="mm",
                            predicted_gain="3.1% -> 1.8%", verified=True),
                Remediation(action="ADD_STITCHING_VIAS", target="seg-2",
                            predicted_gain="unknown", verified=False),
            ],
        )])
        document = render_campaign_html(campaign)
        self.assertIn("VERIFIED BY RE-SIMULATION", document)
        self.assertIn("NOT RE-SIMULATED", document)


class ArtifactTests(unittest.TestCase):
    def test_missing_artifact_renders_placeholder_without_raising(self):
        campaign = campaign_with(
            [],
            artifacts=[AnalysisArtifact(
                artifact_id="plot-01", title="Voltage map", kind="plot",
                path="/definitely/not/here/plot.png", media_type="image/png",
            )],
        )
        document = render_campaign_html(campaign)
        self.assertIn("Artifact unavailable", document)
        self.assertIn("plot.png", document)

    def test_existing_png_is_embedded_as_data_uri(self):
        # Smallest valid 1x1 transparent PNG.
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
            "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "plot.png"
            image.write_bytes(png)
            campaign = campaign_with([], artifacts=[AnalysisArtifact(
                artifact_id="plot-01", title="Voltage map", kind="plot",
                path=str(image), media_type="image/png",
            )])
            document = render_campaign_html(campaign)
            self.assertIn("data:image/png;base64,", document)
            self.assertNotIn("Artifact unavailable", document)


class SelfContainmentTests(unittest.TestCase):
    def test_no_external_references(self):
        campaign = campaign_with([AnalysisFinding(
            rule_id="DC-1", category="DC", severity=FindingSeverity.HIGH,
            title="Drop", description="d", nets=["VCC"],
        )])
        document = render_campaign_html(campaign)
        self.assertNotIn("http://", document)
        self.assertNotIn("https://", document)
        # Every src= must be a data: URI.
        for src in re.findall(r'src="([^"]*)"', document):
            self.assertTrue(src.startswith("data:"), f"external src: {src[:60]}")
        self.assertNotIn("<script", document)


if __name__ == "__main__":
    unittest.main()
