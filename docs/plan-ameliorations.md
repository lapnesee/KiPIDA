# Next improvements

## Resuming from a cold start

Everything needed to continue is in the repository; nothing depends on a
conversation being remembered.

* This file is the ordered backlog. Items are ranked by what makes the tool
  report something untrue, and each names the measurement it rests on. Items
  that are done say so in place, with what closing them exposed, because the
  exposure is usually worth more than the item.
* `docs/validation-cfd.md` holds the CFD's measured behaviour, including the
  three wrong explanations that preceded the right one. Read it before touching
  the enclosure solver, or the same guesses will be made again.
* `docs/audit-cablage.md` is why "wired to nothing" keeps appearing here.
* Commit messages carry the reasoning, not just the change. `git log` on this
  branch is the narrative: what was measured, what was assumed and disproved,
  and what remains open.

Working constraints for this project:

* Branch `claude/pcb-analysis-tool-40t1dr`, commit messages in English.
* `python -m unittest discover -s tests` must stay green. It is currently 817
  tests. How many skip depends on what is installed: on a machine with
  wxPython, CuPy, openEMS and ngspice nothing should skip but the CUDA
  branches. Without wxPython -- the usual case on a Linux runner, where it has
  no wheel -- 25 more skip and `test_plotter`/`test_i18n` fail outright on the
  missing module. Read a skip count against the environment, not against a
  number written down here.
* The reference board at `DAW CONTROLEUR/schema/DAW-Controlleur` is a real
  read-only project. Never modify, move or copy it into the repository.
* Tests stay lean: cover functions that are actually exercised, plus regression
  tests with a real link to a defect. No exhaustive edge-case matrices.
* `validation/` holds re-runnable harnesses. They split in two, and the
  difference decides where a given item can be worked at all:
  * `cfd_benchmarks.py` needs **no** board. It builds its own ducts and
    enclosures from `CFDMesh` directly, so every CFD item -- A1 and A2 -- can
    be worked anywhere Python and numpy run.
  * `advisor_on_board.py` and `mesh_connectivity.py` need the real board and
    take its path as an argument. They open it read-only and write nothing.
    A3 and A4 cannot be measured without them.
* The reference board lives on the author's Windows machine, at
  `C:\Users\jbc66\Documents\DAW CONTROLEUR\schema\DAW-Controlleur\boards\p02_alimentation`.
  Nothing in that directory may be modified, created or deleted, and it is not
  reachable from a cloud session -- so A3 and A4 are local-session work, and
  everything else is not.

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

*Step one is not either candidate.* Those three temperatures are recorded in
this file and nowhere else: no case in `validation/cfd_benchmarks.py` produces
them, `docs/validation-cfd.md` does not mention them, and nothing in the
repository reproduces them. The reproduction case was never committed. So
neither candidate can currently be shown to have changed anything, which is the
condition under which the last three iteration-count changes were each
invisible for a different reason. Commit the case as a benchmark first --
`cfd_benchmarks.py` builds its own enclosures and needs no board, so this is
ordinary work, not a measurement campaign -- then compare the two candidates
against it.

**A2. `converged` is unreachable on a sealed enclosure.**
Continuity now sits at machine zero, but the energy residual is still falling
when the iteration cap arrives (5.6e-4 against a 1e-4 tolerance at 250). Either
the cap is too low for buoyant cases or the energy residual needs its own
normalisation. Measure which before changing either -- the last three
iteration-count changes were all invisible for a different reason each time.

**A3. Advisor actions are unverified except for track width. Done -- both now
re-simulate, and the check disagreed with the estimate.**
Each action had an objection recorded against it, and each objection had an
answer.

* *"There is no equivalent for 'add a via near this one' that does not require
  inventing a position."* The position that invents nothing is the one the via
  already occupies. A duplicate there is joined to the same pair of nodes, and
  the solver sums the parallel conductances -- which is the R/N the sizing
  claims. It is also the conservative reading: real stitching vias are spread
  over the pour and give the current several entry points into it, while a
  stacked duplicate relieves the barrel and leaves the pour's spreading
  resistance untouched. The re-simulated gain is a lower bound.
* *"There is no equivalent for 'the same pour, thicker'."* There is: a stackup
  with one layer re-thicknessed, which the mesher already reads for pours and
  for the tracks sharing the layer. `total_thickness_mm` is deliberately left
  alone, because a fabricator absorbs copper weight in the dielectric and that
  height is what the via barrel model uses -- moving it would re-price every
  via as a side effect of a question about a pour.
* The *second* half of the copper-weight objection survives and is not
  dissolved by re-simulating: heavier copper etches with more undercut, so a
  track drawn at a width finishes narrower, and the mesher does not model it.
  The finding still says so. Re-simulation made the number checkable, not
  complete.

What the check found, which is why this was worth more than a label. Both
sizings attribute the drop in proportion to dissipated power and scale that
share down, which assumes the rest of the network holds still. It does not.
Measured on a two-pour board, source and load on opposite corners:

| ask | first-order promise | re-simulated | error |
| --- | --- | --- | --- |
| 2 -> 4 vias | 4.00 mV | 4.06 mV | 1.4 % |
| 2 -> 6 vias | 3.00 mV | 3.36 mV | 10.6 % |
| 2 -> 10 vias | 2.00 mV | 2.80 mV | 28.4 % |
| 2 -> 18 vias | 1.50 mV | 2.42 mV | 38.0 % |
| F.Cu 1 -> 2 oz | 6.00 mV | 5.63 mV | 6.7 % |
| F.Cu 1 -> 3 oz | 5.00 mV | 4.97 mV | 0.6 % |
| F.Cu 1 -> 4 oz | 4.00 mV | 4.64 mV | 13.8 % |

The estimate is good for a modest ask and over-promises for an aggressive one,
by up to a third, because the drop it cannot reach -- the pour's spreading
resistance for vias, the barrels for copper weight -- is exactly the part it
assumed away. Both actions still help; they just do not arrive where they said.
`verify=False` keeps the old wording and `verified=False`, so the fast path
never presents an estimate as a measurement.

Not yet run on the reference board: `validation/advisor_on_board.py` now
defaults to `verify=True` and takes `--fast` for the old path, so the two can
be compared on real copper by running it twice. The number to look for is the
same one the table shows: how far the re-simulated drop lands above the
promise on a 108,000-node plane, where the spreading resistance the estimate
ignores is far larger than on any synthetic board.

**A4. 900 single-node components in the advisor's mesh. Done -- the lattice
now follows the copper.**
`+3V3_MAIN` meshed to 902 connected components, 900 of them isolated single
nodes. Root cause: `_mesh_zone_cutcell` grids the pour's *bounding box* and
minted a node at every lattice point of it, before knowing whether any copper
was there. A branch is only added where a cut-cell overlaps the pour, so a
lattice point with no copper on any of its four incident cells could never gain
one, and became a component of one node. Nodes are now minted lazily, by the
edge that needs them.

It was constated rather than assumed, on two shapes that isolate the two ways
a pour fails to fill its own bounding box. At a 0.1 mm step, on a 5 mm box:

| geometry | isolated nodes before | after | branches |
| --- | --- | --- | --- |
| square pour filling its bounding box | 0 | 0 | 5,100 |
| diamond pour (empty corners) | 1,200 | 0 | 2,700 |
| square pour, one 0.6 mm antipad hole | 25 | 0 | 5,040 |
| square pour, one 0.4 mm antipad hole | 9 | 0 | 5,076 |

The branch count is identical on both sides of the fix in every case, which is
the claim that matters: nodes were deleted and no conduction path was. Rail
resistances, and every number derived from them, are unchanged. So the two
mechanisms are the empty corners of a non-rectangular pour and the antipad
holes inside it, and 900 is the sum of both -- on the order of thirty 0.6 mm
holes' worth, or a pour whose outline leaves that much of its box empty.

What it exposed, and the reason this outranked "they only inflate the mesh":
the phantom nodes were *indexed as plane nodes*. `probe` reads that index to
decide whether a pad meets copper at all, and its whole purpose is to skip a
pad that touches nothing -- because a private node sitting on the pad wins the
nearest-node lookup against the real network node a fraction of a millimetre
away, which is how a source lands on an island. A phantom node inside a
clearance hole defeated exactly that guard: a pad whose zone connection is
"none" looked connected to the pour and minted a barrel down to copper no
current can reach. `tests/test_mesh_hybrid_zone_nodes.py` covers it, and all
four of its cases fail on the old mesher.

Not yet confirmed on the reference board, because that needs a session on the
machine that has it. The prediction to check is exact and falsifiable:
`validation/mesh_connectivity.py <board> +3V3_MAIN` should now report 2
connected components instead of 902, 0 isolated single nodes instead of 900,
and the **same branch count** as before. A changed branch count would mean the
fix removed copper, not phantoms.

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

**B2. `CampaignEngine` was unreachable. Done -- wired, not deleted.**
`docs/audit-cablage.md` ranked it "effort faible", and it was not: the engine
had never met a real engine, so three of its six adapters were written against
a shape no engine returns. That is the finding, and it is the argument for
wiring rather than deleting -- an orchestrator nobody calls decays silently,
and its tests stay green while it does.

What the wiring exposed, each a call that would have raised on first use:

* `_adapt_differential` read `stackup` and `tolerance_pct` off the *request*.
  `DifferentialRunRequest` has a stackup but no tolerance; the resolved
  tolerance only exists on the outcome, because it is resolved during the
  solve. The request's value would have been the target asked for, not the one
  the results were graded against.
* `_adapt_cfd` demanded `mesh` from the request. The mesh does not exist until
  the solve has built it; it is on `CFDRunOutcome`.
* `_adapt_ac` passed the engine's `(sweep, optimization)` pair where
  `adapt_ac_result` expects the sweep.
* `_adapt_thermal` and `_adapt_emc` were handed the outcome dataclass where
  the underlying adapter expects its `.result`.
* `maximum_drop_pct` had no home at all: nothing on `DCRunRequest` carried the
  voltage-drop budget, so the DC domain could only ever fail its `_require`
  check. It is a field on the request now, still optional and still demanded
  rather than defaulted.

Two orderings had to become explicit, because the registry's order is a
catalogue order and the batch had been relying on a hand-written one:

* EMC sits at 40 and thermal at 50 in the registry, but EMC *reads* the
  thermal field, the AC sweep and the differential results rather than
  recomputing them. `_order_for_emc_inputs` moves it after them.
* Those same three inputs used to reach EMC from whatever the session
  happened to hold, which in a batch is the previous run. The engine now folds
  its own outcomes into the pending EMC request, so every domain in a campaign
  grades the same board state.

Also closed on the way past, because a batch now reports from the campaign's
own results rather than from the published tabs: DC was adapted in two places,
and only the dialog's copy attached the advisor's sized fixes. One
`analysis_adapters.adapt_dc_run` is now the single call both make. The same
double-reading of `txt_drop_pct` is gone with it -- exactly the B1 shape.

Still open, and known rather than assumed:

* The advisor runs once per batch in the campaign's DC adapter and again when
  the DC tab publishes. Correct but wasteful; it is not measured, because
  measuring it needs the reference board.
* `application/schematic_controller.py` -- and through it the whole `rules/`
  package -- is now the only shipped code nothing imports.
  `tests/test_campaign_wiring.py` has the reachability guard
  `docs/audit-cablage.md` asked for, but on `application.campaign_controller`
  alone; widening it to every package is the same one-line test and would fail
  today on `rules`, which is the point of writing it down here rather than
  hiding it behind a pass list.

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

**C2. The batch cannot be cancelled and produces no summary. Closed by B2.**
Running through `CampaignController` gave it a cancel that keeps the domains
that already finished -- the base controller used to raise on cancellation and
throw the partial campaign away, which for a campaign is the opposite of what
it is for -- and the run now ends with the verdict, the domains that produced,
the ones that failed and why, and the consolidated report built without a
second button press. What remains of this item is only a matter of taste: the
cancel is the batch button relabelling itself, as the AC and CFD buttons do,
rather than a separate control.

**C3. "Iterations" now means a cap, not a target.**
Since the loop exits on convergence, the field's meaning changed. The label
should say so, otherwise raising it looks like asking for a longer run.

## What is deliberately not on this list

Turbulence, transients, fan curves and radiation in the enclosure CFD. They are
absent from the model and stated as absent in the result's limitations. Adding
any of them before A1 would be building on a solver that cannot yet move heat
from air to wall.
