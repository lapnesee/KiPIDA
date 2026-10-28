"""Declarative rule registry.

Every analysis rule is described once, with its id, domain, default
severity, normative reference, and i18n-ready text, and the callable that
evaluates it against a domain-specific context. This replaces scattered
``if``/``_add()`` calls with a single source of truth that can be listed,
filtered, and unit-tested independently of any specific analysis run.

A rule's ``evaluate`` callable takes one positional argument (a context
object, meaning is domain-specific) and returns a
``list[analysis_contract.AnalysisFinding]``. A rule that raises during
evaluation is logged and skipped by :meth:`RuleRegistry.evaluate_domain` —
one broken rule must not abort the others.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuleDescriptor:
    rule_id: str
    domain: str
    title: str
    description: str
    default_severity: str
    reference: str = ""
    remediation_template: str = ""


@dataclass(frozen=True)
class RuleRegistration:
    descriptor: RuleDescriptor
    evaluate: Callable[[Any], list]


class RuleRegistry:
    """Holds :class:`RuleRegistration` entries, keyed by rule id."""

    def __init__(self) -> None:
        self._by_id: dict[str, RuleRegistration] = {}

    def register(self, registration: RuleRegistration) -> None:
        self._by_id[registration.descriptor.rule_id] = registration

    def get(self, rule_id: str) -> Optional[RuleRegistration]:
        return self._by_id.get(rule_id)

    def by_domain(self, domain: str) -> list[RuleRegistration]:
        return [r for r in self._by_id.values() if r.descriptor.domain == domain]

    def all(self) -> list[RuleRegistration]:
        return list(self._by_id.values())

    def evaluate_domain(self, domain: str, context: Any) -> list:
        """Run every rule registered for *domain* against *context*.

        Findings from every rule are concatenated in registration order. A
        rule that raises is logged and skipped so the rest of the domain
        still evaluates.
        """
        findings: list = []
        for registration in self.by_domain(domain):
            try:
                result = registration.evaluate(context)
            except Exception:
                logger.exception(
                    "Rule %s raised during evaluation; skipped.",
                    registration.descriptor.rule_id,
                )
                continue
            if result:
                findings.extend(result)
        return findings


#: Module-level registry used by the ``@rule`` decorator. Rule modules
#: (e.g. ``rules.schematic_rules``) register into this instance on import.
DEFAULT_REGISTRY = RuleRegistry()


def rule(
    rule_id: str,
    domain: str,
    title: str,
    description: str,
    default_severity: str,
    reference: str = "",
    remediation_template: str = "",
):
    """Decorator: registers ``def f(context) -> list[AnalysisFinding]``
    into :data:`DEFAULT_REGISTRY` under a :class:`RuleDescriptor`."""

    descriptor = RuleDescriptor(
        rule_id=rule_id,
        domain=domain,
        title=title,
        description=description,
        default_severity=default_severity,
        reference=reference,
        remediation_template=remediation_template,
    )

    def decorator(fn: Callable[[Any], list]) -> Callable[[Any], list]:
        DEFAULT_REGISTRY.register(RuleRegistration(descriptor=descriptor, evaluate=fn))
        return fn

    return decorator
