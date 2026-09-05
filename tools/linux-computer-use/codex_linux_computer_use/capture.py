"""Capture a frame without letting a stalled native pipeline block the host."""

import subprocess
import sys
import tempfile
from pathlib import Path

from .dbus import PortalError

CAPTURE_TIMEOUT = 12
MAX_FRAME_BYTES = 16 * 1024 * 1024


def capture_png(fd, stream, width, height):
    # GStreamer state changes, including cleanup, may block indefinitely. A
    # fresh interpreter also avoids inheriting the portal thread's GLib state.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "codex_linux_computer_use.capture_worker",
                    str(fd),
                    str(stream),
                    str(width),
                    str(height),
                ],
                cwd=Path(__file__).resolve().parent.parent,
                pass_fds=(fd,),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=errors,
                timeout=CAPTURE_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise PortalError(
                "Desktop capture timed out; its worker was stopped."
            ) from error
        except OSError as error:
            raise PortalError(
                f"Cannot start the desktop capture worker: {error}"
            ) from error
        if result.returncode:
            errors.seek(0)
            detail = errors.read(512).decode("utf-8", errors="replace").strip()
            raise PortalError(
                detail
                or f"Desktop capture worker exited with status {result.returncode}."
            )
        output.seek(0)
        png = output.read(MAX_FRAME_BYTES + 1)
    if (
        not 24 <= len(png) <= MAX_FRAME_BYTES
        or not png.startswith(b"\x89PNG\r\n\x1a\n")
        or png[12:16] != b"IHDR"
    ):
        raise PortalError("The capture worker returned an invalid or oversized PNG.")
    width = int.from_bytes(png[16:20], "big")
    height = int.from_bytes(png[20:24], "big")
    if not 0 < width <= 2048 or not 0 < height <= 2048:
        raise PortalError("The capture worker returned invalid image dimensions.")
    return {"png": png, "width": width, "height": height}
