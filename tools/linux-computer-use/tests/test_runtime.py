import asyncio
import threading
import unittest
from types import SimpleNamespace

from codex_linux_computer_use.dbus import PortalError
from codex_linux_computer_use.runtime import DesktopRuntime


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.threads = []

        def record(event):
            self.events.append(event)
            self.threads.append(threading.get_ident())

        def factory():
            record("create")
            return SimpleNamespace(
                bus=SimpleNamespace(poll=lambda: record("poll")),
                release_inputs=lambda: record("release"),
                stop=lambda: record("stop"),
                close=lambda: record("close"),
            )

        self.runtime = DesktopRuntime(factory)
        self.addAsyncCleanup(self.runtime.close)

    async def test_actions_and_cleanup_share_one_owner_thread(self):
        for value in ("first", "second"):
            self.assertEqual(
                await self.runtime.run(lambda desktop, value=value: value), value
            )
        await self.runtime.close()
        await self.runtime.close()
        self.assertEqual(self.events, ["create", "poll", "poll", "close"])
        self.assertEqual(len(set(self.threads)), 1)
        self.assertNotEqual(self.threads[0], threading.get_ident())
        with self.assertRaisesRegex(PortalError, "closed"):
            await self.runtime.run(lambda desktop: None)

    async def test_cancelled_action_holds_slot_until_input_cleanup_finishes(self):
        started = asyncio.Event()
        finish = threading.Event()
        self.addCleanup(finish.set)
        loop = asyncio.get_running_loop()

        def action(desktop):
            loop.call_soon_threadsafe(started.set)
            self.assertTrue(desktop.bus.cancel_event.wait(2))
            self.assertTrue(finish.wait(2))
            return "discard this result"

        task = asyncio.create_task(self.runtime.run(action))
        await asyncio.wait_for(started.wait(), 2)
        with self.assertRaisesRegex(PortalError, "still running"):
            await self.runtime.run(lambda desktop: None)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaisesRegex(PortalError, "still running"):
            await self.runtime.run(lambda desktop: None)
        finish.set()
        await asyncio.wait_for(asyncio.shield(self.runtime.pending), 2)
        self.assertEqual(await self.runtime.run(lambda desktop: "next"), "next")
        self.assertEqual(self.events, ["create", "poll", "release", "stop", "poll"])

    async def test_cancel_after_worker_completion_still_stops_session(self):
        started = asyncio.Event()
        finish = threading.Event()
        self.addCleanup(finish.set)
        loop = asyncio.get_running_loop()

        def action(desktop):
            loop.call_soon_threadsafe(started.set)
            self.assertTrue(finish.wait(2))
            return "completed"

        task = asyncio.create_task(self.runtime.run(action))
        await asyncio.wait_for(started.wait(), 2)
        self.runtime.pending.add_done_callback(lambda future: task.cancel())
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(asyncio.shield(self.runtime.pending), 2)
        self.assertEqual(self.events, ["create", "poll", "stop"])

    async def test_action_failure_releases_inputs_and_preserves_error(self):
        def action(desktop):
            raise ValueError("invalid action")

        with self.assertRaisesRegex(ValueError, "invalid action"):
            await self.runtime.run(action)
        self.assertEqual(self.events, ["create", "poll", "release"])
        self.assertIsNone(self.runtime.desktop.bus.cancel_event)

    async def test_shutdown_cancels_work_before_closing_connection(self):
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def action(desktop):
            loop.call_soon_threadsafe(started.set)
            self.assertTrue(desktop.bus.cancel_event.wait(2))

        task = asyncio.create_task(self.runtime.run(action))
        await asyncio.wait_for(started.wait(), 2)
        await self.runtime.close()
        with self.assertRaisesRegex(PortalError, "cancelled"):
            await task
        self.assertEqual(self.events, ["create", "poll", "release", "close"])


if __name__ == "__main__":
    unittest.main()
