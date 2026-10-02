"""Standalone HTML campaign report.

Everything is inline: CSS in a <style> block, images as base64 data URIs,
collapsible sections via native <details>.  No external assets, no CDN, no
JavaScript -- the file opens over file:// on a machine with no network and
renders identically.

Every number carried by a finding is shown next to its confidence label.
The result contract has always recorded whether a value is DETERMINISTIC,
MEASURED, ESTIMATED or HEURISTIC; a report that hides that distinction
invites the reader to treat an estimate as a measurement.

The first section answers the reader's question -- can the board go to
manufacture -- in lists a beginner can act on (audit of 2026-10-01, R-LE-1
to R-LE-7): what to fix, what to review, what only the prototype settles,
what data is missing, how far the calculation can be trusted, and what
holds. Each action then gets a card: why it is a risk, what to do, how to
check, how sure the tool is.

The report's own words and the rule guide follow the interface language
(i18n); what the analyses wrote -- finding titles, descriptions, figure
labels -- stays as they wrote it. A label is translated with ``_()`` outside
any f-string: the catalog extractor does not read inside f-strings.
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import base64
import html
import json
import re

from analysis_contract import (
    AnalysisFinding,
    AnalysisMetric,
    AnalysisResult,
    AnalysisStatus,
    FindingSeverity,
    Remediation,
    RemediationEffort,
)
from campaign import (
    AIR_AGREEMENT_K, DATA, FIX, GROUPING_MAX_COMPONENTS, PROTOTYPE, REVIEW, CampaignAction,
    CampaignResult, DomainScore, action_bucket,
)
from i18n import N_, _, current_language
from rule_guide import CONFIRMATIONS, FindingNature, finding_nature, guide

_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}

_STATUS_CLASS = {
    AnalysisStatus.PASS: "pass",
    AnalysisStatus.WARN: "warn",
    AnalysisStatus.FAIL: "fail",
    AnalysisStatus.NO_DATA: "nodata",
    AnalysisStatus.CANCELLED: "nodata",
    AnalysisStatus.ERROR: "fail",
}

_SEVERITY_CLASS = {
    FindingSeverity.CRITICAL: "sev-critical",
    FindingSeverity.HIGH: "sev-high",
    FindingSeverity.MEDIUM: "sev-medium",
    FindingSeverity.LOW: "sev-low",
    FindingSeverity.INFO: "sev-info",
}

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       margin: 0; padding: 0 1.5rem 4rem; line-height: 1.5; color: #1b1f23;
       background: #fff; max-width: 68rem; margin-inline: auto; }
h1 { font-size: 1.7rem; margin: 2rem 0 0.25rem; }
h2 { font-size: 1.3rem; margin: 2.5rem 0 0.75rem; padding-bottom: 0.3rem;
     border-bottom: 2px solid #d8dee4; }
h3 { font-size: 1.05rem; margin: 1.5rem 0 0.5rem; }
table { border-collapse: collapse; width: 100%; margin: 0.5rem 0 1rem; font-size: 0.9rem; }
th, td { border: 1px solid #d8dee4; padding: 0.35rem 0.55rem; text-align: left;
         vertical-align: top; }
th { background: #f2f4f6; font-weight: 600; }
code, pre { font-family: ui-monospace, "SFMono-Regular", Consolas, monospace;
            font-size: 0.85em; }
pre { background: #f6f8fa; padding: 0.6rem; overflow-x: auto; border-radius: 4px; }
details { margin: 0.4rem 0; }
summary { cursor: pointer; font-weight: 600; padding: 0.25rem 0; }
.verdict { display: inline-block; padding: 0.3rem 0.9rem; border-radius: 999px;
           font-weight: 700; letter-spacing: 0.03em; }
.pass { background: #d5f2dd; color: #14512b; }
.warn { background: #fdf0cd; color: #6b4c00; }
.fail { background: #fbd9d9; color: #7a1420; }
.nodata { background: #e8eaed; color: #4a5057; }
.scorebar { background: #eceff1; border-radius: 3px; height: 0.85rem;
            position: relative; overflow: hidden; min-width: 8rem; }
.scorebar > span { display: block; height: 100%; background: #4a90d9; }
.scorebar.pass > span { background: #35a35f; }
.scorebar.warn > span { background: #d9a441; }
.scorebar.fail > span { background: #cf4b53; }
.badge { display: inline-block; padding: 0.05rem 0.45rem; border-radius: 3px;
         font-size: 0.75rem; font-weight: 700; letter-spacing: 0.02em; }
.sev-critical { background: #7a1420; color: #fff; }
.sev-high { background: #cf4b53; color: #fff; }
.sev-medium { background: #d9a441; color: #241a00; }
.sev-low { background: #9fb3c8; color: #10202e; }
.sev-info { background: #e8eaed; color: #4a5057; }
.confidence { display: inline-block; margin-left: 0.4rem; padding: 0.02rem 0.4rem;
              border: 1px solid #b9c2cc; border-radius: 3px; font-size: 0.72rem;
              color: #4a5057; background: #f8fafc; white-space: nowrap; }
.action { border: 1px solid #d8dee4; border-left: 4px solid #4a90d9;
          border-radius: 4px; padding: 0.8rem 1rem; margin: 1rem 0; }
.action.sev-critical-border { border-left-color: #7a1420; }
.action.sev-high-border { border-left-color: #cf4b53; }
.action.sev-medium-border { border-left-color: #d9a441; }
.verified { background: #d5f2dd; color: #14512b; border: 1px solid #35a35f; }
.unverified { background: #fdf0cd; color: #6b4c00; border: 1px solid #d9a441; }
.missing-artifact { border: 1px dashed #cf4b53; background: #fdf3f3; color: #7a1420;
                    padding: 0.7rem 0.9rem; border-radius: 4px; margin: 0.6rem 0;
                    font-size: 0.88rem; }
figure { margin: 0.8rem 0; }
figure img { max-width: 100%; height: auto; border: 1px solid #d8dee4; border-radius: 3px; }
figcaption { font-size: 0.85rem; color: #4a5057; margin-top: 0.3rem; }
.meta { color: #4a5057; font-size: 0.9rem; }
.muted { color: #6b737b; font-style: italic; }
ul.tight { margin: 0.3rem 0; padding-left: 1.2rem; }
ul.decision { margin: 0.3rem 0 0.8rem; padding-left: 1.2rem; }
ul.decision li { margin: 0.2rem 0; }
dl.card { margin: 0.5rem 0; display: grid; grid-template-columns: 11rem 1fr; gap: 0.3rem 1rem; }
dl.card dt { font-weight: 600; }
dl.card dd { margin: 0; }
dl.card dd p { margin: 0; }
dl.glossary dt { font-weight: 600; margin-top: 0.5rem; }
dl.glossary dd { margin: 0 0 0 1rem; }
details.guide { border: 1px solid #d8dee4; border-radius: 4px; padding: 0.3rem 0.8rem;
                margin: 1rem 0; background: #f8fafc; }
nav.toc { font-size: 0.9rem; margin: 0.5rem 0 1rem; }
nav.toc a { margin-right: 1rem; }
@media print {
  body { max-width: none; padding: 0; }
  nav.toc { display: none; }
  details::details-content { content-visibility: visible; display: block; }
  .action, figure, tr { break-inside: avoid; }
  h2, h3, h4 { break-after: avoid; }
}
"""


def _local_time(stamp: str) -> str:
    """An ISO UTC stamp as the reader's local time, with its offset.

    The report and its files are named after local time; a header reading
    20:39 UTC over a file named 223902 looks like two different runs.
    Anything that does not parse is shown as recorded.
    """
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return stamp
    if moment.tzinfo is None:
        return stamp
    local = moment.astimezone()
    offset = local.strftime("%z")
    return f"{local:%Y-%m-%d %H:%M:%S} (UTC{offset[:3]}:{offset[3:]})"


def _esc(value: Any) -> str:
    """Escape any value for HTML text/attribute context.

    Net and component names come out of user project files and routinely
    contain ``<``, ``&`` and quotes; nothing reaches the document unescaped.
    """
    return html.escape("" if value is None else str(value), quote=True)


def _join(values: List[str], empty: str = "&mdash;") -> str:
    return ", ".join(_esc(item) for item in values) if values else empty


def _headers(*labels: str) -> str:
    """A table head from labels already translated."""
    return "<thead><tr>" + "".join(f"<th>{_esc(label)}</th>" for label in labels) + "</tr></thead>"


def _confidence_badge(finding: AnalysisFinding) -> str:
    badge = f'<span class="confidence">{_esc(finding.confidence)}</span>'
    if finding.calculation_quality:
        # COH-9: a self-diagnostic about the analysis (mesh, solver limit),
        # not a defect on the board; it still stays in this table -- the
        # score just does not charge it (campaign.score_findings).
        label = _esc(_("CALCULATION QUALITY"))
        badge += f' <span class="confidence muted">{label}</span>'
    return badge


def _number(value: Optional[float], unit: str = "", precision: Optional[int] = None) -> str:
    """The one formatter for every value this report prints (COH-12).

    An explicit precision is honoured as given. Otherwise a value under
    1e-6 in magnitude prints as "< 1e-6" (or "0" when it is exactly zero)
    rather than raw floating-point noise around zero -- P02 Rev E printed
    "8.074349270001136e-14 %" and "1.03e-09 A/mm2" this way -- and
    everything else prints to 3 significant digits (audit R-LE-7: P02 Rev
    E's worst drop read "0.003015 %"). From 1,000 up, 3 significant digits
    need an exponent, and P02 Rev E's 1,530 EMC checks read "1.53e+03": a
    whole number prints whole, anything else rounded to 3 digits, both with
    a narrow space between thousands.
    """
    if value is None:
        return "&mdash;"
    if precision is not None:
        text = f"{value:.{precision}f}"
    elif value == 0.0:
        text = "0"
    elif abs(value) < 1e-6:
        text = "< 1e-6" if value > 0 else "> -1e-6"
    else:
        text = f"{value:.3g}"
        if "e+" in text:
            whole = value if float(value).is_integer() else float(text)
            text = f"{whole:,.0f}".replace(",", " ")
    return f"{_esc(text)}&nbsp;{_esc(unit)}" if unit else _esc(text)


# A frequency reads in its own prefix: AC's worst frequency and target
# corner printed "3.83e+04 Hz" and "3.5e+07 Hz" on P02 Rev E, 38.3 kHz and
# 35 MHz.
_FREQUENCY_PREFIXES = ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz"))


def _scaled(value: float, unit: str):
    """(value, unit) with a frequency in kHz, MHz or GHz from 1 kHz up."""
    if unit == "Hz":
        for factor, prefixed in _FREQUENCY_PREFIXES:
            if abs(value) >= factor:
                return value / factor, prefixed
    return value, unit


_LAYER_POSITION_RE = re.compile(r"^(F|B|In\d+)\.(.+)$", re.IGNORECASE)


def _layer_name(raw: str) -> str:
    """Show a copper layer the same way wherever this report names one.

    DC-004 named a layer by its canonical KiCad id ("F.Cu") while other
    domains read the board's stackup and reported the designer's own name
    for the same physical layer ("F.Signal") -- COH-12. Preferring the given
    name with the canonical id once in parentheses ("F.Signal (F.Cu)") keeps
    both without duplicating a name that is already canonical, and lets a
    reader tell whether two findings from different domains are the same
    layer or not.
    """
    text = str(raw or "").strip()
    match = _LAYER_POSITION_RE.match(text)
    if not match:
        return text
    canonical = f"{match.group(1)}.Cu"
    if match.group(2).lower() == "cu" or text == canonical:
        return canonical
    if text.endswith(f"({canonical})"):
        return text
    return f"{text} ({canonical})"


def _resolve_artifact(raw_path: str, search_roots) -> Optional[Path]:
    """Locate an artifact file, tolerating a bare recorded filename.

    ProjectResultsHistory records ``AnalysisArtifact.path`` as just the file
    name and stores the file inside a per-entry subdirectory, so resolving it
    against the process working directory (KiCad's, in practice) always misses.
    Try the path as given, then each search root, then a recursive search
    under each root by file name.
    """
    if not raw_path:
        return None
    candidate = Path(raw_path)
    if candidate.is_file():
        return candidate
    if candidate.is_absolute():
        return None
    for root in search_roots or ():
        root = Path(root)
        direct = root / candidate
        if direct.is_file():
            return direct
        try:
            found = next(root.rglob(candidate.name), None)
        except OSError:
            found = None
        if found is not None and found.is_file():
            return found
    return None


def _data_uri(path: Path) -> Optional[str]:
    media_type = _IMAGE_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        return None
    try:
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None
    return f"data:{media_type};base64,{payload}"


# ----------------------------------------------------------------------
# Reading aids: plain words for the labels, one notation for units
# ----------------------------------------------------------------------

_CERTAINTY_WORDS = {
    "DETERMINISTIC": N_("Fact: an exact count or the state of the run"),
    "MEASURED": N_("Measured"),
    "DATASHEET_BACKED": N_("Rests on datasheet values"),
    "ESTIMATED": N_("Estimate from a model of the board"),
    "HEURISTIC": N_("Rule of thumb: a threshold this tool sets"),
}

_EFFORT_WORDS = {
    RemediationEffort.LOW: N_("Low: move a part, add a via or fill in a field"),
    RemediationEffort.MEDIUM: N_("Medium: reroute or re-place a group of parts"),
    RemediationEffort.HIGH: N_("High: change the stackup or a plane"),
}

# The analyses write "118.99 C", "85.2 ohm" and "mm2"; P02 Rev E's report of
# 2026-10-01 had 34 temperatures as "C" against 6 as "°C", 16 "ohm" and no
# "Ω" (audit LE-8). One notation, applied where the report prints text. A
# "C" counts as a temperature only after a number and a space, so I2C, C37
# and C0G are left alone.
_UNIT_PATTERNS = (
    (re.compile(r"(?<=\d) C\b"), " °C"),
    (re.compile(r"(?<![A-Za-z])([mkM]?)ohms?\b"), r"\1Ω"),
    (re.compile(r"\bmm2\b"), "mm²"),
)


def _units(text: Any) -> str:
    text = "" if text is None else str(text)
    for pattern, replacement in _UNIT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _t(value: Any) -> str:
    """Prose an analysis wrote: one unit notation, then escaped."""
    return _esc(_units(value))


def _tr(text: str) -> str:
    """Prose this report or the rule guide wrote: translated, then as _t."""
    return _t(_(text)) if text else ""


def _severity_badge(severity: FindingSeverity) -> str:
    return f'<span class="badge {_SEVERITY_CLASS[severity]}">{_esc(severity)}</span>'


_HOW_TO_READ = (
    (N_("What this page answers"),
     N_("Whether the board can go to manufacture. Each item below is one physical problem, "
        "even when several analyses report it, and links to its card in section 2.")),
    (N_("Did not complete"),
     N_("An analysis you selected that produced no result: it could not be prepared, it "
        "failed, or the batch was cancelled first. The verdict says nothing of what it "
        "covers, and is never PASS while it is listed.")),
    (N_("Fix before manufacture"),
     N_("A defect the analyses establish on the board itself, such as a broken connection or "
        "a voltage drop beyond its limit.")),
    (N_("Review before manufacture"),
     N_("A smaller defect, or a margin worth a second look; decide whether to change the "
        "board.")),
    (N_("Check on the prototype"),
     N_("A layout rule of thumb is not met. Only a measurement on the prototype, such as an "
        "emissions scan or an ESD test, settles whether it matters. Improving the layout "
        "first is cheaper than a respin.")),
    (N_("Data to complete"),
     N_("An input the analyses needed and did not have. The verdict sharpens once it is "
        "filled in.")),
    (N_("Calculation reliability"),
     N_("A limit of the calculation, not of the board: a mesh too coarse, a solve that did "
        "not settle. It takes nothing off the scores and is not an action; it says how far "
        "to trust the figures it names.")),
    (N_("What holds"),
     N_("Checks that passed. Read them as what the analyses covered, not as a guarantee.")),
)

_GLOSSARY = (
    (N_("Severity"),
     N_("CRITICAL: the board cannot work as designed. HIGH: likely to fail a requirement. "
        "MEDIUM: a real risk with some margin. LOW: minor. INFO: a note, never an action.")),
    (N_("Certainty"),
     N_("What a finding rests on. A fact (an exact count or the state of the run), a "
        "measurement, datasheet values, an estimate from a model of the board, or a rule of "
        "thumb (a threshold this tool sets).")),
    (N_("Status"),
     N_("Per analysis. FAIL on a CRITICAL finding, or a HIGH one resting on a fact, a "
        "measurement or a datasheet. WARN on any other HIGH, or a MEDIUM. PASS otherwise. "
        "A limit of the calculation gives WARN at most. The verdict at the top fails as soon "
        "as one item is to fix before manufacture.")),
    (N_("Score"),
     N_("100 minus a penalty per finding, weighted by severity and certainty. It summarises; "
        "it is not a margin. The EMC score is the analysis's own uncalibrated risk figure.")),
    (N_("Priority"),
     N_("Orders the actions: severity, times certainty, times 1 plus 0.5 for each further "
        "analysis reporting the problem, divided by the typical effort. It has no unit; only "
        "the order means something.")),
    (N_("Margin"),
     N_("The distance between a value and its limit, such as 25 K under a 150 °C junction "
        "limit.")),
    (N_("Junction temperature (Tj)"),
     N_("The temperature of the silicon inside a package, estimated from the board "
        "temperature and the part's thermal resistance.")),
    (N_("θJB, ψJB"),
     N_("Datasheet figures linking the junction to the board 1 mm from the package (JEDEC "
        "JESD51-8 and -12).")),
    (N_("Derating"),
     N_("How much of its rating (current, power, voltage) a part keeps at a higher "
        "temperature, read off its datasheet curve.")),
    (N_("PDN, target impedance"),
     N_("The power distribution network, from regulator to load. Below its target "
        "impedance, a rail holds its voltage when the load's current steps.")),
    (N_("Decoupling capacitor"),
     N_("A capacitor at a power pin that supplies the pin's fast current locally.")),
    (N_("Return path, reference plane"),
     N_("Every signal current comes back through the plane under it. A gap or a layer "
        "change without a via forces a detour, and the loop radiates.")),
    (N_("Hot loop, switching node"),
     N_("In a switching converter, the loop through the input capacitor and the switches "
        "carries the fastest current edges, and the switching node swings at full input "
        "voltage. Both radiate in proportion to their area.")),
    (N_("Harmonic, emissions band"),
     N_("A switching edge carries energy at every multiple of the switching frequency. "
        "Standards limit what a product radiates in set bands.")),
    (N_("ESD, TVS"),
     N_("An electrostatic discharge enters through a connector. A TVS diode clamps it to "
        "ground, if its ground path is short.")),
    (N_("Stitching via"),
     N_("A via joining ground planes on different layers, so that they act as one at high "
        "frequency.")),
    (N_("Mesh, grid step"),
     N_("The analyses cut the board into small cells. A result that moves when the cells "
        "are halved depends on the mesh, not only on the copper.")),
)


def _render_how_to_read() -> str:
    sections = "".join(
        f"<dt>{_tr(term)}</dt><dd>{_tr(text)}</dd>" for term, text in _HOW_TO_READ
    )
    glossary = "".join(
        f"<dt>{_tr(term)}</dt><dd>{_tr(text)}</dd>" for term, text in _GLOSSARY
    )
    summary = _esc(_("How to read this report"))
    heading = _esc(_("Glossary"))
    return (
        f'<details class="guide"><summary>{summary}</summary>'
        f'<dl class="glossary">{sections}</dl>'
        f"<h4>{heading}</h4>"
        f'<dl class="glossary">{glossary}</dl></details>'
    )


# ----------------------------------------------------------------------
# Section 1 -- decision
# ----------------------------------------------------------------------

_VERDICT_WORDS = {
    AnalysisStatus.FAIL: N_("Do not release for manufacture yet: at least one defect must be "
                            "fixed first."),
    AnalysisStatus.WARN: N_("Review before manufacture: the items below need a decision."),
    AnalysisStatus.PASS: N_("No defect found by the analyses that ran."),
    AnalysisStatus.NO_DATA: N_("No analysis produced a result."),
}


def _where(components: List[str], nets: List[str], more: int = 0) -> str:
    """Parts, then nets; *more* parts beyond those listed are counted with
    the parts, not after the nets as P02 Rev E's 16:51 report printed them."""
    parts = []
    if components:
        listed = _join(components)
        if more > 0:
            listed += " " + _esc(_("and {count} more")).format(count=more)
        parts.append(listed)
    if nets:
        # "net" alone: the extractor skips a template with under four
        # letters outside its fields, and "net {nets}" has three.
        parts.append(_esc(_("net")) + " " + _join(nets))
    return " &middot; ".join(parts)


def _decision_item(action: CampaignAction) -> str:
    where = _where(action.components, action.nets)
    return (
        f"<li>{_severity_badge(action.severity)} "
        f'<a href="#{_esc(action.action_id)}">{_t(action.title)}</a>'
        + (f' <span class="meta">&mdash; {where}</span>' if where else "")
        + "</li>"
    )


# A survey finding (SCH-003 named 49 resistors on P02 Rev E) lists this many.
_DECISION_COMPONENT_LIMIT = 8


def _finding_item(domain: str, finding: AnalysisFinding, badge: bool = True,
                  advice: bool = False) -> str:
    """A finding that is not an action, listed under its decision heading;
    with ``advice``, its recommendation follows (the finer grid TH-004
    names, on P02 Rev E)."""
    shown = finding.components[:_DECISION_COMPONENT_LIMIT]
    where = _where(shown, finding.nets[:4], more=len(finding.components) - len(shown))
    text = finding.recommendation.strip() if advice else ""
    return (
        "<li>" + (f"{_severity_badge(finding.severity)} " if badge else "")
        + f'{_t(finding.title)} <span class="meta">&mdash; {_esc(domain)}'
        + (f" &middot; {where}" if where else "")
        + "</span>"
        + (f'<br><span class="meta">{_t(text)}</span>' if text else "")
        + "</li>"
    )


_KEY_METRIC_LIMIT = 5


def _metric_text(metric: AnalysisMetric) -> str:
    value = metric.value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = _number(*_scaled(float(value), _units(metric.unit)), precision=metric.precision)
    else:
        number = _t(value)
    return f"{_t(metric.label)}: {number}"


def _numeric(metric: AnalysisMetric) -> bool:
    return isinstance(metric.value, (int, float)) and not isinstance(metric.value, bool)


def _worst(metrics: List[AnalysisMetric], pattern: str) -> Optional[AnalysisMetric]:
    found = [m for m in metrics if re.fullmatch(pattern, m.key) and _numeric(m)]
    return max(found, key=lambda m: float(m.value)) if found else None


def _verdict_count(metrics: List[AnalysisMetric], pattern: str, template: str) -> Optional[str]:
    """``template`` is already translated, with {passed} and {total}."""
    judged = [m for m in metrics
              if re.fullmatch(pattern, m.key) and m.status in ("PASS", "WARN", "FAIL")]
    if not judged:
        return None
    passed = sum(1 for m in judged if m.status == "PASS")
    return _esc(template.format(passed=passed, total=len(judged)))


def _worst_line(metric: Optional[AnalysisMetric]) -> Optional[str]:
    if metric is None:
        return None
    return _esc(_("Worst {figure}")).format(figure=_metric_text(metric))


_KEY_METRIC_KEYS = (
    "hotspot", "hottest_junction_margin", "least_junction_margin", "input_heat", "risk_score",
    "maximum_velocity", "board_free_stream_velocity",
)


def key_metrics(result: AnalysisResult) -> List[str]:
    """The three to five figures a reader looks for first in an analysis
    (audit R-LE-5), as HTML; the full table stays below, folded. P02 Rev E's
    DC section printed 169 figure rows, 853 of the report's 1,920 lines.
    A figure marked NOT_USABLE is never a key figure."""
    usable = [m for m in result.metrics if m.status != "NOT_USABLE"]
    lines: List[Optional[str]] = []
    kind = result.analysis_type.upper()
    if kind == "DC":
        lines.append(_verdict_count(usable, r"rail_\d+_drop",
                                    _("{passed} of {total} rail drops within the limit")))
        # P02 Rev E's "14 of 14" left out three of its 17 rails, without a load.
        unjudged = [m for m in usable if re.fullmatch(r"rail_\d+_drop", m.key)
                    and m.status == "NO_DATA"]
        if unjudged:
            lines.append(_esc(_("{count} rail(s) without a load, not judged")).format(
                count=len(unjudged)))
        lines.append(_worst_line(_worst(usable, r"rail_\d+_drop")))
        lines.append(_worst_line(_worst(usable, r"rail_\d+_maximum_via_current")))
    elif kind == "AC":
        lines.append(_verdict_count(usable, r".+:worst_impedance",
                                    _("{passed} of {total} rails within their target impedance")))
    elif kind == "DIFFERENTIAL":
        lines.append(_verdict_count(usable, r"pair_\d+_zdiff",
                                    _("{passed} of {total} pairs within their impedance tolerance")))
        lines.append(_worst_line(_worst(usable, r"pair_\d+_skew")))
    else:
        picked = [m for m in usable if m.key in _KEY_METRIC_KEYS]
        picked = picked or [m for m in usable if m.status in ("FAIL", "WARN")]
        lines.extend(_metric_text(m) for m in picked)
    return [line for line in lines if line][:_KEY_METRIC_LIMIT]


def _render_scores(scores: List[DomainScore], results: List[AnalysisResult]) -> str:
    if not scores:
        return '<p class="muted">' + _esc(_("No domain produced a result.")) + "</p>"
    by_domain = {result.analysis_type: result for result in results}
    not_run = _esc(_("not run"))
    none = _esc(_("none"))
    rows = []
    for score in scores:
        status_class = _STATUS_CLASS.get(score.status, "nodata")
        if score.status == AnalysisStatus.NO_DATA:
            bar = f'<span class="muted">{not_run}</span>'
            value = "&mdash;"
        else:
            width = max(0.0, min(100.0, score.score))
            bar = (f'<div class="scorebar {status_class}">'
                   f'<span style="width:{width:.1f}%"></span></div>')
            value = f"{score.score:.0f}/100"
        counts = " ".join(
            f'<span class="badge {_SEVERITY_CLASS[sev]}">{_esc(sev.value)} '
            f"{score.finding_counts.get(sev.value, 0)}</span>"
            for sev in FindingSeverity if score.finding_counts.get(sev.value, 0)
        ) or f'<span class="muted">{none}</span>'
        result = by_domain.get(score.domain)
        figures = "".join(f"<li>{line}</li>" for line in key_metrics(result)) if result else ""
        rows.append(
            f"<tr><th>{_esc(score.domain)}</th>"
            f'<td><span class="verdict {status_class}">{_esc(score.status)}</span></td>'
            f"<td>{bar}{value}</td><td>{counts}</td>"
            f'<td><ul class="tight">{figures}</ul></td></tr>'
        )
    head = _headers(_("Analysis"), _("Status"), _("Score"), _("Findings"), _("Key figures"))
    return f"<table>{head}<tbody>{''.join(rows)}</tbody></table>"


_COVERAGE_STATES = {
    "NOT_PREPARED": N_("not prepared"),
    "FAILED": N_("failed"),
    "SKIPPED": N_("skipped"),
    "CANCELLED": N_("cancelled"),
}


def _coverage_items(campaign: CampaignResult) -> List[str]:
    """One line per selected domain that produced no result, with its reason
    as the domain or the batch wrote it."""
    items = []
    for entry in campaign.coverage:
        state = _COVERAGE_STATES.get(str(entry.get("state", "")), "")
        reason = str(entry.get("reason", "") or "")
        items.append(
            f"<li><strong>{_esc(entry.get('domain', ''))}</strong> "
            f'<span class="meta">&mdash; {_tr(state)}'
            + (f": {_t(reason)}" if reason else "")
            + "</span></li>"
        )
    return items


def _decision_list(title: str, explanation: str, items: List[str], empty: str) -> str:
    """``title``, ``explanation`` and ``empty`` are English; shown translated."""
    body = (
        f'<ul class="decision">{"".join(items)}</ul>' if items
        else f'<p class="muted">{_tr(empty)}</p>'
    )
    return f'<h3>{_tr(title)} ({len(items)})</h3><p class="meta">{_tr(explanation)}</p>{body}'


def _render_synthesis(campaign: CampaignResult) -> str:
    status_class = _STATUS_CLASS.get(campaign.overall_status, "nodata")
    lists: Dict[str, List[str]] = {FIX: [], REVIEW: [], PROTOTYPE: [], DATA: []}
    for action in campaign.actions:
        lists[action_bucket(action)].append(_decision_item(action))
    calculation: List[str] = []
    holds: List[str] = []
    for result in campaign.results:
        for finding in result.findings:
            nature = finding_nature(finding)
            if nature == FindingNature.CALCULATION:
                calculation.append(_finding_item(result.analysis_type, finding, advice=True))
            elif finding.severity == FindingSeverity.INFO and nature == FindingNature.DATA:
                lists[DATA].append(_finding_item(result.analysis_type, finding, advice=True))
            elif finding.severity == FindingSeverity.INFO and finding.rule_id in CONFIRMATIONS:
                holds.append(_finding_item(result.analysis_type, finding, badge=False))
    air = campaign.air_cross_check()
    if air is not None:
        thermal_c, cfd_c = air
        values = {"thermal": f"{thermal_c:.3g}", "cfd": f"{cfd_c:.4g}",
                  "spread": f"{abs(thermal_c - cfd_c):.2g}"}
        if abs(thermal_c - cfd_c) <= AIR_AGREEMENT_K:
            holds.append("<li>" + _esc(_(
                "Internal air: {thermal} °C by the thermal analysis's enclosure balance, "
                "{cfd} °C as the mean of the CFD's solved air, {spread} K apart."
            ).format(**values)) + "</li>")
        else:
            calculation.append("<li>" + _esc(_(
                "Internal air: {thermal} °C by the thermal analysis's enclosure balance against "
                "{cfd} °C as the mean of the CFD's solved air, {spread} K apart; check the "
                "enclosure inputs and the CFD mesh."
            ).format(**values)) + "</li>")
    by_domain = {result.analysis_type: result for result in campaign.results}
    passes = _esc(_("{analysis} passes"))
    passes_with = _esc(_("{analysis} passes: {figures}"))
    # A pass that rests on a missing input says so: P02 Rev E's AC passed
    # "3 of 3 rails" with two on the default source (AC-006), and THERMAL on
    # U4's estimated efficiency (TH-008).
    resting = _esc(_("rests on {count} input(s) still to complete"))
    for score in campaign.domain_scores:
        if score.status != AnalysisStatus.PASS:
            continue
        result = by_domain.get(score.domain)
        figures = " &middot; ".join(key_metrics(result)) if result else ""
        analysis = f"<strong>{_esc(score.domain)}</strong>"
        line = (passes_with.format(analysis=analysis, figures=figures) if figures
                else passes.format(analysis=analysis))
        missing = sum(1 for finding in (result.findings if result else ())
                      if finding_nature(finding) == FindingNature.DATA)
        if missing:
            line += " &mdash; " + resting.format(count=missing)
        holds.append(f"<li>{line}</li>")
    warnings = campaign.provenance_warnings()
    warning_block = ""
    if warnings:
        items = "".join(f"<li>{_esc(line)}</li>" for line in warnings)
        label = _esc(_("Warning:"))
        text = _esc(_("these domains do not all describe the same board revision:"))
        warning_block = (
            f'<p class="muted"><strong>{label}</strong> {text}</p><ul class="tight">{items}</ul>'
        )
    explanation = dict(_HOW_TO_READ)
    heading = _esc(_("1. Decision"))
    verdict = _tr(_VERDICT_WORDS.get(campaign.overall_status, ""))
    analyses = _esc(_("Analyses"))
    empty = (
        N_("Nothing found that must change before manufacture."),
        N_("Nothing to review."),
        N_("Every rule of thumb checked is met."),
        N_("No input missing."),
        N_("No limit of the calculation reported."),
        N_("No check passed."),
    )
    lists_in_order = (
        ("Fix before manufacture", lists[FIX]),
        ("Review before manufacture", lists[REVIEW]),
        ("Check on the prototype", lists[PROTOTYPE]),
        ("Data to complete", lists[DATA]),
        ("Calculation reliability", calculation),
        ("What holds", holds),
    )
    body = "".join(
        _decision_list(title, explanation[title], items, nothing)
        for (title, items), nothing in zip(lists_in_order, empty)
    )
    coverage = _coverage_items(campaign)
    if coverage:
        # First, and only when something is missing: it qualifies every
        # list below it.
        body = _decision_list("Did not complete", explanation["Did not complete"], coverage,
                              "") + body
    return (
        f'<section id="synthesis"><h2>{heading}</h2>'
        f'<p><span class="verdict {status_class}">{_esc(campaign.overall_status)}</span> '
        f"{verdict}</p>"
        f"{warning_block}"
        f"{_render_how_to_read()}"
        f"{body}"
        f"<h3>{analyses}</h3>"
        f"{_render_scores(campaign.domain_scores, campaign.results)}"
        "</section>"
    )


# ----------------------------------------------------------------------
# Section 2 -- actions
# ----------------------------------------------------------------------

def _render_remediation(remediation: Remediation) -> str:
    if remediation.verified:
        marker = '<span class="badge verified">' + _esc(_("VERIFIED BY RE-SIMULATION")) + "</span>"
    else:
        marker = ('<span class="badge unverified">'
                  + _esc(_("ESTIMATE — NOT RE-SIMULATED")) + "</span>")
    labels = {
        "action": _esc(_("Action")), "target": _esc(_("Target")), "change": _esc(_("Change")),
        "gain": _esc(_("Predicted gain")), "effort": _esc(_("Effort")),
        "location": _esc(_("Location")), "alternatives": _esc(_("Alternatives")),
    }
    location = ""
    if remediation.x_mm is not None and remediation.y_mm is not None:
        location = (f"<tr><th>{labels['location']}</th><td>x={_number(remediation.x_mm, 'mm')}, "
                    f"y={_number(remediation.y_mm, 'mm')} "
                    f"{_esc(_layer_name(remediation.layer))}</td></tr>")
    alternatives = ""
    if remediation.alternatives:
        items = "".join(f"<li>{_t(item)}</li>" for item in remediation.alternatives)
        alternatives = (f"<tr><th>{labels['alternatives']}</th>"
                        f'<td><ul class="tight">{items}</ul></td></tr>')
    unit = _units(remediation.unit)
    return (
        "<table><tbody>"
        f"<tr><th>{labels['action']}</th><td><code>{_esc(remediation.action)}</code> {marker}</td></tr>"
        f"<tr><th>{labels['target']}</th><td>{_esc(remediation.target) or '&mdash;'}</td></tr>"
        f"<tr><th>{labels['change']}</th><td>{_number(remediation.current_value, unit)}"
        f" &rarr; {_number(remediation.proposed_value, unit)}</td></tr>"
        f"<tr><th>{labels['gain']}</th><td>{_t(remediation.predicted_gain) or '&mdash;'}</td></tr>"
        f"<tr><th>{labels['effort']}</th><td>{_esc(remediation.effort)}</td></tr>"
        f"{location}{alternatives}"
        "</tbody></table>"
    )


def _guide_lines(action: CampaignAction, attribute: str) -> List[str]:
    lines: List[str] = []
    for rule in action.rule_ids:
        entry = guide(rule)
        text = getattr(entry, attribute, "") if entry else ""
        if text and text not in lines:
            lines.append(text)
    return lines


def _paragraphs(lines: List[str], translate: bool = False) -> str:
    render = _tr if translate else _t
    if len(lines) == 1:
        return f"<p>{render(lines[0])}</p>"
    return '<ul class="tight">' + "".join(f"<li>{render(line)}</li>" for line in lines) + "</ul>"


def _render_action_detail(action: CampaignAction) -> str:
    rows = "".join(
        f"<tr><td>{_esc(item.get('domain', ''))}</td>"
        f"<td><code>{_esc(item.get('rule_id', ''))}</code></td>"
        f"<td>{_t(item.get('title', ''))}</td><td>{_t(item.get('description', ''))}</td>"
        f'<td><span class="confidence">{_esc(item.get("confidence", ""))}</span></td></tr>'
        for item in action.details
    )
    head = _headers(_("Analysis"), _("Rule"), _("Finding"), _("Description"), _("Certainty"))
    table = f"<table>{head}<tbody>{rows}</tbody></table>" if rows else ""
    summary = _esc(_("Technical detail"))
    priority = _esc(_('Priority {rank} (see "Priority" under How to read this report).')).format(
        rank=f"{action.gain_rank:.1f}")
    return f'<details><summary>{summary}</summary>{table}<p class="meta">{priority}</p></details>'


def _render_actions(campaign: CampaignResult) -> str:
    heading = _esc(_("2. Actions"))
    if not campaign.actions:
        nothing = _esc(_("No actionable findings."))
        return f'<section id="actions"><h2>{heading}</h2><p class="muted">{nothing}</p></section>'
    labels = {
        "why": _esc(_("Why it is a risk")), "todo": _esc(_("What to do")),
        "verify": _esc(_("How to check")), "sure": _esc(_("How sure")),
        "effort": _esc(_("Typical effort")),
    }
    where_template = _esc(_("Where: {where} · found by {domains}"))
    no_change = _esc(_("No change was computed for this action."))
    blocks = []
    for action in campaign.actions:
        border = {
            FindingSeverity.CRITICAL: "sev-critical-border",
            FindingSeverity.HIGH: "sev-high-border",
            FindingSeverity.MEDIUM: "sev-medium-border",
        }.get(action.severity, "")
        # Before this card, every P02 Rev E action of 2026-10-01 read "No
        # structured remediation was computed", even above its own advice,
        # and "Consequences" repeated its title (audit LE-2).
        if action.remediations:
            todo = "".join(_render_remediation(item) for item in action.remediations)
        elif action.recommendations:
            todo = _paragraphs(action.recommendations)
        else:
            todo = f'<p class="muted">{no_change}</p>'
        why = _guide_lines(action, "why")
        verify = _guide_lines(action, "verify")
        where = where_template.format(
            where=_where(action.components, action.nets) or "&mdash;",
            domains=_join(action.domains),
        )
        certainty = _tr(_CERTAINTY_WORDS.get(action.confidence, action.confidence))
        effort = _tr(_EFFORT_WORDS.get(action.effort, str(action.effort)))
        blocks.append(
            f'<article class="action {border}" id="{_esc(action.action_id)}">'
            f"<h3>{_severity_badge(action.severity)} {_t(action.title)}</h3>"
            f'<p class="meta">{where}</p>'
            '<dl class="card">'
            + (f"<dt>{labels['why']}</dt><dd>{_paragraphs(why, True)}</dd>" if why else "")
            + f"<dt>{labels['todo']}</dt><dd>{todo}</dd>"
            + (f"<dt>{labels['verify']}</dt><dd>{_paragraphs(verify, True)}</dd>" if verify else "")
            + f"<dt>{labels['sure']}</dt><dd>{certainty}</dd>"
            f"<dt>{labels['effort']}</dt><dd>{effort}</dd>"
            "</dl>"
            f"{_render_action_detail(action)}"
            "</article>"
        )
    return f'<section id="actions"><h2>{heading}</h2>{"".join(blocks)}</section>'


# ----------------------------------------------------------------------
# Section 3 -- components
# ----------------------------------------------------------------------

# The component view lists the parts the findings name and the hottest
# junctions, this many at most.
_COMPONENT_LIMIT = 20
_HOTTEST_JUNCTIONS = 5


def _render_components(campaign: CampaignResult) -> str:
    """One row per part: its heat, its temperatures and what each analysis
    says of it (audit R-LE-8). "What is wrong with U4?" took reading five
    domain sections on P02 Rev E's 2026-10-01 report."""
    heading = _esc(_("3. Components"))
    thermal: Dict[str, Dict[str, Any]] = {}
    board: Dict[str, float] = {}
    for result in campaign.results:
        if result.analysis_type != "THERMAL":
            continue
        for item in (result.summary or {}).get("components") or ():
            thermal[str(item.get("reference", ""))] = item
        board.update((result.summary or {}).get("placement_temperatures_c") or {})
    # Every finding above INFO says something of the parts it names. One
    # naming many parts (a survey, as in grouping) annotates rows but only
    # opens its own in the room left: on P02 Rev E's 18:31 report TE-002's
    # 13 capacitors filled the view and pushed out C88 and R10, DC-008's
    # parts and one of the two items to fix before manufacture.
    cited: Dict[str, List[tuple]] = {}
    focused = set()
    for result in campaign.results:
        for finding in result.findings:
            if finding.severity == FindingSeverity.INFO:
                continue
            survey = len(finding.components) > GROUPING_MAX_COMPONENTS
            for reference in finding.components:
                entry = (finding.severity, finding.rule_id, result.analysis_type)
                if entry not in cited.setdefault(reference, []):
                    cited[reference].append(entry)
                if not survey:
                    focused.add(reference)
    hottest = sorted(thermal.values(), key=lambda item: -float(item.get("junction_c", 0.0) or 0.0))
    # The hottest junctions keep their rows whatever the findings: on P02
    # Rev E's 16:51 run, sixteen cited parts pushed U14 (124 C of 150) out.
    extra = [
        str(item.get("reference", "")) for item in hottest[:_HOTTEST_JUNCTIONS]
        if str(item.get("reference", "")) not in cited
    ]
    if not cited and not extra:
        return ""
    severities = list(FindingSeverity)

    def rank(reference):
        entries = cited.get(reference, [])
        worst = min((severities.index(severity) for severity, _rule, _domain in entries),
                    default=len(severities))
        hot = float(thermal.get(reference, {}).get("junction_c", board.get(reference, 0.0)) or 0.0)
        return worst, -len(entries), -hot, reference

    room = max(0, _COMPONENT_LIMIT - len(extra))
    primary = sorted((ref for ref in cited if ref in focused), key=rank)[:room]
    secondary = sorted((ref for ref in cited if ref not in focused), key=rank)[:room - len(primary)]
    references = primary + extra + secondary
    rows = []
    for reference in references:
        item = thermal.get(reference, {})
        board_c = item.get("board_c", board.get(reference))
        heat = _number(float(item["power_w"]), "W") if item.get("power_w") else "&mdash;"
        board_text = _number(float(board_c), "°C") if board_c is not None else "&mdash;"
        junction = (
            f"{_number(float(item['junction_c']), '°C')} / {_number(float(item['max_junction_c']), '°C')}"
            if item.get("max_junction_c") else "&mdash;"
        )
        margin = _number(float(item["margin_k"]), "K") if item.get("max_junction_c") else "&mdash;"
        said = "".join(
            f"<li>{_severity_badge(severity)} "
            f"{_tr(guide(rule).name) if guide(rule) else _esc(rule)} "
            f'<span class="meta">({_esc(domain)}, {_esc(rule)})</span></li>'
            for severity, rule, domain in cited.get(reference, [])
        )
        rows.append(
            f"<tr><th>{_esc(reference)}</th><td>{heat}</td><td>{board_text}</td>"
            f"<td>{junction}</td><td>{margin}</td>"
            f'<td><ul class="tight">{said}</ul></td></tr>'
        )
    head = _headers(_("Component"), _("Heat"), _("Board"), _("Junction / limit"), _("Margin"),
                    _("What the analyses say"))
    explanation = _esc(_(
        "Each part a finding names, and the hottest junctions: the heat it dissipates, the "
        "board temperature under it, its junction against its limit, and what each analysis "
        "says of it."
    ))
    return (
        f'<section id="components"><h2>{heading}</h2><p class="meta">{explanation}</p>'
        f"<table>{head}<tbody>{''.join(rows)}</tbody></table></section>"
    )


# ----------------------------------------------------------------------
# Section 4 -- per domain
# ----------------------------------------------------------------------

def _render_metrics(result: AnalysisResult) -> str:
    if not result.metrics:
        return '<p class="muted">' + _esc(_("No figures recorded.")) + "</p>"
    rows = []
    for metric in result.metrics:
        value, unit = metric.value, metric.unit
        if _numeric(metric):
            value, unit = _scaled(float(value), unit)
            value_html = _number(value, precision=metric.precision)
        else:
            value_html = _t(value)
        # The internal key stays as a tooltip for a reader matching the CSV,
        # not as a column: 205 rows of P02 Rev E's report showed one (LE-4).
        rows.append(
            f'<tr><td title="{_esc(metric.key)}">{_t(metric.label)}</td>'
            f"<td>{value_html}</td><td>{_t(unit)}</td>"
            f"<td>{_esc(metric.status)}</td></tr>"
        )
    summary = _esc(_("All {count} figures")).format(count=len(result.metrics))
    head = _headers(_("Figure"), _("Value"), _("Unit"), _("Status"))
    return (f"<details><summary>{summary}</summary>"
            f"<table>{head}<tbody>{''.join(rows)}</tbody></table></details>")


def _render_unusable(result: AnalysisResult) -> str:
    """Name the figures an analysis marks NOT_USABLE, with its own reason,
    where the figures are. P02 Rev E's CFD passed while its table read a
    170.9 C solid maximum; why it was not usable sat in the appendix (audit
    LE-7)."""
    unusable = [metric for metric in result.metrics if metric.status == "NOT_USABLE"]
    if not unusable:
        return ""
    reason = next((line for line in result.limitations if "not usable" in line.lower()), "")
    names = ", ".join(_t(metric.label) for metric in unusable)
    text = _esc(_("Not to be read as results: {figures}.")).format(figures=names)
    return f'<p class="meta">{text}' + (f" {_t(reason)}" if reason else "") + "</p>"


def _render_findings(result: AnalysisResult) -> str:
    if not result.findings:
        return '<p class="muted">' + _esc(_("No findings.")) + "</p>"
    head = _headers(_("Rule"), _("Title"), _("Description"), _("Nets"), _("Components"),
                    _("Recommendation"))
    count_template = _esc(_("{count} finding(s)"))
    blocks = []
    for severity in FindingSeverity:
        group = [f for f in result.findings if f.severity == severity]
        if not group:
            continue
        rows = []
        for finding in group:
            rows.append(
                f"<tr><td><code>{_esc(finding.rule_id)}</code></td>"
                f"<td>{_t(finding.title)}{_confidence_badge(finding)}</td>"
                f"<td>{_t(finding.description)}</td>"
                f"<td>{_join(finding.nets)}</td>"
                f"<td>{_join(finding.components)}</td>"
                f"<td>{_t(finding.recommendation) or '&mdash;'}</td></tr>"
            )
        count = count_template.format(count=len(group))
        blocks.append(
            f'<details><summary><span class="badge {_SEVERITY_CLASS[severity]}">'
            f"{_esc(severity)}</span> &mdash; {count}</summary>"
            f"<table>{head}<tbody>{''.join(rows)}</tbody></table></details>"
        )
    return "".join(blocks)


# A DC run saves 14 figures per rail: 227 (13 MB) for P02 Rev E's 2026-10-01
# run. Each analysis embeds this many and lists the rest by title.
_EMBEDDED_FIGURES = 8


def _plain(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(text or "")).upper()


def _figure_order(result: AnalysisResult) -> List[Any]:
    """Overviews first ("3D" in the title), and among them the nets the
    analysis's findings or its worst DC drop name: on P02 Rev E the
    /+5V_RAIL 3D view, then one 3D view per other rail."""
    named = {
        _plain(net) for finding in result.findings
        if finding.severity != FindingSeverity.INFO for net in finding.nets
    }
    worst = _worst(result.metrics, r"rail_\d+_drop")
    if worst is not None:
        named.add(_plain(worst.label.split(" ")[0]))
    indexed = list(enumerate(result.artifacts))
    indexed.sort(key=lambda item: (
        "3D" not in item[1].title,
        _plain(item[1].title.split(" — ")[0]) not in named,
        item[0],
    ))
    return [artifact for _index, artifact in indexed]


def _render_artifacts(result: AnalysisResult, embed_images: bool, search_roots=()) -> str:
    if not result.artifacts:
        return ""
    ordered = _figure_order(result)
    shown, rest = ordered[:_EMBEDDED_FIGURES], ordered[_EMBEDDED_FIGURES:]
    unavailable = _esc(_("Artifact unavailable:"))
    expected = _esc(_("Expected at"))
    no_path = _esc(_("(no path recorded)"))
    blocks = []
    for artifact in shown:
        title = _esc(artifact.title)
        raw_path = str(artifact.path or "")
        if not embed_images:
            blocks.append(f'<p class="meta">{title} &mdash; <code>{_esc(raw_path)}</code></p>')
            continue
        resolved = _resolve_artifact(raw_path, search_roots)
        uri = _data_uri(resolved) if resolved is not None else None
        if uri is None:
            blocks.append(
                f'<div class="missing-artifact"><strong>{unavailable}</strong> '
                f"{title}<br>{expected} <code>{_esc(raw_path) or no_path}</code>"
                "</div>"
            )
        else:
            blocks.append(
                f'<figure><img src="{uri}" alt="{title}">'
                f"<figcaption>{title}</figcaption></figure>"
            )
    if rest:
        folder = _resolve_artifact(str(rest[0].path or ""), search_roots)
        items = "".join(f"<li>{_esc(artifact.title)}</li>" for artifact in rest)
        more = _esc(_("{count} more figures")).format(count=len(rest))
        where = (_esc(_("In {folder}")).format(folder=f"<code>{_esc(folder.parent)}</code>")
                 if folder else "")
        blocks.append(
            f"<details><summary>{more}</summary>"
            + (f'<p class="meta">{where}</p>' if where else "")
            + f'<ul class="tight">{items}</ul></details>'
        )
    heading = _esc(_("Figures"))
    return f"<h4>{heading}</h4>{''.join(blocks)}"


def _render_domains(campaign: CampaignResult, embed_images: bool, search_roots=()) -> str:
    heading = _esc(_("4. Per domain"))
    if not campaign.results:
        nothing = _esc(_("No analysis results."))
        return f'<section id="domains"><h2>{heading}</h2><p class="muted">{nothing}</p></section>'
    findings_heading = _esc(_("Findings"))
    blocks = []
    for result in campaign.results:
        status_class = _STATUS_CLASS.get(result.status, "nodata")
        figures = "".join(f"<li>{line}</li>" for line in key_metrics(result))
        blocks.append(
            f"<h3>{_esc(result.analysis_type)} &mdash; {_esc(result.title)} "
            f'<span class="verdict {status_class}">{_esc(result.status)}</span></h3>'
            + (f'<ul class="tight">{figures}</ul>' if figures else "")
            + _render_unusable(result)
            + f"{_render_metrics(result)}"
            f"<h4>{findings_heading}</h4>"
            f"{_render_findings(result)}"
            f"{_render_artifacts(result, embed_images, search_roots)}"
        )
    return f'<section id="domains"><h2>{heading}</h2>{"".join(blocks)}</section>'


# ----------------------------------------------------------------------
# Section 5 -- appendices
# ----------------------------------------------------------------------

_MODE_LABELS = {
    "single_board": N_("single-board"),
    "zeo_container": N_("Zeo container"),
    "zeo_member": N_("Zeo member"),
}


def _render_project_context(context) -> str:
    if not context:
        return ""
    raw = str(context.get("mode", ""))
    mode = _(_MODE_LABELS[raw]) if raw in _MODE_LABELS else raw
    notes = "; ".join(str(note) for note in context.get("notes") or ())
    host = _esc(_("Host"))
    project_mode = _esc(_("Project mode"))
    return (
        f"<tr><th>{host}</th><td>{_esc(context.get('host', ''))}</td></tr>"
        f"<tr><th>{project_mode}</th><td>{_esc(mode)}"
        + (f" &mdash; {_esc(notes)}" if notes else "")
        + "</td></tr>"
    )


def _render_scope(campaign: CampaignResult) -> str:
    """Identifiers and the host context, for traceability rather than for
    the decision: shown at the top before, where "host unknown ... host
    detection inconclusive" read like a result (audit CO-9)."""
    rows = "".join(
        f"<tr><td>{_esc(score.domain)}</td><td>{_esc(_local_time(score.completed_at)) or '&mdash;'}</td>"
        f"<td><code>{_esc(score.board_fingerprint) or '&mdash;'}</code></td>"
        f"<td><code>{_esc(result.run_id)}</code></td>"
        f"<td>{result.elapsed_seconds:.2f} s</td>"
        f"<td><code>{_esc(result.tool_version) or '&mdash;'}</code></td></tr>"
        for score, result in zip(campaign.domain_scores, campaign.results)
    )
    label = {key: _esc(text) for key, text in (
        ("scope", _("Scope analysed")), ("project", _("Project")),
        ("fingerprint", _("Board fingerprint")), ("campaign", _("Campaign")),
        ("created", _("Created")), ("compute", _("Total compute")),
        ("analyses", _("Analyses")), ("tool", _("Ki-PIDA build")),
    )}
    head = _headers(_("Analysis"), _("Completed"), _("Board"), _("Run"), _("Compute"),
                    _("Ki-PIDA build"))
    return (
        f"<h3>{label['scope']}</h3><table><tbody>"
        f"<tr><th>{label['project']}</th><td>{_esc(campaign.project_name) or '&mdash;'}</td></tr>"
        f"{_render_project_context(campaign.project_context)}"
        f"<tr><th>{label['fingerprint']}</th>"
        f"<td><code>{_esc(campaign.board_fingerprint) or '&mdash;'}</code></td></tr>"
        f"<tr><th>{label['campaign']}</th><td><code>{_esc(campaign.campaign_id)}</code></td></tr>"
        f"<tr><th>{label['created']}</th><td>{_esc(_local_time(campaign.created_at))}</td></tr>"
        f"<tr><th>{label['tool']}</th><td><code>{_esc(campaign.tool_version) or '&mdash;'}</code>"
        "</td></tr>"
        f"<tr><th>{label['compute']}</th><td>{campaign.total_elapsed_seconds:.2f} s</td></tr>"
        f"<tr><th>{label['analyses']}</th><td>{len(campaign.results)}</td></tr>"
        "</tbody></table>"
        + (f"<table>{head}<tbody>{rows}</tbody></table>" if rows else "")
    )


def _render_mapping(payload: Dict[str, Any]) -> str:
    if not payload:
        return '<p class="muted">' + _esc(_("None recorded.")) + "</p>"
    return f"<pre>{_esc(json.dumps(payload, indent=2, ensure_ascii=False, default=str))}</pre>"


def _render_appendices(campaign: CampaignResult) -> str:
    label = {key: _esc(text) for key, text in (
        ("provenance", _("Provenance")), ("limitations", _("Limitations")),
        ("configuration", _("Configuration")), ("compute", _("Compute metadata")),
        ("no_provenance", _("No provenance recorded.")),
        ("no_limitations", _("No limitations recorded.")),
        ("no_results", _("No results.")), ("heading", _("5. Appendices")),
        ("per_analysis", _("Per analysis")),
    )}
    result_schema = _esc(_("Result schema {version}"))
    head = _headers(_("Source"), _("Detail"), _("Reference"))
    blocks = []
    for result in campaign.results:
        if result.provenance:
            rows = "".join(
                f"<tr><td>{_esc(item.source)}</td><td>{_t(item.detail)}</td>"
                f"<td>{_esc(item.reference) or '&mdash;'}</td></tr>"
                for item in result.provenance
            )
            provenance = f"<table>{head}<tbody>{rows}</tbody></table>"
        else:
            provenance = f'<p class="muted">{label["no_provenance"]}</p>'
        if result.limitations:
            items = "".join(f"<li>{_t(line)}</li>" for line in result.limitations)
            limitations = f'<ul class="tight">{items}</ul>'
        else:
            limitations = f'<p class="muted">{label["no_limitations"]}</p>'
        schema = result_schema.format(version=_esc(result.schema_version))
        blocks.append(
            f"<details><summary>{_esc(result.analysis_type)} &mdash; "
            f"{_esc(result.title)}</summary>"
            f"<h4>{label['provenance']}</h4>{provenance}"
            f"<h4>{label['limitations']}</h4>{limitations}"
            f"<h4>{label['configuration']}</h4>{_render_mapping(result.configuration_snapshot)}"
            f"<h4>{label['compute']}</h4>{_render_mapping(result.compute_metadata)}"
            f'<p class="meta">{schema}</p>'
            "</details>"
        )
    body = "".join(blocks) or f'<p class="muted">{label["no_results"]}</p>'
    campaign_schema = _esc(_("Campaign schema {version}")).format(
        version=_esc(campaign.schema_version))
    return (
        f'<section id="appendices"><h2>{label["heading"]}</h2>'
        f"{_render_scope(campaign)}"
        f"<h3>{label['per_analysis']}</h3>"
        f"{body}"
        f'<p class="meta">{campaign_schema}</p>'
        "</section>"
    )


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

def _subject_items(groups) -> str:
    return "".join(
        f"<li>{_severity_badge(FindingSeverity(severity))} {_t(title)} "
        f'<span class="meta">&mdash; {_esc(domain)}, <code>{_esc(rule)}</code> &middot; '
        f"{_join(names)}</span></li>"
        for severity, title, domain, rule, names in groups
    )


def _render_since(campaign: CampaignResult, previous: Optional[CampaignResult]) -> str:
    """What changed since the previous campaign of this board, finding by
    finding, on the analyses both ran; nothing when there is none."""
    if previous is None:
        return ""
    from report.comparison import compare_campaigns, grouped

    delta = compare_campaigns(previous, campaign)
    heading = _esc(_("Since the previous campaign"))
    intro = _esc(_("Compared with the campaign of {date}, on the analyses both ran.")).format(
        date=_esc(_local_time(previous.created_at)))
    notes = []
    if delta.board_changed:
        notes.append(_esc(_("The board changed in between.")))
    if delta.tool_changed:
        notes.append(_esc(_(
            "Ki-PIDA changed in between ({before} to {after}): a difference may come from the "
            "tool rather than the board."
        )).format(before=_esc(previous.tool_version or "?"), after=_esc(campaign.tool_version or "?")))
    if delta.not_compared:
        notes.append(_esc(_("Not compared, run in only one of the two: {domains}.")).format(
            domains=_esc(", ".join(delta.not_compared))))
    scores = " &middot; ".join(
        f"{_esc(domain)} {value:+.0f}" for domain, value in delta.score_deltas.items() if value
    )
    if scores:
        notes.append(_esc(_("Score changes:")) + " " + scores)
    changed = "".join(
        f"<li>{_severity_badge(FindingSeverity(before.severity))} &rarr; "
        f"{_severity_badge(FindingSeverity(after.severity))} {_t(after.title)} "
        f'<span class="meta">&mdash; {_esc(after.domain)}, <code>{_esc(after.rule_id)}</code> '
        f"&middot; {_esc(after.subject)}</span></li>"
        for before, after in delta.changed
    )
    blocks = []
    for title, subjects in ((_("Resolved"), delta.resolved), (_("New"), delta.introduced)):
        items = _subject_items(grouped(subjects))
        blocks.append(f"<h4>{_esc(title)} ({len(subjects)})</h4>"
                      + (f'<ul class="decision">{items}</ul>' if items else ""))
    if changed:
        regraded = _esc(_("Severity changed"))
        blocks.append(f"<h4>{regraded} ({len(delta.changed)})</h4>"
                      f'<ul class="decision">{changed}</ul>')
    persisting = _subject_items(grouped(delta.persisting))
    if persisting:
        summary = _esc(_("Still present ({count})")).format(count=len(delta.persisting))
        blocks.append(f'<details><summary>{summary}</summary><ul class="decision">'
                      f"{persisting}</ul></details>")
    return (
        f'<section id="since"><h2>{heading}</h2><p class="meta">{intro}</p>'
        + "".join(f'<p class="muted">{note}</p>' for note in notes)
        + "".join(blocks) + "</section>"
    )


def render_campaign_html(
    campaign: CampaignResult, *, embed_images: bool = True, artifact_roots=(),
    previous: Optional[CampaignResult] = None,
) -> str:
    """Render *campaign* as a fully self-contained HTML document.

    ``artifact_roots`` are directories to search for artifact files whose
    recorded path is a bare file name, which is what ProjectResultsHistory
    stores. Without them, every plot renders as "artifact unavailable".
    With ``previous``, what changed since that campaign follows the decision.
    """
    title = _esc(campaign.project_name or _("Ki-PIDA campaign report"))
    suffix = _esc(_("Ki-PIDA report"))
    subtitle = _esc(_("Ki-PIDA consolidated analysis report"))
    body = "".join((
        _render_synthesis(campaign),
        _render_since(campaign, previous),
        _render_actions(campaign),
        _render_components(campaign),
        _render_domains(campaign, embed_images, artifact_roots),
        _render_appendices(campaign),
    ))
    # The contents follow the sections actually rendered: the comparison
    # only exists when there was an earlier campaign.
    toc = " ".join(
        f'<a href="#{anchor}">{heading}</a>'
        for anchor, heading in re.findall(r'<section id="([\w-]+)"><h2>(.*?)</h2>', body)
    )
    return (
        "<!DOCTYPE html>\n"
        f'<html lang="{_esc(current_language())}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title} &mdash; {suffix}</title>"
        f"<style>{_CSS}</style></head><body>"
        f"<h1>{title}</h1>"
        f'<p class="meta">{subtitle} &middot; {_esc(_local_time(campaign.created_at))}</p>'
        f'<nav class="toc">{toc}</nav>'
        f"{body}"
        "</body></html>\n"
    )


def write_campaign_html(
    campaign: CampaignResult, path: Path, *, embed_images: bool = True,
    artifact_roots=(), previous: Optional[CampaignResult] = None,
) -> Path:
    """Render *campaign* and write it to *path*, creating parent directories.

    When ``artifact_roots`` is empty the report's own parent directory is
    searched, which covers the common case of writing beside the analysis
    history that holds the plots.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    roots = tuple(artifact_roots) or (path.parent,)
    path.write_text(
        render_campaign_html(
            campaign, embed_images=embed_images, artifact_roots=roots, previous=previous,
        ),
        encoding="utf-8",
    )
    return path
