"""UI-independent orchestration for thermal and coupled DC/thermal analyses."""

from dataclasses import dataclass, replace
import time
from typing import Any, Callable, Dict, Optional, Tuple

from application.dc_controller import DCSolverEngine
from electrothermal import ElectroThermalSolver
from thermal_mesh import ThermalMesher
from thermal_model import CopperLossPoint
from thermal_solver import ThermalSolver
from application.background_controller import BackgroundAnalysisController


class ThermalAnalysisCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ThermalRunRequest:
    settings: Any
    board_model: Any
    rails: Tuple[Any, ...] = ()
    compute_settings: Any = None
    debug: bool = False
    coupled: bool = False
    dc_request: Any = None
    board_signature: Any = None
    cached_entries: Optional[Dict[Any, Any]] = None


@dataclass(frozen=True)
class ThermalRunOutcome:
    mesh: Any
    result: Any
    coupled_result: Any
    system_results: Dict[str, Any]
    cache_key: Any
    cache_value: Any
    elapsed_seconds: float


@dataclass(frozen=True)
class ThermalControllerCallbacks:
    on_progress: Callable[[int, int, str], None]
    on_complete: Callable[[ThermalRunOutcome], None]
    on_error: Callable[[Exception], None]
    on_log: Callable[[str], None] = lambda _message: None


def thermal_cache_key(settings, compute_settings, coupled, copper_losses):
    airflow = settings.airflow
    components = tuple(sorted(
        (
            component.ref_des, float(component.power_w), float(component.width_mm),
            float(component.depth_mm), float(component.height_mm),
            float(component.theta_jb_c_per_w), float(component.max_junction_c),
            bool(component.enabled), str(component.model_source),
        )
        for component in settings.components
    ))
    losses = tuple(sorted(
        (float(loss.x_mm), float(loss.y_mm), int(loss.layer_id), float(loss.power_w))
        for loss in copper_losses
    ))
    thermal = (
        float(settings.grid_size_mm), float(settings.ambient_c),
        bool(settings.include_radiation), float(settings.emissivity),
        bool(settings.include_dc_copper_losses), str(airflow.mode),
        float(airflow.velocity_m_s), float(airflow.direction_deg),
        float(airflow.custom_h_w_m2k), bool(airflow.expose_top),
        bool(airflow.expose_bottom), bool(airflow.expose_edges), components, losses,
    )
    runtime = (
        str(getattr(compute_settings, "backend", "AUTO")),
        bool(getattr(compute_settings, "cuda_enabled", False)),
        int(getattr(compute_settings, "cuda_device", 0) or 0),
        int(getattr(compute_settings, "cpu_threads", 0) or 0),
    )
    return bool(coupled), thermal, runtime


def dc_copper_loss_points(system_results):
    losses = []
    for data in system_results.values():
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


class ThermalSolverEngine:
    def __init__(
        self, dc_engine_factory=DCSolverEngine, mesher_factory=ThermalMesher,
        solver_factory=ThermalSolver, coupled_solver_factory=ElectroThermalSolver,
    ):
        self._dc_engine_factory = dc_engine_factory
        self._mesher_factory = mesher_factory
        self._solver_factory = solver_factory
        self._coupled_solver_factory = coupled_solver_factory

    def solve(self, request, emit, progress, cancelled):
        started = time.perf_counter()

        def checked_progress(completed, total, detail):
            if cancelled():
                raise ThermalAnalysisCancelled("Thermal analysis cancelled.")
            progress(completed, total, str(detail))

        system_results = {}
        if request.coupled or request.settings.include_dc_copper_losses:
            if request.dc_request is None:
                raise ValueError("The thermal run is missing its captured DC request.")
            emit("Running fresh DC analysis from the captured live-PCB snapshot.")
            system_results = self._dc_engine_factory().solve(
                request.dc_request, emit, checked_progress, cancelled,
            )
            if request.coupled and not system_results:
                raise ValueError("Coupled analysis requires a successful DC analysis.")
        if cancelled():
            raise ThermalAnalysisCancelled("Thermal analysis cancelled.")

        copper_losses = [] if request.coupled else (
            dc_copper_loss_points(system_results)
            if request.settings.include_dc_copper_losses else []
        )
        model = replace(request.board_model, copper_losses=list(copper_losses))
        key = (
            request.board_signature,
            thermal_cache_key(
                request.settings, request.compute_settings, request.coupled, copper_losses,
            ),
        )
        cached = (request.cached_entries or {}).get(key)
        if cached is not None:
            mesh, thermal_solver = cached
            emit(
                f"Reusing in-session thermal mesh ({len(mesh.nodes):,} nodes) "
                "and cached CSR/CUDA workspace."
            )
        else:
            mesher = self._mesher_factory(
                debug=request.debug, log_callback=emit,
                compute_settings=request.compute_settings,
            )
            mesh = mesher.generate_mesh(
                model, request.settings, progress_callback=checked_progress,
            )
            thermal_solver = self._solver_factory(
                debug=request.debug, log_callback=emit,
                compute_settings=request.compute_settings,
            )
            emit("Prepared thermal mesh and sparse solver workspace for session reuse.")
        if cancelled():
            raise ThermalAnalysisCancelled("Thermal analysis cancelled.")

        if request.coupled:
            rail_contexts = {
                name: {
                    "mesh": data["mesh"],
                    "sources": data.get("sources", []),
                    "loads": data.get("loads", []),
                    "initial_voltages": data.get("results", {}),
                }
                for name, data in system_results.items()
            }
            coupled_solver = self._coupled_solver_factory(
                debug=request.debug, log_callback=emit,
                compute_settings=request.compute_settings,
                thermal_solver=thermal_solver,
            )
            coupled_result = coupled_solver.solve(
                mesh, request.settings, rail_contexts,
                progress_callback=checked_progress, rails=list(request.rails),
            )
            result = coupled_result.thermal
        else:
            coupled_result = None
            result = thermal_solver.solve(
                mesh, ambient_c=request.settings.ambient_c,
                progress_callback=checked_progress,
            )
        if cancelled():
            raise ThermalAnalysisCancelled("Thermal analysis cancelled.")
        return ThermalRunOutcome(
            mesh=mesh,
            result=result,
            coupled_result=coupled_result,
            system_results=system_results,
            cache_key=key,
            cache_value=(mesh, thermal_solver),
            elapsed_seconds=time.perf_counter() - started,
        )


class ThermalAnalysisController(BackgroundAnalysisController):
    def __init__(self, dispatch=lambda callback, *args: callback(*args), engine=None):
        super().__init__(
            engine or ThermalSolverEngine(),
            thread_name="KiPIDA-Thermal-Analysis",
            busy_message="A thermal analysis is already running.",
            cancelled_error_factory=lambda: ThermalAnalysisCancelled(
                "Thermal analysis cancelled."
            ),
            dispatch=dispatch,
        )
