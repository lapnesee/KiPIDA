"""Which KiCad file formats the offline readers accept.

Every KiCad S-expression file carries the date its format last changed:
``(kicad_sch (version 20250114) ...)``. Ki-PIDA does not read projects written
by KiCad 7 or earlier (docs/plan-universalite.md, decision 4), and says so
instead of reading them halfway.

The boundary is KiCad 7.0's final format, not KiCad 8.0's first. KiCad leaves a
sub-sheet nobody edited as it was: the vme-wren demo installed with KiCad 10
still holds a sheet at 20230819, a format of KiCad 8's development cycle, and
KiCad 10 opens it. Refusing at 8.0 would refuse a demo KiCad ships.

Values from the KiCad sources: ``eeschema/sch_file_versions.h`` and
``pcbnew/plugins/kicad/pcb_plugin.h`` at tag 7.0.0 (20230121, 20221018);
``eeschema/sch_file_versions.h`` and
``pcbnew/pcb_io/kicad_sexpr/pcb_io_kicad_sexpr.h`` at tag 8.0.0 (20231120,
20240108).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .sexpr import find

KICAD7_SCHEMATIC_FILE_VERSION = 20230121
KICAD7_BOARD_FILE_VERSION = 20221018

_LAST_REFUSED = {
    "schematic": KICAD7_SCHEMATIC_FILE_VERSION,
    "board": KICAD7_BOARD_FILE_VERSION,
}


class UnsupportedFileFormat(ValueError):
    """A KiCad file written by KiCad 7 or earlier."""

    def __init__(self, path, version: int, kind: str):
        self.path = Path(path)
        self.version = version
        self.kind = kind
        super().__init__(
            f"{self.path.name} is a {kind} file in format {version}, written by KiCad 7 "
            "or earlier. Ki-PIDA reads files from KiCad 8 onwards: open the project in a "
            "current KiCad, save it, and run again."
        )


def file_version(root: list) -> Optional[int]:
    """The ``(version N)`` of a parsed file's root node, or None if it has none."""
    node = find(root, "version")
    if node is None or len(node) < 2:
        return None
    try:
        return int(node[1])
    except (TypeError, ValueError):
        return None


def is_supported(version: Optional[int], kind: str) -> bool:
    """A file without a version stamp was not written by KiCad and is not judged."""
    return version is None or version > _LAST_REFUSED[kind]


def require_supported(root: list, path, kind: str) -> Optional[int]:
    """Raise :class:`UnsupportedFileFormat` for a file KiCad 7 or earlier wrote."""
    version = file_version(root)
    if not is_supported(version, kind):
        raise UnsupportedFileFormat(path, version, kind)
    return version
