import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex_linux_computer_use.clipboard import (
    CLIPBOARD,
    MAX_BYTES,
    PortalClipboard,
    Selection,
)
from codex_linux_computer_use.dbus import PortalError
from test_portal import FakeBus


class ClipboardBus(FakeBus):
    def __init__(self):
        super().__init__()
        self.descriptors = {}

    def call(self, interface, method, *args, **kwargs):
        result = super().call(interface, method, *args, **kwargs)
        return self.descriptors.pop(method, result)

    def subscribe(self, interface, signal, path, callback):
        subscription = len(self.callbacks) + 1
        self.callbacks[subscription] = (signal, callback)
        return subscription

    def emit(self, signal, *parameters):
        for name, callback in list(self.callbacks.values()):
            if name == signal:
                callback(parameters)


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.bus = ClipboardBus()
        self.clipboard = PortalClipboard(self.bus, "/session/codex")
        self.addCleanup(self.clipboard.close)
        self.clipboard.request()
        self.bus.calls.clear()

    def test_request_subscribes_before_immediate_owner_signal(self):
        self.clipboard.close()
        clipboard = PortalClipboard(self.bus, "/session/codex")
        self.addCleanup(clipboard.close)
        original = self.bus.call
        mime = 'application/x-openoffice-objectdescriptor-xml;windows_formatname="Star Object Descriptor (XML)"'

        def request(*args, **kwargs):
            self.bus.emit(
                "SelectionOwnerChanged",
                "/session/codex",
                {
                    "mime_types": ["text/plain", mime],
                    "session_is_owner": False,
                },
            )
            return original(*args, **kwargs)

        with patch.object(self.bus, "call", side_effect=request):
            clipboard.request()
        self.assertEqual(
            (clipboard.selection, clipboard.generation),
            (Selection(("text/plain", mime), False), 1),
        )
        self.assertEqual(
            self.bus.calls,
            [(CLIPBOARD, "RequestClipboard", "(oa{sv})", ("/session/codex", {}), {})],
        )

    def test_request_failure_removes_subscriptions(self):
        self.clipboard.close()
        clipboard = PortalClipboard(self.bus, "/session/codex")
        self.bus.fail_method = "RequestClipboard"
        with self.assertRaises(PortalError):
            clipboard.request()
        self.assertEqual((clipboard.closed, self.bus.callbacks), (True, {}))

    def test_foreign_signals_are_ignored_and_close_discards_queued_data(self):
        self.bus.emit(
            "SelectionOwnerChanged",
            "/other",
            {"mime_types": ["text/plain"], "session_is_owner": False},
        )
        self.bus.emit("SelectionTransfer", "/other", "text/plain", 1)
        self.assertEqual(
            (self.clipboard.selection, self.clipboard.take_transfers()), (None, [])
        )
        self.bus.emit("SelectionTransfer", "/session/codex", "text/plain", 2)
        self.assertEqual(self.clipboard.take_transfers(), [("text/plain", 2)])
        self.bus.cancel_event = threading.Event()
        self.bus.cancel_event.set()
        self.clipboard.close()
        self.assertEqual(self.bus.calls[-1][3], ("/session/codex", 2, False))
        self.assertEqual(
            (
                self.bus.callbacks,
                self.clipboard.pending,
                list(self.clipboard.transfers),
            ),
            ({}, set(), []),
        )

    def test_metadata_and_transfer_queues_are_bounded(self):
        for signal, values, expected in [
            (
                "SelectionOwnerChanged",
                ({"mime_types": ["text/plain"] * 33, "session_is_owner": False},),
                "metadata",
            ),
            (
                "SelectionOwnerChanged",
                ({"mime_types": ["x" * 129], "session_is_owner": False},),
                "metadata",
            ),
            ("SelectionTransfer", ("text/plain", True), "transfer limits"),
        ]:
            with self.subTest(signal=signal, values=values):
                self.clipboard.failure = None
                self.bus.emit(signal, "/session/codex", *values)
                with self.assertRaisesRegex(PortalError, expected):
                    self.clipboard.poll()
        self.clipboard.failure = None
        for serial in range(33):
            self.bus.emit("SelectionTransfer", "/session/codex", "text/plain", serial)
        with self.assertRaisesRegex(PortalError, "transfer limits"):
            self.clipboard.take_transfers()
        self.assertEqual(
            (len(self.clipboard.pending), len(self.clipboard.transfers)), (32, 32)
        )

    def test_offers_and_rejections_use_the_clipboard_protocol(self):
        self.clipboard.offer(["text/plain;charset=utf-8", "text/plain"])
        self.bus.emit("SelectionTransfer", "/session/codex", "image/png", 7)
        self.clipboard.reject(7)
        self.assertEqual(
            self.bus.calls,
            [
                (
                    CLIPBOARD,
                    "SetSelection",
                    "(oa{sv})",
                    (
                        "/session/codex",
                        {
                            "mime_types": (
                                "as",
                                ("text/plain;charset=utf-8", "text/plain"),
                            )
                        },
                    ),
                    {},
                ),
                (
                    CLIPBOARD,
                    "SelectionWriteDone",
                    "(oub)",
                    ("/session/codex", 7, False),
                    {},
                ),
            ],
        )
        self.assertEqual(
            (self.clipboard.pending, self.clipboard.take_transfers()), (set(), [])
        )

    def test_reads_exact_unicode_bytes_and_closes_descriptor(self):
        data = "Codex Linux — café Ελληνικά 日本語 🐧\nsecond line".encode()
        read_fd, write_fd = os.pipe()
        os.write(write_fd, data)
        os.close(write_fd)
        self.bus.descriptors["SelectionRead"] = read_fd
        self.assertEqual(self.clipboard.read("text/plain;charset=utf-8"), data)
        with self.assertRaises(OSError):
            os.fstat(read_fd)

    def test_oversized_read_closes_descriptor(self):
        with tempfile.TemporaryFile() as source:
            source.write(b"x" * (MAX_BYTES + 1))
            source.seek(0)
            fd = os.dup(source.fileno())
            self.bus.descriptors["SelectionRead"] = fd
            with self.assertRaisesRegex(PortalError, "exceeds 1 MiB"):
                self.clipboard.read("text/plain")
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_write_handles_partial_pipe_writes_and_reports_completion(self):
        data = "Ελληνικά 日本語 🐧\n".encode() * 8192
        read_fd, write_fd = os.pipe()
        received = bytearray()

        def receive():
            try:
                while chunk := os.read(read_fd, 4096):
                    received.extend(chunk)
            finally:
                os.close(read_fd)

        reader = threading.Thread(target=receive, daemon=True)
        reader.start()
        self.bus.emit("SelectionTransfer", "/session/codex", "text/plain", 9)
        self.bus.descriptors["SelectionWrite"] = write_fd
        self.clipboard.write(9, data)
        reader.join(2)
        self.assertFalse(reader.is_alive())
        self.assertEqual(bytes(received), data)
        self.assertEqual(
            self.bus.calls[-1][1:4],
            ("SelectionWriteDone", "(oub)", ("/session/codex", 9, True)),
        )
        self.assertEqual(self.clipboard.pending, set())

    def test_stalled_reads_and_writes_time_out_and_close_descriptors(self):
        for reading in (True, False):
            with self.subTest(reading=reading):
                read_fd, write_fd = os.pipe()
                transferred, other = (
                    (read_fd, write_fd) if reading else (write_fd, read_fd)
                )
                self.bus.emit("SelectionTransfer", "/session/codex", "text/plain", 10)
                method = "SelectionRead" if reading else "SelectionWrite"
                self.bus.descriptors[method] = transferred
                try:
                    with (
                        patch(
                            "codex_linux_computer_use.clipboard.TRANSFER_TIMEOUT", 0.02
                        ),
                        self.assertRaisesRegex(PortalError, "timed out"),
                    ):
                        if reading:
                            self.clipboard.read("text/plain")
                        else:
                            self.clipboard.write(10, b"x" * MAX_BYTES)
                    with self.assertRaises(OSError):
                        os.fstat(transferred)
                    if not reading:
                        self.assertEqual(
                            self.bus.calls[-1][3], ("/session/codex", 10, False)
                        )
                finally:
                    os.close(other)
                    self.clipboard.pending.clear()
                    self.clipboard.transfers.clear()

    def test_cancelled_write_closes_fd_and_sends_failure_despite_cancellation(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.bus.cancel_event = threading.Event()
        self.bus.emit("SelectionTransfer", "/session/codex", "text/plain", 11)
        self.bus.descriptors["SelectionWrite"] = write_fd
        original_blocking = os.set_blocking

        def cancel(fd, blocking):
            original_blocking(fd, blocking)
            self.bus.cancel_event.set()

        with (
            patch("os.set_blocking", side_effect=cancel),
            self.assertRaisesRegex(PortalError, "cancelled"),
        ):
            self.clipboard.write(11, b"text")
        with self.assertRaises(OSError):
            os.fstat(write_fd)
        self.assertEqual(
            (self.clipboard.pending, self.bus.calls[-1][3]),
            (set(), ("/session/codex", 11, False)),
        )


if __name__ == "__main__":
    unittest.main()
