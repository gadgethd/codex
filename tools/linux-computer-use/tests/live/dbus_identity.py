"""Exercise a real D-Bus-activated GTK application with no desktop Exec key."""

import asyncio
import json
import sys
from pathlib import Path

from gi.repository import Gio, GLib


async def verify_activation(bus, call, output, launchers, wait_file):
    root = output / "dbus"
    root.mkdir()
    name = "com.example.CodexPasteFixture"
    desktop = launchers / f"{name}.desktop"
    desktop.write_text(
        "[Desktop Entry]\nType=Application\nName=Activated GTK fixture\n"
        "DBusActivatable=true\n"
    )
    services = launchers.parent / "dbus-1" / "services"
    services.mkdir(parents=True)
    service = services / f"{name}.service"
    service.write_text(
        f"[D-BUS Service]\nName={name}\n"
        f"Exec={sys.executable} {Path(__file__).with_name('gtk_fixture.py')} {root}\n"
    )
    generic = launchers / "codex-fixture-gtk.desktop"
    saved = generic.read_bytes()
    try:
        reply = bus.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "StartServiceByName",
            GLib.Variant("(su)", (name, 0)),
            None,
            Gio.DBusCallFlags.NONE,
            10000,
            None,
        )
        assert reply.unpack() == (1,), "Expected a newly activated application"
        await wait_file(root / "ready")
        await asyncio.sleep(1)
        for expected in (None, f"{name}.desktop"):
            apps = json.loads((await call("list_apps")).content[0].text)["apps"]
            found = next(
                app for app in apps if app["window"] == "Codex GTK paste fixture"
            )
            assert found["desktop_id"] == expected, found
            if expected is None:
                generic.unlink()
        (root / "quit").touch()
        for _ in range(100):
            owned = bus.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                GLib.Variant("(s)", (name,)),
                None,
                Gio.DBusCallFlags.NO_AUTO_START,
                1000,
                None,
            ).unpack()[0]
            if not owned:
                break
            await asyncio.sleep(0.1)
        assert not owned, "Activated application survived its quit request"
        (root / "result.json").write_text(
            json.dumps({"activated": True, "ambiguity": True, "stopped": True})
        )
    finally:
        (root / "quit").touch()
        generic.write_bytes(saved)
        desktop.unlink()
        service.unlink()
