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
* `python -m unittest discover -s tests` must stay green. It was 987
  tests on 2026-09-13, after phase 4 of the universality plan. How many skip depends on what is installed: on a machine with
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

    "A3 and A4 cannot be measured without them" is how this read, and it was
    too strong -- both were closed without the board. What needed it was the
    *symptom*, and a symptom is not a cause. A4's 900 isolated nodes reproduce
    on a five-millimetre synthetic pour, exactly and in the right proportions,
    because the defect was in the mesher and not in that board. A3 needed no
    board at all: its blocker was two recorded claims that the actions had no
    re-simulable form, and both were wrong.

    Both have since been run on the reference board, and the correction is
    worth more than either item. A4 confirmed cleanly and turned out to have
    been ranked too high -- it changed no number on that board. A3 did not
    confirm at all: it found that neither action it had just verified could
    fire there, and that the third action was actively harmful. The board's
    value was not confirming predictions. It was carrying two properties no
    synthetic fixture had -- layers with a mix of track widths, and a dominant
    path that always contains at least one track branch -- and each of those
    broke a code path that eight hundred green tests agreed was fine. Read a
    "needs the board" note as asking which properties the fixtures do not have.
* The reference board lives on the author's Windows machine, at
  `C:\Users\jbc66\Documents\DAW CONTROLEUR\schema\DAW-Controlleur\boards\p02_alimentation`.
  Nothing in that directory may be modified, created or deleted, and it is not
  reachable from a cloud session.

The reflex this session most needed, learned the hard way: when a fix produces
no visible change, look for a second copy of the value before doubting the
deployment. Three correct fixes were invisible because the default existed in
three places, and seven exchanges were spent blaming a deployment that was fine.

Its companion, from A3: a recorded reason something *cannot* be done is a claim
like any other, and it decays. Both what-ifs carried a careful, well-argued
paragraph explaining why they could not be re-simulated. Both were wrong, and
they had gone unchallenged precisely because they were well argued and written
by someone who had looked. Re-derive the objection before inheriting it.

And a third, from the session that ran the two on the board: a regression test
whose fixture does not reproduce the harm proves nothing, however exactly it
names the defect. The first test written for the narrowing bug passed against
the broken code, because on that fixture narrowing the wide segment happened to
cost less than widening the narrow one gained. The fix looked covered. Run a
new test against the old code before believing it.



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

The reference board now supplies a measurement this item never had, from an
ordinary campaign run rather than an uncommitted reproduction case: solid
225.1 C against air 189.5 C, **35.6 C apart**. That gap is the item stated as a
number. The film is working and has almost nothing left to work against,
because the air it dumps into cannot shed the heat onward. Any candidate fix
should be graded on that difference and not on the solid temperature alone --
a fix that lowers both by the same amount has changed nothing.

CFD-004's own text had gone stale in the meantime, and shipped that way. It
told the reader the solver "carries heat off a solid by cell-to-cell
conduction and advection only, with no surface film coefficient" -- true when
written, false from the commit that added the film, and still being printed
four commits later in a report a user acts on. The same sentence sat a second
time in the result's `limitations`. Both are corrected to name air-to-wall
transport as the limit, which is what this item actually is.

Why it survived: `tests/test_cfd_validation_fixes.py` asserted the finding's
rule id and severity and never read its sentence. A finding is output; text
that makes a physical claim needs asserting on like any other output. There is
now a test that fails if the wording denies the film.

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

**A3. Advisor actions are unverified except for track width. Done -- and the
one exempted by that title was the one reporting something untrue.**
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

Run on the reference board, which changed the item rather than confirming it.
Neither action could be exercised there at all, and the reason was in the
third action.

`build_dc_remediations` reached the via and pour actions only when the dominant
path held *no* track branch. Every rail on p02_alimentation has at least one:
+3V3AO has two carrying 0.0 % of the loss against a pour carrying 100.0 %, and
+5V_RAIL has eleven carrying 3.3 % against a pour carrying 94.5 %. So
`_plane_copper_actions`, which names +5V_RAIL in its own docstring as the case
it was written for, could not fire on +5V_RAIL -- or on anything else.

Two defects in WIDEN_TRACK came out of trying, both first order, both in the
action this item's title called already verified.

* The what-if predicate matched every segment on the layer, and
  `_board_with_widths` sets each match to exactly the proposed width -- so
  segments already wider than the proposal were **narrowed** to it.
  Re-simulating the advice as the code applies it moved +3V3_MAIN from
  1.4972 mV to 2.9083 mV: the advice made the rail twice as bad, and the fast
  path printed the target as the gain. Mixed-width layers are the normal case,
  not the corner: +5V_RAIL/B.Cu runs 0.5 to 1.25 mm and 30 of its 44 segments
  would have been narrowed; VBUS_PD_SW/F.Cu runs 0.25 to 3.0 mm. Every fixture
  in `tests/test_dc_advisor.py` had one width per layer, which is how 838 tests
  stayed green over it.
* It sized against the *whole* rail drop -- `w' = w x (drop / target)` -- which
  assumes every volt lost is lost on those tracks. That is also why the gate
  above had to be all-or-nothing: an action with no notion of how much of the
  drop it can reach has no way to decline, so the only way to let another
  action speak was to suppress this one entirely.

Both fixed. WIDEN_TRACK now attributes by power share and declines when its
share cannot cover the excess, as the other two already did, and the other two
are consulted whenever widening produces nothing. On +5V_RAIL the advisor now
declines the tracks (0.216 mV of 6.558 mV), declines the vias (0.013 mV), and
proposes the In2.Cu pour instead. That is the first time either of these two
actions has run on this board.

The table this item exists for, now on real copper rather than on a synthetic
two-pour board:

The last column is the one the old note asked for -- how far the re-simulated
drop lands from the promise, as `(re-simulated - promise) / promise`. It is
*not* the statistic the synthetic table above reports: that one is the code's
own `prediction_error_pct`, which divides by the re-simulated drop instead. The
two are not comparable digit for digit, only in direction and rough size.

| rail | target | action | promise | re-simulated | vs promise |
| --- | --- | --- | --- | --- | --- |
| +3V3_MAIN | 1.400 mV | widen F.Cu 0.25 -> 0.2997 mm | 1.400 mV | 1.497 mV | +6.9 % |
| +3V3_MAIN | 1.200 mV | widen F.Cu 0.25 -> 0.5069 mm | 1.200 mV | 1.497 mV | +24.7 % |
| +3V3_MAIN | 1.000 mV | widen F.Cu 0.25 -> 1.6428 mm | 1.000 mV | 1.456 mV | +45.6 % |
| +3V3_MAIN | 0.800 mV | In3.Cu 0.437 -> 3 oz | 0.800 mV | 0.668 mV | -16.5 % |
| +3V3_MAIN | 0.600 mV | none: no lever reaches it | -- | -- | -- |
| +5V_RAIL | 5.500 mV | In2.Cu 0.437 -> 1 oz | 5.500 mV | 3.011 mV | -45.3 % |
| +5V_RAIL | 5.000 mV | In2.Cu 0.437 -> 1 oz | 5.000 mV | 3.011 mV | -39.8 % |
| +5V_RAIL | 4.500 mV | In2.Cu 0.437 -> 1 oz | 4.500 mV | 3.011 mV | -33.1 % |
| +5V_RAIL | 4.000 mV | In2.Cu 0.437 -> 1 oz | 4.000 mV | 3.011 mV | -24.7 % |
| +5V_RAIL | 3.000 mV | In2.Cu 0.437 -> 2 oz | 3.000 mV | 1.616 mV | -46.1 % |

Three things in there that the synthetic board could not show.

The widening rows over-promise, and worse the more they are asked for, which
is the known shape. But the top two do not move the drop *at all* -- 1.497 mV
before and after -- while the estimate promises a 7 % and a 20 % cut. A first
-order estimate being optimistic is expected; being wrong about whether
anything happens is not.

The pour rows err the other way and under-promise, for a reason that is not
approximation error: the required weight is snapped up to one a fabricator
will quote. So the two actions' errors have opposite signs and unrelated
causes, and only re-simulation tells them apart.

And the four +5V_RAIL rows at 1 oz are the argument for this item in one
place: four different targets, one identical fix, one identical outcome of
3.011 mV, and four different promises. The fast path's number tracks what was
asked for rather than what the copper does. "It says it is first order" does
not cover that -- a reader has no way to tell that ~5.5 mV and ~4.0 mV are the
same board.

Still open, and the honest limit of what was checked: the promise graded here
is the caller's *target*, so an action that declines is never graded at all.
Whether declining was right on a given rail -- whether some combination of the
three would have reached the target -- is not something this advisor can
answer, because it sizes one lever at a time.

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

Confirmed on the reference board, on all seven configured rails rather than
only the one the prediction named. At a 0.1 mm step:

| rail | nodes before | after | isolated before | components before -> after | branches |
| --- | --- | --- | --- | --- | --- |
| VBUS_PD | 15,602 | 14,414 | 1,188 | 1,194 -> 6 | 28,083 |
| VBUS_PD_SW | 35,301 | 24,458 | 10,843 | 10,845 -> 2 | 47,761 |
| +5V_U4 | 30,869 | 30,710 | 159 | 163 -> 4 | 60,407 |
| +5V_RAIL | 180,373 | 116,560 | 63,813 | 63,825 -> 12 | 228,386 |
| +3V3_MAIN | 7,313 | 6,413 | 900 | 901 -> 1 | 12,466 |
| +3V3AO | 75,827 | 41,701 | 34,126 | 34,131 -> 5 | 81,006 |
| +3V3AON | 8,972 | 8,062 | 910 | 913 -> 3 | 15,538 |

The branch count is the same on both sides of every row, isolated nodes reach
zero everywhere, and nodes-removed equals isolated-before *exactly* in all
seven. The deletion was of phantoms and of nothing else.

Two corrections to what was written above. The count was 901, not 902: 901
reproduces at the commit that recorded 902, so the number was mis-transcribed
rather than since changed, and the "2 components afterwards" prediction
inherited the slip -- there is one. And +3V3_MAIN turned out to be the
*smallest* case on this board, not a representative one: +5V_RAIL carried
63,813 phantoms, 35 % of its mesh, and +3V3AO 34,126, 45 % of its.

The larger correction is to the ranking. A4 sits under "an analysis currently
reports something untrue", and on this board it does not. Every rail's solved
drop is identical on both sides to six decimal places -- 6.558404 mV on
+5V_RAIL, 1.497185 mV on +3V3_MAIN -- as are the source-node count, every load
coordinate, every real component's size, and the reachable/stranded verdict of
every load. The barrel-into-unreachable-copper hazard is real and all four
cases of `tests/test_mesh_hybrid_zone_nodes.py` fail without the fix, but it
did not fire here: an extra barrel is a branch, and the branch counts are
equal. Nor is the payoff speed -- 186.2 s to 181.5 s for the seven rails,
2.5 %, because meshing and not node count dominates. What the fix bought on
this board is that the hazard is gone, not that an answer moved. Ranked
against its measured effect it belongs in section B, with the other defects
that had not yet reached a user.

**A5. Palace goes silent after the upload.**
The log ends at "Uploading the explicit Palace project directory" with no
completion, failure or timeout line, on every run. Whether the remote solve
succeeds is currently unknowable from the log. Also `probe()` reports the
usage text as a version string, which is cosmetic but reads as a malfunction.

Both halves reproduce, unchanged, in the campaign of 2026-09-06 00:20:43.
`Uploading the explicit Palace project directory to jb@AiHub...` at 00:19:18,
then nothing; the EMC domain closed ten seconds later at WARN having said
neither that the solve finished nor that it did not. And the probe line reads
`Palace server ready: Usage: palace [OPTIONS] CONFIG_FILE`. Ten seconds is
also too short for a remote solve, so the likeliest reading is that the upload
returns and nothing waits on it -- but that is a hypothesis, and the reason it
is only a hypothesis is the missing line.

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

  It is measured now, and it is bigger than the advisor. A campaign runs the
  **whole seven-rail DC solve three times**: once for the DC domain (21.3 s),
  once inside THERMAL for the electro-thermal coupling, and once inside CFD
  for the copper-loss heat sources. Same board, same rails, same numbers --
  `+5V_RAIL` reports `Drop 0.0077 V` and 2091 CG iterations in all three. That
  is about 21 s of a 3 min 51 s campaign, and the campaign already holds the
  DC outcome the other two could read.
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

**B5. A finding's text is output, and nothing asserts on it.**
CFD-004 told users the enclosure solver had "no surface film coefficient" for
four commits after it acquired one -- a false statement about the solver's own
physics, in a report, in the sentence explaining why a number should not be
trusted. It survived because the tests around it check the rule id and the
severity and never read the sentence, so the claim and the code that made it
true were free to drift apart. The same sentence had a second copy in the
result's `limitations`, which is the B1 shape again in prose.

This is a class, not an incident. Every adapter in `analysis_adapters.py`
builds descriptions that make physical claims -- what a solver models, what it
excludes, what a correlation covers -- and a claim in a description decays
exactly like a duplicated default, except that nothing fails when it does. The
sweep is the same shape as B1's: for each rule that explains a *mechanism*,
one assertion pinning the mechanism it names. A5's probe line is the cheap end
of the same problem, printing a usage string where a version belongs.

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

## D. Universality — ingestion that holds on any project

The work is planned in `docs/plan-universalite.md` (French), with the decisions
it rests on. It exists because fixes derived from the reference board alone
did not survive other projects: measured on the KiCad 10 demos, a symbol UUID
is ambiguous on multi-instance sheets and the project name in a symbol's
instance block is wrong on renamed projects -- the two keys a board-only fix
would have used.

**Baseline, 2026-09-13**, from `validation/ingest_corpus.py`, before any
ingestion change. The demos are those installed with KiCad 10
(`<install>/share/kicad/demos`, 36 projects, 15 boards with schematic and PCB,
vme-wren and jetson skipped as PCBs over 25 MB). The reference container is
measured by passing `p02_alimentation`, `main` and `LEDS_sub` explicitly.

| invariant | demos: boards failing | reference container: boards failing |
| --- | --- | --- |
| I1 footprint with a path resolves by that path to one symbol | 15 / 15 (0 % everywhere) | 3 / 3 |
| I2 SCH-005 silent on footprints without a path | 4 (tiny_tapeout 159, cm5 28, interf_u 1, NFC antenna 1) | 2 (p02 5, main 15) |
| I3 no rail on open-collector pins alone | 4 (cm5 8, royalblue 4, video 2, kit-dev 1) | 2 (p02 3, main 2) |
| I4 capacitances recognised by the rules | 1 (cm5 17 of 19: `100n`, `10u`, `4u7`) | 1 (main: `100n / INA238`) |
| I7 ambiguous directory fails loudly | ecc83 resolved silently to `ecc83-pp` | -- |

I5 (SCH-002 on no-connect pins) and I6 (power-flag nets classified) print n/a
until phase 1.3 collects the data they need. What they will measure is already
visible by hand: on pic_programmer, kit-dev and interf_u every SCH-002 finding
is a pin the designer marked no-connect (77/77, 69/69, 63/63).

The reference-only fallback hides I1 on most boards -- the netlist still finds
symbols by designator -- but not on multi-instance designs, where it reaches
39 of 68 footprints (complex_hierarchy) and 36 of 114 (multichannel).

Re-run on 2026-09-13 after the audit recommendations (demos plus
`p02_alimentation`, PCBs over 20 MB skipped): 37 projects, no errors, and the
same failures as the baseline -- I1 16 boards, I2 5, I3 5, I4 1, I7 the ecc83
pick. Expected: those invariants belong to the universality plan's phase 1,
which the recommendations did not touch.

Re-run after phase 1 (schematic read by sheet instance, 2026-09-13; demos plus
the three reference boards, no PCB size cap):

| invariant | demos: boards failing | reference container: boards failing |
| --- | --- | --- |
| I1 | 0 / 15 | 1 (main 437/440: three footprints whose symbols the schematic no longer has) |
| I2 | 4 (unchanged) | 2 (unchanged) |
| I3 | 4 (unchanged) | 2 (unchanged) |
| I4 | 1 (unchanged) | 1 (unchanged) |
| I5 SCH-002 on marked pins | 13 (pic_programmer 77/77, kit-dev 69/69, interf_u 63/63, video 119/217, ...) | 2 (p02 25/26, main 163/165) |
| I6 power-symbol nets classified | 9 (38 of 53 nets; openair-max 0/7) | 1 (main: `+5V_LOGIC`) |

I5 and I6 are measured for the first time and reproduce the hand counts above.
They, I2, I3 and I4 belong to phase 4. SCH-005's false findings on
multi-instance sheets are gone with I1 (complex_hierarchy 29 to 0,
multichannel 78 to 0).

After phases 2 to 4 (same corpus): I2, I3, I4, I5, I6 and I7 fail on no board;
I1 fails only on main, whose three footprints point at symbols its schematic no
longer holds and are now SCH-005's three findings. The schematic rules run in
the batch -- B2's note that `rules/` was reached by nothing is closed, and the
reachability guard covers it.

## E. Audit recommendations — carried out

`docs/audit-coherence-analyses.md` §7 holds the eight recommendations and their
status; `docs/plan-recommandations-audit.md` the plan, the DC gate and its
measured result. All eight are done. What executing them exposed, and what is
still open:

* **The DC analysis now solves the saved board with the hybrid mesh**
  (`DefaultDCSolverEngine`), and the thermal, CFD and advisor read the same
  result; DC-004 says when the open board differs from the file. Getting there
  took five ingestion fixes that no unit test had caught and one real board
  did: no stackup block (4 of 19 KiCad 10 demos), via barrels sized on the
  annular ring, pads and via lands treated as points, arc tracks not read,
  sources and loads held on one node.
* **DC findings carried no net.** `attach_dc_remediations` finds a finding's
  rail by its nets, so no DC-003 had ever been given a sized fix. The wiring
  test built its findings by hand, nets included -- the B2 shape again.
* **Open: the contact model dominates low drops.** On the reference board's
  +5V_U4 the worst load drop is 2.94 mV with one node per pad and 1.20 mV with
  the whole pad as contact. The second is more physical, but neither is
  measured; the uncertainty belongs in the result.
* **Open: AC stays raster.** Its track error is now stated, not removed.
* **Open: barrel plating** is in no board file and rests on the 20 um
  fallback, for every via and plated pad in both meshers.
* **Open: thermal mesh sensitivity on the reference board** (needs a launch
  from KiCad or Zeo) and on coupled runs. On a synthetic board convergence is
  not monotonic -- 105.3, 101.3, 105.7, 104.9 C at 0.5, 1, 2, 4 mm -- so the
  check can say "not converged" and never "converged".

## What is deliberately not on this list

Turbulence, transients, fan curves and radiation in the enclosure CFD. They are
absent from the model and stated as absent in the result's limitations. Adding
any of them before A1 would be building on a solver that cannot yet move heat
from air to wall.
