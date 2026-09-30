"""UI-independent orchestration for thermal and coupled DC/thermal analyses."""

import copy
from dataclasses import dataclass, replace
import time
from typing import Any, Callable, Dict, Optional, Tuple

from application.dc_controller import DefaultDCSolverEngine, unsolved_rails_reason
from cfd_model import enclosure_air_rise_k
from electrothermal import ElectroThermalSolver
from thermal_mesh import ThermalMesher
from thermal_model import CopperLossPoint
from physical_constants import COPPER_RESISTIVITY_REFERENCE_C, COPPER_TEMPERATURE_COEFFICIENT_PER_C
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
    # Free-stream speed resolved by a preceding enclosure CFD run, in m/s.
    # None keeps the configured airflow; a value overrides it for the surface
    # coefficients. It is part of thermal_cache_key, because two runs that
    # differ only by this number have genuinely different surface physics and
    # must not share a cached mesh.
    air_velocity_m_s: Optional[float] = None
    # An enclosure CFD profile (models.EnclosureCFDSettings), or None to
    # solve at the configured ambient with no enclosure correction (COH-4).
    # Part of no cache key of its own: it only ever acts by shifting
    # settings.ambient_c below, which thermal_cache_key already hashes.
    enclosure: Any = None
    # A DC result to take copper losses from instead of solving the DC again,
    # and what it is -- a campaign hands over the one its DC domain produced
    # on the same capture. A coupled run takes it as the ambient pass its
    # iterations start from. Private: dc_request already determines it, so the
    # campaign cache digest leaves it out rather than hashing meshes.
    _dc_results: Optional[Dict[str, Any]] = None
    _dc_results_source: str = ""


@dataclass(frozen=True)
class ThermalRunOutcome:
    mesh: Any
    result: Any
    coupled_result: Any
    system_results: Dict[str, Any]
    cache_key: Any
    cache_value: Any
    elapsed_seconds: float
    # Where the copper-loss heat sources came from; empty when there were none.
    copper_loss_source: str = ""
    # The hotspot solved again at twice the grid step, when the settings ask:
    # {"fine_grid_mm", "coarse_grid_mm", "fine_hotspot_c", "coarse_hotspot_c",
    # "delta_c"}. None when not computed.
    mesh_sensitivity: Optional[Dict[str, float]] = None
    # COH-4: the internal air rise applied from an enclosure profile (K), or
    # None when no enclosure was handed to this run.
    enclosure_air_rise_k: Optional[float] = None
    # Whether that rise was actually folded into the ambient this run solved
    # at, as opposed to computed-but-disabled (settings.apply_enclosure_ambient).
    enclosure_ambient_applied: bool = False


def describe_dc_source(system_results, origin):
    """``origin``, with the DC engine that produced *system_results* named."""
    engines = sorted({
        str(data.get("engine") or "raster") for data in (system_results or {}).values()
    })
    return f"{origin} ({', '.join(engines)} mesh)" if engines else origin


@dataclass(frozen=True)
class ThermalControllerCallbacks:
    on_progress: Callable[[int, int, str], None]
    on_complete: Callable[[ThermalRunOutcome], None]
    on_error: Callable[[Exception], None]
    on_log: Callable[[str], None] = lambda _message: None


def thermal_cache_key(settings, compute_settings, coupled, copper_losses,
                      air_velocity_m_s=None):
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
        None if air_velocity_m_s is None else float(air_velocity_m_s),
    )
    runtime = (
        str(getattr(compute_settings, "backend", "AUTO")),
        bool(getattr(compute_settings, "cuda_enabled", False)),
        int(getattr(compute_settings, "cuda_device", 0) or 0),
        int(getattr(compute_settings, "cpu_threads", 0) or 0),
    )
    return bool(coupled), thermal, runtime


def _ground_solutions(system_results):
    """``{context name: GroundReturn}`` for the ground return the rails share."""
    grounds = {}
    for data in system_results.values():
        ground = data.get("ground_solution")
        if ground is not None:
            grounds[f"{ground.net_name} (ground return)"] = ground
    return grounds


def dc_copper_loss_points(system_results, resistance_scale=1.0):
    """Branch losses as heat points, scaled to the copper's temperature.

    The DC is solved at the resistivity reference, 20 C. An uncoupled thermal
    run at another ambient takes those losses as they are: on P02 Rev E, at
    45 C, about 10 % low (0.00393/K x 25 K), and unstated (TH-10). With the
    load currents fixed, I^2 R scales with R, so *resistance_scale* is the
    copper's resistance ratio at the ambient.
    """
    losses = []
    # The ground return, solved once and shared by every rail's entry, heats
    # the board too.
    solutions = list(system_results.values()) + [
        {"mesh": ground.mesh, "detailed_result": ground.detailed_result}
        for ground in _ground_solutions(system_results).values()
    ]
    for data in solutions:
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
                power_w=power * resistance_scale,
            ))
    return losses


def enclosure_internal_air_rise_k(enclosure, heat_w):
    """Steady mean rise of a sealed enclosure's internal air above ambient.

    The CFD report's own lumped balance, Q = h * A_walls * rise
    (cfd_model.enclosure_air_rise_k), so both analyses state one number.
    On P02 Rev E's sealed 165 x 100 x 50 mm box (walls 5 W/m2K, ~4.9 W of
    board heat) it is 16.5 K, the audit's measured ~16 K (CFD-4).
    """
    return enclosure_air_rise_k(enclosure, heat_w)


def _estimate_total_heat_w(model, coupled, system_results):
    """Total heat the board dumps into its surroundings, without solving.

    Used only to size the enclosure air-rise correction before the thermal
    solve runs, so it must not itself require one. Component power is
    already known from the model; copper loss is either the coupled model's
    ambient DC pass (its per-branch losses, not yet redistributed by
    temperature) or the uncoupled model's own copper_losses list.
    """
    component_heat_w = sum(
        max(0.0, float(component.power_w))
        for component in model.components if component.enabled
    )
    if coupled:
        copper_heat_w = sum(
            sum(max(0.0, float(power)) for power in
                getattr(data.get("detailed_result"), "branch_losses_w", None) or ())
            for data in (system_results or {}).values()
        )
    else:
        copper_heat_w = sum(max(0.0, float(loss.power_w)) for loss in model.copper_losses)
    return component_heat_w + copper_heat_w


class ThermalSolverEngine:
    def __init__(
        self, dc_engine_factory=DefaultDCSolverEngine, mesher_factory=ThermalMesher,
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
        copper_loss_source = ""
        handed_over = getattr(request, "_dc_results", None)
        needs_dc = request.coupled or request.settings.include_dc_copper_losses
        if needs_dc and handed_over:
            system_results = handed_over
            source = request._dc_results_source or "a DC result handed to this run"
            if request.coupled:
                # The coupling's first pass is the ambient DC this result
                # already is; every iteration after it re-solves the same
                # meshes against the temperature field, reading them only.
                # COH-11: copper-loss provenance used to stay unpublished for
                # every coupled run, although the coupling's own per-rail
                # solves are exactly where those losses come from.
                copper_loss_source = describe_dc_source(
                    system_results, f"the coupled per-iteration DC solve, seeded from {source}"
                )
                emit(f"The coupling starts from {source}; the ambient DC is not solved again.")
            else:
                copper_loss_source = describe_dc_source(system_results, source)
                emit(f"Copper losses taken from {copper_loss_source}; the DC is not solved again.")
        elif needs_dc:
            if request.dc_request is None:
                raise ValueError("The thermal run is missing its captured DC request.")
            emit("Running fresh DC analysis from the captured live-PCB snapshot.")
            system_results = self._dc_engine_factory().solve(
                request.dc_request, emit, checked_progress, cancelled,
            )
            if request.coupled and not system_results:
                raise ValueError(
                    "Coupled analysis requires a successful DC analysis, and no rail was solved. "
                    + unsolved_rails_reason(getattr(request.dc_request, "rails", ()) or ())
                )
            if request.coupled:
                copper_loss_source = describe_dc_source(
                    system_results, "the coupled per-iteration DC solve"
                )
            else:
                copper_loss_source = describe_dc_source(system_results, "a DC solve run for this analysis")
        if cancelled():
            raise ThermalAnalysisCancelled("Thermal analysis cancelled.")

        resistance_scale = max(0.2, 1.0 + float(getattr(
            request.settings, "copper_temp_coefficient_per_c", COPPER_TEMPERATURE_COEFFICIENT_PER_C,
        )) * (
            float(getattr(request.settings, "ambient_c", COPPER_RESISTIVITY_REFERENCE_C))
            - COPPER_RESISTIVITY_REFERENCE_C
        ))
        copper_losses = [] if request.coupled else (
            dc_copper_loss_points(system_results, resistance_scale)
            if request.settings.include_dc_copper_losses else []
        )
        if copper_losses and abs(resistance_scale - 1.0) > 1e-9:
            emit(
                f"Copper losses scaled x{resistance_scale:.3f} from the DC's "
                f"{COPPER_RESISTIVITY_REFERENCE_C:g} C resistivity to the "
                f"{float(request.settings.ambient_c):g} C ambient."
            )
        model = replace(request.board_model, copper_losses=list(copper_losses))

        # COH-4: a sealed enclosure raises the air the board actually sits in
        # above the ambient its walls see outside; thermal used to ignore an
        # enclosure entirely and solve at that bare outside ambient. The
        # heat used to size the rise is a pre-solve estimate (component
        # power plus the ambient DC pass's copper loss), not a converged
        # one; the correction folds into settings.ambient_c below, so every
        # later use of request.settings -- meshing, the solve itself, the
        # cache key, mesh sensitivity -- sees it for free.
        enclosure = getattr(request, "enclosure", None)
        enclosure_air_rise_k = None
        outside_ambient_c = float(request.settings.ambient_c)
        ambient_for_heat_w = None
        if enclosure is not None:
            heat_w = _estimate_total_heat_w(model, request.coupled, system_results)
            enclosure_air_rise_k = enclosure_internal_air_rise_k(enclosure, heat_w)
            if getattr(request.settings, "apply_enclosure_ambient", True):
                elevated_settings = copy.copy(request.settings)
                elevated_settings.ambient_c = float(request.settings.ambient_c) + enclosure_air_rise_k
                request = replace(request, settings=elevated_settings)
                # The coupled loop balances the air again on the heat each
                # iteration deposits. P02 Rev E, 2026-09-30 13:52: 4.96 W
                # estimated here, 5.18 W converged, the air 0.7 K cooler
                # than its own balance -- a quarter of U15's 2.7 K margin.
                def ambient_for_heat_w(heat_w):
                    return outside_ambient_c + enclosure_internal_air_rise_k(enclosure, heat_w)
                emit(
                    "Enclosure configured (sealed box, wall h="
                    f"{float(enclosure.geometry.wall_heat_transfer_w_m2k):.3g} W/m2K): "
                    f"solving at internal air {elevated_settings.ambient_c:.3g} C (ambient + "
                    f"{enclosure_air_rise_k:.3g} K from the enclosure heat balance, "
                    f"{heat_w:.3g} W estimated)."
                )
            else:
                emit(
                    "Enclosure is configured but its ambient-rise correction is disabled "
                    f"(apply_enclosure_ambient=False): internal air would run "
                    f"{enclosure_air_rise_k:.3g} K warmer than the configured ambient if enabled."
                )

        key = (
            request.board_signature,
            thermal_cache_key(
                request.settings, request.compute_settings, request.coupled, copper_losses,
                request.air_velocity_m_s,
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
                air_velocity_m_s=request.air_velocity_m_s,
            )
            if request.air_velocity_m_s:
                emit(
                    "Surface coefficients use the CFD free-stream speed of "
                    f"{float(request.air_velocity_m_s):.4g} m/s (estimated, not measured)."
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
            for name, ground in _ground_solutions(system_results).items():
                rail_contexts[name] = {
                    "mesh": ground.mesh, "sources": ground.sources, "loads": ground.loads,
                    "initial_voltages": ground.detailed_result.voltages,
                }
            coupled_solver = self._coupled_solver_factory(
                debug=request.debug, log_callback=emit,
                compute_settings=request.compute_settings,
                thermal_solver=thermal_solver,
            )
            coupled_result = coupled_solver.solve(
                mesh, request.settings, rail_contexts,
                progress_callback=checked_progress, rails=list(request.rails),
                **({"ambient_for_heat_w": ambient_for_heat_w} if ambient_for_heat_w else {}),
            )
            result = coupled_result.thermal
            if ambient_for_heat_w is not None:
                balanced_rise_k = float(request.settings.ambient_c) - outside_ambient_c
                emit(
                    "Enclosure air balanced on the coupled heat, "
                    f"{float(coupled_result.total_heat_w):.3g} W: internal air "
                    f"{float(request.settings.ambient_c):.3g} C (ambient + {balanced_rise_k:.3g} K, "
                    f"{balanced_rise_k - enclosure_air_rise_k:+.2g} K on the estimate)."
                )
                enclosure_air_rise_k = balanced_rise_k
        else:
            coupled_result = None
            # Natural convection and radiation both depend on the surface
            # temperature being solved for, so the coefficients are refined
            # against the solution instead of staying at their nominal values.
            result = thermal_solver.solve_with_surface_refinement(
                mesh, request.settings,
                progress_callback=checked_progress,
            )
        if cancelled():
            raise ThermalAnalysisCancelled("Thermal analysis cancelled.")
        mesh_sensitivity = None
        if getattr(request.settings, "mesh_sensitivity_check", False):
            if request.coupled:
                # A campaign always runs this path (ui/main_dialog.py:497), so
                # the double-step error bar must not silently stop existing
                # here -- it only used to run when a plain, non-campaign
                # thermal analysis was requested (TH-3 / COH-6, old R7).
                mesh_sensitivity = self._coupled_mesh_sensitivity(
                    model, request, result, coupled_result, system_results, emit, cancelled,
                )
            else:
                mesh_sensitivity = self._mesh_sensitivity(model, request, result, emit, cancelled)
        return ThermalRunOutcome(
            mesh=mesh,
            result=result,
            coupled_result=coupled_result,
            system_results=system_results,
            cache_key=key,
            cache_value=(mesh, thermal_solver),
            elapsed_seconds=time.perf_counter() - started,
            copper_loss_source=copper_loss_source,
            mesh_sensitivity=mesh_sensitivity,
            enclosure_air_rise_k=enclosure_air_rise_k,
            enclosure_ambient_applied=(
                enclosure_air_rise_k is not None
                and bool(getattr(request.settings, "apply_enclosure_ambient", True))
            ),
        )

    def _coupled_mesh_sensitivity(
        self, model, request, result, coupled_result, system_results, emit, cancelled,
    ):
        """The coupled equivalent of :meth:`_mesh_sensitivity`.

        A coupled run does not end on the static model: component power was
        redistributed by temperature (electrothermal.py's dynamic stages) and
        copper losses came from the final per-rail DC solve, not the ambient
        one the loop started from. Both are frozen from the converged
        iteration -- component power is already mutated in place on
        ``model.components`` by the coupled loop; copper losses are rebuilt
        from ``coupled_result.dc_results`` -- and handed to the same coarse
        re-solve the uncoupled path uses, so the check measures the run that
        was actually reported rather than a plain thermal approximation of it.

        Returns ``{"not_verified_reason": ...}`` instead of raising when the
        frozen re-solve cannot be completed, so a campaign (which always runs
        coupled) still publishes an honest "not verified" rather than either
        crashing or silently dropping the error bar.
        """
        try:
            dc_results = dict(getattr(coupled_result, "dc_results", {}) or {})
            meshes = {name: data.get("mesh") for name, data in (system_results or {}).items()}
            meshes.update({
                name: ground.mesh for name, ground in _ground_solutions(system_results or {}).items()
            })
            combined = {
                name: {"mesh": mesh, "detailed_result": dc_results.get(name)}
                for name, mesh in meshes.items()
                if dc_results.get(name) is not None
            }
            frozen_model = replace(model, copper_losses=dc_copper_loss_points(combined))
            return self._mesh_sensitivity(frozen_model, request, result, emit, cancelled)
        except ThermalAnalysisCancelled:
            raise
        except Exception as exc:
            emit(f"Mesh sensitivity check not verified for the coupled result: {exc}")
            return {"not_verified_reason": str(exc)}

    def _mesh_sensitivity(self, model, request, result, emit, cancelled):
        """Solve at twice the grid step and measure how far the hotspot moves.

        The thermal result carried no error bar; this is the cheapest one it
        can have, since a mesh twice as coarse costs a fraction of the solve
        just done. Its progress stays out of the progress bar, and its log
        lines are marked, so the run the user asked for still reads as one.
        """
        hotspot = getattr(result, "hotspot", None)
        if hotspot is None:
            return None
        fine_step = float(getattr(result, "effective_grid_size_mm", 0.0) or 0.0) or float(
            request.settings.grid_size_mm
        )
        coarse_settings = copy.copy(request.settings)
        coarse_settings.grid_size_mm = fine_step * 2.0

        def quiet(*_args):
            if cancelled():
                raise ThermalAnalysisCancelled("Thermal analysis cancelled.")

        def marked(message):
            emit(f"[mesh sensitivity, {fine_step * 2.0:g} mm] {message}")

        emit(f"Checking mesh sensitivity: solving again at {fine_step * 2.0:g} mm.")
        coarse_mesh = self._mesher_factory(
            debug=request.debug, log_callback=marked, compute_settings=request.compute_settings,
        ).generate_mesh(
            model, coarse_settings, progress_callback=quiet,
            air_velocity_m_s=request.air_velocity_m_s,
        )
        coarse = self._solver_factory(
            debug=request.debug, log_callback=marked, compute_settings=request.compute_settings,
        ).solve_with_surface_refinement(coarse_mesh, coarse_settings, progress_callback=quiet)
        coarse_hotspot = getattr(coarse, "hotspot", None)
        if coarse_hotspot is None:
            return None
        coarse_step = float(getattr(coarse, "effective_grid_size_mm", 0.0) or 0.0) or fine_step * 2.0
        delta = abs(float(hotspot.temperature_c) - float(coarse_hotspot.temperature_c))
        emit(
            f"Hotspot {float(hotspot.temperature_c):.2f} C at {fine_step:g} mm, "
            f"{float(coarse_hotspot.temperature_c):.2f} C at {coarse_step:g} mm: {delta:.2f} C apart."
        )
        return {
            "fine_grid_mm": fine_step,
            "coarse_grid_mm": coarse_step,
            "fine_hotspot_c": float(hotspot.temperature_c),
            "coarse_hotspot_c": float(coarse_hotspot.temperature_c),
            "delta_c": delta,
        }


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
