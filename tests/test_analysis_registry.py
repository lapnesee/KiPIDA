import unittest

from analysis_registry import AnalysisDescriptor, AnalysisRegistry, DEFAULT_ANALYSES


class AnalysisRegistryTests(unittest.TestCase):
    def test_default_tools_have_stable_navigation_groups(self):
        groups = DEFAULT_ANALYSES.grouped()
        self.assertEqual(
            list(groups), ["Power Integrity", "Signal Integrity", "EMI / EMC", "Thermal", "Application"],
        )
        self.assertEqual([item.analysis_id for item in groups["Power Integrity"]], ["DC", "AC"])
        self.assertEqual([item.analysis_id for item in groups["Thermal"]], ["THERMAL", "CFD"])

    def test_duplicate_analysis_id_is_rejected(self):
        descriptor = AnalysisDescriptor("TEST", "Test", "Application", 1)
        registry = AnalysisRegistry([descriptor])
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(descriptor)

    def test_ids_are_normalized_at_lookup_boundary(self):
        self.assertEqual(DEFAULT_ANALYSES.get("differential").title, "Differential Pairs")

    def test_descriptor_requires_uppercase_id(self):
        with self.assertRaisesRegex(ValueError, "uppercase"):
            AnalysisDescriptor("dc", "DC", "Power", 1)


if __name__ == "__main__":
    unittest.main()
