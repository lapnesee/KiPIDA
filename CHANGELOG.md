# Changelog

## Unreleased

The AC batch's log no longer reads "AC: AC: sweeping ..." and "AC: AC
profile 1/3": the campaign already prefixes each line with the analysis.
The differential analysis can be reused by a batch too: its captured
geometry is now something the comparison can read.
Comparing an analysis's inputs for reuse reads each shared object and each
path once: the AC batch's comparison went from 9.70 s to 0.81 s on P02.
A batch sweeps its AC profiles side by side, three at a time: P02 Rev E's
three profiles took 121.9 s through the AC engine instead of 227.4 s, to the
same impedances. The networks are still built one at a time.
A reused analysis is no longer published to its tab and history a second
time: it still feeds the analyses after it, but adds no history entry and
renders no plots again.
A batch run again in the same session reuses each analysis whose inputs did
not change, and says so in the log ("reused the result completed at ...").
The comparison reads the inputs' content, private fields, arrays, geometries
and named files included; anything it cannot read makes the analysis run
again.
No exception is dropped without a word any more: a board collection the
IPC refuses, a failed live-board capture before a batch, a Palace remote
stop that did not go through, a Gmsh probe and the debug plots are logged;
the remaining accessor fallbacks say why they fall back.
Rescan Board... in the power tree offers the rail candidates the board
gained since the configuration was made, to tick, and names configured
rails the board no longer has; configured rails are left as they are.
A rail voltage that is not a number is said under the field ("Not a
number: the rail stays at 5 V.") instead of being dropped without a word.
The README describes the analysis batch, the files it writes, how the
report's verdict and lists are decided and where to complete each kind of
missing data; docs/premier-rapport.md walks through a first report in
French.
The CSV files name their campaign, and finding rows the run, the domain's
status and the build; each action says which list of the report it is in
(fix, review, prototype, data); a new domains-<stamp>.csv gives one row
per domain, those that did not complete included.
A batch that ends on DC or THERMAL now writes its report once their plots
are drawn (five minutes at most), instead of a report without them.
The campaign report shows its times in local time with the UTC offset, has
a contents line linking each section it rendered, and prints with every
collapsed block open.
- Fixed the report's glossary giving the priority as "times 1.5 for
  each further analysis", which compounds, while the ranking adds 0.5
  per analysis: three analyses weigh 2.0, not 2.25.
- Fixed each run's log opening on "Live PCB geometry changed;
  invalidated thermal mesh/CSR cache." though nothing had changed: a
  launch starts with no geometry, so the first capture now reads "Live
  PCB geometry captured for this session.".
- Fixed every new project's SPICE library defaulting to one user's
  folder (C:\Users\jbc66\Documents\DAW CONTROLEUR\Lib\SPICE): it is now
  empty until set, and Phase 10 logs when the library is unset or holds
  no MODEL_CATALOG.csv rather than reporting each part as having no
  model. A project that saved its path keeps it.
- Put the batch button on every page, the log included, where a running
  batch's Cancel now shows; a batch no longer pulls the window to
  Results at each domain's end. Its dialog says what each analysis runs
  on and how long it took last time: on P02 Rev E, "3 AC profile(s) of
  17 rail(s); about 6 min last time", and the CFD's default wall
  coefficient.
- Changed the log: it now shows its lines in the interface's language,
  a batch's "THERMAL: " prefix kept, and copies them in English to
  KiPIDA-results/ki-pida.log, one older copy kept past 5 MB. It follows
  the end only while the reader is there, and the start no longer reads
  "[INIT] [INIT]".
- Fixed parts of the French interface staying in English: the sidebar
  navigation, the batch dialog, and the results page's filters and
  views, which sit outside the wx hooks that translate buttons and
  labels. The findings filter, matched by the text it showed, now keeps
  its English name by position.
- Fixed the numeric fields refusing a decimal comma: 77 of them in the
  AC, CFD, differential, EMI/EMC, thermal, power tree, regulator and
  setup dialogs read their text with float(), so "0,1" was an error
  while labels such as "Rendement (0,0-1,0)" invited it. They now share
  one reader that takes a decimal comma and ignores the spaces between
  thousands.
- Added a "Clean up..." button to the result history, which keeps the
  latest five runs of each analysis, the latest five campaigns and the
  runs those campaigns name, after saying how much goes: on P02 Rev E,
  280 paths and 574 MB of the folder's 685 MB. The results folder now
  gets a .gitignore when it is created, so a project's git repository
  no longer offers its runs for commit.
- Fixed a batch archiving each domain's result as its tab adapted it,
  not as the campaign reported it: P02 Rev E's 22:29 THERMAL was saved
  WARN with 9 figures while the report gave it PASS with 10, and its CFD
  4 figures and a 0 s run. The campaign's result now replaces the tab's
  on screen and in the history, keeping the run's plots, so the tab, the
  history and the consolidate button hold what the campaign reported.
- Added "Since the previous campaign" to the campaign report: what was
  resolved, what is new, what changed severity and what is still
  present, finding by finding, on the analyses both campaigns ran, with
  the score changes. It says when the board or Ki-PIDA changed in
  between. The comparison of P02 Rev E's 21:42 and 22:29 campaigns
  reads SW-004 (L1, U4) resolved and TH-008 still present; comparing
  whole actions read "TH-008 introduced".
- Added campaign-<stamp>.json beside each campaign report: the campaign
  itself, with every domain's result, its actions and its coverage.
  P02 Rev E's results folder held 22 reports and their CSV files, and
  no campaign a later run could be compared with (340 kB for the 22:29
  one).
- Added the Ki-PIDA build to every result and campaign, and to the
  report's appendix: the version, the hash of its sources and, in a copy
  made with git archive, the commit and its date. P02 Rev E's EMC score
  read 65, 35, 63 and 65 between 2026-09-30 and 2026-10-01 at one board
  fingerprint, and nothing said the tool had changed. A result saved
  before keeps an empty build rather than borrowing the current one.
- Fixed two dialogs passing their defaults off as entered values: a
  manual EMI/EMC source read HIGH confidence even when left at 25 MHz,
  2 ns, 3.3 V and 0.1 A (it now reads LOW and names the values kept),
  and a new load took 1.0 A unless changed, while "0,5" or any other
  unreadable current became 0 A without a word. A load's current is now
  required, and a decimal comma is accepted.
- Changed TE-002 to grade each capacitor by its excess against the
  model's uncertainty under it: the board's move between the two
  thermal grids, plus 2.7 K when the enclosure's wall coefficient is
  the default (TH-009). Above it the capacitor stays to fix (HIGH);
  within it, it is to review (MEDIUM), with its excess and uncertainty.
  P02 Rev E's 13 X5R capacitors were all to fix, C49's 0.7 K as much as
  C37's 21.8 K.
- Added an AC source preset for a rail fed through an eFuse, load switch
  or shunt from a regulated one (~20 mOhm + 5 nH): P02 Rev E's
  /+5V_RAIL sits behind U15 (12.2 mOhm) and R49 (7.5 mOhm), and the
  "regulator output" preset alone, 50 mOhm, exceeds its 43 mOhm target.
  AC-006 now names the AC Impedance page and its Source model, R and L
  fields rather than "the rail's AC profile".
- Made the switching inductor models editable on the EMI/EMC page (Edit
  Selected Model, or a double-click): inductance, input and output
  voltage, frequency, output current, ripple and shield attenuation.
  SW-004 named L1's model at 4.36 A against U4's 4.726 A on P02 Rev E,
  in a table that could not change it, and its recommendation now says
  where to enter the value. The table shows each model's current and
  frequency.
- Added an efficiency curve to the regulator dialog, one point per line
  ("Iout efficiency" at the regulator's own input and output, or "Vin
  Vout Iout efficiency"), with its source and reference. TH-008 asked
  for this curve while the dialog took a single efficiency, which the
  loss model always treats as a fallback, so the finding could not be
  cleared without editing the sidecar. The dialog's parameter grid,
  declared 2 x 2 with three rows, no longer trips a wx assertion.
- Fixed settings being lost on close and the configuration file being
  rewritten in place. Closing Ki-PIDA and starting a batch now save the
  project's configuration. A file edited elsewhere since it was loaded
  is never overwritten silently: on close Ki-PIDA asks, before a batch
  it logs why it did not save. Each save writes a temporary file, then
  swaps it in, and keeps the five previous versions as timestamped
  copies; P02 Rev E's own p02_revision_e.kipida.json.bak is not
  touched.
- Fixed the schematic rules reading a rail's voltage from its net name
  only: 11 of P02 Rev E's 17 rails name none, every 20 V one among
  them, so six resistors on them (R62 on /VBUS_SYS, R92 and R3 on
  /J1_PD_OUT, R44, R39, R59) were never checked. The power tree's
  nominal voltages now come first; on P02 the dissipation check covers
  35 resistors, R62 the most loaded at 8.5 mW.
- Added TH-009, data to complete, when the thermal analysis balances its
  ambient against an enclosure whose wall coefficient is the default
  5 W/m2K: on P02 Rev E it put the internal air 16.2 K above 45 C, and
  six of TE-002's thirteen X5R capacitors fall back under 85 C at
  6.5 W/m2K. The finding gives the rise at 1 W/m2K either side.
- Fixed "What holds" claiming more than was checked, on P02 Rev E's
  22:29 report:
  - AC passed "3 of 3 rails" with two on the default source;
  - DC's "14 of 14" left out three rails without a load;
  - the thermal/CFD air check compared one formula with itself.
  A pass resting on inputs still to complete now says how many, the DC
  counts its unjudged rails, and the CFD publishes the mean of its
  solved air, which the check now reads.
- Fixed the report's verdict contradicting its own lists: P02 Rev E's
  22:29 report read "WARN -- Review before manufacture" above "Fix
  before manufacture (2)", one of them C88 and R10 cut off from GND.
  The verdict now fails as soon as one action is to fix, by the same
  rule that sorts the lists. A finding about the calculation (a solve
  that did not converge) makes its domain WARN at most, no longer FAIL.
- Fixed the campaign report leaving out a selected analysis that
  produced no result: one that could not be prepared, failed or was
  cancelled first appeared only in the log, and the verdict could read
  "PASS -- No defect found by the analyses that ran". The report now
  opens on "Did not complete", naming each one with its reason, and the
  verdict is never PASS while one is listed. An unselected analysis is
  no gap.
- Changed TE-002 to say when every capacitor's excess is larger than the
  board under it moved at twice the thermal grid step, with the largest
  move. P02 Rev E's 22:29 report said nothing, which read the same as a
  check that never reached EMC.
- Fixed Phase 10 logging a board circuit's current as differing from
  the entered source's for a rounding: P02 Rev E's 22:29 run logged "U4
  draws 4.73 A ... the source is entered at 4.726 A", the tree's 4.7262
  A as entered. The line now needs SW-004's 5 % to appear.
- Added AC-006, data to complete, for a rail whose source impedance is
  the untouched 10 mOhm + 1 nH default: P02 Rev E's 21:42 report passed
  /+5V_RAIL (U15) and /+5V_U4 (R49) on it, and only the appendix said
  that the default sets the impedance below a few MHz. The rail keeps
  its status; the finding names the source and the rail.
- Changed SCH-003 to bound a chip resistor's dissipation itself, from its
  value, its footprint's size and the known rail voltage across it. P02
  Rev E's 29 resistors without a Power field were all data to complete;
  all are 0603 parts of 4.7 kOhm or more on 3.3 and 5 V rails, at most
  2.3 mW (R15) of the 100 mW an 0603 is rated for. A resistor under half
  its rating now holds (SCH-007, under "what holds"); SCH-003 keeps the
  ones it cannot bound.
- Fixed SCH-006 listing footprints that join no net: P02 Rev E's 21:42
  report put its six fiducials (FID1 to FID6, one pad on no net) under
  "data to complete" with "nothing to do". Only a board-only footprint
  with a pad on a net escapes the schematic rules, and only those are
  listed.
- Fixed TE-002's title and recommendation showing in English under the
  French interface since they became plural (one finding per
  dielectric). The template is regenerated and both are translated.
- Fixed TH-004 saying no part's margin depended on the thermal mesh
  while TE-002 judged capacitors closer to their rating than the mesh
  moves: on P02 Rev E's 21:42 run the hotspot moved 4.02 K between the
  0.2 and 0.4 mm grids, and six of the 13 X5R capacitors were over 85 C
  by less (C68 +3.9 K down to C49 +0.7 K). The double-step check now
  keeps the board under every footprint at both steps on the thermal
  result; TE-002 names the capacitors over their rating by less than the
  board under them moved, and TH-004 speaks of junction margins only.
- Fixed TH-008 giving a small converter's share of the board's heat as
  "0%": P02 Rev E's 21:42 report said U6 "loses 0.0133 W, 0% of the
  4.82 W". A share under 10 % keeps two digits (0.28%).
- Fixed the report printing large figures with an exponent: P02 Rev E's
  21:42 report read 1530 EMC checks as "1.53e+03", a 102,048-node AC
  mesh as "1.02e+05" and /VBUS_SYS's worst frequency as "3.83e+04 Hz".
  A whole number now prints whole and other values to 3 digits, with a
  narrow space between thousands; a frequency reads in kHz, MHz or GHz
  (38.3 kHz, 35 MHz), in the tables and in the AC texts ("100 MHz", not
  "1e+08 Hz").
- Added the least junction margin to the thermal figures when it is not
  the hottest part's: P02 Rev E's 19:12 report gave U14's 25.7 K (124 C
  of 150) as its key margin while U4 keeps 22.4 K under 125 C. A fuse
  judged by its current derating (F1, F2) is left out.
- Fixed the component view giving its rows to a finding about many
  parts: P02 Rev E's 18:31 report listed TE-002's 13 capacitors and left
  out C88 and R10, DC-008's parts. Parts named by a narrower finding
  come first, then the hottest junctions, then a survey's parts in the
  room left (20 rows), hottest first.
- Fixed the decision lists counting the parts beyond the eight shown
  after the nets ("R9 Â· rÃ©seau /+3V3AO, ... et 21 de plus" on P02 Rev
  E's 16:51 report): the count follows the parts. Figures and places are
  separated by "Â·", not by an English "; ".
- Fixed the component view dropping the hottest junctions once sixteen
  parts were cited: P02 Rev E's 16:51 report left out U14, 124 C of its
  150 C limit. The five hottest junctions now keep their rows, and a
  finding naming many parts (TE-002's capacitors) lists them all.
- Fixed SW-004 judging a source revalued from its board circuit, whose
  current is the simulated inductor peak: P02 Rev E's 16:51 run flagged
  U14 (3.87 A against the tree's 3 A) and U5 (1.26 A against 1 A).
  The finding also names only the parts whose values stray: it named L2
  and L3, whose models agreed.
- Changed TE-002 to one finding per dielectric, listing its capacitors
  hottest first, and left it out of the EMC risk score. P02 Rev E's
  16:51 run had 13 X5R capacitors over 85 C (C37 106.8 C, +21.8 K, down
  to C49 85.7 C): 13 actions to fix, and 24 of the 30 points the EMC
  score lost (65 to 35), for a rating question, not an emission risk.
- Fixed a batch's consolidated report still holding no figure: each tab
  filed its result under a run id of its own, not the campaign's, so the
  plots were never found (P02 Rev E, 2026-10-01 16:51, a 166 kB report).
  The campaign now sets one run id per domain run, and the tab files
  its result under it.
- Fixed the French interface showing 450 messages in English: the
  catalog template had not been regenerated since the DC current
  density work. It now holds the 1,581 messages the sources show, all
  translated (batch, AC, DC, EMC, Phase 10, Palace, results views). The
  extractor now skips regular expressions, Windows paths, tags such as
  "board-circuit" and log lines that are only a "[TAG] {p0}".
- Added a component view to the campaign report: one row per part a
  finding names, plus the five hottest junctions, with its heat, the
  board temperature under it, its junction against its limit and what
  each analysis says of it. The thermal result now carries these per
  part. On P02 Rev E, Q10 reads its SW-003 hot loop beside its heat.
- Changed the campaign to make one action of a switching stage: SW-001,
  SW-002 and SW-003 findings on the same switch node now group. P02 Rev
  E's /SYS_5V_SW1 hot loop (C37, Q10, Q11) and its harmonics and copper
  were two actions, ranked second and eighth; they are one, second.
- Changed the campaign report to set the thermal and CFD internal air
  side by side: under "what holds" within 3 K, under calculation
  reliability beyond. P02 Rev E: thermal 61.2 C, CFD 60.65 C, 0.55 K
  apart. Figures an analysis marks NOT_USABLE are named with its reason
  above its table: the CFD's 170.9 C solid maximum, under a PASS, was
  explained only in the appendix.
- Added TH-008, data to complete: a converter stage whose loss is set
  by the configured efficiency fallback, an estimate. LOW from 5 % of
  the board's heat, INFO below. P02 Rev E's U4 loses 1.81 W, 38 % of
  4.82 W, on an estimated 93 %; U6's 0.0133 W is a note. A thermal
  result with notes only now passes.
- Added power_tree.py, where every domain reads a switching converter's
  output current and frequency from the power tree. The EMC rule SW-004
  (LOW, data to complete) names an entered source or inductor model more
  than 5 % off the tree's current or 10 % off its frequency, and Phase
  10's board circuit now draws the tree's current. P02 Rev E's U4 SW1
  source and L1's model read 4.36 A; the tree gives U4 4.726 A, which
  the AC target used. U14's 3 A and U5's 1 A agree.
- Added the board temperature under every footprint's pads to the
  thermal result, heat source or not. TE-001 now names the capacitors,
  inductors and ferrites above 85 C instead of the hotspot alone, and
  the new TE-002 flags a capacitor above its dielectric's rating (X5R
  85 C, X6S 105 C, X7R and C0G 125 C), read from its value. P02 Rev E's
  C37 (22u 35V X5R) has its supply pad 4.4 mm from the 118.99 C hotspot
  on Q10's drain pad, and its own temperature was never reported.
- Changed the campaign report to follow the interface language: its
  headings, decision lists, cards, glossary and the rule guide's names,
  risks and checks are in the French catalog (363 messages). Finding
  titles and descriptions stay as the analyses wrote them. The catalog
  extractor now reads report/ and rule_guide.py, and N_ marks a table
  entry built before the language is chosen.
- Fixed the campaign report showing no figure: plots are saved after a
  result, so the results it aggregates listed none. They are now read
  from each run's own history folder, matched by run id. P02 Rev E's
  2026-10-01 report had none of its 249 plots. Each analysis embeds 8,
  3D overviews first, and lists the rest: 30 images in 5.5 MB, where
  DC's 227 alone take 13 MB.
- Changed the campaign report to open on a decision: fix before
  manufacture, review, check on the prototype, data to complete,
  calculation reliability and what holds, with a "how to read" note and
  a glossary. Each action is a card: why it is a risk, what to do, how
  to check, how sure, typical effort. Figures fold under 3 to 5 key
  ones, identifiers move to the appendix, units read C as Â°C, ohm as Î©.
  On P02 Rev E only DC-008 (C88, R10 cut off from GND) is to fix;
  SW-003's 20 mm threshold goes to the prototype list.
- Added a guide to the 72 rules (rule_guide.py): name, why it is a risk,
  how to check it, typical fix effort, and whether it asks for a change
  on the board, an input, or a better calculation. Calculation findings
  (TH-004, DC-005, CFD-001...) leave the campaign's actions and score:
  on P02 Rev E's 2026-10-01 campaign TH-004 ranked third of 13 actions
  and cost thermal 6 of its 13.8 points (score 86.2, now 92.2). An
  action with no computed remediation takes its rules' typical effort,
  not MEDIUM: DC-008 (C88 and R10 cut off from GND) reads LOW.
- Added a current derating to a thermal component (rated current and its
  datasheet curve, saved in the sidecar): a fuse within 75 % of its
  derated rating (Littelfuse, UL 248-14) gets an INFO TH-007 instead of
  TH-006. P02 Rev E's F1 keeps 4.53 A at 87.2 C and carries 2.17 A, F2
  4.55 A at 83.7 C for 3 A (Bourns SF-1206F curve, digitised).
- Changed the AC target provenance to say "regulators and pass elements"
  for what a rail feeds: P02 Rev E's /+5V_U4 named its eFuse U15 and net
  tie NT2 "converters".
- Changed TH-004 to weigh a junction's move between grid steps against
  its margin: P02 Rev E's Q10 moved 4.02 K 55.16 K under its 175 C limit
  and kept the check MEDIUM; a move that leaves a part over 20 K under its
  limit when doubled now leaves TH-004 LOW.
- Fixed a converter's AC step counting the regulators it feeds by their
  output current: P02 Rev E's U4 read 5 A, U5's 1 A at 3.32 V taken for
  the 0.726 A it draws at 5.08 V; it is 4.726 A (DC: 4.746 A) and
  /VBUS_SYS's target 0.08464 ohm, not 0.08.
- Changed TH-006 on a fuse to speak of its temperature against its
  operating limit and ask for its current derating: P02 Rev E's F1 and
  F2 (SF-1206F, -20 to +105 C) read as thin junction margins.
- Changed TH-004 to go LOW when the hotspot moves between grid steps but
  no junction estimate does, and to name the finest grid the node limit
  allows: P02 Rev E's hotspot moved 2.87 C (F1 and F2 0.1 and 0.2 K), and
  "a finer grid" meant 0.1 mm, 6.2 M nodes against 4 M; it names 0.127 mm.
- Changed a package's thermal heat to enter the board through its heat
  pads (those of at least a quarter of its largest pad's area), not all
  copper under it: P02 Rev E's 115.21 C hotspot sat on the /SYS_5V_SW1
  pour under Q10's body, soldered to nothing; Q10 now heats its 17.4 mm2
  drain pad, U14 its exposed pad, U15 its four power pads.
- Changed SCH-003 to count a wattage written in a resistor's value or any
  field: P02 Rev E's shunt R49, "7.5m 1% / 3W", was listed as unrated (30
  resistors, now 29).
- Changed a derived AC target to print four significant figures: P02 Rev
  E's /+5V_RAIL read "0.0429947 ohm" beside "0.3741 ohm", now 0.04299.
- Fixed a board circuit read from the cache logging "(0 s)": P02 Rev E's
  U4, U14 and U5 now say "read from the cache".
- Changed a psiJB's or thetaJB's junction estimate to add to the board
  1 mm from the package, where JEDEC JESD51-8 and -12 measure it, not to
  the peak of the part's own pads: that peak rose at every refinement (P02
  Rev E's U15 in a model of the board around it: 94.1, 94.2 and 99.6 C at
  0.4, 0.2 and 0.1 mm; 91.3, 91.6 and 93.0 C for Tj now). The metric is
  read off the thetaJB's provenance: U4, U5, U6, U7, U12, U14, U15 and U16
  state one; a thetaJC or thetaJL (Q10, Q11, D3) keeps the copper under
  the part.
- Fixed a package's heat leaving its footprint when no grid cell lay
  wholly on its pads: only whole-copper cells took heat, so at 0.4 mm
  P02 Rev E's U15 (0.5 mm pads) put its 0.35 W on pours up to 1.8 mm away
  and read 103.8 C against 119.8 C at 0.2 mm, which TH-006 extrapolated to
  135.9 C. Each copper cell of the footprint now takes heat in proportion
  to its copper; a model of the board around U15 spreads 2.0 K, not 5.7 K.
- Changed the EMC analysis to value each switching source its board
  circuit settles from that circuit: Phase 10's SPICE audit and board
  circuits now run first, and the spectrum, SW-001 and the near-field
  solver take the simulated frequency, swing and switched current (the
  inductor's peak), the rise time as configured. P02 Rev E, read from the
  cache in 3 s: U4 397 kHz, 20.03 V, 5.55 A for 400 kHz, 20 V, 4.36 A;
  U14 478 kHz, 19.99 V, 3.87 A for 500 kHz, 20 V, 3 A; U5 647 kHz,
  5.07 V, 1.26 A for 650 kHz, 5.08 V, 1 A. Drive indices rise 2.0-2.2 dB.
- Added Phase 10 board circuits: each switching source whose model PSpice
  for TI runs is simulated in the saved board's own circuit (model pins by
  symbol pin name, the passives and MOSFETs around the IC, DNP parts out)
  until it settles. Its switch node and inductor peak make the excitation,
  and PH-005 names a frequency, swing or output it contradicts. P02 Rev E
  (none contradicted): U4 397 kHz after 1.2 ms (556 s), U14 478 kHz and
  U5 647 kHz after 0.3 ms, switching 5.55, 3.87 and 1.26 A at the
  inductors' peaks where the ideal pulse took the 4.36, 3 and 1 A loads.
- Fixed PSpice output columns being lost when a probe's name fills its
  16-character header column and runs into the next one. P02 Rev E's U5
  board bench printed V(N_MAIN_3V3_SW)I(L_L2), which read as a single
  name, so none of its 20,002 rows were parsed.
- Added PSpice for TI to Phase 10: a model ngspice cannot run (an
  encrypted TI library, or one whose ngspice transient is unstable) is
  run in its manufacturer's bench, or a minimal one, and must switch.
  P02 Rev E: U14 406 kHz in 39 s, U5 840 kHz in 25 s (ngspice-unstable),
  U4's TPS55288 402 kHz in 1095 s, each result cached.
- Changed the Phase 10 SPICE audit to recognise an encrypted library
  (TI's $ENCRYPTED_LIB and Cadence's $CDNENCSTART) whatever its pin
  mapping: TI's models for P02 Rev E's U4 (SLVMDD9, sub-circuit TPS55288)
  and U14 (SNVMCB2) run in PSpice only and would have read "available".
- Fixed a CFD obstacle smaller than one cell on every axis taking a whole
  cell of its own bulk conductivity: P02 Rev E's U15 (3 x 3 x 1 mm)
  replaced a 5 mm board cell, renamed and conducting at 8 W/mK. It now
  adds its heat to the solid cell it sits on and leaves that cell as is.
- Changed DC-008 to also name a pad that touches no copper of its net:
  the hybrid mesher left such a pad out without a word. None of P02 Rev
  E's 172 GND and 17 rails' pads is one; C88 and R10 stay the only two.
- Changed TH-006 to read each junction at both mesh steps and to go HIGH
  when a first-order extrapolation crosses the limit: P02 Rev E's U15 sat
  2.7 K under its 125 C limit while the hotspot moved 3.28 K between 0.2
  and 0.4 mm, and nothing said whether its own margin survived the mesh.
- Changed the coupled thermal run to balance a sealed enclosure's air on
  the heat each iteration deposits, not on a pre-solve estimate: P02 Rev
  E's air rise was sized on 4.96 W (16.7 K) while the coupling converged
  on 5.18 W, 0.7 K more, a quarter of U15's 2.7 K margin.
- Added DC-008, naming the pads the saved copper joins to nothing else of
  their net: P02 Rev E's C88 and R10 GND pads sit on F.Cu pour pieces of
  11.99 and 8.74 mm2 with no via, and the solve only logged "Ignoring
  floating island" (329 and 276 nodes).
- Fixed a track segment shorter than the 1 um snap being skipped though
  its ends rounded to two different nodes: P02 Rev E's /J1_PD_OUT has a
  0.8 um segment at (228.67, 66.11), and R92 and U1 sat on a 478-node
  island every DC and thermal run reported as floating copper.
- Fixed AC ports, sources and capacitors meeting the hybrid mesh at one
  node per pad, a contact that shrank as the grid was refined: P02 Rev E's
  /VBUS_SYS, ported on Q10 with no ground pad, read 0.1725 and 0.2013 ohm
  at 100 MHz at 0.5 and 0.25 mm, 16.7 % apart. Every node under the pad,
  and the ground under it, now reads 0.1458 and 0.1530 ohm, 4.9 % apart.
- Fixed PD-001 and PD-002 quoting the flat target beside a verdict read
  against the frequency-dependent one: P02 Rev E's /VBUS_SYS read "worst
  impedance 0.2061 ohm is within the 0.08889 ohm target", 0.392 ohm
  being allowed at its 38.3 kHz. They now quote the least-margin point.
- Changed the AC grid check, and the choice of worst port, to the point
  of least margin to the allowed impedance rather than the worst |Z|:
  P02 Rev E's /VBUS_SYS was checked at its 38.3 kHz source resonance,
  0.2061 ohm against 0.392 ohm allowed (53 %), not at 100 MHz, where the
  0.254 ohm allowed leaves the least margin.
- Fixed a converter's chopped input current replacing a rail's averaged
  load step when it is the smaller of the two: P02 Rev E's /+5V_RAIL
  feeds U5 (0.5 A chopped) and J6 (4 A), and was held to 0.2032 ohm from
  650 kHz up, 0.04657 ohm only below. The averaged step now sets the
  target at every frequency there, and caps a converter's rising mask.
- Changed DE-001 to stay MEDIUM for an IC whose only signals are an
  I2C/SMBus port: P02 Rev E's EEPROM U18, with C13 22.9 mm away, ranked
  the campaign's first HIGH action on the same bar as a processor.
- Changed SW-003 to measure the loop the chopped current runs, input
  capacitor to high side to low side, found from the pads' supply and
  ground nets: P02 Rev E's /SYS_5V_SW1 finding listed U4, L1, C43 and
  R57 but no input capacitor, while C37's ground pad sits 18.2 mm from
  Q11's.
- Fixed the efficiency-residual warning naming the controller when the
  residual split sends it elsewhere: P02 Rev E's U4 residual, split onto
  L1, was reported "placed on Q10".
- Added buck switching losses from the MOSFETs' datasheet charges
  (transitions, output charge, body-diode recovery, dead time, gate
  charge): P02 Rev E's U4 leg put 1.36 W of efficiency residual on Q10;
  its CSD18540Q5B charges give Q10 1.02 W and leave 0.22 W to itemise.
- Fixed a capacitor model taken from its manufacturer's data being
  replaced at each rediscovery by the case-size and generic DC-bias
  estimate: P02 Rev E's C36 rediscovers at 11.4 uF under 20 V, where
  Taiyo Yuden's curve gives 5.87 uF. A "datasheet" source now carries over.
- Changed the CFD solid and air maxima to read INDICATIVE, not PASS, on a
  mesh of 14 cells or more across: P02 Rev E's rose from 181.0 C to
  190.6 C between 10 and 15 cells across (34 s to 737 s), away from the
  3D thermal's 118.0 C. Fourteen cells validates the flow profile only.
- Added a first-order Richardson estimate to the AC grid check: P02 Rev
  E's /VBUS_SYS read 0.2294 ohm at 0.5 mm and 0.2597 ohm at 0.25 mm at
  100 MHz, about 0.29 ohm converged, against 0.254 ohm allowed. AC-005
  now states it, and a mesh-dependent verdict is ESTIMATED, not
  DETERMINISTIC, which had made it fail the domain outright.
- Fixed a pass element hiding the converter behind it from the AC target:
  P02 Rev E's /+5V_U4 feeds U5 through the eFuse U15, and was judged
  7.95x a flat 0.0466 ohm target at 100 MHz instead of 1.28x the 0.290
  ohm U5's 0.5 A step allows there.
- Fixed AC-005 judging a mesh-dependent verdict against the flat target:
  P02 Rev E's /VBUS_SYS passed at 0.5 mm (0.2294 ohm against 0.254 ohm
  allowed at 100 MHz) and failed at 0.25 mm (0.2597 ohm), yet stayed
  MEDIUM because the flat 0.0889 ohm lay below both.
- Changed a converter-step AC target to apply from the converter's
  switching frequency up: below it the chopped current has no content,
  and the rail's averaged step sets the target. P02 Rev E's /VBUS_SYS
  was held to U4's 0.0889 ohm from 1 kHz; below U4's 400 kHz its 2.04 A
  average now gives 0.392 ohm.
- Fixed the AC text report judging a band-limited target flat: P02
  Rev E's /VBUS_SYS read "Status: PASS" above "Worst/target ratio:
  2.48x" and 9 exceeding points, all above its 35 MHz band.
- Fixed CUDA solving a reassembled matrix with the previous one's values
  when both share a sparsity: P02 Rev E's thermal mesh, rebuilt after its
  surface coefficients were refined, solved with the old boundary
  conductances and read a 7.48 % energy balance (4.695 W in, 4.343 W out).
- Added a "continuous" conduction role for a buck-stage MOSFET held on
  through the whole period: P02 Rev E's U4 boost-leg high side, 0.151 W
  in buck mode, drew a warning to declare "high", which would have cut
  its loss by the 0.254 duty.
- Changed the AC target to hold flat only over its step's bandwidth,
  0.35 / rise time, and to rise in proportion to frequency above it. The
  edge comes from the EMC source switching the converter whose chopped
  current sets the target, or from the profile's transient_rise_time_ns.
  P02 Rev E's /VBUS_SYS failed only from 46 MHz on U4's 4.5 A step, whose
  10 ns edge puts its bandwidth at 35 MHz.
- Added TH-006, a MEDIUM finding for a junction under its limit by less
  than 20 K: a thermal PASS needed only a margin above zero, and P02
  Rev E's U14 would pass on about 9 K once its sealed box's 15.25 K air
  rise applies, on a loss closed by a low-confidence efficiency fallback.
- Fixed a campaign's thermal run ignoring the enclosure its CFD models
  when the flow is buoyant: P02 Rev E's THERMAL solved at the 45 C
  outside ambient while the same campaign's CFD put the sealed box's air
  15.25 K above it, so U14's 126.1 C junction (23.9 K margin) was about
  15 K optimistic. The box now reaches THERMAL before either runs.
- Changed AC preparation to parse the saved board once while the file is
  unchanged: P02 Rev E's three AC profiles parsed it in turn on the UI
  thread, 0.9 s each.
- Changed the EMC risk map to draw the board's tracks as one collection:
  one line per track kept P02 Rev E's EMC plots 12 s behind Phase 10 at
  18:31, the map alone taking 6.2 s; it now draws in 0.34 s.
- Changed the DC rail log line to count the regulators a rail is fed by
  and feeds: "Sources: 0, Loads: 0" headed 10 of P02 Rev E's 17 rails,
  /VBUS_SYS among them, which then reported a 4.8 mV drop.
- Fixed the DC ground return naming parts whose return it did model:
  P02 Rev E logged "No GND pad located for: D3, Q10", though U4's ground
  pads carried the return of the converter Q10 feeds, and D3, an OR
  diode, draws nothing from ground.
- Changed a campaign to refresh the live board once while it prepares,
  not once per domain: P02 Rev E's 17:06 batch refreshed it six times,
  about 1 s of frozen PCB Editor each.
- Fixed a campaign domain's own status disagreeing with the synthesis
  table: P02 Rev E's DC logged and headed its report section WARN on
  eight INFO DC-006 findings while the synthesis scored it PASS 100/100.
  The campaign now applies the one status rule to every result.
- Changed Phase 10 to mesh a full-wave region coarser, up to 1 mm, when
  the configured step would exceed the cell limit, instead of skipping
  it: both of P02 Rev E's regions (9.2M and 2.1M cells at 0.25 mm for a
  2M limit) were skipped on every run.
- Fixed an empty live capture making one DC-004 action per rail (17 of
  the 20 actions of P02 Rev E's 16:50 campaign): no comparison with the
  saved board was made, and the DC result now says so once, as a
  limitation.
- Fixed a campaign started while KiCad synchronises: the batch now waits
  up to 6 s for the live board to answer before capturing it, EMC
  refuses a capture with copper but no footprint (P02 Rev E scored 87/100
  on one), and AC names the empty board instead of trying the raster mesh.
- Changed the AC network build to reuse the ground net's hybrid mesh
  across a campaign's profiles and grid checks instead of meshing GND
  again for each: P02 Rev E's /+5V_RAIL network builds in 2.2 s instead
  of 9.6 s once GND is cached, to the same network.
- Fixed a campaign's AC tab staying empty: the batch handler unpacked
  the multi-profile outcome as a (result, optimization) pair and failed
  ("argument after * must be an iterable, not ACBatchOutcome"). It now
  publishes every swept profile, one impedance plot each.
- Fixed EMC scoring the AC verdict a second time: PD-001 restated each
  failing AC-001 as a HIGH finding, which took P02 Rev E's EMC score from
  49 to 25 and put three copies of the AC verdict at the top of the
  actions. It is INFO now, as TE-001 is for the thermal hotspot.
- Changed the hybrid AC networks to be meshed by the AC engine in the
  background instead of on the UI thread, where P02 Rev E's /VBUS_SYS
  took 16 s before the campaign could start; a profile that fails to
  build there still skips only itself.
- Fixed EMC SU-001/SU-002 calling a power plane a signal layer because a
  rail is routed on it: P02 Rev E's /J2_VBUS_FUSED runs 11.4 and 46.4 mm
  on PWR.L3 and PWR.L4, and "Adjacent signal layers" (HIGH) was the
  campaign's first action. Rail and ground tracks no longer count.
- Changed the DC verdict to judge a load's drop from its regulator's set
  point, pass-element chain included, and DC-006 to INFO below the
  budget: P02 Rev E showed 8 MEDIUM actions for chain drops of 0.5 to
  11.7 mV; /+5V_RAIL's worst load now reads 31.0 mV (0.61 %) in all.
- Fixed Phase 10 outcomes reported twice and as actions: P10-001
  repeated the EMC analysis's own PH-001/PH-002, and P10-002 put P02
  Rev E's three unsolved regions in the action list as LOW. Both are now
  INFO findings of the EMC analysis (PH-003 unusable model, PH-004
  failed region or excitation), as PH-001/PH-002 already were.
- Fixed the differential pairs losing the board file's dielectric er
  again: the differential panel read its stackup without the board path,
  so P02 Rev E's pairs fell back to er 4.4, read 81.4 ohm with 0 %
  qualified length. The EMC capture, thermal model and AC stackup now
  pass the board file too.
- Added an AC grid check: with "Check Grid at Half Step", each hybrid AC
  sweep solves its worst frequency again at half its mesh step, in the
  background, and flags a move over 10% (AC-005). P02 Rev E's /VBUS_SYS
  reads 248.7 mohm at 0.5 mm and 282.2 at 0.25 mm, 13.5% apart.
- Changed the electro-thermal coupling to map electrical branches to
  thermal cells as arrays: P02 Rev E's 2,641,865 branches, the ground
  return included, map in 1.5 s instead of 12.1 s, to the same cells.
- Fixed a campaign stalling while the DC maps render: they held the
  matplotlib lock over all 227 figures of P02 Rev E's 17 rails, and the
  differential analysis waited 53 s to draw its two. The DC and thermal
  renders now take the lock one figure at a time.
- Fixed a campaign dropping AC entirely when the rail on screen could
  not be built: P02 Rev E's /VBUS_SYS meshed to 102,401 hybrid nodes,
  over the 100,000 limit. Each profile now stands alone, and the limit
  is 400,000 nodes (5.1 s a point and 1.15 GB measured at 368,315).
- Changed AC capacitor models to take ESR and ESL from "ESR"/"ESL"
  fields on the part when the design states them, and otherwise to name
  the part's MPN in the model source: P02 Rev E's 75 capacitors with an
  MPN (C57: CL32A476KOJNNNE) state no ESR/ESL and stay case-size ones.
- Changed the thermal analysis to take its enclosure air rise from the
  CFD's own formula instead of a copy of it (P02 Rev E: 16.53 K).
- Added the DC ground return: the hybrid engine solves the main ground
  net with every load's current returning to the top-level sources, and
  judges each load on its supply drop plus its ground rise less its
  source's. On P02 Rev E, /+5V_RAIL's worst load (J6) reads 23.13 mV
  instead of 21.62, and the ground's 18.8 mW now heat the thermal model.
- Changed the 2-D differential field solvers to draw a trace as a
  trapezoid (etch factor 2.75, the top 25 um narrower at 1 oz) instead
  of a rectangle. P02 Rev E's USB pairs (0.200 / 0.250 mm on B.Cu) read
  85.32 ohm instead of 83.25; 90 ohm now needs about 0.180 mm.
- Fixed Phase 10 reaching the report only as status strings: a SPICE
  model it could not use (P02 Rev E's U5, TPS562200DDCT, unstable in
  ngspice) is a P10-001 finding and a failed or unsolved region or
  excitation a P10-002 one, both calculation quality and kept out of
  the score; a solved region's peak |E| and |H| are metrics.
- Fixed the DC hotspots and the thermal layer maps naming a copper layer
  by the name IPC reports ("F.Signal" on P02 Rev E) while the campaign
  named it by KiCad's own ("F.Cu"): the stackup now keeps both, and both
  places write "F.Signal (F.Cu)", as the HTML report does (COH-12).
- Fixed the CFD solid-air film (CFD-6): each face now takes its own
  obstacle's plate length instead of the mean cell size, reads the air
  speed outside its boundary layer instead of in the adjacent cell, and,
  with no fan or inlet, takes the larger of the natural and forced
  coefficients instead of blending them. P02 Rev E: Q10 181.0 C, board
  mean 134.3 C (179.8 and 132.3 C before).
- Fixed the CFD board conducting as a 0.6 W/mK solid, its copper ignored
  and its thickness taken as the cell's: it now has its copper layers'
  in-plane conductivity (P02 Rev E: 31.5 W/mK), scaled by its thickness
  over the cell's when snapped into one. P02's hottest solid cell (Q10)
  falls from 293.7 C to 179.8 C and the air maximum from 167.6 to 127.4 C.
- Fixed via barrels having no mutual inductance with the return barrel
  beside them (AC-8): a barrel with one of the other net within 5 mm now
  carries half the two-wire loop, mu0*h/pi ln(s/r). On P02 Rev E's
  /VBUS_SYS, 640 of 1,735 barrel branches pair up (103.8 nH to 67.4 nH)
  and the worst case moves from 252.8 to 247.9 mohm.
- Fixed a campaign sweeping only the AC profile on screen: it now sweeps
  every configured profile and reports them in one AC result, the worst
  verdict kept and a profile it could not build named (AC-004). P02
  Rev E's 3 profiles take 179 s; all three miss their target from 38,
  8 and 16 MHz (/+5V_RAIL, /+5V_U4, /VBUS_SYS).
- Changed the AC network to be built on the hybrid mesh of the saved
  board file when it matches the open board, as the DC report is: tracks
  and barrels at their analytic resistance, pours a cut-cell lattice
  whose edge nodes carry only the cavity their copper covers. On P02
  Rev E's /VBUS_SYS the worst case reads 252.8 mohm at 100 MHz against
  271.8 on the raster mesh. The raster mesh remains the fallback.
- Fixed the AC model losing all plane inductance and cavity capacitance
  when the return net has copper on the rail's own layer: P02 Rev E's GND
  via lands on In2.Cu put /VBUS_SYS's return 0 mm away. Each node now
  takes the nearest other layer with return copper beneath it. On P02
  (file stackup) the worst case goes from 81.6 mohm (PASS) to 271.8 mohm
  at 100 MHz (FAIL, target 88.9 mohm), crossed from 14.7 MHz.
- Changed the decoupling optimizer to evaluate its trials by a Woodbury
  update of one block solve per frequency instead of a full network
  sweep per footprint, value and round. On a 28,800-node two-plane rail
  with 6 DNP footprints, 4 values and 3 rounds, the trials fell from
  about 118 s to 11 s, with the same picks and a 6e-10 deviation from a
  full sweep.
- Added plane-pair (cavity) capacitance to the AC model: each rail cell
  gets C = eps0 er A / h to the return copper on the directly adjacent
  layer, stamped as a vectorised admittance next to the R + jwL
  branches. It was missing entirely, and stated as missing since the
  2026-09-04 audit.
- Fixed thermal cells being all copper or all laminate by their centre:
  a trace narrower than a cell was a full-cell strip or nothing, and a
  pour edge moved by up to half a cell. Cells on a copper edge now carry
  the exact copper fraction of their area, mixed in parallel with the
  laminate; P02 Rev E's In1.Cu reads 5021.2 mm2 of copper, its true area.
- Fixed thermal actions never carrying a remediation: the thermal-via
  advisor was imported by no production code. A junction over its limit
  now gets a sized ADD_THERMAL_VIAS remediation (1-D barrel sizing,
  marked unverified) and names its component.
- Fixed the thermal mesh cache ignoring a via's drill: the board
  signature that decides whether the cached mesh is reused left out
  drill_diameter, which sets the barrel's thermal conductance since vias
  are sized from their drill.
- Fixed the thermal model spreading every small part's heat over at least
  3 x 3 mm: an estimated component now uses its pads' extent plus
  0.5 mm, so an 0402 or SOT-23 is as hot as its footprint; a size the
  user entered is still honoured as a minimum.
- Fixed a cached thermal mesh carrying one coupled run's hot component
  powers into the next: each run now restores the models' own power
  before stripping the stage estimate, instead of drifting junctions by
  the hot-cold difference (under 0.5 K on P02 Rev E).
- Fixed an uncoupled thermal run taking copper losses at the DC's 20 C
  resistivity whatever the ambient: at P02 Rev E's 45 C they were about
  10 % low. They are now scaled by the copper's resistance ratio at the
  ambient, and the log says by how much.
- Fixed the thermal energy balance being blind to heat that never reached
  the mesh: it compared deposited heat with what left, so an unplaced
  component or a copper loss off the copper vanished silently. The model's
  intended power is now tallied, and any shortfall is finding TH-005.
- Changed EMC to say what its sources rest on: switching findings quote
  where the source's parameters came from, and the limitations name every
  source still on the assumed 0.1 A current, which its magnetic field and
  drive index scale with.
- Changed EMC to recognise TVS and ESD protection from the schematic's
  symbols (KiCad's Power_Protection library, D_TVS) before the value
  pattern it relied on alone; the pattern still covers parts from private
  libraries.
- Fixed the near-field E estimate charging every trace through one
  nominal 100 pF/m: each routed element now uses its microstrip
  capacitance over its reference plane (Hammerstad-Jensen), ~117 pF/m for
  P02 Rev E's 0.2 mm USB traces and several times that for switch-node
  strips; 100 pF/m remains only where no plane or width is known.
- Fixed three EMC measurements: the switch-node area now takes the union
  of tracks and pours (P02 Rev E's /J2_SRC_SW read 41.8 mm2 for 31.8, a
  track on its own pour counted twice), ESD distance is pin to pin on the
  protected lines (J1 -> U2: 15.5 mm, not 17.3 centre to centre), and each
  threshold states its basis and that it is the tool's heuristic.
- Fixed the EMC virtual receiver presenting relative levels as field
  strengths: its points carried level_dbuv_m under a QUASI_PEAK detector
  although they are a relative peak with no quasi-peak weighting and no
  far field. They are now level_rel_db, PEAK_RELATIVE (or
  AVERAGE_RELATIVE); the score reads "relative, uncalibrated" and the
  standard "reference (not evaluated)" in the reports.
- Fixed the EMC domain scoring P02 Rev E's 114.7 C hotspot a second time
  (TE-001 cost 4 points the thermal domain already charges; it is now
  informational), and replaced two stale limitations with what the
  analyzer does: component roles from the schematic, unknown frequencies
  excluded. The score and the near-field maps are now stated to be
  relative, uncalibrated rankings, and the standard a reference only.
- Fixed the EMC wire-to-board connector pattern never matching JST or DF
  connectors: it held literal backspace characters where word boundaries
  were meant, so such connectors were taken for user-facing ports. It
  now matches JST_PH, JST_XH and DF13 as whole words.
- Changed the DC result to state two limits it carried silently since
  the 2026-09-13 audit: the drop is the supply side only (the ground
  return is not solved), and via barrels assume 20 um of plating because
  the board file does not record it.
- Changed the hybrid DC mesher to cut each layer's copper to the row band
  before testing cells against it, and to mint lattice nodes by index:
  P02 Rev E's /VBUS_SYS meshes in 13.5 s instead of 29.2 s, /+3V3AO in
  5.0 s instead of 14.5 s, with the same nodes and branches (resistances
  equal to 3e-10).
- Fixed via and plated-hole barrels in the DC mesh being the whole board
  thickness, solder masks included, split equally between layer hops: on
  P02 Rev E, F.Cu -> In1 was modelled 0.232 mm for 0.125 mm (x1.86) and
  In1 -> In2 0.232 mm for 0.366 mm (x0.63). Each hop now spans the copper
  and dielectric between its two layers' centres, from the stackup.
- Fixed a DC rail drawing no current reading "0.0 % PASS" (P02 Rev E's
  /J2_LDO3V3, /+3V3AON, /J2_PP5V): it is now NO_DATA. A configured load or
  regulator input whose pads find no copper, which the hybrid engine only
  logged, is now finding DC-007 naming the part and the current left out.
- Fixed the source-free load finding counting mesh nodes: P02 Rev E's
  "/VBUS_SYS: 81 load node(s) were excluded" was one load, U14's input,
  drawing 0.848 A of the rail's 2.04 A. It now names the excluded loads,
  their current and their share of the rail, and lists them as the
  finding's components so other findings on U14 group with it.
- Removed transmission_line_abcd.py, an ABCD transmission-line module no
  production code imported: three audits (2026-09-04, -13, -23) found it
  unwired, and its nine tests exercised only themselves.
- Fixed AUTO differential geometry ignoring a same-layer ground pour
  running alongside the pair: it solved plain microstrip although P02
  Rev E's USB pairs have GND 0.2 mm away along ~90 % of their length.
  Coverage of 90 % or more now selects the coplanar cross-section.
- Fixed identical differential pairs getting different verdicts: one
  partly referenced connector escape turned a whole pair into an
  "estimate" (P02 Rev E: two USB pairs FAIL/HIGH, three ESTIMATE/MEDIUM on
  the same geometry). A pair is judged on its length with a qualified
  reference (>= 90 %, published per pair); each failing pair now carries
  its P/N nets, and the verdict tolerance is published with the results.
- Fixed pair discovery taking power and LED nets for differential pairs:
  KiCad's +/-/P/N suffix rule made BAT+/BAT-, VREFP/VREFN or SPK_P/N
  "likely" 100-ohm pairs, and LED_DP/LED_DM became USB because "DP"
  occurs in the name. A suffix alone on no identified interface is now
  "suspected" and not analysed until confirmed; interface tokens match
  whole words. P02 Rev E's five USB pairs are unchanged.
- Fixed crosstalk NEXT below saturation carrying a factor 2: it grew as
  2L/L_sat instead of L/L_sat (Bogatin), so XT-001 read 0.50 Kb where the
  coupling is 0.25 Kb at a quarter of the saturation length, the usual
  case (L_sat is 79 mm at a 1 ns edge).
- Removed the "targeted 3-D quasi-static refinement" of differential
  pairs: it was an empirical Z/(1+c) factor, applied at every bend as if
  the line ended there and only to sections already 10 % off target, and
  it pushed them further off (P02 Rev E: 79.16 -> 76.48 ohm). The 2-D
  cross-section is the published impedance; its three settings are gone.
- Fixed the differential recommender inverting the IPC-D-317A closed form
  instead of the 2-D field solve that measured the pair: on P02 Rev E it
  promised 90.6 ohm at 0.155 mm, which the field solve puts at 94.3 ohm
  (an independent reference, 97.1). It now re-solves the section's own
  cross-section, mask included; 90 ohm lands near 0.171 mm.
- Fixed the differential solver ignoring the board's solder mask: IPC's
  stackup carries no mask, so it modelled 0.02 mm / er 3.3 over P02 Rev E's
  USB pairs whose file states 0.01 mm, and it also raised the substrate er
  for a mask the 2-D solve already models (-0.7 % each). The mask is read
  from the board file and applied once.
- Fixed the 2-D differential field solver taking 5-10 s per cross-section
  after its grid was refined to h/20 for accuracy: red-black SOR needed up
  to 20,000 sweeps and the test suite went from 31 s to 488 s. It now
  solves the same finite-volume system directly with a sparse LU: P02
  Rev E's USB pair gives the same 83.2478 ohm in 0.22 s instead of 9.5 s.
- Fixed the convection correlation's stated Rayleigh/Reynolds range
  never being checked (surface_convection.py's *_in_range functions
  had 0 callers). P02 Rev E's Ra ~ 8.0e3 is below the 1e4 laminar
  lower bound; out-of-range faces are now named in a limitation
  instead of publishing the coefficient as if it were validated.
- Fixed Phase 10 printing a solving progress line in export-only mode,
  reporting plain COMPLETED while regions were GEOMETRY_EXPORTED or
  spice_model_audit held MODEL_FILE_MISSING/TRANSIENT_UNSTABLE entries
  (P02 Rev E: U4/U14 and U5), and labelling an ideal-pulse ngspice run
  PARAMETRIC_NGSPICE. Added PH-001/PH-002 findings for a missing or
  unstable SPICE model (old audit item A), verified with a lean test
  reproducing P02 Rev E's exact U4/U5 audit statuses.
- Fixed DC-3: a pass element (fuse, eFuse, shunt, switch, net-tie) now
  propagates its input pad's real solved voltage (minus a declarable
  r_pass_ohm, default 0) to its output rail instead of resetting to
  nominal, and a rail's cumulative chain drop is published as DC-006.
  P02 Rev E (2026-09-23): /J2_VBUS_PORT (U12 -> U7 -> F2) now reads
  22.79 mV of chain drop, previously invisible; /+5V_RAIL (R49 -> U15)
  reads 22.74 mV.
- Fixed the 2-D differential/coplanar field solver reading Zdiff 5-9% low
  (P02 Rev E USB pair: reference 85.36 ohm, tool 76.86 -> 83.25 ohm) by
  giving an exposed pair's copper-thickness band air/mask instead of
  substrate, scaling the mirror boundary margin to the trace gap, and
  tightening the grid to h/20. Full test suite runtime rises to ~495s
  as a result; solve_potential_sor now warns instead of silently
  returning an under-converged potential.
- Added SW-003, a switching hot-loop spread rule, and sized Phase 10's
  SWITCHING regions on the net's own copper plus every hot-loop
  footprint instead of one evidence point +/- a fixed margin: P02 Rev
  E's region_1 held L1 but neither Q10 nor Q11 (20.1 mm apart, now
  flagged HIGH by SW-003 at today's 24.7 mm Q10-R57 spread). Fixed
  SW-002's recommendation to name FET-to-FET/FET-to-inductor copper
  for an external-FET stage instead of always saying IC-to-inductor.
- Fixed the near-field solver exciting a switching trace's H-field
  with source.current_a/n (a square-wave current) instead of the
  linked inductor's own triangular ripple: P02 Rev E's U4 read 58 mA
  on its 75th harmonic against 3.0e-5 A for the same harmonic of the
  same inductor's own dipole model, 66 dB apart. The trace and the
  inductor now share inductor_em.triangular_harmonic_peak(); U4's own
  field dropped from 100% to 14.3% of the map's peak as a result.
- Fixed buck MOSFETs receiving the full inductor RMS current with no
  duty-cycle weighting (power_loss.py). A declared role ("high"/"low")
  now weights conduction loss by D or 1-D; undeclared keeps the old
  behaviour (P02 Rev E's sidecar pre-multiplies RDS(on) by D=0.254
  for exactly this reason) but now warns that a datasheet value would
  overstate loss by up to 1/D.
- Fixed efficiency-closed stages dumping their entire residual on one
  component and holding total loss temperature-independent
  (power_loss.py). residual_split now distributes the residual across
  named components (documented, default unchanged: 100% on
  controller_ref_des); residual_temperature_multiplier_table lets the
  target itself grow with the coupled solve's temperature instead of
  only reshuffling a fixed total.
- Fixed component board temperature averaging every cell under the
  part instead of taking the hottest one (thermal_solver.py,
  electrothermal.py). P02 Rev E's Q10 read Tj 110.64 C under its own
  114.71 C board hotspot; the board-side term now uses the part's
  hottest mapped cell.
- Fixed thermal ignoring the enclosure the CFD domain models
  (application/thermal_controller.py, models.py). P02 Rev E's sealed
  165x100x50 mm box (5 W/m2K walls) raises internal air ~16.5 K above
  the outside ambient at ~4.9 W; thermal now solves at that internal
  air when an enclosure profile is handed to the run, publishing it
  as an "internal air <T> C" metric, and a limitation when the
  correction is configured but disabled.
- Documented (limite publiee): plane-pair cavity capacitance
  (C = eps0*er*A/d) is not modelled anywhere in the AC network. Stated
  on the AC report and at ac_solver.py's _stamps, with the reason a
  per-cell or lumped version was not attempted in this fix.
- Added coverage for Mesher._add_vertical_stack (PTH pad barrels),
  previously untested (audit-2026-09-23.md section 11).
- Fixed: tests/test_ac_controller.py now inserts the repo root on
  sys.path before importing application.ac_controller, matching every
  other test file, so it no longer depends on the test runner adding
  the current directory itself.
- Fixed AC-10: a campaign's AC step now states how many of the
  project's configured AC profiles it actually swept (finding AC-004)
  and no longer scores 100/100 on partial coverage. P02 Rev E: 3
  profiles configured, 1 swept per campaign run.
- Moved ViaLossesGetAnAction and PlaneCopperLossesGetAnAction from
  tests/test_cfd_validation_fixes.py to tests/test_dc_advisor.py; they
  test the DC advisor, not CFD.
- Added validation/cfd_benchmarks.py's first energy/buoyancy case (a
  closed heated cavity) with real assertions -- convergence, energy
  balance, and C/K origin invariance -- where every existing case only
  printed for manual review.
- Removed CFDSolverSettings.pressure_iterations and its UI control: inert
  since the pressure Poisson system moved to a direct sparse solve
  (c4e0ead). A project saved with the key still loads; it is just no
  longer read into anything.
- Fixed the CFD solid-air film always rating a face as upward-facing
  (0.54 vs the 0.27 a downward face gets), added a real (default-off)
  emissivity setting so radiation in the film is configurable instead of
  dead code, and documented the film length/velocity/double-counting
  limitations that remain.
- Fixed CFD's explicit advection missing its own stability bound
  (dt <= 2*nu/u^2, 3.8x violated on P02 Rev E) and its Boussinesq beta
  using a 25 C constant regardless of actual ambient (6.7% low at 45 C);
  added a limitation when beta*dT exceeds the usual ~0.1 validity bound.
- Fixed the campaign thermal report naming no component, no hottest
  junction and no loss provenance (analysis_adapters.py). P02 Rev E:
  the 114.71 C hotspot now names Q10 as the component underneath it,
  U14 (125.0 C, 25 K margin) as the hottest junction, and the 1.365 W
  efficiency-fallback residual (28% of 4.9186 W) placed on Q10. The
  hotspot metric no longer copies the domain PASS/WARN/FAIL onto
  itself; coupled runs now publish COPPER_LOSSES provenance too.
- Fixed the mesh-resolution error bar (double-step hotspot check)
  being silently skipped whenever the thermal run was coupled, which
  is every campaign; the P02 Rev E sidecar's
  mesh_sensitivity_check=true never took effect on a campaign report
  before this. A frozen re-solve that cannot complete now publishes
  "not verified" instead of nothing.
- Fixed the coupled thermal path never refining surface h against the
  solved temperature (electrothermal.py, thermal_solver.py): it now
  runs the same correction the uncoupled path does, and the report's
  "refined" wording is conditional on that having happened
  (ThermalResult.surface_refined). P02 Rev E's published h_top
  16.44 / h_bottom 12.00 W/m2K was the untouched nominal guess.
- Fixed thermal via modelling reading kipy's nonexistent Via.width
  (defaulted all 685 P02 Rev E vias to 0.6 mm vs real 0.3/0.4 mm
  drills) and via barrels bypassing inner copper planes. Synthetic
  9-via/1.5 W board: 84.6 C -> 77.3 C hotspot once drills are read
  correctly and chained through every layer.
- Fixed SW-001, EE-002 and the near-field solver each modelling a
  switching/clock source's harmonic rolloff differently: P02 Rev E's
  U4 (400 kHz, tr 10 ns) read -44.1 dB from SW-001 and -94.8 dB from
  the near-field solver at operating points where Paul's trapezoid
  bound (which EE-002 already used correctly) gives -37.5 dB and
  -77.0 dB. All three now call inductor_em.trapezoid_envelope_db(),
  one function, instead of three disagreeing ones.
- Fixed the near-field solver applying the image method across a
  reference plane that sits between the source and the observer
  instead of shielding it: P02 Rev E's /MAIN_3V3_SW (U5), on B.Cu
  under GND.L2/GND.L5, went from 46% of the map's peak H (0.133 A/m,
  mirrored through 2 planes it should not pass) to 19.8% (0.043 A/m),
  computed from its own side now that em_field_solver.py solves both
  the top and bottom observation planes and reports the worse.
- Fixed AC-9 (partial, correctness half): the decoupling optimizer now
  scores baseline/trials/result with solve_sweep_multiport, matching
  the worst-port verdict AC-4 made the published AC result use,
  instead of a single fixed measurement port that could disagree with
  it. The per-trial full-resolve performance cost (no LU reuse across
  capacitor configurations) is unchanged and stated as a known gap.
- Documented AC-8 (limite publiee): via inductance
  (mesh.Mesher._calculate_vertical_l) is a self inductance with no
  mutual term against a nearby return via, overestimating a close
  decoupling via pair's loop inductance by about 2.6x (1 mm-apart
  pair, 1 mm dielectric span: 2 x 1.0 nH modelled vs about 0.76 nH by
  the two-wire loop formula). Stated on the AC report rather than
  corrected, since via-pair spacing is not available at this call site.
- Fixed AC-7: DC-bias derating and ESR now depend on EIA case size
  (documented tables) instead of one generic curve and a fixed 10 mOhm;
  a 22uF/35V X5R at 20 V derates further and carries higher ESR in
  0603 than in 1210. Rediscovering a rail's capacitors no longer lets
  a stale estimate shadow a corrected one -- only an explicit user
  edit (model_source == "user") survives rediscovery.
- Fixed AC-6: the AC report's limitations no longer say DC-bias
  derating "is not modelled" while the same report shows it applied
  (P02 Rev E: "DC bias X5R 35 V at 20 V: 22uF -> 11.4uF"). The text now
  names the real gaps -- a generic retention curve (AC-7) and
  mounting inductance with no mutual term (AC-8).
- Fixed AC-2: the AC source model is now a documented, named preset
  choice (default/USB-C cable/regulator output) in the UI, always
  stated as a dominant assumption in the report's limitations, and the
  verdict drops from ESTIMATED to HEURISTIC confidence while the
  default is unverified. P02 Rev E: default source hides the
  source/bulk anti-resonance (max|Z| 12 mOhm below 10 MHz); a USB-C
  cable preset shows 362 mOhm, 92% of the 0.39226 ohm target, at 21 kHz.
- Fixed DC-2: the DC current-density maximum and hotspot no longer pick
  a near-empty cut cell carrying a trickle of current. P02 Rev E
  /J2_PD_HV's published 697 A/mm2 maximum (0.7% copper, 4 mA, 4.3 mm
  from any pad) is now excluded by a 25% copper-fraction floor; the
  first physical hotspot was 393 A/mm2. Verified on /+5V_U4
  (2026-09-23): 5,062 of 102,361 samples excluded, reported max
  228.3 A/mm2 vs. 442.3 A/mm2 for the excluded slivers.
- Fixed DC-1: a track end, via barrel or through-hole pad barrel now
  joins every plane/land lattice node under its own footprint instead
  of the single nearest one, removing a fictitious constriction
  resistance that grew with grid refinement. P02 Rev E /+5V_U4
  (2026-09-23): worst-load drop growth 0.1->0.035 mm was +4.9% before
  this fix (24.2->25.7 mV) and is now -4.4% (21.3->20.3 mV, no longer
  growing). DC-005's recommendation text updated to match.
- Fixed the CFD report publishing unusable solid/air maxima as its headline
  numbers while never publishing the one defensible quantity in a sealed
  enclosure, the mean internal air rise (16.0 K on P02 Rev E, 4.755 W into
  a 0.0595 m2 enclosure at 5 W/m2K). Added cfd_model.enclosure_air_rise_k
  for thermal's internal ambient to share.
- Fixed CFD mesh-resolution and solid/air-maxima-trust findings (CFD-003/
  004) scoring and ranking like design defects; they are now limitations,
  and CFD-004's text no longer blames a nonexistent "unresolved buoyant
  plume" for what CFD-1/CFD-2 actually caused.
- Fixed the campaign report's metric values to use significant-digit
  formatting instead of Python's raw float repr, collapsing
  floating-point noise under 1e-6 to "< 1e-6" (P02 Rev E printed
  "8.074349270001136e-14 %" and "1.03e-09 A/mm2").
- Fixed the report to show a Remediation's layer as "<given name>
  (<canonical id>)" so a user stackup name (F.Signal) and the
  canonical KiCad id (F.Cu) for the same layer read as one layer, not
  two.
- Fixed the consolidated report to carry each domain's completion time
  and board fingerprint, and to warn when domains in the same report
  do not share a fingerprint; standalone (non-batch) results are now
  stamped with one, where before only a batch run was.
- Fixed power_loss.estimate_stage to keep Iin = Iout for a series
  element (shunt/fuse/eFuse/net-tie) instead of inflating it through
  the general Pin/Vin formula; P02 Rev E's /SYS_5V_PRE_SENSE, /VBUS_SYS
  and /J1_VBUS_RAW read 1.9-2.9% high in thermal's own power tree
  versus DC's.
- Fixed EMC scoring and DC-004 diffing an empty live capture (KiCad's
  IPC mid-sync) as real board data: P02 Rev E, 2026-09-22 21:39,
  scored empty geometry 62/100 with a false GP-002 CRITICAL and gave
  DC-004 17 false "100% differs" findings. Both now recognise an
  empty capture (no tracks, vias, zones or net pads) via a shared
  extractor.live_capture_has_no_copper() guard: EMC refuses to score
  it and the campaign skips the domain like it already does for
  thermal; DC-004 reports "live capture empty, retry after sync"
  instead of a fabricated 100% diff.
- Fixed AC-4: every child regulator's input pin is now swept as a
  candidate AC port, not just PowerRail.loads. P02 Rev E's /VBUS_SYS
  feeds U4 and U14 with no load of its own; U14's input (pads 10-12)
  was never observed before, though the target already counts its
  current.
- Fixed AC-5: the AC target for a bus feeding switching converters is
  now derived from the largest fed converter's own output current
  (Iout), not 50% of the rail's time-averaged input current. P02 Rev E
  /VBUS_SYS: target moves from 0.39226 ohm (1.02 A assumed step) to
  0.0917 ohm (U4's 4.36 A Iout step); against the worst |Z| of 0.2947
  ohm (AC-1) the verdict moves from PASS to FAIL.
- Fixed AC-1: a PASS verdict no longer stands when the worst |Z| sits
  on the closed sweep edge, still rising, with its extrapolated target
  crossing inside the quasi-static limit. P02 Rev E /VBUS_SYS: 0.2947
  ohm at 100 MHz vs 0.39226 ohm target crosses near 133 MHz, under the
  ~140 MHz limit -- now WARN (AC-003) instead of PASS.
- Fixed AC-3: the AC solver's plane-pair loop inductance was counted
  twice (mu0*d given whole to both the rail and ground mesh instead of
  split). scratchpad/audit/ac/ac_checks.py (A): 0.999 nH modelled vs
  0.500 nH analytic (d=0.0994 mm, l/w=4) before the fix; now split by
  distance to the nearest layer carrying the return net.
- Fixed CFD's AUTO backend selection obeying a CUDA threshold tuned for
  other domains' larger, less frequent solves; CFD now raises it to
  100,000 cells for its own solve unless CUDA was explicitly forced. On
  P02 Rev E with the user's saved runtime.json this moved the 6,600-cell
  solve from CUDA (324 ms/it) to CPU_PARDISO (22 ms/it, 490 it, 10.8 s,
  converged).
- Fixed the CFD free-stream sampler picking its normal axis from the PCB
  mask's own (possibly degenerate) extent instead of the board's actual
  orientation, which on P02 Rev E sampled a line in the board's own plane
  (7 cells at 0.0117 m/s) instead of the air above and below it.
- Fixed non-conservative CFD energy advection: the energy equation now
  advects with the pressure projection's own divergence-free face
  velocities instead of re-averaging cell velocities, which had let a
  21.5 W spurious source (for 4.75 W injected) leak into P02 Rev E's
  solid/air temperatures and made the result depend on the Celsius/Kelvin
  origin (404.7 C vs 1005.9 C for the same physics). Added an origin-
  invariance regression test.
- Fixed a CFD enclosure mesh bug where a board or component thinner than
  one cell on every axis collapsed to a single fallback cell instead of
  spanning its footprint on the axes where it is not thin. P02 Rev E's
  125 x 45 x 1.29 mm board (z 15.0-16.29 mm, cell centres at 12.5/17.5 mm)
  went from one 5 mm cell with no conduction path to its 20 components to
  a full sheet of cells across its footprint. A residual single-cell
  fallback (an obstacle too small on every axis) is now reported as a CFD
  limitation instead of staying silent.
- Fixed dielectric er silently falling back to 4.4 "FR4" when KiCad IPC
  gives no BoardStackupDielectricLayer sub-layers (P02 Rev E,
  20260922-2156: IPC reported 4.4 on all 5 dielectrics vs the board
  file's 4.1/4.6/4.16/4.6/4.1). The stackup now falls back to the saved
  .kicad_pcb file, or is marked untrustworthy with a named-layer warning
  when no file is available.
- Fixed the campaign score to exclude calculation_quality findings
  (CFD-003/004, DC-004/005 on P02 Rev E) instead of scoring a tool
  limitation like a board defect.
- Fixed the action list to exclude INFO findings entirely (8 on P02
  Rev E, e.g. "PDN remains within target"); they remain listed in
  their own domain's section.
- Fixed the campaign to propagate an INCOMPLETE DC rail (DC-001) as a
  stated limitation to every other domain reporting on the same net,
  and to group a DC-001 finding with anything else on that net --
  P02 Rev E's /VBUS_SYS (U14, 81 load nodes excluded) no longer reads
  as a clean PASS in AC/EMC while DC calls the same rail incomplete.
- Fixed the campaign score's per-rule cap to charge every distinct net
  a per-net rule fires on (P02 Rev E: 5 failed differential pairs score
  61.0, not 76.6), while a component-only rule keeps the old cap.
- Fixed before/after campaign comparison to match actions by count per
  identity instead of a dict overwrite, so fixing 4 of 5
  identity-colliding SI-DIFF-001 pairs reports resolved 4, not 0.
- Fixed the campaign report to show a finding's recommendation text
  when no structured Remediation exists, instead of a blanket "No
  structured remediation was computed" on all 26 of P02 Rev E's
  actions; also fixed the action gain_rank tie-break (was domain
  iteration order) to use confidence, then title.
- Fixed the campaign verdict to derive each domain's PASS/WARN/FAIL
  from its findings' severity and confidence, one rule for every
  domain, instead of trusting each adapter's own hard-coded status;
  P02 Rev E's FAIL rested only on two HIGH ESTIMATED SI-DIFF-001
  findings that now WARN like an equivalent DC-001 finding did.
- Fixed the campaign cache so a cache-served domain still hands its
  raw outcome to the domains coupled to it (DC -> THERMAL/CFD,
  AC/DIFFERENTIAL/THERMAL -> EMC), instead of silently dropping every
  handover on a cache hit.
- Fixed the enclosure CFD never finding a free-stream sample over a flat
  board. The sampling slab stopped at the board's own thickness along its
  normal, one layer of cells solid wherever the board is, so no mesh could
  offer one: P02 Rev E's report at 21:58 blamed a coarse mesh and showed
  0 m/s. The slab now spans the enclosure above and below the footprint,
  two cells clear of solids and of the no-slip walls; the same enclosure
  yields 16 cells.
- Fixed a buoyant campaign telling the user thermal "keeps its configured
  airflow" after CFD. On P02 Rev E at 21:58 the sealed enclosure ran CFD
  after THERMAL, as it must, so nothing could be handed over; the CFD speed
  is now offered to thermal only in forced flow, the one case run first.
- Fixed the DC rail maps taking 103 s after P02 Rev E's DC had finished at
  21:54, holding the plot lock and the GIL while AC and thermal ran. Each
  layer map drew every node as a marker (8.4 s for one layer of /VBUS_SYS,
  375,478 nodes); gridded nodes are now one image, as the current-density
  map already was. The 3D view shows at most 20,000 nodes, always with the
  lowest, and the maps use fixed margins: constrained layout drew every
  figure twice. The 227 maps of the same result take 48 s instead of 101 s.
- Changed a batch to share the DC domain's copper capture with thermal and
  CFD. On P02 Rev E at 21:52 the 17 rails were captured three times on
  KiCad's UI thread, about 3 s of frozen editor each, and the campaign then
  found the three equal and used only the first. A run whose rails, grid or
  board file differ still captures its own.
- Fixed every CPU sparse solve re-enumerating the loaded native libraries
  to cap their threads: 6.7 ms a call, 3.5 s over the 523 solves of P02 Rev
  E's enclosure CFD. One controller is now built on the first solve and
  reused, about 20 us a call; a CFD iteration drops from 24 ms to 17 ms.
- Fixed the enclosure CFD spending nine tenths of its run assembling the
  energy equation one cell and face at a time into a LIL matrix. On P02
  Rev E at 21:56 the 33 x 20 x 10 mesh took 129 s for 435 iterations,
  the longest domain of the campaign; each assembly cost 0.34-0.67 s and ran
  every fifth iteration. It is now built with whole-array operations in
  about 3 ms, to the same operator within 1e-16, and an iteration of the
  same enclosure drops from 0.16 s to 0.024 s.
- Fixed the coupled thermal loop iterating on its own relaxation. On P02
  Rev E at 21:56 the hotspot held at 114.68-114.71 C from the first pass,
  yet the deltas ran 69.68, 27.89, 11.17 ... 0.046 C, exactly 0.4^k of the
  first rise over the 45 C ambient: 9 passes and about 18 s spent watching
  the 0.6 relaxation close a gap the coupling had already closed. The first
  solution now replaces the ambient guess whole, and convergence is judged
  on how far the solved field moves between passes, scaled by the
  relaxation, rather than on its distance to the relaxed input.
- Fixed a thermal run solving a board KiCad had not sent. While the PCB
  editor synchronises, IPC answers every collection empty: on P02 Rev E at
  21:35 the AC and differential domains refused, but the thermal model was
  built from the saved outline alone, and the coupled run reported a
  145.8 C hotspot produced by 0.43 W of copper loss it had nowhere to
  spread, with no component heat at all, which EMC then quoted as a fact.
  A board whose nets carry no copper on any layer is now refused, and the
  empty answer is no longer cached; the campaign skips the domain and says
  to retry after synchronisation, as the other two domains do.
- Changed a campaign's coupled thermal run to start from the DC result the
  campaign has just solved. It solved the same rails again from its own
  capture of the same board file: on P02 Rev E, 17 rails and 87 s of a 349 s
  campaign, for meshes the coupling only reads before re-solving them against
  the temperature field. The handover existed for an uncoupled run and
  skipped the coupled one; a capture that differs -- other rails, another
  grid or another board file -- still solves its own, and the log says which.
- Fixed the French catalog, where naive substring substitution had
  rewritten English words inside longer words: "Phase (deg)" became
  "Pae (deg)", "Use Phase 3 heat sources" became "Use Pae 3 heat
  sources", "Airflow mode:" became "Airffaible mode:", "copied to
  clipboard" became "copied to clipPCB", "CUDA driver/runtime" became
  "CUDA pilote/runtemps", "Testing" became "Testering", and the code
  identifier "[_get_mesh_nodes]" became "[_get_mesh_nœuds]".
- Fixed half-translated findings that mixed French and English in the
  UI, plots and reports, such as "Rail-to-ground impedance" rendered as
  "Rail-to-masse impedance", "Fragmented ground plane" as "Fragmented
  masse plan", "Low ground-plane coverage" as "Low masse-plan
  couverture", and "TVS has insufficient nearby ground vias" as "TVS a
  insufficient nearby masse vias".
- Retranslated 371 catalog entries into consistent French, preserving
  every placeholder and format spec, leading and trailing whitespace,
  units, log tags such as [EM FIELD], and code identifiers such as
  _get_mesh_nodes, layer_id and board.nets.
- Changed the AC sweep to reuse one sparse LU factorisation across
  frequencies, and AUTO to keep sweeps on the CPU. On P02 Rev E (85,337
  nodes, 121 points) AUTO sent the sweep to CUDA, whose Jacobi-preconditioned
  BiCGSTAB stopped at status=5000, and the fallback stamped and factorised
  the network afresh at every point: 733 s. The matrix is now stamped from
  index arrays, anchored symmetrically, and each point solved by GMRES
  preconditioned by the LU of an earlier one, refactorised once GMRES needs
  more than 12 iterations. On a 94,000-node network built like P02's the
  sweep takes 42 s with 4 factorisations for 121 points and agrees with a
  direct solve to 1e-10 where both ran; the GPU took 2.3 to 3.9 s a point.
  Forcing CUDA still sweeps on the GPU; cuda_min_nodes_sweep is retired.
- Changed the AC target of a rail that feeds converters to count their
  input current. The target followed from the rail's own loads only, so a
  rail that supplies converters and nothing else had none: P02 Rev E's
  /VBUS_SYS, which feeds U4 and U14, was swept with no verdict. The current
  now runs through the power tree, a switching regulator drawing its
  output current times Vout / (Vin x efficiency) and a linear one passing
  it through; /VBUS_SYS supplies 2.04 A and gets 0.392 ohm.
- Fixed a campaign solving two versions of the saved board. The hybrid DC
  engine read the .kicad_pcb when it ran, not when the run was captured:
  on P02 Rev E, saved at 15:37 during a campaign captured at 15:29 whose AC
  sweep took 12 minutes, the DC domain solved the earlier file and the
  coupled thermal's DC the later one. The saved file is now read with the
  live board, and every DC solve of the run parses that capture.
- Changed SI-DIFF-001 to state the pair's impedance against its target and
  to recommend the geometry the recommender solved for. It gave the 3-D
  refinement's notes alone, whose "2-D impedance error 14.6%" read as the
  model being 14.6 % off, and a generic "review width and gap": on P02 Rev E
  it now reads "Zdiff 76.7 ohm against a 90 ohm target (-14.7%)" and
  recommends 0.155 mm wide with a 0.250 mm gap on B.Signal, for 90.6 ohm.
- Fixed the AC tab dropping a saved measurement port that is not a load. It
  offered the rail's loads only and saved what it showed, so a rail that
  feeds converters lost its port: P02 Rev E's /VBUS_SYS has none but the
  converter inputs, its saved port Q10 was replaced by an empty one, and
  the campaign skipped AC again. The tab now offers the loads, the inputs
  of the regulators the rail feeds and the saved port.
- Fixed the AC tab staying on a rail the project never configured for AC.
  Loading a project's profiles left the tab on the first rail of the tree,
  and the campaign sweeps the rail the tab shows: on P02 Rev E, profiles
  saved for /VBUS_SYS, /+5V_U4 and /+5V_RAIL, the tab showed /J1_VBUS_RAW
  and the campaign's AC sweep was skipped, no measurement port resolving
  there. A loaded project now opens the tab on the first rail it
  configured, unless the one shown is configured already.
- Fixed the differential analysis failing on a thin outer dielectric. The
  recommender searched the width meeting the target up to six times the
  trace width, and IPC-2141's microstrip formula refuses a trace wider
  than about 7.5 times its height: on P02 Rev E, 0.0994 mm above ground,
  the search tried 1.2 mm and its ValueError failed the analysis after all
  five USB pairs were solved at 74-78 ohm. A width the model refuses now
  narrows the search; P02's pairs get 0.155 mm for 90 ohm.
- Fixed the EMC risk score counting one gap twice and an estimate as a
  fact. GP-001 measured one line of a differential pair against its plane,
  repeating DP-004, which judges the pair with its coplanar ground, a level
  higher: on P02 Rev E one 2.3 mm gap under USB_HOST_D drew a HIGH and a
  MEDIUM. GP-001 now leaves an analysed pair's nets to DP-004. SW-001 was
  of HIGH confidence whenever its source was entered by hand, though its
  harmonic level is an uncalibrated edge model; it is MEDIUM now. On P02's
  saved files the EMC rule fixes of this series take the score from 33 to
  65.
- Fixed a rail with loads left out of its DC solve reading "PASS". Loads
  on copper no source reaches are excluded, and the drop of those left was
  judged against the budget: on P02 Rev E /+3V3AO, every load excluded,
  read "0.0 % PASS". Its drop is now INCOMPLETE unless already over budget.
  DC-001 names the loads left out and, for the hybrid mesh, points at
  KiCad's unconnected items rather than a finer grid, which cannot join
  copper the saved board keeps apart.
- Fixed the EMC source scan dropping a saved frequency it could not find
  itself. Every scanned source is rebuilt on each scan, so P02 Rev E's
  /USB_J2_MUX_D pair, saved at 240 MHz, came back at 0 Hz, its interface
  saying only "USB", and EE-003 reported its frequency unknown. A rescanned
  source whose frequency the scan cannot tell keeps the saved one.
- Fixed the near-field map solving a source of unknown frequency at 0 Hz
  and full strength. EE-003 says such a source is left out, yet the "first
  in-band harmonic" mode gave it a weight of one: on P02 Rev E the USB_HOST
  pair, frequency unknown, made the map's strongest E field, 52 V/m, an
  electrostatic artefact. It is now left out, with a warning, until its
  frequency is entered.
- Fixed the connector ESD rules on cable headers and far ESD filters. A
  wire-to-board header whose value says USB was taken for a port: P02 Rev
  E's J8, a Pico-Clasp "USB HOST" header to the main board, drew ES-001,
  IO-001 (both HIGH) and SH-001, and now draws one LOW IO-002 saying it
  was taken as internal. ECMF, EMIF and RClamp parts count as ESD
  protection, and a protector found on a connector's lines but more than
  10 mm away is named in a MEDIUM ES-001 instead of reported missing: J1's
  lines pass U2, an ECMF02, 17 mm away.
- Fixed SU-001 and SU-002 calling power planes signal layers. Every copper
  layer without a ground pour counted as a signal layer, so P02 Rev E's
  PWR.L3 and PWR.L4, which hold its rails' pours and no track, drew a HIGH
  "adjacent signal layers" and two "signal layer far from reference". The
  stackup rules now look only at the layers that carry tracks.
- Fixed DE-001 asking for a decoupling capacitor beside a TVS. A part with
  a power_in pin needs decoupling, and TVS and diode symbols often type
  their ground pin power_in: on P02 Rev E the TVS2200 on VBUS_PD drew a
  HIGH "distant decoupling capacitor" for C1, 24 mm away. A diode, TVS or
  ESD part is no longer checked.
- Fixed a finding that surveys the design gluing unrelated findings into
  one campaign action. Findings sharing a part are one problem, and
  SCH-003 names every resistor it lists: on P02 Rev E its 49 parts included
  C11, which DE-001 named beside U18, and the pair became the first
  priority action, ranked above every HIGH for spanning two domains, with
  50 parts attached. A finding naming more than six parts no longer ties
  others to it through a part.
- Fixed SCH-003 taking ceramic capacitors for resistors. A value read as a
  resistance wherever a number met an R, so the dielectric code of
  "47u 16V X5R" made C56 a 5-ohm part: on P02 Rev E 22 of the 49
  "resistors" lacking a power rating were X5R and X7R capacitors. The
  number must now stand on its own.
- Fixed a component's heat going into the bare laminate between its pads.
  The whole placement window took the power, and a laminate cell, which
  cannot spread it, ran far hotter than the copper beside it: on P02 Rev E
  cells between U14's pads read 128-130 C over copper at 98 C, at 0.5, 1
  and 2 mm alike, and that was the board's hotspot, the one TE-001 quotes.
  Heat now goes into the window's copper cells, the window growing a cell a
  side, twice at most, while the grid samples fewer than four. The P02
  hotspot is Q10, at 115-116 C at all three grid steps.
- Fixed DC-004 calling a pad or via on a half micrometre moved. Positions
  were rounded to the micrometre on both sides and compared exactly, so a
  coordinate the file reader places a hair below the half fell on the other
  side of the live one: on P02 Rev E, D1, D2, U8, R65 and C53 put ten
  untouched pads in six DC-004 findings. Pads and vias now pair within the
  10 um the footprint comparison already allows.
- Fixed DC-004 calling every pour of a board with renamed copper layers
  edited since the last save. The saved fills merged into the live zone
  copper were paired with live layers by canonical name only, and the live
  stackup knows a renamed layer ("PWR.L3" for In2.Cu) by the designer's
  name, so they kept their file ordinal, which in KiCad 10 is another
  layer's IPC id. On P02 Rev E the In2 VBUS_SYS pour was read as In3 copper
  and ten DC-004 findings said 100 % of each pour differed. Saved layers now
  pair by name, designer name, then position, and one nothing pairs is left
  out. The EMC return-path checks read the same zone copper.
- Added a grid check to the DC analysis, on by default ("Check Grid at Half
  Step", beside the mesh resolution). The worst-load drop had no error bar:
  each rail whose drop reaches a tenth of the budget is solved again at half
  the grid step, and a drop that moves by more than 10 % raises DC-005, HIGH
  when the finer solve lands on the other side of the budget. On the P02
  Rev E power board /+5V_U4 reads 40.8 mV at 0.1 mm and 49.7 mV at 0.05 mm,
  /J2_PD_HV 30.7 and 36.4 mV; at twice the step, as the thermal check goes,
  /+5V_U4 reads 39.1 mV and would have passed. The other 14 rails, under a
  tenth of the 5 % budget, are not refined, and the result lists them; the
  check added 42 s to the 73 s run. The DC solves the thermal and CFD runs
  make for their copper losses skip it.
- Fixed the hybrid DC mesh joining a track only to the pads and via lands its
  axis reaches. A land the track's copper covers beside the axis -- vias sewn
  along the edge of a wide track, a pad the track brushes -- reached it only
  through whatever pad the track ended on. On the P02 Rev E power board the
  3 A of J2_SRC_VBUS ran to the end of its 1 mm track and back through two
  slivers of pad copper, for 73.7 mV of drop; it is 27.2 mV now, what the
  raster mesh gives with the same pad locator. Such a land is joined from the
  nearest point of the axis, across the track's own copper.
- Changed AC capacitor discovery to derate a class II ceramic for DC bias when
  its value states its rating and dielectric ("47u 16V X5R"): at the rail's
  voltage V it keeps 1 / (1 + (V / 0.6 Vr)^1.5) of its nominal value, 32 % at
  its rating Vr and 71 % at a third of it, inside the 50-80 % loss at rating
  that docs/audit-si-pdn.md records. Its model source gives both values and
  calls them an estimate. On the P02 Rev E power board C56, 47u 16V X5R on
  the 5.08 V +5V_RAIL, enters the sweep at 33.9 uF instead of 47. C0G,
  polymer and unrated parts keep their nominal value; the ESR stays the
  10 mOhm estimate until part data replace it.
- Changed the power tree's name guesses: a name that says it is a ground, a
  converter node or a control line (SW, FB, BOOT, EN, PG, VALID, SENSE...)
  is no longer guessed a rail, and a name with VBUS is, since a USB
  connector's supply has no power pin. On P02 Rev E the scan guessed
  /MAIN_3V3_SW, /MAIN_3V3_FB, /MAIN_3V3_BOOT, /PWR_VALID2 and /J1_SAFE_PWR_EN
  and missed both VBUS inputs; it now proposes its 18 declared rails, and
  /+3V3_MAIN, /J1_VBUS_RAW, /J2_VBUS_PORT and /J2_VBUS_FUSED from their names.
- Changed ground detection by name to recognise AGND, DGND, PGND and SGND
  wherever they stand in a net's name (/SYS_5V_AGND), and the project's
  declared rails to leave every ground out. A ground symbol's net and a
  converter's analog ground are rails to the netlist -- a power symbol, power
  pins -- but no source feeds them, and meshing GND, the widest copper of
  most boards, bought nothing: on the P02 Rev E power board GND and two
  converter analog grounds were proposed to the power tree. SCH-001 now
  counts a capacitor to such a ground as decoupling.
- Changed an enclosure CFD run whose enclosure was never entered to fit the
  board: the 120 x 100 x 50 mm default grows to the board plus 20 mm on each
  side when the board is larger, and the log and the CFD tab say so. The
  125 mm long P02 Rev E board made the CFD refuse to run. An enclosure that
  does not hold the board now names both sizes.
- Added a resistive loss model for pass stages -- current shunts, fuses, net
  ties: `{"kind": "resistive", "resistors": [{"ref_des", "resistance_ohm"}]}`
  dissipates I2R on each part, with the temperature models the other kinds
  take, and the report gives R(T). A shunt had to be declared a MOSFET to
  dissipate at all.
- Changed the coupled analysis, and the DC log, to say why no rail was solved
  when none was: the rails with neither a source nor a regulator output
  feeding them, or else that their sources found no copper. A power tree
  taken from the board scan and never filled in, as on P02 Rev E, stopped
  with "Coupled analysis requires a successful DC analysis" after 18 rails
  skipped one log line at a time.
- Fixed a heat source's thermal evidence being dropped on every refresh. An
  automatic heat source kept the package and junction values saved for it,
  but its thermal source, condition and provenance went back to "estimate":
  a datasheet thetaJB was then reported as assumed, and TH-001 graded one
  step lower.
- Fixed EMI/EMC source discovery missing converter switch nodes whose name
  carries no Ux_SW, PH or LX token, and taking unconnected pins for sources.
  On the P02 Rev E power board it found none of the four switch nodes
  (/SYS_5V_SW1, /SYS_5V_SW2, /J2_SRC_SW, /MAIN_3V3_SW) and listed six
  unconnected-(U6-SW-...) nets, the power module's internal SW pins left open,
  as switching sources. A net named with an SW token is now a switch node when it
  joins an inductor to an IC and is no rail, which a switched rail such as
  VBUS_PD_SW does not; its frequency is looked up on every part of the net.
  A net KiCad names unconnected-(...) is never a source.
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
