# Next improvements

## Resuming from a cold start

Everything needed to continue is in the repository; nothing depends on a
conversation being remembered.

* This file is the ordered backlog. Items are ranked by what makes the tool
  report something untrue, and each names the measurement it rests on.
* `docs/validation-cfd.md` holds the CFD's measured behaviour, including the
  three wrong explanations that preceded the right one. Read it before touching
  the enclosure solver, or the same guesses will be made again.
* `docs/audit-cablage.md` is why "wired to nothing" keeps appearing here.
* Commit messages carry the reasoning, not just the change. `git log` on this
  branch is the narrative: what was measured, what was assumed and disproved,
  and what remains open.

Working constraints for this project:

* Branch `claude/pcb-analysis-tool-40t1dr`, commit messages in English.
* `python -m unittest discover -s tests` must stay green. It is currently 808
  tests, 3 skips, and the three are CuPy-absent CUDA branches.
* The reference board at `DAW CONTROLEUR/schema/DAW-Controlleur` is a real
  read-only project. Never modify, move or copy it into the repository.
* Tests stay lean: cover functions that are actually exercised, plus regression
  tests with a real link to a defect. No exhaustive edge-case matrices.
* `validation/` holds re-runnable harnesses that need the real board:
  `cfd_benchmarks.py`, `advisor_on_board.py`, `mesh_connectivity.py`. They open
  the board read-only.

The reflex this session most needed, learned the hard way: when a fix produces
no visible change, look for a second copy of the value before doubting the
deployment. Three correct fixes were invisible because the default existed in
three places, and seven exchanges were spent blaming a deployment that was fine.



Ordered by what turns a wrong answer into a right one, not by effort. Every
item names the evidence it rests on, so a reader can check the premise rather
than trust the ranking. Items with no measurement behind them say so.

## A. Correctness — an analysis currently reports something untrue

**A1. Enclosure air cannot reach the walls.**
The surface film closed part of the gap (162.7 C to 128.5 C on the
reproduction case) but the same result shows the air itself at 125.6 C, so what
remains is air-to-wall transport, not the solid interface. A 5 mm mesh cannot
resolve a buoyant plume at 0.076 m/s. Two candidate fixes, and they should be
compared rather than chosen by taste: resolve the plume (expensive, and the
node budget is already the binding constraint), or add a bulk-air-to-wall
exchange term calibrated against the 3D thermal solver's answer for the same
board. The second is a model, not a resolution, and must be labelled as one.
Until this closes, CFD-004 stands and component temperatures come from the
thermal analysis.

**A2. `converged` is unreachable on a sealed enclosure.**
Continuity now sits at machine zero, but the energy residual is still falling
when the iteration cap arrives (5.6e-4 against a 1e-4 tolerance at 250). Either
the cap is too low for buoyant cases or the energy residual needs its own
normalisation. Measure which before changing either -- the last three
iteration-count changes were all invisible for a different reason each time.

**A3. Advisor actions are unverified except for track width.**
`simulate_width_change` re-meshes and re-solves. `ADD_STITCHING_VIAS` and
`INCREASE_COPPER_WEIGHT` are first-order and say so, but "says so" is weaker
than "was checked". A via-count what-if needs a defensible position for the
added vias; a copper-weight what-if needs the stackup change fed back through
the mesher. Both are feasible; neither is trivial.

**A4. 900 single-node components in the advisor's mesh.**
`+3V3_MAIN` meshes to 902 connected components, 900 of them isolated single
nodes. They no longer strand loads, but they are cut-cell grid points with no
connected neighbour and they inflate every mesh. Root cause not investigated.

**A5. Palace goes silent after the upload.**
The log ends at "Uploading the explicit Palace project directory" with no
completion, failure or timeout line, on every run. Whether the remote solve
succeeds is currently unknowable from the log. Also `probe()` reports the
usage text as a version string, which is cosmetic but reads as a malfunction.

## B. Structural — the same defect keeps recurring

**B1. Sweep for duplicated defaults.**
The CFD solver's defaults existed in three places: `models.py`,
`config_manager.py` and `ui/cfd_analysis_panel.py`. Two were fixed in
succession, each believed to be the last, and the third kept the observable
behaviour unchanged through both. The pattern is near-certain to repeat in the
thermal, AC and EMC panels, which are written the same way. One sweep, with a
test per domain asserting `_dict_to_X({}) == X()`, closes a whole class.

This is the highest-value item in the document. It is not a feature; it is the
reason three correct fixes produced no visible change and a deployment was
blamed for seven exchanges.

**B2. `CampaignEngine` is still unreachable.**
The batch button chains the per-domain handlers, which was the smaller change
and works. `CampaignEngine` -- with its failure isolation, caching and
per-domain adapters -- is still called only by tests. Either wire the batch to
it or delete it; a third state where it exists and is maintained but never runs
is what `docs/audit-cablage.md` was written about.

**B3. Sixteen tests skip under full discovery.**
The batch and dialog tests run only when invoked directly, because
`test_plotter` installs a `wx` stub during discovery. That is real coverage
that disappears in CI. Fixing the stub is preferable to weakening the tests.

**B4. The build fingerprint cannot see stale bytecode.**
It hashes sources on disk. Module provenance was added for the shadowing case;
bytecode remains a blind spot. Low priority now that provenance is reported,
but it is a known hole rather than a solved problem.

## C. Interface

**C1. `pressure_iterations` is a dead control that looks live.**
The sparse Poisson solve ignores it. It is documented as inert in the
dataclass, but the CFD panel still shows an editable box. Disable it with a
tooltip, or remove it and migrate saved projects.

**C2. The batch cannot be cancelled and produces no summary.**
It runs to completion or until the dialog closes. It should offer a cancel, and
end by reporting which analyses ran, which failed and why -- and optionally
build the consolidated report, which is the reason to run a batch at all.

**C3. "Iterations" now means a cap, not a target.**
Since the loop exits on convergence, the field's meaning changed. The label
should say so, otherwise raising it looks like asking for a longer run.

## What is deliberately not on this list

Turbulence, transients, fan curves and radiation in the enclosure CFD. They are
absent from the model and stated as absent in the result's limitations. Adding
any of them before A1 would be building on a solver that cannot yet move heat
from air to wall.
