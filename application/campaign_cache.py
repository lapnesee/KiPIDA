"""Skip re-running a domain whose inputs have not changed.

A campaign re-run after fixing one net should not re-solve the thermal model.
The cache is keyed on ``(board_fingerprint, analysis_id,
configuration_digest)``: if all three match a stored result, that result is
reused and the domain is reported as ``from_cache=True``.

Durability
----------
Cache **lookups are process-lifetime only**.  ``get()`` consults an in-memory
map and never reads from disk, so a fresh Ki-PIDA session always recomputes.
Passing a :class:`~result_history.ProjectResultsHistory` makes ``put()``
additionally archive each result into the project history -- that is durable
record-keeping, not a durable cache.  Nothing here reloads those archives as
cache entries, because the manifest does not carry the configuration digest
that would be needed to prove the stored result still matches the request.
"""

import copy
import hashlib
import json
from dataclasses import fields, is_dataclass
from typing import Any, Dict, Optional, Tuple

from analysis_contract import AnalysisResult, _json_safe


def configuration_digest(request: Any) -> str:
    """Stable SHA-256 over a domain request's public dataclass fields.

    Values are normalised through the same JSON-safe path the result contract
    uses, then serialised with sorted keys, so dict ordering and NumPy scalars
    digest reproducibly across runs.  Fields whose name starts with ``_`` are
    private and excluded.

    A non-dataclass request falls back to ``repr()``.  That is weaker -- it is
    only as stable as the object's ``__repr__`` -- so a caller relying on the
    cache should hand the engine a dataclass request.
    """
    if is_dataclass(request) and not isinstance(request, type):
        payload: Dict[str, Any] = {}
        for spec in fields(request):
            if spec.name.startswith("_"):
                continue
            payload[spec.name] = _json_safe(getattr(request, spec.name, None))
        material = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    else:
        material = repr(request)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class CampaignCache:
    """Memoise per-domain AnalysisResults across campaign runs.

    See the module docstring for what is and is not durable.
    """

    def __init__(self, history=None):
        self._entries: Dict[Tuple[str, str, str], AnalysisResult] = {}
        self._history = history

    @staticmethod
    def _key(board_fingerprint: str, analysis_id: str, config_digest: str):
        return (str(board_fingerprint or ""), str(analysis_id or ""), str(config_digest or ""))

    def get(
        self, board_fingerprint: str, analysis_id: str, config_digest: str,
    ) -> Optional[AnalysisResult]:
        """Return the memoised result for these exact inputs, or None."""
        return self._entries.get(self._key(board_fingerprint, analysis_id, config_digest))

    def put(
        self, board_fingerprint: str, analysis_id: str, config_digest: str,
        result: AnalysisResult,
    ) -> None:
        """Memoise *result*, and archive it if a history was supplied."""
        self._entries[self._key(board_fingerprint, analysis_id, config_digest)] = result
        if self._history is None:
            return
        try:
            # ProjectResultsHistory.save() reassigns result.artifacts, so it is
            # handed a copy: archiving a result must not mutate the one the
            # running campaign is holding.
            archived = copy.deepcopy(result)
            self._history.save(
                analysis_id, archived.title, archived.to_json(), plots=[], result=archived,
            )
        except Exception:
            # Archiving is a side benefit; losing it must not fail the run.
            pass

    def invalidate(self, board_fingerprint: str = "", analysis_id: str = "") -> int:
        """Drop matching entries; an empty string means 'any'.

        Returns the number of entries dropped.
        """
        doomed = [
            key for key in self._entries
            if (not board_fingerprint or key[0] == board_fingerprint)
            and (not analysis_id or key[1] == analysis_id)
        ]
        for key in doomed:
            del self._entries[key]
        return len(doomed)

    def __len__(self) -> int:
        return len(self._entries)
