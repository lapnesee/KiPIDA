import unittest
from pathlib import Path

from ipc_connection import connect_to_live_board


class FakeClient:
    def __init__(self, outcome):
        self.outcome = outcome

    def get_board(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class IPCConnectionTests(unittest.TestCase):
    def test_windows_uses_native_endpoint_discovery(self):
        source = (Path(__file__).parents[1] / "ipc_entry.py").read_text(encoding="utf-8")
        self.assertIn("sys.platform != 'win32'", source)
        self.assertIn("native auto-discovery", source)

    def test_retry_uses_fresh_client_and_recovers(self):
        outcomes = [TimeoutError("busy"), object()]
        clients = []
        sleeps = []

        def factory(**kwargs):
            clients.append(kwargs)
            return FakeClient(outcomes.pop(0))

        client, board = connect_to_live_board(
            factory, socket_path="ipc://test", attempts=3, timeout_ms=4321,
            retry_delay_s=0.1, sleep_fn=sleeps.append,
        )
        self.assertIsNotNone(client)
        self.assertIsNotNone(board)
        self.assertEqual(len(clients), 2)
        self.assertEqual(clients[0]["timeout_ms"], 4321)
        self.assertEqual(sleeps, [0.1])

    def test_none_board_is_retried_then_rejected(self):
        calls = []
        with self.assertRaisesRegex(ConnectionError, "after 2 attempt"):
            connect_to_live_board(
                lambda **kwargs: calls.append(kwargs) or FakeClient(None),
                attempts=2, retry_delay_s=0.0,
            )
        self.assertEqual(len(calls), 2)

    def test_last_transport_error_is_reported(self):
        with self.assertRaisesRegex(ConnectionError, "Timed out"):
            connect_to_live_board(
                lambda **kwargs: FakeClient(TimeoutError("Timed out")),
                attempts=1,
            )


if __name__ == "__main__":
    unittest.main()
