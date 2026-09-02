# Ki-PIDA (KiCad Power Integrity & Delivery Analyzer)

Ki-PIDA is a native KiCad plugin for PCB power-integrity, signal-integrity, EMI/EMC, thermal, and airflow analysis. It works from the live Pcbnew board: configure the design intent, run an analysis, and inspect numerical results and visual maps without exporting the layout to a separate tool.

## 🚀 Why Ki-PIDA?

Power-delivery and thermal issues are often discovered late: an undersized copper neck, a missing decoupling capacitor, a hot regulator, or a differential pair with an unreliable return path. Ki-PIDA provides practical engineering models early in the layout cycle so these issues can be located and compared before hardware is built.

- **Stay in KiCad:** Extract geometry, stackup, components, tracks, vias, pads, and filled zones from the open PCB.
- **Compare design choices quickly:** Explore power, decoupling, thermal, airflow, and routing trade-offs from one workspace.
- **Keep the evidence:** Results remain available during the session and are saved with the project for later review.

## ✨ Key Features

### Power delivery and DC analysis

- Automatic discovery of candidate power rails and an editable power-tree configuration.
- Pad-level source and load assignment, rail dependency ordering, regulator efficiency, and LDO loss modelling.
- Multi-layer 2.5D resistive copper mesh with track, pad, zone, via, and plated-through-hole connectivity.
- Per-rail voltage drop, current-density, copper-loss, floating-island, and connectivity diagnostics.
- Live-board refresh before every run: save the PCB, then analyse again without restarting KiCad or the plugin.

### AC impedance and decoupling

- Rail-to-ground impedance magnitude and phase sweep over a configurable logarithmic frequency range.
- Frequency-dependent copper/via RL branches and capacitor RLC estimates.
- Target-impedance pass/fail reporting with worst-frequency identification.
- Non-destructive decoupling optimisation using existing DNP/candidate capacitor footprints.

### 3D thermal analysis

- Steady-state 3D solid-conduction model through the physical stackup, copper coverage, thermal vias, components, convection, and optional radiation.
- Natural, forced, and custom convection modes with exposed top, bottom, and edge surfaces.
- Manual or power-tree-derived component heat sources, regulator loss placement, and compact junction-temperature estimates.
- Electro-thermal coupling: iterates temperature-dependent copper resistance and DC `I²R` loss to convergence.
- Adjustable thermal mesh from 0.01 to 5 mm, including a 0.1 mm Super preset, projected node/branch/memory estimates, and adaptive safety limits.
- 3D, top, bottom, and internal-copper temperature maps with a selectable palette and configurable lower and upper colour bounds. Temperatures above a custom maximum saturate at the hottest colour. The thermal overlay can be injected into dedicated non-electrical KiCad user layers with the same colour scale, then removed from the GUI.

### Enclosure CFD

- Structured volumetric enclosure mesh around the PCB and compact component solids.
- Steady laminar airflow, pressure, temperature, Boussinesq buoyancy, and conjugate solid-air heat transfer.
- Configurable enclosure dimensions, board placement, material settings, and inlet, outlet, vent, wall, and fan boundary patches.
- Temperature, velocity, pressure, residual, mass-balance, and energy-balance results.
- KiCad geometry is captured into a detached thermal board model before the
  run. Optional DC copper losses, CFD solving, and six plot renders are then
  sequenced by one cancellable background controller.

### Differential pairs and controlled impedance

- Separate discovery of differential-pair candidates from power-rail detection using P/N, +/- and DP/DM naming, KiCad pin functions, and short series-passive continuity.
- Confirmation, exclusion, and manual pair management per network.
- Physical stackup extraction from KiCad 10 or validated JSON stackup import.
- Layer-aware coupled microstrip/stripline impedance estimates with local adjacent-ground-plane coverage checks.
- Length mismatch, routed-section, reference-plane, and confidence reporting.
- Editable geometry recommendations based on target impedance and manufacturing width/gap/reference-plane constraints.
- Injection of selected recommendations into dedicated `KiPIDA_DIFF_*` KiCad net classes and predefined routing sizes.
- Live tracks, reference planes, and stackup are captured on the KiCad UI
  thread; impedance solving, recommendation qualification, and plot rendering
  run in a cancellable background controller.

### EMI/EMC pre-compliance

- Automatic discovery and editable modelling of clocks, switching nodes, high-speed interfaces, differential sources, external connectors, and cable lengths.
- Configurable CISPR 32, FCC Part 15, CISPR 25, and MIL-STD-461G target profiles and analysis frequency band.
- Traceable checks for ground-plane continuity, fragmented planes, stackup reference spacing, signal paths crossing voids, layer transitions without return vias, sparse stitching, and board-edge routing.
- Clock, switching-node area, differential skew/reference changes, long parallel-route crosstalk, decoupling, connector filtering, ESD-return, and shield-return risk checks.
- Reuse of the latest AC impedance, differential-pair, and thermal results when available, with explicit confidence levels when only geometric evidence exists.
- Severity-ranked findings with stable rule identifiers, per-net scores, board-coordinate evidence, a PCB risk map, relative harmonic/cavity-resonance plot, and a near-field pre-compliance test plan.
- Quasi-static electric and magnetic near-field maps above the PCB, with configurable probe height, grid size, frequency envelope, per-source voltage swing/current, CPU/CUDA execution, hotspot coordinates, and live field readout under the mouse.
- Switching-inductor magnetic estimates driven by the calculated buck ripple spectrum, with MPN-specific geometry/current limits, explicit shield provenance, per-inductor H-field contributions, and a targeted-refinement gate that refuses unsupported material assumptions. Shield attenuation is applied only when backed by a manufacturer curve or user measurement.
- **Phase 10 multi-fidelity EMC:** automatic detection of ngspice and openEMS outside `PATH`, isolated Python-3.13 openEMS execution, parametric transient-source generation, risk-ranked local 3-D region selection, bounded geometry/stackup/track/zone/via export, mesh cell guards, and optional full-wave execution.
- Phase 10 artifacts are written to a timestamped `KiPIDA-results/*-EMC-PHASE10` directory with a machine-readable manifest. Relative risk spectra pass through an RBW/detector receiver stage but are never compared with regulatory limits until an absolute calibrated far-field result exists.
- Gmsh is detected as an optional FEM dependency. Palace can be selected as a remote LAN backend: Ki-PIDA validates and executes an existing Palace project through OpenSSH, then retrieves its resolved configuration and result artifacts without replacing the user-supplied mesh, materials, ports, or boundary conditions with hidden approximations.
- Results are engineering risk estimates rather than a compliance certificate; absolute emissions, immunity, enclosure seams, and real cable behaviour still require measurement.
- The live PCB is converted to an `EMCGeometrySnapshot` on the KiCad UI thread.
  Deterministic checks, near-field solving, Phase 10 execution, and plot
  rendering then share one cancellable background controller and one error path.

### Results and interaction

- Independent result workspaces for DC, AC, differential, EMI/EMC, thermal, CFD, and debug analyses: a new analysis does not erase another analysis type.
- Persistent, versioned result history stored in `KiPIDA-results` beside the board/project. The Results workspace defaults to the latest campaign per analysis, can filter one analysis or show the complete history, and keeps explicit delete confirmations.
- A common result contract presents the same verdict, severity, confidence, metric, evidence, limitation, and artifact concepts for every analysis domain.
- Findings can be filtered by severity or searched by rule, text, net, and component. Selecting a finding opens its full recommendation and evidence without truncating the table, while a separate view exposes result provenance and model limitations.
- Version-1 history remains readable without rewriting project data and is labelled `LEGACY` when structured metrics or provenance are unavailable.
- Live thermal probing on Top, Bottom, and internal-copper maps: hover a solved map to read the nearest mesh-node temperature, X/Y/Z coordinate, and layer in the persistent status area above the Run buttons.
- Clickable EMI/EMC map and spectrum observations with rule, severity, confidence, evidence targets, engineering interpretation, and corrective recommendation. Click the same point again to close its popup, click another point to replace it, or double-click the popup/point to copy its full contents.
- Wheel zoom and left-drag panning for tables, plots, and 3D representations; output consoles support `Ctrl` + wheel, `Ctrl` + `+`/`-`, and `Ctrl+0` for text scaling.
- Timestamped logs and total elapsed time in published reports.

### Runtime acceleration

- **Runtime & Acceleration** panel showing Ki-PIDA, Python, sparse-backend, CuPy, CUDA driver/runtime, GPU, and available-memory information.
- Automatic, CPU, or NVIDIA CUDA backend selection for compatible DC, AC, thermal, and enclosure-energy sparse solves.
- Configurable CPU thread count used for solver control and multilayer rasterisation.
- CUDA float64 sparse solves, numerical residual reporting, CSR reuse during coupled thermal iterations, and GPU-resident compatible matrix/preconditioner data.
- Machine-local CPU/GPU memory ceiling and a backend self-test. Runtime settings are stored outside the PCB project.

### Interface languages

- Automatic language selection from the operating-system UI locale.
- English is the canonical source language and is always available as the fallback.
- A complete French interface catalog covers panels, dialogs, reports, plots, probes, diagnostics, and user-facing logs.
- The language can be overridden with **System default**, **English**, or **Français** in **Runtime & Acceleration**. The selection is applied the next time the Ki-PIDA window is opened and does not change numeric parsing or project-file formats.

## 📦 Installation

Ki-PIDA targets KiCad 10 with the IPC-based Python API enabled.

### 1. Enable the KiCad API

1. Open KiCad.
2. Go to **Preferences** → **Common**.
3. In **API**, enable the Python/IPC API.
4. Restart KiCad if it requests it.

### 2. Install the plugin

1. Download or clone this repository.
2. Copy the repository folder to the KiCad third-party plugin directory used by your KiCad installation.
3. Install the Python dependencies into the Python runtime used by KiCad:

   ```bash
   python -m pip install -r requirements.txt
   ```

4. Restart KiCad and open a PCB in Pcbnew. **Ki-PIDA Simulation** is available from the PCB editor toolbar/action menu.

To change the interface language, open **Runtime & Acceleration**, select a language, save the runtime settings, close Ki-PIDA, and open it again. Saved result snapshots remain in the language used when they were generated.

For optional NVIDIA acceleration, install the CuPy package matching the installed CUDA runtime:

```bash
python -m pip install -r requirements-cuda13.txt
```

Use `requirements-cuda12.txt` for a CUDA 12 environment. A compatible NVIDIA driver, CuPy build, and KiCad restart are required. CUDA is optional; Ki-PIDA continues to operate with CPU sparse solvers when it is unavailable.

Phase 10 optionally uses `C:\Spice64\bin\ngspice_con.exe` and `C:\openEMS\openEMS.exe` on Windows. The openEMS Python wheels must run in a matching isolated runtime; this installation uses `C:\openEMS\phase10-venv\Scripts\python.exe`. These tools are executed as subprocesses and are not imported into KiCad's Python runtime.

### Palace on a LAN server

Select **Palace — LAN server** in the Phase 10 panel, then configure the host,
SSH port/user, optional private key, remote job root, Palace executable, MPI
process count, and the Palace JSON configuration. Authentication uses the
Windows OpenSSH client, an SSH key or agent, and `BatchMode`; Ki-PIDA never asks
for or stores the server password. The default host-key policy accepts only a
host already present in `known_hosts`. The explicit **Accept new host key** mode
uses OpenSSH's `accept-new` policy and still rejects changed keys.

Palace requires a configuration file and a pre-existing mesh. Ki-PIDA transfers
the entire directory containing the selected JSON so relative mesh/material
references remain valid. It first runs `palace -serial --dry-run`, then starts
`palace -np N config.json`, records the remote job path and logs, and retrieves
the complete project with generated CSV/PVD/resolved-configuration artifacts
under the timestamped `KiPIDA-results/*-EMC-PHASE10/palace-remote` directory.
Keeping remote files is enabled by default for reproducibility. Selecting this
backend therefore explicitly discloses the chosen Palace project directory to
the configured LAN host.

When no project has been selected yet, the UI preselects
`examples/palace-lan-minimal/minimal-electrostatic.json` and its five-tetrahedron
Gmsh mesh. This is a fast end-to-end connection/solver smoke test only; it must
be replaced by a reviewed Palace model before interpreting engineering results.

Differential-pair Phase 10 regions can be excited in differential mode (`+0.5/-0.5`) or common mode (`+0.5/+0.5`). Ki-PIDA creates two configurable lumped legs (45 ohms per leg by default) only when both routed conductors share a verified reference-plane zone at the selected cross-section. This modal approximation is traceable in the report but is not a de-embedded wave port.

## 📖 Quick Start: DC Power Integrity

1. Open the PCB and launch **Ki-PIDA Simulation**.
2. In **Project → Power Tree & DC**, review the discovered rails and create the relationships between input rails and regulator outputs.
3. For each rail, add one or more **Sources** and **Loads**, select their pads, and enter the expected load current.
4. Set the voltage, regulator type, and efficiency where applicable.
5. Click **Run DC Simulation**.
6. Open **Results** to review the voltage-drop summary, per-rail 3D view, and layer maps.

Ki-PIDA captures the live KiCad geometry on the UI thread, then performs DC
meshing and sparse solving in a cancellable background controller. Progress is
reported in Diagnostics, and the same detached snapshot path is used by coupled
DC/thermal runs so worker threads never query KiCad IPC objects directly.

Ki-PIDA solves rail dependencies from downstream loads back towards their source rails. A `12 V → 5 V → 3.3 V` path therefore propagates the 3.3 V demand upstream before solving the input rail.

> [!TIP]
> A disconnected copper island containing a source-free load is a board-connectivity issue, not a valid voltage-drop result. Review the reported islands before using a result for design decisions.

## 📖 AC Impedance and Decoupling

1. Configure the power rail in **Project → Power Tree & DC**, then open **Power Integrity → AC Impedance**.
2. Select the rail, return net, source, and measurement component.
3. Choose a Fast, Balanced, or Accurate analysis preset, then adjust the sweep range, point count, independent AC mesh, source parasitics, and target impedance when required.
4. Review the detected rail-to-return capacitors.
5. Click **Run AC Analysis** for impedance plots or **Optimize Decoupling** to score candidate/DNP footprints.

The optimiser is deliberately non-destructive: it reports placements and values to consider, but does not alter the schematic, BOM, or layout.
AC sweeps and decoupling optimization run in a background application
controller, so navigation, logs, and progress remain responsive during long
frequency sweeps. Progress reports solved points and cancellation remains
available throughout the sweep. A second AC job cannot start until the current
one finishes. The preflight estimate adapts the effective mesh to the configured
node safety limit before solving, and AUTO mode keeps the remaining frequency
points on CPU when a CUDA iterative solve is unsuitable for the network.

## 📖 3D Thermal and Coupled DC/Thermal

1. In **3D Thermal**, set ambient temperature, mesh size, exposed surfaces, and convection mode.
2. Review **Component Heat Sources**. A load can be local to this PCB or external; regulator losses can be assigned to the physical dissipating component.
3. Click **Run Thermal** for a steady-state board solve.
4. Enable **Include DC copper losses** and click **Run Coupled** to iterate electrical copper loss with temperature-dependent resistance.
5. Review hotspot, input/boundary heat, energy balance, component junction estimates, and surface/internal maps in **Results**.

Thermal geometry is captured from KiCad before execution. Meshing, steady-state
solving, and coupled electro-thermal iterations then run through the same
cancellable background-controller pattern as DC and AC analyses. Unchanged
thermal models retain their in-session mesh and sparse-solver cache.
6. Use **Inject thermal overlay** to place the current top and bottom thermal maps plus legends on dedicated KiCad user layers. Use **Clear thermal overlay** to remove only Ki-PIDA-owned overlay images.

Thermal mesh cost grows approximately with the inverse square of the XY grid step: halving the grid step creates roughly four times as many XY cells. Use the estimate shown in the thermal panel and configure a RAM ceiling in **Runtime & Acceleration** before requesting a very fine mesh.

The board model is an engineering steady-state solid-conduction approximation. Junction estimates use compact package thermal parameters and require review against component datasheets and measurement before sign-off.

## 📖 Enclosure CFD

1. Set component powers in **3D Thermal**, then open **Enclosure CFD**.
2. Define enclosure dimensions, PCB placement/orientation, ambient conditions, and wall heat transfer.
3. Select the cell size and add inlet/fan, outlet/vent, or wall patches.
4. Enable board heat sources and optional DC copper losses as required.
5. Click **Run Enclosure CFD** and review the 3D fields, centre slices, residuals, and conservation diagnostics in **Results**.

The enclosure solver is a steady, incompressible, laminar engineering model. It does not model turbulence, rotating fan blades, transient fan curves, leakage, radiation, or certification-grade airflow behaviour.

## 📖 Differential Pairs and Impedance

1. Open **Differential Pairs** and click **Scan Board**.
2. Confirm valid candidates, exclude false positives, or add a manual pair.
3. Use **Refresh from KiCad** to extract the physical stackup, or **Import JSON** to load a fabrication stackup profile.
4. Enter ground-net aliases and the target impedance for each interface.
5. Click **Run Differential Z**.
6. Review impedance, section range, length mismatch, layer type, and adjacent-plane coverage.
7. Select a geometry recommendation and click **Apply Selected to KiCad Rules** when you want Ki-PIDA to create/update its dedicated net class and predefined routing sizes.

Differential impedance uses quasi-static transmission-line approximations. Vias, connector launches, coplanar structures, copper roughness, discontinuities, and fabrication variation need a field solver, test coupon, or measurement for final validation.

## 📖 EMI/EMC Pre-compliance

1. Open **EMI / EMC** and select the target standard, market, and frequency band.
2. Click **Scan Live PCB** and review the detected clocks, switching nodes, fast interfaces, and differential sources.
3. Edit their fundamental frequency and rise time; add manual sources and cable information where detection cannot infer the design intent.
4. For near-field maps, edit each source voltage/current and choose the observation height, grid size, and optional frequency envelope (`0` evaluates each configured source fundamental).
5. Check the ground-net aliases and enable the relevant rule families.
6. Click **Run EMI/EMC**.
7. In **Results**, review critical/high findings first, then inspect the board risk map, relative source spectrum, E/H field maps, per-net scores, and suggested near-field probe points.

Each finding records its rule ID, confidence, affected nets/components, board coordinates when available, and a concrete correction. Geometry checks are refreshed from the live PCB before every run. The relative spectrum ranks frequencies for investigation but does not plot or predict an absolute regulatory limit. The near-field model uses quasi-static line-charge and Biot-Savart trace elements; it does not solve return-current cancellation, dielectric boundaries, phase, enclosure scattering, or full-wave Maxwell coupling.

## 🧭 Results and Saved History

The **Results → Analysis Results** workspace keeps each analysis type separate for the current session. Result snapshots are also saved under `KiPIDA-results` beside the PCB/project. Select a previous snapshot from the history menu to inspect it, or use the deletion control to remove stored results from the GUI. Filter findings by severity, search their rule/text/net/component fields, then select a row to inspect the complete recommendation and finding-specific evidence. The **Evidence & limits** page separates provenance from model limitations.

Each new history entry contains a `manifest.json` index, a structured and
schema-versioned `result.json`, a human-readable `report.txt`, and its plot
artifacts. Older report-only entries remain readable.

## 🧭 Workspace organization

The left sidebar groups tools by engineering purpose instead of presenting
every tool as an equal top-level tab:

- **Project** — power tree, sources, loads, DC mesh, and design limits.
- **Power Integrity** — AC impedance and decoupling optimization.
- **Signal Integrity** — differential-pair impedance, reference, and length checks.
- **EMI / EMC** — pre-compliance rules, risk maps, and field estimates.
- **Thermal** — PCB thermal and enclosure CFD analyses.
- **Results** — structured findings, metrics, plots, and saved campaigns.
- **Application** — runtime acceleration and diagnostics.

Only actions relevant to the selected workspace are shown in the bottom action
bar. Analyses that open Diagnostics or Results automatically keep the sidebar,
workspace title, and actions synchronized.

All long-running analyses use the shared lifecycle in
`application/background_controller.py`: one active run per analysis,
cooperative cancellation, worker-to-UI callback dispatch, typed cancellation
errors, and deterministic completion/error handling. Domain controllers retain
only their request preparation and solver-specific orchestration.

The project configuration is saved as `<project>.kipida.json` beside the `.kicad_pro` file. It stores project-scoped settings such as rails, loads, AC profiles, thermal configuration, CFD settings, differential-pair choices, and EMI/EMC sources and target profile. Runtime acceleration and interface-language settings intentionally remain machine-local.

## 🛠️ Technical Overview

Ki-PIDA is built as a modular Python application around KiCad's IPC API.

| Area | Main modules | Purpose |
|---|---|---|
| Board extraction | `extractor.py`, `discovery.py` | Live PCB geometry, components, zones, stackup, power rails |
| DC and AC | `mesh.py`, `solver.py`, `ac_model.py`, `ac_solver.py`, `decoupling_optimizer.py` | Resistive/complex sparse power-delivery analysis |
| Thermal | `thermal_model.py`, `thermal_mesh.py`, `thermal_solver.py`, `electrothermal.py`, `thermal_overlay.py` | 3D board conduction, coupling, plots, and KiCad overlays |
| CFD | `cfd_model.py`, `cfd_mesh.py`, `cfd_solver.py`, `conjugate_heat_transfer.py` | Enclosure airflow and conjugate heat transfer |
| Differential pairs | `differential_discovery.py`, `reference_plane_analyzer.py`, `differential_impedance.py`, `differential_recommender.py` | Discovery, stackup/reference-plane checks, impedance, recommendations |
| EMI/EMC | `emc_analyzer.py`, `em_field_solver.py`, `ui/emc_analysis_panel.py` | Source discovery, geometric rules, coupled-analysis reuse, risk scoring, spectrum, quasi-static E/H fields and test plan |
| Runtime and UI | `compute_backend.py`, `runtime_config.py`, `ui/` | CPU/CUDA selection, controls, history, and interactive views |

Electrical analysis uses a hybrid 2.5D finite-difference mesh: each copper layer is a 2D grid with vertical via/PTH connections. EMI/EMC analysis combines deterministic geometric rules with relative analytical source/resonance estimates. Thermal analysis uses a separate 3D finite-volume solid mesh through the physical stackup. Enclosure CFD adds a structured volumetric air mesh and conjugate energy equation. These models are intended for design guidance and comparison; they are not full-wave electromagnetic, turbulent CFD, or sign-off solvers.

## 🔧 Development

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests
python tools/i18n_catalog.py extract
python tools/i18n_catalog.py validate locales/fr/LC_MESSAGES/kipida.po --complete
python tools/i18n_catalog.py compile locales/fr/LC_MESSAGES/kipida.po locales/fr/LC_MESSAGES/kipida.mo
```

The project uses Python, wxPython, NumPy, SciPy, Shapely, Matplotlib, and the KiCad Python IPC API. Optional CUDA support uses CuPy.

## 📄 License

See [LICENSE](LICENSE).
