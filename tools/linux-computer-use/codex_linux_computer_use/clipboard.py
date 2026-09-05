"""Bounded clipboard transport for an authorized desktop portal session.

Call request() before starting the session, then check its clipboard_enabled
result before using this transport. The caller owns policy, clipboard offers,
and pumping transfer requests while it advertises content.
"""

import os
import select
import time
from collections import deque
from dataclasses import dataclass
from functools import partial

from .dbus import PortalError

CLIPBOARD = "org.freedesktop.portal.Clipboard"
MAX_BYTES = 1024 * 1024
MAX_FORMATS = 32
MAX_TRANSFERS = 32
TRANSFER_TIMEOUT = 3


class ClipboardTransferError(PortalError):
    """A single clipboard reader or writer failed; the session can stay open."""


class ClipboardChanged(PortalError):
    """The selection changed before a guarded clipboard action could run."""


def valid_mime(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and bool(value.strip())
        and all(32 <= ord(char) <= 126 for char in value)
    )


@dataclass(frozen=True)
class Selection:
    mime_types: tuple[str, ...]
    session_is_owner: bool


class PortalClipboard:
    def __init__(self, bus, session):
        self.bus, self.session = bus, session
        self.selection = None
        self.generation = 0
        self.transfers = deque()
        self.pending = set()
        self.transfer_generations = {}
        self.subscriptions = []
        self.failure = None
        self.closed = False

    def request(self):
        if self.closed or self.subscriptions:
            raise PortalError("Clipboard access was already requested or closed.")
        try:
            for signal, callback in (
                ("SelectionOwnerChanged", self._owner_changed),
                ("SelectionTransfer", self._transfer_requested),
            ):
                self.subscriptions.append(
                    self.bus.subscribe(CLIPBOARD, signal, self.bus.PATH, callback)
                )
            self.bus.call(CLIPBOARD, "RequestClipboard", "(oa{sv})", (self.session, {}))
        except BaseException:
            self.close()
            raise

    def _owner_changed(self, parameters):
        session, options = parameters
        if self.closed or session != self.session:
            return
        formats = options.get("mime_types", [])
        owner = options.get("session_is_owner", False if not formats else None)
        if (
            not isinstance(formats, (list, tuple))
            or len(formats) > MAX_FORMATS
            or not all(valid_mime(mime) for mime in formats)
            or type(owner) is not bool
        ):
            self.failure = "The desktop returned invalid clipboard metadata."
            return
        self.selection = Selection(tuple(formats), owner)
        self.generation += 1

    def _transfer_requested(self, parameters):
        session, mime, serial = parameters
        if self.closed or session != self.session:
            return
        if (
            not valid_mime(mime)
            or type(serial) is not int
            or not 0 <= serial <= 0xFFFFFFFF
            or serial in self.pending
            or len(self.pending) >= MAX_TRANSFERS
        ):
            self.failure = "The desktop exceeded clipboard transfer limits."
            return
        self.pending.add(serial)
        self.transfer_generations[serial] = self.generation
        self.transfers.append((mime, serial))

    def poll(self):
        if self.closed:
            raise PortalError("The clipboard transport is closed.")
        self.bus.poll()
        if self.closed:
            raise PortalError("The clipboard transport is closed.")
        if self.failure:
            raise PortalError(self.failure)

    def take_transfers(self, *, limit=MAX_TRANSFERS):
        if type(limit) is not int or not 1 <= limit <= MAX_TRANSFERS:
            raise ValueError("Clipboard transfer batch must be between 1 and 32.")
        self.poll()
        return [
            self.transfers.popleft() for _ in range(min(limit, len(self.transfers)))
        ]

    def check_generation(self, expected):
        self.poll()
        if self.generation != expected:
            raise ClipboardChanged(
                "The clipboard changed; paste stopped. Verify the target before retrying."
            )

    def offer(self, mime_types, *, expected_generation=None):
        if (
            not isinstance(mime_types, (list, tuple))
            or len(mime_types) > MAX_FORMATS
            or not all(valid_mime(mime) for mime in mime_types)
        ):
            raise ValueError("Clipboard offers need at most 32 valid MIME types.")
        self.poll()
        baseline = self.generation
        guard = (
            {"before_send": partial(self.check_generation, expected_generation)}
            if expected_generation is not None
            else {}
        )
        self.bus.call(
            CLIPBOARD,
            "SetSelection",
            "(oa{sv})",
            (
                self.session,
                {"mime_types": self.bus.variant("as", tuple(mime_types))}
                if mime_types
                else {},
            ),
            **guard,
        )
        return baseline

    def read(self, mime):
        if not valid_mime(mime):
            raise ValueError("Invalid clipboard MIME type.")
        self.poll()
        fd = self.bus.call(
            CLIPBOARD, "SelectionRead", "(os)", (self.session, mime), receive_fd=True
        )
        return self._transfer_bytes(fd, None)

    def write(self, serial, data):
        if not isinstance(data, bytes) or len(data) > MAX_BYTES:
            raise ValueError("Clipboard content must be bytes, at most 1 MiB.")
        if type(serial) is not int or serial not in self.pending:
            raise ValueError("Unknown clipboard transfer serial.")
        success = False
        try:
            self.poll()
            try:
                fd = self.bus.call(
                    CLIPBOARD,
                    "SelectionWrite",
                    "(ou)",
                    (self.session, serial),
                    receive_fd=True,
                )
            except PortalError as error:
                # The desktop can expire a request before we open its pipe.
                # A failed completion below still surfaces a broken session.
                raise ClipboardTransferError(str(error)) from error
            self._transfer_bytes(fd, data)
            success = True
        finally:
            self._complete(serial, success)

    def reject(self, serial):
        if type(serial) is not int or serial not in self.pending:
            raise ValueError("Unknown clipboard transfer serial.")
        self._complete(serial, False)

    def _complete(self, serial, success):
        if serial not in self.pending:
            return
        self.pending.remove(serial)
        self.transfer_generations.pop(serial, None)
        self.transfers = deque(item for item in self.transfers if item[1] != serial)
        with self.bus.cleanup():
            self.bus.call(
                CLIPBOARD,
                "SelectionWriteDone",
                "(oub)",
                (self.session, serial, success),
            )

    def _transfer_bytes(self, fd, data):
        reading = data is None
        result = bytearray()
        offset = 0
        deadline = time.monotonic() + TRANSFER_TIMEOUT
        try:
            os.set_blocking(fd, False)
            poller = select.poll()
            poller.register(fd, select.POLLIN if reading else select.POLLOUT)
            while reading or offset < len(data):
                self.poll()
                if time.monotonic() >= deadline:
                    raise ClipboardTransferError("Clipboard data transfer timed out.")
                if not poller.poll(50):
                    continue
                try:
                    if reading:
                        chunk = os.read(fd, min(65536, MAX_BYTES + 1 - len(result)))
                        if not chunk:
                            return bytes(result)
                        result.extend(chunk)
                        if len(result) > MAX_BYTES:
                            raise ClipboardTransferError(
                                "Clipboard content exceeds 1 MiB."
                            )
                    else:
                        offset += os.write(
                            fd, memoryview(data)[offset : offset + 65536]
                        )
                except BlockingIOError:
                    continue
        except OSError as error:
            raise ClipboardTransferError(
                f"Clipboard data transfer failed: {error.strerror}"
            ) from error
        finally:
            os.close(fd)

    def close(self):
        if self.closed:
            return
        self.closed = True
        for serial in list(self.pending):
            try:
                self.reject(serial)
            except PortalError:
                # Stop retrying a failed connection; the caller closes the session.
                break
        for subscription in self.subscriptions:
            self.bus.unsubscribe(subscription)
        self.subscriptions.clear()
        self.pending.clear()
        self.transfer_generations.clear()
        self.transfers.clear()
        self.selection = None
