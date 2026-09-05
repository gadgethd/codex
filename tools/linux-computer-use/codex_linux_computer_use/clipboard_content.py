"""Own bounded clipboard content and keep generations of offers separate."""

import time

from .clipboard import (
    MAX_BYTES,
    MAX_FORMATS,
    ClipboardTransferError,
    PortalClipboard,
    valid_mime,
)
from .dbus import PortalError


class ClipboardContent:
    def __init__(self, bus, session):
        self.transport = PortalClipboard(bus, session)
        self.data = {}
        self.generation = None
        self.transport.request()

    def offer(self, data):
        if (
            not isinstance(data, dict)
            or not data
            or len(data) > MAX_FORMATS
            or not all(valid_mime(mime) for mime in data)
            or any(not isinstance(value, bytes) for value in data.values())
            or sum(map(len, data.values())) > MAX_BYTES
        ):
            raise ValueError(
                "Clipboard offers may contain at most 32 formats and 1 MiB total."
            )
        self.data.clear()
        self.generation = None
        try:
            for _mime, serial in self.transport.take_transfers():
                self.transport.reject(serial)
            baseline = self.transport.offer(tuple(data))
            deadline = time.monotonic() + 1
            while (
                self.transport.generation <= baseline
                or not self.transport.selection.session_is_owner
                or set(self.transport.selection.mime_types) != set(data)
            ):
                self.transport.poll()
                if time.monotonic() >= deadline:
                    raise PortalError(
                        "The desktop did not confirm clipboard ownership."
                    )
                time.sleep(0.01)
        except BaseException:
            # An ambiguous ownership change must not acknowledge a later offer.
            self.close()
            raise
        self.generation = self.transport.generation
        self.data = dict(data)

    def serve(self):
        self.transport.poll()
        if self.generation != self.transport.generation:
            self.data.clear()
            self.generation = None
        for mime, serial in self.transport.take_transfers(limit=1):
            data = self.data.get(mime)
            if (
                data is None
                or self.transport.transfer_generations[serial] != self.generation
            ):
                self.transport.reject(serial)
            else:
                try:
                    self.transport.write(serial, data)
                except ClipboardTransferError:
                    # The transport already reported this consumer's failure.
                    pass

    def close(self):
        try:
            self.transport.close()
        finally:
            self.data.clear()
            self.generation = None
