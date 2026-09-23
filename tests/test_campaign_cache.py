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
        engine.solve(request, lambda m: None, lambda *a: None, lambda: False)

        self.assertEqual(fake.calls, 1, "second run must be served from cache")
        self.assertIsNotNone(
            request.domain_requests["THERMAL"]._dc_results,
            "a cached DC result must still hand over to THERMAL",
        )


class InvalidateTests(unittest.TestCase):
    def _fill(self):
        cache = CampaignCache()
        result = _stub_result()
        cache.put("fp-1", "DC", "d1", result)
        cache.put("fp-1", "EMC", "d2", result)
        cache.put("fp-2", "DC", "d3", result)
        return cache

    def test_invalidate_by_analysis_id_leaves_other_domains(self):
        cache = self._fill()
        dropped = cache.invalidate(analysis_id="DC")
        self.assertEqual(dropped, 2)
        self.assertIsNone(cache.get("fp-1", "DC", "d1"))
        self.assertIsNotNone(cache.get("fp-1", "EMC", "d2"))


def _stub_result():
    from analysis_contract import AnalysisResult, AnalysisStatus
    return AnalysisResult(
        analysis_type="DC", title="DC analysis", status=AnalysisStatus.PASS,
    )


if __name__ == "__main__":
    unittest.main()
