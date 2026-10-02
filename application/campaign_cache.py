"""Skip re-running a domain whose inputs have not changed.

A campaign re-run after fixing one net should not re-solve the thermal model.
The cache is keyed on ``(board_fingerprint, analysis_id,
configuration_digest)``: if all three match a stored result, that result is
reused and the domain is reported as ``from_cache=True``.

What the digest reads
---------------------
A wrong reuse is worse than a slow run: a report would describe a board
that is no longer there. So the digest reads every field of the request,
private ones included -- the AC request carries its board in ``_build`` --
and the content of what they hold: arrays by their bytes, geometries by
their WKB, files and folders named in a field by their size and time.
A value it cannot read through (a live KiCad object, a callable, anything
without a data layout) makes the request uncomparable, and the domain runs
again; nothing is digested by its ``repr``, which for a NumPy array of
more than 1,000 values prints "..." and made two different arrays equal.

Durability
----------
Cache **lookups are process-lifetime only**.  ``get()`` consults an in-memory
map and never reads from disk, so a fresh Ki-PIDA session always recomputes.
Only the latest entry of each analysis is kept: each holds the domain's raw
outcome, a thermal mesh or a DC solution, for as long as the dialog lives.
Passing a :class:`~result_history.ProjectResultsHistory` makes ``put()``
additionally archive each result into the project history -- that is durable
record-keeping, not a durable cache.  Nothing here reloads those archives as
cache entries, because the manifest does not carry the configuration digest
that would be needed to prove the stored result still matches the request.
"""

import copy
import hashlib
import os
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import PurePath
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

from analysis_contract import AnalysisResult

# A folder named in a request (a SPICE library) is read this far.
_FOLDER_ENTRIES = 10000
_MAX_DEPTH = 200


class UncomparableRequest(ValueError):
    """A request holds a value the digest cannot read through."""


def _file_state(text: str) -> Optional[str]:
    """Size and time of the file or folder *text* names, or None."""
    if len(text) > 1024 or not ("/" in text or os.sep in text):
        return None
    try:
        if os.path.isfile(text):
            stat = os.stat(text)
            return f"file:{stat.st_size}:{stat.st_mtime_ns}"
        if os.path.isdir(text):
            entries = []
            with os.scandir(text) as listing:
                for entry in listing:
                    if entry.is_file():
                        stat = entry.stat()
                        entries.append(f"{entry.name}:{stat.st_size}:{stat.st_mtime_ns}")
                    if len(entries) >= _FOLDER_ENTRIES:
                        break
            return "dir:" + "|".join(sorted(entries))
    except OSError:
        return "unreadable"
    return None


def _feed(digest, value: Any, depth: int = 0, active=None) -> None:
    """Hash *value* by content into *digest*, or raise UncomparableRequest."""
    if depth > _MAX_DEPTH:
        raise UncomparableRequest("nesting too deep")
    active = active if active is not None else set()

    def tag(text: str) -> None:
        digest.update(text.encode("utf-8", "surrogatepass") + b"\x00")

    if isinstance(value, Enum):
        value = value.value
    if value is None or isinstance(value, (bool, int, float, complex)):
        tag(f"{type(value).__name__}:{value!r}")
        return
    if isinstance(value, (str, PurePath)):
        text = str(value)
        tag(f"str:{text}")
        state = _file_state(text)
        if state is not None:
            tag(state)
        return
    if isinstance(value, (bytes, bytearray)):
        tag(f"bytes:{len(value)}")
        digest.update(value)
        return
    if type(value).__module__ in ("_thread", "threading"):
        # A lock serialises plotting; it is not an input.
        tag("sync")
        return
    if hasattr(value, "dtype") and hasattr(value, "tobytes") and hasattr(value, "shape"):
        if getattr(value.dtype, "kind", "") == "O":
            _feed(digest, value.tolist(), depth + 1, active)
            return
        tag(f"array:{value.dtype}:{tuple(value.shape)}")
        digest.update(value.tobytes())
        return
    if hasattr(value, "tocsr") and hasattr(value, "nnz"):
        matrix = value.tocsr()
        tag(f"sparse:{tuple(matrix.shape)}")
        for part in (matrix.data, matrix.indices, matrix.indptr):
            _feed(digest, part, depth + 1, active)
        return
    if hasattr(value, "wkb") and hasattr(value, "geom_type"):
        tag(f"geometry:{value.geom_type}")
        digest.update(value.wkb)
        return

    marker = id(value)
    if marker in active:
        raise UncomparableRequest(f"{type(value).__name__} refers to itself")
    active.add(marker)
    try:
        if isinstance(value, dict):
            tag(f"dict:{len(value)}")
            for key in sorted(value, key=repr):
                _feed(digest, key, depth + 1, active)
                _feed(digest, value[key], depth + 1, active)
        elif isinstance(value, (list, tuple)):
            tag(f"seq:{len(value)}")
            for item in value:
                _feed(digest, item, depth + 1, active)
        elif isinstance(value, (set, frozenset)):
            tag(f"set:{len(value)}")
            for item in sorted(value, key=repr):
                _feed(digest, item, depth + 1, active)
        elif is_dataclass(value) and not isinstance(value, type):
            tag(f"dataclass:{type(value).__qualname__}")
            for spec in fields(value):
                # A field marked digest=False carries work to reuse (the
                # thermal session's meshes), not an input.
                if not spec.metadata.get("digest", True):
                    continue
                tag(spec.name)
                _feed(digest, getattr(value, spec.name, None), depth + 1, active)
        elif isinstance(value, SimpleNamespace):
            tag("namespace")
            _feed(digest, vars(value), depth + 1, active)
        else:
            raise UncomparableRequest(f"holds a {type(value).__name__}")
    finally:
        active.discard(marker)


def configuration_digest(request: Any, context: Any = ()) -> str:
    """SHA-256 of a domain request's content, with *context* (the tool's
    build, the runs the domain's inputs came from) folded in.

    Dict ordering does not change it; any field, private or not, does.
    Raises UncomparableRequest when the request holds something it cannot
    read through: such a domain is solved again rather than reused.
    """
    digest = hashlib.sha256()
    _feed(digest, context)
    _feed(digest, request)
    return digest.hexdigest()


class CampaignCache:
    """Memoise per-domain AnalysisResults across campaign runs.

    See the module docstring for what is and is not durable.
    """

    def __init__(self, history=None):
        self._entries: Dict[Tuple[str, str, str], AnalysisResult] = {}
        # The domain engine's own outcome (a raw dict for DC, a frozen
        # *RunOutcome for the rest), kept beside the adapted result so a
        # cache hit can still feed CampaignEngine._handover -- the DC ->
        # thermal/CFD result sharing and the EMC input coupling both read
        # this, not the AnalysisResult, and neither can be reconstructed
        # from it after the fact.
        self._raw: Dict[Tuple[str, str, str], Any] = {}
        self._history = history

    @staticmethod
    def _key(board_fingerprint: str, analysis_id: str, config_digest: str):
        return (str(board_fingerprint or ""), str(analysis_id or ""), str(config_digest or ""))

    def get(
        self, board_fingerprint: str, analysis_id: str, config_digest: str,
    ) -> Optional[AnalysisResult]:
        """Return the memoised result for these exact inputs, or None."""
        return self._entries.get(self._key(board_fingerprint, analysis_id, config_digest))

    def get_domain_result(
        self, board_fingerprint: str, analysis_id: str, config_digest: str,
    ) -> Any:
        """Return the memoised raw domain outcome for these inputs, or None."""
        return self._raw.get(self._key(board_fingerprint, analysis_id, config_digest))

    def put(
        self, board_fingerprint: str, analysis_id: str, config_digest: str,
        result: AnalysisResult, domain_result: Any = None,
    ) -> None:
        """Memoise *result* (and its raw *domain_result*), and archive it if a history was supplied."""
        key = self._key(board_fingerprint, analysis_id, config_digest)
        # One entry per analysis: an older one holds a raw outcome nobody
        # will ask for again once the inputs moved on.
        for stale in [item for item in self._entries if item[1] == key[1] and item != key]:
            del self._entries[stale]
            self._raw.pop(stale, None)
        self._entries[key] = result
        if domain_result is not None:
            self._raw[key] = domain_result
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
            self._raw.pop(key, None)
        return len(doomed)

    def __len__(self) -> int:
        return len(self._entries)
