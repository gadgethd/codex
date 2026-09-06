"""Bound application discovery, including native connection and shutdown time."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from .dbus import PortalError

MAX_RESULT_BYTES = 4096
DISCOVERY_TIMEOUT = 8


def list_apps(cursor=0):
    if type(cursor) is not int or not 0 <= cursor < 4096:
        raise ValueError("Application cursor must be between 0 and 4095.")
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "codex_linux_computer_use.apps_worker",
                    str(cursor),
                ],
                cwd=Path(__file__).resolve().parent.parent,
                stdin=subprocess.DEVNULL,
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
            raise PortalError("Linux accessibility is unavailable or discovery failed.")
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
