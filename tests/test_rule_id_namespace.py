"""A rule identifier belongs to one domain.

``DC-001`` meant "copper island with no source" in the DC analysis and "far
decoupling capacitor" in the EMC analysis; ``DC-002`` meant a DC solver that
did not converge and a missing decoupling capacitor; ``TH-001`` meant a
junction over its limit and a hot EMC component. A report filtered or exported
by rule, and a campaign action identified by its rules, could not tell them
apart.

The identifiers are read from the syntax tree rather than the text, because the
adapters write ``_finding(`` on one line and the identifier on the next.
"""

import ast
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULE_ID = re.compile(r"^[A-Z]{2,6}(?:-[A-Z]+)?-\d{3}$")
FINDING_BUILDERS = {"_add", "_finding", "rule"}
DOMAIN_WORDS = {
    "emc": "EMC", "ac": "AC", "differential": "DIFFERENTIAL", "thermal": "THERMAL",
    "cfd": "CFD", "dc": "DC", "schematic": "SCHEMATIC",
}
# Where a module's findings all belong to one domain, whatever function built them.
MODULE_DOMAINS = {
    "emc_analyzer.py": "EMC",
    os.path.join("rules", "schematic_rules.py"): "SCHEMATIC",
}
SKIPPED_DIRECTORIES = {".git", ".venv", ".runtime", ".claude", "__pycache__", "tests", "validation", "tools"}


def _domain_from_functions(stack):
    for name in reversed(stack):
        for word in name.lower().split("_"):
            if word in DOMAIN_WORDS:
                return DOMAIN_WORDS[word]
    return None


def _rule_ids_by_domain():
    found = {}
    for directory, subdirectories, files in os.walk(ROOT):
        subdirectories[:] = [name for name in subdirectories if name not in SKIPPED_DIRECTORIES]
        for file_name in files:
            if not file_name.endswith(".py"):
                continue
            path = os.path.join(directory, file_name)
            relative = os.path.relpath(path, ROOT)
            with open(path, "rb") as handle:
                tree = ast.parse(handle.read().decode("utf-8-sig"), filename=relative)
            _collect(tree, relative, [], found)
    return found


def _collect(node, relative, stack, found):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        stack = stack + [node.name]
    if isinstance(node, ast.Call):
        identifier = _literal_rule_id(node)
        if identifier:
            domain = (
                MODULE_DOMAINS.get(relative)
                or _domain_from_functions(stack)
                or relative
            )
            found.setdefault(identifier, set()).add(domain)
    for child in ast.iter_child_nodes(node):
        _collect(child, relative, stack, found)


def _literal_rule_id(call):
    candidates = []
    name = getattr(call.func, "attr", None) or getattr(call.func, "id", None)
    if name in FINDING_BUILDERS and call.args:
        candidates.append(call.args[0])
    candidates.extend(keyword.value for keyword in call.keywords if keyword.arg == "rule_id")
    for candidate in candidates:
        if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
            if RULE_ID.match(candidate.value):
                return candidate.value
    return None


class RuleIdNamespaceTest(unittest.TestCase):
    def test_no_rule_id_is_shared_between_domains(self):
        by_domain = _rule_ids_by_domain()
        shared = {
            identifier: sorted(domains)
            for identifier, domains in by_domain.items() if len(domains) > 1
        }
        self.assertEqual(shared, {}, f"Rule identifiers used by several domains: {shared}")

    def test_the_scan_sees_the_multiline_adapter_findings(self):
        # A scan that missed the adapters would pass trivially.
        by_domain = _rule_ids_by_domain()
        self.assertEqual(by_domain.get("DC-003"), {"DC"})
        self.assertEqual(by_domain.get("AC-001"), {"AC"})
        self.assertIn("EMC", by_domain.get("GP-001", set()))


if __name__ == "__main__":
    unittest.main()
