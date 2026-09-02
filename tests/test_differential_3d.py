import unittest

from differential_3d import Targeted3DRefiner
from models import DifferentialAnalysisSettings, DifferentialSectionResult


class Targeted3DRefinerTests(unittest.TestCase):
    def _section(self, impedance=110.0, coverage=100.0, length=10.0):
        return DifferentialSectionResult(
            layer_id=0, layer_name="B.Cu", length_mm=length, width_mm=0.11,
            gap_mm=0.18, topology="COPLANAR_MICROSTRIP",
            differential_impedance_ohm=impedance, reference_coverage_pct=coverage,
        )

    def test_only_problem_sections_are_selected(self):
        refiner = Targeted3DRefiner(DifferentialAnalysisSettings())
        self.assertEqual(refiner.select(self._section(92.0), 90.0), "")
        self.assertIn("impedance error", refiner.select(self._section(110.0), 90.0))

    def test_refinement_is_bounded_and_traceable(self):
        refined = Targeted3DRefiner(DifferentialAnalysisSettings()).refine(
            self._section(110.0, coverage=80.0, length=1.0), 90.0
        )
        self.assertEqual(refined.status, "REFINED_3D_QS")
        self.assertGreater(refined.impedance_ohm, 90.0)
        self.assertLess(refined.impedance_ohm, 110.0)


if __name__ == "__main__":
    unittest.main()
