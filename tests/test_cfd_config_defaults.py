"""An empty config must load the dataclass defaults, not a copy of them.

config_manager restated every CFD solver default as a literal, which made it a
second source of truth. Raising max_iterations from 250 to 1000 in models.py
therefore changed nothing for any project without a saved CFD section -- which
is every project that has not opened the CFD panel.

The symptom was indistinguishable from a stale deployment: the enclosure solver
kept stopping at exactly 250 iterations while the UI plainly carried newer
code, and it was chased as a deployment problem before the duplicate default
was found.

Comparing whole dataclasses rather than one field is the point: a test that
checked only max_iterations would have passed while the other ten defaults
drifted.
"""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from config_manager import _dict_to_cfd_settings  # noqa: E402
from models import CFDSolverSettings, EnclosureCFDSettings, FluidProperties  # noqa: E402


class EmptyConfigUsesDataclassDefaults(unittest.TestCase):
    def test_every_solver_default_comes_from_the_dataclass(self):
        self.assertEqual(_dict_to_cfd_settings({}).solver, CFDSolverSettings())

    def test_every_fluid_default_comes_from_the_dataclass(self):
        self.assertEqual(_dict_to_cfd_settings({}).fluid, FluidProperties())

    def test_ambient_matches_too(self):
        self.assertEqual(
            _dict_to_cfd_settings({}).ambient_c, EnclosureCFDSettings().ambient_c,
        )

    def test_a_saved_value_still_wins_over_the_default(self):
        # The defaults must not become unconditional: a project that did tune
        # the solver keeps its own numbers.
        loaded = _dict_to_cfd_settings({"solver": {"max_iterations": 42}})
        self.assertEqual(loaded.solver.max_iterations, 42)
        # ...and the untouched fields still follow the dataclass.
        self.assertEqual(loaded.solver.tolerance, CFDSolverSettings().tolerance)


if __name__ == "__main__":
    unittest.main()
