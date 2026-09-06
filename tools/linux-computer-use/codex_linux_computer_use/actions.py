"""Gate a prepared native action on fresh lock and cancellation checks."""

import json
import os
import selectors
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .apps import DISCOVERY_TIMEOUT, MAX_RESULT_BYTES
from .dbus import PortalError
from .worker_policy import policy_file


def perform(params, *, poll, check_lock, policy=None):
    granted = False
    prepared = False
    try:
        with (
            selectors.DefaultSelector() as ready,
            tempfile.TemporaryFile() as errors,
            policy_file(policy) as policy_fd,
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "codex_linux_computer_use.action_worker",
                    json.dumps(params),
                    str(policy_fd),
                ],
                cwd=Path(__file__).resolve().parent.parent,
                stdin=subprocess.PIPE,
                pass_fds=(policy_fd,),
                stdout=subprocess.PIPE,
                stderr=errors,
                bufsize=0,
            ) as proc,
        ):
            deadline = time.monotonic() + DISCOVERY_TIMEOUT
            output = bytearray()
            try:
                ready.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    poll()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Native action timed out")
                    if not ready.select(min(0.05, remaining)):
                        continue
                    chunk = os.read(proc.stdout.fileno(), MAX_RESULT_BYTES + 1)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > MAX_RESULT_BYTES:
                        raise ValueError("Oversized action result")
                    if not granted and b"\n" in output:
                        if output != (b"READY\n" if prepared else b"PREPARE\n"):
                            raise ValueError("Invalid dispatch handshake")
                        if prepared:
                            check_lock()
                        poll()
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Action preparation timed out")
                        # From this point an error cannot establish whether the
                        # application accepted the action. Never retry it here.
                        granted, prepared = prepared, True
                        proc.stdin.write(b"y")
                        proc.stdin.flush()
                        output.clear()
                status = proc.wait(timeout=max(0.001, deadline - time.monotonic()))
                if status or not granted:
                    raise ValueError("Cannot prepare or complete this control action")
                result = json.loads(output)
                if (
                    not isinstance(result, dict)
                    or set(result) != {"accepted"}
                    or type(result["accepted"]) is not bool
                ):
                    raise ValueError("Invalid action result")
                return result
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
    except (OSError, ValueError, PortalError, subprocess.TimeoutExpired) as error:
        if granted:
            raise PortalError(
                "Action outcome uncertain; inspect the app before retrying."
            ) from error
        raise PortalError(f"Action was not dispatched: {error}") from error
