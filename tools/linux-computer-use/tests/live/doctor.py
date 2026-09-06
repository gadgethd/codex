"""Verify installed prerequisites before requesting any desktop sharing."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path


async def verify(output):
    scanner = output / "unexpected-scanner"
    scanner.write_text('#!/bin/sh\n: > "$0.started"\nexit 99\n')
    scanner.chmod(0o700)
    result = await asyncio.to_thread(
        subprocess.run,
        [str(Path(sys.executable).with_name("codex-linux-computer-use")), "--doctor"],
        cwd=output,
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
        env={
            **os.environ,
            "GST_REGISTRY": str(output / "fresh-gst-registry.bin"),
            "GST_PLUGIN_SCANNER": str(scanner),
        },
    )
    (output / "doctor.json").write_text(result.stdout)
    (output / "doctor.stderr").write_text(result.stderr)
    report = json.loads(result.stdout)
    assert result.returncode == 0, report
    assert not Path(str(scanner) + ".started").exists(), "Probe forked a scanner"
    assert all(check["status"] == "ok" for check in report["checks"]), report
