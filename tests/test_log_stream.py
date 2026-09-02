import sys
import unittest

from ui.log_stream import DialogStreamCapture, LogStream


class LogStreamTests(unittest.TestCase):
    def test_stream_ignores_whitespace_and_normalizes_messages(self):
        messages = []
        stream = LogStream(messages.append)
        stream.write("  hello\n")
        stream.write("  \n")
        self.assertEqual(messages, ["hello"])

    def test_capture_restores_previous_process_streams(self):
        original = (sys.stdout, sys.stderr)
        capture = DialogStreamCapture(lambda _message: None)
        try:
            capture.install()
            self.assertIs(sys.stdout, capture.stdout)
            self.assertIs(sys.stderr, capture.stderr)
        finally:
            capture.restore()
        self.assertEqual((sys.stdout, sys.stderr), original)

    def test_restore_does_not_overwrite_a_newer_redirect(self):
        original = (sys.stdout, sys.stderr)
        capture = DialogStreamCapture(lambda _message: None)
        newer_stdout = object()
        try:
            capture.install()
            sys.stdout = newer_stdout
            capture.restore()
            self.assertIs(sys.stdout, newer_stdout)
            self.assertIs(sys.stderr, original[1])
        finally:
            sys.stdout, sys.stderr = original


if __name__ == "__main__":
    unittest.main()
