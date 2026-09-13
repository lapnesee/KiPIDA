"""The hybrid DC engine solves the saved board under the raster engine's contract.

A result from either engine is read by the same adapters, plots, thermal and
CFD couplings, so the hybrid one must carry the same keys and live-board layer
ids -- and, being analytic on tracks, it must reproduce a hand calculation.
"""

import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

try:
    import numpy  # noqa: F401
    import scipy  # noqa: F401
    import shapely  # noqa: F401
    _DEPS = True
except ImportError:  # pragma: no cover - the plugin requires them
    _DEPS = False


TWO_LAYER_ROUTE = """\
(kicad_pcb
    (version 20260206)
    (generator "pcbnew")
    (general (thickness 1.6))
    (layers
        (0 "F.Cu" signal "TOP")
        (2 "B.Cu" signal)
        (25 "Edge.Cuts" user)
    )
    (setup
        (stackup
            (layer "F.Cu" (type "copper") (thickness 0.035))
            (layer "dielectric 1" (type "core") (thickness 1.53))
            (layer "B.Cu" (type "copper") (thickness 0.035))
        )
    )
    (footprint "Package:U" (layer "F.Cu") (uuid "u1") (at 10 10 0)
        (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS") (uuid "u1r"))
        (pad "1" smd rect (at 0 0 0) (size PAD PAD) (layers "F.Cu" "F.Mask")
            (net "VCC") (pintype "power_out") (uuid "u1p1"))
    )
    (footprint "Package:R" (layer "B.Cu") (uuid "r1") (at 30 10 0)
        (property "Reference" "R1" (at 0 0 0) (layer "B.SilkS") (uuid "r1r"))
        (pad "1" smd rect (at 0 0 0) (size PAD PAD) (layers "B.Cu" "B.Mask")
            (net "VCC") (pintype "passive") (uuid "r1p1"))
    )
    (segment (start 10 10) (end 20 10) (width 0.5) (layer "F.Cu") (net "VCC") (uuid "s1"))
    (via (at 20 10) (size VIA) (drill 0.3) (layers "F.Cu" "B.Cu") (net "VCC") (uuid "v1"))
    (segment (start 20 10) (end 30 10) (width 0.5) (layer "B.Cu") (net "VCC") (uuid "s2"))
)
"""

# What KiCad's IPC stackup reports for the same board: its own layer enum, and
# the designer's name where one was given.
LIVE_STACKUP = {
    "copper": {
        3: {"name": "TOP", "thickness_mm": 0.035},
        34: {"name": "B.Cu", "thickness_mm": 0.035},
    },
    "layer_order": [3, 34],
    "substrate": [{"thickness_mm": 1.53, "between": [3, 34]}],
    "trustworthy": True,
    "warnings": [],
}


def _rail(current_a=1.0):
    from models import ComponentRef, PowerRail, UnifiedLoad, UnifiedSource

    return PowerRail(
        "VCC", 5.0,
        sources=[UnifiedSource(ComponentRef("U1"), ["1"])],
        loads=[UnifiedLoad(ComponentRef("R1"), current_a, ["1"])],
    )


def _request(board_path, rails, stackup=LIVE_STACKUP, grid=0.1):
    from application.dc_controller import DCBoardSnapshot, DCRunRequest

    return DCRunRequest(
        rails=tuple(rails), geometry_by_rail={}, classification_geometry_by_rail={},
        stackup=stackup, board=DCBoardSnapshot((), ()), grid_size_mm=grid,
        board_path=None if board_path is None else str(board_path),
    )


def _solve(engine, request):
    logs = []
    results = engine.solve(request, logs.append, lambda *_: None, lambda: False)
    return results, logs


def _write_board(pad_mm, via_mm):
    handle = tempfile.NamedTemporaryFile(
        suffix=".kicad_pcb", mode="w", encoding="utf-8", delete=False,
    )
    handle.write(TWO_LAYER_ROUTE.replace("PAD", str(pad_mm)).replace("VIA", str(via_mm)))
    handle.close()
    return Path(handle.name)


def _route_resistance(track_mm):
    from ingest.track_resistance import via_resistance
    from physical_constants import COPPER_RESISTIVITY_OHM_M, PLATING_THICKNESS_FALLBACK_MM

    track = COPPER_RESISTIVITY_OHM_M * track_mm * 1e-3 / (0.5e-3 * 0.035e-3)
    return track + via_resistance(1.6, 0.3, PLATING_THICKNESS_FALLBACK_MM)


@unittest.skipUnless(_DEPS, "numpy/scipy/shapely not available")
class HybridEngineOnARoute(unittest.TestCase):
    def setUp(self):
        self.path = _write_board(pad_mm=1, via_mm=0.6)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_without_copper_lands_the_drop_is_the_hand_calculation(self):
        from application.dc_controller import HybridDCSolverEngine

        bare = _write_board(pad_mm=0, via_mm=0)
        try:
            results, _logs = _solve(HybridDCSolverEngine(), _request(bare, [_rail(1.0)]))
        finally:
            bare.unlink(missing_ok=True)
        expected = 1.0 * _route_resistance(20.0)
        data = results["VCC"]
        self.assertAlmostEqual(data["load_drop_v"], expected, delta=1e-6 * expected)
        self.assertAlmostEqual(data["source_voltage_v"], 5.0)

    def test_lands_shorten_the_track_only_where_they_cover_it(self):
        # The pads (1 mm) and via land (0.6 mm) lie over 0.5 + 0.3 mm of each
        # track: their copper parallels it there, and at best shorts it.
        from application.dc_controller import HybridDCSolverEngine

        results, _logs = _solve(HybridDCSolverEngine(), _request(self.path, [_rail(1.0)]))
        drop = results["VCC"]["load_drop_v"]
        self.assertLess(drop, _route_resistance(20.0))
        self.assertGreater(drop, _route_resistance(20.0 - 2 * (0.5 + 0.3)))

    def test_the_result_carries_the_raster_contract(self):
        from application.dc_controller import HybridDCSolverEngine

        results, _logs = _solve(HybridDCSolverEngine(), _request(self.path, [_rail()]))
        self.assertTrue({
            "mesh", "results", "stats", "sources", "loads", "detailed_result",
            "current_density", "compute_metadata", "grid_size_mm",
            "requested_grid_size_mm", "adaptive_grid", "load_drop_v",
            "source_voltage_v", "engine",
        } <= set(results["VCC"]))
        self.assertEqual(results["VCC"]["engine"], "hybrid")
        self.assertTrue(getattr(results["VCC"]["detailed_result"], "valid", False))

    def test_layers_are_published_under_the_live_board_ids(self):
        from application.dc_controller import HybridDCSolverEngine

        results, _logs = _solve(HybridDCSolverEngine(), _request(self.path, [_rail()]))
        mesh = results["VCC"]["mesh"]
        self.assertEqual({coord[2] for coord in mesh.node_coords.values()}, {3, 34})
        self.assertEqual({key[2] for key in mesh.node_map}, {3, 34})

    def test_density_uses_the_track_section_and_the_barrel(self):
        from application.dc_controller import HybridDCSolverEngine

        results, _logs = _solve(HybridDCSolverEngine(), _request(self.path, [_rail(1.0)]))
        density = results["VCC"]["current_density"]
        self.assertAlmostEqual(density.maximum_track_a_per_mm2, 1.0 / (0.5 * 0.035), places=6)
        self.assertAlmostEqual(density.maximum_via_current_a, 1.0, places=9)

    def test_a_run_without_a_board_path_is_refused(self):
        from application.dc_controller import HybridDCSolverEngine

        with self.assertRaisesRegex(ValueError, "saved board file"):
            _solve(HybridDCSolverEngine(), _request(None, [_rail()]))


class LiveLayerPairing(unittest.TestCase):
    @staticmethod
    def _file_stackup(*names):
        from ingest.board_reader import CopperLayer, Stackup

        ids = {"F.Cu": 0, "In1.Cu": 4, "In2.Cu": 6, "B.Cu": 2}
        layers = [
            CopperLayer(ids[name.split("|")[0]], name.split("|")[0], "signal", 0.035,
                        user_name=(name.split("|") + [""])[1])
            for name in names
        ]
        return Stackup(layers, 1.6, len(layers))

    def test_names_pair_layers_including_designer_names(self):
        from application.dc_controller import live_layer_ids

        live = {"copper": {3: {"name": "F.Cu"}, 4: {"name": "GND"}, 34: {"name": "B.Cu"}},
                "layer_order": [3, 4, 34]}
        mapping, positional, unmatched = live_layer_ids(
            self._file_stackup("F.Cu", "In1.Cu|GND", "B.Cu"), live,
        )
        self.assertEqual(mapping, {0: 3, 4: 4, 2: 34})
        self.assertEqual((positional, unmatched), ([], []))

    def test_equal_stacks_pair_unnamed_layers_by_position(self):
        from application.dc_controller import live_layer_ids

        live = {"copper": {3: {"name": "F.Cu"}, 4: {"name": "Plane"}, 34: {"name": "B.Cu"}},
                "layer_order": [3, 4, 34]}
        mapping, positional, unmatched = live_layer_ids(
            self._file_stackup("F.Cu", "In1.Cu", "B.Cu"), live,
        )
        self.assertEqual(mapping[4], 4)
        self.assertEqual((positional, unmatched), (["In1.Cu"], []))

    def test_a_layer_the_live_board_lacks_is_reported(self):
        from application.dc_controller import live_layer_ids

        live = {"copper": {0: {"name": "F.Cu"}, 31: {"name": "B.Cu"}}, "layer_order": [0, 31]}
        mapping, positional, unmatched = live_layer_ids(
            self._file_stackup("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"), live,
        )
        self.assertEqual(mapping, {0: 0, 2: 31})
        self.assertEqual((positional, unmatched), ([], ["In1.Cu", "In2.Cu"]))


class RegulatorCascade(unittest.TestCase):
    """A rail skipped for want of copper still passes its regulators' demand up."""

    def test_a_skipped_middle_rail_keeps_its_downstream_demand(self):
        from application.dc_controller import (
            DCBoardSnapshot, DCFootprintSnapshot, DCNetSnapshot, DCPadSnapshot,
            DCPointSnapshot, DCRunRequest, DCSolverEngine,
        )
        from mesh import Mesh
        from models import (
            ComponentRef, DCSolveResult, PowerRail, UnifiedLoad, UnifiedSource,
            VoltageRegulator,
        )

        # VIN (12 V) -LDO1-> MID (5 V) -LDO2-> OUT (3.3 V); MID has no copper.
        ldo1 = VoltageRegulator("LDO1", "VIN", "U1", ["1"], "MID", "U1", ["2"])
        ldo2 = VoltageRegulator("LDO2", "MID", "U2", ["1"], "OUT", "U2", ["2"])
        vin = PowerRail("VIN", 12.0, sources=[UnifiedSource(ComponentRef("J1"), ["1"])],
                        child_regulators=[ldo1])
        mid = PowerRail("MID", 5.0, loads=[UnifiedLoad(ComponentRef("R1"), 0.2, ["1"])],
                        child_regulators=[ldo2])
        out = PowerRail("OUT", 3.3, loads=[UnifiedLoad(ComponentRef("R2"), 0.5, ["1"])])

        origin = DCPointSnapshot(0.0, 0.0)
        pads = tuple(DCPadSnapshot(number, number, origin, DCNetSnapshot("")) for number in ("1", "2"))
        board = DCBoardSnapshot(tuple(
            DCFootprintSnapshot(ref, pads) for ref in ("J1", "U1", "U2", "R1", "R2")
        ), ())

        class OneNodeMesher:
            def __init__(self, *_args, **_kwargs):
                pass

            def generate_mesh(self, *_args, **_kwargs):
                mesh = Mesh()
                mesh.nodes = [0]
                mesh.node_coords = {0: (0.0, 0.0, 0)}
                mesh.node_map = {(0, 0, 0): 0}
                mesh.grid_step = 1.0
                return mesh

        seen = {}

        class RecordingSolver:
            last_compute = None

            def __init__(self, *_args, **_kwargs):
                pass

            def solve_detailed(self, mesh, sources, loads):
                seen[len(seen)] = loads
                return DCSolveResult(voltages={0: 12.0})

        request = DCRunRequest(
            rails=(vin, mid, out),
            geometry_by_rail={"VIN": {0: object()}, "OUT": {0: object()}},
            classification_geometry_by_rail={}, stackup={}, board=board,
            grid_size_mm=1.0,
        )
        engine = DCSolverEngine(mesher_factory=OneNodeMesher, solver_factory=RecordingSolver)
        results, _logs = _solve(engine, request)
        self.assertEqual(set(results), {"VIN", "OUT"})
        vin_loads = seen[max(seen)]
        # LDO1 carries MID's own 0.2 A plus LDO2's 0.5 A input.
        self.assertAlmostEqual(sum(item["current"] for item in vin_loads), 0.7)


if __name__ == "__main__":
    unittest.main()
