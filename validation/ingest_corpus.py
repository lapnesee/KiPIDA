"""Measure the ingestion invariants over any set of KiCad projects.

Correctness fixes proposed from the reference board alone did not survive
other projects: a symbol UUID looked like a sufficient key until multi-instance
sheets made it ambiguous, and the project name in a symbol's instance block
looked reliable until renamed projects appeared. docs/plan-universalite.md
therefore accepts each phase on invariants that must hold for *every* project,
and this harness measures them.

    python validation/ingest_corpus.py <project.kicad_pro | directory> [...]
                                       [--max-pcb-mb N] [--json OUT]

A directory is searched recursively for .kicad_pro files, skipping hidden
directories. The KiCad demos installed with KiCad or Zeo are a ready corpus:
``<install>/share/kicad/demos``. To include a real project without sweeping its
backups or variants, pass its .kicad_pro files one by one.

Nothing is written next to the projects; ``--json`` writes only where it is
told to.

The invariants, and what each one exposed when this harness was written:

I1  every footprint that carries a schematic path resolves, by that path, to
    exactly one symbol instance, through ``SymbolInstance.footprint_path``.
    The baseline was 0 % everywhere: the reader walked files rather than sheet
    instances and offered only the sheet's path. A footprint can still miss
    when the board holds a symbol the schematic has since deleted.
I2  SCH-005 never reports a footprint that has no schematic path: such a
    footprint was placed on the PCB directly and never had a symbol.
I3  no net is a power rail on open-collector/open-emitter pins alone.
I4  every C-prefixed component whose value is written as a capacitance is
    recognised as a capacitor by the rules.
I5  SCH-002 never reports a pin the designer marked no-connect.
I6  every net carried by a power-flag symbol is classified a rail.
    I5 and I6 need data the schematic reader does not collect yet (no-connect
    markers, the power flag); phase 1.3 collects it and adds their measurement
    here. Until then they print n/a rather than a guess.
I7  resolving a directory that holds more than one project fails loudly
    instead of picking one.
"""

import argparse
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest.board_reader import read_board  # noqa: E402
from ingest.netlist_builder import build_netlist  # noqa: E402
from ingest.project_resolver import resolve_project  # noqa: E402
from ingest.schematic_reader import read_schematic  # noqa: E402
from rules import schematic_rules  # noqa: E402
from rules.registry import DEFAULT_REGISTRY  # noqa: E402

_VERSION_RE = re.compile(rb"\(version\s+(\d+)\)")
_CAPACITOR_REF_RE = re.compile(r"^C\d")
# A capacitance written anywhere in the value: digits, a p/n/u/µ multiplier,
# then an F, a digit or a non-letter ("100n", "4u7", "10uF", and part-number
# values such as "CC1206_100NF_50V_10%_X7R"). "0", "DNP" or "10uH" is not a
# capacitance the rules could have recognised.
_CAPACITANCE_LIKE_RE = re.compile(r"(?<![\d.,])\d+(?:[.,]\d+)?\s*[pnuµ](?:f|\d|(?![a-z]))", re.IGNORECASE)


def file_format_version(path):
    """The ``(version N)`` stamp near the top of a KiCad S-expression file."""
    try:
        with open(path, "rb") as handle:
            match = _VERSION_RE.search(handle.read(512))
        return int(match.group(1)) if match else None
    except OSError:
        return None


def find_projects(arguments):
    projects = []
    for argument in arguments:
        path = Path(argument)
        if path.is_file() and path.suffix == ".kicad_pro":
            projects.append(path)
            continue
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            projects.extend(Path(root) / name for name in sorted(files) if name.endswith(".kicad_pro"))
    return projects


def measure_project(pro, max_pcb_mb):
    row = {"project": str(pro), "status": "", "errors": []}
    sch_path = pro.with_suffix(".kicad_sch")
    pcb_path = pro.with_suffix(".kicad_pcb")
    has_sch, has_pcb = sch_path.is_file(), pcb_path.is_file()
    row["status"] = (
        "FULL" if has_sch and has_pcb else
        "SCHEMATIC_ONLY" if has_sch else
        "PCB_ONLY" if has_pcb else "NO_DESIGN_FILES"
    )
    row["sch_version"] = file_format_version(sch_path) if has_sch else None
    row["pcb_version"] = file_format_version(pcb_path) if has_pcb else None
    started = time.perf_counter()

    schematic = None
    if has_sch:
        schematic = read_schematic(sch_path)
        row["symbols"] = len(schematic.instances)
        row["I6_power_flag_nets"] = "n/a (phase 1.3)"
    if not has_pcb:
        row["seconds"] = round(time.perf_counter() - started, 2)
        return row
    if pcb_path.stat().st_size > max_pcb_mb * 1e6:
        row["status"] += f" (PCB over {max_pcb_mb} MB, board invariants skipped)"
        row["seconds"] = round(time.perf_counter() - started, 2)
        return row

    board = read_board(pcb_path)
    row["footprints"] = len(board.footprints)
    netlist = build_netlist(board, schematic, board_uuid=pro.stem)

    with_path = [fp for fp in board.footprints if fp.sch_path]
    row["footprints_without_path"] = len(board.footprints) - len(with_path)
    if schematic is not None:
        keys = {}
        for inst in schematic.instances:
            keys[inst.footprint_path] = keys.get(inst.footprint_path, 0) + 1
        refs = {inst.reference for inst in schematic.instances if inst.reference}
        exact = sum(1 for fp in with_path if keys.get(fp.sch_path) == 1)
        row["I1_matched_by_path"] = f"{exact}/{len(with_path)}"
        row["I1_ok"] = exact == len(with_path)
        row["reference_only_fallback"] = sum(
            1 for fp in with_path if keys.get(fp.sch_path) != 1 and fp.reference in refs
        )

    findings = DEFAULT_REGISTRY.evaluate_domain(
        "SCHEMATIC", schematic_rules.SchematicRuleContext(netlist=netlist, schematic=schematic),
    )
    by_rule = {}
    for finding in findings:
        by_rule[finding.rule_id] = by_rule.get(finding.rule_id, 0) + 1
    row["rules"] = by_rule

    pathless = {fp.reference for fp in board.footprints if not fp.sch_path}
    sch005_pathless = sum(
        1 for f in findings if f.rule_id == "SCH-005" and set(f.components) & pathless
    )
    row["I2_sch005_on_pathless"] = sch005_pathless
    row["I2_ok"] = sch005_pathless == 0

    power_pin_types = {"power_in", "power_out"}
    oc_only = [
        net.name for net in netlist.nets
        if net.is_power_rail and not any(pin.electrical_type in power_pin_types for pin in net.pins)
    ]
    row["I3_open_collector_only_rails"] = len(oc_only)
    row["I3_ok"] = not oc_only

    classifier = getattr(schematic_rules, "_is_capacitor_value", None)
    caps = [
        c for c in netlist.components
        if _CAPACITOR_REF_RE.match(c.reference or "") and _CAPACITANCE_LIKE_RE.search(c.value or "")
    ]
    if classifier is None:
        row["I4_capacitors_unrecognised"] = "n/a"
    else:
        missed = sorted({c.value for c in caps if not classifier(c.value)})
        row["I4_capacitors_unrecognised"] = f"{sum(1 for c in caps if not classifier(c.value))}/{len(caps)}"
        row["I4_examples"] = missed[:5]
        row["I4_ok"] = not missed

    row["I5_sch002_on_no_connect"] = "n/a (phase 1.3)"

    row["seconds"] = round(time.perf_counter() - started, 2)
    return row


def directory_resolution_invariant(arguments):
    """I7: directories holding several projects must not resolve silently."""
    silent = []
    for argument in arguments:
        path = Path(argument)
        if not path.is_dir():
            continue
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            if sum(1 for name in files if name.endswith(".kicad_pro")) < 2:
                continue
            try:
                resolved = resolve_project(Path(root))
            except (FileNotFoundError, ValueError):
                continue
            silent.append(f"{root} -> {getattr(resolved, 'pro_path', resolved)}")
    return silent


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--max-pcb-mb", type=float, default=25.0)
    parser.add_argument("--json", dest="json_out")
    args = parser.parse_args(argv[1:])

    rows = []
    for pro in find_projects(args.paths):
        try:
            row = measure_project(pro, args.max_pcb_mb)
        except Exception as exc:  # a project that breaks ingestion is a result, not a crash
            row = {"project": str(pro), "status": "ERROR", "errors": [f"{type(exc).__name__}: {exc}"],
                   "traceback": traceback.format_exc().splitlines()[-4:]}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    silent = directory_resolution_invariant(args.paths)
    summary = {
        "projects": len(rows),
        "errors": sum(1 for r in rows if r["status"] == "ERROR"),
        "I1_boards_failing": sum(1 for r in rows if r.get("I1_ok") is False),
        "I2_boards_failing": sum(1 for r in rows if r.get("I2_ok") is False),
        "I3_boards_failing": sum(1 for r in rows if r.get("I3_ok") is False),
        "I4_boards_failing": sum(1 for r in rows if r.get("I4_ok") is False),
        "I7_silent_directory_picks": silent,
    }
    print(json.dumps({"summary": summary}, ensure_ascii=False, indent=1))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps({"rows": rows, "summary": summary}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv)
