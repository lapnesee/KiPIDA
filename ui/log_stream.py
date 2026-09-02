"""Recoverable stdout/stderr capture for the dialog log."""

import sys


class LogStream:
    def __init__(self, log_callback):
        self.log_callback = log_callback

    def write(self, message):
        value = str(message)
        if value.strip():
            self.log_callback(value.strip())

    def flush(self):
        return None


class DialogStreamCapture:
    """Install log streams and restore only redirects owned by this instance."""

    def __init__(self, log_callback):
        self.stdout = LogStream(log_callback)
        self.stderr = LogStream(log_callback)
        self._previous = None

    def install(self):
        if self._previous is None:
            self._previous = (sys.stdout, sys.stderr)
            sys.stdout = self.stdout
            sys.stderr = self.stderr

    def restore(self):
        if self._previous is None:
            return
        previous_stdout, previous_stderr = self._previous
        if sys.stdout is self.stdout:
            sys.stdout = previous_stdout
        if sys.stderr is self.stderr:
            sys.stderr = previous_stderr
        self._previous = None
