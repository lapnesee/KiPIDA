# Changelog

## Unreleased

- Changed how a campaign groups findings into actions: a shared component still
  groups them, a shared net only together with evidence at the same spot on the
  same layer. Every finding names a ground or a rail, and a shared net alone had
  merged unrelated problems -- a PDN impedance miss, a fragmented plane and a DC
  solve that did not converge -- into one action ranked first. Grouping is now
  transitive and independent of domain order.
- Changed domain scores to weigh each finding by its confidence and to charge a
  rule for its three worst findings only, with the EMC analyzer's severity
  weights; the EMC domain keeps its own risk score, so the campaign and the EMC
  tab no longer show two different figures.
- Changed what DETERMINISTIC means: a fact about the run, never a model's
  verdict or a hard-coded threshold. Findings graded HIGH by the older analyzers
  (the EMC rules above all) now read ESTIMATED; AC-001 is always ESTIMATED and
  the AC result states that capacitor mounting inductance and DC-bias derating
  are not modelled; DC-001 is ESTIMATED and says when the mesh step can open
  narrow diagonal tracks; TH-001 is CRITICAL only when the component's thetaJB
  was stated, and HIGH with the assumed value named otherwise.
- Renamed three EMC rules whose identifiers were also used by another domain:
  decoupling `DC-001`/`DC-002` are now `DE-001`/`DE-002` (the DC analysis
  keeps `DC-001`/`DC-002` for copper islands and solver convergence), and the
  hot-component rule `TH-001` is now `TE-001` (the thermal analysis keeps it
  for junction limits). Campaign comparison still pairs EMC actions saved under
  the old identifiers.
- Changed the DC analysis, the AC sweep and the copper losses fed to thermal
  and CFD to model copper at 1.72e-8 ohm-m, the value the DC advisor already
  used; they used 1.68e-8, so every raster-mesh resistance rises by 2.4 %.
  The fallback plating of a via or plated pad whose geometry does not give it
  is now 20 um (IPC-6012 Class 2 minimum) in every engine instead of 25 um in
  some and 20 um in others. All shared physical constants now come from
  `physical_constants.py`, and the DC result names the resistivity and its
  reference temperature.
- Fixed the enclosure CFD's CFD-004 finding, and the matching limitation,
  stating that the solver has no surface film coefficient. Solid-air faces have
  carried one since the film was added; what still limits solid temperatures is
  air-to-wall transport, which is what both now say.
- Fixed the DC advisor's `WIDEN_TRACK` action narrowing copper: the what-if
  applied the proposed width to every segment on the layer, so on a layer with
  a mix of widths -- the normal case -- segments already wider were reduced to
  the proposal. Re-simulated on the reference board, taking the advice moved
  one rail's drop from 1.4972 mV to 2.9083 mV. Only the segments narrower than
  the proposal are changed now, and the finding names them.
- Fixed the DC advisor sizing `WIDEN_TRACK` against the whole rail drop rather
  than the share the layer's tracks carry, which promised targets the copper
  could not reach at any width. Each action is now sized against the drop it
  can actually reach and declines when that share cannot cover the excess.
- Fixed `ADD_STITCHING_VIAS` and `INCREASE_COPPER_WEIGHT` being unreachable
  whenever any track branch appeared on the dominant path -- which on real
  boards is always. They are consulted whenever widening cannot reach the
  target.
- Changed the advisor's predicted-gain wording to report drops in millivolts
  with four significant figures; volts to three decimals rounded whole
  millivolt-scale rails to a single indistinguishable digit.
- Changed the analysis batch to run through `CampaignEngine` instead of
  chaining the per-domain buttons: one cancellable background run that keeps
  the domains already finished, per-domain failure isolation, and one
  aggregated verdict with its consolidated report built without a second
  button press.
- Fixed the campaign's default adapters, which had never met a real engine:
  the differential tolerance and the CFD mesh are read from the outcome rather
  than demanded from the request, the AC engine's `(sweep, optimization)` pair
  is unpacked, and the thermal and EMC adapters take the outcome's `.result`.
- Added the DC voltage-drop budget and board path to `DCRunRequest`, so the
  pass/fail threshold travels with the run instead of being read off the panel
  again when the results arrive.
- Added `analysis_adapters.adapt_dc_run` as the single way to turn a DC solve
  into an `AnalysisResult`, so the sized copper fixes reach the batch's report
  and the DC tab alike; an advisor failure is recorded as a limitation rather
  than costing the DC result.
- Fixed EMI/EMC in a batch reading the session's previous AC, differential and
  thermal results: it now runs after those domains and consumes the ones the
  same campaign produced.

- Added branch-current-based DC current-density diagnostics for routed copper,
  filled zones, overlaps, vias, and plated through holes. Planar maps use an
  automatic P99.5 colour cap while retaining the real maximum in reports and
  persistent structured metrics.
- Added explicit geometry-confidence warnings and clarified that DC density
  maps are diagnostics rather than IPC ampacity certification.

## 0.19.0 - 2026-09-02

- Added an official Palace project backend for a solver installed on a LAN host.
- Added non-interactive OpenSSH configuration with key/agent authentication,
  strict or explicit accept-new host-key policy, bounded connection tests, and
  no persisted password.
- Added explicit Palace project-directory transfer, remote `--dry-run`
  validation, MPI execution, cancellation/timeout handling, artifact retrieval,
  resolved-config/CSV discovery, and structured result provenance.
- Added Palace server settings and a background connection test to the Phase 10
  EMC panel, including an explicit design-data disclosure notice.
- Added and preselected a minimal Palace electrostatic JSON/Gmsh project for an
  immediate end-to-end LAN connection and solver smoke test.
- Added persistence, injection-boundary, command-construction, orchestration,
  validation-failure, and artifact-retrieval regression tests.

## 0.18.0 - 2026-09-02

### Workspace and interaction

- Reorganized the application into Project, Power Integrity, Signal Integrity,
  EMI/EMC, Thermal, Results, and Application workspaces with contextual actions.
- Added resizable/maximizable window behavior, responsive scrollable panels,
  cancellable background analyses, persistent progress, and consistent empty,
  running, completed, cancelled, and error states.
- Added fit-page, fit-width, 100%, zoom, and pan controls for result plots.
- Replaced nested native result/plot notebooks with lightweight selectors after
  Windows crash reports showed access violations while changing pages. Delayed
  plot resize callbacks now ignore views whose native windows are being deleted.

### Results and compatibility

- Added a shared structured result contract for verdicts, findings, confidence,
  metrics, provenance, limitations, artifacts, and elapsed time.
- Added project-persistent, versioned result history with latest-per-analysis and
  full-history views, explicit deletion confirmation, and read-only compatibility
  for version-1 report histories.
- Added severity filters, free-text finding search, complete finding details, and
  dedicated provenance/model-limitation views.

### Analyses

- Added AC Fast/Balanced/Accurate presets, independent AC mesh controls, preflight
  node limits, reliable capacitor detection, cancellable sweeps, and CPU fallback
  after an unsuitable CUDA iterative solve.
- Unified DC, AC, differential, EMI/EMC, 3D thermal, and enclosure CFD result
  adapters and their evidence/limitation reporting.
- Added multi-fidelity EMC source management and Phase 10 isolated external-solver
  execution while keeping relative spectra distinct from calibrated limits.

### Verification

- Expanded deterministic contract, adapter, controller, history, presenter,
  filtering, numerical, and UI compatibility coverage. The current local suite
  contains 334 tests, with one optional hardware/backend test skipped when its
  prerequisite is unavailable.
