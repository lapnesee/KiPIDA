"""Every domain loader must return the dataclass defaults for an empty config.

config_manager restates each field's default as a literal, which makes it a
second source of truth. For the CFD solver there was a third, in the panel's
widget values, and the three disagreed: raising max_iterations from 250 to 1000
in models.py was corrected once in the loader and still produced no visible
change, because the panel box said 250 and get_settings() reads the widgets.

Three correct fixes, no observable difference, and a deployment blamed for it
across seven exchanges. That is what this file exists to prevent.

Whole dataclasses are compared, not selected fields. A test that checked only
max_iterations would have passed throughout while the other ten drifted -- and
the CFD case survived precisely because nothing compared the whole structure.
"""

import dataclasses
import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

import config_manager  # noqa: E402
from models import (  # noqa: E402
    ACAnalysisSettings, DifferentialAnalysisSettings, EMCAnalysisSettings,
    EnclosureCFDSettings, ThermalAnalysisSettings,
)

LOADERS = (
    ("AC", config_manager._dict_to_ac_settings, ACAnalysisSettings),
    ("differential", config_manager._dict_to_differential_settings,
     DifferentialAnalysisSettings),
    ("EMC", config_manager._dict_to_emc_settings, EMCAnalysisSettings),
    ("thermal", config_manager._dict_to_thermal_settings, ThermalAnalysisSettings),
    ("CFD", config_manager._dict_to_cfd_settings, EnclosureCFDSettings),
)


def _drift(loaded, fresh):
    """Field paths where the loaded value differs from the dataclass default.

    Nested dataclasses are walked one level, because that is where the CFD
    defaults lived -- `solver.max_iterations`, not a top-level field. Lists are
    skipped: an empty config has no rails, patches or components, and their
    absence is not drift.
    """
    differences = []
    for field in dataclasses.fields(fresh):
        got = getattr(loaded, field.name, None)
        want = getattr(fresh, field.name)
        if dataclasses.is_dataclass(want) and dataclasses.is_dataclass(got):
            for inner in dataclasses.fields(want):
                if getattr(got, inner.name) != getattr(want, inner.name):
                    differences.append(
                        f"{field.name}.{inner.name}: "
                        f"{getattr(got, inner.name)!r} != {getattr(want, inner.name)!r}"
                    )
        elif isinstance(want, (list, tuple, dict)):
            continue
        elif got != want:
            differences.append(f"{field.name}: {got!r} != {want!r}")
    return differences


class LoadersHonourTheDataclassDefaults(unittest.TestCase):
    def test_no_domain_loader_has_drifted(self):
        for name, loader, klass in LOADERS:
            with self.subTest(domain=name):
                differences = _drift(loader({}), klass())
                self.assertEqual(
                    differences, [],
                    f"{name} config loader restates defaults that no longer match "
                    f"{klass.__name__}. Take the fallback from the dataclass "
                    "instead of retyping the value.",
                )

    def test_a_saved_value_still_overrides_the_default(self):
        # The guard must not turn into "defaults always win": a project that
        # tuned a value keeps it.
        loaded = config_manager._dict_to_cfd_settings(
            {"solver": {"max_iterations": 42}},
        )
        self.assertEqual(loaded.solver.max_iterations, 42)


class ThePanelAgreesWithTheDataclass(unittest.TestCase):
    """The third source of truth, and the one that hid the other two fixes."""

    def setUp(self):
        try:
            import wx
        except ImportError:  # pragma: no cover
            self.skipTest("wxPython not available")
        if not hasattr(wx, "App") or not callable(getattr(wx, "Frame", None)):
            self.skipTest("wx is stubbed by another test module")
        self.app = wx.App(False)
        self.frame = wx.Frame(None)

    def tearDown(self):
        self.frame.Destroy()

    def test_the_cfd_panel_starts_at_the_dataclass_defaults(self):
        from ui.cfd_analysis_panel import CFDAnalysisPanel

        panel = CFDAnalysisPanel(self.frame)
        self.assertEqual(_drift(panel.get_settings(), EnclosureCFDSettings()), [])

    def test_the_thermal_panel_starts_at_the_dataclass_defaults(self):
        from ui.thermal_analysis_panel import ThermalAnalysisPanel

        panel = ThermalAnalysisPanel(self.frame, lambda: [])
        self.assertEqual(_drift(panel.get_settings(), ThermalAnalysisSettings()), [])


if __name__ == "__main__":
    unittest.main()
