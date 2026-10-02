import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
import types

from result_history import ProjectResultsHistory

from analysis_contract import AnalysisResult, AnalysisStatus
from application.result_detail_presenter import format_result_basis


class FakeImage:
    def SaveFile(self, path, _kind):
        Path(path).write_bytes(b"png")
        return True


class FakeBitmap:
    def IsOk(self):
        return True

    def ConvertToImage(self):
        return FakeImage()


FAKE_WX = types.ModuleType("wx")
FAKE_WX.BITMAP_TYPE_PNG = 1


class ProjectResultsHistoryTests(unittest.TestCase):
    def test_v2_history_persists_structured_result_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            history = ProjectResultsHistory(directory)
            result = AnalysisResult("DC", "DC Power", status=AnalysisStatus.WARN).finish()
            entry = history.save("DC", "DC Power", "Readable report", result=result)
            self.assertTrue((Path(entry["directory"]) / "result.json").is_file())
            metadata = json.loads((Path(entry["directory"]) / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "WARN")
            self.assertEqual(history.load_result(entry).run_id, result.run_id)
            self.assertEqual(history.entries()[0]["run_id"], result.run_id)
            report, plots = history.load(entry)
            self.assertEqual(report, "Readable report")
            self.assertEqual(plots, [])

    def test_legacy_history_entry_is_wrapped_without_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            entry_dir = Path(directory) / "legacy"
            entry_dir.mkdir()
            (entry_dir / "report.txt").write_text("Old report", encoding="utf-8")
            entry = {
                "directory": entry_dir, "version": 1, "analysis_id": "AC",
                "title": "AC Impedance", "report_file": "report.txt", "plots": [],
            }
            result = ProjectResultsHistory(directory).load_result(entry)
            self.assertEqual(result.analysis_type, "AC")
            self.assertTrue(result.summary["legacy_report"])
            basis = format_result_basis(result)
            self.assertIn("LEGACY_HISTORY", basis)
            self.assertIn("findings, metrics, and evidence were not structured", basis)

    def test_late_plots_update_structured_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            history = ProjectResultsHistory(directory)
            result = AnalysisResult("THERMAL", "Thermal").finish()
            entry = history.save("THERMAL", "Thermal", "report", result=result)
            with patch.dict(sys.modules, {"wx": FAKE_WX}):
                history.update_plots(entry, [("Top Surface", FakeBitmap())])
            loaded = history.load_result(entry)
            self.assertEqual(len(loaded.artifacts), 1)
            self.assertEqual(loaded.artifacts[0].title, "Top Surface")
            self.assertEqual(loaded.artifacts[0].media_type, "image/png")

    def test_a_campaign_result_gets_its_own_runs_plots(self):
        # P02 Rev E, 2026-10-01 00:59: the campaign's results in memory had
        # no artifacts (plots come after save), so the report held no image;
        # every CFD run's folder holds a plot-01-CFD_3D.png.
        with tempfile.TemporaryDirectory() as directory:
            history = ProjectResultsHistory(directory)
            runs = []
            for _ in range(2):
                result = AnalysisResult("CFD", "Enclosure CFD").finish()
                entry = history.save("CFD", "Enclosure CFD", "report", result=result)
                with patch.dict(sys.modules, {"wx": FAKE_WX}):
                    history.update_plots(entry, [("CFD 3D", FakeBitmap())])
                runs.append((result, entry))
            latest, entry = runs[1]
            self.assertEqual(latest.artifacts, [])
            self.assertEqual(history.attach_artifacts([latest]), 1)
            self.assertEqual(
                Path(latest.artifacts[0].path),
                Path(entry["directory"]) / "plot-01-CFD_3D.png",
            )

    def test_the_campaigns_result_replaces_the_tabs_own_copy(self):
        # P02 Rev E, 2026-10-01 22:29: THERMAL was archived WARN with 9 figures
        # (the tab's adaptation) while the campaign reported it PASS with 10.
        from analysis_contract import AnalysisMetric

        with tempfile.TemporaryDirectory() as directory:
            history = ProjectResultsHistory(directory)
            tab = AnalysisResult("THERMAL", "3D Thermal", status=AnalysisStatus.WARN).finish()
            entry = history.save("THERMAL", "3D Thermal", "report", result=tab)
            with patch.dict(sys.modules, {"wx": FAKE_WX}):
                history.update_plots(entry, [("Top Surface", FakeBitmap())])
            campaign = AnalysisResult("THERMAL", "3D Thermal", status=AnalysisStatus.PASS,
                                      run_id=tab.run_id, elapsed_seconds=89.19, metrics=[
                                          AnalysisMetric("internal_air_c", "Internal air", 61.2, "°C"),
                                      ]).finish()
            self.assertTrue(history.replace_result(tab.run_id, campaign))
            saved = history.load_result(history.entries()[0])
            self.assertEqual((saved.status, saved.elapsed_seconds), (AnalysisStatus.PASS, 89.19))
            self.assertEqual([metric.key for metric in saved.metrics], ["internal_air_c"])
            self.assertEqual([artifact.title for artifact in saved.artifacts], ["Top Surface"])
            self.assertEqual(history.entries()[0]["status"], "PASS")
            self.assertFalse(history.replace_result("no-such-run", campaign))

    def test_a_clean_up_keeps_the_latest_runs_and_what_campaigns_name(self):
        # P02 Rev E's results folder reached 685 MB and 330 entries in 11
        # days, untracked in the project's git repository.
        with tempfile.TemporaryDirectory() as directory:
            history = ProjectResultsHistory(directory)
            runs = [history.save("DC", "DC Power", "report",
                                 result=AnalysisResult("DC", "DC Power").finish())
                    for _ in range(7)]
            root = Path(directory)
            self.assertEqual((root / ".gitignore").read_text(encoding="utf-8").splitlines()[-1], "*")
            for day in range(1, 8):
                stamp = f"2026100{day}-120000"
                # The newest campaign names the oldest DC run.
                results = [{"run_id": runs[0]["run_id"]}] if day == 7 else []
                (root / f"campaign-{stamp}.json").write_text(json.dumps({"results": results}),
                                                            encoding="utf-8")
                (root / f"campaign-{stamp}.html").write_text("<html></html>", encoding="utf-8")
                (root / f"20261001-12000{day}-EMC-PHASE10").mkdir()
            doomed, size = history.cleanup_plan(keep_runs=5, keep_campaigns=5)
            names = sorted(path.name for path in doomed)
            self.assertEqual([name for name in names if name.startswith("campaign-")], [
                "campaign-20261001-120000.html", "campaign-20261001-120000.json",
                "campaign-20261002-120000.html", "campaign-20261002-120000.json",
            ])
            self.assertEqual([name for name in names if "PHASE10" in name],
                             ["20261001-120001-EMC-PHASE10", "20261001-120002-EMC-PHASE10"])
            dc_doomed = [path for path in doomed if (path / "manifest.json").exists()]
            self.assertEqual(dc_doomed, [Path(runs[1]["directory"])])
            self.assertGreater(size, 0)
            self.assertEqual(history.cleanup(keep_runs=5, keep_campaigns=5)[0], len(doomed))
            self.assertEqual(len(history.entries()), 6)
            self.assertTrue(Path(runs[0]["directory"]).exists())

    def test_entries_can_filter_type_and_keep_latest_per_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            history = ProjectResultsHistory(directory)
            first_dc = history.save("DC", "DC Power", "first")
            history.save("THERMAL", "3D Thermal", "thermal")
            second_dc = history.save("DC", "DC Power", "second")

            dc_entries = history.entries(analysis_id="dc")
            self.assertEqual(len(dc_entries), 2)
            latest = history.entries(latest_per_analysis=True)
            self.assertEqual({entry["analysis_id"] for entry in latest}, {"DC", "THERMAL"})
            latest_dc = next(entry for entry in latest if entry["analysis_id"] == "DC")
            self.assertEqual(Path(latest_dc["directory"]), Path(second_dc["directory"]))
            self.assertNotEqual(Path(latest_dc["directory"]), Path(first_dc["directory"]))


if __name__ == "__main__":
    unittest.main()
