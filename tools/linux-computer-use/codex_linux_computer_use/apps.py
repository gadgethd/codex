"""Bound application discovery, including native connection and shutdown time."""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .dbus import PortalError
from .worker_policy import policy_file

MAX_RESULT_BYTES = 4096
DISCOVERY_TIMEOUT = 8


def list_apps(cursor=0, *, policy=None):
    if type(cursor) is not int or not 0 <= cursor < 4096:
        raise ValueError("Application cursor must be between 0 and 4095.")
    return run_worker("apps_worker", [str(cursor)], policy=policy)


def get_app_state(app_id, path=(), cursor=0, text_offset=0, *, policy=None):
    if not isinstance(app_id, str) or not re.fullmatch(r"[0-9a-f]{32}", app_id):
        raise ValueError("Use an application ID returned by list_apps.")
    if (
        not isinstance(path, (list, tuple))
        or len(path) > 16
        or any(type(index) is not int or not 0 <= index < 4096 for index in path)
        or type(cursor) is not int
        or not 0 <= cursor < 4096
        or type(text_offset) is not int
        or not 0 <= text_offset <= 2147483647
    ):
        raise ValueError("Invalid accessibility path, cursor or text offset.")
    return run_worker(
        "state_worker",
        [app_id, json.dumps(path), str(cursor), str(text_offset)],
        policy=policy,
    )


def run_worker(module, args, *, policy=None):
    with (
        tempfile.TemporaryFile() as output,
        tempfile.TemporaryFile() as errors,
        policy_file(policy) as policy_fd,
    ):
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    f"codex_linux_computer_use.{module}",
                    *args,
                    str(policy_fd),
                ],
                cwd=Path(__file__).resolve().parent.parent,
                stdin=subprocess.DEVNULL,
                pass_fds=(policy_fd,),
                stdout=output,
                stderr=errors,
                timeout=DISCOVERY_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise PortalError(
                "Application discovery timed out; its worker was stopped."
            ) from error
        except OSError as error:
            raise PortalError("Cannot start application discovery.") from error
        if result.returncode:
            raise PortalError(
                "Application is unavailable, denied by policy, or its inspection failed."
            )
        output.seek(0)
        data = output.read(MAX_RESULT_BYTES + 1)
    try:
        text = data.decode("utf-8")
        if len(data) > MAX_RESULT_BYTES or not isinstance(json.loads(text), dict):
            raise ValueError("Invalid discovery result")
    except (ValueError, UnicodeError) as error:
        raise PortalError(
            "Application discovery returned invalid or oversized data."
        ) from error
    return text
