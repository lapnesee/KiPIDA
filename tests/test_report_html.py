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
    AnalysisMetric,
    AnalysisResult,
    AnalysisStatus,
    EvidenceConfidence,
    FindingSeverity,
    Remediation,
)
from campaign import CampaignResult  # noqa: E402
import i18n  # noqa: E402
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

    def test_a_domain_that_did_not_complete_heads_the_decision(self):
        # Review of 2026-10-02: a failed THERMAL left the report reading
        # "PASS -- No defect found by the analyses that ran".
        campaign = campaign_with([], coverage=[
            {"domain": "THERMAL", "state": "FAILED", "reason": "RuntimeError: boom"},
        ])
        document = render_campaign_html(campaign)
        self.assertEqual(campaign.overall_status, AnalysisStatus.WARN)
        self.assertNotIn("No defect found", document)
        self.assertIn("Did not complete (1)", document)
        self.assertIn("<strong>THERMAL</strong>", document)
        self.assertIn("failed: RuntimeError: boom", document)

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


class NumberFormattingTests(unittest.TestCase):
    def test_tiny_floating_point_noise_prints_as_below_threshold(self):
        # COH-12, P02 Rev E raw metric values: "8.074349270001136e-14 %"
        # and "1.03e-09 A/mm2".
        from report.html_report import _number
        self.assertEqual(_number(8.074349270001136e-14), "&lt; 1e-6")
        self.assertEqual(_number(1.03e-09), "&lt; 1e-6")
        self.assertEqual(_number(0.0), "0")

    def test_ordinary_values_use_significant_digits_not_python_repr(self):
        from report.html_report import _number
        self.assertEqual(_number(0.1 + 0.2), "0.3")

    def test_explicit_precision_is_still_honoured(self):
        from report.html_report import _number
        self.assertEqual(_number(4.44444, precision=1), "4.4")

    def test_large_values_print_without_an_exponent(self):
        # P02 Rev E, 2026-10-01 21:42: 1530 EMC checks read "1.53e+03" and
        # /VBUS_SYS's 102,048-node AC mesh "1.02e+05".
        from report.html_report import _number
        self.assertEqual(_number(1530), "1 530")
        self.assertEqual(_number(102048), "102 048")
        self.assertEqual(_number(38311.87), "38 300")

    def test_frequency_metric_reads_in_its_prefix(self):
        # Same run: /VBUS_SYS's worst frequency printed "3.83e+04 Hz".
        campaign = campaign_with([])
        campaign.results[0].metrics.append(AnalysisMetric(
            key="/VBUS_SYS:worst_frequency", label="/VBUS_SYS Worst frequency",
            value=38311.86849557285, unit="Hz",
        ))
        document = render_campaign_html(campaign)
        self.assertIn("<td>38.3</td><td>kHz</td>", document)
        self.assertNotIn("e+04", document)

    def test_metric_table_shows_formatted_value_not_raw_repr(self):
        campaign = campaign_with([])
        campaign.results[0].metrics.append(AnalysisMetric(
            key="usb_pair_impedance_error", label="USB pair impedance error",
            value=8.074349270001136e-14, unit="%",
        ))
        document = render_campaign_html(campaign)
        self.assertNotIn("8.074349270001136e-14", document)
        self.assertIn("&lt; 1e-6", document)


class LayerNameTests(unittest.TestCase):
    def test_user_stackup_name_keeps_the_canonical_id(self):
        # COH-12: DC-004 named the same physical layer "F.Cu" while other
        # domains reported the stackup's user name, "F.Signal"/"B.Signal".
        from report.html_report import _layer_name
        self.assertEqual(_layer_name("F.Signal"), "F.Signal (F.Cu)")
        self.assertEqual(_layer_name("B.Signal"), "B.Signal (B.Cu)")

    def test_canonical_name_is_not_parenthesised_against_itself(self):
        from report.html_report import _layer_name
        self.assertEqual(_layer_name("F.Cu"), "F.Cu")
        self.assertEqual(_layer_name("In2.Cu"), "In2.Cu")

    def test_unrecognised_string_is_left_alone(self):
        from report.html_report import _layer_name
        self.assertEqual(_layer_name("Top Silkscreen"), "Top Silkscreen")


class ProvenanceTests(unittest.TestCase):
    def test_mismatched_board_fingerprints_are_warned_about(self):
        # COH-10: two domains describing different board saves used to be
        # invisible in the consolidated report.
        dc = AnalysisResult("DC", "DC", status=AnalysisStatus.PASS, board_fingerprint="abc123")
        emc = AnalysisResult("EMC", "EMC", status=AnalysisStatus.PASS, board_fingerprint="def456")
        campaign = CampaignResult.from_results([dc, emc])
        document = render_campaign_html(campaign)
        self.assertIn("do not all", document)
        self.assertIn("abc123", document)
        self.assertIn("def456", document)


class RecommendationFallbackTests(unittest.TestCase):
    def test_finding_recommendation_shown_when_no_structured_remediation(self):
        # COH-1: on P02 Rev E (campaign-20260922-215852.html) all 26 actions
        # read "No structured remediation was computed", including the
        # differential adapter's SI-DIFF-001 findings, which carry the
        # recommender's geometry only as AnalysisFinding.recommendation text
        # (analysis_adapters.py:469-475) -- never shown, never printed.
        campaign = campaign_with([AnalysisFinding(
            rule_id="SI-DIFF-001", category="DIFFERENTIAL", severity=FindingSeverity.HIGH,
            title="/USB_DEVICE_D: Fail High", description="d",
            recommendation=(
                "Route the pair 0.155 mm wide with a 0.250 mm gap on B.Cu: "
                "the model predicts 90.6 ohm."
            ),
        )])
        document = render_campaign_html(campaign)
        self.assertNotIn("No change was computed for this action.</p>", document)
        self.assertIn("0.155 mm wide", document)

    def test_no_recommendation_still_reports_the_gap_honestly(self):
        campaign = campaign_with([AnalysisFinding(
            rule_id="DC-1", category="DC", severity=FindingSeverity.HIGH,
            title="Drop", description="d", nets=["VCC"],
        )])
        document = render_campaign_html(campaign)
        self.assertIn("No change was computed for this action.</p>", document)


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


class DecisionTests(unittest.TestCase):
    def test_p02_findings_land_in_the_list_a_beginner_acts_on(self):
        # Audit of 2026-10-01 (LE-10, LE-11): on P02 Rev E, DC-008 (C88 and
        # R10 cut off from GND) and SW-003 (a 20 mm hot-loop threshold) both
        # read HIGH among 13 actions, with TH-004, a mesh diagnostic, third.
        campaign = CampaignResult.from_results([
            AnalysisResult("DC", "DC", status=AnalysisStatus.WARN, findings=[AnalysisFinding(
                rule_id="DC-008", category="CONNECTIVITY", severity=FindingSeverity.HIGH,
                title="GND pads are cut off from the rest of the net", description="d",
                nets=["GND"], components=["C88", "R10"],
            )]),
            AnalysisResult("EMC", "EMC", status=AnalysisStatus.WARN, findings=[AnalysisFinding(
                rule_id="SW-003", category="SWITCHING", severity=FindingSeverity.HIGH,
                title="Switching hot loop is spread out", description="Q10 and Q11 are 21.3 mm apart.",
                components=["C37", "Q10", "Q11"],
            )]),
            AnalysisResult("THERMAL", "Thermal", status=AnalysisStatus.WARN, findings=[AnalysisFinding(
                rule_id="TH-004", category="NUMERICS", severity=FindingSeverity.MEDIUM,
                title="Thermal result is not converged in mesh", description="Hotspot 118.99 C.",
                recommendation="Rerun at 0.127 mm.",
            )]),
        ])
        document = render_campaign_html(campaign)
        decision = document.split('id="actions"')[0]
        lists = {
            heading: decision.split(f"<h3>{heading}")[1].split("<h3>")[0]
            for heading in ("Fix before manufacture", "Check on the prototype",
                            "Calculation reliability")
        }
        self.assertIn("C88, R10", lists["Fix before manufacture"])
        self.assertIn("C37, Q10, Q11", lists["Check on the prototype"])
        self.assertIn("Rerun at 0.127 mm.", lists["Calculation reliability"])
        self.assertIn("Hotspot 118.99 °C.", document)


class DecisionListTests(unittest.TestCase):
    def test_parts_beyond_the_list_are_counted_with_the_parts(self):
        # P02 Rev E, 16:51: SCH-003's 29 resistors read "R9 · réseau /+3V3AO,
        # ... et 21 de plus", the count of parts printed after the nets.
        resistors = [f"R{index}" for index in range(1, 30)]
        campaign = campaign_with([AnalysisFinding(
            rule_id="SCH-003", category="SCHEMATIC", severity=FindingSeverity.INFO,
            title="Missing power-rating data", description="d",
            components=resistors, nets=["/+3V3AO", "/+5V_U4"],
        )])
        document = render_campaign_html(campaign)
        self.assertIn("R7, R8 and 21 more &middot; net /+3V3AO, /+5V_U4", document)


class CrossCheckTests(unittest.TestCase):
    def test_thermal_and_cfd_air_are_set_side_by_side(self):
        # P02 Rev E, 2026-10-01 (audit CO-5, LE-7): thermal solved at 61.2 C
        # internal air; the CFD table also read a 170.9 C solid maximum marked
        # NOT_USABLE, explained in the appendix. The CFD's 60.65 C "internal
        # ambient" is the thermal's own formula (review 2026-10-02); the
        # solved field's mean is what checks it.
        thermal = AnalysisResult("THERMAL", "3D Thermal", status=AnalysisStatus.WARN, metrics=[
            AnalysisMetric("internal_air_c", "Internal air (enclosure)", 61.2, "°C", "INFO"),
        ])
        cfd = AnalysisResult("CFD", "Enclosure CFD", status=AnalysisStatus.PASS, metrics=[
            AnalysisMetric("internal_ambient", "Internal ambient", 60.65, "°C", "PASS"),
            AnalysisMetric("solved_mean_air", "Mean air of the solved field", 62.1, "°C", "INFO"),
            AnalysisMetric("maximum_solid_temperature", "Maximum solid temperature", 170.9, "°C",
                           "NOT_USABLE"),
        ], limitations=["Solid and air maxima are not usable at this mesh size."])
        document = render_campaign_html(CampaignResult.from_results([thermal, cfd]))
        holds = document.split("<h3>What holds")[1].split("<h3>")[0]
        self.assertIn("Internal air: 61.2 °C by the thermal analysis&#x27;s enclosure balance, "
                      "62.1 °C as the mean of the CFD&#x27;s solved air, 0.9 K apart.", holds)
        domains = document.split('id="domains"')[1]
        self.assertIn("Not to be read as results: Maximum solid temperature. Solid and air maxima "
                      "are not usable at this mesh size.", domains)

    def test_a_pass_resting_on_missing_inputs_says_so(self):
        # P02 Rev E, 2026-10-01 22:29: "AC passes: 3 of 3 rails within their
        # target impedance", two of them on the default source (AC-006); DC's
        # "14 of 14 rail drops" left out three rails without a load.
        ac = AnalysisResult("AC", "AC Impedance", status=AnalysisStatus.PASS, findings=[
            AnalysisFinding(rule_id="AC-006", category="MODEL_INPUT", severity=FindingSeverity.INFO,
                            title=f"{rail} source impedance left at its default", description="d")
            for rail in ("/+5V_RAIL", "/+5V_U4")
        ])
        dc = AnalysisResult("DC", "DC Power", status=AnalysisStatus.PASS, metrics=[
            AnalysisMetric("rail_1_drop", "/+5V_RAIL drop", 0.315, "%", "PASS"),
            AnalysisMetric("rail_9_drop", "/J2_PP5V drop", 0.0, "%", "NO_DATA"),
        ])
        document = render_campaign_html(CampaignResult.from_results([ac, dc]))
        holds = document.split("<h3>What holds")[1].split("<h3>")[0]
        self.assertIn("<strong>AC</strong> passes &mdash; rests on 2 input(s) still to complete",
                      holds)
        self.assertIn("1 of 1 rail drops within the limit &middot; 1 rail(s) without a load, "
                      "not judged", holds)


class ComponentViewTests(unittest.TestCase):
    def test_a_part_reads_its_heat_temperatures_and_findings_in_one_row(self):
        # Audit R-LE-8, P02 Rev E 2026-10-01: Q10 is in SW-003's hot loop and
        # U14 has the hottest junction, 25.67 K under its 150 C limit.
        thermal = AnalysisResult("THERMAL", "3D Thermal", status=AnalysisStatus.WARN, summary={
            "components": [
                {"reference": "U14", "power_w": 1.24, "board_c": 105.1, "junction_c": 124.33,
                 "max_junction_c": 150.0, "margin_k": 25.67},
                {"reference": "Q10", "power_w": 1.06, "board_c": 118.99, "junction_c": 120.1,
                 "max_junction_c": 175.0, "margin_k": 54.9},
            ],
            "placement_temperatures_c": {"C37": 96.0},
        })
        emc = AnalysisResult("EMC", "EMC", status=AnalysisStatus.WARN, findings=[AnalysisFinding(
            rule_id="SW-003", category="SWITCHING", severity=FindingSeverity.HIGH,
            title="Switching hot loop is spread out", description="d", components=["C37", "Q10", "Q11"],
        )])
        document = render_campaign_html(CampaignResult.from_results([thermal, emc]))
        view = document.split('id="components"')[1].split("</section>")[0]
        rows = re.findall(r"<tr><th>([^<]+)</th>", view.split("<tbody>")[1])
        self.assertEqual(rows, ["Q10", "C37", "Q11", "U14"])
        self.assertIn("Spread-out switching hot loop", view)
        self.assertIn("124&nbsp;°C / 150&nbsp;°C", view)
        self.assertIn("<td>96&nbsp;°C</td>", view)

    def test_a_finding_about_many_parts_fills_only_the_room_left(self):
        # P02 Rev E: at 16:51 cited parts pushed U14 (124 C of 150) out; at
        # 18:31 TE-002's 13 capacitors pushed out DC-008's C88 and R10.
        thermal = AnalysisResult("THERMAL", "3D Thermal", status=AnalysisStatus.PASS, summary={
            "components": [{"reference": "U14", "power_w": 1.08, "board_c": 102.0, "junction_c": 124.33,
                            "max_junction_c": 150.0, "margin_k": 25.67}],
            "placement_temperatures_c": {f"C{index}": 80.0 + index for index in range(1, 31)},
        })
        survey = [f"C{index}" for index in range(1, 31)]
        dc = AnalysisResult("DC", "DC", status=AnalysisStatus.WARN, findings=[AnalysisFinding(
            rule_id="DC-008", category="CONNECTIVITY", severity=FindingSeverity.HIGH,
            title="GND pads are cut off from the rest of the net", description="d",
            nets=["GND"], components=["C88", "R10"],
        )])
        emc = AnalysisResult("EMC", "EMC", status=AnalysisStatus.WARN, findings=[AnalysisFinding(
            rule_id="TE-002", category="THERMAL", severity=FindingSeverity.HIGH,
            title="Capacitors above their dielectric's rated temperature", description="d",
            components=survey,
        )])
        document = render_campaign_html(CampaignResult.from_results([thermal, dc, emc]))
        view = document.split('id="components"')[1].split("</section>")[0]
        rows = re.findall(r"<tr><th>([^<]+)</th>", view.split("<tbody>")[1])
        self.assertEqual(rows[:4], ["C88", "R10", "U14", "C30"])
        self.assertEqual(len(rows), 20)


class FrenchReportTests(unittest.TestCase):
    def tearDown(self):
        i18n.configure("en")

    def test_the_decision_and_the_guide_follow_the_interface_language(self):
        # Audit of 2026-10-01 (LE-1): the report stayed English under a French
        # interface. P02 Rev E's DC-008 on C88 and R10.
        campaign = campaign_with([AnalysisFinding(
            rule_id="DC-008", category="CONNECTIVITY", severity=FindingSeverity.HIGH,
            title="GND pads are cut off from the rest of the net", description="d",
            nets=["GND"], components=["C88", "R10"],
        )])
        i18n.configure("fr")
        document = render_campaign_html(campaign)
        self.assertIn('<html lang="fr">', document)
        self.assertIn("À corriger avant fabrication (1)", document)
        self.assertIn("Une pastille isolée du reste de son réseau", document)
        self.assertIn("C88, R10 &middot; réseau GND", document)


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
