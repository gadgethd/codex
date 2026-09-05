import base64
import unittest
from unittest.mock import patch

from mcp import Client
from mcp.types import ImageContent, TextContent

from codex_linux_computer_use.dbus import PortalError
from codex_linux_computer_use.policy import POLICY_KEY
from codex_linux_computer_use.portal import PortalDesktop
from codex_linux_computer_use.server import create_server
from test_portal import FakeBus

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB2kAAAAASUVORK5CYII="
)


class FakeRuntime:
    def __init__(self):
        self.desktop = PortalDesktop(FakeBus())

    async def run(self, action):
        return action(self.desktop)

    async def close(self):
        self.desktop.close()


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = FakeRuntime()
        self.server = create_server(lambda: self.runtime)
        self.policy = {
            "version": 1,
            "enabled": True,
            "defaultAppAccess": "allow",
            "desktopIds": {},
            "allowLockedComputerUse": False,
        }
        self.lock = patch(
            "codex_linux_computer_use.server.is_locked", return_value=False
        )
        self.lock_check = self.lock.start()
        self.addCleanup(self.lock.stop)

    async def test_protocol_session_capture_and_cleanup(self):
        with patch.object(self.runtime.desktop, "screenshot", return_value=(PNG, 1, 1)):
            async with Client(self.server) as client:
                tools = await client.list_tools()
                self.assertEqual(
                    [tool.name for tool in tools.tools],
                    ["start_session", "screenshot", "stop_session"],
                )
                started = await client.call_tool(
                    "start_session", meta={POLICY_KEY: self.policy}
                )
                self.assertFalse(started.is_error)
                self.assertEqual(
                    started.structured_content,
                    {"result": [{"stream": 42, "width": 1920, "height": 1080}]},
                )
                result = await client.call_tool(
                    "screenshot", {"stream": 42}, meta={POLICY_KEY: self.policy}
                )
                self.assertFalse(result.is_error)
                self.assertEqual(
                    result.content,
                    [
                        TextContent(type="text", text="PNG dimensions: 1x1."),
                        ImageContent(
                            type="image",
                            data=base64.b64encode(PNG).decode(),
                            mime_type="image/png",
                        ),
                    ],
                )
                stopped = await client.call_tool("stop_session")
                self.assertFalse(stopped.is_error)
                self.assertIsNone(self.runtime.desktop.session)
        self.assertTrue(self.runtime.desktop.bus.closed)

    async def test_missing_invalid_or_restrictive_policy_never_opens_desktop(self):
        policies = [
            None,
            {},
            {**self.policy, "version": True},
            {**self.policy, "enabled": False},
            {**self.policy, "defaultAppAccess": "deny"},
            {**self.policy, "desktopIds": {"private.desktop": "deny"}},
            {**self.policy, "desktopIds": {str(n): "allow" for n in range(257)}},
        ]
        async with Client(self.server) as client:
            for policy in policies:
                with self.subTest(policy=policy):
                    result = await client.call_tool(
                        "start_session", meta={POLICY_KEY: policy}
                    )
                    self.assertTrue(result.is_error)
                    self.assertLess(len(result.content[0].text), 700)
            forged = await client.call_tool("start_session", {"policy": self.policy})
            self.assertTrue(forged.is_error)
        self.assertEqual(self.runtime.desktop.bus.calls, [])

    async def test_lock_or_unknown_state_blocks_capture_and_post_capture_lock_discards_image(
        self,
    ):
        async with Client(self.server) as client:
            for state in (True, PortalError("Unknown lock state")):
                self.lock_check.side_effect = [state]
                result = await client.call_tool(
                    "screenshot", {"stream": 42}, meta={POLICY_KEY: self.policy}
                )
                self.assertTrue(result.is_error)
            self.lock_check.side_effect = [False, True]
            with patch.object(
                self.runtime.desktop, "screenshot", return_value=(PNG, 1, 1)
            ):
                result = await client.call_tool(
                    "screenshot", {"stream": 42}, meta={POLICY_KEY: self.policy}
                )
            self.assertTrue(result.is_error)
            self.assertTrue(all(item.type == "text" for item in result.content))

    async def test_policy_changes_are_checked_each_call_and_stop_remains_available(
        self,
    ):
        async with Client(self.server) as client:
            await client.call_tool("start_session", meta={POLICY_KEY: self.policy})
            result = await client.call_tool(
                "screenshot",
                {"stream": 42},
                meta={POLICY_KEY: {**self.policy, "enabled": False}},
            )
            self.assertTrue(result.is_error)
            result = await client.call_tool("stop_session")
            self.assertFalse(result.is_error)
        self.assertIsNone(self.runtime.desktop.session)


if __name__ == "__main__":
    unittest.main()
