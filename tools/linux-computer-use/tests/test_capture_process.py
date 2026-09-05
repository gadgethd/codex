import base64
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_linux_computer_use.capture import capture_png
from codex_linux_computer_use.dbus import PortalError

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB2kAAAAASUVORK5CYII="
)


class CaptureProcessTests(unittest.TestCase):
    def run_worker(self, code):
        popen = subprocess.Popen
        children = []

        def start(args, **kwargs):
            # Replace only native media work. Exercise real FD inheritance,
            # process timeouts, termination, output parsing and reaping.
            child = popen([sys.executable, "-c", code, args[3]], **kwargs)
            children.append(child)
            return child

        with (
            tempfile.TemporaryFile() as remote,
            patch(
                "codex_linux_computer_use.capture.subprocess.Popen", side_effect=start
            ),
            patch("codex_linux_computer_use.capture.CAPTURE_TIMEOUT", 0.5),
        ):
            try:
                return capture_png(remote.fileno(), 42, 1920, 1080)
            finally:
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].poll())
                # The parent still owns its remote FD after the worker exits.
                remote.write(b"still open")

    def test_frame_crosses_process_boundary_and_parent_keeps_remote_fd(self):
        frame = self.run_worker(
            f"import os, sys; os.fstat(int(sys.argv[1])); sys.stdout.buffer.write({PNG!r})"
        )
        self.assertEqual(frame, {"png": PNG, "width": 1, "height": 1})

    def test_hung_cleanup_is_terminated_even_after_frame_was_written(self):
        with self.assertRaisesRegex(PortalError, "timed out"):
            self.run_worker(
                f"import sys, time; sys.stdout.buffer.write({PNG!r}); "
                "sys.stdout.flush(); time.sleep(60)"
            )

    def test_failed_worker_returns_bounded_diagnostics(self):
        with self.assertRaises(PortalError) as raised:
            self.run_worker("import sys; sys.stderr.write('error' * 1000); sys.exit(1)")
        self.assertEqual(str(raised.exception), ("error" * 1000)[:512])

    def test_malformed_or_oversized_worker_output_is_rejected(self):
        for payload in (b"not a PNG", PNG[:16] + (2049).to_bytes(4, "big") + PNG[20:]):
            with self.subTest(payload=payload), self.assertRaises(PortalError):
                self.run_worker(f"import sys; sys.stdout.buffer.write({payload!r})")


if __name__ == "__main__":
    unittest.main()
