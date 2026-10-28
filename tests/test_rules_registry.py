import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from rules.registry import RuleRegistry, RuleDescriptor, RuleRegistration, rule, DEFAULT_REGISTRY


class TestRuleRegistry(unittest.TestCase):

    def test_register_and_get(self):
        registry = RuleRegistry()
        descriptor = RuleDescriptor("X-001", "TEST", "Title", "Desc", "LOW")
        registry.register(RuleRegistration(descriptor, lambda ctx: []))
        self.assertIs(registry.get("X-001").descriptor, descriptor)
        self.assertIsNone(registry.get("missing"))

    def test_by_domain_filters(self):
        registry = RuleRegistry()
        registry.register(RuleRegistration(RuleDescriptor("A-1", "ALPHA", "t", "d", "LOW"), lambda ctx: []))
        registry.register(RuleRegistration(RuleDescriptor("B-1", "BETA", "t", "d", "LOW"), lambda ctx: []))
        self.assertEqual([r.descriptor.rule_id for r in registry.by_domain("ALPHA")], ["A-1"])
        self.assertEqual(len(registry.all()), 2)

    def test_evaluate_domain_aggregates_findings(self):
        registry = RuleRegistry()
        registry.register(RuleRegistration(
            RuleDescriptor("A-1", "ALPHA", "t", "d", "LOW"), lambda ctx: ["f1"]
        ))
        registry.register(RuleRegistration(
            RuleDescriptor("A-2", "ALPHA", "t", "d", "LOW"), lambda ctx: ["f2", "f3"]
        ))
        result = registry.evaluate_domain("ALPHA", context=None)
        self.assertEqual(sorted(result), ["f1", "f2", "f3"])

    def test_one_broken_rule_does_not_abort_others(self):
        registry = RuleRegistry()

        def _raises(ctx):
            raise RuntimeError("boom")

        registry.register(RuleRegistration(RuleDescriptor("A-1", "ALPHA", "t", "d", "LOW"), _raises))
        registry.register(RuleRegistration(
            RuleDescriptor("A-2", "ALPHA", "t", "d", "LOW"), lambda ctx: ["ok"]
        ))
        result = registry.evaluate_domain("ALPHA", context=None)
        self.assertEqual(result, ["ok"])

    def test_rule_decorator_registers_into_default_registry(self):
        before = len(DEFAULT_REGISTRY.all())

        @rule("Z-999", "ZTEST", "t", "d", "LOW")
        def _dummy(ctx):
            return []

        self.assertEqual(len(DEFAULT_REGISTRY.all()), before + 1)
        self.assertIsNotNone(DEFAULT_REGISTRY.get("Z-999"))


if __name__ == "__main__":
    unittest.main()
