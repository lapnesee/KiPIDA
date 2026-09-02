"""Pure text presenters for structured result evidence and finding details."""


def _evidence_line(item):
    reference = f" [{item.reference}]" if getattr(item, "reference", "") else ""
    location = []
    if getattr(item, "layer", ""):
        location.append(f"layer {item.layer}")
    if getattr(item, "x_mm", None) is not None and getattr(item, "y_mm", None) is not None:
        location.append(f"x={item.x_mm:g} mm, y={item.y_mm:g} mm")
    suffix = f" ({'; '.join(location)})" if location else ""
    return f"- {item.source}{reference}: {item.detail}{suffix}"


def format_finding_detail(finding):
    if finding is None:
        return "Select a finding to inspect its description, recommendation, and evidence."
    lines = [
        f"{finding.rule_id} — {finding.severity.value} — {finding.confidence.value}",
        f"Category: {finding.category}",
        "",
        "Finding",
        "-------",
        finding.title,
        finding.description or "No detailed description was supplied.",
        "",
        "Recommendation",
        "--------------",
        finding.recommendation or "No recommendation was supplied.",
        "",
        f"Nets: {', '.join(finding.nets) if finding.nets else 'None recorded'}",
        f"Components: {', '.join(finding.components) if finding.components else 'None recorded'}",
        "",
        "Finding evidence",
        "----------------",
    ]
    if finding.evidence:
        lines.extend(_evidence_line(item) for item in finding.evidence)
    else:
        lines.append("No finding-specific evidence was recorded.")
    return "\n".join(lines)


def format_result_basis(result):
    if result is None:
        return "No structured evidence basis is available."
    lines = ["Provenance", "----------"]
    if result.provenance:
        lines.extend(_evidence_line(item) for item in result.provenance)
    else:
        lines.append("No provenance source was recorded.")
    lines.extend(["", "Model limitations", "-----------------"])
    if result.limitations:
        lines.extend(f"- {item}" for item in result.limitations)
    else:
        lines.append("No model limitation was recorded.")
    return "\n".join(lines)
