import asyncio
import unittest
from unittest.mock import patch

from codex_linux_computer_use.dbus import PortalError
from codex_linux_computer_use.policy import POLICY_KEY
from codex_linux_computer_use.portal import PortalDesktop
from codex_linux_computer_use.runtime import DesktopRuntime
from codex_linux_computer_use.server import create_server
from mcp import Client
from test_portal import FakeBus


class InputToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bus = FakeBus()
        self.desktop = PortalDesktop(self.bus)
        self.runtime = DesktopRuntime(lambda: self.desktop)
        await self.runtime.run(PortalDesktop.start)
        self.bus.calls.clear()
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

    def events(self):
        return [(method, values[2:]) for _, method, _, values, _ in self.bus.calls]

    async def test_move_double_click_and_scroll_send_complete_sequences(self):
        async with Client(self.server) as client:
            for name, args in [
                ("move_pointer", {"stream": 42, "x": 10, "y": 20}),
                (
                    "click",
                    {"stream": 42, "x": 30, "y": 40, "button": "right", "count": 2},
                ),
                (
                    "scroll",
                    {"stream": 42, "x": 50, "y": 60, "vertical": -2, "horizontal": 3},
                ),
            ]:
                result = await client.call_tool(name, args, meta=self.meta)
                self.assertFalse(result.is_error, result.content)
            self.assertEqual(
                self.events(),
                [
                    ("NotifyPointerMotionAbsolute", (42, 10.0, 20.0)),
                    ("NotifyPointerMotionAbsolute", (42, 30.0, 40.0)),
                    ("NotifyPointerButton", (273, 1)),
                    ("NotifyPointerButton", (273, 0)),
                    ("NotifyPointerButton", (273, 1)),
                    ("NotifyPointerButton", (273, 0)),
                    ("NotifyPointerMotionAbsolute", (42, 50.0, 60.0)),
                    ("NotifyPointerAxisDiscrete", (0, -2)),
                    ("NotifyPointerAxisDiscrete", (1, 3)),
                ],
            )
            self.assertEqual(self.desktop.pressed, {})

    async def test_chord_releases_all_modifiers_after_ambiguous_press_failure(self):
        original = self.bus.call

        def fail_letter(interface, method, signature, values, **kwargs):
            result = original(interface, method, signature, values, **kwargs)
            if method == "NotifyKeyboardKeysym" and values[2:] == (ord("a"), 1):
                raise PortalError("ambiguous press failure")
            return result

        with patch.object(self.bus, "call", side_effect=fail_letter):
            async with Client(self.server) as client:
                result = await client.call_tool(
                    "press_key", {"keys": ["CTRL", "a"]}, meta=self.meta
                )
                self.assertTrue(result.is_error)
                self.assertIn("ambiguous press failure", result.content[0].text)
                self.assertEqual(
                    self.events(),
                    [
                        ("NotifyKeyboardKeysym", (0xFFE3, 1)),
                        ("NotifyKeyboardKeysym", (ord("a"), 1)),
                        ("NotifyKeyboardKeysym", (ord("a"), 0)),
                        ("NotifyKeyboardKeysym", (0xFFE3, 0)),
                    ],
                )
                self.assertEqual(self.desktop.pressed, {})

    async def test_invalid_arguments_send_no_input(self):
        async with Client(self.server) as client:
            for name, args in [
                ("press_key", {"keys": ["CTRL", "é"]}),
                ("press_key", {"keys": ["CTRL", "CONTROL"]}),
                ("press_key", {"keys": []}),
                ("move_pointer", {"stream": True, "x": 1, "y": 2}),
                ("move_pointer", {"stream": 42, "x": True, "y": 2}),
                ("click", {"stream": 42, "x": 10, "y": 20, "count": 2.0}),
                ("click", {"stream": 42, "x": 10, "y": 20, "button": "invalid"}),
                ("scroll", {"stream": 42, "x": 10, "y": 20, "horizontal": 101}),
            ]:
                with self.subTest(name=name, args=args):
                    result = await client.call_tool(name, args, meta=self.meta)
                    self.assertTrue(result.is_error)
                    self.assertEqual((self.bus.calls, self.desktop.pressed), ([], {}))

    async def test_pointer_failures_release_the_button(self):
        original = self.bus.call
        async with Client(self.server) as client:
            for name, args, failure_method in [
                ("click", {"stream": 42, "x": 10, "y": 20}, "NotifyPointerButton"),
            ]:
                self.bus.calls.clear()

                def fail(
                    interface,
                    method,
                    signature,
                    values,
                    failure_method=failure_method,
                    **kwargs,
                ):
                    result = original(interface, method, signature, values, **kwargs)
                    if (
                        method == failure_method
                        and self.desktop.pressed
                        and (method != "NotifyPointerButton" or values[3] == 1)
                    ):
                        raise PortalError("pointer failed")
                    return result

                with patch.object(self.bus, "call", side_effect=fail):
                    result = await client.call_tool(name, args, meta=self.meta)
                self.assertTrue(result.is_error)
                self.assertEqual(self.events()[-1], ("NotifyPointerButton", (272, 0)))
                self.assertEqual(self.desktop.pressed, {})

    async def test_cancelled_click_releases_input_and_stops_sharing(self):
        loop = asyncio.get_running_loop()
        pressed = asyncio.Event()
        original = self.bus.call

        def signal_press(interface, method, signature, values, **kwargs):
            result = original(interface, method, signature, values, **kwargs)
            if method == "NotifyPointerButton" and values[3] == 1:
                loop.call_soon_threadsafe(pressed.set)
            return result

        with patch.object(self.bus, "call", side_effect=signal_press):
            async with Client(self.server) as client:
                task = asyncio.create_task(
                    client.call_tool(
                        "click",
                        {"stream": 42, "x": 10, "y": 20, "count": 3},
                        meta=self.meta,
                    )
                )
                await asyncio.wait_for(pressed.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

                async def cleaned():
                    while self.runtime.busy:
                        await asyncio.sleep(0.005)

                await asyncio.wait_for(cleaned(), 2)
                self.assertEqual(
                    (self.desktop.pressed, self.desktop.session), ({}, None)
                )

    async def test_every_input_tool_checks_host_policy_and_lock_state(self):
        async with Client(self.server) as client:
            for name, args in [
                ("move_pointer", {"stream": 42, "x": 10, "y": 20}),
                ("click", {"stream": 42, "x": 10, "y": 20}),
                ("scroll", {"stream": 42, "x": 10, "y": 20, "vertical": 1}),
                ("press_key", {"keys": ["ENTER"]}),
            ]:
                with self.subTest(name=name):
                    result = await client.call_tool(name, args)
                    self.assertTrue(result.is_error)
                    self.assertIn(
                        "requires Linux policy metadata", result.content[0].text
                    )
                    self.lock_check.return_value = True
                    result = await client.call_tool(name, args, meta=self.meta)
                    self.assertTrue(result.is_error)
                    self.assertIn("desktop is locked", result.content[0].text)
                    self.lock_check.return_value = False
                    self.assertEqual(self.bus.calls, [])

    async def test_lock_during_gesture_stops_new_input_but_releases_held_keys(self):
        original = self.bus.call

        def lock_after_press(interface, method, signature, values, **kwargs):
            result = original(interface, method, signature, values, **kwargs)
            if (
                method in ("NotifyPointerButton", "NotifyKeyboardKeysym")
                and values[3] == 1
            ):
                self.lock_check.return_value = True
            return result

        async with Client(self.server) as client:
            for name, args, first_event in [
                (
                    "press_key",
                    {"keys": ["CTRL", "a"]},
                    ("NotifyKeyboardKeysym", 0xFFE3),
                ),
                (
                    "click",
                    {"stream": 42, "x": 10, "y": 20, "count": 2},
                    ("NotifyPointerButton", 272),
                ),
            ]:
                with self.subTest(name=name):
                    self.bus.calls.clear()
                    self.lock_check.return_value = False
                    with patch.object(self.bus, "call", side_effect=lock_after_press):
                        result = await client.call_tool(name, args, meta=self.meta)
                    self.assertTrue(result.is_error)
                    method, code = first_event
                    prefix = (
                        []
                        if name == "press_key"
                        else [("NotifyPointerMotionAbsolute", (42, 10.0, 20.0))]
                    )
                    self.assertEqual(
                        self.events(),
                        [*prefix, (method, (code, 1)), (method, (code, 0))],
                    )
                    self.assertEqual(self.desktop.pressed, {})


if __name__ == "__main__":
    unittest.main()
