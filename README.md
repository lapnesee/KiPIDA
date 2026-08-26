# Ki-PIDA (KiCad Power Integrity & Delivery Analyzer)

Ki-PIDA is a native KiCad plugin for DC, AC, thermal, and enclosure-airflow Power Integrity (PI) analysis. It allows PCB designers to simulate voltage drops (IR drop), current densities, rail-to-ground impedance, steady-state 3D board temperatures, and enclosure airflow directly within the KiCad Pcbnew environment, eliminating complex external workflows.

## 🚀 Why Ki-PIDA?

Modern electronics operate with tight voltage margins. An IR drop of just 30mV can lead to system instability in sub-1.0V Socs. High current densities also pose thermal risks and reliability hazards like electromigration. 

Ki-PIDA democratizes high-end PI analysis by:
- **Ensuring Stability:** Detect voltage violations at the layout stage.
- **Reducing Iterations:** Identify "neck-down" regions and hotspots before prototyping.
- **Seamless Workflow:** Interactive layout-driven analysis without leaving KiCad.

## ✨ Key Features

- **Native Integration:** Built for KiCad 9.0+ using the Python Scripting API.
- **Power Tree Management:** Auto-discover power rails and manage complex hierarchies including VRM efficiency modeling.
- **Hybrid 2.5D Solver:** Fast and accurate simulation using an optimized resistive mesh approach.
- **Multi-Physics Support:** Coupled electro-thermal simulation to account for temperature-dependent copper resistivity.
- **Multi-Rail Analysis:** Simulate complex power trees with nested regulators (Buck, LDO) and enforce correct dependency solving.
- **Project Persistence:** Automatically saves your power tree configuration (sources, loads, regulators) in the project directory, so you don't lose your setup.
- **Visual Feedback:** Interactive heatmaps for voltage and current density, with dedicated tabs for each power rail in the system.
- **AC Impedance:** Sweep rail-to-ground impedance magnitude and phase over a logarithmic frequency range.
- **Decoupling Optimization:** Rank values for existing unpopulated/DNP capacitor footprints against a target-impedance envelope.
- **3D Thermal Model:** Solve through-stack and lateral heat conduction using the extracted PCB stackup, spatial copper coverage, and thermal vias.
- **Airflow Convection:** Configure natural, forced, or custom heat-transfer coefficients with exposed top, bottom, and edge surfaces.
- **Electro-Thermal Coupling:** Iterate DC copper loss and temperature-dependent copper resistance until the configured convergence threshold is reached.
- **Volumetric Enclosure CFD:** Solve steady laminar airflow, pressure, and temperature on a structured 3D enclosure mesh with Boussinesq buoyancy.
- **Conjugate Heat Transfer:** Map Phase 3 PCB/component/copper losses into solid obstacles coupled directly to the enclosure air-energy equation.
- **Differential Pair Discovery:** Detect P/N, +/- and DP/DM pairs from net names and KiCad pin functions, including continuity through two-pin series passives.
- **Stackup-Aware Differential Impedance:** Estimate routed-pair impedance by layer with adjacent filled-ground-plane coverage checks and imported stackup overrides.
- **Configurable Compute Backends:** Select automatic, CPU/PARDISO, or optional NVIDIA CUDA sparse solves with residual reporting and safe CPU fallback.

## 📦 Installation

Ki-PIDA is designed to run within the KiCad 9.0+ environment. Follow these steps to install and enable the plugin:

### 1. Enable the KiCad API
Ki-PIDA communicates with KiCad via the new IPC-based API.
1. Open KiCad.
2. Go to **Preferences** > **Common**.
3. Under the **API** section, check the box for **Enable API**.
4. Restart KiCad if prompted.
![alt text](image-3.png)

### 2. Install the Plugin
1. Locate your KiCad plugins directory:
   - **Windows:** `%APPDATA%\kicad\9.0\plugins`
   - **Linux:** `~/.local/share/kicad/9.0/plugins`
   - **macOS:** `~/Library/Application Support/kicad/9.0/plugins`
2. Download or clone this repository.
3. Copy the `KiPIDA` folder into the `plugins` directory.

Install the core numerical environment into the Python runtime used to launch the
plugin with `python -m pip install -r requirements.txt`. The optional CUDA
environment can then be installed from **Runtime & Acceleration**, or with
`python -m pip install -r requirements-cuda12.txt`. A compatible NVIDIA driver
and a KiCad restart are required after installing CuPy.

---

## 📖 Tutorial: Your First IR Drop Analysis

Follow these steps to perform a DC Power Integrity analysis on your board.

### 1. Launch the Plugin
Open your PCB layout in KiCad Pcbnew and click the **Ki-PIDA** icon in the top toolbar to open the analyzer.

### 2. Review Discovered Power Rails
- **Auto-Discovery:** Upon launch, Ki-PIDA scans your board for power rails and attempts to load any existing configuration from `kipida_config.json` in your project folder.
- **Add Roots:** Identify your main input rails (e.g., `+12V_IN`, `VBUS`).
- **Define Regulators:** Use the **+ Regulator** button to create relationships between rails (e.g., `12V -> 5V`). Ki-PIDA supports:
    - **Linear Regulators (LDOs):** Pass current 1:1 from input to output.
    - **Switching Regulators (Buck/Boost):** Conserve power based on efficiency (e.g., 90%).
    - **Multi-Output Support:** Handle PMICs where one component drives multiple output rails.

![alt text](image-4.png)

### 3. Add Sources (VRMs / Power Inputs)
Identify where power enters this net:
1. Click **+ Source**.
2. Select the source component (e.g., a regulator `U1` or connector `J1`).
3. In the dialog, check the **Pads** that are connected to the power net.
4. Click **OK**.

### 4. Add Loads (Integrated Circuits / Sinks)
Identify the components consuming power:
1. Click **+ Load**.
2. Select the sink component (e.g., MCU `U2` or FPGA `U3`).
3. Enter the **Total Current (A)** consumed by this component (e.g., `0.5` for 500mA).
4. Check the **Pads** through which the current is drawn.
5. Click **OK**.

![alt text](image-5.png)

### 5. Run the Simulation
Before running, you can adjust the **Mesh Resolution (mm)**. A value of `0.1mm` is usually sufficient for accurate results.
- Click **Run Simulation**.
- The solver analyzes the power tree topology to determine the correct solution order (Leaf-to-Root) for current propagation.
- Example: For a `12V -> 5V -> 3.3V` chain, it solves 3.3V first, applies that load to the 5V rail, solves 5V, and finally solves the 12V input.

### 6. Analyze Results
Once "Simulation Success" appears, the UI will jump to the **Results** tab.

- **Rail Selection:** A tab will be created for each power rail in your system.
- **Per-Rail Visualization:** Inside each rail's tab, you can view:
    - **3D View:** A 3D voltage plot of the entire net.
    - **Layer Views:** Individual 2D heatmaps for every layer containing copper for that net.

> [!TIP]
> Use the **Enable Debug Log** checkbox if you encounter issues during meshing or solving to see more detail in the Log tab.

## Tutorial: AC Impedance and Decoupling Optimization

1. Define the rail's sources and loads in **Power Tree & Config**, then open **AC Impedance**.
2. Select the power rail, return net, source component, and measurement component. Regulator outputs that feed the selected rail are offered as source components.
3. Set the logarithmic sweep range, point count, target impedance, and the source's small-signal resistance/inductance.
4. Review the detected rail-to-ground capacitors. Ki-PIDA reads common KiCad values such as `100n`, `4u7`, and `10uF`; package-derived ESR/ESL values remain engineering estimates.
5. Choose **Run AC Analysis** to plot impedance magnitude and phase, or **Optimize Decoupling** to evaluate the detected disabled/DNP capacitor footprints.
6. Save the project configuration to persist the AC profile in `<project>.kipida.json` beside the `.kicad_pro` file.

The optimizer is intentionally non-destructive: it reports footprint/value recommendations but does not modify the PCB or schematic. It only uses candidate capacitor locations already present on the board.

## Tutorial: 3D Thermal and Airflow Analysis

1. Define rail voltages, loads, and regulator efficiencies in **Power Tree & Config**, then open **3D Thermal**.
2. Select the ambient temperature, thermal grid size, exposed surfaces, and an airflow mode:
   - **Natural:** uses a conservative natural-convection coefficient.
   - **Forced:** derives the coefficient from air speed and applies the configured flow direction across the board surface.
   - **Custom:** uses a user-supplied heat-transfer coefficient.
3. Classify each electrical load with **Thermal load** in its Power Tree dialog:
   - **Auto:** connector references (`J*`) export power off-board; other loads dissipate locally.
   - **Local:** converts the rail load to local heat using `V × I`.
   - **External:** retains the current for upstream regulator sizing but injects `0 W` on this PCB.
4. For each regulator, select the **Thermal loss component** independently from its connectivity endpoints. By default, conversion loss is placed on the input component, avoiding accidental placement on an output inductor.
5. Choose **Refresh Power Estimates**. Regulator dissipation uses LDO voltage drop or switching efficiency. External loads remain visible as zero-watt rows so they can be reviewed or overridden. Double-click any component to enter a reviewed power and compact package thermal model.
6. Choose **Run Thermal** for a single steady-state solve. Enable **Include DC copper losses** to reuse losses from the DC branch solution.
7. Choose **Run Coupled** to iterate copper resistance, DC branch loss, and board temperature. Review the hotspot, energy balance, component junction estimates, and 3D/top/bottom plots in **Results**. The textual report is published immediately; the plots are rendered in the background so the KiCad interface remains responsive.
8. Save the project configuration to persist the thermal profile in `<project>.kipida.json`.

DC meshes can contain small copper fragments that are not electrically connected to a configured source. Ki-PIDA reports and excludes unloaded floating islands from voltage-drop statistics, loss calculations, and plots. An island containing a configured load but no source is reported as a connectivity error.

The airflow model applies convective boundary conditions to the 3D solid board mesh. It is intended for board-level design comparison and hotspot screening; it is not a volumetric CFD enclosure or fan model. Component junction temperatures use the configured compact `theta-JB` estimate and therefore require engineering review before sign-off.

## Tutorial: Enclosure CFD and Conjugate Heat Transfer

1. Configure component powers in **3D Thermal**, then open **Enclosure CFD**.
2. Enter the enclosure width, depth, height, PCB orientation/offset, ambient temperature, and external wall heat-transfer coefficient.
3. Select a CFD cell size. The displayed cell estimate and the built-in maximum-cell guard prevent accidental oversized runs.
4. Add rectangular boundary patches on any enclosure face. **Fan/INLET** patches prescribe inward velocity and temperature; **OUTLET/VENT** patches provide an exhaust pressure boundary; **WALL** restores a no-slip section. **Add Fan Pair** creates a typical inlet/outlet starting point.
5. Keep **Use Phase 3 heat sources** enabled to map PCB/component dissipation. **Include DC copper losses** reuses the latest DC branch losses, running DC first when necessary.
6. Choose **Run Enclosure CFD**. The solve runs on a worker thread; the same button requests cancellation while it is active.
7. Review maximum air/solid temperature, velocity, mass/energy balance, 3D temperature, central temperature/velocity/pressure slices, and convergence residuals in **Results**.
8. Save the project configuration to persist the enclosure, fluid, solver, and boundary-patch profile in `<project>.kipida.json`.

The Phase 4 solver is a steady, incompressible, laminar engineering model. It uses a cell-centred projection method, Boussinesq buoyancy, finite-volume advection/diffusion, and a unified sparse solid-air energy solve. Fans are boundary-flow patches rather than rotating blade geometry. Turbulence, radiation, compressibility, leakage, transient fan curves, and certification-grade validation are outside the current scope.

## Tutorial: Differential Pairs and Stackup-Aware Impedance

1. Open **Differential Pairs** and choose **Scan Board**. Candidates are kept separate from detected power rails and carry name/pin-function evidence plus a confidence level.
2. Confirm valid candidates, ignore false positives, or add a manual P/N pair. Interface defaults provide common targets such as USB 90 ohm, PCIe 85 ohm, LVDS/Ethernet 100 ohm, and CAN 120 ohm.
3. Choose **Refresh from KiCad** to read the live KiCad 10 physical stackup. When the API cannot supply a reviewed stackup, the built-in two-layer profile is marked **estimate only** and cannot produce a trusted PASS/FAIL result.
4. To use a fabrication profile without modifying the board, choose **Import JSON**. The file contains an ordered `layers` list; copper entries require their KiCad `layer_id`, while dielectric entries require thickness and `epsilon_r`.
5. Enter the ground nets that may act as reference planes. The analyzer checks actual filled-zone coverage on the physically adjacent copper layers above and below every matched route section.
6. Choose **Run Differential Z**. Review the length-weighted impedance, section range, layer topology, upper/lower references, plane coverage, and length mismatch in **Results**.
7. Review **Geometry Recommendations** for each network. Ki-PIDA proposes an editable width/gap combination using the same quasi-static model, the nearest adjacent reference plane, and your minimum W/G/GND manufacturing limits. It never edits routing or the KiCad stackup.
8. Save the project configuration to retain confirmed/manual pairs, ignored candidates, targets, ground-net aliases, imported stackup, and the manufacturing limits in `<project>.kipida.json`.

Phase 5/6 uses quasi-static coupled microstrip and stripline engineering approximations. Recommendations remain indicative when the stackup or adjacent-plane coverage is not trusted. Vias, connector launches, coplanar guard geometry, roughness and other three-dimensional discontinuities require a field solver or measurement coupon for final sign-off.

## Phase 6: Result Navigation

Every analysis now retains its own console and plot tabs for the current Ki-PIDA session. Use the mouse wheel over a table, plot, or 3D view to zoom; when zoomed, drag with the left mouse button to pan. In read-only output consoles, use `Ctrl` + mouse wheel or `Ctrl` + `+`/`-` to change the text size (`Ctrl+0` resets it).

Selected differential recommendations can be applied from **Differential Pairs** with **Apply Selected to KiCad Rules**. Ki-PIDA creates or updates only its named `KiPIDA_DIFF_*` net classes, assigns their two exact nets, and adds the suggested width/gap to KiCad's predefined routing sizes. Each new run refreshes live IPC board geometry, components, and differential discovery; saving a PCB modification no longer requires restarting KiCad or Ki-PIDA before re-analysing.

## Phase 7: CPU and CUDA Acceleration

Open **Runtime & Acceleration** to review the installed Ki-PIDA/Python/CuPy
versions, select `AUTO`, `CPU`, or `CUDA`, limit CPU solver threads, select a GPU,
and inspect live thermal-mesh node, branch, CPU/GPU-memory, and backend estimates.
The common sparse backend is used by thermal, DC, and complex AC solves. CUDA
keeps compatible CSR structures and constant thermal matrices resident in VRAM
across iterative sweeps, while multilayer electrical and thermal rasterization
uses the configured CPU worker count. The panel can also run a numerical backend
test. These settings are machine-local and are saved
to `%LOCALAPPDATA%\KiPIDA\runtime.json`; they are intentionally not stored in the
PCB project configuration.

`AUTO` uses CUDA only when it is enabled, healthy, and the matrix exceeds the
configured node threshold. Otherwise it uses PARDISO when available or SciPy as
the portable fallback. Forced `CUDA` mode reports an error instead of silently
using the CPU. Every thermal and enclosure-energy result records the backend,
device, solve time, iteration count, and linear residual. Board thermal matrices
use preconditioned conjugate gradient on CUDA; non-symmetric enclosure-energy
matrices use BiCGSTAB. All CUDA results are computed in float64 and validated
before publication.

Large DC planes are rasterized only inside their per-layer geometry bounds and
in bounded row chunks. Shapely 2's vector point-in-polygon engine releases the
Python GIL, so a single large copper layer is split across the configured CPU
worker count instead of limiting parallelism to the number of PCB layers. A
bounded in-flight queue keeps peak RAM controlled, and older Shapely releases
retain the compatible Matplotlib fallback. When a requested DC resolution would
exceed 400,000 nodes for one rail, Ki-PIDA automatically retries that rail with the smallest
safe grid step, reports the adaptation in the log, and records both requested
and effective grid sizes in the DC result. Coupled-analysis worker logs are
marshalled back to wxPython's GUI thread.

For multi-rail DC and coupled runs, Ki-PIDA indexes tracks, vias, pads, and zones
by net once per live-board snapshot. The DC rasterizer consumes unmerged shape
collections, avoiding an expensive GEOS union of large filled planes such as
`+3V3_MAIN`. Geometry extraction, rasterization, layer construction, and mesh
completion now emit elapsed-time or progress messages even outside Debug mode.

CUDA-enabled thermal runs accept up to 1.25 million projected nodes; CPU-only
runs retain a 500,000-node safety budget and automatically increase the grid
step when necessary. Million-node thermal conductance systems are assembled as
vectorized COO matrices rather than through SciPy LIL row updates. CUDA runs
transfer the COO arrays and perform COO-to-CSR assembly on the GPU; coupled
iterations reuse both the host COO representation and the CSR matrix and
preconditioner kept resident in VRAM. The Results tab reports this sparse-matrix
path and records an adapted thermal grid when one was required.

The **3D Thermal** tab provides a 0.1–5 mm spin control, Fast/Normal/Fine presets,
and the relative XY cell cost. Halving the grid step creates approximately four
times as many XY cells. The existing 500,000-node safety guard remains active.

## 🛠️ Technical Overview (For Developers)

Ki-PIDA is built on a modular architecture designed for performance and maintainability.

### Architecture
- **Extractor (`extractor.py`):** Interfaces with the KiCad API to pull filled zone geometry, track layouts, and physical stackup data.
- **Mesher (`mesh.py`):** Discretizes continuous copper geometry into a 2D/3D resistive grid (Rasterization).
- **Solver (`solver.py`):** Uses an Admittance Matrix (Stamps method) and optimized SciPy sparse solvers (SuperLU/CG) to solve the electrical system.
- **AC Model (`ac_model.py`):** Builds coupled rail/return meshes and maps sources, measurement ports, and rail-to-ground capacitors.
- **AC Solver (`ac_solver.py`):** Stamps frequency-dependent sparse complex admittance matrices for copper/via RL branches and capacitor RLC models.
- **Decoupling Optimizer (`decoupling_optimizer.py`):** Deterministically searches existing candidate footprints against the target-impedance score.
- **Thermal Model (`thermal_model.py`):** Combines stackup, board copper, vias, component placement, power-tree dissipation, and optional DC branch losses.
- **Thermal Mesh (`thermal_mesh.py`):** Builds a finite-volume 3D solid mesh with anisotropic FR-4, spatial copper conductivity, thermal-via branches, radiation, and convective surface boundaries.
- **Thermal Solver (`thermal_solver.py`):** Solves the sparse steady-state heat equation and reports hotspot, component junction estimates, and energy balance.
- **Electro-Thermal Solver (`electrothermal.py`):** Iterates DC branch resistance and loss with the solved copper temperature field.
- **Enclosure Model (`cfd_model.py`):** Places the extracted PCB and compact component solids inside an axis-aligned enclosure and maps Phase 3 heat sources.
- **CFD Mesh (`cfd_mesh.py`):** Builds the bounded structured volumetric grid, solid/fluid masks, heat distribution, and rectangular face patches.
- **CFD Solver (`cfd_solver.py`):** Solves steady laminar momentum/pressure with buoyancy and sparse conjugate solid-air energy, reporting conservation diagnostics and residual histories.
- **CHT Orchestrator (`conjugate_heat_transfer.py`):** Coordinates enclosure construction, volumetric meshing, and the CFD/thermal solve.
- **Differential Discovery (`differential_discovery.py`):** Combines net-name, pin-function, and short series-passive evidence while retaining user confirmations and exclusions.
- **Reference Plane Analyzer (`reference_plane_analyzer.py`):** Resolves the nearest physical copper above/below each signal layer and checks local filled-ground-zone coverage.
- **Differential Impedance (`differential_impedance.py`):** Matches parallel P/N route sections and evaluates coupled microstrip, embedded microstrip, and symmetric/asymmetric stripline estimates.
- **Differential Recommender (`differential_recommender.py`):** Inverts the measured-section model within editable manufacturing limits to propose non-destructive width/gap/reference-plane actions.
- **Interactive Views (`ui/interactive_views.py`):** Provides reusable wheel zoom, drag pan, and output-console text zoom for wxPython views.
- **Results Workspace (`ui/results_workspace.py`):** Keeps DC, AC, differential, thermal, CFD, and debug results isolated during one session.
- **Stackup Import (`stackup_io.py`):** Validates user-owned JSON stackup profiles without editing the KiCad board.
- **Visualizer (`visualizer.py`):** Generates heatmaps via Matplotlib and renders them as overlays in KiCad.

### Methodology
Electrical analysis utilizes a **Hybrid 2.5D Finite Difference Method (FDM)**. It represents PCB layers as 2D grids connected vertically by via/PTH elements. DC analysis uses resistive branches; AC analysis retains the same topology and adds stackup-sensitive branch inductance plus lumped source/capacitor RLC models. Phase 3 thermal analysis uses a separate **3D finite-volume solid-conduction model** through the physical stackup. Phase 4 adds a structured volumetric enclosure grid, steady incompressible projection flow, Boussinesq buoyancy, and conjugate solid-air energy. Phase 5 adds quasi-static coupled transmission-line estimates with local adjacent-plane coverage evidence. These are engineering models, not full-wave electromagnetic, turbulent RANS/LES, or rotating-fan solvers.

### Stack
- **Languages:** Python 3.9+
- **UI:** wxPython
- **Math:** NumPy & SciPy
- **Geometry:** Shapely

## � Current State (Alpha)

As of the current version, Ki-PIDA implements end-to-end DC IR drop, AC target-impedance, steady-state 3D board thermal analysis, volumetric enclosure CFD, stackup-aware differential-pair impedance screening, and per-network geometry recommendations.

### Capabilities:
- **Comprehensive Extraction:** Extracts tracks, pads, and filled zones (respecting thermal reliefs and voids) from KiCad 9.0+ boards.
- **3D Meshing Engine:** Converts geometry into a resistive mesh across multiple layers, correctly modeling via and PTH conductances.
- **Robust Linear Solver:** Solves the circuit using SciPy's sparse matrix backend. Includes island detection to warn about floating sections of copper that could cause numerical issues.
- **Automated Diagnostics:** Detects isolated copper nodes and disjoint electrical islands during the solve phase.
- **Target-Impedance Sweep:** Reports worst-case impedance/frequency and PASS/FAIL against the configured envelope.
- **Deterministic Decoupling Search:** Recommends values for existing DNP/candidate footprints without editing the board.
- **Thermal and Airflow Solve:** Models 3D board conduction, natural/forced/custom convection, radiation, vias, and component heat sources.
- **Coupled Iteration:** Feeds branch-level DC `I²R` losses into the thermal solve and updates copper resistance with temperature.
- **Enclosure CFD:** Resolves 3D air velocity, gauge pressure, air/solid temperature, and natural or prescribed forced convection with mass/energy diagnostics.
- **Differential Signal Integrity:** Separately discovers differential nets, checks adjacent ground-plane continuity, and estimates impedance per routed layer section.

### User Experience:
- **Automated Rail Discovery:** Instantly find power nets based on zone connectivity.
- **Granular Control:** Assign sources and loads down to the individual pad level.
- **In-Memory Visualization:** Instant generation of color-coded heatmaps to inspect voltage distribution without exporting files.

## �🗺️ Roadmap

- **Phase 1:** DC IR Drop, basic thermal checks, and power tree UI.
- **Phase 2:** AC Impedance Analysis ($Z$ vs Frequency) and decoupling capacitor optimization.
- **Phase 3:** Full 3D board thermal modeling with airflow convection and iterative DC coupling.
- **Phase 4:** Volumetric enclosure CFD with boundary-patch fans/vents and conjugate PCB-to-air heat transfer.
- **Phase 5:** Differential-pair discovery, stackup import, adjacent reference-plane analysis, and routed-pair impedance estimates.
- **Phase 6 (Current):** Zoom/pan navigation, persistent per-analysis results, scalable output consoles, and non-destructive differential geometry recommendations.
