"""A finding must not claim a confidence its basis does not support.

The consolidated report on the real board printed
"J2_ESD_D: Estimate  [DETERMINISTIC]" -- the deliverable contradicting itself
on the project's central rule. _finding() hardcoded DETERMINISTIC for every
adapter, so a swept impedance, a meshed temperature and an observed
non-convergence all carried the same badge.
"""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from analysis_contract import EvidenceConfidence, FindingSeverity


class FindingConfidenceTests(unittest.TestCase):
    def test_a_model_output_defaults_to_estimated(self):
        from analysis_adapters import _finding

        finding = _finding(
            "DC-003", 1, "VOLTAGE_DROP", FindingSeverity.HIGH,
            "rail exceeds the target", "computed from the solved mesh",
        )
        self.assertEqual(finding.confidence, EvidenceConfidence.ESTIMATED)

    def test_an_observed_fact_can_still_claim_deterministic(self):
        from analysis_adapters import _finding

        finding = _finding(
            "DC-002", 1, "NUMERICS", FindingSeverity.HIGH,
            "solve did not converge", "observed, not modelled",
            confidence=EvidenceConfidence.DETERMINISTIC,
        )
        self.assertEqual(finding.confidence, EvidenceConfidence.DETERMINISTIC)

    def test_a_differential_estimate_is_not_labelled_deterministic(self):
        # The exact contradiction seen in the report: a section the solver
        # itself calls an estimate must not be badged as measured fact.
        from analysis_adapters import adapt_differential_result

        class _Section:
            status = "Estimate"
            trustworthy = False
            warnings = ["Trace separation exceeds the coupled-pair limit."]
            layer_name = "F.Cu"
            length_mm = 5.0

        class _Pair:
            signature = "J2_ESD_D"
            name = "J2_ESD_D"
            positive_net = "J2_ESD_D+"
            negative_net = "J2_ESD_D-"
            target_impedance_ohm = 90.0

        class _Result:
            pair = _Pair()
            sections = [_Section()]
            status = "Estimate"
            weighted_impedance_ohm = 82.19
            minimum_impedance_ohm = 82.19
            maximum_impedance_ohm = 82.19
            error_pct = 8.7
            estimated_skew_ps = 0.0
            skew_limit_ps = 25.0
            length_mismatch_mm = 0.0
            trustworthy = False
            warnings = []
            recommendations = []

        result = adapt_differential_result([_Result()], None, 10.0)
        for finding in result.findings:
            if "Estimate" in finding.title:
                self.assertNotEqual(
                    finding.confidence, EvidenceConfidence.DETERMINISTIC,
                    f"{finding.title!r} claims DETERMINISTIC while calling itself an estimate",
                )


if __name__ == "__main__":
    unittest.main()
