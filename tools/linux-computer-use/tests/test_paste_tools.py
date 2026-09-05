import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from codex_linux_computer_use.policy import POLICY_KEY
from codex_linux_computer_use.portal import PortalDesktop
from codex_linux_computer_use.runtime import DesktopRuntime
from codex_linux_computer_use.server import create_server
from mcp import Client
from test_clipboard import ClipboardBus


class PasteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bus = ClipboardBus()
        self.bus.results["Start"]["clipboard_enabled"] = True
        self.desktop = PortalDesktop(self.bus)
        self.runtime = DesktopRuntime(lambda: self.desktop)
        self.server = create_server(lambda: self.runtime)
        self.meta = {
            POLICY_KEY: {
                "version": 1,
                "enabled": True,
                "defaultAppAccess": "allow",
                "desktopIds": {},
                "allowLockedComputerUse": False,
            }
        }
        self.lock = patch(
            "codex_linux_computer_use.server.is_locked", return_value=False
        )
        self.lock_check = self.lock.start()
        self.addCleanup(self.lock.stop)
        self.addAsyncCleanup(self.runtime.close)
        self.previous = {
            "text/plain": b"private previous text",
            "text/html": b"<b>private</b>",
        }
        self.writes = []
        self.after_read = lambda: None
        self.after_press = lambda: None
        original = self.bus.call

        def call(interface, method, signature, values, **kwargs):
            if method in ("SelectionRead", "SelectionWrite"):
                with tempfile.TemporaryFile() as file:
                    if method == "SelectionRead":
                        file.write(self.previous[values[1]])
                        file.seek(0)
                    else:
                        fd = os.dup(file.fileno())
                        self.addCleanup(os.close, fd)
                        self.writes.append(fd)
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
            elif method == "SelectionRead":
                self.after_read()
            elif method == "NotifyKeyboardKeysym" and values[3] == 1:
                if values[2] in (ord("v"), 0xFF63):
                    self.bus.emit(
                        "SelectionTransfer",
                        self.desktop.session,
                        "text/plain;charset=utf-8",
                        100,
                    )
                self.after_press()
            return result

        self.bus.call = call

    async def start(self, client):
        result = await client.call_tool(
            "start_session", {"clipboard": True}, meta=self.meta
        )
        self.assertFalse(result.is_error, result.content)
        self.bus.emit(
            "SelectionOwnerChanged",
            self.desktop.session,
            {
                "mime_types": tuple(self.previous),
                "session_is_owner": False,
            },
        )
        self.bus.calls.clear()

    async def test_unicode_shortcuts_restore_all_formats_without_returning_them(self):
        text = "Native — Ελληνικά 日本語 🐧\nمرحبا 한국어"
        async with Client(self.server) as client:
            await self.start(client)
            for shortcut, keys in (
                ("ctrl+v", [0xFFE3, ord("v")]),
                ("ctrl+shift+v", [0xFFE3, 0xFFE1, ord("v")]),
                ("shift+insert", [0xFFE1, 0xFF63]),
            ):
                self.bus.calls.clear()
                result = await client.call_tool(
                    "paste_text", {"text": text, "shortcut": shortcut}, meta=self.meta
                )
                self.assertFalse(result.is_error, result.content)
                self.assertIn("Previous clipboard restored", result.content[0].text)
                self.assertNotIn("private", result.content[0].text)
                self.assertEqual(self.desktop.clipboard.data, self.previous)
                os.lseek(self.writes[-1], 0, os.SEEK_SET)
                self.assertEqual(os.read(self.writes[-1], 16384), text.encode())
                self.assertEqual(
                    [
                        values[2:]
                        for _, method, _, values, _ in self.bus.calls
                        if method == "NotifyKeyboardKeysym"
                    ],
                    [(key, 1) for key in keys] + [(key, 0) for key in reversed(keys)],
                )
                self.assertEqual(self.desktop.pressed, {})

    async def test_empty_clipboard_is_released_using_an_absent_mime_option(self):
        self.previous = {}
        async with Client(self.server) as client:
            await self.start(client)
            result = await client.call_tool(
                "paste_text", {"text": "hello"}, meta=self.meta
            )
            self.assertFalse(result.is_error, result.content)
            self.assertEqual(
                [
                    values[1]
                    for _, method, _, values, _ in self.bus.calls
                    if method == "SetSelection"
                ][-1],
                {},
            )
            self.assertEqual(self.desktop.clipboard.data, {})

    async def test_unavailable_initial_clipboard_is_reported_honestly(self):
        async with Client(self.server) as client:
            started = await client.call_tool(
                "start_session", {"clipboard": True}, meta=self.meta
            )
            self.assertFalse(started.is_error, started.content)
            result = await client.call_tool(
                "paste_text", {"text": "hello"}, meta=self.meta
            )
            self.assertFalse(result.is_error, result.content)
            self.assertIn(
                "Previous clipboard state unavailable", result.content[0].text
            )
            self.assertEqual(set(self.desktop.clipboard.data.values()), {b"hello"})

    async def test_external_copy_after_modifier_stops_paste_and_preserves_new_content(
        self,
    ):
        async with Client(self.server) as client:
            await self.start(client)
            self.after_press = lambda: self.bus.emit(
                "SelectionOwnerChanged",
                self.desktop.session,
                {
                    "mime_types": ["text/plain"],
                    "session_is_owner": False,
                },
            )
            result = await client.call_tool(
                "paste_text", {"text": "hello"}, meta=self.meta
            )
            self.assertTrue(result.is_error, result.content)
            self.assertIn("clipboard changed", result.content[0].text)
            self.assertEqual(
                [
                    values[2:]
                    for _, method, _, values, _ in self.bus.calls
                    if method == "NotifyKeyboardKeysym"
                ],
                [(0xFFE3, 1), (0xFFE3, 0)],
            )
            self.assertEqual(
                [method for _, method, _, _, _ in self.bus.calls].count("SetSelection"),
                1,
            )

    async def test_changed_or_oversized_backup_never_replaces_clipboard_or_sends_input(
        self,
    ):
        async with Client(self.server) as client:
            await self.start(client)
            for oversized in (False, True):
                if oversized:
                    self.previous = {
                        "text/plain": b"x" * 600000,
                        "text/html": b"y" * 600000,
                    }
                    self.after_read = lambda: None
                else:
                    self.after_read = lambda: self.bus.emit(
                        "SelectionOwnerChanged",
                        self.desktop.session,
                        {
                            "mime_types": tuple(self.previous),
                            "session_is_owner": False,
                        },
                    )
                self.bus.calls.clear()
                result = await client.call_tool(
                    "paste_text", {"text": "hello"}, meta=self.meta
                )
                self.assertTrue(result.is_error)
                self.assertFalse(
                    any(
                        method in ("SetSelection", "NotifyKeyboardKeysym")
                        for _, method, _, _, _ in self.bus.calls
                    )
                )

    async def test_external_copy_during_final_key_dispatch_stops_paste(self):
        original = self.bus.call

        def call(interface, method, signature, values, **kwargs):
            if method == "NotifyKeyboardKeysym" and values[2:] == (ord("v"), 1):
                self.bus.emit(
                    "SelectionOwnerChanged",
                    self.desktop.session,
                    {"mime_types": ["text/plain"], "session_is_owner": False},
                )
            return original(interface, method, signature, values, **kwargs)

        async with Client(self.server) as client:
            await self.start(client)
            with patch.object(self.bus, "call", side_effect=call):
                result = await client.call_tool(
                    "paste_text", {"text": "hello"}, meta=self.meta
                )
            self.assertTrue(result.is_error, result.content)
            self.assertIn("clipboard changed", result.content[0].text)
            self.assertEqual(
                [
                    values[2:]
                    for _, method, _, values, _ in self.bus.calls
                    if method == "NotifyKeyboardKeysym"
                ],
                [(0xFFE3, 1), (ord("v"), 0), (0xFFE3, 0)],
            )
            self.assertEqual(self.desktop.pressed, {})

    async def test_lock_after_modifier_restores_clipboard_and_releases_key(self):
        async with Client(self.server) as client:
            await self.start(client)
            self.after_press = lambda: setattr(self.lock_check, "return_value", True)
            result = await client.call_tool(
                "paste_text", {"text": "hello"}, meta=self.meta
            )
            self.assertTrue(result.is_error)
            self.assertEqual(
                [
                    values[2:]
                    for _, method, _, values, _ in self.bus.calls
                    if method == "NotifyKeyboardKeysym"
                ],
                [(0xFFE3, 1), (0xFFE3, 0)],
            )
            self.assertEqual(
                (self.desktop.pressed, self.desktop.clipboard.data), ({}, self.previous)
            )

    async def test_missing_permission_policy_and_invalid_arguments_do_not_paste(self):
        async with Client(self.server) as client:
            for name, args, meta in (
                ("start_session", {"clipboard": "true"}, self.meta),
                ("paste_text", {"text": "hello"}, None),
                ("paste_text", {"text": ""}, self.meta),
                ("paste_text", {"text": "x\0y"}, self.meta),
                ("paste_text", {"text": "🐧" * 5000}, self.meta),
                ("paste_text", {"text": "hello", "shortcut": "alt+v"}, self.meta),
            ):
                result = await client.call_tool(name, args, meta=meta)
                self.assertTrue(result.is_error)
                self.assertEqual(self.bus.calls, [])
            await client.call_tool("start_session", meta=self.meta)
            self.bus.calls.clear()
            result = await client.call_tool(
                "paste_text", {"text": "hello"}, meta=self.meta
            )
            self.assertTrue(result.is_error)
            self.assertIn("clipboard=true", result.content[0].text)
            self.assertEqual(self.bus.calls, [])

    async def test_failed_offer_closes_sharing(self):
        async with Client(self.server) as client:
            await self.start(client)
            self.bus.fail_method = "SetSelection"
            result = await client.call_tool(
                "paste_text", {"text": "hello"}, meta=self.meta
            )
            self.assertTrue(result.is_error)
            self.assertEqual(
                (self.desktop.session, self.desktop.clipboard), (None, None)
            )

    async def test_invalid_text_returns_actionable_errors_without_accessing_desktop(
        self,
    ):
        async with Client(self.server) as client:
            for text, message in (
                ("x\0y", "16 KiB UTF-8 and contain no NUL"),
                ("🐧" * 5000, "16 KiB UTF-8 and contain no NUL"),
                ("\ud800", "unicode string"),
            ):
                result = await client.call_tool(
                    "paste_text", {"text": text}, meta=self.meta
                )
                self.assertTrue(result.is_error)
                self.assertIn(message, result.content[0].text)
                self.assertEqual(self.bus.calls, [])

    async def test_cancelled_paste_releases_keys_and_stops_sharing(self):
        loop = asyncio.get_running_loop()
        pressed = asyncio.Event()
        self.after_press = lambda: loop.call_soon_threadsafe(pressed.set)
        async with Client(self.server) as client:
            await self.start(client)
            task = asyncio.create_task(
                client.call_tool("paste_text", {"text": "cancel me"}, meta=self.meta)
            )
            await asyncio.wait_for(pressed.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            async def cleaned():
                while self.runtime.busy:
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(cleaned(), 2)
            self.assertEqual((self.desktop.pressed, self.desktop.session), ({}, None))
