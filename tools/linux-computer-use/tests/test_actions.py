import json
import unittest
from unittest.mock import Mock, call, patch

from codex_linux_computer_use.action_worker import ACTION, actions
from codex_linux_computer_use.apps_worker import identifier
from codex_linux_computer_use.dbus import PortalError
from codex_linux_computer_use.server import create_server
from mcp import Client
from test_server import FakeRuntime
from test_state import StateBus


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.bus = StateBus()
        self.names = ["click", "open"]
        self.dispatched = []
        original = self.bus.property
        self.bus.property = lambda owner, path, interface, name: (
            len(self.names)
            if interface == ACTION
            else original(owner, path, interface, name)
        )

        def native_call(
            owner, path, interface, method, signature, args, *, before_call=None
        ):
            if before_call is not None:
                before_call()
            return (
                (self.names[args[0]],)
                if method == "GetName"
                else (self.dispatched.append(args[0]) is None,)
            )

        self.bus.call = native_call
        self.params = {
            "app_id": identifier(self.bus, ":1.0", "/root"),
            "node_id": identifier(self.bus, ":1.0", "/text"),
            "path": [0],
            "cursor": 0,
        }

    def test_listing_and_exact_action_dispatch(self):
        self.assertEqual(
            actions(self.bus, self.params),
            {
                "actions": [
                    {"index": 0, "name": "click"},
                    {"index": 1, "name": "open"},
                ],
                "next_cursor": None,
                "unavailable": 0,
                "limited": False,
            },
        )
        authorize = Mock()
        params = {**self.params, "action_index": 0, "action_name": "click"}
        self.assertEqual(actions(self.bus, params, authorize), {"accepted": True})
        self.assertEqual(authorize.call_args_list, [call("prepare"), call("dispatch")])
        self.assertEqual(self.dispatched, [0])

    def test_replaced_targets_and_changed_actions_are_not_dispatched(self):
        params = {**self.params, "action_index": 0, "action_name": "click"}
        for changed in [{"path": [1]}, {"action_name": "old"}, {"action_index": 9}]:
            authorize = Mock()
            with self.assertRaises(ValueError):
                actions(self.bus, {**params, **changed}, authorize)
            authorize.assert_not_called()
        with self.assertRaises(ValueError):
            actions(
                self.bus,
                params,
                lambda phase: self.names.reverse() if phase == "prepare" else None,
            )
        self.names.reverse()
        with self.assertRaises(PortalError):
            actions(self.bus, params, Mock(side_effect=[None, PortalError("Locked")]))
        self.assertEqual(self.dispatched, [])

    def test_action_pages_bound_escaped_names_and_skip_invalid_entries(self):
        self.names = ["x" * 97] + ["\x01" * 96] * 4096
        page = actions(self.bus, self.params)
        self.assertEqual(page["unavailable"], 1)
        self.assertTrue(page["limited"])
        self.assertTrue(1 < page["next_cursor"] < 8)
        self.assertLessEqual(len(json.dumps(page).encode()), 4096)


class ActionToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_effective_policy_validation_and_fresh_lock_guard_dispatch(self):
        server = create_server(FakeRuntime)
        policy = {
            "version": 1,
            "enabled": True,
            "defaultAppAccess": "allow",
            "desktopIds": {},
            "allowLockedComputerUse": False,
        }
        args = {
            "app_id": "a" * 32,
            "node_id": "b" * 32,
            "path": [0],
            "action_index": 0,
            "action_name": "click",
        }
        effects = []

        def dispatch(params, *, poll, check_lock, policy):
            try:
                policy.require_app("editor.desktop")
            except PermissionError as error:
                raise PortalError("Action was not dispatched: denied app") from error
            check_lock()
            effects.append(params)
            return {"accepted": True}

        with (
            patch(
                "codex_linux_computer_use.action_tools.perform", side_effect=dispatch
            ),
            patch(
                "codex_linux_computer_use.server.is_locked", return_value=False
            ) as locked,
        ):
            async with Client(server) as client:

                async def call(values, rule):
                    return await client.call_tool(
                        "perform_action",
                        values,
                        meta={"codex/linuxComputerUsePolicy": rule},
                    )

                for changed, rule in [
                    ({}, None),
                    ({}, {**policy, "defaultAppAccess": "deny"}),
                    ({"path": [True]}, policy),
                    ({"action_index": True}, policy),
                    ({"node_id": "bad"}, policy),
                ]:
                    result = await call({**args, **changed}, rule)
                    self.assertTrue(result.is_error)
                locked.side_effect = [False, True]
                result = await call(args, policy)
                self.assertTrue(result.is_error)
                self.assertEqual(effects, [])
                locked.side_effect = [False, False, False]
                result = await call(
                    args,
                    {
                        **policy,
                        "defaultAppAccess": "deny",
                        "desktopIds": {"editor.desktop": "allow"},
                    },
                )
                self.assertEqual(result.structured_content, {"accepted": True})
                self.assertEqual(effects, [{**args, "path": (0,)}])
                for state in (True, PortalError("Unknown lock state")):
                    locked.side_effect = [False, False, state]
                    result = await call(args, policy)
                    self.assertTrue(result.is_error)
                    self.assertIn("outcome uncertain", result.content[0].text)
                self.assertEqual(len(effects), 3)
