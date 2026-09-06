"""Probe running services and bindings without sharing or reading app content."""

import importlib
import json
import os
import sys
import time
from contextlib import contextmanager
from types import SimpleNamespace

from .dbus_identity import process_handle
from .session_state import is_locked


@contextmanager
def checked(results, name, hint, *, success="Available."):
    item = {"check": name, "status": "ok", "detail": success}
    try:
        yield
    except (
        ImportError,
        AttributeError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        # Native errors can contain bus addresses or other private session data.
        item.update(status="unavailable", detail=hint)
    results.append(item)


def dependencies():
    results = []
    with checked(
        results, "mcp", "Reinstall this package in the selected Python environment."
    ):
        _ = importlib.import_module("mcp.server").MCPServer
    with checked(
        results,
        "gobject",
        "Install distribution PyGObject/Gio bindings; use its system Python with --system-site-packages.",
    ):
        importlib.import_module("gi.repository.Gio")
        importlib.import_module("gi.repository.GLib")
    with checked(
        results,
        "capture_plugins",
        "Install GStreamer/GstApp Python bindings and pipewiresrc, videoconvert, videoscale, pngenc and appsink plugins.",
    ):
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstApp", "1.0")
        from gi.repository import Gst

        importlib.import_module("gi.repository.GstApp")
        Gst.init(None)
        for name in ("pipewiresrc", "videoconvert", "videoscale", "pngenc", "appsink"):
            if Gst.ElementFactory.find(name) is None:
                raise ValueError("Missing capture element")
    return results


def session(address):
    from gi.repository import Gio, GLib

    results, connections = [], []

    def connect(address):
        connection = Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )
        connections.append(connection)
        return connection

    def call(connection, name, path, interface, method, parameters=None):
        return connection.call_sync(
            name,
            path,
            interface,
            method,
            parameters,
            None,
            Gio.DBusCallFlags.NO_AUTO_START,
            500,
            None,
        ).unpack()[0]

    try:
        connection = None
        with checked(
            results,
            "session_bus",
            "Run from the graphical session with DBUS_SESSION_BUS_ADDRESS passed to the service.",
        ):
            # Do not autolaunch a new bus or silently select another desktop.
            if not address:
                raise ValueError("Missing desktop session address")
            connection = connect(address)
        if connection is None:
            return results
        bus = SimpleNamespace(
            connection=connection,
            Gio=Gio,
            GLib=GLib,
            poll=lambda: None,
            deadline=time.monotonic() + 12,
        )
        for check, interface, property_name, mask in (
            ("portal_input", "RemoteDesktop", "AvailableDeviceTypes", 3),
            ("portal_capture", "ScreenCast", "AvailableSourceTypes", 1),
            ("portal_clipboard", "Clipboard", "version", None),
        ):
            with checked(
                results,
                check,
                f"Start/update the desktop's xdg-desktop-portal backend with {interface} support; this probe does not activate services.",
            ):
                value = call(
                    connection,
                    "org.freedesktop.portal.Desktop",
                    "/org/freedesktop/portal/desktop",
                    "org.freedesktop.DBus.Properties",
                    "Get",
                    GLib.Variant(
                        "(ss)", (f"org.freedesktop.portal.{interface}", property_name)
                    ),
                )
                if (
                    type(value) is not int
                    or value < 1
                    or (mask is not None and value & mask != mask)
                ):
                    raise ValueError("Unsupported portal capability")
        with checked(
            results,
            "unlocked_desktop",
            "Unlock the graphical session and ensure its supported screen saver service exposes lock state.",
        ):
            if is_locked(bus):
                raise ValueError("Desktop locked")
        with (
            checked(
                results,
                "session_identity",
                "Per-app identity needs a session D-Bus with ProcessFD credentials and readable /proc process handles.",
            ),
            process_handle(bus, connection, connection.get_unique_name()),
        ):
            pass
        with checked(
            results,
            "accessibility_launcher",
            "Enable desktop accessibility and its AT-SPI launcher; this probe does not activate services.",
            success="Launcher is running. Its AT-SPI transport and identity still need a permitted app check.",
        ):
            if (
                call(
                    connection,
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "NameHasOwner",
                    GLib.Variant("(s)", ("org.a11y.Bus",)),
                )
                is not True
            ):
                raise ValueError("Accessibility launcher is not running")
        return results
    finally:
        for connection in reversed(connections):
            connection.close_sync(None)


if __name__ == "__main__":
    print(
        json.dumps(
            dependencies()
            if sys.argv[1] == "dependencies"
            else session(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))
        )
    )
