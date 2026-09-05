"""The batch button runs a campaign, and its results reach the result tabs.

The batch used to chain the six per-domain handlers and poll every controller
for idleness; "nothing overlaps" was a property of that polling loop and had
to be tested. It is now a property of CampaignEngine, which runs its domains
sequentially on one thread, so what is left to check here is the wiring the
dialog owns: that the batch reaches the engine at all, that a domain which
cannot be prepared is dropped rather than cancelling the run, and that each
raw outcome still reaches the handler that fills its result tab.

Exercised against the unbound methods with a stub rather than a constructed
wx.Dialog: building the real dialog needs a live KiCad board.
"""

import os
import sys
import unittest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)


class _Controller:
    def __init__(self, running=False):
        self.is_running = running


class _Buttons(dict):
    def __missing__(self, key):
        self[key] = _Button()
        return self[key]


class _Button:
    def __init__(self):
        self.enabled = True
        self.label = ""

    def Enable(self, enable=True):
        self.enabled = bool(enable)

    def Disable(self):
        self.enabled = False

    def SetLabel(self, label):
        self.label = label


class _ActionBar:
    def __init__(self):
        self.buttons = _Buttons()


class _Stub:
    """Enough of the dialog for the batch methods to run."""

    def __init__(self):
        from ui.main_dialog import KiPIDA_MainDialog

        self.BATCH_ANALYSES = KiPIDA_MainDialog.BATCH_ANALYSES
        self._closing = False
        self._campaign_requests = {}
        self.messages = []
        self.status = []
        self.finished = []
        self.action_bar = _ActionBar()
        self.campaign_controller = _Controller()
        self.builders = {}

    def log(self, message):
        self.messages.append(message)

    def _set_interaction_status(self, message):
        self.status.append(message)

    def _batch_request_builders(self, selected):
        return self.builders

    def _set_batch_running(self, running):
        from ui.main_dialog import KiPIDA_MainDialog

        return KiPIDA_MainDialog._set_batch_running(self, running)

    # The completion handlers _batch_domain_result dispatches to.
    def _finish_dc_job(self, outcome):
        self.finished.append(("DC", outcome))

    def _finish_ac_job(self, result, optimization=None):
        self.finished.append(("AC", result, optimization))

    def _finish_differential_job(self, outcome):
        self.finished.append(("DIFFERENTIAL", outcome))

    def _finish_emc_job(self, outcome):
        self.finished.append(("EMC", outcome))

    def _finish_thermal_job(self, outcome, settings):
        self.finished.append(("THERMAL", outcome, settings))

    def _finish_cfd_job(self, outcome):
        self.finished.append(("CFD", outcome))


def _dialog():
    from ui.main_dialog import KiPIDA_MainDialog

    return KiPIDA_MainDialog


class BatchPreparation(unittest.TestCase):
    def setUp(self):
        try:
            from ui.main_dialog import KiPIDA_MainDialog  # noqa: F401
        except Exception as exc:  # pragma: no cover - depends on import order
            self.skipTest(f"ui.main_dialog unavailable: {exc}")

    def test_every_selected_analysis_is_prepared(self):
        stub = _Stub()
        stub.builders = {"DC": lambda: "dc-request", "AC": lambda: "ac-request"}

        requests = _dialog()._build_batch_requests(stub, ["DC", "AC"])

        self.assertEqual(requests, {"DC": "dc-request", "AC": "ac-request"})

    def test_a_domain_that_cannot_be_prepared_is_dropped_not_fatal(self):
        # Preparation reads live panels and the IPC board, so one domain
        # failing there must cost its own section and not the whole batch --
        # the same isolation the engine gives a domain that fails solving.
        stub = _Stub()
        stub.builders = {
            "DC": lambda: (_ for _ in ()).throw(ValueError("No power rails defined.")),
            "AC": lambda: "ac-request",
        }

        requests = _dialog()._build_batch_requests(stub, ["DC", "AC"])

        self.assertEqual(requests, {"AC": "ac-request"})
        self.assertTrue(any("No power rails defined." in m for m in stub.messages))
        self.assertTrue(any("will be\nskipped" in m or "skipped" in m
                            for m in stub.messages))


class BatchResultPublication(unittest.TestCase):
    """A CampaignResult carries findings; the tabs need the raw outcomes."""

    def setUp(self):
        try:
            from ui.main_dialog import KiPIDA_MainDialog  # noqa: F401
        except Exception as exc:  # pragma: no cover - depends on import order
            self.skipTest(f"ui.main_dialog unavailable: {exc}")

    def test_each_domain_outcome_reaches_its_completion_handler(self):
        stub = _Stub()
        outcome = object()

        _dialog()._batch_domain_result(stub, "DIFFERENTIAL", outcome)

        self.assertEqual(stub.finished, [("DIFFERENTIAL", outcome)])

    def test_the_ac_pair_is_unpacked_for_its_handler(self):
        # ACSolverEngine returns (sweep, optimization); _finish_ac_job takes two.
        stub = _Stub()

        _dialog()._batch_domain_result(stub, "AC", ("sweep", "optimization"))

        self.assertEqual(stub.finished, [("AC", "sweep", "optimization")])

    def test_thermal_is_given_the_settings_its_request_was_built_from(self):
        class _Request:
            settings = "thermal-settings"

        stub = _Stub()
        stub._campaign_requests = {"THERMAL": _Request()}
        outcome = object()

        _dialog()._batch_domain_result(stub, "THERMAL", outcome)

        self.assertEqual(stub.finished, [("THERMAL", outcome, "thermal-settings")])

    def test_a_failing_publication_does_not_fail_the_batch(self):
        stub = _Stub()
        stub._finish_cfd_job = lambda outcome: (_ for _ in ()).throw(
            RuntimeError("no bitmap")
        )

        _dialog()._batch_domain_result(stub, "CFD", object())

        self.assertTrue(any("could not be displayed" in m for m in stub.messages))

    def test_publication_leaves_the_run_buttons_disabled_while_the_batch_runs(self):
        # The per-domain handlers re-enable their own run button on the way
        # out, which would offer the user a second analysis mid-batch.
        stub = _Stub()
        stub.campaign_controller.is_running = True
        stub.action_bar.buttons["dc"].Enable()

        _dialog()._batch_domain_result(stub, "DIFFERENTIAL", object())

        self.assertFalse(stub.action_bar.buttons["dc"].enabled)
        self.assertEqual(
            stub.action_bar.buttons["batch"].label, "Cancel Analysis Batch",
        )


class BatchOrdering(unittest.TestCase):
    def setUp(self):
        try:
            from ui.main_dialog import KiPIDA_MainDialog  # noqa: F401
        except Exception as exc:  # pragma: no cover - depends on import order
            self.skipTest(f"ui.main_dialog unavailable: {exc}")

    def test_the_offered_analyses_are_the_engine_ids(self):
        # The dialog names domains by analysis id now, because that is what
        # CampaignRunRequest keys on; a label-keyed batch would silently run
        # nothing.
        from analysis_registry import DEFAULT_ANALYSES

        runnable = {
            descriptor.analysis_id for descriptor in DEFAULT_ANALYSES.all()
            if "run" in descriptor.capabilities
        }
        offered = {key for key, _label in _dialog().BATCH_ANALYSES}
        self.assertEqual(offered, runnable)

    def test_a_builder_exists_for_every_offered_analysis(self):
        # An offered analysis with no builder would be selectable and then
        # silently absent from the campaign.
        class _WithBuilders(_Stub):
            def _build_dc_request(self): return "dc"

            def _build_ac_request(self, optimize=False): return "ac"

            def _build_differential_request(self): return "differential"

            def _build_thermal_request(self, coupled, resolve_air_velocity=True):
                return "thermal"

            def _build_emc_request(self): return "emc"

            def _build_cfd_request(self): return "cfd"

        builders = _dialog()._batch_request_builders(_WithBuilders(), ["CFD"])
        for key, _label in _dialog().BATCH_ANALYSES:
            self.assertIn(key, builders)
            self.assertTrue(callable(builders[key]))


if __name__ == "__main__":
    unittest.main()
