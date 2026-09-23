"""A campaign solves the DC once; thermal and CFD take it from there.

Both used to solve the same DC again from their own capture of the same board
in the same campaign, for the same losses (audit R8). A coupled run takes it
too, as the ambient pass its iterations start from -- 87 s of a 349 s campaign
on P02 Rev E. A capture that differs keeps its own solve.
"""

import os
import sys
import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from analysis_contract import AnalysisResult, AnalysisStatus  # noqa: E402
from analysis_registry import AnalysisDescriptor, AnalysisRegistry  # noqa: E402
from application.campaign_controller import CampaignEngine, CampaignRunRequest  # noqa: E402
from mesh import Mesh  # noqa: E402
from models import DCSolveResult  # noqa: E402


def _dc_results(engine="hybrid"):
    mesh = Mesh()
    mesh.nodes = [0, 1]
    mesh.node_coords = {0: (1.0, 2.0, 3), 1: (3.0, 2.0, 3)}
    mesh.add_edge_direct(0, 1, 10.0)
    return {"VCC": {
        "mesh": mesh, "engine": engine,
        "detailed_result": DCSolveResult(branch_currents_a=[1.0], branch_losses_w=[0.25]),
    }}


def _settings(include_losses=True):
    airflow = SimpleNamespace(
        mode="NATURAL", velocity_m_s=0.0, direction_deg=0.0, custom_h_w_m2k=5.0,
        expose_top=True, expose_bottom=True, expose_edges=True,
    )
    return SimpleNamespace(
        airflow=airflow, components=[], grid_size_mm=1.0, ambient_c=25.0,
        include_radiation=False, emissivity=0.9, include_dc_copper_losses=include_losses,
        use_phase3_heat_sources=True,
    )


@dataclass
class _BoardModel:
    copper_losses: list = field(default_factory=list)
    components: list = field(default_factory=list)


class _NoDC:
    calls = 0

    def __init__(self):
        type(self).calls += 1

    def solve(self, *_args):
        return _dc_results("raster")


class ThermalTakesAHandedOverResult(unittest.TestCase):
    def _engine(self, seen, coupled=None):
        from application.thermal_controller import ThermalSolverEngine

        class Mesher:
            def __init__(self, **_kwargs):
                pass

            def generate_mesh(self, model, settings, progress_callback=None, air_velocity_m_s=None):
                seen["losses"] = list(model.copper_losses)
                return SimpleNamespace(nodes=[0])

        class Solver:
            def __init__(self, **_kwargs):
                pass

            def solve_with_surface_refinement(self, mesh, settings, progress_callback=None):
                return "thermal result"

        _NoDC.calls = 0
        factories = {"coupled_solver_factory": coupled} if coupled else {}
        return ThermalSolverEngine(
            dc_engine_factory=_NoDC, mesher_factory=Mesher, solver_factory=Solver, **factories,
        )

    def test_the_handed_over_result_is_used_and_named(self):
        from application.thermal_controller import ThermalRunRequest

        seen, logs = {}, []
        request = ThermalRunRequest(
            settings=_settings(), board_model=_BoardModel(), dc_request=None,
            _dc_results=_dc_results(), _dc_results_source="this campaign's DC result",
        )
        outcome = self._engine(seen).solve(request, logs.append, lambda *_: None, lambda: False)
        self.assertEqual(_NoDC.calls, 0)
        self.assertEqual(len(seen["losses"]), 1)
        self.assertAlmostEqual(seen["losses"][0].power_w, 0.25)
        self.assertEqual(outcome.copper_loss_source, "this campaign's DC result (hybrid mesh)")
        self.assertTrue(any("not solved again" in line for line in logs))

    def test_a_coupled_run_couples_from_the_handed_over_result(self):
        from application.thermal_controller import ThermalRunRequest

        seen, logs = {}, []

        class Coupled:
            def __init__(self, **_kwargs):
                pass

            def solve(self, _mesh, _settings, rail_contexts, progress_callback=None, rails=None):
                seen["contexts"] = dict(rail_contexts)
                return SimpleNamespace(thermal="coupled result")

        request = ThermalRunRequest(
            settings=_settings(include_losses=False), board_model=_BoardModel(),
            dc_request=None, coupled=True,
            _dc_results=_dc_results(), _dc_results_source="this campaign's DC result",
        )
        outcome = self._engine(seen, Coupled).solve(
            request, logs.append, lambda *_: None, lambda: False,
        )
        self.assertEqual(_NoDC.calls, 0, "the ambient DC must not be solved twice")
        self.assertEqual(list(seen["contexts"]), ["VCC"])
        self.assertEqual(outcome.result, "coupled result")
        self.assertTrue(any("coupling starts from" in line for line in logs))
        # COH-11: copper-loss provenance used to stay empty for every coupled
        # run, although the coupling's own per-rail solves are exactly where
        # those losses come from.
        self.assertIn("this campaign's DC result", outcome.copper_loss_source)
        self.assertIn("hybrid mesh", outcome.copper_loss_source)

    def test_without_one_the_run_solves_and_says_so(self):
        from application.thermal_controller import ThermalRunRequest

        request = ThermalRunRequest(settings=_settings(), board_model=_BoardModel(), dc_request="captured")
        outcome = self._engine({}).solve(request, lambda _m: None, lambda *_: None, lambda: False)
        self.assertEqual(_NoDC.calls, 1)
        self.assertEqual(outcome.copper_loss_source, "a DC solve run for this analysis (raster mesh)")

    def test_the_provenance_reaches_the_structured_result(self):
        from analysis_adapters import adapt_thermal_result

        result = adapt_thermal_result(
            SimpleNamespace(converged=True, energy_balance_error_pct=0.0),
            copper_loss_source="this campaign's DC result (hybrid mesh)",
        )
        evidence = [item for item in result.provenance if item.source == "COPPER_LOSSES"]
        self.assertEqual(len(evidence), 1)
        self.assertIn("this campaign's DC result", evidence[0].detail)


class CFDTakesAHandedOverResult(unittest.TestCase):
    def test_the_handed_over_result_is_used(self):
        from application.cfd_controller import CFDRunRequest, CFDSolverEngine

        seen = {}

        class Solver:
            def __init__(self, **_kwargs):
                pass

            def solve(self, board_model, settings, progress_callback=None, cancel_callback=None):
                seen["losses"] = list(board_model.copper_losses)
                return "mesh", "result"

        class Plotter:
            def __init__(self, **_kwargs):
                pass

            def __getattr__(self, _name):
                return lambda *_args, **_kwargs: b""

        _NoDC.calls = 0
        engine = CFDSolverEngine(solver_factory=Solver, dc_engine_factory=_NoDC, plotter_factory=Plotter)
        request = CFDRunRequest(
            board_model=_BoardModel(), settings=_settings(),
            _dc_results=_dc_results(), _dc_results_source="this campaign's DC result",
        )
        outcome = engine.solve(request, lambda _m: None, lambda *_: None, lambda: False)
        self.assertEqual(_NoDC.calls, 0)
        self.assertEqual(len(seen["losses"]), 1)
        self.assertEqual(outcome.copper_loss_source, "this campaign's DC result (hybrid mesh)")


class _Recording:
    def __init__(self, returns):
        self.requests, self._returns = [], returns

    def solve(self, request, emit_log, emit_progress, cancelled):
        self.requests.append(request)
        return self._returns


def _campaign(engines):
    ids = list(engines)
    registry = AnalysisRegistry([
        AnalysisDescriptor(aid, aid, "Group", (index + 1) * 10, ("board",), ("run",))
        for index, aid in enumerate(ids)
    ])
    adapters = {aid: (lambda _result, _request, aid=aid: AnalysisResult(aid, aid, status=AnalysisStatus.PASS)) for aid in ids}
    return CampaignEngine(domain_engines=engines, adapters=adapters, registry=registry)


def _dc_request(grid=0.1):
    rail = SimpleNamespace(net_name="VCC", nominal_voltage=5.0, loads=[SimpleNamespace(total_current=1.0)])
    return SimpleNamespace(rails=(rail,), grid_size_mm=grid, board_path="board.kicad_pcb")


class CampaignHandsTheDCResultOver(unittest.TestCase):
    def _run(self, thermal_request):
        from application.thermal_controller import ThermalRunRequest  # noqa: F401

        results = _dc_results()
        dc, thermal = _Recording(results), _Recording("thermal outcome")
        logs = []
        request = CampaignRunRequest(domain_requests={"DC": _dc_request(), "THERMAL": thermal_request})
        _campaign({"DC": dc, "THERMAL": thermal}).solve(request, logs.append, lambda *_: None, lambda: False)
        return results, thermal.requests[0], logs

    def test_a_matching_capture_receives_the_dc_result(self):
        from application.thermal_controller import ThermalRunRequest

        results, received, logs = self._run(ThermalRunRequest(
            settings=_settings(), board_model=_BoardModel(), dc_request=_dc_request(),
        ))
        self.assertIs(received._dc_results, results)
        self.assertEqual(received._dc_results_source, "this campaign's DC result")
        self.assertTrue(any("THERMAL will take its copper losses" in line for line in logs))

    def test_a_coupled_run_starts_from_the_campaign_result(self):
        # Its ambient pass is the solve the DC domain just ran on the same
        # capture: 87 s of the 349 s P02 Rev E campaign, solved twice.
        from application.thermal_controller import ThermalRunRequest

        results, received, logs = self._run(ThermalRunRequest(
            settings=_settings(include_losses=False), board_model=_BoardModel(),
            dc_request=_dc_request(), coupled=True,
        ))
        self.assertIs(received._dc_results, results)
        self.assertTrue(any("starts its coupling" in line for line in logs))

    def test_a_coupled_run_whose_capture_differs_keeps_its_own_solve(self):
        from application.thermal_controller import ThermalRunRequest

        _results, received, logs = self._run(ThermalRunRequest(
            settings=_settings(), board_model=_BoardModel(),
            dc_request=_dc_request(grid=0.25), coupled=True,
        ))
        self.assertIsNone(received._dc_results)
        self.assertTrue(any("solves its own DC" in line and "grid" in line for line in logs))

    def test_a_different_capture_solves_its_own_and_the_log_says_why(self):
        from application.thermal_controller import ThermalRunRequest

        _results, received, logs = self._run(ThermalRunRequest(
            settings=_settings(), board_model=_BoardModel(), dc_request=_dc_request(grid=0.25),
        ))
        self.assertIsNone(received._dc_results)
        self.assertTrue(any("solves its own DC" in line and "grid" in line for line in logs))

    def test_a_run_without_copper_losses_receives_nothing(self):
        from application.thermal_controller import ThermalRunRequest

        _results, received, _logs = self._run(ThermalRunRequest(
            settings=_settings(include_losses=False), board_model=_BoardModel(), dc_request=_dc_request(),
        ))
        self.assertIsNone(received._dc_results)

    def test_the_handed_over_result_stays_out_of_the_cache_digest(self):
        from dataclasses import replace

        from application.campaign_cache import configuration_digest
        from application.thermal_controller import ThermalRunRequest

        base = ThermalRunRequest(settings=_settings(), board_model=_BoardModel(), dc_request="capture")
        self.assertEqual(
            configuration_digest(base),
            configuration_digest(replace(base, _dc_results=_dc_results())),
        )



class BatchSharesTheDCCapture(unittest.TestCase):
    """P02 Rev E, 2026-09-22 21:52: the batch captured the 17 rails' copper
    three times on the UI thread -- DC, thermal, CFD -- about 3 s each, and the
    campaign then found the three equal and used only the first."""

    def _captured(self):
        from application.dc_controller import DCRunRequest

        rail = _dc_request().rails[0]
        return DCRunRequest(
            rails=(rail,), geometry_by_rail={"VCC": {3: "copper"}},
            classification_geometry_by_rail={}, stackup={}, board=None,
            grid_size_mm=0.1, maximum_drop_pct=5.0, board_path="board.kicad_pcb",
            grid_sensitivity_check=True,
        )

    def test_a_matching_run_shares_the_capture_without_the_grid_check(self):
        from application.campaign_controller import reuse_dc_capture

        captured = self._captured()
        shared = reuse_dc_capture(captured, captured.rails, 0.1, "board.kicad_pcb")

        self.assertIs(shared.geometry_by_rail, captured.geometry_by_rail)
        self.assertFalse(shared.grid_sensitivity_check)
        self.assertIsNone(shared.maximum_drop_pct)
        self.assertEqual(CampaignEngine._dc_capture_mismatch(captured, shared), "")

    def test_another_grid_captures_its_own(self):
        from application.campaign_controller import reuse_dc_capture

        captured = self._captured()
        self.assertIsNone(reuse_dc_capture(captured, captured.rails, 0.25, "board.kicad_pcb"))

if __name__ == "__main__":
    unittest.main()
