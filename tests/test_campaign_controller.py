"""Tests for the campaign orchestrator.

Scope: sequencing, failure isolation, cancellation, and that aggregation is
actually invoked.  Domain engines and adapters are injected fakes -- running
the real DC or thermal solvers here would be slow and would test those
solvers, not this orchestrator.
"""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from analysis_contract import (
    AnalysisFinding, AnalysisResult, AnalysisStatus, EvidenceConfidence,
    FindingSeverity,
)
from analysis_registry import AnalysisDescriptor, AnalysisRegistry
from application.campaign_controller import (
    CampaignEngine, CampaignRunRequest,
)


class FakeRequest:
    """A domain request stand-in; the fake adapter ignores its contents."""

    def __init__(self, tag="x"):
        self.tag = tag


class FakeEngine:
    """Records that it ran and returns a trivial domain object."""

    def __init__(self, findings=None, raises=None):
        self.calls = 0
        self._findings = findings or []
        self._raises = raises

    def solve(self, request, emit_log, emit_progress, cancelled):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        emit_log("working")
        return {"findings": self._findings}


def fake_adapter(analysis_type):
    """Build an adapter turning the fake domain object into an AnalysisResult."""

    def _adapt(domain_result, request):
        result = AnalysisResult(
            analysis_type=analysis_type,
            title=f"{analysis_type} analysis",
            status=AnalysisStatus.PASS,
        )
        for spec in domain_result.get("findings", []):
            result.findings.append(AnalysisFinding(
                rule_id=spec.get("rule_id", "X-001"),
                category="TEST",
                severity=spec.get("severity", FindingSeverity.MEDIUM),
                title=spec.get("title", "finding"),
                description="synthetic",
                confidence=EvidenceConfidence.DETERMINISTIC,
                nets=spec.get("nets", []),
            ))
        if result.findings:
            result.status = AnalysisStatus.WARN
        return result

    return _adapt


def registry_of(*analysis_ids, include_debug=False):
    """A registry containing the given runnable ids, in the given order."""
    descriptors = [
        AnalysisDescriptor(aid, f"{aid} title", "Group", (index + 1) * 10,
                           ("board",), ("run",))
        for index, aid in enumerate(analysis_ids)
    ]
    if include_debug:
        descriptors.append(
            AnalysisDescriptor("DEBUG", "Diagnostics", "Application", 90,
                               ("board",), ("inspect",))
        )
    return AnalysisRegistry(descriptors)


def build(engines, analysis_ids, cache=None, include_debug=False):
    return CampaignEngine(
        domain_engines=engines,
        adapters={aid: fake_adapter(aid) for aid in analysis_ids},
        registry=registry_of(*analysis_ids, include_debug=include_debug),
        cache=cache,
    )


def run(engine, request):
    """Drive the engine directly, collecting logs and progress calls."""
    logs, progress = [], []
    campaign = engine.solve(
        request, logs.append,
        lambda *args: progress.append(args),
        lambda: False,
    )
    return campaign, logs, progress


class SequencingTests(unittest.TestCase):
    def test_domains_run_in_registry_order_not_dict_order(self):
        order = []

        class Recording(FakeEngine):
            def __init__(self, name):
                super().__init__()
                self.name = name

            def solve(self, request, emit_log, emit_progress, cancelled):
                order.append(self.name)
                return super().solve(request, emit_log, emit_progress, cancelled)

        # Insert the dict in deliberately reversed order.
        engines = {
            "CFD": Recording("CFD"),
            "EMC": Recording("EMC"),
            "DC": Recording("DC"),
        }
        engine = build(engines, ["DC", "EMC", "CFD"])
        request = CampaignRunRequest(domain_requests={
            "CFD": FakeRequest(), "EMC": FakeRequest(), "DC": FakeRequest(),
        })
        run(engine, request)
        self.assertEqual(order, ["DC", "EMC", "CFD"])

    def test_debug_is_never_executed(self):
        debug_engine = FakeEngine()
        engine = build(
            {"DC": FakeEngine(), "DEBUG": debug_engine},
            ["DC"], include_debug=True,
        )
        request = CampaignRunRequest(domain_requests={
            "DC": FakeRequest(), "DEBUG": FakeRequest(),
        })
        campaign, _, _ = run(engine, request)
        self.assertEqual(debug_engine.calls, 0)
        self.assertEqual([r.analysis_type for r in campaign.results], ["DC"])

    def test_progress_emitted_once_per_domain_plus_final(self):
        engine = build(
            {"DC": FakeEngine(), "EMC": FakeEngine()}, ["DC", "EMC"],
        )
        request = CampaignRunRequest(domain_requests={
            "DC": FakeRequest(), "EMC": FakeRequest(),
        })
        _, _, progress = run(engine, request)
        # one before each domain, plus the terminal "campaign complete"
        self.assertEqual(len(progress), 3)
        self.assertEqual(progress[-1][2], "campaign complete")


class FailureIsolationTests(unittest.TestCase):
    def test_one_failing_domain_does_not_lose_the_others(self):
        good_a, bad, good_b = FakeEngine(), FakeEngine(raises=RuntimeError("boom")), FakeEngine()
        engine = build(
            {"DC": good_a, "EMC": bad, "CFD": good_b}, ["DC", "EMC", "CFD"],
        )
        request = CampaignRunRequest(domain_requests={
            "DC": FakeRequest(), "EMC": FakeRequest(), "CFD": FakeRequest(),
        })
        campaign, _, _ = run(engine, request)

        self.assertEqual(len(campaign.results), 2)
        self.assertEqual(
            sorted(r.analysis_type for r in campaign.results), ["CFD", "DC"],
        )
        self.assertEqual(good_b.calls, 1, "domain after the failure must still run")

        failed = [o for o in engine.last_outcomes if o.analysis_id == "EMC"][0]
        self.assertIsNone(failed.result)
        self.assertIn("boom", failed.error)

    def test_stop_on_error_halts_after_first_failure(self):
        later = FakeEngine()
        engine = build(
            {"DC": FakeEngine(raises=ValueError("nope")), "EMC": later},
            ["DC", "EMC"],
        )
        request = CampaignRunRequest(
            domain_requests={"DC": FakeRequest(), "EMC": FakeRequest()},
            stop_on_error=True,
        )
        run(engine, request)
        self.assertEqual(later.calls, 0)


class SkippingTests(unittest.TestCase):
    def test_domain_without_request_is_skipped_not_failed(self):
        engine = build({"DC": FakeEngine(), "EMC": FakeEngine()}, ["DC", "EMC"])
        request = CampaignRunRequest(domain_requests={"DC": FakeRequest()})
        campaign, _, _ = run(engine, request)

        skipped = [o for o in engine.last_outcomes if o.analysis_id == "EMC"][0]
        self.assertEqual(skipped.skipped_reason, "no request provided")
        self.assertIsNone(skipped.error)
        # A skipped domain must not show up as a passing score.
        self.assertNotIn("EMC", [s.domain for s in campaign.domain_scores])


class CancellationTests(unittest.TestCase):
    def test_cancel_after_first_domain_keeps_completed_work(self):
        state = {"count": 0}

        def cancelled():
            # False for the first domain's pre-check, True from then on.
            state["count"] += 1
            return state["count"] > 1

        engine = build(
            {"DC": FakeEngine(), "EMC": FakeEngine()}, ["DC", "EMC"],
        )
        request = CampaignRunRequest(domain_requests={
            "DC": FakeRequest(), "EMC": FakeRequest(),
        })
        campaign = engine.solve(request, lambda m: None, lambda *a: None, cancelled)

        self.assertEqual(campaign.overall_status, AnalysisStatus.CANCELLED)
        self.assertEqual(len(campaign.results), 1)
        self.assertEqual(campaign.results[0].analysis_type, "DC")


class AggregationTests(unittest.TestCase):
    def test_two_domains_on_same_net_merge_into_one_action(self):
        shared = [{"rule_id": "R-1", "title": "Problem on VCC",
                   "severity": FindingSeverity.HIGH, "nets": ["VCC"]}]
        engine = build(
            {
                "DC": FakeEngine(findings=shared),
                "EMC": FakeEngine(findings=[dict(shared[0], rule_id="R-2")]),
            },
            ["DC", "EMC"],
        )
        request = CampaignRunRequest(domain_requests={
            "DC": FakeRequest(), "EMC": FakeRequest(),
        })
        campaign, _, _ = run(engine, request)

        self.assertEqual(len(campaign.results), 2)
        self.assertEqual(len(campaign.actions), 1,
                         "findings on the same net must deduplicate")
        self.assertEqual(sorted(campaign.actions[0].domains), ["DC", "EMC"])

    def test_campaign_carries_project_identity(self):
        engine = build({"DC": FakeEngine()}, ["DC"])
        request = CampaignRunRequest(
            project_name="p02_alimentation",
            board_fingerprint="fp-abc",
            domain_requests={"DC": FakeRequest()},
        )
        campaign, _, _ = run(engine, request)
        self.assertEqual(campaign.project_name, "p02_alimentation")
        self.assertEqual(campaign.board_fingerprint, "fp-abc")
        # Propagated down to the individual result as well.
        self.assertEqual(campaign.results[0].board_fingerprint, "fp-abc")


class DefaultAdapterContextTests(unittest.TestCase):
    """Missing adapter context must fail loudly, not fabricate a verdict."""

    def test_dc_without_target_refuses_rather_than_defaulting_to_zero(self):
        # Regression: a 0.0 fallback for maximum_drop_pct turns
        # `drop_pct > maximum_drop_pct` into "every rail with any drop at
        # all", so a clean board would report one HIGH voltage-drop failure
        # per rail against a 0.000% budget nobody configured.
        from application.campaign_controller import _adapt_dc

        with self.assertRaises(ValueError) as caught:
            _adapt_dc({"+5V": {"stats": (4.9, 5.0, 0.1)}}, FakeRequest())
        message = str(caught.exception)
        self.assertIn("maximum_drop_pct", message)
        self.assertIn("DC", message)

    def test_optional_context_still_falls_back(self):
        # adapt_thermal_result declares coupled/elapsed_seconds WITH defaults,
        # so falling back to them is correct and must keep working.
        from application.campaign_controller import _adapt_thermal

        result = _adapt_thermal(_ThermalStub(), FakeRequest())
        self.assertEqual(result.analysis_type, "THERMAL")

    def test_missing_context_is_isolated_to_its_own_domain(self):
        # The refusal must surface as one domain error while siblings still run.
        from application.campaign_controller import _adapt_dc

        engine = build({"DC": FakeEngine(), "EMC": FakeEngine()}, ["DC", "EMC"])
        engine._adapters["DC"] = _adapt_dc          # real adapter, no context
        outcomes = []
        engine.set_domain_listener(outcomes.append)

        campaign, _, _ = run(engine, CampaignRunRequest(domain_requests={
            "DC": FakeRequest(), "EMC": FakeRequest(),
        }))

        dc = next(o for o in outcomes if o.analysis_id == "DC")
        emc = next(o for o in outcomes if o.analysis_id == "EMC")
        self.assertIsNotNone(dc.error)
        self.assertIn("maximum_drop_pct", dc.error)
        self.assertIsNotNone(emc.result, "a sibling domain must survive")
        self.assertEqual([r.analysis_type for r in campaign.results], ["EMC"])


class _ThermalStub:
    """Minimal stand-in for a thermal domain result."""
    converged = True
    maximum_temperature_c = 40.0
    ambient_temperature_c = 25.0


if __name__ == "__main__":
    unittest.main()
