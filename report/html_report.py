"""Standalone HTML campaign report.

Everything is inline: CSS in a <style> block, images as base64 data URIs,
collapsible sections via native <details>.  No external assets, no CDN, no
JavaScript -- the file opens over file:// on a machine with no network and
renders identically.

Every number carried by a finding is shown next to its confidence label.
The result contract has always recorded whether a value is DETERMINISTIC,
MEASURED, ESTIMATED or HEURISTIC; a report that hides that distinction
invites the reader to treat an estimate as a measurement.
"""

from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional
import base64
import html
import json

from analysis_contract import (
    AnalysisArtifact,
    AnalysisFinding,
    AnalysisResult,
    AnalysisStatus,
    FindingSeverity,
    Remediation,
)
from campaign import CampaignAction, CampaignResult, DomainScore

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
    return f'<span class="confidence">{_esc(finding.confidence)}</span>'


def _number(value: Optional[float], unit: str = "") -> str:
    if value is None:
        return "&mdash;"
    text = f"{value:g}"
    return f"{_esc(text)}&nbsp;{_esc(unit)}" if unit else _esc(text)


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
# Section 1 -- synthesis
# ----------------------------------------------------------------------

def _render_scores(scores: List[DomainScore]) -> str:
    if not scores:
        return '<p class="muted">No domain produced a result.</p>'
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
            f'<span class="badge {_SEVERITY_CLASS[sev]}">{sev.value[:4]} {score.finding_counts.get(sev.value, 0)}</span>'
            for sev in FindingSeverity if score.finding_counts.get(sev.value, 0)
        ) or '<span class="muted">none</span>'
        rows.append(
            f"<tr><th>{_esc(score.domain)}</th>"
            f'<td><span class="verdict {status_class}">{_esc(score.status)}</span></td>'
            f"<td>{bar}</td><td>{value}</td><td>{counts}</td>"
            f"<td>{_esc(score.headline)}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Domain</th><th>Status</th><th>Score</th>"
        "<th></th><th>Findings</th><th>Headline</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_top_actions(actions: List[CampaignAction]) -> str:
    if not actions:
        return '<p class="muted">No actionable findings.</p>'
    rows = []
    for index, action in enumerate(actions, start=1):
        rows.append(
            f"<tr><td>{index}</td>"
            f'<td><span class="badge {_SEVERITY_CLASS[action.severity]}">'
            f"{_esc(action.severity)}</span></td>"
            f"<td>{_esc(action.title)}</td>"
            f"<td>{_join(action.domains)}</td>"
            f"<td>{_esc(action.effort)}</td>"
            f"<td>{action.gain_rank:.1f}</td></tr>"
        )
    return (
        "<table><thead><tr><th>#</th><th>Severity</th><th>Action</th>"
        "<th>Domains</th><th>Effort</th><th>Gain rank</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_synthesis(campaign: CampaignResult) -> str:
    status_class = _STATUS_CLASS.get(campaign.overall_status, "nodata")
    scope = (
        "<table><tbody>"
        f"<tr><th>Project</th><td>{_esc(campaign.project_name) or '&mdash;'}</td></tr>"
        f"<tr><th>Board fingerprint</th><td><code>{_esc(campaign.board_fingerprint) or '&mdash;'}</code></td></tr>"
        f"<tr><th>Campaign</th><td><code>{_esc(campaign.campaign_id)}</code></td></tr>"
        f"<tr><th>Created</th><td>{_esc(campaign.created_at)}</td></tr>"
        f"<tr><th>Total compute</th><td>{campaign.total_elapsed_seconds:.2f} s</td></tr>"
        f"<tr><th>Analyses</th><td>{len(campaign.results)}</td></tr>"
        "</tbody></table>"
    )
    return (
        '<section id="synthesis"><h2>1. Synthesis</h2>'
        f'<p>Overall verdict: <span class="verdict {status_class}">'
        f"{_esc(campaign.overall_status)}</span></p>"
        "<h3>Domain scores</h3>"
        f"{_render_scores(campaign.domain_scores)}"
        "<h3>Priority actions</h3>"
        f"{_render_top_actions(campaign.top_actions(10))}"
        "<h3>Scope analysed</h3>"
        f"{scope}</section>"
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
                    f"{_esc(remediation.layer)}</td></tr>")
    alternatives = ""
    if remediation.alternatives:
        items = "".join(f"<li>{_esc(item)}</li>" for item in remediation.alternatives)
        alternatives = f'<tr><th>Alternatives</th><td><ul class="tight">{items}</ul></td></tr>'
    return (
        "<table><tbody>"
        f"<tr><th>Action</th><td><code>{_esc(remediation.action)}</code> {marker}</td></tr>"
        f"<tr><th>Target</th><td>{_esc(remediation.target) or '&mdash;'}</td></tr>"
        f"<tr><th>Change</th><td>{_number(remediation.current_value, remediation.unit)}"
        f" &rarr; {_number(remediation.proposed_value, remediation.unit)}</td></tr>"
        f"<tr><th>Predicted gain</th><td>{_esc(remediation.predicted_gain) or '&mdash;'}</td></tr>"
        f"<tr><th>Effort</th><td>{_esc(remediation.effort)}</td></tr>"
        f"{location}{alternatives}"
        "</tbody></table>"
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
        consequences = "".join(f"<li>{_esc(line)}</li>" for line in action.consequences)
        if action.remediations:
            remediations = "".join(_render_remediation(item) for item in action.remediations)
        else:
            remediations = ('<p class="muted">No structured remediation was computed '
                            'for this action.</p>')
        blocks.append(
            f'<article class="action {border}">'
            f'<h3><span class="badge {_SEVERITY_CLASS[action.severity]}">'
            f"{_esc(action.severity)}</span> {_esc(action.title)}</h3>"
            f'<p class="meta">Domains: {_join(action.domains)} &middot; '
            f"Rules: {_join(action.rule_ids)} &middot; Effort: {_esc(action.effort)} "
            f"&middot; Gain rank: {action.gain_rank:.1f}</p>"
            f'<p class="meta">Nets: {_join(action.nets)} &middot; '
            f"Components: {_join(action.components)}</p>"
            '<p><strong>Consequences</strong></p>'
            f'<ul class="tight">{consequences}</ul>'
            f"<p><strong>Remediation</strong></p>{remediations}"
            "</article>"
        )
    return f'<section id="actions"><h2>2. Actions</h2>{"".join(blocks)}</section>'


# ----------------------------------------------------------------------
# Section 3 -- per domain
# ----------------------------------------------------------------------

def _render_metrics(result: AnalysisResult) -> str:
    if not result.metrics:
        return '<p class="muted">No metrics recorded.</p>'
    rows = []
    for metric in result.metrics:
        value = metric.value
        if isinstance(value, float) and metric.precision is not None:
            value = f"{value:.{metric.precision}f}"
        rows.append(
            f"<tr><td>{_esc(metric.label)}</td>"
            f"<td><code>{_esc(metric.key)}</code></td>"
            f"<td>{_esc(value)}</td><td>{_esc(metric.unit)}</td>"
            f"<td>{_esc(metric.status)}</td></tr>"
        )
    return ("<table><thead><tr><th>Metric</th><th>Key</th><th>Value</th>"
            "<th>Unit</th><th>Status</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


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
                f"<td>{_esc(finding.title)}{_confidence_badge(finding)}</td>"
                f"<td>{_esc(finding.description)}</td>"
                f"<td>{_join(finding.nets)}</td>"
                f"<td>{_join(finding.components)}</td>"
                f"<td>{_esc(finding.recommendation) or '&mdash;'}</td></tr>"
            )
        blocks.append(
            f'<details><summary><span class="badge {_SEVERITY_CLASS[severity]}">'
            f"{_esc(severity)}</span> &mdash; {len(group)} finding(s)</summary>"
            "<table><thead><tr><th>Rule</th><th>Title</th><th>Description</th>"
            "<th>Nets</th><th>Components</th><th>Recommendation</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></details>"
        )
    return "".join(blocks)


def _render_artifacts(result: AnalysisResult, embed_images: bool) -> str:
    if not result.artifacts:
        return ""
    blocks = []
    for artifact in result.artifacts:
        title = _esc(artifact.title)
        raw_path = str(artifact.path or "")
        if not embed_images:
            blocks.append(f'<p class="meta">{title} &mdash; <code>{_esc(raw_path)}</code></p>')
            continue
        uri = _data_uri(Path(raw_path)) if raw_path else None
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


def _render_domains(campaign: CampaignResult, embed_images: bool) -> str:
    if not campaign.results:
        return ('<section id="domains"><h2>3. Per domain</h2>'
                '<p class="muted">No analysis results.</p></section>')
    blocks = []
    for result in campaign.results:
        status_class = _STATUS_CLASS.get(result.status, "nodata")
        blocks.append(
            f"<h3>{_esc(result.analysis_type)} &mdash; {_esc(result.title)} "
            f'<span class="verdict {status_class}">{_esc(result.status)}</span></h3>'
            f'<p class="meta">Run <code>{_esc(result.run_id)}</code> &middot; '
            f"{result.elapsed_seconds:.2f} s</p>"
            "<h4>Metrics</h4>"
            f"{_render_metrics(result)}"
            "<h4>Findings</h4>"
            f"{_render_findings(result)}"
            f"{_render_artifacts(result, embed_images)}"
        )
    return f'<section id="domains"><h2>3. Per domain</h2>{"".join(blocks)}</section>'


# ----------------------------------------------------------------------
# Section 4 -- appendices
# ----------------------------------------------------------------------

def _render_mapping(payload: Dict[str, Any]) -> str:
    if not payload:
        return '<p class="muted">None recorded.</p>'
    return f"<pre>{_esc(json.dumps(payload, indent=2, ensure_ascii=False, default=str))}</pre>"


def _render_appendices(campaign: CampaignResult) -> str:
    blocks = []
    for result in campaign.results:
        if result.provenance:
            rows = "".join(
                f"<tr><td>{_esc(item.source)}</td><td>{_esc(item.detail)}</td>"
                f"<td>{_esc(item.reference) or '&mdash;'}</td></tr>"
                for item in result.provenance
            )
            provenance = ("<table><thead><tr><th>Source</th><th>Detail</th>"
                          f"<th>Reference</th></tr></thead><tbody>{rows}</tbody></table>")
        else:
            provenance = '<p class="muted">No provenance recorded.</p>'
        if result.limitations:
            items = "".join(f"<li>{_esc(line)}</li>" for line in result.limitations)
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
        f"{body}"
        f'<p class="meta">Campaign schema {_esc(campaign.schema_version)}</p>'
        "</section>"
    )


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

def render_campaign_html(campaign: CampaignResult, *, embed_images: bool = True) -> str:
    """Render *campaign* as a fully self-contained HTML document."""
    title = _esc(campaign.project_name or "Ki-PIDA campaign report")
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title} &mdash; Ki-PIDA report</title>"
        f"<style>{_CSS}</style></head><body>"
        f"<h1>{title}</h1>"
        '<p class="meta">Ki-PIDA consolidated analysis report</p>'
        f"{_render_synthesis(campaign)}"
        f"{_render_actions(campaign)}"
        f"{_render_domains(campaign, embed_images)}"
        f"{_render_appendices(campaign)}"
        "</body></html>\n"
    )


def write_campaign_html(
    campaign: CampaignResult, path: Path, *, embed_images: bool = True,
) -> Path:
    """Render *campaign* and write it to *path*, creating parent directories."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_campaign_html(campaign, embed_images=embed_images), encoding="utf-8",
    )
    return path
