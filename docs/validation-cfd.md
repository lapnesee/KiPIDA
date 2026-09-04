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
| coupling | `EnclosureCFDResult.board_free_stream_velocity_m_s` samples fluid cells two cells clear of any solid, within the board's slab; `ThermalRunRequest.air_velocity_m_s` carries it to `surface_coefficient`. |

Findings 2 and 5 are *not* fixed. The inlet-cell mass loss is inherent to
imposing a plug profile against no-slip, and the production defaults
(`cell_size_mm = 5.0`, `max_iterations = 250`) remain below the resolved
regime. Both are now stated in the CFD result's `limitations` so a reader sees
them next to the number rather than in a document they may never open.

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
