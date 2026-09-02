"""Bounded KiCad IPC connection and retry helpers."""

import time


def connect_to_live_board(
    kicad_factory, socket_path=None, attempts=3, timeout_ms=5000,
    retry_delay_s=0.35, log_callback=None, sleep_fn=time.sleep,
):
    """Return ``(client, board)`` or raise without exposing a half-live board.

    A new client is created for every attempt because a timed-out request can
    leave the previous request/reply transport in an indeterminate state.
    """
    attempts = max(1, int(attempts))
    last_error = None
    for attempt in range(1, attempts + 1):
        if log_callback:
            log_callback(f"KiCad IPC connection attempt {attempt}/{attempts}.")
        try:
            client = kicad_factory(socket_path=socket_path, timeout_ms=int(timeout_ms))
            board = client.get_board()
            if board is None:
                raise ConnectionError("KiCad returned no open PCB document.")
            if log_callback:
                log_callback(f"KiCad IPC connected on attempt {attempt}.")
            return client, board
        except Exception as exc:
            last_error = exc
            if log_callback:
                log_callback(f"KiCad IPC attempt {attempt} failed: {exc}")
            if attempt < attempts and retry_delay_s > 0.0:
                sleep_fn(float(retry_delay_s))
    raise ConnectionError(
        f"Unable to obtain the active PCB from KiCad after {attempts} attempt(s): {last_error}"
    ) from last_error
