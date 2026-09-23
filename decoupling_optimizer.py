"""Deterministic target-impedance decoupling optimization."""

from dataclasses import replace

try:
    import numpy as np
except ImportError:
    np = None

try:
    from .models import (
        DecouplingOptimizationResult,
        OptimizationRecommendation,
    )
except (ImportError, ValueError):
    from models import DecouplingOptimizationResult, OptimizationRecommendation


class DecouplingOptimizer:
    """Greedily populate existing/DNP capacitor footprints.

    The optimizer never invents placement locations and never writes the PCB.
    Each recommendation is tied to a capacitor footprint already present in the
    AC network.
    """

    def __init__(self, solver, debug=False, log_callback=None):
        self.solver = solver
        self.debug = debug
        self.log_callback = log_callback

    def _log(self, message):
        if self.log_callback:
            self.log_callback(f"[DECAP OPT] {message}")

    @staticmethod
    def _score(result, target):
        magnitudes = np.abs(np.asarray(result.impedance_ohm))
        if target > 0:
            ratios = magnitudes / target
            return float(np.max(ratios) + 0.05 * np.mean(np.maximum(ratios - 1.0, 0.0)))
        return float(np.max(magnitudes))

    @staticmethod
    def _clone_capacitors(capacitors):
        return [replace(capacitor) for capacitor in capacitors]

    def optimize(self, network, settings, progress_callback=None):
        # Every trial below still calls solve_sweep_multiport from scratch
        # (fresh Python _connectivity, fresh stamps, a fresh LU
        # factorisation per frequency point) rather than reusing the current
        # LU as a preconditioner across trials, though adding one capacitor
        # is a low-rank update to the network. AC-9 asked for that reuse;
        # it is NOT implemented here -- it needs ACSolver's recycled-LU
        # machinery to accept a factorisation carried across capacitor
        # configurations, not just across frequency points, which is a
        # solver-level change out of scope for this fix. Cost stays
        # max_additions x N_DNP x N_values solve_sweep_multiport calls.
        if np is None:
            raise ImportError("NumPy is required for decoupling optimization.")

        # solve_sweep_multiport sweeps every candidate observation point and
        # scores the worst one, the same way the published AC result does
        # (AC-4's multiport ports). solve_sweep only ever looked at
        # network.measurement, so on a rail with several candidate ports the
        # optimizer's baseline/score could read PASS while the published,
        # worst-port result read FAIL (AC-9). A network with a single port
        # equal to network.measurement (every rail before AC-4, and every
        # existing caller here) delegates straight through to solve_sweep and
        # returns identical numbers, so this is a no-op change for those.
        baseline_caps = self._clone_capacitors(settings.capacitors)
        baseline = self.solver.solve_sweep_multiport(network, settings, capacitors=baseline_caps)
        target = max(0.0, float(settings.target_impedance_ohm))
        if baseline.meets_target:
            return DecouplingOptimizationResult(
                baseline=baseline,
                optimized=baseline,
                recommendations=[],
                reached_target=True,
            )

        working = self._clone_capacitors(baseline_caps)
        available_refs = sorted(
            capacitor.ref_des for capacitor in working
            if not capacitor.enabled and capacitor.ref_des in network.capacitor_nodes
        )
        values = sorted(set(float(value) for value in settings.optimizer_values_f if value > 0))
        max_additions = max(0, int(settings.optimizer_max_additions))
        recommendations = []

        coarse_settings = replace(settings, frequency_points=min(41, settings.frequency_points))
        current_result = self.solver.solve_sweep_multiport(network, coarse_settings, capacitors=working)
        current_score = self._score(current_result, target)
        total_trials = max(1, max_additions * max(1, len(available_refs)) * max(1, len(values)))
        completed_trials = 0

        for _ in range(max_additions):
            best = None
            best_result = None
            best_score = current_score

            for ref_des in list(available_refs):
                for capacitance in values:
                    trial = self._clone_capacitors(working)
                    selected = next(cap for cap in trial if cap.ref_des == ref_des)
                    selected.enabled = True
                    selected.candidate = False
                    selected.capacitance_f = capacitance
                    result = self.solver.solve_sweep_multiport(network, coarse_settings, capacitors=trial)
                    score = self._score(result, target)
                    completed_trials += 1
                    if progress_callback:
                        progress_callback(completed_trials, total_trials, ref_des)

                    key = (score, ref_des, capacitance)
                    if best is None or key < best[0]:
                        best = (key, ref_des, capacitance, trial)
                        best_result = result
                        best_score = score

            if best is None or best_score >= current_score * (1.0 - 1e-9):
                self._log("No remaining candidate improves the target-impedance score.")
                break

            _, ref_des, capacitance, working = best
            available_refs.remove(ref_des)
            current_result = best_result
            current_score = best_score
            recommendations.append(OptimizationRecommendation(
                ref_des=ref_des,
                capacitance_f=capacitance,
                action="populate",
            ))
            self._log(f"Selected {ref_des} = {capacitance:g} F (score {current_score:.4g}).")

            if target > 0 and current_result.meets_target:
                break
            if not available_refs:
                break

        optimized = self.solver.solve_sweep_multiport(network, settings, capacitors=working)
        return DecouplingOptimizationResult(
            baseline=baseline,
            optimized=optimized,
            recommendations=recommendations,
            reached_target=optimized.meets_target,
        )
