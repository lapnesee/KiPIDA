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
        class FakeImage:
            def SaveFile(self, path, _kind):
                Path(path).write_bytes(b"png")
                return True

        class FakeBitmap:
            def IsOk(self):
                return True

            def ConvertToImage(self):
                return FakeImage()

        fake_wx = types.ModuleType("wx")
        fake_wx.BITMAP_TYPE_PNG = 1
        with tempfile.TemporaryDirectory() as directory:
            history = ProjectResultsHistory(directory)
            result = AnalysisResult("THERMAL", "Thermal").finish()
            entry = history.save("THERMAL", "Thermal", "report", result=result)
            with patch.dict(sys.modules, {"wx": fake_wx}):
                history.update_plots(entry, [("Top Surface", FakeBitmap())])
            loaded = history.load_result(entry)
            self.assertEqual(len(loaded.artifacts), 1)
            self.assertEqual(loaded.artifacts[0].title, "Top Surface")
            self.assertEqual(loaded.artifacts[0].media_type, "image/png")

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
