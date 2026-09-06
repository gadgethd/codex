"""Operate the KDE permission dialog only in the disposable test container."""

import os
import sys
import time
from pathlib import Path

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

assert Path("/run/.containerenv").exists()
assert os.environ["XDG_RUNTIME_DIR"] == os.environ["CUA_PRIVATE_RUNTIME"]
assert os.environ["XDG_RUNTIME_DIR"].startswith("/tmp/cua-")
assert not os.environ.get("DISPLAY")
Atspi.set_timeout(1000, 1000)

for attempt in range(40):
    desktop = Atspi.get_desktop(0)
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        if app.get_name() != "xdg-desktop-portal-kde":
            continue
        pending, nodes = [app], []
        while pending and len(nodes) < 400:
            node = pending.pop()
            nodes.append(node)
            pending.extend(
                node.get_child_at_index(i)
                for i in range(min(node.get_child_count(), 40))
            )
        for node in nodes:
            if node.get_name() == "Allow restoring on future sessions" and (
                node.get_state_set().contains(Atspi.StateType.CHECKED)
            ):
                assert node.do_action(0)
        for node in nodes:
            if (
                node.get_role_name() == "button"
                and node.get_name() == "Approve"
                and node.get_state_set().contains(Atspi.StateType.SENSITIVE)
            ):
                assert node.do_action(0)
                # The dialog is destroyed immediately; do not inspect stale nodes.
                sys.exit(0)
    time.sleep(0.5)
raise RuntimeError("Private KDE permission dialog was not available")
