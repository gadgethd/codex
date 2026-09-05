"""Approve only the portal dialog in the smoke test's private desktop."""

import os
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

assert os.environ["XDG_RUNTIME_DIR"] == os.environ["CUA_PRIVATE_RUNTIME"]
assert os.environ["XDG_RUNTIME_DIR"].startswith("/tmp/cua-")
assert not os.environ.get("DISPLAY")
Atspi.set_timeout(1000, 1000)
for _ in range(500):
    desktop = Atspi.get_desktop(0)
    for index in range(min(desktop.get_child_count(), 100)):
        app = desktop.get_child_at_index(index)
        if app.get_name() != "xdg-desktop-portal-gnome":
            continue
        pending, nodes = [app], []
        while pending and len(nodes) < 500:
            node = pending.pop()
            nodes.append(node)
            pending.extend(
                filter(
                    None,
                    (
                        node.get_child_at_index(i)
                        for i in range(min(node.get_child_count(), 30))
                    ),
                )
            )
        for node in nodes:
            if (
                node.get_role_name() == "switch"
                and node.get_child_count() == 0
                and node.get_name()
                in ("Allow Remote Interaction", "Allow Clipboard Access")
                and not node.get_state_set().contains(Atspi.StateType.CHECKED)
            ):
                node.do_action(0)
        time.sleep(0.3)
        for node in nodes:
            if (
                node.get_role_name() == "button"
                and node.get_name() == "Share"
                and node.get_state_set().contains(Atspi.StateType.SENSITIVE)
            ):
                assert node.do_action(0)
                raise SystemExit(0)
    time.sleep(0.1)
raise RuntimeError("Private portal consent dialog did not appear")
