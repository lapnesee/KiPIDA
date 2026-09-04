"""Voltage-drop findings must carry a sized fix, not an empty section.

Every action in the consolidated report read "No structured remediation was
computed": advisor/ was written, tested, and called by nothing. These tests
pin the connection rather than the advisor's arithmetic, which
tests/test_dc_advisor.py already covers.
"""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from analysis_adapters import attach_dc_remediations
from analysis_contract import (
    AnalysisFinding, AnalysisResult, EvidenceConfidence, FindingSeverity,
)


def _drop_finding(net="+5V_RAIL"):
    return AnalysisFinding(
        rule_id="DC-003", category="VOLTAGE_DROP", severity=FindingSeverity.HIGH,
        title=f"{net} exceeds the voltage-drop target", description="synthetic",
        confidence=EvidenceConfidence.ESTIMATED, nets=[net],
    )


def _result(*findings):
    result = AnalysisResult(analysis_type="DC", title="DC Power")
    result.findings.extend(findings)
    return result


class _Rail:
    def __init__(self, net_name, nominal_voltage):
        self.net_name = net_name
        self.nominal_voltage = nominal_voltage
        self.sources = []
        self.loads = []


class AdvisorWiringTests(unittest.TestCase):
    def test_no_board_path_is_a_no_op(self):
        result = _result(_drop_finding())
        self.assertEqual(attach_dc_remediations(result, "", [_Rail("+5V_RAIL", 5.0)], 3.0), 0)
        self.assertEqual(result.findings[0].remediations, [])

    def test_an_unreadable_board_does_not_lose_the_dc_result(self):
        # The advisor is an addition to the DC result, never a risk to it.
        result = _result(_drop_finding())
        attached = attach_dc_remediations(
            result, "does-not-exist.kicad_pcb", [_Rail("+5V_RAIL", 5.0)], 3.0,
        )
        self.assertEqual(attached, 0)
        self.assertEqual(len(result.findings), 1)

    def test_only_voltage_drop_findings_are_considered(self):
        # DC-002 is a non-convergence fact; widening copper does not address it.
        other = AnalysisFinding(
            rule_id="DC-002", category="NUMERICS", severity=FindingSeverity.HIGH,
            title="solve did not converge", description="synthetic",
            confidence=EvidenceConfidence.DETERMINISTIC, nets=["+5V_RAIL"],
        )
        result = _result(other)
        attach_dc_remediations(result, "", [_Rail("+5V_RAIL", 5.0)], 3.0)
        self.assertEqual(other.remediations, [])

    def test_a_rail_without_a_nominal_voltage_is_skipped(self):
        # target_drop_v is a fraction of the nominal; without one there is no
        # target to size against, and inventing one would be a fabricated goal.
        result = _result(_drop_finding())
        self.assertEqual(
            attach_dc_remediations(result, "board.kicad_pcb", [_Rail("+5V_RAIL", 0.0)], 3.0),
            0,
        )


class MainDialogCallsItTests(unittest.TestCase):
    def test_the_dc_publish_path_attaches_remediations(self):
        # Guards the wiring itself: the audit found four packages that were
        # complete, tested and never called.
        source = (
            os.path.join(_root, "ui", "main_dialog.py")
        )
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("attach_dc_remediations", text)
        self.assertLess(
            text.index("attach_dc_remediations("), text.index('"DC", format_dc_report'),
            "remediations must be attached before the result is published",
        )


if __name__ == "__main__":
    unittest.main()
