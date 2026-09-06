import selectors
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from codex_linux_computer_use.actions import perform
from codex_linux_computer_use.dbus import PortalError


class DispatchTests(unittest.TestCase):
    def worker(self, code, *, check=lambda: None, poll=lambda: None):
        popen, children = subprocess.Popen, []

        def start(args, **kwargs):
            child = popen([sys.executable, "-c", code], **kwargs)
            children.append(child)
            return child

        with (
            patch(
                "codex_linux_computer_use.actions.subprocess.Popen", side_effect=start
            ),
            patch("codex_linux_computer_use.actions.DISCOVERY_TIMEOUT", 0.3),
        ):
            try:
                return perform({}, poll=poll, check_lock=check)
            finally:
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].poll())

    def test_gate_success_lock_denial_and_timeouts_reap_workers(self):
        gate = 'import sys; print("PREPARE", flush=True); sys.stdin.buffer.read(1); print("READY", flush=True); assert sys.stdin.buffer.read(1) == b"y"; '
        self.assertEqual(
            self.worker(gate + "print('{\"accepted\":true}')"), {"accepted": True}
        )
        with self.assertRaisesRegex(PortalError, "not dispatched"):
            self.worker(gate, check=Mock(side_effect=PortalError("Locked")))
        for prefix, message in [
            ('print("REA", end="", flush=True); ', "not dispatched"),
            (gate, "uncertain"),
        ]:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(PortalError, message),
            ):
                self.worker(prefix + "import time; time.sleep(60)")

    def test_cancel_after_gate_and_invalid_output_do_not_retry(self):
        gated_polls = None

        def check():
            nonlocal gated_polls
            gated_polls = 0

        def poll():
            nonlocal gated_polls
            if gated_polls is not None:
                gated_polls += 1
                if gated_polls > 1:
                    raise PortalError("Cancelled")

        gate = 'import sys; print("PREPARE", flush=True); sys.stdin.buffer.read(1); print("READY", flush=True); sys.stdin.buffer.read(1); '
        with self.assertRaisesRegex(PortalError, "uncertain"):
            self.worker(gate + "import time; time.sleep(60)", check=check, poll=poll)
        for code in ["print('x'*5000)", gate + "print('x'*5000)", gate + "print('{}')"]:
            with self.subTest(code=code), self.assertRaises(PortalError):
                self.worker(code)

    def test_selector_registration_failure_reaps_the_started_worker(self):
        started = time.monotonic()
        with (
            patch.object(
                selectors.DefaultSelector, "register", side_effect=OSError("fd limit")
            ),
            self.assertRaisesRegex(PortalError, "not dispatched"),
        ):
            self.worker("import time; time.sleep(60)")
        self.assertLess(time.monotonic() - started, 2)
