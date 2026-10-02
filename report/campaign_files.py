"""The files a campaign leaves beside the board: its report, its exports and
the campaign itself.

Only the HTML report and two CSV files were written. The campaign was not:
nothing could compare a later run with it, and a report could not be
rebuilt from what produced it. ``campaign-<stamp>.json`` is that record.
"""

from pathlib import Path
from typing import Dict, Iterable

from report.csv_export import write_actions_csv, write_findings_csv
from report.html_report import write_campaign_html


def write_campaign_files(campaign, directory, stamp: str,
                         artifact_roots: Iterable = ()) -> Dict[str, Path]:
    """Write campaign-<stamp>.json and .html, findings- and actions-<stamp>.csv."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": directory / f"campaign-{stamp}.json",
        "html": directory / f"campaign-{stamp}.html",
        "findings": directory / f"findings-{stamp}.csv",
        "actions": directory / f"actions-{stamp}.csv",
    }
    paths["json"].write_text(campaign.to_json(), encoding="utf-8")
    write_campaign_html(campaign, paths["html"], artifact_roots=tuple(artifact_roots))
    write_findings_csv(campaign, paths["findings"])
    write_actions_csv(campaign, paths["actions"])
    return paths
