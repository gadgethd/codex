"""Verify native capture, Unicode paste and clipboard preservation on KDE."""

import asyncio
import base64
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import gi
from accessibility import activate_button, verify_text
from app_policy import verify_reads
from dbus_identity import verify_activation
from mcp import Client, StdioServerParameters

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio

HERE = Path(__file__).resolve().parent
TEXT = (
    "a" * 127 + "🐧" + "b" * 126 + "🐧"
    "\nKDE native paste — café Ελληνικά 日本語 🐧\nSecond line: naïve مرحبا 한국어"
)
PREVIOUS = "Existing clipboard — preserved"
POLICY = {
    "version": 1,
    "enabled": True,
    "defaultAppAccess": "allow",
    "desktopIds": {},
    "allowLockedComputerUse": False,
}


async def wait_file(path, expected=None):
    for _ in range(150):
        if path.exists() and (expected is None or path.read_text() == expected):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"Missing expected observation: {path}")


async def exercise(output, spawn):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    launchers = Path(os.environ["XDG_DATA_HOME"]) / "applications"
    launchers.mkdir()
    for name in ("gtk", "qt"):
        (launchers / f"codex-fixture-{name}.desktop").write_text(
            "[Desktop Entry]\nType=Application\n"
            f"Name=Codex {name} fixture\n"
            f"Exec={sys.executable} {HERE / f'{name}_fixture.py'} %F\n"
        )

    def session_paths():
        paths = ["/org/freedesktop/portal/desktop/session"]
        for _ in range(2):
            children = []
            for path in paths:
                reply = bus.call_sync(
                    "org.freedesktop.portal.Desktop",
                    path,
                    "org.freedesktop.DBus.Introspectable",
                    "Introspect",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    1000,
                    None,
                )
                nodes = ET.fromstring(reply.unpack()[0]).findall("node")
                assert len(nodes) <= 4
                children.extend(f"{path}/{node.attrib['name']}" for node in nodes)
            paths = children
        return paths

    env = {
        key: os.environ[key]
        for key in (
            "DBUS_SESSION_BUS_ADDRESS",
            "WAYLAND_DISPLAY",
            "XDG_RUNTIME_DIR",
            "XDG_CURRENT_DESKTOP",
            "XDG_SESSION_TYPE",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
        )
    }
    transport = StdioServerParameters(
        command=sys.executable,
        args=["-m", "codex_linux_computer_use"],
        cwd=HERE.parents[1],
        env=env,
    )
    results = []
    async with Client(transport, read_timeout_seconds=150) as client:

        async def call(name, args=None, *, policy=POLICY):
            result = await client.call_tool(
                name, args or {}, meta={"codex/linuxComputerUsePolicy": policy}
            )
            assert not result.is_error, (name, result.content)
            return result

        async def capture(name):
            result = await call("screenshot", {"stream": stream})
            item = next(item for item in result.content if item.type == "image")
            path = output / f"{name}.png"
            path.write_bytes(base64.b64decode(item.data))
            frame = GdkPixbuf.Pixbuf.new_from_file(str(path))
            assert (frame.get_width(), frame.get_height()) == (1280, 720)
            # Inspect the second text line, away from the caret and pointer.
            area = frame.new_subpixbuf(350, 195, 540, 70)
            pixels = area.get_pixels()
            dark = 0
            for y in range(area.get_height()):
                for x in range(area.get_width()):
                    offset = y * area.get_rowstride() + x * area.get_n_channels()
                    dark += max(pixels[offset : offset + 3]) < 100
            return dark

        denied = await client.call_tool(
            "start_session",
            meta={
                "codex/linuxComputerUsePolicy": {**POLICY, "defaultAppAccess": "deny"}
            },
        )
        assert denied.is_error and "application policy" in denied.content[0].text
        start = asyncio.create_task(call("start_session", {"clipboard": True}))
        consent = spawn("consent", [sys.executable, str(HERE / "kde_consent.py")])
        started = await start
        await asyncio.to_thread(consent.wait, timeout=5)
        assert consent.returncode == 0
        assert len(session_paths()) == 1
        stream = started.structured_content["result"][0]["stream"]
        for name in ("gtk", "qt"):
            fixture = output / name
            fixture.mkdir()
            proc = spawn(
                name, [sys.executable, str(HERE / f"{name}_fixture.py"), str(fixture)]
            )
            await wait_file(fixture / "ready")
            await asyncio.sleep(1)
            titles = {"gtk": "Codex GTK paste fixture", "qt": "Codex Qt paste fixture"}
            apps = json.loads((await call("list_apps")).content[0].text)["apps"]
            found = next(app for app in apps if app["window"] == titles[name])
            assert found["desktop_id"] == f"codex-fixture-{name}.desktop", found
            refreshed = json.loads((await call("list_apps")).content[0].text)["apps"]
            assert found in refreshed, "Application identity changed between queries"
            before = await capture(f"{name}-before")
            assert before < 50, "The editor should be blank before paste"
            # These coordinates select the editor in the fresh 1280x720 desktop.
            await call("click", {"stream": stream, "x": 600, "y": 350})
            (fixture / "copy-before").touch()
            await wait_file(fixture / "copied")
            await asyncio.sleep(0.3)
            await call("paste_text", {"text": TEXT})
            await wait_file(fixture / "text.txt", TEXT)
            await verify_text(call, found["id"], TEXT)
            await verify_reads(
                client,
                call,
                found,
                launchers / f"codex-fixture-{name}.desktop",
                TEXT,
                POLICY,
                fixture / "activated",
                wait_file,
            )
            (fixture / "read-clipboard").touch()
            await wait_file(fixture / "clipboard.txt", PREVIOUS)
            assert await capture(f"{name}-pasted") > before + 50, (
                "Pasted text was not rendered"
            )
            assert not (fixture / "activated").exists()
            await activate_button(call, found["id"])
            await wait_file(fixture / "activated", "1")
            results.append(
                {
                    "app": name,
                    "exact_unicode": True,
                    "clipboard_restored": True,
                    "accessibility_text": True,
                    "per_app_reads": True,
                    "per_app_actions": True,
                }
            )
            proc.terminate()
            await asyncio.to_thread(proc.wait, timeout=5)
        await verify_activation(bus, client, call, output, launchers, wait_file, POLICY)
        await call("stop_session")
        assert session_paths() == [], "Portal session survived stop_session"
        stopped = await client.call_tool(
            "screenshot",
            {"stream": stream},
            meta={"codex/linuxComputerUsePolicy": POLICY},
        )
        assert stopped.is_error and "session is closed" in stopped.content[0].text
    (output / "observations.json").write_text(
        json.dumps({"policy_deny": True, "results": results}, indent=2)
    )
