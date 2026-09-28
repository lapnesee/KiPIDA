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

    def test_the_board_solved_is_the_one_captured_not_the_one_saved_since(self):
        # P02 Rev E was saved at 15:37 during a campaign captured at 15:29: the
        # DC domain solved the earlier file, the coupled thermal's DC the later.
        from dataclasses import replace

        from application.dc_controller import HybridDCSolverEngine

        captured = TWO_LAYER_ROUTE.replace("PAD", "0").replace("VIA", "0")
        request = replace(_request(self.path, [_rail(1.0)]), board_file_text=captured)
        results, _logs = _solve(HybridDCSolverEngine(), request)
        expected = 1.0 * _route_resistance(20.0)   # the bare route, not the landed one on disk
        self.assertAlmostEqual(results["VCC"]["load_drop_v"], expected, delta=1e-6 * expected)

    def test_a_run_without_a_board_path_is_refused(self):
        from application.dc_controller import HybridDCSolverEngine

        with self.assertRaisesRegex(ValueError, "saved board file"):
            _solve(HybridDCSolverEngine(), _request(None, [_rail()]))

    def test_the_grid_check_refines_only_a_drop_that_can_decide_the_verdict(self):
        # P02 Rev E: +5V_U4 read 40.8 mV at 0.1 mm, 49.7 mV at 0.05 mm, and
        # 39.1 mV at 0.2 mm -- a coarser check would have agreed with the run.
        from dataclasses import replace

        from application.dc_controller import HybridDCSolverEngine

        request = replace(_request(self.path, [_rail(1.0)]), grid_sensitivity_check=True)
        # The route drops about 20 mV of 5 V: over a tenth of a 1% budget...
        results, _logs = _solve(HybridDCSolverEngine(), replace(request, maximum_drop_pct=1.0))
        check = results["VCC"]["grid_sensitivity"]
        self.assertAlmostEqual(check["check_grid_mm"], check["grid_mm"] / 2.0)
        self.assertAlmostEqual(check["drop_v"], results["VCC"]["load_drop_v"])
        self.assertLess(check["delta_pct"], 1.0)  # tracks are analytic
        # ...and under a tenth of a 5% one, where no finer solve is spent.
        results, logs = _solve(HybridDCSolverEngine(), replace(request, maximum_drop_pct=5.0))
        self.assertNotIn("delta_pct", results["VCC"]["grid_sensitivity"])
        self.assertTrue(any("not refined" in line for line in logs))


@unittest.skipUnless(_DEPS, "numpy/scipy/shapely not available")
class OneEngineForEveryAnalysis(unittest.TestCase):
    """Plan step 5.5: the saved file, when known, is the copper every reader gets."""

    def setUp(self):
        self.path = _write_board(pad_mm=1, via_mm=0.6)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    class _Recording:
        def __init__(self, name):
            self.name, self.calls = name, 0

        def solve(self, request, emit, progress, cancelled):
            self.calls += 1
            return {"VCC": {"engine": self.name}}

    def test_a_known_saved_file_is_solved_by_the_hybrid_engine(self):
        from application.dc_controller import DefaultDCSolverEngine

        hybrid, raster = self._Recording("hybrid"), self._Recording("raster")
        results, _logs = _solve(DefaultDCSolverEngine(hybrid, raster), _request(self.path, [_rail()]))
        self.assertEqual((hybrid.calls, raster.calls), (1, 0))
        self.assertEqual(results["VCC"]["engine"], "hybrid")

    def test_without_a_file_the_raster_engine_runs_and_the_log_says_why(self):
        from application.dc_controller import DefaultDCSolverEngine

        hybrid, raster = self._Recording("hybrid"), self._Recording("raster")
        _results, logs = _solve(DefaultDCSolverEngine(hybrid, raster), _request(None, [_rail()]))
        self.assertEqual((hybrid.calls, raster.calls), (0, 1))
        self.assertTrue(any("No saved board file" in line and "raster" in line for line in logs))

    def test_an_unreadable_file_falls_back_to_the_raster_engine(self):
        from application.dc_controller import DefaultDCSolverEngine, HybridDCSolverEngine

        def unreadable(_path):
            raise ValueError("Cannot parse: no root node found")

        raster = self._Recording("raster")
        engine = DefaultDCSolverEngine(HybridDCSolverEngine(board_reader=unreadable), raster)
        results, logs = _solve(engine, _request(self.path, [_rail()]))
        self.assertEqual((raster.calls, results["VCC"]["engine"]), (1, "raster"))
        self.assertTrue(any("could not be read" in line for line in logs))

    def test_every_consumer_defaults_to_the_one_engine(self):
        import inspect

        from application.campaign_controller import default_domain_engines
        from application.cfd_controller import CFDSolverEngine
        from application.dc_controller import DCAnalysisController, DefaultDCSolverEngine
        from application.thermal_controller import ThermalSolverEngine

        self.assertIsInstance(default_domain_engines()["DC"], DefaultDCSolverEngine)
        for consumer in (ThermalSolverEngine, CFDSolverEngine):
            factory = inspect.signature(consumer.__init__).parameters["dc_engine_factory"].default
            self.assertIs(factory, DefaultDCSolverEngine)
        self.assertIsInstance(DCAnalysisController()._engine, DefaultDCSolverEngine)

    def test_dc_003_and_the_advisor_baseline_report_the_same_drop(self):
        # The plan's acceptance for the switch: the fix is sized against the
        # very drop the finding reports.
        from advisor.dc_advisor import baseline_solution
        from application.dc_controller import HybridDCSolverEngine
        from ingest.board_reader import read_board

        results, _logs = _solve(HybridDCSolverEngine(), _request(self.path, [_rail(2.0)]))
        data = results["VCC"]
        _mesh, _voltages, advisor_drop = baseline_solution(
            read_board(self.path), "VCC", "", data["sources"], data["loads"],
            grid_step_mm=data["requested_grid_size_mm"],
        )
        self.assertAlmostEqual(advisor_drop, data["load_drop_v"], delta=1e-6 * data["load_drop_v"])

    def test_the_advisor_sizes_on_the_results_own_sources_and_loads(self):
        from unittest import mock

        import analysis_adapters
        from application.dc_controller import HybridDCSolverEngine

        rail = _rail(2.0)
        results, _logs = _solve(HybridDCSolverEngine(), _request(self.path, [rail]))
        with mock.patch("advisor.dc_advisor.build_dc_remediations", return_value=[]) as sizing:
            result = analysis_adapters.adapt_dc_run(
                results, 0.1, board_path=str(self.path), rails=[rail],
            )
        self.assertIn("DC-003", {finding.rule_id for finding in result.findings})
        kwargs = sizing.call_args.kwargs
        self.assertEqual(kwargs["sources"], results["VCC"]["sources"])
        self.assertEqual(kwargs["loads"], results["VCC"]["loads"])
        self.assertEqual(kwargs["grid_step_mm"], results["VCC"]["requested_grid_size_mm"])

    def test_an_edited_open_board_raises_dc_004(self):
        from dataclasses import replace

        import analysis_adapters
        from application.dc_controller import HybridDCSolverEngine
        from application.dc_controller import DCNetSnapshot, DCPointSnapshot, DCViaSnapshot

        # The open board's via sits 0.5 mm from where the saved file has it.
        moved = DCPointSnapshot(20.5e6, 10e6)
        via = DCViaSnapshot(DCNetSnapshot("VCC"), moved, moved, 0.6e6, layers=(3, 34))
        request = replace(_request(self.path, [_rail()]), board=replace(
            _request(self.path, [_rail()]).board, vias=(via,),
        ))
        results, _logs = _solve(HybridDCSolverEngine(), request)
        self.assertEqual(
            results["VCC"]["saved_board_differences"],
            ["1 via(s) only on the open board and 1 only in the saved file"],
        )
        finding = next(
            item for item in analysis_adapters.adapt_dc_result(results, 50.0).findings
            if item.rule_id == "DC-004"
        )
        self.assertEqual(str(finding.confidence.value if hasattr(finding.confidence, "value") else finding.confidence), "DETERMINISTIC")
        self.assertIn("Save the board", finding.recommendation)

    def test_an_unchanged_open_board_raises_nothing(self):
        import analysis_adapters
        from dataclasses import replace

        from application.dc_controller import HybridDCSolverEngine
        from application.dc_controller import DCNetSnapshot, DCPointSnapshot, DCViaSnapshot

        same = DCPointSnapshot(20e6, 10e6)
        via = DCViaSnapshot(DCNetSnapshot("VCC"), same, same, 0.6e6, layers=(3, 34))
        base = _request(self.path, [_rail()])
        results, _logs = _solve(HybridDCSolverEngine(), replace(base, board=replace(base.board, vias=(via,))))
        self.assertEqual(results["VCC"]["saved_board_differences"], [])
        self.assertNotIn("DC-004", {item.rule_id for item in analysis_adapters.adapt_dc_result(results, 50.0).findings})

    def test_a_pad_on_a_half_micrometre_is_not_called_moved(self):
        # P02 Rev E: D2 pad 4 sits at x = 169.3075 mm, 169307500 nm on the
        # open board and 169.30749999999998 once the file reader places it.
        # Rounded to the micrometre the two fell apart, and DC-004 reported
        # ten untouched pads as moved.
        from types import SimpleNamespace

        from application.dc_controller import (
            DCBoardSnapshot, DCFootprintSnapshot, DCNetSnapshot, DCPadSnapshot, DCPointSnapshot,
            saved_board_differences,
        )

        live = DCPadSnapshot("4", "4", DCPointSnapshot(169307500, 78390000), DCNetSnapshot("VCC"))
        request = SimpleNamespace(
            board=DCBoardSnapshot((DCFootprintSnapshot("D2", (live,)),), ()),
            classification_geometry_by_rail={},
        )
        saved = SimpleNamespace(vias=[], footprints=[SimpleNamespace(reference="D2", pads=[
            SimpleNamespace(number="4", net_name="VCC", position=(169.30749999999998, 78.39)),
        ])])
        self.assertEqual(saved_board_differences(saved, "VCC", request, {}), [])

    def test_an_empty_live_capture_says_so_instead_of_100_percent_differs(self):
        # P02 Rev E, 2026-09-22 21:39 (report campaign-...-213933): the PCB
        # editor was still synchronising, IPC returned every collection
        # empty, and the old code compared that to the saved file and
        # reported 17 rails "differs ... (100% of it)" -- none of them a
        # real edit.
        from application.dc_controller import DCBoardSnapshot, saved_board_differences

        request = SimpleNamespace(
            board=DCBoardSnapshot((), ()), classification_geometry_by_rail={},
        )
        saved = SimpleNamespace(vias=[], footprints=[])
        self.assertEqual(
            saved_board_differences(saved, "VCC", request, {}),
            ["live capture empty, retry after sync"],
        )


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
        # DC-3 solves upstream-first (VIN, then OUT) rather than in the
        # downstream-first order _sorted_rails uses for current aggregation,
        # so VIN's call is found by its own load ref_des (LDO1's input, U1)
        # rather than by position.
        vin_loads = next(loads for loads in seen.values() if any(item["ref_des"] == "U1" for item in loads))
        # LDO1 carries MID's own 0.2 A plus LDO2's 0.5 A input.
        self.assertAlmostEqual(sum(item["current"] for item in vin_loads), 0.7)


if __name__ == "__main__":
    unittest.main()
