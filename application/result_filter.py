"""Pure filtering rules shared by the Analysis Results workspace and tests."""

from analysis_contract import FindingSeverity


SEVERITY_FILTERS = {
    "Critical / High": {FindingSeverity.CRITICAL, FindingSeverity.HIGH},
    "Actionable (Critical–Medium)": {
        FindingSeverity.CRITICAL, FindingSeverity.HIGH, FindingSeverity.MEDIUM,
    },
    "Low / Info": {FindingSeverity.LOW, FindingSeverity.INFO},
}


def filter_findings(findings, severity_filter="All", query=""):
    """Return findings matching the requested severity group and free-text query."""
    accepted = SEVERITY_FILTERS.get(severity_filter)
    needle = (query or "").strip().casefold()
    visible = []
    for finding in findings or ():
        if accepted is not None and finding.severity not in accepted:
            continue
        searchable = " ".join([
            finding.rule_id or "",
            finding.category or "",
            finding.title or "",
            finding.description or "",
            finding.recommendation or "",
            " ".join(finding.nets or ()),
            " ".join(finding.components or ()),
        ]).casefold()
        if needle and needle not in searchable:
            continue
        visible.append(finding)
    return visible
