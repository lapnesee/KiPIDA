# Validation of the enclosure CFD

Before this document, nothing in the project had ever checked the CFD against a
known answer. `tests/test_cfd_solver.py` asserts that outputs are *finite* and
that a heated solid gets hotter than ambient — true of almost any arithmetic,
including wrong arithmetic. The question that mattered, and that nobody had
asked, was whether the velocity field means anything.

It was asked because of a specific proposal: feed the CFD's resolved air speed
into the thermal solver's surface coefficient via
`Nu = 0.664·Re^½·Pr^⅓`, blended with natural convection. A velocity field is
allowed to be decorative on a plot and still be unfit to drive a number that
lands in an engineering verdict. So the field had to be measured first.

Reproduce everything here with:

```
python validation/cfd_benchmarks.py            # all cases
python validation/cfd_benchmarks.py profile    # one case
```

## The benchmark, and why this one

A wide rectangular duct, 100 × 8 × 64 mm, with a uniform inlet on XMIN and an
outlet on XMAX. Making the duct eight times wider than it is tall means the
mid-span profile approaches flow between parallel plates, whose fully-developed
laminar solution is exact:

    u(y) = 6·u_mean·(y/H)·(1 − y/H)        u_max / u_mean = 1.5

Two preconditions decide whether that is the right yardstick, and the harness
checks both rather than assuming them:

| quantity | value | requirement | verdict |
|---|---|---|---|
| Reynolds number | 46 | < 2300 for laminar | satisfied |
| hydraulic diameter | 14.22 mm | — | — |
| entry length `0.05·Re·Dh` | 32.4 mm | < duct length (100 mm) | satisfied |

Buoyancy is off and the walls are adiabatic, so the case isolates momentum and
continuity. The energy equation is exercised elsewhere.

## What is right

**No-slip is exact.** `u = 0` at both walls, to the last bit.

**The profile is perfectly symmetric.** Asymmetry 0.00 % — there is no
directional bias in the discretisation.

**The momentum discretisation converges to the analytic answer.** This is the
central positive result, and it takes some care to state, because the obvious
comparison is unfair to the solver.

No-slip is imposed by zeroing the *cell-centred* velocity in the wall cell, so
the effective wall sits at the centre of cell 0, not on the domain face. The
open height is therefore `(ny−1)·dy`, and two cells of dead fluid drag down the
section average. A perfect solver on this grid would not report 1.500; it would
report `1.5·ny/(ny−2)`. Comparing against 1.500 would condemn a correct scheme
for a first-order boundary-placement error.

Against the yardstick the grid can actually reach:

| ny | cells | u_max/u_mean | expected `1.5·ny/(ny−2)` | error |
|---|---|---|---|---|
| 6 | 864 | 1.000 | 2.250 | no profile at all |
| 10 | 2 400 | 1.742 | 1.875 | −7.1 % |
| 14 | 4 704 | 1.688 | 1.750 | −3.5 % |
| 20 | 9 600 | 1.661 | 1.667 | **−0.4 %** |

Monotonic convergence to 0.4 %. The scheme is correct. `u_max` settles the same
way: 0.1082 → 0.1050 → 0.1035 m/s.

**Once developed, mass is conserved plane to plane** to 0.22 %.

## What is wrong

### 1. `mass_balance_error_pct` cannot fail

`_pressure_projection` overwrites the outlet velocity with
`incoming / outlet_area` (`cfd_solver.py:236-244`). `_flow_balance`
(`cfd_solver.py:343`) then computes the error by comparing the inflow against
that same imposed outflow. The two are the same number by construction. The
benchmark returns **3.97e-14 %** — floating-point noise around an identity.

This is not a harmless statistic. It is printed in the report
(`report_presenters.py:88`) and it gates the `CFD-002` conservation finding
(`analysis_adapters.py:742`), which therefore can never fire on mass. The tool
reports perfect mass conservation while losing 5 % of it.

### 2. Five percent of the mass is lost at the inlet

Measuring the streamwise volumetric flux plane by plane, against the flux the
inlet boundary actually imposes:

| plane | flux / imposed |
|---|---|
| 0 (inlet) | 1.000 |
| 1 | 0.770 |
| 5 | 0.929 |
| 15 | 0.947 |
| 28 | 0.950 |

The loss happens in the single cell where the uniform inlet profile meets
no-slip, and the pressure field never pushes it back. Downstream the field is
self-consistent (0.22 % spread) but permanently 5 % short — and the outlet is
then force-fed the full imposed flux, manufacturing a 5 % discontinuity in the
last cell.

*A caution on this number.* An earlier version of this benchmark referenced
plane 1 instead of the imposed inlet flux and reported **+23 %**, with the wrong
sign. Plane 1 is the worst possible reference: one cell downstream of a uniform
inlet, exactly where no-slip switches on. The corrected figure is −5.2 %.

### 3. The deficit is under-solved pressure, not a structural flaw

The projection cleans divergence with plain Jacobi sweeps
(`pressure_iterations`, default 60). Jacobi damps low-frequency error at a rate
that scales with the square of the grid size, and it is precisely the
long-wavelength part of the pressure field that enforces global mass balance.

Testing the mechanism rather than assuming it:

| sweeps | mass error |
|---|---|
| 30 | −8.21 % |
| 60 *(default)* | −5.21 % |
| 240 | −1.62 % |
| 960 | −0.56 % |

Monotonic decay towards zero. The hypothesis is confirmed: this is a solver
setting, not a modelling defect. The principled fix is to solve the Poisson
equation with the sparse backend the project already owns
(`SparseComputeBackend`) instead of iterating Jacobi by hand.

### 4. The convergence test is dimensionally meaningless

```python
combined = max(continuity, momentum, energy_residual)
if iteration >= 5 and combined <= max(1e-10, float(controls.tolerance)):
```

`continuity` is a divergence in **1/s**, `momentum` a velocity change in
**m/s**, `energy` a temperature change in **K**. Three different units are
maxed together and compared against one dimensionless tolerance of 1e-4.

`continuity` sits at ~2.4 1/s and is dominated by the irreducible inlet
discontinuity of finding 2 — it does not fall with more pressure sweeps (2.384,
2.452, 2.544, 2.573 for 30/60/240/960; it slightly *rises* as more flow is
pushed through). So the criterion cannot be met, and `converged` is
structurally always `False`.

That flag is not inert: `adapt_cfd_result` raises a **HIGH** `CFD-001` finding
on `converged == False`. Every enclosure CFD run in the product reports a
severe numerics failure that carries no information.

### 5. The production defaults are far below the resolved regime

`cell_size_mm` defaults to 5.0. A 50 mm enclosure is then 10 cells tall, and a
board-to-wall gap is a handful — the ny=6–10 regime above, where the error runs
from 7 % to total failure. At ny=6 the solver reports `u_max/u_mean = 1.000`:
plug flow, no boundary layer whatsoever. `max_iterations` defaults to 250; none
of the runs here had converged after 3000.

## Verdict on the coupling

**The bulk velocity is usable. The near-wall velocity is not.**

This distinction is what makes the coupling defensible, because
`Nu = 0.664·Re^½·Pr^⅓` takes the **free-stream** velocity as its input — the
correlation models the boundary layer itself, analytically. It does not want
the near-wall value, which is the one this mesh resolves worst.

The bulk speed is the quantity the solver gets right: profile shape correct to
0.4 % when resolved, mass conservation recoverable to 0.6 % by a solver
setting, and plane-to-plane consistency of 0.22 %. Since `h_forced ∝ √u`, even
an uncorrected 5 % velocity error propagates to **2.6 %** in `h` — comfortably
inside the ±20–25 % spread of the flat-plate correlation itself.

So coupling is justified, on these conditions:

1. Take the free-stream speed from cells **offset** from the board, never the
   wall-adjacent cell.
2. Fix the mass diagnostic to measure interior planes, so the number that
   qualifies a run is real.
3. Make the convergence test dimensionless, so `converged` means something.
4. Raise the pressure-sweep default onto the evidence above.
5. Report the coupled `h` as **ESTIMATED**, never DETERMINISTIC, and state the
   mesh resolution it rests on.

What must *not* be claimed: that this is a validated CFD. It is a solver whose
momentum discretisation is now demonstrated correct on one laminar benchmark,
with quantified defects elsewhere. Turbulence, transients, fan curves and
radiation remain out of the model entirely, and no comparison against
measurement has been made.

## What was changed as a result

| finding | change |
|---|---|
| 1 — tautological diagnostic | `_pressure_projection` records the outflow it produced before the fix-up; `_flow_balance` compares against that. On the 12-iteration smoke case the reported error went from **4e-14 % to 10.1 %**. |
| 3 — under-solved pressure | `pressure_iterations` default 60 → **240**, set from the measured table, not from taste. |
| 4 — dimensionless convergence | Each residual is normalised by its own scale before being compared to the tolerance. They now *fall* (0.0505 → 0.0372 → 0.0240 over 12 iterations) instead of sitting at 2.4 forever. |
| 4b — the flag still says False | Normalising made the residuals meaningful; it did **not** make convergence reachable. See below. |
| coupling | `EnclosureCFDResult.board_free_stream_velocity_m_s` samples fluid cells two cells clear of any solid, within the board's slab; `ThermalRunRequest.air_velocity_m_s` carries it to `surface_coefficient`. |

### The Poisson solve replaced Jacobi

Raising `pressure_iterations` to 240 was a workaround for the method, not a
fix. The operator is geometry — the fluid mask and the cell spacing — while
only the right-hand side moves between iterations, so it is now assembled once
per solve and handed to `SparseComputeBackend`, which the project already owned.

| forced smoke case | Jacobi | sparse |
|---|---|---|
| mass error, 12 iterations | 10.10 % | **0.13 %** |
| mass error, 250 iterations | — | **0.084 %** |
| continuity floor | 1.7e-2 | **1.4e-3** |

`pressure_iterations` is now inert. It is documented as dead and kept so saved
projects still load, rather than removed or quietly repurposed; a test asserts
that 1 and 960 sweeps give identical answers, which would fail if anything
still read it.

**The floor moved but did not go away**: 1.4e-3 against a 1e-4 tolerance, so
`converged` is still False. And it is now the binding constraint rather than
the iteration count — 60 and 250 iterations both end at 0.00143 on that case.
That matters for a decision that looked independent: raising `max_iterations`
buys nothing on a forced run until the floor itself is understood. The two are
not separate work items.

### The inlet was fixed; the residual floor was not what I said it was

The plug inlet is gone. Only the inlet cells that must vanish — those on a
transverse wall or against a solid — are zeroed, and the rest are scaled up so
the requested volumetric flow is unchanged. In effect the inlet is now plug
flow across the *open* area rather than the nominal area.

| | before | after |
|---|---|---|
| first interior plane / imposed flux | 0.770 | **0.997** |
| settled interior mass error | −1.62 % | **−0.25 %** |
| reported `mass_balance_error_pct` | 1.424 % | **0.055 %** |

A parabolic taper was tried first and **rejected on measurement**: on a
three-cell-wide patch it zeroes both edges and forces the whole flow through
the middle cell, taking the smoke case from 10 % to 15 % mass error. Coarse
patches are the normal case for this tool, so the gentler rule won.

**And it disproved my own explanation.** The continuity residual floor did not
move: 0.0169 → 0.0174. Finding 4 above claimed the floor was "dominated by the
irreducible inlet discontinuity of finding 2". Mass is now conserved to 0.25 %
and the floor is unchanged, so that attribution was wrong.

The cause is currently **unidentified**. The most plausible remaining
candidate, stated as a hypothesis and not as a result: `_pressure_projection`
calls `_apply_velocity_boundaries` *after* correcting the velocity, which
re-zeroes wall cells the projection had just cleaned, re-introducing divergence
in the cells adjacent to every wall. That would also explain why the residual
rose slightly with more sweeps (2.384 → 2.573 for 30 → 960) — a better-solved
interior makes the boundary correction larger, not smaller. This has not been
tested.

### Found: the residual was measuring the boundary conditions

The floor is gone, and the cause was not what either earlier guess said.

`_apply_velocity_boundaries` prescribes the velocity in the six outer cell
layers and in every patch cell. The pressure projection cannot move those
values — they are Dirichlet data — so whatever divergence they carry is a
property of the boundary condition, not a convergence failure. The residual
averaged them in anyway, reporting the solver as stuck on cells it does not own.

Measured before it was believed: on the validation duct the one-cell shell
against the walls holds **99.9 %** of the total squared divergence, with a mean
9.5× the deep interior's.

Restricting the residual to cells the projection actually controls:

| forced case | before | after |
|---|---|---|
| continuity residual | 1.4e-3 | **7e-17** |
| `converged` at 250 iterations | False | **True** |

Seven times ten-to-the-minus-seventeen is machine zero: the projection was
always exact on its own degrees of freedom. `converged` is now reachable for
the first time in this solver's life, and it means the flow has settled rather
than that a fixed artifact happened to clear.

**Why this is not narrowing the measurement until it passes.** The obvious
objection to excluding cells from an error metric is that it can hide the
error. The defence is that mass balance is an independent, physical check that
was not touched — and it did not move: 0.0845 % before and after. A masked
divergence problem would have shown up there. `tests/test_cfd_validation_fixes`
asserts both together for exactly this reason.

At 60 iterations the same case still reports `converged=False`, because the
momentum residual has not settled. That is the flag doing its job.

### The old finding: `converged` was structurally unreachable

After normalisation the validation duct ends at:

```
continuity = 0.0169    momentum = 1.29e-12    energy = 1.5e-11    tolerance = 1e-5
```

Momentum and energy are converged to twelve digits. Continuity was floored
around 1e-2, so the combined test failed and `converged` was `False` on every
run. **This is now fixed** — see the section above; the floor was the residual
averaging in prescribed boundary cells. The history is kept because two
explanations were offered and both were wrong before the third was measured.

Normalising the residuals made the *number* meaningful without removing the
*consequence* it was blamed for. Since `CFD-001` fires on that flag, the
remaining repair was to stop reporting a bare boolean: the finding now names
the limiting residual and grades severity by distance from tolerance, so a run
at 1e-2 continuity with 1e-12 momentum reports MEDIUM with an explanation,
while a genuinely stalled solve still reports HIGH. Crying wolf on every run
would have trained a reader to ignore the one run that mattered.

The real fix is to ramp the inlet profile instead of imposing a plug against
no-slip, which would remove the floor rather than explain it.

### Cross-validation of the mass diagnostic

The two independent measurements now agree, which is the point of having both:

| method | value |
|---|---|
| solver's own `mass_balance_error_pct` (outlet, pre-fix-up) | 1.42 % |
| benchmark's plane-by-plane interior flux | 1.62 % |

Before the fix these read 4e-14 % and 5.2 %. The reproduced −1.62 % also lands
exactly on the 240-sweep row of the pressure table above.

Finding 2 is now fixed (see above). Finding 5 is not: the production defaults
(`cell_size_mm = 5.0`, `max_iterations = 250`) remain below the resolved
regime. Rather than silently lower a default and multiply everyone's runtime,
`CFD-003` now *measures* the narrowest enclosure direction and says so — HIGH
below 7 cells, MEDIUM below 10, quoting the accuracy the benchmark actually
found at each resolution.

The CFD result's `limitations` carry the same facts, so a reader sees them next
to the number rather than in a document they may never open.

Two guards worth noting in `tests/test_cfd_validation_fixes.py`: the mass-error
test asserts `> 1.0 %` rather than `> 0`, because the old tautology returned
4e-14 % and would have passed a naive positivity check; and the cache-key test
exists because two thermal runs differing only by CFD velocity have genuinely
different surface physics, so silently sharing a cached mesh would be a
correctness bug rather than a stale-cache annoyance.

## Still open

The velocity is plumbed from the CFD result to the thermal surface
coefficients, but nothing yet *decides* to run the enclosure CFD before the
thermal analysis. The campaign runs each domain in isolation with its own
request, by design, so making thermal consume a CFD velocity is a sequencing
decision about the product, not a missing wire — and it is left to be chosen
deliberately rather than introduced silently.
