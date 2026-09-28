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


if __name__ == "__main__":
    unittest.main()
