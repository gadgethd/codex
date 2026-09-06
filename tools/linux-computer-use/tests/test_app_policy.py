import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codex_linux_computer_use.apps_worker import AccessibilityBus, identifier
from codex_linux_computer_use.dbus import PortalError
from codex_linux_computer_use.policy import POLICY_KEY, LinuxPolicy
from codex_linux_computer_use.server import create_server
from codex_linux_computer_use.state_worker import inspect_app
from mcp import Client
from test_server import FakeRuntime
from test_state import StateBus


class AppPolicyTests(unittest.TestCase):
    def test_known_unknown_and_disabled_access(self):
        for default, rules, allowed in [
            ("allow", {}, [None, "editor.desktop", "other.desktop"]),
            ("deny", {"editor.desktop": "allow"}, ["editor.desktop"]),
            ("allow", {"other.desktop": "deny"}, ["editor.desktop"]),
        ]:
            policy = LinuxPolicy(True, default, rules, False)
            for identity in [None, "editor.desktop", "other.desktop"]:
                with self.subTest(default=default, rules=rules, identity=identity):
                    if identity in allowed:
                        policy.require_app(identity)
                    else:
                        with self.assertRaises(PermissionError):
                            policy.require_app(identity)
        with self.assertRaises(PortalError):
            LinuxPolicy(False, "allow", {}, False).require_app("editor.desktop")

    def test_embedded_owner_is_checked_and_identity_change_discards_text(self):
        state = StateBus()
        native = object.__new__(AccessibilityBus)
        native.identity = state.identity
        native.deadline = time.monotonic() + 5
        native.policy = LinuxPolicy(True, "deny", {"editor.desktop": "allow"}, False)
        identities = {":1.0": "editor.desktop", ":1.1": "denied.desktop"}
        native.desktop_id = lambda owner: identities[owner]
        native.Gio = SimpleNamespace(
            DBusCallFlags=SimpleNamespace(NO_AUTO_START=1),
            dbus_is_unique_name=lambda owner: owner.startswith(":"),
        )
        native.GLib = SimpleNamespace(
            Variant=lambda signature, args: args, Error=RuntimeError
        )

        def reply(owner, path, interface, method, args, *rest):
            if method == "Get":
                value = (state.property(owner, path, *args),)
            elif method == "GetChildAtIndex":
                value = (
                    ((":1.1", "/password"),)
                    if path == "/root" and args == (1,)
                    else (state.child(owner, path, args[0]),)
                )
            else:
                value = state.call(owner, path, interface, method, args=args or ())
            return SimpleNamespace(unpack=lambda: value)

        transport = Mock(side_effect=reply)
        native.bus = SimpleNamespace(call_sync=transport)
        app = identifier(native, ":1.0", "/root")
        page = inspect_app(native, app, [], 0, 0)
        self.assertEqual(
            ([child["path"] for child in page["children"]], page["unavailable"]),
            ([[0]], 1),
        )
        self.assertFalse(
            any(call.args[0] == ":1.1" for call in transport.call_args_list)
        )

        def changed(*args):
            value = reply(*args)
            if args[3] == "GetText":
                identities[":1.0"] = None
            return value

        transport.side_effect = changed
        with self.assertRaises(PermissionError):
            inspect_app(native, app, [0], 0, 0)


class AppPolicyProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_effective_app_policy_is_forwarded_but_desktop_remains_restricted(
        self,
    ):
        policy = {
            "version": 1,
            "enabled": True,
            "defaultAppAccess": "deny",
            "desktopIds": {"editor.desktop": "allow"},
            "allowLockedComputerUse": False,
        }
        with (
            patch("codex_linux_computer_use.server.is_locked", return_value=False),
            patch(
                "codex_linux_computer_use.apps.list_apps",
                return_value=json.dumps({"apps": []}),
            ) as discover,
        ):
            async with Client(create_server(FakeRuntime)) as client:
                result = await client.call_tool("list_apps", meta={POLICY_KEY: policy})
                self.assertFalse(result.is_error)
                discover.assert_called_once_with(
                    0, policy=LinuxPolicy.from_meta({POLICY_KEY: policy})
                )
                result = await client.call_tool(
                    "start_session", meta={POLICY_KEY: policy}
                )
                self.assertTrue(result.is_error)
