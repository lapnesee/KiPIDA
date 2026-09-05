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
import time
from typing import Any, Callable, Dict, List, Optional

from analysis_contract import AnalysisResult, AnalysisStatus
from analysis_registry import DEFAULT_ANALYSES
from campaign import CampaignResult
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
    on_complete: Callable[[CampaignResult], None] = lambda campaign: None
    on_error: Callable[[Exception], None] = lambda exc: None


# ---------------------------------------------------------------------------
# Default adapters
# ---------------------------------------------------------------------------
#
# The per-domain adapters in analysis_adapters do not share a signature: each
# takes whatever extra context its domain needs (a target percentage, a
# stackup, a settings object).  These wrappers normalise them to one shape --
# ``callable(domain_result, request) -> AnalysisResult`` -- by pulling the
# extra arguments off the domain request.
#
# Two classes of extra argument, treated differently on purpose:
#
#   * Arguments the underlying adapter declares WITH a default
#     (``adapt_ac_result(..., optimization=None, settings=None)``,
#     ``adapt_thermal_result(..., coupled=False, elapsed_seconds=0.0)``) are
#     genuinely optional.  Falling back to that same default is correct.
#
#   * Arguments the adapter declares as required positionals carry no safe
#     default, and inventing one silently corrupts the verdict.  The clearest
#     case is ``maximum_drop_pct``: substituting 0.0 turns
#     ``if drop_pct > maximum_drop_pct`` into "every rail with any drop at
#     all", so a clean board would report one HIGH "exceeds the voltage-drop
#     target" finding per rail, each claiming a 0.000% budget that nobody set.
#     A campaign full of confident, fabricated failures is worse than one that
#     says it was not configured.
#
# So required context is demanded, not defaulted.  ``CampaignEngine`` isolates
# per-domain failures, so a missing value surfaces as one honest DomainOutcome
# error naming exactly what to supply, while the other domains still run.
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
    return analysis_adapters.adapt_dc_result(
        domain_result, _require(request, "maximum_drop_pct", "DC"),
    )


def _adapt_ac(domain_result, request) -> AnalysisResult:
    # Both extras are optional in adapt_ac_result's own signature.
    return analysis_adapters.adapt_ac_result(
        domain_result,
        getattr(request, "optimization", None),
        getattr(request, "settings", None),
    )


def _adapt_differential(domain_result, request) -> AnalysisResult:
    return analysis_adapters.adapt_differential_result(
        domain_result,
        _require(request, "stackup", "DIFFERENTIAL"),
        _require(request, "tolerance_pct", "DIFFERENTIAL"),
    )


def _adapt_emc(domain_result, request) -> AnalysisResult:
    return analysis_adapters.adapt_emc_result(
        _require(request, "settings", "EMC"), domain_result,
    )


def _adapt_thermal(domain_result, request) -> AnalysisResult:
    # Both extras are optional in adapt_thermal_result's own signature.
    return analysis_adapters.adapt_thermal_result(
        domain_result,
        getattr(request, "coupled", False),
        getattr(request, "elapsed_seconds", 0.0),
    )


def _adapt_cfd(domain_result, request) -> AnalysisResult:
    return analysis_adapters.adapt_cfd_result(
        _require(request, "mesh", "CFD"), domain_result,
    )


DEFAULT_ADAPTERS: Dict[str, Callable[[Any, Any], AnalysisResult]] = {
    "DC": _adapt_dc,
    "AC": _adapt_ac,
    "DIFFERENTIAL": _adapt_differential,
    "EMC": _adapt_emc,
    "THERMAL": _adapt_thermal,
    "CFD": _adapt_cfd,
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

    def set_domain_listener(self, listener: Optional[Callable[[DomainOutcome], None]]) -> None:
        """Receive each DomainOutcome as it completes.

        The base controller only forwards ``on_log``/``on_progress`` to an
        engine, so per-domain completion is wired here rather than through the
        callback bundle.
        """
        self._domain_listener = listener

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
        """Hand the CFD free-stream speed to the pending thermal request."""
        thermal = request.domain_requests.get("THERMAL")
        if thermal is None or getattr(thermal, "air_velocity_m_s", None) is not None:
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
            thermal, air_velocity_m_s=velocity,
        )
        emit_log(
            f"THERMAL will use the CFD free-stream speed of {velocity:.4g} m/s "
            f"sampled over {cells} cell(s) -- estimated, not measured."
        )

    def solve(self, request, emit_log, emit_progress, cancelled) -> CampaignResult:
        descriptors = self._order_for_cfd_coupling(request, emit_log)
        total = len(descriptors)
        outcomes: List[DomainOutcome] = []
        results: List[AnalysisResult] = []
        was_cancelled = False
        done = 0

        for descriptor in descriptors:
            if cancelled():
                was_cancelled = True
                emit_log("Campaign cancelled; keeping completed domains.")
                break

            analysis_id = descriptor.analysis_id
            emit_progress(done, total, analysis_id)
            outcome = self._run_domain(
                analysis_id, request, emit_log, emit_progress, cancelled,
                # Scalars are lifted off the raw result here rather than stored
                # on the outcome: a CFDRunOutcome holds a mesh and rendered
                # plots, and retaining one per domain for the whole campaign
                # would keep every field alive until the report is built.
                on_domain_result=(
                    (lambda result: self._couple_cfd_into_thermal(request, result, emit_log))
                    if analysis_id == "CFD" else None
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

        campaign = CampaignResult.from_results(
            results,
            project_name=request.project_name,
            board_fingerprint=request.board_fingerprint,
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
            f"verdict {campaign.overall_status.value}."
        )
        return campaign

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _notify(self, outcome: DomainOutcome) -> None:
        if self._domain_listener is None:
            return
        try:
            self._domain_listener(outcome)
        except Exception:
            # A listener raising must not abort the campaign it is observing.
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
            from application.campaign_cache import configuration_digest

            digest = configuration_digest(domain_request)
            cached = self._cache.get(request.board_fingerprint, analysis_id, digest)
            if cached is not None:
                emit_log(f"{analysis_id}: reused cached result (inputs unchanged).")
                return DomainOutcome(analysis_id, cached, from_cache=True)

        started = time.perf_counter()
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
        except Exception as exc:
            elapsed = time.perf_counter() - started
            emit_log(f"{analysis_id}: FAILED after {elapsed:.2f}s -- {exc}")
            return DomainOutcome(
                analysis_id, None, error=f"{type(exc).__name__}: {exc}",
                elapsed_seconds=elapsed,
            )

        elapsed = time.perf_counter() - started
        self._stamp(result, request, elapsed)
        if self._cache is not None:
            self._cache.put(request.board_fingerprint, analysis_id, digest, result)
        emit_log(f"{analysis_id}: completed in {elapsed:.2f}s ({result.status.value}).")
        return DomainOutcome(analysis_id, result, elapsed_seconds=elapsed)

    @staticmethod
    def _stamp(result: AnalysisResult, request, elapsed: float) -> None:
        """Fill campaign-level provenance the domain adapter could not know."""
        if not result.board_fingerprint and request.board_fingerprint:
            result.board_fingerprint = request.board_fingerprint
        if not result.elapsed_seconds:
            result.elapsed_seconds = elapsed
        if not result.completed_at:
            result.finish()


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

    def start(self, request, callbacks) -> None:
        listener = getattr(callbacks, "on_domain_complete", None)
        setter = getattr(self._engine, "set_domain_listener", None)
        if listener is not None and callable(setter):
            setter(lambda outcome: self._emit(listener, outcome))
        super().start(request, callbacks)
