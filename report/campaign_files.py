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


def previous_campaign(directory, log=None):
    """The latest campaign saved in *directory*, or None. One that cannot be
    read is skipped with a word to *log*, never an error."""
    from campaign import CampaignResult

    for path in sorted(Path(directory).glob("campaign-*.json"), reverse=True):
        try:
            return CampaignResult.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if log:
                log(f"Campaign {path.name} could not be read for comparison: {exc}")
    return None


def write_campaign_files(campaign, directory, stamp: str,
                         artifact_roots: Iterable = (), log=None) -> Dict[str, Path]:
    """Write campaign-<stamp>.json and .html, findings- and actions-<stamp>.csv.

    The report compares the campaign with the latest one saved before it.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    previous = previous_campaign(directory, log)
    paths = {
        "json": directory / f"campaign-{stamp}.json",
        "html": directory / f"campaign-{stamp}.html",
        "findings": directory / f"findings-{stamp}.csv",
        "actions": directory / f"actions-{stamp}.csv",
    }
    paths["json"].write_text(campaign.to_json(), encoding="utf-8")
    write_campaign_html(campaign, paths["html"], artifact_roots=tuple(artifact_roots),
                        previous=previous)
    write_findings_csv(campaign, paths["findings"])
    write_actions_csv(campaign, paths["actions"])
    return paths
