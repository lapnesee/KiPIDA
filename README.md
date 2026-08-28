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
- Adjustable thermal mesh from 0.01 to 5 mm, including a 0.01 mm Super preset, projected node/branch/memory estimates, and adaptive safety limits.
- 3D, top, bottom, and internal-copper temperature maps with a selectable palette and configurable lower and upper colour bounds. Temperatures above a custom maximum saturate at the hottest colour. The thermal overlay can be injected into dedicated non-electrical KiCad user layers with the same colour scale, then removed from the GUI.

### Enclosure CFD

- Structured volumetric enclosure mesh around the PCB and compact component solids.
- Steady laminar airflow, pressure, temperature, Boussinesq buoyancy, and conjugate solid-air heat transfer.
- Configurable enclosure dimensions, board placement, material settings, and inlet, outlet, vent, wall, and fan boundary patches.
- Temperature, velocity, pressure, residual, mass-balance, and energy-balance results.

### Differential pairs and controlled impedance

- Separate discovery of differential-pair candidates from power-rail detection using P/N, +/- and DP/DM naming, KiCad pin functions, and short series-passive continuity.
- Confirmation, exclusion, and manual pair management per network.
- Physical stackup extraction from KiCad 10 or validated JSON stackup import.
- Layer-aware coupled microstrip/stripline impedance estimates with local adjacent-ground-plane coverage checks.
- Length mismatch, routed-section, reference-plane, and confidence reporting.
- Editable geometry recommendations based on target impedance and manufacturing width/gap/reference-plane constraints.
- Injection of selected recommendations into dedicated `KiPIDA_DIFF_*` KiCad net classes and predefined routing sizes.

### EMI/EMC pre-compliance

- Automatic discovery and editable modelling of clocks, switching nodes, high-speed interfaces, differential sources, external connectors, and cable lengths.
- Configurable CISPR 32, FCC Part 15, CISPR 25, and MIL-STD-461G target profiles and analysis frequency band.
- Traceable checks for ground-plane continuity, fragmented planes, stackup reference spacing, signal paths crossing voids, layer transitions without return vias, sparse stitching, and board-edge routing.
- Clock, switching-node area, differential skew/reference changes, long parallel-route crosstalk, decoupling, connector filtering, ESD-return, and shield-return risk checks.
- Reuse of the latest AC impedance, differential-pair, and thermal results when available, with explicit confidence levels when only geometric evidence exists.
- Severity-ranked findings with stable rule identifiers, per-net scores, board-coordinate evidence, a PCB risk map, relative harmonic/cavity-resonance plot, and a near-field pre-compliance test plan.
- Results are engineering risk estimates rather than a compliance certificate; absolute emissions, immunity, enclosure seams, and real cable behaviour still require measurement.

### Results and interaction

- Independent result workspaces for DC, AC, differential, EMI/EMC, thermal, CFD, and debug analyses: a new analysis does not erase another analysis type.
- Persistent result history stored in `KiPIDA-results` beside the board/project, selectable from the Results tab and removable from the GUI.
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

For optional NVIDIA acceleration, install the CuPy package matching the installed CUDA runtime:

```bash
python -m pip install -r requirements-cuda13.txt
```

Use `requirements-cuda12.txt` for a CUDA 12 environment. A compatible NVIDIA driver, CuPy build, and KiCad restart are required. CUDA is optional; Ki-PIDA continues to operate with CPU sparse solvers when it is unavailable.

## 📖 Quick Start: DC Power Integrity

1. Open the PCB and launch **Ki-PIDA Simulation**.
2. In **Power Tree Config**, review the discovered rails and create the relationships between input rails and regulator outputs.
3. For each rail, add one or more **Sources** and **Loads**, select their pads, and enter the expected load current.
4. Set the voltage, regulator type, and efficiency where applicable.
5. Click **Run DC Simulation**.
6. Open **Results** to review the voltage-drop summary, per-rail 3D view, and layer maps.

Ki-PIDA solves rail dependencies from downstream loads back towards their source rails. A `12 V → 5 V → 3.3 V` path therefore propagates the 3.3 V demand upstream before solving the input rail.

> [!TIP]
> A disconnected copper island containing a source-free load is a board-connectivity issue, not a valid voltage-drop result. Review the reported islands before using a result for design decisions.

## 📖 AC Impedance and Decoupling

1. Configure the power rail in **Power Tree Config**, then open **AC Impedance**.
2. Select the rail, return net, source, and measurement component.
3. Enter the sweep range, points per sweep, source parasitics, and target impedance.
4. Review the detected rail-to-return capacitors.
5. Click **Run AC Analysis** for impedance plots or **Optimize Decoupling** to score candidate/DNP footprints.

The optimiser is deliberately non-destructive: it reports placements and values to consider, but does not alter the schematic, BOM, or layout.

## 📖 3D Thermal and Coupled DC/Thermal

1. In **3D Thermal**, set ambient temperature, mesh size, exposed surfaces, and convection mode.
2. Review **Component Heat Sources**. A load can be local to this PCB or external; regulator losses can be assigned to the physical dissipating component.
3. Click **Run Thermal** for a steady-state board solve.
4. Enable **Include DC copper losses** and click **Run Coupled** to iterate electrical copper loss with temperature-dependent resistance.
5. Review hotspot, input/boundary heat, energy balance, component junction estimates, and surface/internal maps in **Results**.
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
4. Check the ground-net aliases and enable the relevant rule families.
5. Click **Run EMI/EMC**.
6. In **Results**, review critical/high findings first, then inspect the board risk map, relative source spectrum, per-net scores, and suggested near-field probe points.

Each finding records its rule ID, confidence, affected nets/components, board coordinates when available, and a concrete correction. Geometry checks are refreshed from the live PCB before every run. The relative spectrum ranks frequencies for investigation but does not plot or predict an absolute regulatory limit.

## 🧭 Results and Saved History

The **Results** tab keeps each analysis type separate for the current session. Result snapshots are also saved under `KiPIDA-results` beside the PCB/project. Select a previous snapshot from the history menu to inspect it, or use the deletion control to remove stored results from the GUI.

The project configuration is saved as `<project>.kipida.json` beside the `.kicad_pro` file. It stores project-scoped settings such as rails, loads, AC profiles, thermal configuration, CFD settings, differential-pair choices, and EMI/EMC sources and target profile. Runtime acceleration settings intentionally remain machine-local.

## 🛠️ Technical Overview

Ki-PIDA is built as a modular Python application around KiCad's IPC API.

| Area | Main modules | Purpose |
|---|---|---|
| Board extraction | `extractor.py`, `discovery.py` | Live PCB geometry, components, zones, stackup, power rails |
| DC and AC | `mesh.py`, `solver.py`, `ac_model.py`, `ac_solver.py`, `decoupling_optimizer.py` | Resistive/complex sparse power-delivery analysis |
| Thermal | `thermal_model.py`, `thermal_mesh.py`, `thermal_solver.py`, `electrothermal.py`, `thermal_overlay.py` | 3D board conduction, coupling, plots, and KiCad overlays |
| CFD | `cfd_model.py`, `cfd_mesh.py`, `cfd_solver.py`, `conjugate_heat_transfer.py` | Enclosure airflow and conjugate heat transfer |
| Differential pairs | `differential_discovery.py`, `reference_plane_analyzer.py`, `differential_impedance.py`, `differential_recommender.py` | Discovery, stackup/reference-plane checks, impedance, recommendations |
| EMI/EMC | `emc_analyzer.py`, `ui/emc_analysis_panel.py` | Source discovery, geometric rules, coupled-analysis reuse, risk scoring, spectrum and test plan |
| Runtime and UI | `compute_backend.py`, `runtime_config.py`, `ui/` | CPU/CUDA selection, controls, history, and interactive views |

Electrical analysis uses a hybrid 2.5D finite-difference mesh: each copper layer is a 2D grid with vertical via/PTH connections. EMI/EMC analysis combines deterministic geometric rules with relative analytical source/resonance estimates. Thermal analysis uses a separate 3D finite-volume solid mesh through the physical stackup. Enclosure CFD adds a structured volumetric air mesh and conjugate energy equation. These models are intended for design guidance and comparison; they are not full-wave electromagnetic, turbulent CFD, or sign-off solvers.

## 🔧 Development

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests
```

The project uses Python, wxPython, NumPy, SciPy, Shapely, Matplotlib, and the KiCad Python IPC API. Optional CUDA support uses CuPy.

## 📄 License

See [LICENSE](LICENSE).
