"""Stable, JSON-serializable contracts shared by every Ki-PIDA analysis.

Solver-specific result objects remain useful inside each numerical domain.  The
types in this module form the boundary consumed by the UI, history, exporters,
and future comparison tooling.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
import json
import uuid


SCHEMA_VERSION = "1.0.0"


class _StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class AnalysisStatus(_StringEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    NO_DATA = "NO_DATA"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


class FindingSeverity(_StringEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class EvidenceConfidence(_StringEnum):
    DETERMINISTIC = "DETERMINISTIC"
    DATASHEET_BACKED = "DATASHEET_BACKED"
    MEASURED = "MEASURED"
    ESTIMATED = "ESTIMATED"
    HEURISTIC = "HEURISTIC"


def normalize_evidence_confidence(value: Any) -> EvidenceConfidence:
    """Normalize legacy certainty labels at every UI/result boundary."""
    if isinstance(value, EvidenceConfidence):
        return value
    name = str(value or "LOW").upper().replace("-", "_")
    direct = {item.value: item for item in EvidenceConfidence}
    if name in direct:
        return direct[name]
    return {
        "HIGH": EvidenceConfidence.DETERMINISTIC,
        "MEDIUM": EvidenceConfidence.ESTIMATED,
        "LOW": EvidenceConfidence.HEURISTIC,
    }.get(name, EvidenceConfidence.HEURISTIC)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _json_safe(value: Any) -> Any:
    """Convert common scientific/Python values without importing NumPy."""
    value = _enum_value(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, complex):
        return {"real": value.real, "imag": value.imag}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except (TypeError, ValueError):
            pass
    if hasattr(value, "to_dict"):
        return _json_safe(value.to_dict())
    if hasattr(value, "__dataclass_fields__"):
        return _json_safe(asdict(value))
    return str(value)


@dataclass
class AnalysisEvidence:
    source: str
    detail: str
    reference: str = ""
    x_mm: Optional[float] = None
    y_mm: Optional[float] = None
    layer: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AnalysisEvidence":
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


class RemediationEffort(_StringEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass
class Remediation:
    """A structured, quantified corrective action attached to a finding.

    The free-text ``AnalysisFinding.recommendation`` says *what* to consider;
    this says *where*, *from what value to what value*, and *what it buys* --
    the difference between an audit note and an actionable design change.

    ``predicted_gain`` is a human-readable statement of the expected effect
    ("drop 3.1% -> 1.8%").  Whether that prediction has actually been
    re-simulated is carried by ``verified``: an unverified prediction is a
    first-order estimate, a verified one has been re-solved with the change
    applied.  Never present the former as the latter.
    """
    action: str                       # WIDEN_TRACK, ADD_STITCHING_VIAS, MOVE_CAPACITOR...
    target: str = ""                  # refdes / net / segment identifier
    current_value: Optional[float] = None
    proposed_value: Optional[float] = None
    unit: str = ""
    predicted_gain: str = ""
    effort: RemediationEffort = RemediationEffort.MEDIUM
    verified: bool = False            # True only after what-if re-simulation
    x_mm: Optional[float] = None
    y_mm: Optional[float] = None
    layer: str = ""
    alternatives: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.action = str(self.action).strip().upper()
        if not self.action:
            raise ValueError("Remediation.action must not be empty")
        if not isinstance(self.effort, RemediationEffort):
            self.effort = RemediationEffort(str(self.effort).upper())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Remediation":
        payload = dict(data)
        payload["effort"] = RemediationEffort(str(payload.get("effort", "MEDIUM")).upper())
        return cls(**{key: payload[key] for key in cls.__dataclass_fields__ if key in payload})


@dataclass
class AnalysisFinding:
    rule_id: str
    category: str
    severity: FindingSeverity
    title: str
    description: str
    finding_id: str = ""
    recommendation: str = ""
    confidence: EvidenceConfidence = EvidenceConfidence.HEURISTIC
    nets: List[str] = field(default_factory=list)
    components: List[str] = field(default_factory=list)
    evidence: List[AnalysisEvidence] = field(default_factory=list)
    remediations: List[Remediation] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AnalysisFinding":
        payload = dict(data)
        payload["severity"] = FindingSeverity(str(payload.get("severity", "INFO")).upper())
        payload["confidence"] = EvidenceConfidence(
            str(payload.get("confidence", "HEURISTIC")).upper().replace("-", "_")
        )
        payload["evidence"] = [AnalysisEvidence.from_dict(item) for item in payload.get("evidence", [])]
        payload["remediations"] = [
            Remediation.from_dict(item) for item in payload.get("remediations", [])
        ]
        return cls(**{key: payload[key] for key in cls.__dataclass_fields__ if key in payload})


@dataclass
class AnalysisMetric:
    key: str
    label: str
    value: Any
    unit: str = ""
    status: str = ""
    precision: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AnalysisMetric":
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


@dataclass
class AnalysisArtifact:
    artifact_id: str
    title: str
    kind: str
    path: str = ""
    media_type: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AnalysisArtifact":
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})


@dataclass
class AnalysisResult:
    analysis_type: str
    title: str
    status: AnalysisStatus = AnalysisStatus.NO_DATA
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: str = SCHEMA_VERSION
    started_at: str = field(default_factory=_utc_now)
    completed_at: str = ""
    elapsed_seconds: float = 0.0
    board_fingerprint: str = ""
    configuration_snapshot: Dict[str, Any] = field(default_factory=dict)
    summary: Dict[str, Any] = field(default_factory=dict)
    findings: List[AnalysisFinding] = field(default_factory=list)
    metrics: List[AnalysisMetric] = field(default_factory=list)
    artifacts: List[AnalysisArtifact] = field(default_factory=list)
    provenance: List[AnalysisEvidence] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    compute_metadata: Dict[str, Any] = field(default_factory=dict)
    report_file: str = "report.txt"

    def __post_init__(self) -> None:
        self.analysis_type = str(self.analysis_type).strip().upper()
        self.title = str(self.title).strip()
        if not self.analysis_type:
            raise ValueError("analysis_type must not be empty")
        if not self.title:
            raise ValueError("title must not be empty")
        if not isinstance(self.status, AnalysisStatus):
            self.status = AnalysisStatus(str(self.status).upper())

    @property
    def severity_counts(self) -> Dict[str, int]:
        counts = {severity.value: 0 for severity in FindingSeverity}
        for finding in self.findings:
            counts[finding.severity.value] += 1
        return counts

    def finish(self, status: Optional[AnalysisStatus] = None) -> "AnalysisResult":
        self.completed_at = self.completed_at or _utc_now()
        if status is not None:
            self.status = status
        return self

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported analysis result schema: {self.schema_version}")
        finding_ids = [finding.finding_id for finding in self.findings if finding.finding_id]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("non-empty finding_id values must be unique within one result")
        metric_keys = [metric.key for metric in self.metrics]
        if len(metric_keys) != len(set(metric_keys)):
            raise ValueError("metric keys must be unique within one result")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        payload = _json_safe(asdict(self))
        payload["status"] = self.status.value
        payload["severity_counts"] = self.severity_counts
        return payload

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False) + "\n"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AnalysisResult":
        payload = dict(data)
        payload.pop("severity_counts", None)
        payload["status"] = AnalysisStatus(str(payload.get("status", "NO_DATA")).upper())
        payload["findings"] = [AnalysisFinding.from_dict(item) for item in payload.get("findings", [])]
        payload["metrics"] = [AnalysisMetric.from_dict(item) for item in payload.get("metrics", [])]
        payload["artifacts"] = [AnalysisArtifact.from_dict(item) for item in payload.get("artifacts", [])]
        payload["provenance"] = [AnalysisEvidence.from_dict(item) for item in payload.get("provenance", [])]
        result = cls(**{key: payload[key] for key in cls.__dataclass_fields__ if key in payload})
        result.validate()
        return result

    @classmethod
    def from_json(cls, text: str) -> "AnalysisResult":
        return cls.from_dict(json.loads(text))

    @classmethod
    def legacy_report(
        cls,
        analysis_type: str,
        title: str,
        report: str,
        artifact_titles: Iterable[str] = (),
    ) -> "AnalysisResult":
        """Wrap an existing text/bitmap publication during incremental migration."""
        artifacts = [
            AnalysisArtifact(f"plot-{index:02d}", str(name), "plot")
            for index, name in enumerate(artifact_titles, start=1)
        ]
        result = cls(
            analysis_type=analysis_type,
            title=title,
            status=AnalysisStatus.NO_DATA,
            summary={"legacy_report": True},
            artifacts=artifacts,
            provenance=[AnalysisEvidence(
                "LEGACY_HISTORY",
                "Text report and plot references were loaded from a legacy result index.",
                reference="result history version 1",
            )],
            limitations=[
                "Legacy result: detailed findings, metrics, and evidence were not structured."
            ],
        )
        return result.finish()
