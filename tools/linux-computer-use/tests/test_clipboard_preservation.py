import os
import tempfile
import unittest
from unittest.mock import patch

from codex_linux_computer_use.clipboard import ClipboardChanged
from codex_linux_computer_use.clipboard_content import ClipboardContent
from codex_linux_computer_use.clipboard_preservation import (
    ClipboardSnapshot,
    capture_clipboard,
    restore_clipboard,
)
from codex_linux_computer_use.dbus import PortalError
from test_clipboard import ClipboardBus


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.bus = ClipboardBus()
        self.content = ClipboardContent(self.bus, "/session/codex")
        self.addCleanup(self.content.close)
        self.data = {"text/plain": b"previous", "text/html": b"<b>previous</b>"}
        original = self.bus.call

        def call(interface, method, signature, values, **kwargs):
            if method == "SelectionRead":
                with tempfile.TemporaryFile() as file:
                    file.write(self.data[values[1]])
                    file.seek(0)
                    self.bus.descriptors[method] = os.dup(file.fileno())
            result = original(interface, method, signature, values, **kwargs)
            if method == "SetSelection":
                formats = values[1].get("mime_types", ("as", ()))[1]
                self.bus.emit(
                    "SelectionOwnerChanged",
                    values[0],
                    {
                        "mime_types": formats,
                        "session_is_owner": bool(formats),
                    },
                )
            return result

        self.bus.call = call

    def test_snapshot_copies_external_formats_and_owned_content(self):
        self.bus.emit(
            "SelectionOwnerChanged",
            "/session/codex",
            {
                "mime_types": tuple(self.data),
                "session_is_owner": False,
            },
        )
        snapshot = capture_clipboard(self.content, lambda: None)
        self.assertEqual(snapshot, ClipboardSnapshot(1, self.data))
        self.content.offer({"text/plain": b"temporary"}, expected_generation=1)
        self.assertIn(
            "restored",
            restore_clipboard(self.content, snapshot, self.content.generation),
        )
        self.bus.calls.clear()
        own_snapshot = capture_clipboard(self.content, lambda: None)
        self.assertEqual(
            own_snapshot, ClipboardSnapshot(self.content.generation, self.data)
        )
        self.assertEqual(self.bus.calls, [])
        self.content.data.clear()
        self.assertEqual(own_snapshot.data, self.data)

    def test_unknown_and_empty_initial_states_are_distinct(self):
        self.assertEqual(
            capture_clipboard(self.content, lambda: None), ClipboardSnapshot(0, None)
        )
        self.bus.emit("SelectionOwnerChanged", "/session/codex", {})
        snapshot = capture_clipboard(self.content, lambda: None)
        self.assertEqual(snapshot, ClipboardSnapshot(1, {}))
        self.content.offer({"text/plain": b"temporary"})
        restore_clipboard(self.content, snapshot, self.content.generation)
        self.assertEqual(
            (self.bus.calls[-1][3], self.content.data), (("/session/codex", {}), {})
        )

    def test_late_external_copy_prevents_both_publication_and_restoration(self):
        original = self.bus.call

        def copy_before_send(*args, **kwargs):
            if args[1] == "SetSelection":
                self.bus.emit(
                    "SelectionOwnerChanged",
                    "/session/codex",
                    {
                        "mime_types": ["text/plain"],
                        "session_is_owner": False,
                    },
                )
            return original(*args, **kwargs)

        for snapshot_data in (self.data, {}):
            self.content.offer({"text/plain": b"temporary"})
            generation = self.content.generation
            self.bus.calls.clear()
            with patch.object(self.bus, "call", side_effect=copy_before_send):
                message = restore_clipboard(
                    self.content, ClipboardSnapshot(0, snapshot_data), generation
                )
            self.assertIn("newer content kept", message)
            self.assertEqual(
                (self.bus.calls, self.content.transport.closed), ([], False)
            )
        generation = self.content.transport.generation
        with (
            patch.object(self.bus, "call", side_effect=copy_before_send),
            self.assertRaises(ClipboardChanged),
        ):
            self.content.offer(
                {"text/plain": b"new paste"}, expected_generation=generation
            )
        self.assertEqual((self.bus.calls, self.content.transport.closed), ([], False))

    def test_backup_limits_and_lock_failures_leave_the_selection_untouched(self):
        self.data = {"text/plain": b"x" * 600000, "text/html": b"y" * 600000}
        self.bus.emit(
            "SelectionOwnerChanged",
            "/session/codex",
            {
                "mime_types": tuple(self.data),
                "session_is_owner": False,
            },
        )
        with self.assertRaisesRegex(PortalError, "exceeds 1 MiB"):
            capture_clipboard(self.content, lambda: None)
        self.bus.calls.clear()

        def locked():
            raise PortalError("locked")

        with self.assertRaisesRegex(PortalError, "locked"):
            capture_clipboard(self.content, locked)
        self.assertEqual(self.bus.calls, [])

    def test_copy_during_backup_aborts_without_publishing(self):
        self.bus.emit(
            "SelectionOwnerChanged",
            "/session/codex",
            {"mime_types": tuple(self.data), "session_is_owner": False},
        )
        original = self.bus.call

        def call(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[1] == "SelectionRead":
                self.bus.emit("SelectionOwnerChanged", "/session/codex", {})
            return result

        with (
            patch.object(self.bus, "call", side_effect=call),
            self.assertRaisesRegex(PortalError, "clipboard changed"),
        ):
            capture_clipboard(self.content, lambda: None)
        self.assertEqual(
            [method for _, method, _, _, _ in self.bus.calls],
            ["RequestClipboard", "SelectionRead"],
        )
