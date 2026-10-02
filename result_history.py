"""Project result persistence with no import-time dependency on wxPython."""

from datetime import datetime
import json
from pathlib import Path
import re
import shutil

from analysis_contract import AnalysisArtifact, AnalysisResult


KEEP_RUNS = 5
KEEP_CAMPAIGNS = 5


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
        # The folder sits in the project, often a git repository: P02 Rev E's
        # held 685 MB, untracked, that one "git add -A" would have taken.
        ignore = self.directory / ".gitignore"
        if not ignore.exists():
            try:
                ignore.write_text("# Ki-PIDA results, written by each run.\n*\n", encoding="utf-8")
            except OSError:
                pass
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

    def replace_result(self, run_id, result):
        """File *result* as the saved result of the entry with *run_id*,
        keeping that entry's plots; False when no entry has it.

        In a batch a domain's tab adapts the raw outcome a second time, and
        that copy was the one saved: P02 Rev E's THERMAL of 2026-10-01 22:29
        was archived WARN with 9 figures while the campaign reported it PASS
        with 10, and its CFD archived 4 figures and a 0 s run. The
        campaign's own result replaces it.
        """
        for entry in self.entries():
            if str(entry.get("run_id") or "") != str(run_id):
                continue
            saved = AnalysisResult.from_dict(result.to_dict())
            saved.artifacts = self._plot_artifacts(entry.get("plots", []))
            path = Path(entry["directory"]) / str(entry.get("result_file") or "result.json")
            path.write_text(saved.to_json(), encoding="utf-8")
            entry["status"] = saved.status.value
            entry["severity_counts"] = saved.severity_counts
            self._write_manifest(entry)
            return True
        return False

    def attach_artifacts(self, results):
        """Give each result the plots its history entry holds; return how
        many results gained some.

        Plots are rendered after a result is saved (update_plots writes them
        to result.json only), so the result in memory -- the one a campaign
        aggregates -- keeps the empty list save() gave it: P02 Rev E's
        2026-10-01 00:59 campaign report held no image, though its six
        domains' folders held 249 plots. Entries are matched by run_id, and
        each path is the entry's own file: a plot is recorded by a bare
        name that repeats in every run's folder ("plot-01-CFD_3D.png"), so a
        search of the history by name finds whichever run it meets first.
        """
        by_run = {}
        for entry in self.entries():
            run_id = str(entry.get("run_id") or "")
            if run_id:
                by_run.setdefault(run_id, entry)
        attached = 0
        for result in results or ():
            entry = by_run.get(str(getattr(result, "run_id", "")))
            if entry is None:
                continue
            directory = Path(entry["directory"])
            plots = [
                plot for plot in entry.get("plots", [])
                if isinstance(plot, dict) and (directory / str(plot.get("file", ""))).is_file()
            ]
            if not plots:
                continue
            artifacts = self._plot_artifacts(plots)
            for artifact, plot in zip(artifacts, plots):
                artifact.path = str(directory / str(plot.get("file", "")))
            result.artifacts = artifacts
            attached += 1
        return attached

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

    def cleanup_plan(self, keep_runs=KEEP_RUNS, keep_campaigns=KEEP_CAMPAIGNS):
        """The paths a clean-up deletes, and their size in bytes.

        Kept: the latest *keep_runs* runs of each analysis and of Phase 10's
        export folders, the files of the latest *keep_campaigns* campaigns,
        and every run those campaigns' saved results name. Nothing was ever
        removed but by hand: P02 Rev E's folder grew to 685 MB and 330
        entries in 11 days, about 19 MB per campaign.
        """
        if self.directory is None or not self.directory.is_dir():
            return [], 0
        stamps = sorted({
            match.group(2) for match in (
                re.fullmatch(r"(campaign|findings|actions)-(\d{8}-\d{6})\.(json|html|csv)", path.name)
                for path in self.directory.iterdir()
            ) if match
        }, reverse=True)
        kept_stamps = set(stamps[:keep_campaigns])
        protected = set()
        for stamp in kept_stamps:
            try:
                saved = json.loads((self.directory / f"campaign-{stamp}.json").read_text(encoding="utf-8"))
                protected.update(str(item.get("run_id", "")) for item in saved.get("results", []))
            except (OSError, ValueError, AttributeError):
                continue
        doomed = [
            path for path in self.directory.iterdir()
            if re.fullmatch(r"(campaign|findings|actions)-(\d{8}-\d{6})\.(json|html|csv)", path.name)
            and path.name.split("-", 1)[1].rsplit(".", 1)[0] not in kept_stamps
        ]
        counts = {}
        for entry in self.entries():
            analysis = str(entry.get("analysis_id", "")).upper()
            counts[analysis] = counts.get(analysis, 0) + 1
            if counts[analysis] > keep_runs and str(entry.get("run_id", "")) not in protected:
                doomed.append(Path(entry["directory"]))
        exports = sorted((path for path in self.directory.iterdir()
                          if path.is_dir() and "-EMC-PHASE10" in path.name), reverse=True)
        doomed.extend(exports[keep_runs:])
        size = sum(
            item.stat().st_size for path in doomed
            for item in ([path] if path.is_file() else path.rglob("*")) if item.is_file()
        )
        return doomed, size

    def cleanup(self, keep_runs=KEEP_RUNS, keep_campaigns=KEEP_CAMPAIGNS):
        """Delete what cleanup_plan names; (paths deleted, bytes freed)."""
        doomed, size = self.cleanup_plan(keep_runs, keep_campaigns)
        root = self.directory.resolve() if self.directory is not None else None
        deleted = 0
        for path in doomed:
            if root is None or path.resolve().parent != root:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted += 1
        return deleted, size
