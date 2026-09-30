import threading
import unittest

from application.emc_controller import (
    EMCAnalysisCancelled, EMCAnalysisController, EMCControllerCallbacks,
    EMCRunOutcome, _phase10_spice_findings,
)
from models import EMCPhase10Result, EMCSpiceModelAudit


class FakeEngine:
    def solve(self, request, emit, progress, cancelled):
        emit("emc worker started")
        progress(1, 1, "rules")
        if cancelled():
            raise EMCAnalysisCancelled("EMI/EMC analysis cancelled.")
        return EMCRunOutcome("settings", "result", b"risk", b"spectrum")


class BlockingEngine:
    release = threading.Event()

    def solve(self, request, emit, progress, cancelled):
        self.release.wait(2.0)
        if cancelled():
            raise EMCAnalysisCancelled("EMI/EMC analysis cancelled.")
        return FakeEngine().solve(request, emit, progress, cancelled)


class EMCControllerTests(unittest.TestCase):
    @staticmethod
    def callbacks(completed, errors, progress=None, logs=None):
        return EMCControllerCallbacks(
            on_progress=lambda *args: (progress if progress is not None else []).append(args),
            on_complete=completed.append,
            on_error=errors.append,
            on_log=lambda message: (logs if logs is not None else []).append(message),
        )

    def test_background_analysis_returns_all_artifacts(self):
        completed, errors, progress, logs = [], [], [], []
        controller = EMCAnalysisController(engine=FakeEngine())
        controller.start("request", self.callbacks(completed, errors, progress, logs))
        self.assertTrue(controller.wait(2.0))
        self.assertEqual(completed[0].risk_png, b"risk")
        self.assertEqual(completed[0].spectrum_png, b"spectrum")
        self.assertEqual(progress, [(1, 1, "rules")])
        self.assertEqual(logs, ["emc worker started"])
        self.assertFalse(errors)

    def test_concurrent_start_and_cancellation(self):
        BlockingEngine.release.clear()
        completed, errors = [], []
        controller = EMCAnalysisController(engine=BlockingEngine())
        controller.start("request", self.callbacks(completed, errors))
        with self.assertRaisesRegex(RuntimeError, "already running"):
            controller.start("other", self.callbacks([], []))
        self.assertTrue(controller.cancel())
        BlockingEngine.release.set()
        self.assertTrue(controller.wait(2.0))
        self.assertFalse(completed)
        self.assertIsInstance(errors[0], EMCAnalysisCancelled)

    def test_errors_cross_the_controller_boundary(self):
        class BrokenEngine:
            def solve(self, request, emit, progress, cancelled):
                raise ValueError("bad emc snapshot")

        completed, errors = [], []
        controller = EMCAnalysisController(engine=BrokenEngine())
        controller.start("request", self.callbacks(completed, errors))
        self.assertTrue(controller.wait(2.0))
        self.assertFalse(completed)
        self.assertEqual(str(errors[0]), "bad emc snapshot")


class Phase10BeforeAnalysisTests(unittest.TestCase):
    def test_the_spectrum_takes_each_converter_from_its_board_circuit(self):
        # P02 Rev E's U4 settled in its board circuit at 396.8 kHz, switching
        # -0.10 to 19.93 V with its inductor peaking at 5.55 A; its source
        # says 400 kHz, 20 V and a 4.36 A load.
        from application.emc_controller import EMCRunRequest, EMCSolverEngine
        from models import EMCAnalysisResult, EMCAnalysisSettings, EMCBoardCircuitResult, EMCSignalSource

        source = EMCSignalSource(
            "U4 SW1 buck leg (Q10/Q11/L1)", "/SYS_5V_SW1", kind="SWITCHING", frequency_hz=400e3,
            rise_time_ns=10.0, voltage_swing_v=20.0, current_a=4.36, source="manual",
        )
        settings = EMCAnalysisSettings(sources=[source], field_simulation_enabled=False)
        circuit = EMCBoardCircuitResult(
            source.name, "U4", "SWITCHING", "TPS55288", frequency_hz=396825.4,
            switch_low_v=-0.099, switch_high_v=19.933, inductor_peak_a=5.553, inductor_valley_a=3.426,
        )
        seen = {}

        class FakePipeline:
            def __init__(self, snapshot, settings, analysis_result, **_kwargs):
                self.analysis_result = analysis_result

            def simulate_circuits(self):
                return EMCPhase10Result(board_circuits=[circuit])

            def run(self):
                seen["analysis"] = self.analysis_result
                return EMCPhase10Result(status="COMPLETED", board_circuits=[circuit])

        class FakeAnalyzer:
            def __init__(self, _snapshot, analysed_settings, **_kwargs):
                seen["sources"] = analysed_settings.sources

            def analyze(self, progress_callback):
                seen["result"] = EMCAnalysisResult()
                return seen["result"]

        class FakePlotter:
            def __init__(self, debug=False):
                pass

            def __getattr__(self, _name):
                return lambda *args, **kwargs: None

        engine = EMCSolverEngine(
            analyzer_factory=FakeAnalyzer, phase10_factory=FakePipeline, plotter_factory=FakePlotter,
        )
        outcome = engine.solve(
            EMCRunRequest(settings=settings, pairs=(), snapshot=None),
            lambda _message: None, lambda *_args: None, lambda: False,
        )
        (analysed,) = seen["sources"]
        self.assertAlmostEqual(analysed.frequency_hz, 396825.4)
        self.assertAlmostEqual(analysed.voltage_swing_v, 20.032)
        self.assertAlmostEqual(analysed.current_a, 5.553)
        self.assertEqual((analysed.source, analysed.rise_time_ns), ("board-circuit", 10.0))
        self.assertIs(seen["analysis"], seen["result"])
        self.assertIs(outcome.settings.sources[0], analysed)
        # The configured source keeps its 4.36 A: the next bench's load.
        self.assertEqual(settings.sources[0].current_a, 4.36)


class Phase10FindingsTests(unittest.TestCase):
    def test_missing_and_unstable_spice_models_become_findings(self):
        # Old audit item A + EMC-6, P02 Rev E: U4/U14 read MODEL_FILE_MISSING
        # and U5 read MAPPING_VERIFIED_NGSPICE_TRANSIENT_UNSTABLE in
        # phase10-result.json, invisible anywhere in the report until this.
        phase10 = EMCPhase10Result(spice_model_audit=[
            EMCSpiceModelAudit("U4", "TPS562200", "U4 SW1 buck leg", "MODEL_FILE_MISSING"),
            EMCSpiceModelAudit(
                "U5", "TPS562200DDCT", "U5 SW (+3V3_MAIN buck)",
                "MAPPING_VERIFIED_NGSPICE_TRANSIENT_UNSTABLE",
            ),
            EMCSpiceModelAudit("U14", "TPS562201", "U14 SW (J2 source buck)", "AVAILABLE_VERIFIED_MAPPING"),
        ])
        findings = _phase10_spice_findings(phase10)
        by_ref = {finding.components[0]: finding for finding in findings}
        self.assertEqual(set(by_ref), {"U4", "U5"})
        self.assertEqual(by_ref["U4"].rule_id, "PH-001")
        self.assertEqual(by_ref["U5"].rule_id, "PH-002")
        self.assertTrue(all(finding.severity == "INFO" for finding in findings))

    def test_a_region_phase10_could_not_solve_is_an_info_finding(self):
        # P02 Rev E, 2026-09-29 14:24: all three regions ended
        # SKIPPED_CELL_LIMIT; they were LOW actions in the campaign, noise
        # beside the board's own, when they are facts about the run.
        from models import EMCPhase10RegionResult
        phase10 = EMCPhase10Result(regions=[
            EMCPhase10RegionResult("region_1_sw-003", "SKIPPED_CELL_LIMIT"),
        ])
        (finding,) = _phase10_spice_findings(phase10)
        self.assertEqual((finding.rule_id, finding.severity), ("PH-004", "INFO"))


if __name__ == "__main__":
    unittest.main()
