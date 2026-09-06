import hashlib
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from codex_linux_computer_use.apps import list_apps
from codex_linux_computer_use.apps_worker import AccessibilityBus, discover, encode
from codex_linux_computer_use.dbus import PortalError


class AppBus:
    identity = "desktop-a"

    def __init__(self, names):
        self.names = names
        self.failures = {}

    def desktop_id(self, owner):
        return None

    def property(self, owner, path, interface, name):
        if owner.startswith(":") and owner in self.failures:
            raise self.failures[owner]
        if owner == "org.a11y.atspi.Registry":
            return len(self.names)
        if name == "ToolkitName":
            return "GTK"
        if name == "ChildCount":
            return 1
        return self.names[int(owner[1:])] if path == "/root" else "Editor"

    def child(self, owner, path, index):
        if owner == "org.a11y.atspi.Registry":
            return f":{index}", "/root"
        return owner, "/window"


class AppDiscoveryTests(unittest.TestCase):
    def test_pagination_preserves_app_records_and_identity_ignores_labels(self):
        bus = AppBus([f"App {n}" for n in range(10)])
        page = discover(bus, 0)
        expected = [
            {
                "id": hashlib.sha256(f"desktop-a\0:{n}\0/root".encode()).hexdigest()[
                    :32
                ],
                "name": f"App {n}",
                "desktop_id": None,
                "toolkit": "GTK",
                "window": "Editor",
            }
            for n in range(8)
        ]
        self.assertEqual(
            page,
            {"apps": expected, "next_cursor": 8, "unavailable": 0, "limited": False},
        )
        last = discover(bus, page["next_cursor"])
        self.assertEqual([app["name"] for app in last["apps"]], ["App 8", "App 9"])
        self.assertIsNone(last["next_cursor"])
        bus.names[0] = "Renamed"
        self.assertEqual(discover(bus, 0)["apps"][0]["id"], expected[0]["id"])
        bus.identity = "desktop-b"
        self.assertNotEqual(discover(bus, 0)["apps"][0]["id"], expected[0]["id"])

    def test_unresponsive_apps_are_counted_and_deadline_returns_partial_page(self):
        bus = AppBus(["Closed", "Working", "Slow", "Next"])
        bus.failures = {":0": ValueError("Disconnected"), ":2": TimeoutError()}
        page = discover(bus, 0)
        self.assertEqual([app["name"] for app in page["apps"]], ["Working"])
        self.assertEqual((page["unavailable"], page["next_cursor"]), (1, 2))
        del bus.failures[":2"]
        self.assertEqual(
            [app["name"] for app in discover(bus, 2)["apps"]], ["Slow", "Next"]
        )

    def test_deadline_before_query_preserves_cursor_and_no_progress_fails(self):
        bus = AppBus(["First", "Second"])
        child = bus.child

        def next_child(owner, path, index):
            if owner == "org.a11y.atspi.Registry" and index == 1:
                raise TimeoutError()
            return child(owner, path, index)

        with patch.object(bus, "child", side_effect=next_child):
            page = discover(bus, 0)
        self.assertEqual((page["next_cursor"], page["unavailable"]), (1, 0))
        self.assertEqual([app["name"] for app in discover(bus, 1)["apps"]], ["Second"])
        with (
            patch.object(bus, "child", side_effect=TimeoutError()),
            self.assertRaises(TimeoutError),
        ):
            discover(bus, 0)

    def test_malformed_native_property_does_not_discard_other_apps(self):
        bus = AppBus(["First", "Malformed", "Last"])
        prop = bus.property

        def reply(owner, path, interface, method, signature, args):
            return () if owner == ":1" else (prop(owner, path, *args),)

        bus.call = reply
        bus.property = AccessibilityBus.property.__get__(bus)
        page = discover(bus, 0)
        self.assertEqual([app["name"] for app in page["apps"]], ["First", "Last"])
        self.assertEqual(page["unavailable"], 1)

    def test_escaped_labels_and_registry_size_obey_hard_limits(self):
        bus = AppBus(["\x01" * 1000] * 4100)
        page = discover(bus, 0)
        self.assertLessEqual(len(encode(page)), 4096)
        self.assertTrue(page["limited"])
        self.assertGreater(page["next_cursor"], 0)
        self.assertLess(page["next_cursor"], 8)
        page = discover(bus, 4095)
        self.assertIsNone(page["next_cursor"])
        bus.names = ["日本語🐧" * 1000]
        app = discover(bus, 0)["apps"][0]
        self.assertLessEqual(len(app["name"].encode()), 96)

    def test_oversized_single_record_advances_to_the_next_application(self):
        bus = AppBus(["Oversized", "Working"])
        original = bus.property
        bus.desktop_id = lambda owner: "\x01" * 504 + ".desktop"
        bus.property = lambda owner, path, interface, name: (
            "\x01" * 200
            if owner == ":0" and name != "ChildCount"
            else original(owner, path, interface, name)
        )
        page = discover(bus, 0)
        self.assertEqual([app["name"] for app in page["apps"]], ["Working"])
        self.assertEqual(page["unavailable"], 1)
        self.assertIsNone(page["next_cursor"])
        self.assertLessEqual(len(encode(page)), 4096)


class AppWorkerTests(unittest.TestCase):
    def worker(self, code):
        popen = subprocess.Popen
        children = []

        def start(args, **kwargs):
            proc = popen([sys.executable, "-c", code], **kwargs)
            children.append(proc)
            return proc

        with (
            patch("codex_linux_computer_use.apps.subprocess.Popen", side_effect=start),
            patch("codex_linux_computer_use.apps.DISCOVERY_TIMEOUT", 0.5),
        ):
            try:
                return list_apps()
            finally:
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].poll())

    def test_completed_worker_returns_data_and_hung_worker_is_reaped(self):
        self.assertEqual(json.loads(self.worker('print("{}")')), {})
        with self.assertRaisesRegex(PortalError, "timed out"):
            self.worker('import time; print("{}", flush=True); time.sleep(60)')

    def test_failure_and_invalid_or_large_output_never_become_app_results(self):
        for code in [
            "raise SystemExit(1)",
            'print("bad")',
            'print("x" * 5000)',
            'print("[]")',
        ]:
            with self.subTest(code=code), self.assertRaises(PortalError):
                self.worker(code)


if __name__ == "__main__":
    unittest.main()
