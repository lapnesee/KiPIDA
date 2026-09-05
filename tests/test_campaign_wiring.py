"""The campaign orchestrator is reachable, and it speaks the real engines.

tests/test_campaign_controller.py drives CampaignEngine with injected fakes,
which is right for sequencing and isolation but says nothing about whether the
production engines and adapters fit together -- and for as long as nothing but
those tests called it, they did not.  Three of the six default adapters were
written against a shape no engine returns.

So this module tests the joins: that a production engine set exists and covers
the registry, that each default adapter reads the outcome its engine actually
hands back, that the cross-domain inputs EMC consumes come from the campaign
rather than from a previous session, and that the module is imported by
production code at all.
"""

import ast
import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

import analysis_adapters
from analysis_registry import AnalysisDescriptor, AnalysisRegistry, DEFAULT_ANALYSES
from application.campaign_controller import (
    CampaignCallbacks, CampaignController, CampaignEngine, CampaignRunRequest,
    DEFAULT_ADAPTERS, default_domain_engines,
)


class _Recorder:
    """Replace an analysis_adapters entry and record what it was handed."""

    def __init__(self, test, name):
        self.calls = []
        self._test = test
        self._name = name
        self._original = getattr(analysis_adapters, name)
        setattr(analysis_adapters, name, self)
        test.addCleanup(setattr, analysis_adapters, name, self._original)

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return "adapted"


class ProductionEngineSet(unittest.TestCase):
    def test_every_runnable_analysis_has_an_engine_and_an_adapter(self):
        # A runnable analysis with no engine is skipped with "no engine
        # registered" -- silently absent from the report rather than failed.
        runnable = [
            descriptor.analysis_id for descriptor in DEFAULT_ANALYSES.all()
            if "run" in descriptor.capabilities
        ]
        engines = default_domain_engines()
        for analysis_id in runnable:
            self.assertIn(analysis_id, engines)
            self.assertIn(analysis_id, DEFAULT_ADAPTERS)
            self.assertTrue(hasattr(engines[analysis_id], "solve"))

    def test_engines_are_not_shared_between_calls(self):
        # Engines hold solver factories and, for thermal, a mesh cache; two
        # campaigns must not share one.
        first, second = default_domain_engines(), default_domain_engines()
        self.assertIsNot(first["DC"], second["DC"])


class DefaultAdaptersMatchTheirEngines(unittest.TestCase):
    """Each adapter must read the outcome its engine actually returns."""

    def test_dc_goes_through_the_run_adapter_so_fixes_are_sized(self):
        # adapt_dc_result alone leaves every voltage-drop action reading "No
        # structured remediation was computed"; adapt_dc_run is the one call
        # the dialog makes too, so the two cannot disagree.
        recorder = _Recorder(self, "adapt_dc_run")

        class _Request:
            maximum_drop_pct = 5.0
            board_path = "/boards/p02.kicad_pcb"
            rails = ("+5V",)

        DEFAULT_ADAPTERS["DC"]({"+5V": {}}, _Request())
        args, kwargs = recorder.calls[0]
        self.assertEqual(args, ({"+5V": {}}, 5.0))
        self.assertEqual(kwargs["board_path"], "/boards/p02.kicad_pcb")
        self.assertEqual(kwargs["rails"], ("+5V",))

    def test_dc_still_refuses_a_run_with_no_drop_target(self):
        class _Request:
            maximum_drop_pct = None

        with self.assertRaises(ValueError) as caught:
            DEFAULT_ADAPTERS["DC"]({"+5V": {}}, _Request())
        self.assertIn("maximum_drop_pct", str(caught.exception))

    def test_ac_unpacks_the_engine_pair(self):
        # ACSolverEngine.solve returns (sweep, optimization), not the sweep.
        recorder = _Recorder(self, "adapt_ac_result")

        class _Request:
            settings = "ac-settings"

        DEFAULT_ADAPTERS["AC"](("sweep", "optimization"), _Request())
        self.assertEqual(
            recorder.calls[0][0], ("sweep", "optimization", "ac-settings"),
        )

    def test_differential_reads_the_resolved_tolerance_off_the_outcome(self):
        # The tolerance is resolved during the solve. Reading it off the
        # request would report the target that was asked for, not the one the
        # results were graded against.
        from application.differential_controller import DifferentialRunOutcome

        recorder = _Recorder(self, "adapt_differential_result")
        outcome = DifferentialRunOutcome(
            results=("r1",), stackup="solved-stackup", impedance_png=None,
            stackup_png=None, target_tolerance_pct=7.5,
        )

        class _Request:
            stackup = "requested-stackup"
            tolerance_pct = 99.0

        DEFAULT_ADAPTERS["DIFFERENTIAL"](outcome, _Request())
        self.assertEqual(recorder.calls[0][0], (("r1",), "solved-stackup", 7.5))

    def test_emc_reads_the_settings_the_run_ended_with(self):
        # The engine may turn Phase 10 off mid-run; the outcome's copy is the
        # one that describes what happened.
        from application.emc_controller import EMCRunOutcome

        recorder = _Recorder(self, "adapt_emc_result")
        outcome = EMCRunOutcome(
            settings="amended-settings", result="emc-result",
            risk_png=None, spectrum_png=None,
        )

        class _Request:
            settings = "requested-settings"

        DEFAULT_ADAPTERS["EMC"](outcome, _Request())
        self.assertEqual(recorder.calls[0][0], ("amended-settings", "emc-result"))

    def test_thermal_reads_coupling_and_elapsed_off_the_outcome(self):
        from application.thermal_controller import ThermalRunOutcome

        recorder = _Recorder(self, "adapt_thermal_result")
        outcome = ThermalRunOutcome(
            mesh=None, result="thermal-result", coupled_result="electrothermal",
            system_results={}, cache_key=None, cache_value=None,
            elapsed_seconds=3.25,
        )

        DEFAULT_ADAPTERS["THERMAL"](outcome, object())
        self.assertEqual(recorder.calls[0][0], ("thermal-result", True, 3.25))

    def test_cfd_takes_the_mesh_from_the_outcome_not_the_request(self):
        # The request carries a board model; the mesh only exists once the
        # solve has built it.
        from application.cfd_controller import CFDRunOutcome

        recorder = _Recorder(self, "adapt_cfd_result")
        outcome = CFDRunOutcome(mesh="solved-mesh", result="cfd-result", plots=())

        DEFAULT_ADAPTERS["CFD"](outcome, object())
        self.assertEqual(recorder.calls[0][0], ("solved-mesh", "cfd-result"))


class _Engine:
    """Returns a fixed outcome and records the request it was given."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.seen = None

    def solve(self, request, emit_log, emit_progress, cancelled):
        self.seen = request
        return self.outcome


def _adapter(analysis_type):
    from analysis_contract import AnalysisResult, AnalysisStatus

    return lambda domain_result, request: AnalysisResult(
        analysis_type=analysis_type, title=analysis_type,
        status=AnalysisStatus.PASS,
    )


def _registry(*analysis_ids):
    return AnalysisRegistry([
        AnalysisDescriptor(aid, f"{aid} title", "Group", (index + 1) * 10,
                           ("board",), ("run",))
        for index, aid in enumerate(analysis_ids)
    ])


class EMCReadsThisCampaignsResults(unittest.TestCase):
    """EMC consumes the other domains rather than recomputing them."""

    def setUp(self):
        from application.emc_controller import EMCRunRequest
        from application.differential_controller import DifferentialRunOutcome
        from application.thermal_controller import ThermalRunOutcome

        self.EMCRunRequest = EMCRunRequest
        self.DifferentialRunOutcome = DifferentialRunOutcome
        self.ThermalRunOutcome = ThermalRunOutcome

    def _run(self, analysis_ids, domain_requests, engines):
        engine = CampaignEngine(
            domain_engines=engines,
            adapters={aid: _adapter(aid) for aid in analysis_ids},
            registry=_registry(*analysis_ids),
        )
        logs = []
        engine.solve(
            CampaignRunRequest(domain_requests=domain_requests),
            logs.append, lambda *args: None, lambda: False,
        )
        return logs

    def test_emc_runs_after_the_domains_it_reads(self):
        # The registry lists EMC at 40 and thermal at 50, which is the right
        # order to show them and the wrong one to run them.
        order = []

        class _Ordered(_Engine):
            def __init__(self, name, outcome):
                super().__init__(outcome)
                self.name = name

            def solve(self, request, emit_log, emit_progress, cancelled):
                order.append(self.name)
                return super().solve(request, emit_log, emit_progress, cancelled)

        thermal_outcome = self.ThermalRunOutcome(
            mesh=None, result="thermal-result", coupled_result=None,
            system_results={}, cache_key=None, cache_value=None,
            elapsed_seconds=1.0,
        )
        self._run(
            ["EMC", "THERMAL"],
            {"EMC": self.EMCRunRequest(settings=None, pairs=(), snapshot=None),
             "THERMAL": object()},
            {"EMC": _Ordered("EMC", object()),
             "THERMAL": _Ordered("THERMAL", thermal_outcome)},
        )
        self.assertEqual(order, ["THERMAL", "EMC"])

    def test_the_campaigns_thermal_field_replaces_the_sessions(self):
        thermal_outcome = self.ThermalRunOutcome(
            mesh=None, result="fresh-thermal", coupled_result=None,
            system_results={}, cache_key=None, cache_value=None,
            elapsed_seconds=1.0,
        )
        emc_engine = _Engine(object())
        self._run(
            ["THERMAL", "EMC"],
            {"THERMAL": object(),
             "EMC": self.EMCRunRequest(
                 settings=None, pairs=(), snapshot=None,
                 thermal_result="stale-thermal",
             )},
            {"THERMAL": _Engine(thermal_outcome), "EMC": emc_engine},
        )
        self.assertEqual(emc_engine.seen.thermal_result, "fresh-thermal")

    def test_differential_results_are_keyed_the_way_emc_looks_them_up(self):
        # emc_analyzer does differential_results.get(pair.signature).
        class _Pair:
            signature = ("D+", "D-")

        class _Result:
            pair = _Pair()

        outcome = self.DifferentialRunOutcome(
            results=(_Result(),), stackup=None, impedance_png=None,
            stackup_png=None, target_tolerance_pct=10.0,
        )
        emc_engine = _Engine(object())
        self._run(
            ["DIFFERENTIAL", "EMC"],
            {"DIFFERENTIAL": object(),
             "EMC": self.EMCRunRequest(settings=None, pairs=(), snapshot=None)},
            {"DIFFERENTIAL": _Engine(outcome), "EMC": emc_engine},
        )
        self.assertEqual(list(emc_engine.seen.differential_results), [("D+", "D-")])

    def test_only_the_domains_actually_folded_in_are_announced(self):
        # The log is the provenance the report's reader has; naming a domain
        # EMC did not receive is the shape of an untrue log.
        thermal_outcome = self.ThermalRunOutcome(
            mesh=None, result="fresh-thermal", coupled_result=None,
            system_results={}, cache_key=None, cache_value=None,
            elapsed_seconds=1.0,
        )
        logs = self._run(
            ["THERMAL", "EMC"],
            {"THERMAL": object(),
             "EMC": self.EMCRunRequest(settings=None, pairs=(), snapshot=None)},
            {"THERMAL": _Engine(thermal_outcome), "EMC": _Engine(object())},
        )
        line = next(line for line in logs if "will use this campaign's own" in line)
        self.assertIn("THERMAL", line)
        self.assertNotIn("AC", line)

    def test_a_domain_that_did_not_run_leaves_its_input_alone(self):
        # Absent is absent, not empty: the caller's value stands.
        emc_engine = _Engine(object())
        self._run(
            ["EMC"],
            {"EMC": self.EMCRunRequest(
                settings=None, pairs=(), snapshot=None,
                thermal_result="session-thermal",
            )},
            {"EMC": emc_engine},
        )
        self.assertEqual(emc_engine.seen.thermal_result, "session-thermal")


class CancellationKeepsWhatFinished(unittest.TestCase):
    def test_a_cancelled_campaign_is_delivered_rather_than_discarded(self):
        # BackgroundAnalysisController turns cancellation into an error, which
        # is right for one analysis and wrong for a campaign: it threw away
        # every domain that had already completed.
        from analysis_contract import AnalysisStatus
        from campaign import CampaignResult

        class _CancellingEngine:
            def solve(self, request, emit_log, emit_progress, cancelled):
                campaign = CampaignResult()
                campaign.overall_status = AnalysisStatus.CANCELLED
                return campaign

        controller = CampaignController(engine=_CancellingEngine())
        controller._cancel_event.set()
        delivered, errors = [], []
        controller._run(
            CampaignRunRequest(),
            CampaignCallbacks(on_complete=delivered.append, on_error=errors.append),
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].overall_status, AnalysisStatus.CANCELLED)


class OrchestratorIsReachable(unittest.TestCase):
    """The guard docs/audit-cablage.md asks for, on the module it named.

    A module nothing imports never runs, however green its own tests are.
    That went unnoticed four times in this project, and this orchestrator was
    one of them.
    """

    MODULE = "application.campaign_controller"

    @staticmethod
    def _imports(path):
        # Some sources carry a UTF-8 BOM, which the import machinery strips
        # and ast.parse does not.
        with open(path, encoding="utf-8-sig") as handle:
            tree = ast.parse(handle.read(), filename=path)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        return names

    def _production_modules(self):
        for base, directories, files in os.walk(_root):
            directories[:] = [
                name for name in directories
                if name not in {"tests", "validation", "__pycache__", ".git",
                                "docs", "locales", "resources", "examples",
                                ".runtime", ".venv"}
            ]
            for name in files:
                if name.endswith(".py"):
                    yield os.path.join(base, name)

    def test_a_production_module_imports_the_campaign_orchestrator(self):
        importers = [
            os.path.relpath(path, _root)
            for path in self._production_modules()
            if any(name == self.MODULE or name.startswith(self.MODULE + ".")
                   for name in self._imports(path))
        ]
        self.assertTrue(
            importers,
            f"{self.MODULE} is imported by no production module, so nothing "
            "the user can press reaches it.",
        )


if __name__ == "__main__":
    unittest.main()
