import unittest
from unittest.mock import patch

from codex_linux_computer_use.apps_worker import REGISTRY, encode, identifier
from codex_linux_computer_use.state_worker import TEXT, inspect_app


class StateBus:
    identity = "desktop"
    toolkit = "GTK"

    def __init__(self):
        self.nodes = {
            "/root": {"name": "Editor", "children": ["/text", "/password"]},
            "/text": {"role": 61, "text": "日本語🐧" * 80},
            "/password": {"role": 40, "text": "secret"},
        }
        self.failures = {}
        self.calls = []

    def child(self, owner, path, index):
        if owner == REGISTRY:
            return ":1.0", "/root"
        return owner, self.nodes[path].get("children", [])[index]

    def property(self, owner, path, interface, name):
        if owner == REGISTRY:
            return 1
        if name == "ToolkitName":
            return self.toolkit
        value = self.nodes[path]
        if name == "ChildCount":
            return len(value.get("children", []))
        if name == "CharacterCount":
            return (
                len(value["text"].encode("utf-16-le")) // 2
                if self.toolkit == "Qt"
                else len(value["text"])
            )
        return value.get("name", "")

    def call(self, owner, path, interface, method, signature=None, args=()):
        self.calls.append((path, method, args))
        if path in self.failures:
            raise self.failures[path]
        value = self.nodes[path]
        if method == "GetText":
            if self.toolkit == "Qt":
                data = value["text"].encode("utf-16-le")[2 * args[0] : 2 * args[1]]
                return (data.decode("utf-16-le", errors="ignore"),)
            return (value.get("text", "")[slice(*args)],)
        return {
            "GetRole": (value.get("role", 75),),
            "GetRoleName": ("text" if value.get("role") == 61 else "container",),
            "GetState": ([(1 << 7) | (1 << 12), 1 << 11],),
            "GetInterfaces": ([TEXT] if "text" in value else [],),
        }[method]


class StateTests(unittest.TestCase):
    def setUp(self):
        self.bus = StateBus()
        self.app_id = identifier(self.bus, ":1.0", "/root")

    def read(self, path=(), cursor=0, offset=0):
        return inspect_app(self.bus, self.app_id, list(path), cursor, offset)

    def test_navigates_controls_and_pages_exact_unicode_text(self):
        root = self.read()
        self.assertEqual(
            root["node"],
            {
                "id": self.app_id,
                "path": [],
                "role": "container",
                "name": "Editor",
                "states": ["editable", "focused", "readOnly"],
                "child_count": 2,
                "password": False,
            },
        )
        path = root["children"][0]["path"]
        self.assertEqual(path, [0])
        text, offset = "", 0
        for _ in range(3):
            page = self.read(path, offset=offset)
            text += page["text"]
            offset = page["next_text_offset"]
        self.assertEqual((text, offset), (self.bus.nodes["/text"]["text"], None))
        self.assertEqual(self.read([1])["text"], None)
        self.assertFalse(
            any(
                path == "/password" and method == "GetText"
                for path, method, _ in self.bus.calls
            )
        )

    def test_stale_app_path_and_text_offsets_fail(self):
        for path, offset in [([2], 0), ([0], 1000)]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.read(path, offset=offset)
        self.bus.identity = "different-session"
        with self.assertRaisesRegex(ValueError, "no longer registered"):
            self.read()

    def test_unavailable_children_and_deadlines_preserve_next_child(self):
        self.bus.failures["/text"] = ValueError("malformed")
        page = self.read()
        self.assertEqual(
            (page["unavailable"], [n["path"] for n in page["children"]]), (1, [[1]])
        )
        self.bus.failures = {"/password": TimeoutError()}
        page = self.read()
        self.assertEqual((page["next_cursor"], page["unavailable"]), (1, 0))
        self.bus.failures.clear()
        self.assertEqual(self.read(cursor=1)["children"][0]["path"], [1])
        self.bus.failures["/text"] = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.read()

    def test_escaped_names_broad_trees_and_depth_stay_bounded(self):
        self.bus.nodes["/root"]["children"] = ["/root"] * 4100
        self.bus.nodes["/root"]["name"] = "\x01" * 1000
        self.bus.nodes["/root"]["text"] = "\x02" * 1000
        page = self.read()
        self.assertTrue(page["limited"])
        self.assertGreater(page["next_cursor"], 0)
        self.assertLessEqual(len(encode(page)), 4096)
        end = self.read(cursor=4095)
        self.assertIsNone(end["next_cursor"])
        deep = self.read([0] * 16)
        self.assertEqual(
            (deep["children"], deep["next_cursor"], deep["limited"]), ([], None, True)
        )

    def test_bad_native_states_and_oversized_text_are_rejected(self):
        original = self.bus.call
        for method, reply in [("GetState", ([],)), ("GetText", ("x" * 130,))]:

            def malformed(
                owner, path, interface, called, *args, reply=reply, method=method
            ):
                return (
                    reply
                    if called == method
                    else original(owner, path, interface, called, *args)
                )

            with (
                patch.object(self.bus, "call", side_effect=malformed),
                self.assertRaises(ValueError),
            ):
                self.read([0])

    def test_qt_utf16_boundaries_preserve_emoji(self):
        self.bus.toolkit = "Qt"
        expected = "a" * 127 + "🐧" + "b" * 126 + "🐧tail"
        self.bus.nodes["/text"]["text"] = expected
        text, offset = "", 0
        for _ in range(4):
            page = self.read([0], offset=offset)
            text += page["text"]
            offset = page["next_text_offset"]
            if offset is None:
                break
        self.assertEqual((text, offset), (expected, None))
