"""Regression: a via drill arrives as a Vector2, not a scalar.

kipy models DrillProperties.diameter as a Vector2 ("may also be a milled slot
with different X and Y dimensions"). Code that did float(value) or value / 1e6
on it raised TypeError on every real board that has vias:

    DC preparation failed: float() argument must be a string or a real
        number, not 'Vector2'
    AC Analysis Error: unsupported operand type(s) for /: 'Vector2' and 'float'

Observed on p02_alimentation (463 vias) with Zeo/KiCad 9.99.
"""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


class FakeVector2:
    """Stands in for kipy.geometry.Vector2: has x/y, is not a number."""

    def __init__(self, x, y=None):
        self.x = x
        self.y = x if y is None else y


class FakeDrillProperties:
    def __init__(self, diameter):
        self.diameter = diameter


class FakePadStack:
    def __init__(self, drill):
        self.drill = drill


class FakeVia:
    """Only the attributes kipy's Via actually exposes."""

    def __init__(self, drill_vector):
        self.padstack = FakePadStack(FakeDrillProperties(drill_vector))


class ScalarDimensionTests(unittest.TestCase):
    def setUp(self):
        try:
            import numpy, shapely, matplotlib  # noqa: F401
        except ImportError:
            self.skipTest("mesh dependencies not available")

    def test_round_drill_vector_reduces_to_its_diameter(self):
        from mesh import scalar_dimension

        self.assertEqual(scalar_dimension(FakeVector2(300000)), 300000.0)

    def test_slot_drill_reduces_to_the_mean_of_both_axes(self):
        from mesh import scalar_dimension

        # A milled slot: the circular-barrel models downstream need one number.
        self.assertEqual(scalar_dimension(FakeVector2(200000, 400000)), 300000.0)

    def test_plain_number_passes_through(self):
        from mesh import scalar_dimension

        self.assertEqual(scalar_dimension(600000), 600000.0)
        self.assertIsNone(scalar_dimension(None))

    def test_to_mm_of_a_vector_drill_no_longer_raises(self):
        # The exact AC failure: to_mm(Vector2) -> Vector2 / 1e6 -> TypeError.
        from mesh import scalar_dimension, to_mm

        self.assertAlmostEqual(to_mm(scalar_dimension(FakeVector2(300000))), 0.3)


class DrillSnapshotTests(unittest.TestCase):
    def test_vector_diameter_keeps_both_axes(self):
        # The exact DC failure: float(Vector2) -> TypeError.
        from application.dc_controller import _drill_snapshot

        snapshot = _drill_snapshot(FakeVia(FakeVector2(200000, 400000)))
        self.assertIsNotNone(snapshot)
        self.assertEqual((snapshot.x, snapshot.y), (200000.0, 400000.0))

    def test_round_vector_diameter(self):
        from application.dc_controller import _drill_snapshot

        snapshot = _drill_snapshot(FakeVia(FakeVector2(300000)))
        self.assertEqual((snapshot.x, snapshot.y), (300000.0, 300000.0))

    def test_scalar_diameter_still_supported(self):
        # Older API shape must keep working.
        from application.dc_controller import _drill_snapshot

        snapshot = _drill_snapshot(FakeVia(300000))
        self.assertEqual((snapshot.x, snapshot.y), (300000.0, 300000.0))


if __name__ == "__main__":
    unittest.main()
