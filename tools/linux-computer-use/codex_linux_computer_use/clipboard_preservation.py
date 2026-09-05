"""Capture bounded clipboard formats without exposing them to model context."""

import time
from dataclasses import dataclass

from .clipboard import MAX_BYTES, ClipboardChanged
from .dbus import PortalError


@dataclass(frozen=True)
class ClipboardSnapshot:
    generation: int
    # None means the portal has not reported its initial selection.
    data: dict[str, bytes] | None


def capture_clipboard(content, check_lock):
    transport = content.transport
    check_lock()
    transport.poll()
    generation, selection = transport.generation, transport.selection
    if selection is None:
        return ClipboardSnapshot(generation, None)
    if selection.session_is_owner:
        if content.generation != generation:
            raise PortalError("The current clipboard offer is unavailable.")
        return ClipboardSnapshot(generation, dict(content.data))
    data = {}
    deadline = time.monotonic() + 5
    for mime in dict.fromkeys(selection.mime_types):
        check_lock()
        if time.monotonic() >= deadline:
            raise PortalError("Clipboard preservation timed out before pasting.")
        data[mime] = transport.read(mime)
        if sum(map(len, data.values())) > MAX_BYTES:
            raise PortalError("Clipboard preservation exceeds 1 MiB; nothing pasted.")
        transport.poll()
        if transport.generation != generation:
            raise PortalError("The clipboard changed before pasting; try again.")
    return ClipboardSnapshot(generation, data)


def restore_clipboard(content, snapshot, generation):
    transport = content.transport
    with transport.bus.cleanup():
        transport.poll()
        if transport.generation != generation:
            return "Clipboard changed externally; newer content kept."
        if snapshot.data is None:
            return "Previous clipboard state unavailable; paste text remains on the clipboard."
        try:
            if snapshot.data:
                content.offer(snapshot.data, expected_generation=generation)
            else:
                transport.offer((), expected_generation=generation)
                content.data.clear()
                content.generation = None
        except ClipboardChanged:
            return "Clipboard changed externally; newer content kept."
        return "Previous clipboard restored for this sharing session."
