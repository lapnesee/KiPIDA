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

import analysis_adapters as analysis_adapters_module
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


class EveryDCPathAttachesRemediationsTests(unittest.TestCase):
    """Guards the wiring itself, on both paths that produce a DC result.

    The audit found four packages that were complete, tested and never
    called. The advisor was one; it is now called from adapt_dc_run, which is
    the single entry point precisely so a second caller cannot quietly skip
    the sizing and publish a result whose actions all read "No structured
    remediation was computed".
    """

    def test_adapt_dc_run_sizes_the_fixes_it_adapts(self):
        from analysis_adapters import adapt_dc_run

        calls = []
        original = analysis_adapters_module.attach_dc_remediations
        analysis_adapters_module.attach_dc_remediations = (
            lambda *args, **kwargs: calls.append((args, kwargs)) or 0
        )
        self.addCleanup(
            setattr, analysis_adapters_module, "attach_dc_remediations", original,
        )

        adapt_dc_run({}, 3.0, board_path="board.kicad_pcb", rails=[_Rail("+5V", 5.0)])

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][1], "board.kicad_pcb")

    def test_an_advisor_that_raises_costs_the_fixes_not_the_result(self):
        from analysis_adapters import adapt_dc_run

        original = analysis_adapters_module.attach_dc_remediations

        def _explode(*args, **kwargs):
            raise RuntimeError("mesh blew up")

        analysis_adapters_module.attach_dc_remediations = _explode
        self.addCleanup(
            setattr, analysis_adapters_module, "attach_dc_remediations", original,
        )

        result = adapt_dc_run({}, 3.0, board_path="board.kicad_pcb", rails=[])

        self.assertEqual(result.analysis_type, "DC")
        self.assertTrue(any("not sized" in item for item in result.limitations),
                        "the report must say the fixes are missing")

    def test_the_dialogs_dc_publish_path_goes_through_it(self):
        with open(os.path.join(_root, "ui", "main_dialog.py"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("adapt_dc_run(", text)
        self.assertLess(
            text.index("adapt_dc_run("), text.index('"DC", format_dc_report'),
            "the DC result must be adapted before it is published",
        )

    def test_the_campaigns_dc_adapter_goes_through_it(self):
        # A batch reports from the campaign's own results, so a campaign
        # adapter calling adapt_dc_result directly would drop every sized fix
        # from the one report the batch exists to produce.
        from application.campaign_controller import DEFAULT_ADAPTERS

        calls = []
        original = analysis_adapters_module.adapt_dc_run
        analysis_adapters_module.adapt_dc_run = (
            lambda *args, **kwargs: calls.append((args, kwargs))
        )
        self.addCleanup(
            setattr, analysis_adapters_module, "adapt_dc_run", original,
        )

        class _Request:
            maximum_drop_pct = 3.0
            board_path = "board.kicad_pcb"
            rails = ()

        DEFAULT_ADAPTERS["DC"]({}, _Request())
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
