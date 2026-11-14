import unittest
from unittest.mock import Mock, patch

from rag_infra.platform import BASELINE, CANDIDATE, FRONT, promote, switch


def reports(candidate_status):
    return {name: {"model": model, "status": status, "dataset_sha256": "suite", "backend_mode": "fixture"}
            for name, model, status in (("baseline", BASELINE, "passed"),
                                        ("candidate", CANDIDATE, candidate_status))}


class PlatformReleaseTest(unittest.TestCase):
    @patch("rag_infra.platform.switch")
    @patch("rag_infra.platform.evaluate_probe", return_value=(200, BASELINE, True))
    def test_blocked_evaluations_never_patch_live_selector(self, probe, change):
        for gate in ("regression", "incomplete"):
            with self.subTest(gate=gate):
                result = promote(Mock(), reports(gate), gate)
                self.assertEqual((result["gate"], result["action"], result["active_version"]),
                                 (gate, "blocked", BASELINE))
        change.assert_not_called()
        self.assertEqual(probe.call_count, 2)

    @patch("rag_infra.platform.switch")
    @patch("rag_infra.platform.evaluate_probe", return_value=(200, CANDIDATE, True))
    def test_candidate_response_confirms_promotion(self, probe, change):
        client = Mock()
        result = promote(client, reports("passed"), "promotion")
        change.assert_called_once_with(client, BASELINE, CANDIDATE)
        self.assertEqual(result["active_version"], CANDIDATE)
        self.assertEqual(result["action"], "promoted")
        probe.assert_called_once()

    @patch("rag_infra.platform.switch")
    @patch("rag_infra.platform.evaluate_probe", side_effect=[(503, CANDIDATE, False), (200, BASELINE, True)])
    def test_candidate_failure_rolls_back_and_waits_for_stable_response(self, probe, change):
        client = Mock()
        result = promote(client, reports("passed"), "rollback")
        self.assertEqual([call.args for call in change.call_args_list],
                         [(client, BASELINE, CANDIDATE), (client, CANDIDATE, BASELINE)])
        self.assertEqual(result["action"], "rolled_back")
        self.assertEqual(result["active_version"], BASELINE)
        self.assertEqual(result["failed_probe_requests"], 1)
        self.assertGreaterEqual(result["recovery_seconds"], 0)
        self.assertEqual(probe.call_count, 2)

    def test_unexpected_live_selector_prevents_switch(self):
        client = Mock()
        client.get.side_effect = lambda kind, name: {"spec": {"selector": {"app": name}}}
        with self.assertRaisesRegex(RuntimeError, "live selector changed"):
            switch(client, BASELINE, CANDIDATE)
        client.get.assert_any_call("service", FRONT)
        client.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
