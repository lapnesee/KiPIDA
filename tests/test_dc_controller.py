import threading
from types import SimpleNamespace
import unittest

from application.dc_controller import (
    DCAnalysisCancelled,
    DCAnalysisController,
    DCBoardSnapshot,
    DCControllerCallbacks,
    DCFootprintSnapshot,
    DCNetSnapshot,
    DCPadSnapshot,
    DCPointSnapshot,
    _mesh_nodes,
    _propagated_pass_voltages,
    _rail_injections,
    capture_dc_board,
)
from models import ComponentRef, PowerRail, VoltageRegulator


class FakeEngine:
    def solve(self, request, emit, progress, cancelled):
        emit("worker started")
        progress(1, 1, "done")
        if cancelled():
            raise DCAnalysisCancelled("DC analysis cancelled.")
        return {"VCC": "result"}


class BlockingEngine:
    release = threading.Event()

    def solve(self, request, emit, progress, cancelled):
        self.release.wait(2.0)
        if cancelled():
            raise DCAnalysisCancelled("DC analysis cancelled.")
        return {}


class DCControllerTests(unittest.TestCase):
    @staticmethod
    def callbacks(completed, errors, progress=None, logs=None):
        return DCControllerCallbacks(
            on_progress=lambda *args: (progress if progress is not None else []).append(args),
            on_complete=completed.append,
            on_error=errors.append,
            on_log=lambda message: (logs if logs is not None else []).append(message),
        )

    def test_analysis_runs_off_caller_thread_and_reports_progress(self):
        completed, errors, progress, logs, callback_threads = [], [], [], [], []
        caller = threading.get_ident()
        callbacks = DCControllerCallbacks(
            on_progress=lambda *args: (progress.append(args), callback_threads.append(threading.get_ident())),
            on_complete=lambda result: (completed.append(result), callback_threads.append(threading.get_ident())),
            on_error=errors.append,
            on_log=logs.append,
        )
        controller = DCAnalysisController(engine=FakeEngine())
        controller.start("request", callbacks)
        self.assertTrue(controller.wait(2.0))
        self.assertEqual(completed, [{"VCC": "result"}])
        self.assertEqual(progress, [(1, 1, "done")])
        self.assertEqual(logs, ["worker started"])
        self.assertFalse(errors)
        self.assertTrue(all(thread_id != caller for thread_id in callback_threads))

    def test_concurrent_start_is_rejected_and_cancel_is_typed(self):
        BlockingEngine.release.clear()
        controller = DCAnalysisController(engine=BlockingEngine())
        completed, errors = [], []
        controller.start("request", self.callbacks(completed, errors))
        with self.assertRaisesRegex(RuntimeError, "already running"):
            controller.start("other", self.callbacks([], []))
        self.assertTrue(controller.cancel())
        BlockingEngine.release.set()
        self.assertTrue(controller.wait(2.0))
        self.assertFalse(completed)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], DCAnalysisCancelled)

    def test_capture_detaches_only_plain_board_data(self):
        point = SimpleNamespace(x=12_000_000, y=5_000_000)
        net = SimpleNamespace(name="VCC")
        pad = SimpleNamespace(
            number="1", name="1", position=point, net=net, pad_type=1,
            type="THROUGH_HOLE", drill_size=SimpleNamespace(x=300_000, y=300_000),
            layers=[0, 31],
        )
        footprint = SimpleNamespace(reference="J1", pads=[pad])
        via = SimpleNamespace(
            start=point, net=net, width=600_000,
            drill_size=SimpleNamespace(x=300_000, y=300_000), layers=[0, 31],
        )
        snapshot = capture_dc_board(SimpleNamespace(footprints=[footprint], vias=[via]))
        self.assertEqual(snapshot.footprints[0].reference, "J1")
        self.assertEqual(snapshot.footprints[0].pads[0].net.name, "VCC")
        self.assertEqual(snapshot.vias[0].layers, (0, 31))
        self.assertEqual(snapshot.vias[0].position, DCPointSnapshot(12_000_000, 5_000_000))
        self.assertEqual(snapshot.vias[0].drill_size, DCPointSnapshot(300_000, 300_000))

    def test_engine_errors_cross_the_controller_boundary(self):
        class BrokenEngine:
            def solve(self, request, emit, progress, cancelled):
                raise ValueError("bad snapshot")

        completed, errors = [], []
        controller = DCAnalysisController(engine=BrokenEngine())
        controller.start("request", self.callbacks(completed, errors))
        self.assertTrue(controller.wait(2.0))
        self.assertFalse(completed)
        self.assertEqual(str(errors[0]), "bad snapshot")

    def test_capture_recovers_via_drill_from_padstack(self):
        point = SimpleNamespace(x=1_000_000, y=2_000_000)
        via = SimpleNamespace(
            start=point, net=SimpleNamespace(name="VCC"), width=600_000,
            layers=[0, 31],
            padstack=SimpleNamespace(drill=SimpleNamespace(diameter=320_000)),
        )
        snapshot = capture_dc_board(SimpleNamespace(footprints=[], vias=[via]))
        self.assertEqual(snapshot.vias[0].drill_size, DCPointSnapshot(320_000, 320_000))

    def test_a_pass_element_propagates_its_input_voltage_not_nominal(self):
        # DC-3 regression. P02 Rev E (2026-09-22): the J2 chain (buck L3 ->
        # fuse -> eFuse -> shunt -> switch -> J2) totals 20.4+26.5+6.7+2.4 =
        # 56.1 mV of copper the old Dirichlet-at-nominal reset made invisible
        # at every link; the 5 V chain to J6 totals 3.3+24.2+15.3 = 42.8 mV.
        # This checks the propagation mechanism directly: a fuse (LINEAR,
        # same nominal in and out -- a pass element, not a regulator) must
        # carry its own input rail's real solved voltage, minus I*r_pass_ohm,
        # to its output rail's source -- not reset to nominal.
        fuse = VoltageRegulator(
            name="F1 fuse", input_rail_name="UPSTREAM", input_ref_des="F1",
            input_pad_names=["1"], output_rail_name="DOWNSTREAM",
            output_ref_des="F1", output_pad_names=["2"], reg_type="LINEAR",
            r_pass_ohm=0.05,
        )
        upstream = PowerRail("UPSTREAM", nominal_voltage=5.0, child_regulators=[fuse])
        downstream = PowerRail("DOWNSTREAM", nominal_voltage=5.0)
        rails = [upstream, downstream]
        demands = [(fuse, 1.0)]  # 1 A drawn through the fuse

        def locate(ref_des, pad_names):
            return [42] if ref_des == "F1" else []

        sources, loads, regulator_input_nodes = _rail_injections(
            upstream, rails, demands, locate, lambda m: None, pass_voltages={},
        )
        self.assertEqual(regulator_input_nodes, {"DOWNSTREAM": [42]})
        pass_voltages = {}
        # The upstream rail's own copper drops the fuse's input pad to 4.95 V.
        _propagated_pass_voltages(
            upstream, rails, demands, regulator_input_nodes, {42: 4.95}, pass_voltages,
        )
        self.assertAlmostEqual(pass_voltages["DOWNSTREAM"], 4.95 - 1.0 * 0.05)  # 4.90 V

        def locate_output(ref_des, pad_names):
            return [7] if ref_des == "F1" else []

        down_sources, _loads, _nodes = _rail_injections(
            downstream, rails, [], locate_output, lambda m: None, pass_voltages=pass_voltages,
        )
        self.assertEqual(down_sources, [{"node_id": 7, "voltage": 4.90}])

        # Without pass_voltages (or before the upstream rail is solved), the
        # output still falls back to nominal -- the pre-DC-3 behaviour, kept
        # for the case propagation is not yet available.
        down_sources_no_chain, _loads, _nodes = _rail_injections(
            downstream, rails, [], locate_output, lambda m: None,
        )
        self.assertEqual(down_sources_no_chain, [{"node_id": 7, "voltage": 5.0}])

    def test_raster_locator_finds_a_pad_on_the_bottom_layer(self):
        # KiCad 10's IPC numbers B.Cu 34. The locator stopped at id 31, which
        # left the J1, F1 and U6 sources of the P02 Rev E board unlocated.
        pad = DCPadSnapshot(
            "A4", "A4", DCPointSnapshot(1_000_000, 2_000_000), DCNetSnapshot("VBUS"), layers=(34,),
        )
        board = DCBoardSnapshot((DCFootprintSnapshot("J1", (pad,)),), ())
        mesh = SimpleNamespace(grid_origin=(0.0, 0.0), grid_step=0.1, node_map={(10, 20, 34): 7})
        self.assertEqual(_mesh_nodes(mesh, board, "J1", ["A4"]), [7])


if __name__ == "__main__":
    unittest.main()
