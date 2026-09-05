import os
import unittest
from unittest.mock import patch

from codex_linux_computer_use.clipboard import MAX_BYTES
from codex_linux_computer_use.clipboard_content import ClipboardContent
from codex_linux_computer_use.dbus import PortalError
from codex_linux_computer_use.portal import PortalDesktop
from test_clipboard import ClipboardBus


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.bus = ClipboardBus()
        self.content = ClipboardContent(self.bus, "/session/codex")
        self.addCleanup(self.content.close)
        original = self.bus.call

        def acknowledge(interface, method, signature, values, **kwargs):
            result = original(interface, method, signature, values, **kwargs)
            if method == "SetSelection":
                self.bus.emit(
                    "SelectionOwnerChanged",
                    values[0],
                    {
                        "mime_types": values[1]["mime_types"][1],
                        "session_is_owner": True,
                    },
                )
            return result

        self.bus.call = acknowledge

    def test_serves_owned_bytes_one_request_at_a_time(self):
        data = {"text/plain": "Native — Ελληνικά 日本語 🐧".encode()}
        self.content.offer(data)
        data["text/plain"] = b"caller mutated its dictionary"
        self.bus.emit("SelectionTransfer", "/session/codex", "text/plain", 1)
        self.bus.emit("SelectionTransfer", "/session/codex", "image/png", 2)
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.bus.descriptors["SelectionWrite"] = write_fd
        self.content.serve()
        self.assertEqual(os.read(read_fd, 1024), "Native — Ελληνικά 日本語 🐧".encode())
        self.assertEqual(self.content.transport.pending, {2})
        self.content.serve()
        self.assertEqual(
            [call[3] for call in self.bus.calls if call[1] == "SelectionWriteDone"],
            [("/session/codex", 1, True), ("/session/codex", 2, False)],
        )

    def test_old_transfer_arriving_during_new_offer_never_gets_new_bytes(self):
        self.content.offer({"text/plain": b"old"})
        original = self.bus.call

        def delayed_transfer(*args, **kwargs):
            if args[1] == "SetSelection":
                self.bus.emit("SelectionTransfer", "/session/codex", "text/plain", 3)
            return original(*args, **kwargs)

        with patch.object(self.bus, "call", side_effect=delayed_transfer):
            self.content.offer({"text/plain": b"new"})
        self.content.serve()
        self.assertEqual(self.bus.calls[-1][3], ("/session/codex", 3, False))
        self.assertFalse(any(call[1] == "SelectionWrite" for call in self.bus.calls))

    def test_external_owner_and_close_discard_retained_content(self):
        self.content.offer({"text/plain": b"owned"})
        self.bus.emit(
            "SelectionOwnerChanged",
            "/session/codex",
            {"mime_types": ["text/plain"], "session_is_owner": False},
        )
        self.content.serve()
        self.assertEqual((self.content.data, self.content.generation), ({}, None))
        self.content.offer({"text/plain": b"owned again"})
        self.bus.emit("SelectionTransfer", "/session/codex", "text/plain", 4)
        self.content.close()
        self.assertEqual(
            (self.content.data, self.bus.callbacks, self.bus.calls[-1][3]),
            ({}, {}, ("/session/codex", 4, False)),
        )

    def test_invalid_or_oversized_offers_preserve_previous_content(self):
        self.content.offer({"text/plain": b"previous"})
        calls = list(self.bus.calls)
        for data in (
            {},
            {"text/plain": b"x" * MAX_BYTES, "text/html": b"x"},
            {"text/plain": "not bytes"},
            {"bad\nformat": b"x"},
        ):
            with (
                self.subTest(formats=tuple(data)),
                self.assertRaises(ValueError),
            ):
                self.content.offer(data)
        self.assertEqual(
            (self.content.data, self.bus.calls), ({"text/plain": b"previous"}, calls)
        )

    def test_missing_ownership_confirmation_closes_ambiguous_offer(self):
        self.content.offer({"text/plain": b"previous"})
        with (
            patch.object(self.bus, "call", return_value=()),
            patch(
                "codex_linux_computer_use.clipboard_content.time.monotonic",
                side_effect=[0, 2],
            ),
            self.assertRaisesRegex(PortalError, "confirm clipboard ownership"),
        ):
            self.content.offer({"text/plain": b"new"})
        self.assertEqual((self.content.data, self.content.transport.closed), ({}, True))

    def test_failed_rejection_cleans_the_remaining_batch(self):
        self.content.offer({"text/plain": b"previous"})
        for serial in (7, 8):
            self.bus.emit("SelectionTransfer", "/session/codex", "text/plain", serial)
        self.bus.fail_method = "SelectionWriteDone"
        with self.assertRaises(PortalError):
            self.content.offer({"text/plain": b"new"})
        self.assertEqual(
            (self.content.data, self.content.transport.pending, self.bus.callbacks),
            ({}, set(), {}),
        )

    def test_aborted_consumer_keeps_offer_available(self):
        self.content.offer({"text/plain": b"available"})
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        self.bus.descriptors["SelectionWrite"] = write_fd
        self.bus.emit("SelectionTransfer", "/session/codex", "text/plain", 9)
        self.content.serve()
        self.assertEqual(
            (self.content.data, self.content.transport.closed),
            ({"text/plain": b"available"}, False),
        )
        self.bus.fail_method = "SelectionWrite"
        self.bus.emit("SelectionTransfer", "/session/codex", "text/plain", 10)
        self.content.serve()
        self.assertEqual(self.bus.calls[-1][3], ("/session/codex", 10, False))


class ClipboardSessionTests(unittest.TestCase):
    def setUp(self):
        self.bus = ClipboardBus()
        self.desktop = PortalDesktop(self.bus)
        self.addCleanup(self.desktop.close)

    def test_clipboard_is_requested_before_start_and_closed_on_revocation(self):
        self.bus.results["Start"]["clipboard_enabled"] = True
        self.desktop.start(clipboard=True)
        self.assertEqual(
            [call[1] for call in self.bus.calls],
            [
                "CreateSession",
                "SelectDevices",
                "SelectSources",
                "RequestClipboard",
                "Start",
            ],
        )
        content = self.desktop.clipboard
        self.bus.emit("Closed", {})
        self.assertEqual(
            (self.desktop.clipboard, content.transport.closed), (None, True)
        )

    def test_denied_clipboard_permission_closes_session_and_can_retry(self):
        with self.assertRaisesRegex(PortalError, "grant clipboard access"):
            self.desktop.start(clipboard=True)
        self.assertEqual(
            (self.desktop.session, self.desktop.clipboard, self.bus.callbacks),
            (None, None, {}),
        )
        self.desktop.start()
        self.assertIsNone(self.desktop.clipboard)
        with self.assertRaisesRegex(PortalError, "Stop sharing"):
            self.desktop.start(clipboard=True)

    def test_idle_serves_clipboard_until_explicit_stop(self):
        self.bus.results["Start"]["clipboard_enabled"] = True
        self.desktop.start(clipboard=True)
        content = self.desktop.clipboard
        with patch.object(content, "serve") as serve:
            self.desktop.idle()
            serve.assert_called_once_with()
            self.desktop.stop()
            self.desktop.idle()
            serve.assert_called_once_with()
        self.assertEqual(
            (self.desktop.clipboard, content.transport.closed, self.bus.callbacks),
            (None, True, {}),
        )
