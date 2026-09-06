"""Local prerequisite reports, with native probes isolated from the caller."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_BYTES = 8192


def probe(group, *, timeout=15):
    try:
        with tempfile.TemporaryFile() as output:
            subprocess.run(
                [sys.executable, "-m", "codex_linux_computer_use.doctor_worker", group],
                cwd=Path(__file__).resolve().parent.parent,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=True,
                # Keep scanner startup inside the bounded diagnostic worker,
                # including bindings that disallow registry APIs before init.
                env={**os.environ, "GST_REGISTRY_FORK": "no"},
            )
            output.seek(0)
            data = output.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Oversized report")
        checks = json.loads(data)
        if not isinstance(checks, list) or not 1 <= len(checks) <= 16:
            raise ValueError("Invalid report")
        for item in checks:
            if (
                not isinstance(item, dict)
                or set(item) != {"check", "status", "detail"}
                or any(not isinstance(v, str) or len(v) > 512 for v in item.values())
                or item["status"] not in ("ok", "unavailable")
            ):
                raise ValueError("Invalid check")
        return checks
    except (OSError, subprocess.SubprocessError, ValueError):
        return [
            {
                "check": group,
                "status": "unknown",
                "detail": "Probe failed or exceeded 15 seconds; check installation and session services, then rerun.",
            }
        ]


def main():
    checks = []
    if sys.platform == "linux":
        for group in ("dependencies", "session"):
            checks.extend(probe(group))
    else:
        checks.append(
            {
                "check": "platform",
                "status": "unavailable",
                "detail": "Requires a Linux desktop session.",
            }
        )
    print(
        json.dumps(
            {
                "version": 1,
                "checks": checks,
                "scope": "Prerequisites only. Host policy, desktop consent, actual capture/input and app compatibility still require verification.",
            },
            indent=2,
        )
    )
    return 0 if all(item["status"] == "ok" for item in checks) else 1
