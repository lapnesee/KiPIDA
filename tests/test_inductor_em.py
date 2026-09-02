import unittest
from types import SimpleNamespace

from emc_analyzer import EMCFootprint
from inductor_em import (
    apply_catalog, buck_ripple_current_pp, resolve_inductor_models,
    triangular_harmonic_peak, TargetedInductorRefiner,
)
from models import EMCInductorModel, EMCSignalSource


class InductorEMTests(unittest.TestCase):
    def test_buck_ripple_matches_ccm_equation(self):
        ripple = buck_ripple_current_pp(12.0, 5.0, 2.2e-6, 600e3)
        self.assertAlmostEqual(ripple, 2.2096, places=3)

    def test_triangular_harmonics_are_deterministic_and_decay(self):
        first = triangular_harmonic_peak(2.0, 1, 0.42)
        fifth = triangular_harmonic_peak(2.0, 5, 0.42)
        self.assertGreater(first, fifth)
        self.assertEqual(first, triangular_harmonic_peak(2.0, 1, 0.42))

    def test_catalog_does_not_invent_shield_attenuation(self):
        model = apply_catalog(EMCInductorModel("L1", mpn="SPM6530T-2R2M"))
        self.assertEqual(model.shield_state, "SHIELDED")
        self.assertAlmostEqual(model.inductance_h, 2.2e-6)
        self.assertIsNone(model.shielding_attenuation_db)
        self.assertIn("MISSING_COMPLEX_MATERIAL", TargetedInductorRefiner.status(model))

    def test_resolver_links_power_tree_source_and_live_placement(self):
        output = SimpleNamespace(net_name="5V", nominal_voltage=5.0, child_regulators=[])
        regulator = SimpleNamespace(
            reg_type="SWITCHING", output_ref_des="L1", output_rail_name="5V",
            input_ref_des="U4", loss_model={
                "controller_ref_des": "U4", "switching_frequency_hz": {"value": 600e3},
            },
        )
        input_rail = SimpleNamespace(
            net_name="12V", nominal_voltage=12.0, child_regulators=[regulator],
        )
        footprint = EMCFootprint(
            "L1", "2.2uH", (5.0, 5.0), ("U4_SW", "5V"),
            (("U4_SW", 4.0, 5.0), ("5V", 6.0, 5.0)),
        )
        source = EMCSignalSource(
            "U4_SW", "U4_SW", "SWITCHING", 600e3, 10.0, current_a=4.0,
        )
        configured = [EMCInductorModel("L1", mpn="SPM6530T-2R2M")]
        models = resolve_inductor_models(
            configured, [footprint], [input_rail, output], [source],
        )
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].source_name, "U4_SW")
        self.assertGreater(models[0].ripple_current_pp_a, 2.0)
        self.assertEqual(models[0].parameter_confidence, "HIGH")


if __name__ == "__main__":
    unittest.main()
