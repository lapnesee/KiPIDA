"""Which build of Ki-PIDA is running, and which produced a result.

A result recorded no build of the tool. P02 Rev E's EMC score read 65, 35,
63 and 65 between 2026-09-30 and 2026-10-01 at one board fingerprint: only
the tool had changed, and nothing in the results said so.
"""

import functools
import hashlib
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def plugin_version(plugin_root=None):
    root = Path(plugin_root) if plugin_root else _ROOT
    try:
        return str(json.loads((root / "plugin.json").read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"


def source_fingerprint(plugin_root=None, length=8):
    """Short hash of the plugin's Python sources, or "" if it cannot be read.

    plugin.json carries a release number, which is bumped by hand at release
    time. Every change between two releases therefore reports the same string,
    so the version display cannot answer the question that actually comes up
    when testing: is the copy in the plugin folder the one I just built?

    Working that out from behaviour costs real time -- it took cross-checking a
    solver iteration count, a residual curve and the presence of one log line
    to establish that a deployed copy predated five commits.

    Hashing the sources rather than asking git keeps this working for a folder
    that was copied rather than cloned, and rather than stamping mtimes, which
    a copy rewrites.

    Line endings are normalised before hashing. The first version hashed raw
    bytes, so a CRLF checkout could never match an LF one even when every line
    of code was identical -- and on Windows git converts on checkout by
    default. That made the fingerprint report a difference between two copies
    of the same commit, which is precisely the false alarm it exists to
    prevent, and it did mislead once.

    One blind spot remains, and it is not small: this measures the sources on
    disk, not the module that Python actually imported. With flat top-level
    imports, a second plugin directory earlier on sys.path can supply a module
    while this reports the directory the entry point lives in. See
    ``runtime_environment.imported_module_origins``.
    """
    root = Path(plugin_root) if plugin_root else _ROOT
    skip = {".runtime", ".venv", ".claude", ".git", "__pycache__", "tests", "validation"}
    digest = hashlib.sha256()
    try:
        for path in sorted(root.rglob("*.py")):
            if skip.intersection(path.relative_to(root).parts):
                continue
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    except (OSError, ValueError):
        return ""
    return digest.hexdigest()[:length]


def build_commit(plugin_root=None):
    """The commit a ``git archive`` copy was made from, "abc1234 2026-10-02".

    build_commit.txt is filled in by the archive (export-subst in
    .gitattributes); a working copy still holds the placeholder and reads
    "". It is not a .py file, so the substitution leaves the source
    fingerprint of the copy equal to the repository's.
    """
    root = Path(plugin_root) if plugin_root else _ROOT
    try:
        text = (root / "build_commit.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return "" if "$Format" in text else text


@functools.lru_cache(maxsize=1)
def tool_identity():
    """"0.19.0+2fb7b88c", with " (abc1234 2026-10-02)" in an archived copy."""
    commit = build_commit()
    fingerprint = source_fingerprint() or "unknown"
    return f"{plugin_version()}+{fingerprint}" + (f" ({commit})" if commit else "")
