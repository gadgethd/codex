import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from codex_linux_computer_use.action_worker import actions
from codex_linux_computer_use.apps_worker import REGISTRY, AccessibilityBus, identifier
from codex_linux_computer_use.dbus import PortalError
from codex_linux_computer_use.policy import LinuxPolicy
from test_state import StateBus


class ActionPolicyTests(unittest.TestCase):
    def setUp(self):
        state = StateBus()
        self.native = object.__new__(AccessibilityBus)
        self.native.identity = state.identity
        self.native.deadline = time.monotonic() + 5
        self.native.policy = LinuxPolicy(
            True, "deny", {"root.desktop": "allow", "target.desktop": "allow"}, False
        )
        self.identities = {":1.0": "root.desktop", ":1.1": "target.desktop"}
        self.events = []
        self.effects = []
        self.after_effect = lambda: None
        self.native.Gio = SimpleNamespace(
            DBusCallFlags=SimpleNamespace(NO_AUTO_START=1),
            dbus_is_unique_name=lambda name: name.startswith(":"),
        )
        self.native.GLib = SimpleNamespace(
            Variant=lambda signature, args: args, Error=RuntimeError
        )

        def identity(owner):
            self.events.append(("identity", owner))
            return self.identities[owner]

        self.native.desktop_id = identity

        def reply(owner, path, interface, method, args, *rest):
            self.events.append(("native", method))
            if method == "Get":
                value = (
                    1 if args[1] == "NActions" else state.property(owner, path, *args),
                )
            elif method == "GetChildAtIndex":
                value = (
                    state.child(owner, path, args[0])
                    if owner == REGISTRY
                    else (":1.1", "/text"),
                )
            elif method == "GetName":
                value = ("click",)
            else:
                assert method == "DoAction"
                self.effects.append((owner, path, args[0]))
                self.after_effect()
                value = (True,)
            return SimpleNamespace(unpack=lambda: value)

        self.native.bus = SimpleNamespace(call_sync=Mock(side_effect=reply))
        self.params = {
            "app_id": identifier(self.native, ":1.0", "/root"),
            "node_id": identifier(self.native, ":1.1", "/text"),
            "path": [0],
            "action_index": 0,
            "action_name": "click",
        }

    def authorize(self, phase):
        self.events.append(("authorize", phase))

    def test_final_identity_check_precedes_dispatch_gate_and_native_effect(self):
        self.assertEqual(
            actions(self.native, self.params, self.authorize), {"accepted": True}
        )
        self.assertEqual(self.effects, [(":1.1", "/text", 0)])
        self.assertEqual(
            self.events[-4:],
            [
                ("identity", ":1.1"),
                ("authorize", "dispatch"),
                ("native", "DoAction"),
                ("identity", ":1.1"),
            ],
        )

    def test_denied_unknown_embedded_owner_and_revocation_never_dispatch(self):
        for identity in (None, "denied.desktop"):
            self.identities[":1.1"] = identity
            with self.assertRaises(PermissionError):
                actions(self.native, self.params, self.authorize)
        self.identities[":1.1"] = "target.desktop"

        def revoke(phase):
            self.authorize(phase)
            if phase == "prepare":
                self.identities[":1.1"] = None

        with self.assertRaises(PermissionError):
            actions(self.native, self.params, revoke)
        self.assertNotIn(("authorize", "dispatch"), self.events)
        self.assertEqual(self.effects, [])

    def test_lock_cancellation_and_expired_gate_prevent_native_effect(self):
        for failure in (PortalError("Locked"), PortalError("Cancelled")):

            def stop(phase, failure=failure):
                if phase == "dispatch":
                    raise failure

            with self.assertRaises(PortalError):
                actions(self.native, self.params, stop)

        def expire(phase):
            if phase == "dispatch":
                self.native.deadline = 0

        with self.assertRaises(TimeoutError):
            actions(self.native, self.params, expire)
        self.assertEqual(self.effects, [])

    def test_identity_loss_after_effect_is_rejected_without_another_effect(self):
        self.after_effect = lambda: self.identities.update({":1.1": None})
        with self.assertRaises(PermissionError):
            actions(self.native, self.params, self.authorize)
        self.assertEqual(self.effects, [(":1.1", "/text", 0)])
