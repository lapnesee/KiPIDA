import wx
import wx.dataview
import sys
import os
import threading
import time
from pathlib import Path

# Ensure plugin dir is in path to import modules
plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)

from extractor import GeometryExtractor
from mesh import Mesher
from solver import Solver
from ac_model import ACModelBuilder, format_capacitance
from ac_solver import ACSolver
from decoupling_optimizer import DecouplingOptimizer
from differential_impedance import DifferentialGeometrySnapshot, DifferentialImpedanceSolver
from conjugate_heat_transfer import ConjugateHeatTransferSolver
from electrothermal import ElectroThermalSolver
from thermal_mesh import ThermalMesher
from thermal_model import CopperLossPoint, ThermalModelBuilder
from thermal_solver import ThermalSolver
from ui.ac_analysis_panel import ACAnalysisPanel
from ui.cfd_analysis_panel import CFDAnalysisPanel
from ui.differential_analysis_panel import DifferentialAnalysisPanel
from ui.thermal_analysis_panel import ThermalAnalysisPanel
from ui.runtime_settings_panel import RuntimeSettingsPanel
from ui.power_tree_panel import PowerTreePanel
from ui.interactive_views import ZoomableBitmapPanel, install_navigation
from ui.results_workspace import ResultsWorkspace
from plotter import Plotter

class KiPIDA_MainDialog(wx.Dialog):
    PAGE_CONFIG = 0
    PAGE_AC = 1
    PAGE_DIFFERENTIAL = 2
    PAGE_THERMAL = 3
    PAGE_CFD = 4
    PAGE_RUNTIME = 5
    PAGE_RESULTS = 6
    PAGE_LOG = 7

    def __init__(self, parent, board_adapter, project=None):
        super(KiPIDA_MainDialog, self).__init__(parent, title="Ki-PIDA: Power Integrity Analyzer", 
                                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        
        self.SetSize((1180, 760))
        self.SetMinSize((950, 600))
        
        self.board = board_adapter
        self.project = project
        self._cfd_thread = None
        self._differential_thread = None
        self._cfd_cancel_requested = False
        self._thermal_plot_thread = None
        self._thermal_thread = None
        self._result_generation = 0
        self._thermal_result_generation = 0
        self._closing = False
        self._plot_lock = threading.Lock()
        
        self._init_ui()
        self.Center()
        
        # Redirect stdout/stderr to our log window
        class LogRedirector:
            def __init__(self, log_func):
                self.log_func = log_func
            def write(self, msg):
                if msg.strip():
                     self.log_func(msg.strip())
            def flush(self): pass
            
        sys.stdout = LogRedirector(self.log)
        sys.stderr = LogRedirector(self.log)
        
        self.log("Ki-PIDA UI Initialized.")
        if not self.board:
             self.log("ERROR: No board object connected. Plugin will not function properly.")
        else:
             self.log(f"Board object connected: {type(self.board)}")
        
        if self.project:
            self.log(f"Project: {self.project.name} at {self.project.path}")
        else:
            self.log("WARNING: No project object available.")

        
    def _init_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # 1. Notebook for tabs
        self.notebook = wx.Notebook(self)
        
        # Tab 1: Configuration (New Power Tree Panel)
        self.tab_config = wx.Panel(self.notebook)
        self.power_tree = PowerTreePanel(self.tab_config, self.board, project=self.project, log_callback=self.log)
        
        # Config Tab Layout
        config_sizer = wx.BoxSizer(wx.VERTICAL)
        config_sizer.Add(self.power_tree, 1, wx.EXPAND | wx.ALL, 5)
        
        # Global Settings (Grid Size, Drop %, Debug)
        sett_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        lbl_grid = wx.StaticText(self.tab_config, label="Mesh Resolution (mm):")
        self.txt_grid_size = wx.TextCtrl(self.tab_config, value="0.1", size=(60, -1))
        sett_sizer.Add(lbl_grid, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        sett_sizer.Add(self.txt_grid_size, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)
        
        lbl_drop = wx.StaticText(self.tab_config, label="Max Drop %:")
        self.txt_drop_pct = wx.TextCtrl(self.tab_config, value="5", size=(60, -1))
        sett_sizer.Add(lbl_drop, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        sett_sizer.Add(self.txt_drop_pct, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 20)
        
        self.chk_debug = wx.CheckBox(self.tab_config, label="Enable Debug Log")
        sett_sizer.Add(self.chk_debug, 0, wx.ALIGN_CENTER_VERTICAL)
        
        config_sizer.Add(sett_sizer, 0, wx.EXPAND | wx.ALL, 5)
        self.tab_config.SetSizer(config_sizer)
        
        self.notebook.AddPage(self.tab_config, "Power Tree & Config")
        
        # Tab 2: AC Impedance Configuration
        self.tab_ac = wx.Panel(self.notebook)
        ac_sizer = wx.BoxSizer(wx.VERTICAL)
        self.ac_panel = ACAnalysisPanel(
            self.tab_ac,
            self.board,
            rails_provider=lambda: self.power_tree.rails,
            log_callback=self.log,
        )
        ac_sizer.Add(self.ac_panel, 1, wx.EXPAND | wx.ALL, 5)
        self.tab_ac.SetSizer(ac_sizer)
        self.notebook.AddPage(self.tab_ac, "AC Impedance")
        self.power_tree.ac_profiles_provider = self.ac_panel.get_profiles
        self.power_tree.ac_profiles_consumer = self.ac_panel.set_profiles

        # Tab 3: Differential-pair / signal-integrity configuration
        self.tab_differential = wx.Panel(self.notebook)
        differential_sizer = wx.BoxSizer(wx.VERTICAL)
        self.differential_panel = DifferentialAnalysisPanel(
            self.tab_differential,
            self.board,
            project=self.project,
            log_callback=self.log,
        )
        differential_sizer.Add(self.differential_panel, 1, wx.EXPAND | wx.ALL, 5)
        self.tab_differential.SetSizer(differential_sizer)
        self.notebook.AddPage(self.tab_differential, "Differential Pairs")
        self.power_tree.differential_profile_provider = self.differential_panel.get_settings
        self.power_tree.differential_profile_consumer = self.differential_panel.set_settings

        # Tab 4: 3D Thermal Configuration
        self.tab_thermal = wx.Panel(self.notebook)
        thermal_sizer = wx.BoxSizer(wx.VERTICAL)
        self.thermal_panel = ThermalAnalysisPanel(
            self.tab_thermal,
            rails_provider=lambda: self.power_tree.rails,
            log_callback=self.log,
            mesh_context_provider=self._thermal_mesh_context,
        )
        thermal_sizer.Add(self.thermal_panel, 1, wx.EXPAND | wx.ALL, 5)
        self.tab_thermal.SetSizer(thermal_sizer)
        self.notebook.AddPage(self.tab_thermal, "3D Thermal")
        self.power_tree.thermal_profile_provider = self.thermal_panel.get_settings
        self.power_tree.thermal_profile_consumer = self.thermal_panel.set_settings

        # Tab 5: Enclosure CFD Configuration
        self.tab_cfd = wx.Panel(self.notebook)
        cfd_sizer = wx.BoxSizer(wx.VERTICAL)
        self.cfd_panel = CFDAnalysisPanel(self.tab_cfd, log_callback=self.log)
        cfd_sizer.Add(self.cfd_panel, 1, wx.EXPAND | wx.ALL, 5)
        self.tab_cfd.SetSizer(cfd_sizer)
        self.notebook.AddPage(self.tab_cfd, "Enclosure CFD")
        self.power_tree.cfd_profile_provider = self.cfd_panel.get_settings
        self.power_tree.cfd_profile_consumer = self.cfd_panel.set_settings

        # Tab 6: machine-local compute configuration
        self.tab_runtime = wx.Panel(self.notebook)
        runtime_sizer = wx.BoxSizer(wx.VERTICAL)
        self.runtime_panel = RuntimeSettingsPanel(self.tab_runtime, log_callback=self.log)
        runtime_sizer.Add(self.runtime_panel, 1, wx.EXPAND | wx.ALL, 5)
        self.tab_runtime.SetSizer(runtime_sizer)
        self.notebook.AddPage(self.tab_runtime, "Runtime & Acceleration")

        # Tab 7: Results
        self.tab_results = wx.Panel(self.notebook)
        self._init_results_tab(self.tab_results)
        self.notebook.AddPage(self.tab_results, "Results")
        
        # Tab 8: Log/Debug
        self.tab_log = wx.Panel(self.notebook)
        self._init_log_tab(self.tab_log)
        self.notebook.AddPage(self.tab_log, "Log")
        
        main_sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 5)
        
        # 2. Action Buttons (Bottom)
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_run = wx.Button(self, label="Run DC Simulation")
        self.btn_run_ac = wx.Button(self, label="Run AC Analysis")
        self.btn_optimize = wx.Button(self, label="Optimize Decoupling")
        self.btn_run_differential = wx.Button(self, label="Run Differential Z")
        self.btn_run_thermal = wx.Button(self, label="Run Thermal")
        self.btn_run_coupled = wx.Button(self, label="Run Coupled")
        self.btn_run_cfd = wx.Button(self, label="Run Enclosure CFD")
        self.btn_cancel = wx.Button(self, wx.ID_CANCEL, "Close")
        
        btn_sizer.AddStretchSpacer()
        btn_sizer.Add(self.btn_run, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_run_ac, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_optimize, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_run_differential, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_run_thermal, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_run_coupled, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_run_cfd, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_cancel, 0, wx.ALL, 5)
        
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 5)
        
        self.SetSizer(main_sizer)
        
        # Bind events
        self.btn_run.Bind(wx.EVT_BUTTON, self.on_run)
        self.btn_run_ac.Bind(wx.EVT_BUTTON, self.on_run_ac)
        self.btn_optimize.Bind(wx.EVT_BUTTON, self.on_optimize_decoupling)
        self.btn_run_differential.Bind(wx.EVT_BUTTON, self.on_run_differential)
        self.btn_run_thermal.Bind(wx.EVT_BUTTON, self.on_run_thermal)
        self.btn_run_coupled.Bind(wx.EVT_BUTTON, self.on_run_coupled_thermal)
        self.btn_run_cfd.Bind(wx.EVT_BUTTON, self.on_run_cfd)
        self.btn_cancel.Bind(wx.EVT_BUTTON, self.on_close)
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.on_notebook_page_changed)
        install_navigation(self)
        
        # Auto-scan board after UI is ready
        wx.CallAfter(self.power_tree.auto_scan)

    def on_notebook_page_changed(self, event):
        if event.GetSelection() == self.PAGE_AC:
            wx.CallAfter(self.ac_panel.refresh)
        elif event.GetSelection() == self.PAGE_DIFFERENTIAL:
            wx.CallAfter(self.differential_panel.refresh)
        elif event.GetSelection() == self.PAGE_THERMAL:
            if not self.thermal_panel.settings.components:
                wx.CallAfter(self.thermal_panel.refresh_components)
            wx.CallAfter(self.thermal_panel._update_mesh_cost)
        elif event.GetSelection() == self.PAGE_CFD:
            wx.CallAfter(self.cfd_panel._update_estimate)
        elif event.GetSelection() == self.PAGE_RUNTIME:
            wx.CallAfter(self.runtime_panel.refresh_status)
        event.Skip()

    def _refresh_live_board_state(self):
        """Re-read KiCad's live IPC board data before a new analysis run."""
        try:
            self._thermal_geometry_context = None
            self.ac_panel.refresh(force_discovery=True)
            self.differential_panel.refresh_live_board()
            self.thermal_panel.refresh_components(preserve_user=True)
            self.log("Refreshed live PCB geometry and component discovery.")
        except Exception as exc:
            self.log(f"Live PCB refresh warning: {exc}")

    def _board_file_path(self):
        project_path = getattr(self.project, "path", "")
        if not project_path:
            return None
        candidate = Path(project_path).with_suffix(".kicad_pcb")
        return str(candidate) if candidate.exists() else None
    
    def _init_results_tab(self, parent):
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.results_workspace = ResultsWorkspace(parent)
        sizer.Add(self.results_workspace, 1, wx.EXPAND | wx.ALL, 5)
        parent.SetSizer(sizer)

    def _publish_results(self, analysis_id, report, plots=None):
        """Publish one analysis without discarding other session results."""
        page = self.results_workspace.publish(analysis_id, report, plots)
        self.notebook.SetSelection(self.PAGE_RESULTS)
        return page


    def _init_log_tab(self, parent):
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.log_ctrl = wx.TextCtrl(parent, style=wx.TE_MULTILINE | wx.TE_READONLY)
        sizer.Add(self.log_ctrl, 1, wx.EXPAND | wx.ALL, 5)
        parent.SetSizer(sizer)

    def log(self, msg):
        if not hasattr(self, 'log_ctrl'): return
        if not wx.IsMainThread():
            if not self._closing:
                wx.CallAfter(self.log, msg)
            return
        self.log_ctrl.AppendText(msg + "\n")
        self.log_ctrl.ShowPosition(self.log_ctrl.GetLastPosition())
        wx.SafeYield()
        
    def to_mm(self, val_nm):
        return val_nm / 1e6

    def _get_mesh_nodes(self, mesh, ref_des, pad_names, debug_mode, log_callback=None):
        emit = log_callback or self.log
        if debug_mode:
            emit(f"  [_get_mesh_nodes] Looking up {ref_des} pads={pad_names}")
        nodes = []
        
        # Helper to get attribute value (property or getter)
        def _get_val(obj, attr_name, default=None):
            if obj is None: return default
            # Try property
            if hasattr(obj, attr_name):
                val = getattr(obj, attr_name)
                if val is not None: return val
            # Try getter
            for prefix in ["get_", ""]:
                method_name = prefix + attr_name
                if hasattr(obj, method_name):
                    try:
                        val = getattr(obj, method_name)()
                        if val is not None: return val
                    except: pass
            return default
        
        # Helper to get board items (same pattern as discovery.py)
        def get_board_items(attr_name):
            if hasattr(self.board, attr_name):
                return getattr(self.board, attr_name)
            method_name = f"get_{attr_name}"
            if hasattr(self.board, method_name):
                try: return getattr(self.board, method_name)()
                except: pass
            return []
        
        # Find the footprint
        found_fp = None
        footprints = get_board_items('footprints')
        
        for fp in footprints:
            # Get reference (same logic as discovery.py)
            ref = _get_val(fp, 'reference', _get_val(fp, 'ref_des', ''))
            if not ref:
                # Try reference_field for Kipy
                ref_field = _get_val(fp, 'reference_field')
                if ref_field:
                    text = _get_val(ref_field, 'text')
                    if text:
                        ref = _get_val(text, 'value', '')
            
            if ref == ref_des:
                found_fp = fp
                break
        
        if not found_fp:
            if debug_mode: emit(f"  Warning: Footprint {ref_des} not found.")
            return []
        
        # Get pads (same logic as discovery.py)
        pads = _get_val(found_fp, 'pads')
        if pads is None:
            defn = _get_val(found_fp, 'definition')
            pads = _get_val(defn, 'pads', [])
        
        target_pads = []
        if not pad_names:
            target_pads = pads  # All pads
        else:
            for p in pads:
                # Get pad number/name
                pad_num = _get_val(p, 'number', _get_val(p, 'name', ''))
                if pad_num in pad_names:
                    target_pads.append(p)
        
        if not target_pads:
            if debug_mode: emit(f"  No pads found for {ref_des} matching {pad_names}")
            return []
        
        origin = mesh.grid_origin
        gs = mesh.grid_step
        
        for p in target_pads:
            pos = _get_val(p, 'position')
            if not pos: continue
            
            # Handle KiCad/Protobuf position types
            px, py = 0, 0
            if hasattr(pos, 'x') and hasattr(pos, 'y'):
                px = _get_val(pos, 'x', 0)
                py = _get_val(pos, 'y', 0)
            elif hasattr(pos, '__getitem__'):
                px = pos[0]
                py = pos[1]
            
            # Convert to mm if likely in nm (KiCad native)
            # Heuristic: if > 10000, assume nm
            if abs(px) > 10000 or abs(py) > 10000:
                px /= 1e6
                py /= 1e6
            
            tx = int(round((px - origin[0]) / gs))
            ty = int(round((py - origin[1]) / gs))
            
            # Search 3x3 neighborhood across layers
            found_any = False
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    for layer in range(32):  # search layers
                        nid = mesh.node_map.get((tx+dx, ty+dy, layer))
                        if nid is not None:
                            nodes.append(nid)
                            found_any = True
            
            if not found_any and debug_mode:
                emit(f"  Pad {ref_des} at ({px:.2f},{py:.2f}) not on mesh.")
        
        return list(set(nodes))

    def _build_rail_dependency_graph(self, system_rails):
        """
        Build dependency graph from regulator connections.
        Returns: dict mapping rail_name -> list of rails it depends on
        """
        graph = {rail.net_name: [] for rail in system_rails}
        
        for rail in system_rails:
            for reg in rail.child_regulators:
                # reg.output_rail_name depends on rail.net_name (input)
                if reg.output_rail_name in graph:
                    graph[reg.output_rail_name].append(rail.net_name)
        
        return graph
    
    def _topological_sort_rails(self, system_rails):
        """
        Sort rails in dependency order (leaves first, roots last).
        Raises ValueError if cycle detected.
        Returns: list of PowerRail objects in solve order
        """
        graph = self._build_rail_dependency_graph(system_rails)
        
        # DFS-based topological sort with cycle detection
        visited = set()
        rec_stack = set()  # Recursion stack for cycle detection
        result = []
        
        def dfs(rail_name):
            if rail_name in rec_stack:
                raise ValueError(f"Cycle detected in power rail dependencies involving '{rail_name}'")
            
            if rail_name in visited:
                return
            
            visited.add(rail_name)
            rec_stack.add(rail_name)
            
            # Visit dependencies first
            for dep in graph.get(rail_name, []):
                dfs(dep)
            
            rec_stack.remove(rail_name)
            result.append(rail_name)
        
        # Process all rails
        for rail in system_rails:
            if rail.net_name not in visited:
                dfs(rail.net_name)
        
        # Reverse to get leaves-first order (output rails before input rails)
        result.reverse()
        
        # Convert rail names back to PowerRail objects
        rail_map = {r.net_name: r for r in system_rails}
        return [rail_map[name] for name in result]

    def _thermal_mesh_context(self):
        try:
            geometry = getattr(self, "_thermal_geometry_context", None)
            if geometry is None:
                extractor = GeometryExtractor(self.board)
                bounds = extractor.get_board_bounds(board_file_path=self._board_file_path())
                stackup = extractor.get_board_stackup()
                if not bounds:
                    return {}
                copper_layers = max(1, len(stackup.get("layer_order", [])))
                geometry = {
                    "width_mm": bounds[2] - bounds[0],
                    "height_mm": bounds[3] - bounds[1],
                    "thermal_layers": copper_layers * 2 - 1,
                }
                self._thermal_geometry_context = geometry
            runtime = self.runtime_panel.get_settings() if hasattr(self, "runtime_panel") else None
            return dict(geometry, **{
                "cuda_available": bool(
                    runtime and runtime.cuda_enabled and getattr(self.runtime_panel, "_devices", [])
                ),
                "cuda_min_nodes": runtime.cuda_min_nodes if runtime else 100000,
            })
        except Exception:
            return {}

    def _solve_system(
        self, system_rails, grid_size, debug_mode, compute_settings=None,
        log_callback=None,
    ):
        """Solve configured DC rails without reading or updating wx widgets."""
        emit = log_callback or self.log
        emit(f"--- Starting System Simulation ({len(system_rails)} rails) ---")
        
        try:
            extractor = GeometryExtractor(self.board, debug=debug_mode, log_callback=emit)
            try:
                stackup = extractor.get_board_stackup()
            except Exception as e:
                emit(f"Error extracting stackup: {e}")
                return {}
            
            system_results = {} # rail_name -> { mesh, results, stats }
            rail_total_current = {rail.net_name: 0.0 for rail in system_rails}
            
            # 2. Sort rails in dependency order and check for cycles
            try:
                sorted_rails = self._topological_sort_rails(system_rails)
                rail_order = [r.net_name for r in sorted_rails]
                emit(f"Rail solve order: {' -> '.join(rail_order)}")
            except ValueError as e:
                emit(f"ERROR: {e}")
                return {}
            
            # 3. Solve each rail in topological order
            for rail in sorted_rails:
                emit(f"Processing Rail: {rail.net_name} (Sources: {len(rail.sources)}, Loads: {len(rail.loads)})")
                
                # Update total current for this rail (starting with direct loads)
                rail_total_current[rail.net_name] = sum(load.total_current for load in rail.loads)
                
                # A. Get Geometry & Mesh
                geometry_started = time.perf_counter()
                emit(f"  Extracting copper geometry for {rail.net_name}...")
                geo = extractor.get_net_geometry(rail.net_name, merge=False)
                emit(
                    f"  Geometry ready for {rail.net_name}: {len(geo)} layer(s) in "
                    f"{time.perf_counter() - geometry_started:.3f} s."
                )
                if not geo:
                    emit(f"  Skipping {rail.net_name}: No geometry.")
                    continue
                    
                mesher = Mesher(
                    self.board, debug=debug_mode, log_callback=emit,
                    compute_settings=compute_settings,
                )
                mesh_started = time.perf_counter()
                mesh = mesher.generate_mesh(rail.net_name, geo, stackup, grid_size_mm=grid_size)
                emit(
                    f"  Mesh ready for {rail.net_name}: {len(mesh.nodes):,} nodes in "
                    f"{time.perf_counter() - mesh_started:.3f} s."
                )
                
                if len(mesh.nodes) == 0:
                     emit(f"  Skipping {rail.net_name}: Mesh empty.")
                     continue
                     
                # B. Map Sources & Loads
                solver_sources = []
                solver_loads = []
                
                # 1. Standard Sources
                for src in rail.sources:
                    nodes = self._get_mesh_nodes(
                        mesh, src.component_ref.ref_des, src.pad_names, debug_mode, emit,
                    )
                    if debug_mode:
                        emit(f"  Source {src.component_ref.ref_des} pads {src.pad_names} -> {len(nodes)} nodes")
                    if not nodes:
                        emit(f"  WARNING: Source {src.component_ref.ref_des} pads {src.pad_names} found NO mesh nodes!")
                    v_set = rail.nominal_voltage
                    for nid in nodes:
                        solver_sources.append({'node_id': nid, 'voltage': v_set})

                # 2. Regulator Outputs (Sources for THIS rail)
                for other_rail in system_rails:
                    for reg in other_rail.child_regulators:
                        if reg.output_rail_name == rail.net_name:
                            # Regulator feeds THIS rail. It is a SOURCE.
                            # Use specific OUTPUT location
                            nodes = self._get_mesh_nodes(
                                mesh, reg.output_ref_des, reg.output_pad_names, debug_mode, emit,
                            )
                            if debug_mode:
                                emit(f"  Regulator {reg.name} output {reg.output_ref_des} pads {reg.output_pad_names} -> {len(nodes)} nodes")
                            if not nodes:
                                emit(f"  WARNING: Regulator {reg.name} output {reg.output_ref_des} pads {reg.output_pad_names} found NO mesh nodes!")
                            v_out = rail.nominal_voltage # Output target IS the rail voltage
                            for nid in nodes:
                                solver_sources.append({'node_id': nid, 'voltage': v_out})
                                
                # 3. Standard Loads
                for load in rail.loads:
                    nodes = self._get_mesh_nodes(
                        mesh, load.component_ref.ref_des, load.pad_names, debug_mode, emit,
                    )
                    if not nodes: continue
                    i_per_node = load.total_current / len(nodes)
                    for nid in nodes:
                        solver_loads.append({'node_id': nid, 'current': i_per_node})
                        
                # 4. Downstream Regulators (Loads on THIS rail)
                for reg in rail.child_regulators:
                    # Find the output rail to calculate total load
                    # (It should have been solved already due to topological sort)
                    total_output_current = rail_total_current.get(reg.output_rail_name, 0.0)
                    
                    if total_output_current == 0:
                        if debug_mode:
                            emit(f"  Regulator {reg.name} has no load on output rail {reg.output_rail_name}")
                        continue
                    
                    # Convert to input current based on regulator type
                    if reg.reg_type == "LINEAR":
                        input_current = total_output_current
                    elif reg.reg_type == "SWITCHING":
                        # Power-based conversion: P_in = P_out / efficiency
                        # We need output rail voltage
                        output_rail_v = 0.0
                        for r in system_rails:
                            if r.net_name == reg.output_rail_name:
                                output_rail_v = r.nominal_voltage
                                break
                                
                        p_out = total_output_current * output_rail_v
                        p_in = p_out / reg.efficiency if reg.efficiency > 0 else p_out
                        input_current = p_in / rail.nominal_voltage if rail.nominal_voltage > 0 else 0
                    else:
                        input_current = total_output_current
                    
                    if input_current == 0:
                        continue
                        
                    # Accumulate this regulator's input current into the total for THIS rail
                    rail_total_current[rail.net_name] += input_current
                    
                    # Apply load at regulator input pads
                    nodes = self._get_mesh_nodes(
                        mesh, reg.input_ref_des, reg.input_pad_names, debug_mode, emit,
                    )
                    if not nodes:
                        emit(f"  WARNING: Regulator {reg.name} input at {reg.input_ref_des} pads {reg.input_pad_names} found NO mesh nodes!")
                        continue
                    
                    i_per_node = input_current / len(nodes)
                    for nid in nodes:
                        solver_loads.append({'node_id': nid, 'current': i_per_node})
                    
                    if debug_mode:
                        emit(f"  Regulator {reg.name} draws {input_current:.2f}A from {rail.net_name} ({reg.reg_type})")
                    
                # C. Solve
                if not solver_sources:
                    emit(f"  Warning: No sources for {rail.net_name}. Skipping solve.")
                    continue
                    
                solver = Solver(
                    debug=debug_mode, log_callback=emit,
                    compute_settings=compute_settings,
                )
                detailed_result = solver.solve_detailed(mesh, solver_sources, solver_loads)
                results = detailed_result.voltages
                
                # D. Store Results
                v_vals = list(results.values())
                if v_vals:
                    v_min, v_max = min(v_vals), max(v_vals)
                    drop = v_max - v_min
                    system_results[rail.net_name] = {
                        'mesh': mesh,
                        'results': results,
                        'stats': (v_min, v_max, drop),
                        'sources': solver_sources,
                        'loads': solver_loads,
                        'detailed_result': detailed_result,
                        'compute_metadata': solver.last_compute,
                        'grid_size_mm': mesh.grid_step,
                        'requested_grid_size_mm': mesh.requested_grid_step,
                        'adaptive_grid': mesh.adaptive_grid,
                    }
                    emit(f"  Solved {rail.net_name}: Drop {drop:.4f} V")
                else:
                    emit(f"  Solved {rail.net_name}: No result.")

            return system_results
        except Exception as e:
            emit(f"System Solve Error: {e}")
            import traceback
            emit(traceback.format_exc().rstrip())
            return {}

    def on_run(self, event, update_results=True):
        self._refresh_live_board_state()
        system_rails = self.power_tree.rails
        if not system_rails:
            wx.MessageBox("No power rails defined.")
            return
        self.notebook.SetSelection(self.PAGE_LOG)
        try:
            grid_size = max(0.01, float(self.txt_grid_size.GetValue()))
        except ValueError:
            grid_size = 0.1
        self.system_results = self._solve_system(
            system_rails,
            grid_size,
            self.chk_debug.GetValue(),
            self.runtime_panel.get_settings(persist=True),
        )
        if update_results and self.system_results:
            self._update_results_ui()

    def _prepare_ac_analysis(self):
        self._refresh_live_board_state()
        if self.ac_panel.choice_rail.GetCount() == 0:
            self.ac_panel.refresh(force_discovery=True)
        settings = self.ac_panel.get_settings()
        if settings is None:
            raise ValueError("Select a power rail for AC analysis.")

        rail = next((item for item in self.power_tree.rails if item.net_name == settings.rail_name), None)
        if rail is None:
            raise ValueError(f"Power rail '{settings.rail_name}' is not available.")
        if not settings.source.ref_des:
            raise ValueError("Select a source component in the AC Impedance tab.")
        if not settings.measurement_port.ref_des:
            raise ValueError("Select a measurement component in the AC Impedance tab.")

        try:
            grid_size = max(0.01, float(self.txt_grid_size.GetValue()))
        except ValueError:
            grid_size = 0.1
        debug_mode = self.chk_debug.GetValue()
        builder = ACModelBuilder(self.board, debug=debug_mode, log_callback=self.log)
        network = builder.build(rail, settings, grid_size_mm=grid_size)
        return settings, network

    def _ac_progress(self, completed, total, detail):
        interval = max(1, total // 10)
        if completed == total or completed % interval == 0:
            self.log(f"AC progress: {completed}/{total} ({detail})")
            wx.SafeYield()

    def on_run_ac(self, event):
        self.notebook.SetSelection(self.PAGE_LOG)
        self.log("--- Starting AC Impedance Analysis ---")
        try:
            settings, network = self._prepare_ac_analysis()
            solver = ACSolver(
                debug=self.chk_debug.GetValue(), log_callback=self.log,
                compute_settings=self.runtime_panel.get_settings(persist=True),
            )
            result = solver.solve_sweep(network, settings, progress_callback=self._ac_progress)
            self.ac_result = result
            self.ac_optimization_result = None
            self._update_ac_results_ui(result)
        except Exception as exc:
            self.log(f"AC Analysis Error: {exc}")
            wx.MessageBox(str(exc), "AC Analysis Error", wx.OK | wx.ICON_ERROR)

    def on_optimize_decoupling(self, event):
        self.notebook.SetSelection(self.PAGE_LOG)
        self.log("--- Starting Decoupling Optimization ---")
        try:
            settings, network = self._prepare_ac_analysis()
            solver = ACSolver(
                debug=self.chk_debug.GetValue(), log_callback=self.log,
                compute_settings=self.runtime_panel.get_settings(persist=True),
            )
            optimizer = DecouplingOptimizer(
                solver,
                debug=self.chk_debug.GetValue(),
                log_callback=self.log,
            )
            optimization = optimizer.optimize(network, settings, progress_callback=self._ac_progress)
            self.ac_result = optimization.optimized
            self.ac_optimization_result = optimization
            self._update_ac_results_ui(optimization.baseline, optimization)
        except Exception as exc:
            self.log(f"Decoupling Optimization Error: {exc}")
            wx.MessageBox(str(exc), "Decoupling Optimization Error", wx.OK | wx.ICON_ERROR)

    def _update_ac_results_ui(self, result, optimization=None):
        self._result_generation += 1
        final_result = optimization.optimized if optimization else result
        status = "PASS" if final_result.meets_target else "TARGET NOT MET"
        lines = [
            "AC Impedance Analysis Results",
            "=============================",
            f"Status: {status}",
            f"Worst |Z|: {final_result.worst_impedance_ohm:.6g} ohm",
            f"Worst frequency: {final_result.worst_frequency_hz:.6g} Hz",
            f"Target: {final_result.target_impedance_ohm:.6g} ohm",
            f"Compute backend: {final_result.compute_backend} ({final_result.compute_device})",
            f"Sparse solve time: {final_result.compute_solve_seconds:.4g} s "
            f"(transfer {final_result.compute_transfer_seconds:.4g} s)",
            f"Linear residual: {final_result.compute_relative_residual:.4g}; "
            f"CUDA structure cache hits: {final_result.compute_cache_hits}",
        ]
        if optimization:
            lines.extend(["", "Decoupling recommendations:"])
            if optimization.recommendations:
                for recommendation in optimization.recommendations:
                    lines.append(
                        f"  - {recommendation.action} {recommendation.ref_des}: "
                        f"{format_capacitance(recommendation.capacitance_f)}"
                    )
            else:
                lines.append("  - No capacitor changes recommended.")
        lines.extend([
            "",
            "Model note: ESR/ESL and distributed inductance may be estimates; review before sign-off.",
        ])
        plotter = Plotter(debug=self.chk_debug.GetValue())
        bitmap = plotter.plot_impedance_sweep(
            result,
            optimization.optimized if optimization else None,
        )
        self._publish_results("AC", "\n".join(lines), [("AC Impedance", bitmap)] if bitmap else [])

    def _prepare_differential_analysis(self):
        self._refresh_live_board_state()
        settings = self.differential_panel.get_settings()
        pairs = [pair for pair in settings.pairs if pair.enabled]
        if not pairs:
            raise ValueError("No enabled differential pairs are available. Scan or add a pair first.")
        stackup = self.differential_panel.get_stackup()
        if stackup is None or not stackup.layers:
            raise ValueError("No PCB stackup is available for differential impedance analysis.")
        extractor = GeometryExtractor(
            self.board,
            debug=self.chk_debug.GetValue(),
            log_callback=self.log,
        )
        snapshot = DifferentialGeometrySnapshot.capture(
            extractor, pairs, settings.reference_net_names
        )
        return settings, pairs, stackup, snapshot

    def _differential_progress(self, completed, total, detail):
        wx.CallAfter(self.log, f"Differential progress: {completed}/{total} ({detail})")

    def on_run_differential(self, event):
        if self._differential_thread is not None and self._differential_thread.is_alive():
            return
        self.notebook.SetSelection(self.PAGE_LOG)
        self.log("--- Starting Differential Pair Impedance Analysis ---")
        try:
            settings, pairs, stackup, snapshot = self._prepare_differential_analysis()
        except Exception as exc:
            self.log(f"Differential Setup Error: {exc}")
            wx.MessageBox(str(exc), "Differential Setup Error", wx.OK | wx.ICON_ERROR)
            return
        self.btn_run_differential.Disable()
        self._differential_thread = threading.Thread(
            target=self._run_differential_worker,
            args=(settings, pairs, stackup, snapshot, self.chk_debug.GetValue()),
            name="KiPIDA-Differential-Impedance",
            daemon=True,
        )
        self._differential_thread.start()

    def _run_differential_worker(self, settings, pairs, stackup, snapshot, debug_mode):
        try:
            solver = DifferentialImpedanceSolver(
                snapshot, stackup, settings,
                log_callback=lambda message: wx.CallAfter(self.log, message),
            )
            results = solver.solve(pairs, progress_callback=self._differential_progress)
            with self._plot_lock:
                plotter = Plotter(debug=debug_mode)
                impedance_png = plotter.plot_differential_impedance(results, as_png=True)
                stackup_png = plotter.plot_stackup_profile(stackup, as_png=True)
            if not self._closing:
                wx.CallAfter(
                    self._finish_differential_analysis,
                    results, stackup, impedance_png, stackup_png,
                )
        except Exception as exc:
            if not self._closing:
                wx.CallAfter(self._fail_differential_analysis, exc)

    def _finish_differential_analysis(self, results, stackup, impedance_png, stackup_png):
        self._differential_thread = None
        self.btn_run_differential.Enable()
        self.differential_panel.apply_results(results)
        lines = [
            "Differential Pair Impedance Results",
            "===================================",
            f"Stackup: {stackup.source} ({'trusted' if stackup.trustworthy else 'estimate only'})",
            "",
        ]
        if stackup.warnings:
            lines.append("Stackup warnings:")
            lines.extend(f"  - {warning}" for warning in stackup.warnings)
            lines.append("")
        for result in results:
            pair = result.pair
            lines.append(
                f"{pair.name}: {pair.positive_net} / {pair.negative_net} "
                f"[{pair.interface}; {pair.confidence}]"
            )
            lines.append(
                f"  Status: {result.status}; Zdiff={result.weighted_impedance_ohm:.3f} ohm; "
                f"target={pair.target_impedance_ohm:g} ohm; error={result.error_pct:+.2f}%"
            )
            lines.append(
                f"  Range: {result.minimum_impedance_ohm:.3f} .. "
                f"{result.maximum_impedance_ohm:.3f} ohm; "
                f"length mismatch={result.length_mismatch_mm:.3f} mm"
            )
            for section in result.sections:
                lines.append(
                    f"  - {section.layer_name}: {section.topology}, "
                    f"w={section.width_mm:.3f} mm, gap={section.gap_mm:.3f} mm, "
                    f"Zdiff={section.differential_impedance_ohm:.3f} ohm, "
                    f"refs={section.reference_above or '-'} / {section.reference_below or '-'}, "
                    f"coverage={section.reference_coverage_pct:.1f}%"
                )
            for warning in result.warnings:
                lines.append(f"  WARNING: {warning}")
            if result.recommendations:
                lines.append("  Recommendations:")
                for recommendation in result.recommendations:
                    geometry = (
                        f"w={recommendation.recommended_width_mm:.3f} mm, "
                        f"gap={recommendation.recommended_gap_mm:.3f} mm"
                        if recommendation.recommended_width_mm else "geometry unavailable"
                    )
                    lines.append(
                        f"  - {recommendation.action} [{recommendation.feasibility}; "
                        f"{recommendation.confidence}]: {geometry}; "
                        f"predicted Zdiff={recommendation.predicted_impedance_ohm:.3f} ohm; "
                        f"GND clearance >= {recommendation.recommended_ground_clearance_mm:.3f} mm"
                    )
            lines.append("")
        lines.extend([
            "Model scope: quasi-static coupled microstrip/stripline estimates. ",
            "Vias and reference-plane transitions are reported as discontinuities; this is not a 3D full-wave solver.",
        ])
        plots = []
        if impedance_png:
            plots.append(("Differential Z", Plotter.bitmap_from_png(impedance_png)))
        if stackup_png:
            plots.append(("Stackup", Plotter.bitmap_from_png(stackup_png)))
        self._publish_results("DIFFERENTIAL", "\n".join(lines), plots)
        self.log("Differential impedance results ready.")

    def _fail_differential_analysis(self, exc):
        self._differential_thread = None
        self.btn_run_differential.Enable()
        self.log(f"Differential Analysis Error: {exc}")
        wx.MessageBox(str(exc), "Differential Analysis Error", wx.OK | wx.ICON_ERROR)

    def _dc_copper_loss_points(self, system_results=None):
        losses = []
        results = system_results if system_results is not None else getattr(self, "system_results", {})
        for data in results.values():
            mesh = data.get("mesh")
            detailed = data.get("detailed_result")
            if mesh is None or detailed is None:
                continue
            for branch, power in zip(mesh.branches, detailed.branch_losses_w):
                if power <= 0:
                    continue
                coord_a = mesh.node_coords.get(branch.node_a)
                coord_b = mesh.node_coords.get(branch.node_b)
                if coord_a is None or coord_b is None:
                    continue
                losses.append(CopperLossPoint(
                    x_mm=(coord_a[0] + coord_b[0]) / 2.0,
                    y_mm=(coord_a[1] + coord_b[1]) / 2.0,
                    layer_id=coord_a[2],
                    power_w=power,
                ))
        return losses

    def _thermal_progress(self, completed, total, detail):
        self.log(f"Thermal progress: {completed}/{total} ({detail})")
        wx.SafeYield()

    def on_run_thermal(self, event):
        if self._thermal_thread is not None and self._thermal_thread.is_alive():
            return
        self.notebook.SetSelection(self.PAGE_LOG)
        self.log("--- Starting 3D Thermal Analysis ---")
        try:
            self._start_thermal_pipeline(coupled=False)
        except Exception as exc:
            self.log(f"Thermal Analysis Error: {exc}")
            wx.MessageBox(str(exc), "Thermal Analysis Error", wx.OK | wx.ICON_ERROR)

    def on_run_coupled_thermal(self, event):
        if self._thermal_thread is not None and self._thermal_thread.is_alive():
            return
        self.notebook.SetSelection(self.PAGE_LOG)
        self.log("--- Starting Coupled DC / 3D Thermal Analysis ---")
        try:
            self._start_thermal_pipeline(coupled=True)
        except Exception as exc:
            self.log(f"Coupled Thermal Analysis Error: {exc}")
            wx.MessageBox(str(exc), "Coupled Thermal Analysis Error", wx.OK | wx.ICON_ERROR)

    def _start_thermal_pipeline(self, coupled):
        """Capture wx settings, then prepare and solve entirely off the GUI thread."""
        self._refresh_live_board_state()
        if not self.thermal_panel.settings.components:
            self.thermal_panel.refresh_components(preserve_user=True)
        settings = self.thermal_panel.get_settings()
        compute_settings = self.runtime_panel.get_settings(persist=True)
        try:
            dc_grid_size = max(0.01, float(self.txt_grid_size.GetValue()))
        except ValueError:
            dc_grid_size = 0.1
        debug_mode = self.chk_debug.GetValue()
        rails = list(self.power_tree.rails)
        board_file_path = self._board_file_path()
        if coupled and not rails:
            raise ValueError("Coupled analysis requires at least one configured power rail.")

        self.btn_run_thermal.Disable()
        self.btn_run_coupled.Disable()
        self._thermal_thread = threading.Thread(
            target=self._thermal_pipeline_worker,
            args=(
                settings, coupled, compute_settings, debug_mode, rails,
                dc_grid_size, board_file_path,
            ),
            name="KiPIDA-Thermal-Pipeline",
            daemon=True,
        )
        self._thermal_thread.start()

    def _thermal_worker_log(self, message):
        if not self._closing:
            wx.CallAfter(self.log, message)

    def _thermal_worker_progress(self, completed, total, detail):
        if not self._closing:
            wx.CallAfter(self.log, f"Thermal progress: {completed}/{total} ({detail})")

    def _thermal_pipeline_worker(
        self,
        settings,
        coupled,
        compute_settings,
        debug_mode,
        rails,
        dc_grid_size,
        board_file_path,
    ):
        try:
            system_results = {}
            if coupled or settings.include_dc_copper_losses:
                self._thermal_worker_log(
                    "Running fresh DC analysis for the current live PCB geometry."
                )
                system_results = self._solve_system(
                    rails, dc_grid_size, debug_mode, compute_settings,
                    log_callback=self._thermal_worker_log,
                )
                if coupled and not system_results:
                    raise ValueError("Coupled analysis requires a successful DC analysis.")

            copper_losses = [] if coupled else (
                self._dc_copper_loss_points(system_results)
                if settings.include_dc_copper_losses else []
            )
            builder = ThermalModelBuilder(
                self.board,
                debug=debug_mode,
                log_callback=self._thermal_worker_log,
                board_file_path=board_file_path,
            )
            model = builder.build(settings, rails=rails, copper_losses=copper_losses)
            mesher = ThermalMesher(
                debug=debug_mode,
                log_callback=self._thermal_worker_log,
                compute_settings=compute_settings,
            )
            mesh = mesher.generate_mesh(
                model,
                settings,
                progress_callback=self._thermal_worker_progress,
            )

            if coupled:
                rail_contexts = {
                    name: {
                        "mesh": data["mesh"],
                        "sources": data.get("sources", []),
                        "loads": data.get("loads", []),
                    }
                    for name, data in system_results.items()
                }
                solver = ElectroThermalSolver(
                    debug=debug_mode,
                    log_callback=self._thermal_worker_log,
                    compute_settings=compute_settings,
                )
                solved = solver.solve(
                    mesh, settings, rail_contexts,
                    progress_callback=self._thermal_worker_progress,
                )
                result = solved.thermal
            else:
                solver = ThermalSolver(
                    debug=debug_mode,
                    log_callback=self._thermal_worker_log,
                    compute_settings=compute_settings,
                )
                solved = None
                result = solver.solve(
                    mesh, ambient_c=settings.ambient_c,
                    progress_callback=self._thermal_worker_progress,
                )
            if not self._closing:
                wx.CallAfter(
                    self._finish_thermal_worker,
                    mesh,
                    result,
                    coupled,
                    solved,
                    system_results,
                )
        except Exception as exc:
            if not self._closing:
                wx.CallAfter(self._fail_thermal_worker, coupled, exc)

    def _finish_thermal_worker(self, mesh, result, coupled, coupled_result, system_results):
        self._thermal_thread = None
        self.btn_run_thermal.Enable()
        self.btn_run_coupled.Enable()
        if system_results:
            self.system_results = system_results
        self.thermal_mesh = mesh
        self.thermal_result = result
        if coupled_result is not None:
            self.electrothermal_result = coupled_result
        self._update_thermal_results_ui(mesh, result, coupled=coupled)

    def _fail_thermal_worker(self, coupled, exc):
        self._thermal_thread = None
        self.btn_run_thermal.Enable()
        self.btn_run_coupled.Enable()
        label = "Coupled Thermal" if coupled else "Thermal"
        self.log(f"{label} Analysis Error: {exc}")
        wx.MessageBox(str(exc), f"{label} Analysis Error", wx.OK | wx.ICON_ERROR)

    def _update_thermal_results_ui(self, mesh, result, coupled=False):
        hotspot = result.hotspot
        lines = [
            "3D Thermal Analysis Results",
            "===========================",
            f"Mode: {'Coupled DC / thermal' if coupled else 'Thermal'}",
            f"Hotspot: {hotspot.temperature_c:.3f} C at "
            f"({hotspot.x_mm:.2f}, {hotspot.y_mm:.2f}, {hotspot.z_mm:.3f}) mm",
            f"Input heat: {result.total_input_power_w:.6g} W",
            f"Boundary heat: {result.total_boundary_power_w:.6g} W",
            f"Energy balance error: {result.energy_balance_error_pct:.4g}%",
            f"Effective h: {result.convection_coefficient_w_m2k:.4g} W/m2K",
            f"Iterations: {result.iterations} ({'converged' if result.converged else 'limit reached'})",
            f"Compute backend: {result.compute_backend} ({result.compute_device})",
            f"CPU threads: {result.compute_cpu_threads}",
            f"Solve time: {result.compute_solve_seconds:.4g} s "
            f"(transfer {result.compute_transfer_seconds:.4g} s)",
            f"Linear residual: {result.compute_relative_residual:.4g} "
            f"({result.compute_iterations} iteration(s))",
            "",
            "Component junction estimates:",
        ]
        if result.component_results:
            for component in result.component_results:
                status = "OK" if component.margin_c >= 0 else "OVER LIMIT"
                lines.append(
                    f"  - {component.ref_des}: Tj={component.junction_temperature_c:.2f} C, "
                    f"P={component.power_w:.4g} W, margin={component.margin_c:.2f} C "
                    f"[{status}; {component.model_source}]"
                )
        else:
            lines.append("  - No mapped component heat source.")
        lines.extend([
            "",
            "Model scope: steady-state 3D solid conduction with convective boundaries; "
            "this is not a volumetric CFD airflow solution.",
        ])
        if result.compute_fallback_reason:
            lines.append(f"Compute fallback: {result.compute_fallback_reason}")
        self._thermal_result_generation += 1
        generation = self._thermal_result_generation
        page = self._publish_results("THERMAL", "\n".join(lines), [])
        page.show_rendering("Rendering thermal plots in background...")
        self.log("Thermal solve complete; rendering plots in background.")

        self._thermal_plot_thread = threading.Thread(
            target=self._render_thermal_plots_worker,
            args=(mesh, result, generation),
            name="KiPIDA-Thermal-Plots",
            daemon=True,
        )
        self._thermal_plot_thread.start()

    def _render_thermal_plots_worker(self, mesh, result, generation):
        try:
            with self._plot_lock:
                plotter = Plotter(debug=False)
                board_bounds = getattr(mesh, "bounds_mm", None)
                plots = [
                    ("Thermal 3D", plotter.plot_thermal_3d(
                        mesh, result, as_png=True, board_bounds=board_bounds,
                    )),
                    ("Top Surface", plotter.plot_thermal_surface(
                        mesh, result, "TOP", as_png=True, board_bounds=board_bounds,
                    )),
                    ("Bottom Surface", plotter.plot_thermal_surface(
                        mesh, result, "BOTTOM", as_png=True, board_bounds=board_bounds,
                    )),
                ]
            if not self._closing:
                wx.CallAfter(self._finish_thermal_plots, generation, plots)
        except Exception as exc:
            if not self._closing:
                wx.CallAfter(self._fail_thermal_plots, generation, exc)

    def _finish_thermal_plots(self, generation, plots):
        self._thermal_plot_thread = None
        if self._closing or generation != self._thermal_result_generation:
            return
        available_plots = [(title, data) for title, data in plots if data]
        if not available_plots:
            self._fail_thermal_plots(
                generation,
                RuntimeError("Matplotlib did not produce any thermal plot."),
            )
            return
        page = self.results_workspace.page_for("THERMAL")
        page.set_plots([
            (title, Plotter.bitmap_from_png(png_bytes))
            for title, png_bytes in available_plots
        ])
        self.log("Thermal result plots ready.")

    def _fail_thermal_plots(self, generation, exc):
        self._thermal_plot_thread = None
        if self._closing or generation != self._thermal_result_generation:
            return
        page = self.results_workspace.page_for("THERMAL")
        current_page = page.plots.GetCurrentPage()
        if current_page:
            labels = [
                child for child in current_page.GetChildren()
                if isinstance(child, wx.StaticText)
            ]
            if labels:
                labels[0].SetLabel("Thermal plot rendering failed; see Log for details.")
                current_page.Layout()
        self.log(f"Thermal plot rendering error: {exc}")

    def _prepare_cfd_analysis(self):
        """Extract all KiCad-dependent data before starting the worker thread."""
        self._refresh_live_board_state()
        settings = self.cfd_panel.get_settings()
        if not self.thermal_panel.settings.components:
            self.thermal_panel.refresh_components(preserve_user=True)
        thermal_settings = self.thermal_panel.get_settings()
        if settings.include_dc_copper_losses and not getattr(self, "system_results", None):
            self.log("No current DC result; running DC analysis before enclosure CFD.")
            self.on_run(None, update_results=False)
        copper_losses = (
            self._dc_copper_loss_points()
            if settings.include_dc_copper_losses and getattr(self, "system_results", None)
            else []
        )
        builder = ThermalModelBuilder(
            self.board,
            debug=self.chk_debug.GetValue(),
            log_callback=self.log,
            board_file_path=self._board_file_path(),
        )
        board_model = builder.build(
            thermal_settings,
            rails=self.power_tree.rails,
            copper_losses=copper_losses,
        )
        if not settings.use_phase3_heat_sources:
            board_model.components = []
            board_model.copper_losses = []
        return settings, board_model

    def _cfd_worker_log(self, message):
        wx.CallAfter(self.log, message)

    def _cfd_worker_progress(self, completed, total, detail):
        wx.CallAfter(self.log, f"CFD progress: {completed}/{total} ({detail})")

    def on_run_cfd(self, event):
        if self._cfd_thread is not None and self._cfd_thread.is_alive():
            self._cfd_cancel_requested = True
            self.btn_run_cfd.Disable()
            self.btn_run_cfd.SetLabel("Cancelling CFD...")
            self.log("Cancellation requested for enclosure CFD.")
            return

        self.notebook.SetSelection(self.PAGE_LOG)
        self.log("--- Starting Phase 4 Enclosure CFD Analysis ---")
        try:
            settings, board_model = self._prepare_cfd_analysis()
        except Exception as exc:
            self.log(f"Enclosure CFD setup error: {exc}")
            wx.MessageBox(str(exc), "Enclosure CFD Setup Error", wx.OK | wx.ICON_ERROR)
            return

        self._cfd_cancel_requested = False
        self.btn_run_cfd.SetLabel("Cancel Enclosure CFD")
        debug_mode = self.chk_debug.GetValue()
        compute_settings = self.runtime_panel.get_settings(persist=True)
        self._cfd_thread = threading.Thread(
            target=self._run_cfd_worker,
            args=(board_model, settings, debug_mode, compute_settings),
            name="KiPIDA-Enclosure-CFD",
            daemon=True,
        )
        self._cfd_thread.start()

    def _run_cfd_worker(self, board_model, settings, debug_mode, compute_settings):
        try:
            solver = ConjugateHeatTransferSolver(
                debug=debug_mode,
                log_callback=self._cfd_worker_log,
                compute_settings=compute_settings,
            )
            mesh, result = solver.solve(
                board_model,
                settings,
                progress_callback=self._cfd_worker_progress,
                cancel_callback=lambda: self._cfd_cancel_requested,
            )
            wx.CallAfter(self._finish_cfd_analysis, mesh, result)
        except Exception as exc:
            wx.CallAfter(self._fail_cfd_analysis, exc)

    def _finish_cfd_analysis(self, mesh, result):
        self._cfd_thread = None
        self.btn_run_cfd.Enable()
        self.btn_run_cfd.SetLabel("Run Enclosure CFD")
        self.cfd_mesh = mesh
        self.cfd_result = result
        self.log(
            f"Enclosure CFD complete: {result.iterations} iterations, "
            f"Vmax={result.maximum_velocity_m_s:.4g} m/s."
        )
        self._update_cfd_results_ui(mesh, result)

    def _fail_cfd_analysis(self, exc):
        self._cfd_thread = None
        self.btn_run_cfd.Enable()
        self.btn_run_cfd.SetLabel("Run Enclosure CFD")
        message = str(exc)
        self.log(f"Enclosure CFD error: {message}")
        if "cancelled" not in message.lower():
            wx.MessageBox(message, "Enclosure CFD Error", wx.OK | wx.ICON_ERROR)

    def _update_cfd_results_ui(self, mesh, result):
        self._result_generation += 1
        lines = [
            "Phase 4 Enclosure CFD Results",
            "=============================",
            "Mode: steady incompressible laminar flow with Boussinesq buoyancy",
            f"Cells: {mesh.cell_count:,} ({mesh.shape[0]} x {mesh.shape[1]} x {mesh.shape[2]})",
            f"Iterations: {result.iterations} ({'converged' if result.converged else 'limit reached'})",
            f"Maximum velocity: {result.maximum_velocity_m_s:.6g} m/s",
            f"Maximum air temperature: {result.maximum_air_temperature_c:.3f} C",
            f"Maximum solid temperature: {result.maximum_solid_temperature_c:.3f} C",
            f"Mapped heat: {result.total_heat_w:.6g} W",
            f"Mass balance error: {result.mass_balance_error_pct:.4g}%",
            f"Energy balance error: {result.energy_balance_error_pct:.4g}%",
            f"Compute backend: {result.compute_backend} ({result.compute_device})",
            f"Last energy solve: {result.compute_solve_seconds:.4g} s, "
            f"residual {result.compute_relative_residual:.4g}",
            "",
            "Model scope: structured volumetric CFD, boundary-patch fans/vents, and "
            "conjugate solid-air heat transfer. Fan blades, turbulence, radiation, "
            "and transient effects are outside this Phase 4 solver.",
        ]
        plotter = Plotter(debug=self.chk_debug.GetValue())
        plots = [
            ("CFD 3D", plotter.plot_cfd_3d(mesh, result)),
            ("Temperature XY", plotter.plot_cfd_slice(mesh, result, "TEMPERATURE", "XY")),
            ("Temperature XZ", plotter.plot_cfd_slice(mesh, result, "TEMPERATURE", "XZ")),
            ("Velocity XY", plotter.plot_cfd_slice(mesh, result, "VELOCITY", "XY")),
            ("Pressure XY", plotter.plot_cfd_slice(mesh, result, "PRESSURE", "XY")),
            ("Residuals", plotter.plot_cfd_residuals(result)),
        ]
        self._publish_results("CFD", "\n".join(lines), [(title, bitmap) for title, bitmap in plots if bitmap])

    def _debug_plot_geo(self, extractor, geo):
        try:
            buf = extractor.plot_geometry(geo)
            if buf:
                img = wx.Image(buf, wx.BITMAP_TYPE_PNG)
                self._publish_results("DEBUG", "Geometry debug plot", [("Geometry Debug", wx.Bitmap(img))])
        except: pass

    def _debug_plot_mesh(self, mesher, mesh, stackup):
        try:
            plotter = Plotter(debug=self.chk_debug.GetValue())
            bmp = plotter.plot_3d_mesh(mesh, stackup)
            if bmp:
                self._publish_results("DEBUG", "Mesh debug plot", [("Mesh Debug", bmp)])
        except: pass

    def _update_results_ui(self):
        self._result_generation += 1
        # Populate text stats
        txt = "System Simulation Results:\n==========================\n"
        for net, data in self.system_results.items():
            vmin, vmax, drop = data['stats']
            txt += f"Rail: {net}\n"
            txt += f"  Range: {vmin:.4f} - {vmax:.4f} V\n"
            txt += f"  Drop:  {drop:.4f} V\n\n"
            actual_grid = data.get('grid_size_mm')
            requested_grid = data.get('requested_grid_size_mm', actual_grid)
            if actual_grid is not None:
                suffix = " (adapted for mesh safety)" if data.get('adaptive_grid') else ""
                txt += f"  DC grid: {actual_grid:.4g} mm{suffix}\n"
                if data.get('adaptive_grid'):
                    txt += f"  Requested DC grid: {requested_grid:.4g} mm\n"
            compute = data.get('compute_metadata')
            if compute is not None:
                txt += (
                    f"  Backend: {compute.backend} ({compute.device}), "
                    f"solve {compute.solve_seconds:.4g} s, "
                    f"residual {compute.relative_residual:.3g}\n\n"
                )
        page = self._publish_results("DC", txt, [])
        results_notebook = page.plots
        
        if not self.system_results:
            return
        
        # Get stackup once
        try:
            extractor = GeometryExtractor(self.board)
            stackup = extractor.get_board_stackup()
            board_bounds = extractor.get_board_bounds(board_file_path=self._board_file_path())
        except: 
            stackup = None
            board_bounds = None
        
        # Get Drop % from UI for coloring scale
        try:
            drop_pct_ui = float(self.txt_drop_pct.GetValue())
            if drop_pct_ui < 0: drop_pct_ui = 0
            if drop_pct_ui > 100: drop_pct_ui = 100
        except:
            drop_pct_ui = 5.0
        
        debug_mode = self.chk_debug.GetValue()
        plotter = Plotter(debug=debug_mode)
        
        # Create nested tabs for each rail
        for rail_name, data in self.system_results.items():
            # Create rail-level notebook
            rail_notebook = wx.Notebook(results_notebook)
            
            mesh = data['mesh']
            mesh.results = data['results']
            vmin, vmax, _ = data['stats']
            
            # Override vmin for plot based on drop %
            nominal = vmax
            plot_vmin = nominal * (1.0 - drop_pct_ui / 100.0)
            
            # Add 3D plot tab
            bmp_3d = plotter.plot_3d_mesh(mesh, stackup, vmin=plot_vmin, vmax=vmax, board_bounds=board_bounds)
            if bmp_3d:
                rail_notebook.AddPage(ZoomableBitmapPanel(rail_notebook, bmp_3d), "3D View")
            
            # Add layer tabs
            unique_layers = list(set(n[2] for n in mesh.node_coords.values()))
            unique_layers.sort()
            
            for lid in unique_layers:
                # Get Layer Name
                l_name = str(lid)
                if stackup and 'copper' in stackup and lid in stackup['copper']:
                    l_name = stackup['copper'][lid].get('name', str(lid))
                
                bmp_2d = plotter.plot_layer_2d(mesh, lid, stackup, vmin=plot_vmin, vmax=vmax, layer_name=l_name, board_bounds=board_bounds)
                if bmp_2d:
                    rail_notebook.AddPage(ZoomableBitmapPanel(rail_notebook, bmp_2d), l_name)
            
            # Add rail notebook as a page in the main results notebook
            results_notebook.AddPage(rail_notebook, rail_name)
        
        # Switch to Results tab
        self.notebook.SetSelection(self.PAGE_RESULTS)

    def on_close(self, event):
        if self._cfd_thread is not None and self._cfd_thread.is_alive():
            self._cfd_cancel_requested = True
            self.btn_run_cfd.Disable()
            self.btn_run_cfd.SetLabel("Cancelling CFD...")
            self.log("Close requested; cancelling enclosure CFD first.")
            return
        self._closing = True
        self._result_generation += 1
        self._thermal_result_generation += 1
        self.EndModal(wx.ID_CANCEL)
