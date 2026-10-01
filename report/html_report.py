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
"""

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
from campaign import CampaignAction, CampaignResult, DomainScore
from rule_guide import CONFIRMATIONS, PROTOTYPE_CHECK, FindingNature, finding_nature, guide

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
"""


def _esc(value: Any) -> str:
    """Escape any value for HTML text/attribute context.

    Net and component names come out of user project files and routinely
    contain ``<``, ``&`` and quotes; nothing reaches the document unescaped.
    """
    return html.escape("" if value is None else str(value), quote=True)


def _join(values: List[str], empty: str = "&mdash;") -> str:
    return ", ".join(_esc(item) for item in values) if values else empty


def _confidence_badge(finding: AnalysisFinding) -> str:
    badge = f'<span class="confidence">{_esc(finding.confidence)}</span>'
    if finding.calculation_quality:
        # COH-9: a self-diagnostic about the analysis (mesh, solver limit),
        # not a defect on the board; it still stays in this table -- the
        # score just does not charge it (campaign.score_findings).
        badge += ' <span class="confidence muted">CALCULATION QUALITY</span>'
    return badge


def _number(value: Optional[float], unit: str = "", precision: Optional[int] = None) -> str:
    """The one formatter for every value this report prints (COH-12).

    An explicit precision is honoured as given. Otherwise a value under
    1e-6 in magnitude prints as "< 1e-6" (or "0" when it is exactly zero)
    rather than raw floating-point noise around zero -- P02 Rev E printed
    "8.074349270001136e-14 %" and "1.03e-09 A/mm2" this way -- and
    everything else prints to 3 significant digits (audit R-LE-7: P02 Rev
    E's worst drop read "0.003015 %").
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
    return f"{_esc(text)}&nbsp;{_esc(unit)}" if unit else _esc(text)


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
    "DETERMINISTIC": "Fact: an exact count or the state of the run",
    "MEASURED": "Measured",
    "DATASHEET_BACKED": "Rests on datasheet values",
    "ESTIMATED": "Estimate from a model of the board",
    "HEURISTIC": "Rule of thumb: a threshold this tool sets",
}

_EFFORT_WORDS = {
    RemediationEffort.LOW: "Low: move a part, add a via or fill in a field",
    RemediationEffort.MEDIUM: "Medium: reroute or re-place a group of parts",
    RemediationEffort.HIGH: "High: change the stackup or a plane",
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
    """Prose from a finding or the guide: one unit notation, then escaped."""
    return _esc(_units(value))


def _severity_badge(severity: FindingSeverity) -> str:
    return f'<span class="badge {_SEVERITY_CLASS[severity]}">{_esc(severity)}</span>'


_HOW_TO_READ = (
    ("What this page answers",
     "Whether the board can go to manufacture. Each item below is one physical problem, "
     "even when several analyses report it, and links to its card in section 2."),
    ("Fix before manufacture",
     "A defect the analyses establish on the board itself, such as a broken connection or "
     "a voltage drop beyond its limit."),
    ("Review before manufacture",
     "A smaller defect, or a margin worth a second look; decide whether to change the "
     "board."),
    ("Check on the prototype",
     "A layout rule of thumb is not met. Only a measurement on the prototype, such as an "
     "emissions scan or an ESD test, settles whether it matters. Improving the layout "
     "first is cheaper than a respin."),
    ("Data to complete",
     "An input the analyses needed and did not have. The verdict sharpens once it is "
     "filled in."),
    ("Calculation reliability",
     "A limit of the calculation, not of the board: a mesh too coarse, a solve that did "
     "not settle. It takes nothing off the scores and is not an action; it says how far "
     "to trust the figures it names."),
    ("What holds",
     "Checks that passed. Read them as what the analyses covered, not as a guarantee."),
)

_GLOSSARY = (
    ("Severity",
     "CRITICAL: the board cannot work as designed. HIGH: likely to fail a requirement. "
     "MEDIUM: a real risk with some margin. LOW: minor. INFO: a note, never an action."),
    ("Certainty",
     "What a finding rests on. A fact (an exact count or the state of the run), a "
     "measurement, datasheet values, an estimate from a model of the board, or a rule of "
     "thumb (a threshold this tool sets)."),
    ("Status",
     "Per analysis. FAIL on a CRITICAL finding, or a HIGH one resting on a fact, a "
     "measurement or a datasheet. WARN on any other HIGH, or a MEDIUM. PASS otherwise."),
    ("Score",
     "100 minus a penalty per finding, weighted by severity and certainty. It summarises; "
     "it is not a margin. The EMC score is the analysis's own uncalibrated risk figure."),
    ("Priority",
     "Orders the actions: severity, times certainty, times 1.5 for each further analysis "
     "reporting the problem, divided by the typical effort. It has no unit; only the "
     "order means something."),
    ("Margin",
     "The distance between a value and its limit, such as 25 K under a 150 °C junction "
     "limit."),
    ("Junction temperature (Tj)",
     "The temperature of the silicon inside a package, estimated from the board "
     "temperature and the part's thermal resistance."),
    ("θJB, ψJB",
     "Datasheet figures linking the junction to the board 1 mm from the package (JEDEC "
     "JESD51-8 and -12)."),
    ("Derating",
     "How much of its rating (current, power, voltage) a part keeps at a higher "
     "temperature, read off its datasheet curve."),
    ("PDN, target impedance",
     "The power distribution network, from regulator to load. Below its target "
     "impedance, a rail holds its voltage when the load's current steps."),
    ("Decoupling capacitor",
     "A capacitor at a power pin that supplies the pin's fast current locally."),
    ("Return path, reference plane",
     "Every signal current comes back through the plane under it. A gap or a layer change "
     "without a via forces a detour, and the loop radiates."),
    ("Hot loop, switching node",
     "In a switching converter, the loop through the input capacitor and the switches "
     "carries the fastest current edges, and the switching node swings at full input "
     "voltage. Both radiate in proportion to their area."),
    ("Harmonic, emissions band",
     "A switching edge carries energy at every multiple of the switching frequency. "
     "Standards limit what a product radiates in set bands."),
    ("ESD, TVS",
     "An electrostatic discharge enters through a connector. A TVS diode clamps it to "
     "ground, if its ground path is short."),
    ("Stitching via",
     "A via joining ground planes on different layers, so that they act as one at high "
     "frequency."),
    ("Mesh, grid step",
     "The analyses cut the board into small cells. A result that moves when the cells are "
     "halved depends on the mesh, not only on the copper."),
)


def _render_how_to_read() -> str:
    sections = "".join(
        f"<dt>{_esc(term)}</dt><dd>{_esc(text)}</dd>" for term, text in _HOW_TO_READ
    )
    glossary = "".join(
        f"<dt>{_esc(term)}</dt><dd>{_esc(text)}</dd>" for term, text in _GLOSSARY
    )
    return (
        '<details class="guide"><summary>How to read this report</summary>'
        f'<dl class="glossary">{sections}</dl>'
        "<h4>Glossary</h4>"
        f'<dl class="glossary">{glossary}</dl></details>'
    )


# ----------------------------------------------------------------------
# Section 1 -- decision
# ----------------------------------------------------------------------

_VERDICT_WORDS = {
    AnalysisStatus.FAIL: "Do not release for manufacture yet: at least one defect must be "
                         "fixed first.",
    AnalysisStatus.WARN: "Review before manufacture: the items below need a decision.",
    AnalysisStatus.PASS: "No defect found by the analyses that ran.",
    AnalysisStatus.NO_DATA: "No analysis produced a result.",
}

FIX, REVIEW, PROTOTYPE, DATA = "fix", "review", "prototype", "data"


def action_bucket(action: CampaignAction) -> str:
    """Which decision list an action belongs to.

    A CRITICAL finding is always to fix. Otherwise an action asking only for
    data is data to complete; one whose every rule is a layout rule of thumb
    (rule_guide.PROTOTYPE_CHECK) is for the prototype to settle; a HIGH one
    is to fix; the rest is to review. On P02 Rev E's 2026-10-01 campaign this
    puts DC-008 (C88 and R10 cut off from GND) alone under "fix", and SW-003
    (a 20 mm hot-loop threshold) under "check on the prototype".
    """
    if action.severity == FindingSeverity.CRITICAL:
        return FIX
    if action.nature == FindingNature.DATA.value:
        return DATA
    if action.rule_ids and all(rule in PROTOTYPE_CHECK for rule in action.rule_ids):
        return PROTOTYPE
    if action.severity == FindingSeverity.HIGH:
        return FIX
    return REVIEW


def _where(components: List[str], nets: List[str]) -> str:
    parts = []
    if components:
        parts.append(_join(components))
    if nets:
        parts.append(f"net {_join(nets)}")
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
    where = _where(shown, finding.nets[:4])
    more = len(finding.components) - len(shown)
    if more > 0:
        where += f" and {more} more"
    text = finding.recommendation.strip() if advice else ""
    return (
        "<li>" + (f"{_severity_badge(finding.severity)} " if badge else "")
        + f'{_t(finding.title)} <span class="meta">&mdash; {_esc(domain)}'
        + (f"; {where}" if where else "")
        + "</span>"
        + (f'<br><span class="meta">{_t(text)}</span>' if text else "")
        + "</li>"
    )


_KEY_METRIC_LIMIT = 5


def _metric_text(metric: AnalysisMetric) -> str:
    value = metric.value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = _number(float(value), _units(metric.unit), precision=metric.precision)
    else:
        number = _t(value)
    return f"{_t(metric.label)}: {number}"


def _numeric(metric: AnalysisMetric) -> bool:
    return isinstance(metric.value, (int, float)) and not isinstance(metric.value, bool)


def _worst(metrics: List[AnalysisMetric], pattern: str) -> Optional[AnalysisMetric]:
    found = [m for m in metrics if re.fullmatch(pattern, m.key) and _numeric(m)]
    return max(found, key=lambda m: float(m.value)) if found else None


def _verdict_count(metrics: List[AnalysisMetric], pattern: str, noun: str) -> Optional[str]:
    judged = [m for m in metrics
              if re.fullmatch(pattern, m.key) and m.status in ("PASS", "WARN", "FAIL")]
    if not judged:
        return None
    passed = sum(1 for m in judged if m.status == "PASS")
    return f"{passed} of {len(judged)} {noun}"


_KEY_METRIC_KEYS = (
    "hotspot", "hottest_junction_margin", "input_heat", "risk_score",
    "maximum_velocity", "board_free_stream_velocity",
)


def key_metrics(result: AnalysisResult) -> List[str]:
    """The three to five figures a reader looks for first in an analysis
    (audit R-LE-5), as HTML; the full table stays below, folded. P02 Rev E's
    DC section printed 169 figure rows, 853 of the report's 1,920 lines.
    A figure marked NOT_USABLE is never a key figure."""
    usable = [m for m in result.metrics if m.status != "NOT_USABLE"]
    lines: List[str] = []
    kind = result.analysis_type.upper()
    if kind == "DC":
        count = _verdict_count(usable, r"rail_\d+_drop", "rail drops within the limit")
        if count:
            lines.append(_esc(count))
        for pattern in (r"rail_\d+_drop", r"rail_\d+_maximum_via_current"):
            metric = _worst(usable, pattern)
            if metric is not None:
                lines.append("Worst " + _metric_text(metric))
    elif kind == "AC":
        count = _verdict_count(usable, r".+:worst_impedance", "rails within their target impedance")
        if count:
            lines.append(_esc(count))
    elif kind == "DIFFERENTIAL":
        count = _verdict_count(usable, r"pair_\d+_zdiff", "pairs within their impedance tolerance")
        if count:
            lines.append(_esc(count))
        skew = _worst(usable, r"pair_\d+_skew")
        if skew is not None:
            lines.append("Worst " + _metric_text(skew))
    else:
        picked = [m for m in usable if m.key in _KEY_METRIC_KEYS]
        picked = picked or [m for m in usable if m.status in ("FAIL", "WARN")]
        lines.extend(_metric_text(m) for m in picked)
    return lines[:_KEY_METRIC_LIMIT]


def _render_scores(scores: List[DomainScore], results: List[AnalysisResult]) -> str:
    if not scores:
        return '<p class="muted">No domain produced a result.</p>'
    by_domain = {result.analysis_type: result for result in results}
    rows = []
    for score in scores:
        status_class = _STATUS_CLASS.get(score.status, "nodata")
        if score.status == AnalysisStatus.NO_DATA:
            bar = '<span class="muted">not run</span>'
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
        ) or '<span class="muted">none</span>'
        result = by_domain.get(score.domain)
        figures = "".join(f"<li>{line}</li>" for line in key_metrics(result)) if result else ""
        rows.append(
            f"<tr><th>{_esc(score.domain)}</th>"
            f'<td><span class="verdict {status_class}">{_esc(score.status)}</span></td>'
            f"<td>{bar}{value}</td><td>{counts}</td>"
            f'<td><ul class="tight">{figures}</ul></td></tr>'
        )
    return (
        "<table><thead><tr><th>Analysis</th><th>Status</th><th>Score</th>"
        "<th>Findings</th><th>Key figures</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _decision_list(title: str, explanation: str, items: List[str], empty: str) -> str:
    body = (
        f'<ul class="decision">{"".join(items)}</ul>' if items
        else f'<p class="muted">{_esc(empty)}</p>'
    )
    return f'<h3>{_esc(title)} ({len(items)})</h3><p class="meta">{_esc(explanation)}</p>{body}'


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
    by_domain = {result.analysis_type: result for result in campaign.results}
    for score in campaign.domain_scores:
        if score.status != AnalysisStatus.PASS:
            continue
        result = by_domain.get(score.domain)
        figures = "; ".join(key_metrics(result)) if result else ""
        holds.append(f"<li><strong>{_esc(score.domain)}</strong> passes"
                     + (f": {figures}" if figures else "") + "</li>")
    warnings = campaign.provenance_warnings()
    warning_block = ""
    if warnings:
        items = "".join(f"<li>{_esc(line)}</li>" for line in warnings)
        warning_block = (
            '<p class="muted"><strong>Warning:</strong> these domains do not all '
            f"describe the same board revision:</p><ul class=\"tight\">{items}</ul>"
        )
    explanation = dict(_HOW_TO_READ)
    return (
        '<section id="synthesis"><h2>1. Decision</h2>'
        f'<p><span class="verdict {status_class}">{_esc(campaign.overall_status)}</span> '
        f"{_esc(_VERDICT_WORDS.get(campaign.overall_status, ''))}</p>"
        f"{warning_block}"
        f"{_render_how_to_read()}"
        + _decision_list("Fix before manufacture", explanation["Fix before manufacture"],
                         lists[FIX], "Nothing found that must change before manufacture.")
        + _decision_list("Review before manufacture", explanation["Review before manufacture"],
                         lists[REVIEW], "Nothing to review.")
        + _decision_list("Check on the prototype", explanation["Check on the prototype"],
                         lists[PROTOTYPE], "Every rule of thumb checked is met.")
        + _decision_list("Data to complete", explanation["Data to complete"],
                         lists[DATA], "No input missing.")
        + _decision_list("Calculation reliability", explanation["Calculation reliability"],
                         calculation, "No limit of the calculation reported.")
        + _decision_list("What holds", explanation["What holds"], holds, "No check passed.")
        + "<h3>Analyses</h3>"
        f"{_render_scores(campaign.domain_scores, campaign.results)}"
        "</section>"
    )


# ----------------------------------------------------------------------
# Section 2 -- actions
# ----------------------------------------------------------------------

def _render_remediation(remediation: Remediation) -> str:
    if remediation.verified:
        marker = ('<span class="badge verified">VERIFIED BY RE-SIMULATION</span>')
    else:
        marker = ('<span class="badge unverified">ESTIMATE &mdash; NOT RE-SIMULATED</span>')
    location = ""
    if remediation.x_mm is not None and remediation.y_mm is not None:
        location = (f"<tr><th>Location</th><td>x={_number(remediation.x_mm, 'mm')}, "
                    f"y={_number(remediation.y_mm, 'mm')} "
                    f"{_esc(_layer_name(remediation.layer))}</td></tr>")
    alternatives = ""
    if remediation.alternatives:
        items = "".join(f"<li>{_t(item)}</li>" for item in remediation.alternatives)
        alternatives = f'<tr><th>Alternatives</th><td><ul class="tight">{items}</ul></td></tr>'
    unit = _units(remediation.unit)
    return (
        "<table><tbody>"
        f"<tr><th>Action</th><td><code>{_esc(remediation.action)}</code> {marker}</td></tr>"
        f"<tr><th>Target</th><td>{_esc(remediation.target) or '&mdash;'}</td></tr>"
        f"<tr><th>Change</th><td>{_number(remediation.current_value, unit)}"
        f" &rarr; {_number(remediation.proposed_value, unit)}</td></tr>"
        f"<tr><th>Predicted gain</th><td>{_t(remediation.predicted_gain) or '&mdash;'}</td></tr>"
        f"<tr><th>Effort</th><td>{_esc(remediation.effort)}</td></tr>"
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


def _paragraphs(lines: List[str]) -> str:
    if len(lines) == 1:
        return f"<p>{_t(lines[0])}</p>"
    return '<ul class="tight">' + "".join(f"<li>{_t(line)}</li>" for line in lines) + "</ul>"


def _render_action_detail(action: CampaignAction) -> str:
    rows = "".join(
        f"<tr><td>{_esc(item.get('domain', ''))}</td>"
        f"<td><code>{_esc(item.get('rule_id', ''))}</code></td>"
        f"<td>{_t(item.get('title', ''))}</td><td>{_t(item.get('description', ''))}</td>"
        f'<td><span class="confidence">{_esc(item.get("confidence", ""))}</span></td></tr>'
        for item in action.details
    )
    table = (
        "<table><thead><tr><th>Analysis</th><th>Rule</th><th>Finding</th>"
        f"<th>Description</th><th>Certainty</th></tr></thead><tbody>{rows}</tbody></table>"
        if rows else ""
    )
    return (
        "<details><summary>Technical detail</summary>"
        f"{table}"
        f'<p class="meta">Priority {action.gain_rank:.1f} (see "Priority" under How to '
        "read this report).</p></details>"
    )


def _render_actions(campaign: CampaignResult) -> str:
    if not campaign.actions:
        return ('<section id="actions"><h2>2. Actions</h2>'
                '<p class="muted">No actionable findings.</p></section>')
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
            todo = '<p class="muted">No change was computed for this action.</p>'
        why = _guide_lines(action, "why")
        verify = _guide_lines(action, "verify")
        where = _where(action.components, action.nets) or "&mdash;"
        certainty = _CERTAINTY_WORDS.get(action.confidence, action.confidence)
        effort = _EFFORT_WORDS.get(action.effort, str(action.effort))
        blocks.append(
            f'<article class="action {border}" id="{_esc(action.action_id)}">'
            f"<h3>{_severity_badge(action.severity)} {_t(action.title)}</h3>"
            f'<p class="meta">Where: {where} &middot; found by {_join(action.domains)}</p>'
            '<dl class="card">'
            + (f"<dt>Why it is a risk</dt><dd>{_paragraphs(why)}</dd>" if why else "")
            + f"<dt>What to do</dt><dd>{todo}</dd>"
            + (f"<dt>How to check</dt><dd>{_paragraphs(verify)}</dd>" if verify else "")
            + f"<dt>How sure</dt><dd>{_esc(certainty)}</dd>"
            f"<dt>Typical effort</dt><dd>{_esc(effort)}</dd>"
            "</dl>"
            f"{_render_action_detail(action)}"
            "</article>"
        )
    return f'<section id="actions"><h2>2. Actions</h2>{"".join(blocks)}</section>'


# ----------------------------------------------------------------------
# Section 3 -- per domain
# ----------------------------------------------------------------------

def _render_metrics(result: AnalysisResult) -> str:
    if not result.metrics:
        return '<p class="muted">No figures recorded.</p>'
    rows = []
    for metric in result.metrics:
        value = metric.value
        if _numeric(metric):
            value_html = _number(float(value), precision=metric.precision)
        else:
            value_html = _t(value)
        # The internal key stays as a tooltip for a reader matching the CSV,
        # not as a column: 205 rows of P02 Rev E's report showed one (LE-4).
        rows.append(
            f'<tr><td title="{_esc(metric.key)}">{_t(metric.label)}</td>'
            f"<td>{value_html}</td><td>{_t(metric.unit)}</td>"
            f"<td>{_esc(metric.status)}</td></tr>"
        )
    return (f"<details><summary>All {len(result.metrics)} figures</summary>"
            "<table><thead><tr><th>Figure</th><th>Value</th>"
            "<th>Unit</th><th>Status</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></details>")


def _render_findings(result: AnalysisResult) -> str:
    if not result.findings:
        return '<p class="muted">No findings.</p>'
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
        blocks.append(
            f'<details><summary><span class="badge {_SEVERITY_CLASS[severity]}">'
            f"{_esc(severity)}</span> &mdash; {len(group)} finding(s)</summary>"
            "<table><thead><tr><th>Rule</th><th>Title</th><th>Description</th>"
            "<th>Nets</th><th>Components</th><th>Recommendation</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></details>"
        )
    return "".join(blocks)


def _render_artifacts(result: AnalysisResult, embed_images: bool, search_roots=()) -> str:
    if not result.artifacts:
        return ""
    blocks = []
    for artifact in result.artifacts:
        title = _esc(artifact.title)
        raw_path = str(artifact.path or "")
        if not embed_images:
            blocks.append(f'<p class="meta">{title} &mdash; <code>{_esc(raw_path)}</code></p>')
            continue
        resolved = _resolve_artifact(raw_path, search_roots)
        uri = _data_uri(resolved) if resolved is not None else None
        if uri is None:
            blocks.append(
                f'<div class="missing-artifact"><strong>Artifact unavailable:</strong> '
                f"{title}<br>Expected at <code>{_esc(raw_path) or '(no path recorded)'}</code>"
                "</div>"
            )
        else:
            blocks.append(
                f'<figure><img src="{uri}" alt="{title}">'
                f"<figcaption>{title}</figcaption></figure>"
            )
    return f"<h4>Artifacts</h4>{''.join(blocks)}"


def _render_domains(campaign: CampaignResult, embed_images: bool, search_roots=()) -> str:
    if not campaign.results:
        return ('<section id="domains"><h2>3. Per domain</h2>'
                '<p class="muted">No analysis results.</p></section>')
    blocks = []
    for result in campaign.results:
        status_class = _STATUS_CLASS.get(result.status, "nodata")
        figures = "".join(f"<li>{line}</li>" for line in key_metrics(result))
        blocks.append(
            f"<h3>{_esc(result.analysis_type)} &mdash; {_esc(result.title)} "
            f'<span class="verdict {status_class}">{_esc(result.status)}</span></h3>'
            + (f'<ul class="tight">{figures}</ul>' if figures else "")
            + f"{_render_metrics(result)}"
            "<h4>Findings</h4>"
            f"{_render_findings(result)}"
            f"{_render_artifacts(result, embed_images, search_roots)}"
        )
    return f'<section id="domains"><h2>3. Per domain</h2>{"".join(blocks)}</section>'


# ----------------------------------------------------------------------
# Section 4 -- appendices
# ----------------------------------------------------------------------

_MODE_LABELS = {"single_board": "single-board", "zeo_container": "Zeo container", "zeo_member": "Zeo member"}


def _render_project_context(context) -> str:
    if not context:
        return ""
    mode = _MODE_LABELS.get(str(context.get("mode", "")), str(context.get("mode", "")))
    notes = "; ".join(str(note) for note in context.get("notes") or ())
    return (
        f"<tr><th>Host</th><td>{_esc(context.get('host', ''))}</td></tr>"
        f"<tr><th>Project mode</th><td>{_esc(mode)}"
        + (f" &mdash; {_esc(notes)}" if notes else "")
        + "</td></tr>"
    )


def _render_scope(campaign: CampaignResult) -> str:
    """Identifiers and the host context, for traceability rather than for
    the decision: shown at the top before, where "host unknown ... host
    detection inconclusive" read like a result (audit CO-9)."""
    rows = "".join(
        f"<tr><td>{_esc(score.domain)}</td><td>{_esc(score.completed_at) or '&mdash;'}</td>"
        f"<td><code>{_esc(score.board_fingerprint) or '&mdash;'}</code></td>"
        f"<td><code>{_esc(result.run_id)}</code></td>"
        f"<td>{result.elapsed_seconds:.2f} s</td></tr>"
        for score, result in zip(campaign.domain_scores, campaign.results)
    )
    return (
        "<h3>Scope analysed</h3><table><tbody>"
        f"<tr><th>Project</th><td>{_esc(campaign.project_name) or '&mdash;'}</td></tr>"
        f"{_render_project_context(campaign.project_context)}"
        f"<tr><th>Board fingerprint</th><td><code>{_esc(campaign.board_fingerprint) or '&mdash;'}</code></td></tr>"
        f"<tr><th>Campaign</th><td><code>{_esc(campaign.campaign_id)}</code></td></tr>"
        f"<tr><th>Created</th><td>{_esc(campaign.created_at)}</td></tr>"
        f"<tr><th>Total compute</th><td>{campaign.total_elapsed_seconds:.2f} s</td></tr>"
        f"<tr><th>Analyses</th><td>{len(campaign.results)}</td></tr>"
        "</tbody></table>"
        + ("<table><thead><tr><th>Analysis</th><th>Completed</th><th>Board</th>"
           f"<th>Run</th><th>Compute</th></tr></thead><tbody>{rows}</tbody></table>"
           if rows else "")
    )


def _render_mapping(payload: Dict[str, Any]) -> str:
    if not payload:
        return '<p class="muted">None recorded.</p>'
    return f"<pre>{_esc(json.dumps(payload, indent=2, ensure_ascii=False, default=str))}</pre>"


def _render_appendices(campaign: CampaignResult) -> str:
    blocks = []
    for result in campaign.results:
        if result.provenance:
            rows = "".join(
                f"<tr><td>{_esc(item.source)}</td><td>{_t(item.detail)}</td>"
                f"<td>{_esc(item.reference) or '&mdash;'}</td></tr>"
                for item in result.provenance
            )
            provenance = ("<table><thead><tr><th>Source</th><th>Detail</th>"
                          f"<th>Reference</th></tr></thead><tbody>{rows}</tbody></table>")
        else:
            provenance = '<p class="muted">No provenance recorded.</p>'
        if result.limitations:
            items = "".join(f"<li>{_t(line)}</li>" for line in result.limitations)
            limitations = f'<ul class="tight">{items}</ul>'
        else:
            limitations = '<p class="muted">No limitations recorded.</p>'
        blocks.append(
            f"<details><summary>{_esc(result.analysis_type)} &mdash; "
            f"{_esc(result.title)}</summary>"
            f"<h4>Provenance</h4>{provenance}"
            f"<h4>Limitations</h4>{limitations}"
            f"<h4>Configuration</h4>{_render_mapping(result.configuration_snapshot)}"
            f"<h4>Compute metadata</h4>{_render_mapping(result.compute_metadata)}"
            f'<p class="meta">Result schema {_esc(result.schema_version)}</p>'
            "</details>"
        )
    body = "".join(blocks) or '<p class="muted">No results.</p>'
    return (
        '<section id="appendices"><h2>4. Appendices</h2>'
        f"{_render_scope(campaign)}"
        "<h3>Per analysis</h3>"
        f"{body}"
        f'<p class="meta">Campaign schema {_esc(campaign.schema_version)}</p>'
        "</section>"
    )


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

def render_campaign_html(
    campaign: CampaignResult, *, embed_images: bool = True, artifact_roots=(),
) -> str:
    """Render *campaign* as a fully self-contained HTML document.

    ``artifact_roots`` are directories to search for artifact files whose
    recorded path is a bare file name, which is what ProjectResultsHistory
    stores. Without them, every plot renders as "artifact unavailable".
    """
    title = _esc(campaign.project_name or "Ki-PIDA campaign report")
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title} &mdash; Ki-PIDA report</title>"
        f"<style>{_CSS}</style></head><body>"
        f"<h1>{title}</h1>"
        f'<p class="meta">Ki-PIDA consolidated analysis report &middot; '
        f"{_esc(campaign.created_at)}</p>"
        f"{_render_synthesis(campaign)}"
        f"{_render_actions(campaign)}"
        f"{_render_domains(campaign, embed_images, artifact_roots)}"
        f"{_render_appendices(campaign)}"
        "</body></html>\n"
    )


def write_campaign_html(
    campaign: CampaignResult, path: Path, *, embed_images: bool = True,
    artifact_roots=(),
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
            campaign, embed_images=embed_images, artifact_roots=roots,
        ),
        encoding="utf-8",
    )
    return path
