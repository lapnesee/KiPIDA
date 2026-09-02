"""Project result persistence with no import-time dependency on wxPython."""

from datetime import datetime
import json
from pathlib import Path
import re
import shutil

from analysis_contract import AnalysisArtifact, AnalysisResult


class ProjectResultsHistory:
    """Versioned structured results with transparent legacy-index support."""

    INDEX_NAME = "manifest.json"
    LEGACY_INDEX_NAME = "result.json"

    def __init__(self, directory):
        self.directory = Path(directory) if directory else None

    @staticmethod
    def _safe_name(value):
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "result")).strip("_.") or "result"

    def _ensure_directory(self):
        if self.directory is None:
            return None
        self.directory.mkdir(parents=True, exist_ok=True)
        return self.directory

    @staticmethod
    def _plot_artifacts(saved_plots):
        return [
            AnalysisArtifact(
                artifact_id=f"plot-{index:02d}",
                title=str(plot.get("title", "Plot")), kind="plot",
                path=str(plot.get("file", "")), media_type="image/png",
            )
            for index, plot in enumerate(saved_plots, start=1)
        ]

    def entries(self, analysis_id=None, latest_per_analysis=False):
        if self.directory is None or not self.directory.is_dir():
            return []
        entries = []
        paths = list(self.directory.glob(f"*/{self.INDEX_NAME}"))
        paths.extend(self.directory.glob(f"*/{self.LEGACY_INDEX_NAME}"))
        seen = set()
        for index_path in paths:
            if index_path.parent in seen:
                continue
            try:
                metadata = json.loads(index_path.read_text(encoding="utf-8"))
                if not isinstance(metadata, dict) or not metadata.get("analysis_id"):
                    continue
                metadata["directory"] = index_path.parent
                entries.append(metadata)
                seen.add(index_path.parent)
            except (OSError, ValueError, TypeError):
                continue
        entries = sorted(
            entries,
            key=lambda item: (
                str(item.get("created_at", "")),
                Path(item.get("directory", "")).name,
            ),
            reverse=True,
        )
        if analysis_id:
            wanted = str(analysis_id).upper()
            entries = [
                entry for entry in entries
                if str(entry.get("analysis_id", "")).upper() == wanted
            ]
        if latest_per_analysis:
            latest = []
            seen_types = set()
            for entry in entries:
                entry_type = str(entry.get("analysis_id", "UNKNOWN")).upper()
                if entry_type in seen_types:
                    continue
                seen_types.add(entry_type)
                latest.append(entry)
            entries = latest
        return entries

    def save(self, analysis_id, title, report, plots=None, result=None):
        root = self._ensure_directory()
        if root is None:
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = f"{stamp}-{self._safe_name(analysis_id)}"
        entry_dir = root / base
        suffix = 1
        while entry_dir.exists():
            entry_dir = root / f"{base}-{suffix}"
            suffix += 1
        entry_dir.mkdir()
        plot_titles = [entry[0] for entry in (plots or []) if entry]
        result = result or AnalysisResult.legacy_report(
            str(analysis_id), str(title), str(report), plot_titles,
        )
        result.validate()
        metadata = {
            "version": 2,
            "analysis_id": str(analysis_id),
            "title": str(title),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "report_file": "report.txt",
            "result_file": "result.json",
            "schema_version": result.schema_version,
            "run_id": result.run_id,
            "status": result.status.value,
            "severity_counts": result.severity_counts,
            "plots": [],
        }
        (entry_dir / metadata["report_file"]).write_text(str(report), encoding="utf-8")
        metadata["directory"] = entry_dir
        self.update_plots(metadata, plots or [])
        result.artifacts = self._plot_artifacts(metadata.get("plots", []))
        (entry_dir / metadata["result_file"]).write_text(result.to_json(), encoding="utf-8")
        return metadata

    def _write_manifest(self, metadata):
        entry_dir = Path(metadata["directory"])
        serializable = {key: value for key, value in metadata.items() if key != "directory"}
        (entry_dir / self.INDEX_NAME).write_text(
            json.dumps(serializable, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )

    def update_plots(self, metadata, plots):
        if not metadata or not metadata.get("directory"):
            return
        entry_dir = Path(metadata["directory"])
        for previous in entry_dir.glob("plot-*.png"):
            try:
                previous.unlink()
            except OSError:
                pass
        saved = []
        if plots:
            import wx
            for index, entry in enumerate(plots, start=1):
                title, bitmap = entry[:2]
                click_probe = entry[3] if len(entry) > 3 else None
                if bitmap is None or not bitmap.IsOk():
                    continue
                filename = f"plot-{index:02d}-{self._safe_name(title)}.png"
                image = bitmap.ConvertToImage()
                if image.SaveFile(str(entry_dir / filename), wx.BITMAP_TYPE_PNG):
                    plot_metadata = {"title": str(title), "file": filename}
                    if click_probe is not None and hasattr(click_probe, "to_dict"):
                        plot_metadata["click_probe"] = click_probe.to_dict()
                    saved.append(plot_metadata)
        metadata["plots"] = saved
        self._write_manifest(metadata)
        result_file = metadata.get("result_file")
        result_path = entry_dir / str(result_file) if result_file else None
        if result_path is not None and result_path.is_file():
            result = AnalysisResult.from_json(result_path.read_text(encoding="utf-8"))
            result.artifacts = self._plot_artifacts(saved)
            result_path.write_text(result.to_json(), encoding="utf-8")

    def load(self, metadata):
        entry_dir = Path(metadata["directory"])
        report_path = entry_dir / str(metadata.get("report_file", "report.txt"))
        report = report_path.read_text(encoding="utf-8")
        plots = []
        if metadata.get("plots"):
            import wx
            from emc_probe import RenderedPointProbe
            for plot in metadata.get("plots", []):
                if not isinstance(plot, dict):
                    continue
                image = wx.Image(str(entry_dir / str(plot.get("file", ""))))
                if image.IsOk():
                    click_probe = None
                    if isinstance(plot.get("click_probe"), dict):
                        click_probe = RenderedPointProbe.from_dict(plot["click_probe"])
                    plots.append((str(plot.get("title", "Plot")), wx.Bitmap(image), None, click_probe))
        return report, plots

    def load_result(self, metadata):
        entry_dir = Path(metadata["directory"])
        result_file = metadata.get("result_file")
        if result_file:
            result_path = entry_dir / str(result_file)
            if result_path.is_file():
                return AnalysisResult.from_json(result_path.read_text(encoding="utf-8"))
        report_path = entry_dir / str(metadata.get("report_file", "report.txt"))
        report = report_path.read_text(encoding="utf-8") if report_path.is_file() else ""
        return AnalysisResult.legacy_report(
            str(metadata.get("analysis_id", "UNKNOWN")),
            str(metadata.get("title", "Legacy result")), report,
            [plot.get("title", "Plot") for plot in metadata.get("plots", []) if isinstance(plot, dict)],
        )

    def delete(self, metadata):
        if not metadata or self.directory is None:
            return False
        target = Path(metadata.get("directory", "")).resolve()
        root = self.directory.resolve()
        if target.parent != root:
            return False
        shutil.rmtree(target)
        return True

    def clear(self):
        deleted = 0
        for entry in self.entries():
            if self.delete(entry):
                deleted += 1
        return deleted
