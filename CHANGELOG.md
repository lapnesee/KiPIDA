# Changelog

## Unreleased

- Fixed the AC tab reading no capacitance from a value that goes on after its
  rating, such as "47u 16V X5R" or "4.7u 50V X7R; Ceff>=1u": every such
  capacitor was taken for a 100 nF DNP candidate and left out of the sweep.
  None of the 29 capacitor values on the P02 Rev E power board was read; all
  are now. The value opening the text counts when a separator ends it and it
  names its unit, so a bare number or a part number still reads as nothing.
- Fixed the raster DC engine's pad locator skipping the bottom copper layer.
  It looked for nodes on layer ids 0 to 31, the old pcbnew numbering, while
  KiCad 10's IPC numbers B.Cu 34, so a bottom-side source or load was found
  only through copper of another layer right above it. On the P02 Rev E power
  board the raster engine left 6 of 17 rails unsolved (J1, F1, D3, U5 and U6
  sit on B.Cu) and dropped the J2 load; all 17 now solve. The locator searches
  every layer the mesh holds.
- Changed how the project's saved files are read for its declarations: once
  per session while no file changes, and not at all without being asked for a
  board over 50 MB, where reading takes tens of seconds; the log gives the
  board's size and names stand in. Zeo container paths written with
  backslashes now resolve on macOS and Linux.
- Changed the EMI/EMC decoupling and connector rules to use what the project's
  files say about each part, on top of the reference prefixes: a part with a
  power_in pin is checked for decoupling even when it is not a U or IC (a
  compute module, a regulator annotated VR), a capacitor symbol or footprint
  counts as a capacitor, and a Connector symbol or footprint marks a
  connector, which is never checked for decoupling nor counted as a capacitor.
- Changed ground detection to start from the project's ground symbols: a net
  KiCad's GND, GNDA, Earth or VSS symbol names is a ground whatever it is
  called, and a net name is only a hint. SCH-001 counts a capacitor to such a
  ground as decoupling; the AC tab lists declared grounds first and selects one
  when the profile has none of its own; the EMI/EMC and differential scans add
  declared grounds to their reference nets without removing any.
- Changed the differential-pair scan to pair nets as KiCad's own router does
  -- past trailing digits and underscores, "+"/"-" or "P"/"N" -- and to read
  the project's netclasses: a pair whose nets share a netclass other than
  Default says so, becomes likely, and takes its target impedance from a class
  named for one ("90ohm", "85Ohm-diff_PCIE"). Other naming habits such as
  USB_DP/USB_DM are still found, as suspected pairs.
- Changed EMI/EMC source discovery to stop inventing frequencies. A source
  takes its frequency from the power tree's converter model, from the value of
  a crystal, oscillator or resonator on its net, from a Frequency or Fsw field
  on its part, or from the standard its differential pair names (USB_HS,
  USB_FS); otherwise it is unknown, where clocks used to get 25 MHz, switching
  nodes 500 kHz, fast interfaces 100 MHz and pairs 240 or 125 MHz. A declared
  crystal or oscillator is a clock source even when its nets have no clock-like
  name. A new EE-003 finding names the sources whose frequency is unknown and
  says their harmonic, switching-band and near-field estimates were not
  computed.
- Changed the power tree's board scan, used when a project has no saved
  configuration, to propose first the rails the project's files declare --
  nets a power symbol names, then nets with power pins -- and net-name guesses
  last. Each scanned rail is listed with how it was found, and a voltage read
  off a name says so. A DC result states which of its rails came from the scan
  rather than the configuration, and lists those only guessed from a name.
- Added the schematic rules to the analysis batch, as the first domain of the
  campaign. They read the saved files of the project the host named and say
  when the open board's footprints differ from them. SCH-001 recognises
  capacitors written without "F" ("100n", "4u7") and skips unconnected power
  inputs; SCH-002 skips pins marked no-connect and pins their library types
  no-connect; SCH-003 is one finding for the project; SCH-004 exists only in a
  Zeo mode and reports cross-board links a board no longer honours and
  differential pairs naming no link; SCH-005 reports only footprints whose
  symbol was deleted, and the new SCH-006 notes footprints placed on the board
  without a symbol. A net named by a power symbol is a rail, and open-collector
  pins alone no longer make one.
- Added the project context: at launch the plugin records which application
  launched it, with the evidence, and the mode that makes of the project it
  was given -- single-board, Zeo container or Zeo member -- and the campaign
  report shows the host and the mode. No detection rule is chosen yet, so every
  launch reads as an unknown host and runs in single-board mode, and the report
  says the Zeo multi-board layer was not applied for that reason.
- Fixed the design model's source hash ignoring every sheet but the root, so
  an edit below the root sheet left it unchanged. It now covers every file the
  model read. A board, schematic or project file that cannot be read, and a
  board project a container names but that does not exist, are listed in the
  model's issues instead of being skipped; the model also carries each
  footprint's schematic link, the netlist's link issues and the schematic's
  sheet issues.
- Changed the offline netlist to join a footprint to its symbol by the
  schematic path the footprint records, and never by reference. A footprint
  now says whether it is linked, placed on the board only, pointing at a
  symbol the schematic no longer has, or read without a schematic; the netlist
  reports footprints sharing a reference and linked footprints whose reference
  differs from their symbol's. A footprint placed on the board only no longer
  takes the pin types and fields of whichever symbol shares its reference.
- Changed the offline board and schematic readers to refuse a file saved by
  KiCad 7 or earlier, with a message naming the file and its format version
  and asking for a save from a current KiCad. The hybrid DC engine then falls
  back to the raster engine and logs that message. A sub-sheet of a current
  project that KiCad 7 last saved is still read, and reported: KiCad leaves
  unedited sheets as they are, and ships a demo with such a sheet.
- Fixed the offline schematic reader reading a sheet placed several times only
  once, under whichever reference the symbol's own property held. The hierarchy
  is now walked by sheet instance: each placement contributes its symbols with
  the reference recorded for its path, whatever project name that record
  carries, and each symbol knows the path a PCB footprint uses for it. On the
  KiCad 10 demos, complex_hierarchy reads 119 symbols instead of 73 and
  multichannel 169 instead of 61; every footprint with a schematic path now
  finds its symbol by that path on the 15 demo boards measured, where none did.
- Changed the offline schematic reader to report a sheet it cannot follow
  instead of skipping it silently: a missing or unreadable sheet file, a sheet
  that contains itself, and -- read, but flagged -- a sheet file named with
  backslashes or with a case the file on disk does not have, which only some
  filesystems resolve.
- Added to the offline schematic reader what the schematic rules will need:
  whether a symbol is a power symbol, which of its pins sit under a no-connect
  marker, its unit and body style and the pins that unit draws, and its BOM,
  board and DNP attributes combined with those of the sheets above it. Placed
  symbols whose embedded definition differs from the library (a `lib_name`,
  261 placements across the KiCad 10 demos) now get their pins; they had none.
- Added a mesh sensitivity check to the thermal analysis, on by default: the
  board is solved again at twice the grid step, the hotspot change is
  published, and above 2 C a TH-004 finding says the result is not converged
  in mesh. Below it the result says that two steps agreeing does not prove
  convergence: on a synthetic board the hotspot read 105.3, 101.3, 105.7 and
  104.9 C at 0.5, 1, 2 and 4 mm (`validation/thermal_mesh_sensitivity.py`).
  Coupled runs are not checked.
- Changed a campaign to solve the DC once: the thermal and CFD runs that need
  copper losses take them from the campaign's DC result instead of solving the
  same DC again from their own capture. A coupled electro-thermal run keeps
  its own solves, a run whose capture differs (rails, grid or board file)
  solves its own and the log says why, and every thermal and CFD result names
  where its copper losses came from.
- Changed EMI/EMC sources discovered without a converter loss model -- clocks,
  fast interfaces other than USB, name-matched switching nodes -- to swing to
  the highest configured rail powering the parts on their net instead of a
  flat 3.3 V; the source's notes name the rail and the parts, and still call
  the 0.1 A current an assumed default.
- Added the raster mesh's track error to every result that uses it: the AC
  sweep always, and a DC run that fell back to the raster engine. Each
  publishes its narrowest track in grid steps and its oblique tracks (arcs
  included), and below three steps -- or with oblique tracks below ten -- a
  limitation quotes the measured resistance ratio, e.g. x0.75 to x1.50 for a
  straight track 1.5 steps wide and x1.06 to x2.12 at 45 degrees. The bounds
  live in `raster_fidelity.py`, and a test re-measures one row.
- Changed every DC consumer -- the DC tab, the campaign, the thermal and CFD
  copper losses -- to solve with the hybrid engine whenever the saved board
  file is known and readable, so they and the advisor read one copper. Without
  a file, or with one that cannot be parsed, the raster engine runs and the log
  says so, with the raster error it carries. The DC result's provenance names
  the engine and the file.
- Added DC-004: the open board differs from the saved file the hybrid engine
  solved. Vias and pads of each rail are compared by position, track and pour
  copper by the area of their difference; the finding names what differs and
  asks for a save and a rerun. Measured silent on the reference board's seven
  rails, and at most 0.05 s per rail.
- Fixed DC findings carrying no net. The advisor finds a voltage-drop
  finding's rail by its nets, so no DC-003 the adapter produced was ever given
  a sized fix, and the campaign could not group DC findings by net.
- Changed the DC advisor to size a hybrid result's fix on that result's own
  sources, loads and grid step -- every declared source, regulator output,
  load and regulator input -- so the drop it sizes against is the one DC-003
  reports (5.694 mV on the reference board's +5V_RAIL for both).
- Fixed the live track geometry drawing an arc track through its start, mid
  and end points only, which cut a quarter turn's corner by 7.6 % of its
  radius; it now follows the same chords as the offline reader.
- Changed how the DC advisor and the hybrid engine place a source or load on a
  pad: on every node inside the pad's outline, on its layers, instead of the
  single nearest node. Holding or loading one node forced the current through a
  point, a spreading resistance the soldered pad does not have: on the
  reference board's +5V_U4 the worst load drop was 2.94 mV that way and is
  1.20 mV with the pad as contact.
- Added `validation/dc_engine_comparison.py`, which solves the same board file
  with both DC engines and judges the gate the plan fixed before measuring. On
  the reference board all four criteria pass: exact synthetic tracks, no
  isolated load on seven rails, 1.9 to 2.4 times the raster time, and pour
  rails agreeing within 5.8 % once both engines locate pads the same way.
- Fixed the offline board reader ignoring arc tracks, an open circuit wherever
  a board routes with them (3 of 4 boards checked, 2,316 arcs on the Jetson
  baseboard demo). Arcs are read as chords of at most 10 degrees, within
  0.13 % of the arc length.
- Added a hybrid DC solver engine: the same rails, regulators, injections and
  result contract as the raster engine, solved on the saved board file with
  exact tracks, barrels, cut-cell pours and pad lands. Its layers are published
  under the live board's ids, paired by name (designer names included). It is
  not selected by default yet.
- Changed DC-003 to judge the drop at the worst-placed load, the quantity the
  DC advisor sizes its fixes against; the spread between the net's highest and
  lowest nodes, which also counts copper no load draws through, is still
  published as a metric. The DC report names both and the engine that ran.
- Fixed a regulator cascade losing demand: a rail skipped for want of copper
  no longer drops its own regulators' input current from the rail feeding it.
- Fixed the hybrid DC mesh treating pads and via lands as points. A track
  ending anywhere but a pad's centre, a pour's thermal spokes, and a track
  running straight across a pad all stayed unjoined, which left loads on four
  of the reference board's seven rails with no path to their source. Pad
  outlines (read from the board file with their shape, size and orientation)
  and via lands are now meshed as copper on the pour lattice, and tracks are
  split where they cross them; all seven rails solve with every load reached.
- Fixed the DC advisor anchoring a surface pad on whatever node lay nearest on
  any layer, typically the plane directly beneath it, which fed the load past
  the vias that really carry it. Pads are now located on their own copper
  layers, a through-hole pad on every layer it reaches, through a spatial index
  instead of a scan of every node per pad.
- Changed planar current density to use the copper section a branch records --
  a track's width, a cut cell's copper fraction -- when there is one, and its
  recorded material (track or pour) over the geometric guess; raster edges,
  which record none, keep thickness times grid step. Hybrid barrels count as
  vias and through-hole pads, with their plating flagged as estimated.
- Fixed via barrel resistance in both DC meshers. The hybrid mesher took half
  of the via size minus its drill -- the annular ring -- for the plating wall,
  making a 0.6/0.3 mm via ten times too conductive; the raster mesher sized the
  barrel from the via pad diameter instead of the drill, nearly doubling it.
  Both now use the drill and the shared plating fallback, since the board file
  records no plating.
- Fixed the offline board reader finding no copper layers on a board whose
  setup was never edited: KiCad writes the stackup block only then, and 4 of
  the 19 KiCad 10 demo boards have none. The copper layers now come from the
  layer table, in physical order, with thickness left unknown, so vias and
  through-hole pads join the layers again. The reader also keeps each layer's
  designer name ("L2GND" for In1.Cu), the name KiCad's IPC stackup reports.
- Fixed the offline board reader mirroring the pads of every footprint rotated
  by anything but 0 or 180 degrees: it rotated pad offsets with a Y-up matrix in
  KiCad's Y-down frame. The DC advisor located sources and loads on such parts at
  the mirrored position. Checked against track ends, which land on pad centres.
- Fixed the offline board reader dropping copper: a zone declared on several
  layers lost all its fills, and a zone split into islands kept only the largest.
  On the reference board 16 filled islands were read where the file holds 40;
  the DC advisor sized every fix on that incomplete copper. Every island on every layer is read now.
- Changed the hybrid mesher to mesh a net's pours as one copper union per layer
  on a lattice aligned on the step, so overlapping zones no longer count shared
  copper twice and islands attach consistently, and to compute cell fractions
  a row at a time: meshing the seven configured rails of the reference board
  went from 142 s to 13.5 s.
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
