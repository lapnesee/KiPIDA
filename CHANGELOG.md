# Changelog

## Unreleased

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
