"""Shared lifecycle for cancellable, UI-independent background analyses."""

import threading
from typing import Callable, Optional


class BackgroundAnalysisController:
    """Run an engine with consistent concurrency, cancellation, and dispatch.

    Engines implement ``solve(request, emit_log, emit_progress, cancelled)``.
    Callback bundles expose ``on_log``, ``on_progress``, ``on_complete`` and
    ``on_error``.  The controller owns no wx objects and the dispatch function
    defines the UI-thread boundary.
    """

    def __init__(
        self,
        engine,
        *,
        thread_name: str,
        busy_message: str,
        cancelled_error_factory: Callable[[], Exception],
        dispatch: Callable = lambda callback, *args: callback(*args),
    ):
        self._engine = engine
        self._thread_name = str(thread_name)
        self._busy_message = str(busy_message)
        self._cancelled_error_factory = cancelled_error_factory
        self._dispatch = dispatch
        self._lock = threading.Lock()
        self._thread = None
        self._cancel_event = threading.Event()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, request, callbacks) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError(self._busy_message)
            self._cancel_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(request, callbacks),
                name=self._thread_name,
                daemon=True,
            )
            self._thread.start()

    def cancel(self) -> bool:
        if not self.is_running:
            return False
        self._cancel_event.set()
        return True

    def wait(self, timeout: Optional[float] = None) -> bool:
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _emit(self, callback, *args) -> None:
        self._dispatch(callback, *args)

    def _run(self, request, callbacks) -> None:
        try:
            outcome = self._engine.solve(
                request,
                lambda message: self._emit(callbacks.on_log, message),
                lambda *args: self._emit(callbacks.on_progress, *args),
                self._cancel_event.is_set,
            )
            if self._cancel_event.is_set():
                raise self._cancelled_error_factory()
            self._emit(callbacks.on_complete, outcome)
        except Exception as exc:
            self._emit(callbacks.on_error, exc)
