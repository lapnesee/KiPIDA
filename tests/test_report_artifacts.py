"""Regression: plots recorded by name must still be found and embedded.

ProjectResultsHistory stores AnalysisArtifact.path as a bare file name and
writes the file into a per-entry subdirectory. The report resolved it against
the process working directory, so every plot rendered as "artifact
unavailable" -- observed on the first real p02_alimentation report, which
showed both differential plots missing.
"""

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from analysis_contract import AnalysisArtifact, AnalysisResult, AnalysisStatus
from campaign import CampaignResult
from report.html_report import write_campaign_html

# Smallest valid PNG (1x1, transparent).
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _campaign_with_plot(name="plot-01-Differential_Z.png"):
    result = AnalysisResult(
        "DIFFERENTIAL", "Differential Pairs", status=AnalysisStatus.WARN,
    )
    result.artifacts.append(AnalysisArtifact(
        artifact_id="plot-01", title="Differential Z", kind="plot",
        path=name, media_type="image/png",
    ))
    return CampaignResult(project_name="p02", results=[result]).recompute()


class ArtifactResolutionTests(unittest.TestCase):
    def test_plot_in_history_subdirectory_is_embedded(self):
        # The real layout: KiPIDA-results/<stamp>-DIFFERENTIAL/<plot>.png
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = root / "20260903-213518-DIFFERENTIAL"
            entry.mkdir()
            (entry / "plot-01-Differential_Z.png").write_bytes(_PNG)

            html = write_campaign_html(
                _campaign_with_plot(), root / "campaign.html",
            ).read_text(encoding="utf-8")

            self.assertIn("data:image/png;base64,", html)
            self.assertNotIn("Artifact unavailable", html)

    def test_missing_plot_still_reports_unavailable(self):
        # The honest fallback must survive: a genuinely absent file says so
        # rather than silently vanishing from the report.
        with tempfile.TemporaryDirectory() as tmp:
            html = write_campaign_html(
                _campaign_with_plot(), Path(tmp) / "campaign.html",
            ).read_text(encoding="utf-8")

            self.assertIn("Artifact unavailable", html)
            self.assertNotIn("data:image/png;base64,", html)


if __name__ == "__main__":
    unittest.main()
