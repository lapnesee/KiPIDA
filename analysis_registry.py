"""Declarative catalogue used to organize Ki-PIDA analysis tools."""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple


@dataclass(frozen=True)
class AnalysisDescriptor:
    analysis_id: str
    title: str
    group: str
    order: int
    prerequisites: Tuple[str, ...] = ()
    capabilities: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.analysis_id or self.analysis_id != self.analysis_id.upper():
            raise ValueError("analysis_id must be a non-empty uppercase identifier")
        if not self.title.strip() or not self.group.strip():
            raise ValueError("analysis title and group must not be empty")


class AnalysisRegistry:
    def __init__(self, descriptors: Iterable[AnalysisDescriptor] = ()):
        self._descriptors: Dict[str, AnalysisDescriptor] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: AnalysisDescriptor) -> None:
        if descriptor.analysis_id in self._descriptors:
            raise ValueError(f"Analysis already registered: {descriptor.analysis_id}")
        self._descriptors[descriptor.analysis_id] = descriptor

    def get(self, analysis_id: str) -> AnalysisDescriptor:
        try:
            return self._descriptors[str(analysis_id).upper()]
        except KeyError as exc:
            raise KeyError(f"Unknown analysis: {analysis_id}") from exc

    def all(self) -> List[AnalysisDescriptor]:
        return sorted(self._descriptors.values(), key=lambda item: (item.order, item.title))

    def grouped(self) -> Dict[str, List[AnalysisDescriptor]]:
        groups: Dict[str, List[AnalysisDescriptor]] = {}
        for descriptor in self.all():
            groups.setdefault(descriptor.group, []).append(descriptor)
        return groups


DEFAULT_ANALYSES = AnalysisRegistry([
    AnalysisDescriptor("DC", "DC Power", "Power Integrity", 10, ("board", "power_tree"), ("run",)),
    AnalysisDescriptor("AC", "AC Impedance", "Power Integrity", 20, ("board", "power_tree"), ("run", "optimize")),
    AnalysisDescriptor("DIFFERENTIAL", "Differential Pairs", "Signal Integrity", 30, ("board", "stackup"), ("run", "apply_rules")),
    AnalysisDescriptor("EMC", "EMI / EMC", "EMI / EMC", 40, ("board",), ("run", "cancel")),
    AnalysisDescriptor("THERMAL", "3D Thermal", "Thermal", 50, ("board",), ("run", "coupled", "overlay")),
    AnalysisDescriptor("CFD", "Enclosure CFD", "Thermal", 60, ("board", "enclosure"), ("run", "cancel")),
    AnalysisDescriptor("DEBUG", "Diagnostics", "Application", 90, ("board",), ("inspect",)),
])
