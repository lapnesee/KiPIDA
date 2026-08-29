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
                reference_epsilon_r=4.2, trustworthy=True,
            )],
        )

    def test_width_recommendation_predicts_target(self):
        result = self._result()
        recommendation = DifferentialRecommendationEngine(self.settings).recommend_pair(result)[0]
        self.assertEqual(recommendation.action, "ADJUST_WIDTH")
        self.assertGreaterEqual(recommendation.recommended_width_mm, self.settings.minimum_width_mm)
        self.assertAlmostEqual(recommendation.predicted_impedance_ohm, 90.0, places=1)

    def test_passing_pair_keeps_geometry(self):
        result = self._result(impedance=91.0)
        recommendation = DifferentialRecommendationEngine(self.settings).recommend_pair(result)[0]
        self.assertEqual(recommendation.action, "KEEP_GEOMETRY")
        self.assertEqual(recommendation.feasibility, "PASS")

    def test_missing_reference_plane_blocks_geometry_tuning(self):
        result = self._result(impedance=0.0, topology="UNREFERENCED")
        recommendation = DifferentialRecommendationEngine(self.settings).recommend_pair(result)[0]
        self.assertEqual(recommendation.action, "RESTORE_GND_REFERENCE")
        self.assertEqual(recommendation.feasibility, "BLOCKED")


if __name__ == "__main__":
    unittest.main()
