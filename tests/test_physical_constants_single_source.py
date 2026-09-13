"""A physical constant is written once, in physical_constants.py.

The DC analysis modelled copper at 1.68e-8 ohm-m while the DC advisor used
1.72e-8, so a voltage-drop finding and the fix sized for it disagreed by 2.4 %
before any geometry was considered. A plated hole with no parsed plating was
25 um in two engines and 20 um in a third. Each copy looked reasonable where it
stood, which is how they drifted.

This reads the number tokens of every production module -- comments and
docstrings are not tokens, so explaining a value stays allowed -- and fails
when one of these values is written out anywhere but the constants module.
"""

import io
import os
import tokenize
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIPPED_DIRECTORIES = {
    ".git", ".venv", ".runtime", ".claude", "__pycache__", "tests", "validation", "tools",
}
DEFINITION = os.path.join(ROOT, "physical_constants.py")

# Values that mean only one thing in this code base, wherever they appear.
UNAMBIGUOUS = {
    1.68e-5: "copper resistivity (ohm-mm)",
    1.7e-5: "copper resistivity (ohm-mm)",
    1.72e-5: "copper resistivity (ohm-mm)",
    1.68e-8: "copper resistivity (ohm-m)",
    1.72e-8: "copper resistivity (ohm-m)",
    1.7241e-8: "copper resistivity (ohm-m)",
    0.00393: "copper temperature coefficient",
    385.0: "copper thermal conductivity",
    0.0348: "copper thickness per ounce",
}

# Values that are one of the constants only in the context the line names.
CONTEXTUAL = {
    0.035: ("thick", "copper"),
    0.025: ("plating",),
    0.02: ("plating",),
    1.53: ("thick", "dielectric"),
    1.6: ("thick", "height"),
}


def _production_modules():
    for directory, subdirectories, files in os.walk(ROOT):
        subdirectories[:] = [name for name in subdirectories if name not in SKIPPED_DIRECTORIES]
        for name in files:
            path = os.path.join(directory, name)
            if name.endswith(".py") and path != DEFINITION:
                yield path


def _copies(path):
    with open(path, "rb") as handle:
        source = handle.read().decode("utf-8")
    found = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.NUMBER:
            continue
        try:
            value = float(token.string)
        except ValueError:
            continue
        line = token.line.lower()
        if value in UNAMBIGUOUS:
            found.append((token.start[0], token.string, UNAMBIGUOUS[value]))
        elif value in CONTEXTUAL and any(word in line for word in CONTEXTUAL[value]):
            found.append((token.start[0], token.string, "fallback " + "/".join(CONTEXTUAL[value])))
    return found


class PhysicalConstantsSingleSourceTest(unittest.TestCase):
    def test_no_module_writes_out_a_shared_constant(self):
        copies = [
            f"{os.path.relpath(path, ROOT)}:{line}: {text} ({meaning})"
            for path in _production_modules()
            for line, text, meaning in _copies(path)
        ]
        self.assertEqual(
            copies, [],
            "Import these from physical_constants instead:\n" + "\n".join(copies),
        )

    def test_the_engines_share_one_resistivity(self):
        # The two meshers used to disagree by 2.4 %; both now read the module.
        from ingest.track_resistance import RHO_COPPER
        from physical_constants import COPPER_RESISTIVITY_OHM_M, COPPER_RESISTIVITY_OHM_MM

        self.assertEqual(RHO_COPPER, COPPER_RESISTIVITY_OHM_M)
        self.assertAlmostEqual(COPPER_RESISTIVITY_OHM_MM, COPPER_RESISTIVITY_OHM_M * 1e3, places=20)


if __name__ == "__main__":
    unittest.main()
