"""Tests for incremental recompute.

Scope: that the digest actually discriminates, and that a cache hit really
does prevent the engine from running -- the only reason this module exists.
"""

import os
import sys
import unittest
from dataclasses import dataclass, field
from typing import Any, Dict

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from application.campaign_cache import CampaignCache, configuration_digest
from application.campaign_controller import CampaignRunRequest

from tests.test_campaign_controller import FakeEngine, FakeRequest, build


@dataclass
class _ThermalLikeRequest:
    """Stand-in for a coupled THERMAL/CFD request, just enough for
    CampaignEngine._share_dc_results to accept the handover."""

    coupled: bool = True
    dc_request: Any = None
    grid_size_mm: float = 0.0
    board_path: Any = None
    _dc_results: Any = None
    _dc_results_source: str = ""


@dataclass
class _ACLikeRequest:
    settings: Any = None
    _build: Any = None


@dataclass
class DigestRequest:
    grid_size_mm: float = 0.1
    debug: bool = False
    options: Dict[str, int] = field(default_factory=dict)


class DigestTests(unittest.TestCase):
    def test_identical_requests_digest_identically(self):
        a = DigestRequest(0.1, False, {"x": 1, "y": 2})
        # Same content, different insertion order: must not change the digest.
        b = DigestRequest(0.1, False, {"y": 2, "x": 1})
        self.assertEqual(configuration_digest(a), configuration_digest(b))

    def test_changed_field_changes_the_digest(self):
        base = DigestRequest(0.1)
        self.assertNotEqual(
            configuration_digest(base), configuration_digest(DigestRequest(0.2)),
        )

    def test_what_a_request_holds_is_read_by_content(self):
        # Review of 2026-10-02: two NumPy arrays of more than 1,000 values
        # digested alike (their repr elides the middle), and the AC request's
        # board, in its private _build, was left out -- the AC took 329 s of
        # P02 Rev E's 557 s batch at 17:26, the most a reuse would save.
        import numpy as np

        first, second = np.zeros(2000), np.zeros(2000)
        second[1000] = 1.0
        self.assertNotEqual(configuration_digest(_ACLikeRequest(_build=first)),
                            configuration_digest(_ACLikeRequest(_build=second)))
        self.assertNotEqual(configuration_digest(DigestRequest(), context=("run-a",)),
                            configuration_digest(DigestRequest(), context=("run-b",)))

    def test_an_object_met_again_is_read_once(self):
        # P02 Rev E's AC batch request took 9.70 s to digest: its parsed
        # board read six times (_build and _grid_check of three profiles)
        # and 50,130 stat calls on net names such as "/VBUS_SYS".
        from unittest import mock
        from types import SimpleNamespace
        import application.campaign_cache as cache_module

        board = SimpleNamespace(nets=["/VBUS_SYS"] * 1000)
        request = _ACLikeRequest(settings=board, _build=SimpleNamespace(parsed_board=board))
        real = cache_module._file_state
        with mock.patch.object(cache_module, "_file_state", side_effect=real) as state:
            configuration_digest(request)
        self.assertEqual([call.args[0] for call in state.call_args_list].count("/VBUS_SYS"), 1)
        self.assertEqual(len(state.call_args_list), len({call.args[0] for call in state.call_args_list}))

    def test_the_thermal_session_meshes_are_not_an_input(self):
        # The first batch fills the session's mesh cache with a mesh and its
        # solver; reading them would make the second batch's thermal differ
        # from the first, and the solver cannot be read at all.
        from application.thermal_controller import ThermalRunRequest

        before = ThermalRunRequest(settings=None, board_model=None, cached_entries={})
        after = ThermalRunRequest(settings=None, board_model=None,
                                  cached_entries={("sig", "key"): ("mesh", object())})
        self.assertEqual(configuration_digest(before), configuration_digest(after))

    def test_a_value_it_cannot_read_makes_the_domain_run_again(self):
        from application.campaign_cache import UncomparableRequest

        with self.assertRaises(UncomparableRequest):
            configuration_digest(_ACLikeRequest(_build=object()))
        cache, fake, logged = CampaignCache(), FakeEngine(), []
        engine = build({"DC": fake}, ["DC"], cache=cache)
        request = CampaignRunRequest(board_fingerprint="fp-1",
                                     domain_requests={"DC": _ACLikeRequest(_build=object())})
        for _ in range(2):
            engine.solve(request, logged.append, lambda *a: None, lambda: False)
        self.assertEqual(fake.calls, 2)
        self.assertEqual(len(cache), 0)
        self.assertIn("DC: solved again, its inputs cannot be compared (holds a object).", logged)


class CacheReuseTests(unittest.TestCase):
    def _run(self, engine_obj, cache, fingerprint="fp-1"):
        engine = build({"DC": engine_obj}, ["DC"], cache=cache)
        request = CampaignRunRequest(
            board_fingerprint=fingerprint,
            domain_requests={"DC": DigestRequest()},
        )
        campaign = engine.solve(request, lambda m: None, lambda *a: None, lambda: False)
        return engine, campaign

    def test_second_run_reuses_cache_without_calling_the_engine(self):
        cache = CampaignCache()
        fake = FakeEngine()

        self._run(fake, cache)
        self.assertEqual(fake.calls, 1)

        engine, campaign = self._run(fake, cache)
        self.assertEqual(fake.calls, 1, "cached domain must not re-run the engine")
        self.assertTrue(engine.last_outcomes[0].from_cache)
        self.assertEqual(len(campaign.results), 1)

    def test_different_fingerprint_misses_the_cache(self):
        cache = CampaignCache()
        fake = FakeEngine()

        self._run(fake, cache, fingerprint="fp-1")
        self._run(fake, cache, fingerprint="fp-2")
        self.assertEqual(fake.calls, 2, "a different board must be recomputed")

    def test_no_cache_always_recomputes(self):
        fake = FakeEngine()
        self._run(fake, None)
        self._run(fake, None)
        self.assertEqual(fake.calls, 2)

    def test_cache_hit_still_hands_the_dc_result_to_thermal(self):
        """Latent defect (COH campaign-cache): a cache hit returned before
        on_domain_result, so a re-run after fixing one net -- the whole point
        of the cache -- handed THERMAL/CFD nothing and each re-solved its own
        ambient DC, the 87 s of P02 Rev E's 349 s campaign _share_dc_results
        exists to avoid.
        """
        cache = CampaignCache()
        fake = FakeEngine()
        engine = build({"DC": fake}, ["DC"], cache=cache)
        request = CampaignRunRequest(
            board_fingerprint="fp-1",
            domain_requests={"DC": DigestRequest(), "THERMAL": _ThermalLikeRequest()},
        )

        engine.solve(request, lambda m: None, lambda *a: None, lambda: False)
        self.assertIsNotNone(
            request.domain_requests["THERMAL"]._dc_results,
            "live solve must hand the DC result to THERMAL",
        )

        request.domain_requests["THERMAL"] = _ThermalLikeRequest()
        shown = []
        engine.set_raw_result_listener(lambda analysis_id, _raw: shown.append(analysis_id))
        engine.solve(request, lambda m: None, lambda *a: None, lambda: False)

        self.assertEqual(fake.calls, 1, "second run must be served from cache")
        self.assertIsNotNone(
            request.domain_requests["THERMAL"]._dc_results,
            "a cached DC result must still hand over to THERMAL",
        )
        # Its tab and history entry already hold it: handed to the UI again,
        # it was saved a second time under the same run_id.
        self.assertEqual(shown, [])


class InvalidateTests(unittest.TestCase):
    def test_invalidate_by_analysis_id_leaves_other_domains(self):
        cache = CampaignCache()
        result = _stub_result()
        cache.put("fp-1", "DC", "d1", result)
        cache.put("fp-1", "EMC", "d2", result)
        dropped = cache.invalidate(analysis_id="DC")
        self.assertEqual(dropped, 1)
        self.assertIsNone(cache.get("fp-1", "DC", "d1"))
        self.assertIsNotNone(cache.get("fp-1", "EMC", "d2"))

    def test_only_the_latest_entry_of_an_analysis_is_kept(self):
        # Each entry holds the raw outcome -- a DC solution, a thermal mesh
        # -- for as long as the dialog lives.
        cache = CampaignCache()
        cache.put("fp-1", "DC", "d1", _stub_result(), domain_result={"mesh": 1})
        cache.put("fp-2", "DC", "d3", _stub_result(), domain_result={"mesh": 2})
        self.assertEqual(len(cache), 1)
        self.assertIsNone(cache.get_domain_result("fp-1", "DC", "d1"))


def _stub_result():
    from analysis_contract import AnalysisResult, AnalysisStatus
    return AnalysisResult(
        analysis_type="DC", title="DC analysis", status=AnalysisStatus.PASS,
    )


if __name__ == "__main__":
    unittest.main()
