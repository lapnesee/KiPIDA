"""Run every applicable analysis and aggregate them into one CampaignResult.

The per-domain controllers each solve one problem and hand back one
AnalysisResult.  Nothing so far turns "run all of them" into a single verdict
with a single report -- that is this module.  It owns sequencing, cancellation,
per-domain failure isolation, and aggregation; it owns no wx objects, exactly
like :class:`~application.background_controller.BackgroundAnalysisController`.

Failure isolation is the load-bearing property here.  A campaign exists so the
user can press one button and read one report; a thermal engine that raises
must cost them the thermal section, not the five domains that worked.
"""

from dataclasses import dataclass, field, replace
import copy
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from analysis_contract import AnalysisResult, AnalysisStatus
from analysis_registry import DEFAULT_ANALYSES
from campaign import CampaignResult, domain_status_from_findings
from application.background_controller import BackgroundAnalysisController

import analysis_adapters


class CampaignCancelled(RuntimeError):
    pass


@dataclass
class CampaignRunRequest:
    """What to run and with what per-domain inputs.

    ``domain_requests`` maps an analysis_id ("DC", "EMC", ...) to the request
    object that domain's engine expects.  A domain absent from this mapping is
    not run -- that is how the caller expresses "I only have a board, no
    enclosure, so skip CFD".
    """

    project_name: str = ""
    board_fingerprint: str = ""
    domain_requests: Dict[str, Any] = field(default_factory=dict)
    stop_on_error: bool = False
    # ProjectContext.to_dict(): the host and the mode, rendered in the report.
    project_context: Dict[str, Any] = field(default_factory=dict)
    # {analysis_id: reason} for a domain the user selected that could not be
    # prepared, so it is absent from domain_requests yet still owed a line in
    # the report's coverage rather than only one in the log.
    unprepared: Dict[str, str] = field(default_factory=dict)


@dataclass
class DomainOutcome:
    """Per-domain bookkeeping, including the ways a domain can not produce."""

    analysis_id: str
    result: Optional[AnalysisResult] = None
    error: Optional[str] = None
    skipped_reason: str = ""
    elapsed_seconds: float = 0.0
    from_cache: bool = False

    @property
    def ran(self) -> bool:
        return self.result is not None and not self.skipped_reason


@dataclass
class CampaignCallbacks:
    on_log: Callable[[str], None] = lambda message: None
    on_progress: Callable[..., None] = lambda *args: None
    on_domain_complete: Callable[[DomainOutcome], None] = lambda outcome: None
    # The domain engine's own outcome, before adaptation. It is what fills a
    # per-domain result tab -- report text and plots that the AnalysisResult
    # does not carry -- and it is not retained past this call.
    on_domain_result: Callable[[str, Any], None] = lambda analysis_id, result: None
    on_complete: Callable[[CampaignResult], None] = lambda campaign: None
    on_error: Callable[[Exception], None] = lambda exc: None


# ---------------------------------------------------------------------------
# Default adapters
# ---------------------------------------------------------------------------
#
# Every domain engine returns its own outcome type -- a bare dict of system
# results for DC, a ``(result, optimization)`` pair for AC, a frozen
# ``*RunOutcome`` dataclass for the rest -- and the adapters in
# analysis_adapters each take that outcome unpacked into the two or three
# pieces they actually read.  These wrappers are the only place that knows how
# to go from one to the other, normalised to ``callable(domain_result,
# request) -> AnalysisResult``.
#
# Where each piece comes from matters, and the rule is: the outcome first, the
# request only for what the outcome cannot carry.  The differential tolerance
# and the CFD mesh are properties of the run that happened, not of the run that
# was asked for, and reading them off the request would report the intent
# instead of the result.
#
# One value has no home in either: ``maximum_drop_pct``, the DC pass/fail
# threshold.  It now travels on DCRunRequest, but it is optional there because
# the DC solve itself does not need it, so it can still arrive as None.  It is
# demanded rather than defaulted, because substituting 0.0 turns
# ``if drop_pct > maximum_drop_pct`` into "every rail with any drop at all": a
# clean board would report one HIGH "exceeds the voltage-drop target" finding
# per rail, each citing a 0.000% budget nobody set.  A campaign full of
# confident, fabricated failures is worse than one that says it was not
# configured.  ``CampaignEngine`` isolates per-domain failures, so a missing
# value surfaces as one honest DomainOutcome error naming exactly what to
# supply, while the other domains still run.
#
# Override any entry via ``CampaignEngine(adapters=...)`` when the real value
# lives somewhere other than the request.


def _require(request, attribute: str, analysis_id: str):
    """Pull required adapter context off *request* or fail with a usable message."""
    value = getattr(request, attribute, None)
    if value is None:
        raise ValueError(
            f"{analysis_id} result cannot be adapted: the run request carries no "
            f"'{attribute}'. Supply it on the request, or override the {analysis_id} "
            f"entry via CampaignEngine(adapters=...). Refusing to substitute a "
            f"default, which would report fabricated pass/fail verdicts."
        )
    return value


def _adapt_dc(domain_result, request) -> AnalysisResult:
    # DCSolverEngine returns the system_results mapping itself.  adapt_dc_run,
    # not adapt_dc_result: the sized copper fixes are part of the DC result
    # everywhere else, and a campaign report missing them would say "No
    # structured remediation was computed" on every voltage-drop action.
    return analysis_adapters.adapt_dc_run(
        domain_result, _require(request, "maximum_drop_pct", "DC"),
        board_path=getattr(request, "board_path", None),
        rails=getattr(request, "rails", None),
    )


def _adapt_ac(domain_result, request) -> AnalysisResult:
    # ACSolverEngine returns (sweep_result, optimization) for one profile, the
    # optimization None unless the request asked for one, and an
    # ACBatchOutcome for a campaign's every profile.
    runs = getattr(domain_result, "runs", None)
    if runs is not None:
        return analysis_adapters.adapt_ac_batch(
            [(result, optimization, item.settings) for item, result, optimization in runs],
            skipped=domain_result.skipped,
        )
    result, optimization = domain_result
    return analysis_adapters.adapt_ac_result(
        result, optimization, getattr(request, "settings", None),
    )


def _adapt_differential(domain_result, request) -> AnalysisResult:
    # The tolerance is resolved during the solve, so it is read off the
    # outcome: the request carries the settings, not the resolved number.
    return analysis_adapters.adapt_differential_result(
        domain_result.results,
        domain_result.stackup,
        domain_result.target_tolerance_pct,
    )


def _adapt_emc(domain_result, request) -> AnalysisResult:
    # The engine may amend the settings it was given (Phase 10 can be turned
    # off mid-run), so the outcome's copy is the one that describes the run.
    return analysis_adapters.adapt_emc_result(
        domain_result.settings, domain_result.result,
    )


def _adapt_thermal(domain_result, request) -> AnalysisResult:
    # TH-1: the campaign report is what a reader actually sees, so it needs
    # the mesh (to name the component at the hotspot) and the power-stage
    # accounting (to say which stages are efficiency-closed fallbacks) that
    # the plain per-run text report already had.
    return analysis_adapters.adapt_thermal_result(
        domain_result.result,
        domain_result.coupled_result is not None,
        domain_result.elapsed_seconds,
        copper_loss_source=getattr(domain_result, "copper_loss_source", ""),
        mesh_sensitivity=getattr(domain_result, "mesh_sensitivity", None),
        mesh=getattr(domain_result, "mesh", None),
        power_stage_reports=getattr(getattr(request, "settings", None), "power_stage_reports", None),
        # COH-4: request.settings still holds the nominal, pre-enclosure
        # ambient -- ThermalSolverEngine.solve elevates its own local copy,
        # never the caller's -- so the adapter can report "ambient + rise".
        ambient_c=getattr(getattr(request, "settings", None), "ambient_c", None),
        enclosure_air_rise_k=getattr(domain_result, "enclosure_air_rise_k", None),
        enclosure_ambient_applied=getattr(domain_result, "enclosure_ambient_applied", False),
        enclosure=getattr(request, "enclosure", None),
    )


def _adapt_cfd(domain_result, request) -> AnalysisResult:
    return analysis_adapters.adapt_cfd_result(
        domain_result.mesh, domain_result.result,
        copper_loss_source=getattr(domain_result, "copper_loss_source", ""),
        settings=getattr(request, "settings", None),
    )


def _adapt_schematic(domain_result, request) -> AnalysisResult:
    # SchematicRuleEngine builds its AnalysisResult itself: the rules are findings already.
    return domain_result


DEFAULT_ADAPTERS: Dict[str, Callable[[Any, Any], AnalysisResult]] = {
    "SCHEMATIC": _adapt_schematic,
    "DC": _adapt_dc,
    "AC": _adapt_ac,
    "DIFFERENTIAL": _adapt_differential,
    "EMC": _adapt_emc,
    "THERMAL": _adapt_thermal,
    "CFD": _adapt_cfd,
}


def default_domain_engines() -> Dict[str, Any]:
    """One engine per runnable analysis, built with the production defaults.

    ``CampaignEngine`` takes its engines by injection, which is right for
    testing and was also, for as long as nothing built the production set, the
    reason the campaign never ran anything.  This is that set, in one place, so
    "run every domain" is a call rather than six imports at the call site.
    """
    from application.ac_controller import ACSolverEngine
    from application.cfd_controller import CFDSolverEngine
    from application.dc_controller import DefaultDCSolverEngine
    from application.differential_controller import DifferentialSolverEngine
    from application.emc_controller import EMCSolverEngine
    from application.schematic_controller import SchematicRuleEngine
    from application.thermal_controller import ThermalSolverEngine

    return {
        "SCHEMATIC": SchematicRuleEngine(),
        "DC": DefaultDCSolverEngine(),
        "AC": ACSolverEngine(),
        "DIFFERENTIAL": DifferentialSolverEngine(),
        "EMC": EMCSolverEngine(),
        "THERMAL": ThermalSolverEngine(),
        "CFD": CFDSolverEngine(),
    }


class CampaignEngine:
    """Sequence domain engines and aggregate their results.

    Conforms to the same engine protocol as the per-domain engines:
    ``solve(request, emit_log, emit_progress, cancelled)``.
    """

    def __init__(
        self,
        domain_engines: Dict[str, Any],
        adapters: Optional[Dict[str, Callable[[Any, Any], AnalysisResult]]] = None,
        registry=None,
        cache=None,
    ):
        self._engines = dict(domain_engines or {})
        self._adapters = dict(DEFAULT_ADAPTERS)
        if adapters:
            self._adapters.update(adapters)
        self._registry = registry or DEFAULT_ANALYSES
        self._cache = cache
        self._domain_listener: Optional[Callable[[DomainOutcome], None]] = None
        self._raw_listener: Optional[Callable[[str, Any], None]] = None
        # {analysis_id: run_id} of each domain's latest run, set before its
        # raw outcome is handed over (run_id_for).
        self._run_ids: Dict[str, str] = {}
        # The same, for this campaign only: what a cached domain's inputs
        # came from (campaign_cache.configuration_digest's context).
        self._campaign_run_ids: Dict[str, str] = {}

    def set_domain_listener(self, listener: Optional[Callable[[DomainOutcome], None]]) -> None:
        """Receive each DomainOutcome as it completes.

        The base controller only forwards ``on_log``/``on_progress`` to an
        engine, so per-domain completion is wired here rather than through the
        callback bundle.
        """
        self._domain_listener = listener

    def set_raw_result_listener(self, listener: Optional[Callable[[str, Any], None]]) -> None:
        """Receive each domain engine's own outcome, before it is adapted.

        A DomainOutcome deliberately carries only the adapted AnalysisResult:
        a CFDRunOutcome holds a mesh and six rendered figures, and keeping one
        per domain alive until the report is built would hold the whole
        campaign's fields in memory at once.  A UI still needs those raw
        outcomes -- they are what fills the per-domain result tabs with plots
        and a report -- so they are handed over as they arrive and not
        retained here.  The listener is called on the campaign thread; a
        caller that owns widgets is responsible for marshalling.
        """
        self._raw_listener = listener

    def run_id_for(self, analysis_id: str) -> str:
        """The run_id the campaign's result for *analysis_id* carries.

        A UI publishes the raw outcome with an AnalysisResult of its own, and
        the result history files that one. Giving it this run_id lets the
        campaign's result find its saved plots (ProjectResultsHistory.
        attach_artifacts): with two ids, P02 Rev E's 2026-10-01 16:51 report
        found none of them.
        """
        return self._run_ids.get(analysis_id, "")

    def runnable_descriptors(self) -> List[Any]:
        """Registry order, executable analyses only.

        DEBUG advertises ``inspect`` rather than ``run``; filtering on the
        capability keeps it out without naming it.
        """
        return [
            descriptor for descriptor in self._registry.all()
            if "run" in descriptor.capabilities
        ]

    @staticmethod
    def _forced_flow_patches(cfd_request) -> bool:
        """True when the enclosure is driven by a fan or inlet rather than heat."""
        settings = getattr(cfd_request, "settings", None)
        patches = getattr(settings, "patches", None) or ()
        return any(
            str(getattr(patch, "kind", "")).upper() in {"INLET", "FAN"}
            and float(getattr(patch, "velocity_m_s", 0.0) or 0.0) > 0.0
            for patch in patches
        )

    def _run_order(self, request, emit_log):
        """The order the domains actually run in.

        The registry's order is a catalogue order -- it groups the analyses the
        way the workspace lists them.  Two couplings need more than that, and
        both are applied here so one place answers "why did CFD run third?".
        """
        self._hand_enclosure_to_thermal(request, emit_log)
        descriptors = self._order_for_cfd_coupling(request, emit_log)
        return self._order_for_emc_inputs(descriptors, request, emit_log)

    @staticmethod
    def _hand_enclosure_to_thermal(request, emit_log):
        """Give THERMAL the enclosure the campaign's CFD models, before either runs.

        The internal air rise, Q / (h * A_walls), needs the box's walls and
        the board's heat, not the CFD's flow field, so it does not wait for
        CFD. It used to ride the forced-flow handover, which a buoyant
        enclosure never takes: on P02 Rev E (2026-09-29 18:43) THERMAL solved
        at the 45 C outside ambient while the CFD of the same campaign put
        the sealed box's air 15.25 K above it -- U14's 126.1 C junction and
        its 23.9 K margin were both about 15 K optimistic.
        """
        requests = request.domain_requests
        thermal, cfd = requests.get("THERMAL"), requests.get("CFD")
        enclosure = getattr(cfd, "settings", None)
        if thermal is None or enclosure is None or getattr(thermal, "enclosure", None) is not None:
            return
        requests["THERMAL"] = replace(thermal, enclosure=enclosure)
        emit_log("THERMAL will balance its ambient against the enclosure CFD's box.")

    def _order_for_emc_inputs(self, descriptors, request, emit_log):
        """Move EMC after every domain whose results it reads.

        The EMI/EMC rules consume the AC sweep, the differential results and
        the thermal field rather than recomputing them, so EMC running before
        them would grade the board against inputs this campaign has not
        produced yet.  In the registry EMC sits at 40 and thermal at 50, which
        is the right order to *list* them and the wrong one to run them.
        """
        ids = [descriptor.analysis_id for descriptor in descriptors]
        if "EMC" not in ids:
            return descriptors
        inputs = [
            analysis_id for analysis_id in self._EMC_INPUT_DOMAINS
            if analysis_id in ids and analysis_id in request.domain_requests
        ]
        if "EMC" not in request.domain_requests or not inputs:
            return descriptors
        last_input = max(ids.index(analysis_id) for analysis_id in inputs)
        if ids.index("EMC") > last_input:
            return descriptors
        emc = descriptors[ids.index("EMC")]
        reordered = [d for d in descriptors if d.analysis_id != "EMC"]
        position = max(
            [d.analysis_id for d in reordered].index(analysis_id)
            for analysis_id in inputs
        ) + 1
        reordered.insert(position, emc)
        emit_log(
            "EMC runs after " + ", ".join(inputs)
            + ": it reads their results rather than recomputing them."
        )
        return reordered

    def _order_for_cfd_coupling(self, request, emit_log):
        """Registry order, with CFD hoisted ahead of THERMAL when it can feed it.

        The thermal surface coefficients can use a CFD-resolved free-stream
        speed, but only in forced flow. Under pure buoyancy the velocity is
        *caused* by the temperature field the thermal solve produces, so running
        CFD first against a cold board would hand thermal a velocity derived
        from the answer it has not computed yet. One-way coupling in that
        direction is only sound when a fan sets the flow independently of the
        board temperature.
        """
        descriptors = self.runnable_descriptors()
        ids = [descriptor.analysis_id for descriptor in descriptors]
        if "CFD" not in ids or "THERMAL" not in ids:
            return descriptors
        if ids.index("CFD") < ids.index("THERMAL"):
            return descriptors
        requests = request.domain_requests
        if "CFD" not in requests or "THERMAL" not in requests:
            return descriptors
        if not self._forced_flow_patches(requests["CFD"]):
            emit_log(
                "Enclosure flow is buoyancy-driven, so CFD keeps its usual place "
                "after THERMAL: its velocity depends on the temperature field "
                "rather than setting it."
            )
            return descriptors
        cfd = descriptors[ids.index("CFD")]
        reordered = [d for d in descriptors if d.analysis_id != "CFD"]
        reordered.insert(
            [d.analysis_id for d in reordered].index("THERMAL"), cfd,
        )
        emit_log("Forced enclosure flow: running CFD before THERMAL so its "
                 "free-stream velocity can drive the surface coefficients.")
        return reordered

    def _couple_cfd_into_thermal(self, request, domain_result, emit_log) -> None:
        """Hand the CFD free-stream speed, and its enclosure, to the pending
        thermal request."""
        thermal = request.domain_requests.get("THERMAL")
        if thermal is None or getattr(thermal, "air_velocity_m_s", None) is not None:
            return
        # Only forced flow is handed over, and only forced flow runs CFD ahead
        # of THERMAL (_order_for_cfd_coupling). Under buoyancy THERMAL has
        # already run by now: on P02 Rev E at 21:58 this still logged that
        # thermal "keeps its configured airflow", as if it were yet to run.
        # The enclosure itself was handed over before the run
        # (_hand_enclosure_to_thermal).
        if not self._forced_flow_patches(request.domain_requests.get("CFD")):
            return
        result = getattr(domain_result, "result", domain_result)
        velocity = float(getattr(result, "board_free_stream_velocity_m_s", 0.0) or 0.0)
        cells = int(getattr(result, "board_free_stream_cells", 0) or 0)
        if velocity <= 0.0 or cells <= 0:
            emit_log(
                "CFD produced no usable free-stream sample over the board "
                "(mesh too coarse to offer cells clear of every solid); "
                "thermal keeps its configured airflow."
            )
            return
        request.domain_requests["THERMAL"] = replace(
            request.domain_requests["THERMAL"], air_velocity_m_s=velocity,
        )
        emit_log(
            f"THERMAL will use the CFD free-stream speed of {velocity:.4g} m/s "
            f"sampled over {cells} cell(s) -- estimated, not measured."
        )

    # Which domains feed the EMI/EMC rules, and how their raw outcome is
    # reduced to the field EMCRunRequest expects.  EMC reads the other
    # domains' answers rather than recomputing them, so in a campaign it must
    # read the ones this campaign just produced.
    _EMC_INPUT_DOMAINS = ("AC", "DIFFERENTIAL", "THERMAL")

    @classmethod
    def _emc_ac_results(cls, domain_result, ac_request):
        """(rail, impedance result) pairs, taking the optimized sweep if there is one."""
        runs = getattr(domain_result, "runs", None)
        if runs is not None:
            return tuple(
                pair for item, result, optimization in runs
                for pair in cls._emc_ac_results((result, optimization), item)
            )
        result, optimization = domain_result
        final = optimization.optimized if optimization is not None else result
        if final is None:
            return ()
        rail = str(getattr(getattr(ac_request, "settings", None), "rail_name", "") or "")
        return ((rail or "Analysed rail", final),)

    @staticmethod
    def _emc_differential_results(domain_result, _request):
        """Per-pair results keyed by pair signature, as the EMC analyzer looks them up."""
        return {
            item.pair.signature: item
            for item in (domain_result.results or ())
            if getattr(item, "pair", None) is not None
        }

    @staticmethod
    def _emc_thermal_result(domain_result, _request):
        return domain_result.result

    def _couple_results_into_emc(self, request, produced, emit_log) -> None:
        """Fold this campaign's AC/differential/thermal outcomes into EMC's request.

        Standalone, EMC reads whatever the session last computed, which may be
        a different board revision or nothing at all.  Inside a campaign every
        domain describes the same board at the same moment, so the campaign's
        own outcomes are the ones EMC must see -- otherwise pressing "run
        everything" would grade the return paths against yesterday's sweep.

        A domain that did not run in this campaign leaves whatever the caller
        supplied alone: absent is absent, not empty.
        """
        emc = request.domain_requests.get("EMC")
        if emc is None:
            return
        reducers = {
            "AC": ("ac_results", self._emc_ac_results),
            "DIFFERENTIAL": ("differential_results", self._emc_differential_results),
            "THERMAL": ("thermal_result", self._emc_thermal_result),
        }
        updates = {}
        for analysis_id in self._EMC_INPUT_DOMAINS:
            if analysis_id not in produced:
                continue
            field_name, reduce = reducers[analysis_id]
            try:
                updates[field_name] = reduce(
                    produced[analysis_id], request.domain_requests.get(analysis_id),
                )
            except Exception as exc:
                # One unreadable outcome must not cost EMC the other two, nor
                # the run.  Say which, so the gap is visible in the report's
                # provenance rather than silently absent.
                emit_log(
                    f"EMC will not see the campaign's {analysis_id} outcome "
                    f"({type(exc).__name__}: {exc}); it keeps whatever the "
                    "request carried for it."
                )
        if not updates:
            return
        request.domain_requests["EMC"] = replace(emc, **updates)
        applied = sorted(
            analysis_id for analysis_id, (field_name, _reduce) in reducers.items()
            if field_name in updates
        )
        emit_log(
            "EMC will use this campaign's own " + ", ".join(applied)
            + " results rather than the session's previous ones."
        )

    def solve(self, request, emit_log, emit_progress, cancelled) -> CampaignResult:
        descriptors = self._run_order(request, emit_log)
        total = len(descriptors)
        outcomes: List[DomainOutcome] = []
        results: List[AnalysisResult] = []
        # Raw domain outcomes, kept only until the domain that consumes them
        # has run.  Scalars and cross-domain inputs are lifted off them here
        # rather than stored on a DomainOutcome: a CFDRunOutcome holds a mesh
        # and rendered plots, and retaining one per domain for the whole
        # campaign would keep every field alive until the report is built.
        produced: Dict[str, Any] = {}
        was_cancelled = False
        done = 0
        self._campaign_run_ids = {}

        for descriptor in descriptors:
            if cancelled():
                was_cancelled = True
                emit_log("Campaign cancelled; keeping completed domains.")
                break

            analysis_id = descriptor.analysis_id
            if analysis_id == "EMC":
                self._couple_results_into_emc(request, produced, emit_log)
                # EMC is the only consumer, so nothing needs these outcomes
                # after this point -- and one of them is a thermal mesh.
                produced.clear()
            emit_progress(done, total, analysis_id)
            outcome = self._run_domain(
                analysis_id, request, emit_log, emit_progress, cancelled,
                on_domain_result=(
                    lambda domain_result, analysis_id=analysis_id: self._handover(
                        analysis_id, request, domain_result, produced, emit_log,
                    )
                ),
            )
            done += 1
            outcomes.append(outcome)
            if outcome.result is not None:
                results.append(outcome.result)
            self._notify(outcome)

            if outcome.error and request.stop_on_error:
                emit_log(f"Stopping campaign after {analysis_id} failed (stop_on_error).")
                break

        if cancelled():
            was_cancelled = True

        produced.clear()
        campaign = CampaignResult.from_results(
            results,
            project_name=request.project_name,
            board_fingerprint=request.board_fingerprint,
            project_context=request.project_context,
            coverage=self._coverage(request, descriptors, outcomes, was_cancelled),
        )
        if was_cancelled:
            # recompute() derives the verdict from the domain scores, so the
            # cancelled state has to be stamped after aggregation or it is
            # overwritten by a PASS/WARN computed from a partial run.
            campaign.overall_status = AnalysisStatus.CANCELLED
        self.last_outcomes = outcomes
        emit_progress(done, total, "campaign complete")
        emit_log(
            f"Campaign finished: {len(results)} domain result(s), "
            + (f"{len(campaign.coverage)} selected that did not complete, "
               if campaign.coverage else "")
            + f"verdict {campaign.overall_status.value}."
        )
        return campaign

    @staticmethod
    def _coverage(request, descriptors, outcomes, was_cancelled) -> List[Dict[str, str]]:
        """Each selected domain that produced no result, in run order.

        A domain left out of domain_requests on purpose is not one: only
        those the caller could not prepare (request.unprepared), and those
        that were requested but failed, were skipped or never ran because
        the batch was cancelled.
        """
        reasons = {
            outcome.analysis_id: (
                ("FAILED", outcome.error) if outcome.error
                else ("SKIPPED", outcome.skipped_reason)
            )
            for outcome in outcomes
            if outcome.result is None and outcome.analysis_id in request.domain_requests
        }
        ran = {outcome.analysis_id for outcome in outcomes}
        coverage = []
        for descriptor in descriptors:
            analysis_id = descriptor.analysis_id
            if analysis_id in request.unprepared:
                state, reason = "NOT_PREPARED", request.unprepared[analysis_id]
            elif analysis_id in reasons:
                state, reason = reasons[analysis_id]
            elif was_cancelled and analysis_id in request.domain_requests and analysis_id not in ran:
                state, reason = "CANCELLED", "the batch was cancelled before it ran"
            else:
                continue
            coverage.append({"domain": analysis_id, "state": state, "reason": str(reason or "")})
        return coverage

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _dc_capture_mismatch(dc_request, other) -> str:
        """Why *other*'s DC capture would not reproduce *dc_request*'s, or ''."""
        if other is None:
            return ""

        def rails(req):
            return tuple(
                (
                    str(rail.net_name), float(getattr(rail, "nominal_voltage", 0.0) or 0.0),
                    round(sum(float(getattr(load, "total_current", 0.0) or 0.0) for load in rail.loads), 9),
                )
                for rail in getattr(req, "rails", ()) or ()
            )

        checks = (
            ("its rails differ", rails(dc_request), rails(other)),
            ("its DC grid differs", float(getattr(dc_request, "grid_size_mm", 0.0)),
             float(getattr(other, "grid_size_mm", 0.0))),
            ("it was captured from another board file", getattr(dc_request, "board_path", None),
             getattr(other, "board_path", None)),
        )
        return next((reason for reason, ours, theirs in checks if ours != theirs), "")

    def _share_dc_results(self, request, system_results, emit_log) -> None:
        """Hand the DC result to the thermal and CFD runs that need it.

        Both used to solve the same DC again from their own capture of the same
        board, in the same campaign, for the same losses. A coupled thermal run
        takes it too: its first pass is the ambient DC this result already is,
        and the iterations that follow re-solve against the temperature field
        from the same meshes, which they only read. On P02 Rev E that pass was
        87 s of a 349 s campaign. A run whose capture differs from the DC
        domain's -- other rails, grid or board file -- solves its own, and the
        log says why.
        """
        if not system_results:
            return
        dc_request = request.domain_requests.get("DC")
        for analysis_id in ("THERMAL", "CFD"):
            pending = request.domain_requests.get(analysis_id)
            if pending is None:
                continue
            settings = getattr(pending, "settings", None)
            # A coupled run needs the DC whatever its copper-loss setting says.
            if not (
                getattr(pending, "coupled", False)
                or getattr(settings, "include_dc_copper_losses", False)
            ):
                continue
            mismatch = self._dc_capture_mismatch(dc_request, getattr(pending, "dc_request", None))
            if mismatch:
                emit_log(f"{analysis_id} solves its own DC: {mismatch}.")
                continue
            request.domain_requests[analysis_id] = replace(
                pending, _dc_results=system_results,
                _dc_results_source="this campaign's DC result",
            )
            emit_log(
                f"{analysis_id} starts its coupling from this campaign's DC result "
                "rather than solving the ambient DC again."
                if getattr(pending, "coupled", False) else
                f"{analysis_id} will take its copper losses from this campaign's DC "
                "result rather than solving the DC again."
            )

    def _handover(self, analysis_id, request, domain_result, produced, emit_log) -> None:
        """Everything that has to happen with a raw outcome before it is dropped."""
        if analysis_id == "DC":
            self._share_dc_results(request, domain_result, emit_log)
        if analysis_id == "CFD":
            self._couple_cfd_into_thermal(request, domain_result, emit_log)
        if analysis_id in self._EMC_INPUT_DOMAINS and "EMC" in request.domain_requests:
            produced[analysis_id] = domain_result
        self._notify_raw(analysis_id, domain_result)

    def _notify(self, outcome: DomainOutcome) -> None:
        if self._domain_listener is None:
            return
        try:
            self._domain_listener(outcome)
        except Exception:
            # A listener raising must not abort the campaign it is observing.
            pass

    def _notify_raw(self, analysis_id: str, domain_result: Any) -> None:
        if self._raw_listener is None:
            return
        try:
            self._raw_listener(analysis_id, domain_result)
        except Exception:
            # Same rule as _notify: an observer must not abort the campaign.
            # It runs before the adapter, so letting it raise here would also
            # turn a successful solve into a failed domain.
            pass

    def _run_domain(self, analysis_id, request, emit_log, emit_progress, cancelled,
                    on_domain_result=None) -> DomainOutcome:
        domain_request = request.domain_requests.get(analysis_id)
        if domain_request is None:
            emit_log(f"{analysis_id}: skipped (no request provided).")
            return DomainOutcome(analysis_id, None, skipped_reason="no request provided")

        engine = self._engines.get(analysis_id)
        if engine is None:
            emit_log(f"{analysis_id}: skipped (no engine registered).")
            return DomainOutcome(analysis_id, None, skipped_reason="no engine registered")

        digest = ""
        if self._cache is not None:
            from application.campaign_cache import UncomparableRequest, configuration_digest
            from build_info import tool_identity

            # Digested as the domain starts, after the earlier domains handed
            # it their results, with the runs those came from and the build.
            try:
                digest = configuration_digest(domain_request, context=(
                    tool_identity(), sorted(self._campaign_run_ids.items()),
                ))
            except UncomparableRequest as exc:
                emit_log(f"{analysis_id}: solved again, its inputs cannot be compared ({exc}).")
            cached = self._cache.get(request.board_fingerprint, analysis_id, digest) if digest else None
            if cached is not None:
                # A copy: the campaign annotates the results it aggregates.
                cached = copy.deepcopy(cached)
                emit_log(
                    f"{analysis_id}: reused the result completed at {cached.completed_at}, "
                    "its inputs unchanged."
                )
                self._run_ids[analysis_id] = cached.run_id
                self._campaign_run_ids[analysis_id] = cached.run_id
                if on_domain_result is not None:
                    # A cached domain still has to hand its raw outcome to
                    # the domains that couple to it -- DC's rails feed
                    # THERMAL/CFD, and AC/DIFFERENTIAL/THERMAL feed EMC
                    # (_handover). Returning here without this used to
                    # leave a re-run's later domains solving against no
                    # handover at all whenever their input was a cache hit.
                    cached_raw = self._cache.get_domain_result(
                        request.board_fingerprint, analysis_id, digest,
                    )
                    if cached_raw is not None:
                        on_domain_result(cached_raw)
                return DomainOutcome(analysis_id, cached, from_cache=True)

        started = time.perf_counter()
        run_id = uuid.uuid4().hex
        self._run_ids[analysis_id] = run_id
        self._campaign_run_ids[analysis_id] = run_id
        try:
            domain_result = engine.solve(
                domain_request,
                lambda message: emit_log(f"{analysis_id}: {message}"),
                emit_progress,
                cancelled,
            )
            if on_domain_result is not None:
                on_domain_result(domain_result)
            adapter = self._adapters.get(analysis_id)
            if adapter is None:
                raise KeyError(f"No adapter registered for analysis {analysis_id}")
            result = adapter(domain_result, domain_request)
            result.run_id = run_id
        except Exception as exc:
            elapsed = time.perf_counter() - started
            emit_log(f"{analysis_id}: FAILED after {elapsed:.2f}s -- {exc}")
            return DomainOutcome(
                analysis_id, None, error=f"{type(exc).__name__}: {exc}",
                elapsed_seconds=elapsed,
            )

        elapsed = time.perf_counter() - started
        self._stamp(result, request, elapsed)
        if self._cache is not None and digest:
            self._cache.put(
                request.board_fingerprint, analysis_id, digest, result, domain_result,
            )
        emit_log(f"{analysis_id}: completed in {elapsed:.2f}s ({result.status.value}).")
        return DomainOutcome(analysis_id, result, elapsed_seconds=elapsed)

    @staticmethod
    def _stamp(result: AnalysisResult, request, elapsed: float) -> None:
        """Fill campaign-level provenance the domain adapter could not know."""
        # The status rule the synthesis table applies. The DC adapter WARNs
        # on any finding: P02 Rev E, 2026-09-29 17:15, DC logged "completed
        # (WARN)" and headed its report section WARN on eight INFO DC-006,
        # while the synthesis scored it PASS 100/100.
        if result.status in (AnalysisStatus.PASS, AnalysisStatus.WARN, AnalysisStatus.FAIL):
            result.status = domain_status_from_findings(result.findings)
        if not result.board_fingerprint and request.board_fingerprint:
            result.board_fingerprint = request.board_fingerprint
        if not result.elapsed_seconds:
            result.elapsed_seconds = elapsed
        if not result.completed_at:
            result.finish()


def reuse_dc_capture(dc_request, rails, grid_size_mm, board_path):
    """The DC domain's capture, fit for a thermal or CFD run, or None.

    A batch prepares every domain on the UI thread before the first solve,
    and the thermal and CFD builders each captured the DC copper again from
    the same board a few seconds after the DC domain had: on P02 Rev E at
    21:52, 17 rails three times, about 3 s of frozen editor each, for captures
    _share_dc_results then compared equal and discarded. When the rails, grid
    and board file match -- the same test that hands over the DC result --
    the DC domain's capture is shared instead, without its drop budget or its
    grid check, which a copper-loss solve does not make.
    """
    if dc_request is None:
        return None
    probe = type("DCCaptureProbe", (), {})()
    probe.rails = tuple(rails)
    probe.grid_size_mm = float(grid_size_mm)
    probe.board_path = None if board_path is None else str(board_path)
    if CampaignEngine._dc_capture_mismatch(dc_request, probe):
        return None
    return replace(dc_request, maximum_drop_pct=None, grid_sensitivity_check=False)


class CampaignController(BackgroundAnalysisController):
    """Own one cancellable background campaign and marshal its callbacks."""

    def __init__(self, dispatch=lambda callback, *args: callback(*args), engine=None):
        super().__init__(
            engine if engine is not None else CampaignEngine({}),
            thread_name="KiPIDA-Campaign",
            busy_message="A campaign is already running.",
            cancelled_error_factory=lambda: CampaignCancelled("Campaign cancelled."),
            dispatch=dispatch,
        )

    def run_id_for(self, analysis_id: str) -> str:
        reader = getattr(self._engine, "run_id_for", None)
        return reader(analysis_id) if callable(reader) else ""

    def start(self, request, callbacks) -> None:
        listener = getattr(callbacks, "on_domain_complete", None)
        setter = getattr(self._engine, "set_domain_listener", None)
        if listener is not None and callable(setter):
            setter(lambda outcome: self._emit(listener, outcome))
        raw_listener = getattr(callbacks, "on_domain_result", None)
        raw_setter = getattr(self._engine, "set_raw_result_listener", None)
        if raw_listener is not None and callable(raw_setter):
            raw_setter(
                lambda analysis_id, domain_result:
                    self._emit(raw_listener, analysis_id, domain_result)
            )
        super().start(request, callbacks)

    def _run(self, request, callbacks) -> None:
        """Deliver a cancelled campaign instead of discarding it.

        The base controller treats cancellation as an error, which is right
        for a single analysis: half a thermal solve is not a temperature
        field.  A campaign is the opposite -- it exists to isolate domains
        from one another, and CampaignEngine already stamps the partial result
        CANCELLED and logs "keeping completed domains".  Raising here threw
        those completed domains away, so cancelling after five of six
        analyses lost all five.
        """
        try:
            campaign = self._engine.solve(
                request,
                lambda message: self._emit(callbacks.on_log, message),
                lambda *args: self._emit(callbacks.on_progress, *args),
                self._cancel_event.is_set,
            )
            self._emit(callbacks.on_complete, campaign)
        except Exception as exc:
            self._emit(callbacks.on_error, exc)
