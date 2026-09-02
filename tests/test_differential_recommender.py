import unittest

from differential_recommender import DifferentialRecommendationEngine
from models import (
    DifferentialAnalysisSettings, DifferentialPairCandidate,
    DifferentialPairResult, DifferentialSectionResult,
)


class DifferentialRecommendationTests(unittest.TestCase):
    def setUp(self):
        self.pair = DifferentialPairCandidate("USB", "USB_DP", "USB_DM", target_impedance_ohm=90.0)
        self.settings = DifferentialAnalysisSettings(
            minimum_width_mm=0.09, minimum_gap_mm=0.09, target_tolerance_pct=5.0,
        )

    def _result(self, impedance=110.0, topology="MICROSTRIP"):
        return DifferentialPairResult(
            pair=self.pair, weighted_impedance_ohm=impedance,
            error_pct=100.0 * (impedance - 90.0) / 90.0, trustworthy=True,
            sections=[DifferentialSectionResult(
                layer_id=0, layer_name="F.Cu", length_mm=20.0,
                width_mm=0.18, gap_mm=0.20, topology=topology,
                differential_impedance_ohm=impedance, copper_thickness_mm=0.035,
                reference_distance_mm=0.20, reference_below_distance_mm=0.20,
                reference_epsilon_r=4.2, reference_coverage_pct=100.0,
                trustworthy=True,
            )],
        )

    def test_width_recommendation_predicts_target(self):
        result = self._result()
        recommendation = DifferentialRecommendationEngine(self.settings).recommend_pair(result)[0]
        self.assertEqual(recommendation.action, "SET_PRIMARY_ROUTE")
        self.assertGreaterEqual(recommendation.recommended_width_mm, self.settings.minimum_width_mm)
        self.assertAlmostEqual(
            recommendation.recommended_width_mm / 0.005,
            round(recommendation.recommended_width_mm / 0.005),
        )
        self.assertAlmostEqual(recommendation.predicted_impedance_ohm, 90.0, delta=0.6)

    def test_passing_pair_keeps_geometry(self):
        result = self._result(impedance=91.0)
        engine = DifferentialRecommendationEngine(self.settings)
        result.pair.target_impedance_ohm = engine._predict(
            result.sections[0], result.sections[0].width_mm, result.sections[0].gap_mm
        )
        recommendation = engine.recommend_pair(result)[0]
        self.assertEqual(recommendation.action, "KEEP_PRIMARY_ROUTE")
        self.assertEqual(recommendation.feasibility, "PASS")

    def test_missing_reference_plane_blocks_geometry_tuning(self):
        result = self._result(impedance=0.0, topology="UNREFERENCED")
        recommendation = DifferentialRecommendationEngine(self.settings).recommend_pair(result)[0]
        self.assertEqual(recommendation.action, "RESTORE_GND_REFERENCE")
        self.assertEqual(recommendation.feasibility, "BLOCKED")

    def test_nonuniform_gap_blocks_a_global_width_recommendation(self):
        result = self._result()
        result.sections.append(DifferentialSectionResult(
            layer_id=0, layer_name="F.Cu", length_mm=25.0,
            width_mm=0.18, gap_mm=1.80, topology="MICROSTRIP",
            differential_impedance_ohm=115.0, copper_thickness_mm=0.035,
            reference_distance_mm=0.20, reference_below_distance_mm=0.20,
            reference_epsilon_r=4.2, reference_coverage_pct=100.0,
            trustworthy=True,
        ))
        recommendations = DifferentialRecommendationEngine(self.settings).recommend_pair(result)
        self.assertEqual(len(recommendations), 1)
        self.assertEqual(recommendations[0].action, "REROUTE_PAIR")
        self.assertEqual(recommendations[0].feasibility, "REVIEW")
        self.assertAlmostEqual(recommendations[0].recommended_gap_mm, 0.20)
        self.assertLess(recommendations[0].recommended_width_mm, 0.30)
        self.assertTrue(any("Normalize" in warning for warning in recommendations[0].warnings))

    def test_unreferenced_section_blocks_global_geometry_tuning(self):
        result = self._result()
        result.sections.append(DifferentialSectionResult(
            layer_id=0, layer_name="F.Cu", length_mm=1.0,
            width_mm=0.18, gap_mm=0.20, topology="UNREFERENCED",
            differential_impedance_ohm=0.0, copper_thickness_mm=0.035,
        ))
        recommendations = DifferentialRecommendationEngine(self.settings).recommend_pair(result)
        self.assertEqual(recommendations[0].action, "SET_PRIMARY_ROUTE")
        self.assertEqual(recommendations[0].feasibility, "AFTER_REFERENCE_FIX")
        self.assertEqual(recommendations[1].action, "RESTORE_GND_REFERENCE")
        self.assertEqual(recommendations[1].feasibility, "BLOCKED")

    def test_breakout_gap_is_not_compensated_with_a_second_width(self):
        result = self._result(impedance=102.0)
        result.sections[0].gap_mm = 0.18
        result.sections[0].length_mm = 20.0
        result.sections.append(DifferentialSectionResult(
            layer_id=0, layer_name="F.Cu", length_mm=2.0,
            width_mm=0.18, gap_mm=1.80, topology="MICROSTRIP",
            differential_impedance_ohm=115.0, copper_thickness_mm=0.035,
            reference_distance_mm=0.20, reference_below_distance_mm=0.20,
            reference_epsilon_r=4.2, reference_coverage_pct=100.0,
            trustworthy=True,
        ))
        recommendations = DifferentialRecommendationEngine(self.settings).recommend_pair(result)
        self.assertEqual(len(recommendations), 1)
        self.assertAlmostEqual(recommendations[0].recommended_gap_mm, 0.18)
        self.assertNotEqual(recommendations[0].recommended_gap_mm, 1.80)

    def test_coplanar_primary_rule_uses_configured_ground_gap(self):
        self.settings.geometry_mode = "JLCPCB_COPLANAR"
        self.settings.coplanar_ground_gap_mm = 0.20
        result = self._result(impedance=100.0, topology="COPLANAR_MICROSTRIP")
        result.sections[0].geometry_mode = "JLCPCB_COPLANAR"
        result.sections[0].width_mm = 0.11
        result.sections[0].gap_mm = 0.18
        result.sections[0].ground_clearance_mm = 0.274
        recommendation = DifferentialRecommendationEngine(
            self.settings
        ).recommend_pair(result)[0]
        self.assertAlmostEqual(recommendation.recommended_ground_clearance_mm, 0.20)
        self.assertAlmostEqual(recommendation.recommended_gap_mm, 0.18)

    def test_fabrication_floor_does_not_rewrite_measured_geometry(self):
        self.settings.minimum_width_mm = 0.13
        self.settings.minimum_gap_mm = 0.13
        self.settings.minimum_ground_clearance_mm = 0.20
        result = self._result(impedance=100.0)
        result.sections[0].width_mm = 0.11
        result.sections[0].gap_mm = 0.18
        recommendation = DifferentialRecommendationEngine(
            self.settings
        ).recommend_pair(result)[0]
        self.assertAlmostEqual(recommendation.current_width_mm, 0.11)
        self.assertAlmostEqual(recommendation.current_gap_mm, 0.18)
        self.assertGreaterEqual(recommendation.recommended_width_mm, 0.13)
        self.assertGreaterEqual(recommendation.recommended_gap_mm, 0.13)
        self.assertGreaterEqual(recommendation.recommended_ground_clearance_mm, 0.20)
        self.assertEqual(recommendation.action, "SET_PRIMARY_ROUTE")

    def test_low_primary_coverage_requests_reroute(self):
        result = self._result(impedance=100.0)
        result.sections[0].length_mm = 0.1
        result.sections[0].gap_mm = 0.18
        result.sections.append(DifferentialSectionResult(
            layer_id=0, layer_name="F.Cu", length_mm=2.0,
            width_mm=0.18, gap_mm=1.8, topology="MICROSTRIP",
            differential_impedance_ohm=115.0, copper_thickness_mm=0.035,
            reference_distance_mm=0.20, reference_below_distance_mm=0.20,
            reference_epsilon_r=4.2, reference_coverage_pct=100.0,
            trustworthy=True,
        ))
        recommendation = DifferentialRecommendationEngine(
            self.settings
        ).recommend_pair(result)[0]
        self.assertEqual(recommendation.action, "REROUTE_PAIR")
        self.assertEqual(recommendation.feasibility, "REVIEW")

    def test_reroute_decision_cannot_leave_pair_status_pass(self):
        result = self._result(impedance=90.0)
        result.status = "PASS"
        result.sections[0].length_mm = 0.1
        result.sections[0].gap_mm = 0.18
        result.sections.append(DifferentialSectionResult(
            layer_id=0, layer_name="F.Cu", length_mm=2.0,
            width_mm=0.18, gap_mm=1.8, topology="MICROSTRIP",
            differential_impedance_ohm=98.0, copper_thickness_mm=0.035,
            reference_distance_mm=0.20, reference_below_distance_mm=0.20,
            reference_epsilon_r=4.2, reference_coverage_pct=100.0,
            trustworthy=True,
        ))
        DifferentialRecommendationEngine(self.settings).recommend([result])
        self.assertEqual(result.recommendations[0].action, "REROUTE_PAIR")
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(any("Routing qualification failed" in item for item in result.warnings))

    def test_unreferenced_length_counts_against_primary_coverage(self):
        result = self._result(impedance=100.0)
        result.sections[0].length_mm = 1.0
        result.sections.append(DifferentialSectionResult(
            layer_id=0, layer_name="F.Cu", length_mm=1.2,
            width_mm=0.18, gap_mm=0.20, topology="UNREFERENCED",
            differential_impedance_ohm=0.0, copper_thickness_mm=0.035,
        ))
        recommendations = DifferentialRecommendationEngine(
            self.settings
        ).recommend_pair(result)
        self.assertEqual(recommendations[0].action, "REROUTE_PAIR")
        self.assertEqual(recommendations[0].feasibility, "AFTER_REFERENCE_FIX")
        self.assertTrue(any(
            "45.5%" in warning for warning in recommendations[0].warnings
        ))


if __name__ == "__main__":
    unittest.main()
