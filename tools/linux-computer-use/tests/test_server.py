import base64
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_linux_computer_use.dbus import PortalError
from codex_linux_computer_use.policy import POLICY_KEY
from codex_linux_computer_use.portal import PortalDesktop
from codex_linux_computer_use.server import create_server
from mcp import Client, StdioServerParameters
from mcp.types import ImageContent, TextContent
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
        with patch(
            "codex_linux_computer_use.capture.capture_png",
            return_value={"png": PNG, "width": 1, "height": 1},
        ):
            async with Client(self.server) as client:
                tools = await client.list_tools()
                self.assertEqual(
                    [tool.name for tool in tools.tools],
                    [
                        "start_session",
                        "screenshot",
                        "stop_session",
                        "list_apps",
                        "get_app_state",
                        "get_actions",
                        "perform_action",
                        "move_pointer",
                        "click",
                        "drag",
                        "scroll",
                        "press_key",
                        "paste_text",
                    ],
                )
                started = await client.call_tool(
                    "start_session", meta={POLICY_KEY: self.policy}
                )
                self.assertFalse(started.is_error)
                self.assertEqual(
                    started.structured_content,
                    {"result": [{"stream": 42, "width": 1920, "height": 1080}]},
                )
                read_fd, write_fd = os.pipe()
                os.close(write_fd)
                with patch.object(
                    self.runtime.desktop.bus, "call", return_value=read_fd
                ):
                    result = await client.call_tool(
                        "screenshot", {"stream": 42}, meta={POLICY_KEY: self.policy}
                    )
                with self.assertRaises(OSError):
                    os.fstat(read_fd)
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
            (None, "requires Linux policy metadata"),
            ({}, "requires Linux policy metadata"),
            ({**self.policy, "version": True}, "requires Linux policy metadata"),
            ({**self.policy, "enabled": False}, "disabled by Codex policy"),
            (
                {**self.policy, "defaultAppAccess": "deny"},
                "under this application policy",
            ),
            (
                {**self.policy, "desktopIds": {"private.desktop": "deny"}},
                "under this application policy",
            ),
            (
                {**self.policy, "desktopIds": {str(n): "allow" for n in range(257)}},
                "invalid Linux computer-use policy",
            ),
        ]
        async with Client(self.server) as client:
            for policy, message in policies:
                with self.subTest(policy=policy):
                    result = await client.call_tool(
                        "start_session", meta={POLICY_KEY: policy}
                    )
                    self.assertTrue(result.is_error)
                    self.assertIn(message, result.content[0].text)
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
                self.runtime.desktop,
                "screenshot",
                return_value={"png": PNG, "width": 1, "height": 1},
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

    async def test_stdio_entry_point_exposes_tools_and_rejects_missing_host_policy(
        self,
    ):
        transport = StdioServerParameters(
            command=sys.executable,
            args=["-m", "codex_linux_computer_use"],
            cwd=Path(__file__).resolve().parents[1],
        )
        async with Client(transport, read_timeout_seconds=10) as client:
            result = await client.call_tool("start_session")
            self.assertTrue(result.is_error)
            self.assertIn("requires Linux policy metadata", result.content[0].text)

    async def test_app_state_is_guarded_and_validates_paths(self):
        args = {"app_id": "a" * 32, "path": [0], "text_offset": 128}
        with patch(
            "codex_linux_computer_use.apps.get_app_state",
            return_value='{"text":"hello"}',
        ) as inspect:
            async with Client(self.server) as client:
                for invalid in [None, {**self.policy, "defaultAppAccess": "deny"}]:
                    result = await client.call_tool(
                        "get_app_state", args, meta={POLICY_KEY: invalid}
                    )
                    self.assertTrue(result.is_error)
                for changed in [
                    {"app_id": "bad"},
                    {"path": [True]},
                    {"path": [0] * 17},
                    {"cursor": -1},
                    {"text_offset": True},
                ]:
                    result = await client.call_tool(
                        "get_app_state",
                        {**args, **changed},
                        meta={POLICY_KEY: self.policy},
                    )
                    self.assertTrue(result.is_error)
                self.lock_check.side_effect = [True]
                result = await client.call_tool(
                    "get_app_state", args, meta={POLICY_KEY: self.policy}
                )
                self.assertTrue(result.is_error)
                inspect.assert_not_called()
                self.lock_check.side_effect = [False, False]
                result = await client.call_tool(
                    "get_app_state", args, meta={POLICY_KEY: self.policy}
                )
                self.assertEqual(result.content[0].text, '{"text":"hello"}')
                inspect.assert_called_once_with("a" * 32, (0,), 0, 128)
                self.lock_check.side_effect = [False, True]
                result = await client.call_tool(
                    "get_app_state", args, meta={POLICY_KEY: self.policy}
                )
                self.assertTrue(result.is_error)
                self.assertNotIn("hello", result.content[0].text)

    async def test_app_discovery_is_guarded_by_policy_lock_and_cursor_validation(self):
        with patch(
            "codex_linux_computer_use.apps.list_apps", return_value='{"apps":[]}'
        ) as discover:
            async with Client(self.server) as client:
                for args, policy in [
                    ({}, None),
                    ({}, {**self.policy, "enabled": False}),
                    ({}, {**self.policy, "desktopIds": {"denied.desktop": "deny"}}),
                    ({"cursor": True}, self.policy),
                    ({"cursor": 4096}, self.policy),
                ]:
                    result = await client.call_tool(
                        "list_apps", args, meta={"codex/linuxComputerUsePolicy": policy}
                    )
                    self.assertTrue(result.is_error)
                self.lock_check.side_effect = [True]
                result = await client.call_tool(
                    "list_apps", meta={"codex/linuxComputerUsePolicy": self.policy}
                )
                self.assertTrue(result.is_error)
                discover.assert_not_called()
                self.lock_check.side_effect = [False, False]
                result = await client.call_tool(
                    "list_apps",
                    {"cursor": 8},
                    meta={"codex/linuxComputerUsePolicy": self.policy},
                )
                self.assertFalse(result.is_error)
                self.assertEqual(result.content[0].text, '{"apps":[]}')
                discover.assert_called_once_with(8)
                self.lock_check.side_effect = [False, True]
                result = await client.call_tool(
                    "list_apps", meta={"codex/linuxComputerUsePolicy": self.policy}
                )
                self.assertTrue(result.is_error)
                self.assertNotIn('"apps"', result.content[0].text)


if __name__ == "__main__":
    unittest.main()
