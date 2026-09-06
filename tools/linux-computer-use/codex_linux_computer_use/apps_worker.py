"""Read app roots directly, avoiding libatspi's unbounded accessibility cache."""

import hashlib
import json
import sys
import time

from .app_identity import desktop_id
from .apps import MAX_RESULT_BYTES

ACCESSIBLE = "org.a11y.atspi.Accessible"
REGISTRY = "org.a11y.atspi.Registry"
ROOT = "/org/a11y/atspi/accessible/root"


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def label(value, size):
    if not isinstance(value, str):
        raise TypeError("Invalid accessibility label")
    return value[:size].encode("utf-8")[:size].decode("utf-8", errors="ignore")


def identifier(bus, owner, path):
    return hashlib.sha256(f"{bus.identity}\0{owner}\0{path}".encode()).hexdigest()[:32]


class AccessibilityBus:
    def __init__(self):
        from gi.repository import Gio, GLib

        self.Gio, self.GLib = Gio, GLib
        self.deadline = time.monotonic() + 5
        session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        address = session.call_sync(
            "org.a11y.Bus",
            "/org/a11y/bus",
            "org.a11y.Bus",
            "GetAddress",
            None,
            None,
            Gio.DBusCallFlags.NONE,
            1000,
            None,
        ).unpack()[0]
        # Resolve accessibility from this session, never a separately inherited
        # AT_SPI_BUS_ADDRESS that could point at another desktop.
        self.bus = Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )
        self.identity = self.call(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "GetId",
        )[0]

    def call(self, owner, path, interface, method, signature=None, args=()):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Accessibility query deadline exceeded")
        try:
            return self.bus.call_sync(
                owner,
                path,
                interface,
                method,
                self.GLib.Variant(signature, args) if signature else None,
                None,
                self.Gio.DBusCallFlags.NO_AUTO_START,
                1000,
                None,
            ).unpack()
        except self.GLib.Error:
            raise OSError("Accessibility call failed") from None

    def property(self, owner, path, interface, name):
        # Qt's bridge supports individual Get calls but can reject GetAll.
        (value,) = self.call(
            owner,
            path,
            "org.freedesktop.DBus.Properties",
            "Get",
            "(ss)",
            (interface, name),
        )
        return value

    def desktop_id(self, owner):
        return desktop_id(self, owner)

    def child(self, owner, path, index):
        (child,) = self.call(
            owner, path, ACCESSIBLE, "GetChildAtIndex", "(i)", (index,)
        )
        child_owner, child_path = child
        if not self.Gio.dbus_is_unique_name(child_owner) or len(child_path) > 1024:
            raise ValueError("Invalid accessible object reference")
        return child_owner, child_path


def discover(bus, cursor):
    count = bus.property(REGISTRY, ROOT, ACCESSIBLE, "ChildCount")
    if type(count) is not int or count < 0:
        raise ValueError("Invalid accessibility application count")
    result = {
        "apps": [],
        "next_cursor": None,
        "unavailable": 0,
        "limited": count > 4096,
    }
    index = cursor
    while index < min(count, 4096, cursor + 16) and len(result["apps"]) < 8:
        try:
            owner, path = bus.child(REGISTRY, ROOT, index)
            app = {
                "id": identifier(bus, owner, path),
                "desktop_id": bus.desktop_id(owner),
                "name": label(bus.property(owner, path, ACCESSIBLE, "Name"), 96),
                "toolkit": label(
                    bus.property(
                        owner, path, "org.a11y.atspi.Application", "ToolkitName"
                    ),
                    24,
                ),
                "window": "",
            }
            children = bus.property(owner, path, ACCESSIBLE, "ChildCount")
            if type(children) is not int or children < 0:
                raise ValueError("Invalid accessibility child count")
            if children:
                window_owner, window_path = bus.child(owner, path, 0)
                app["window"] = label(
                    bus.property(window_owner, window_path, ACCESSIBLE, "Name"), 128
                )
        except TimeoutError:
            if index == cursor:
                raise
            break
        except (OSError, TypeError, ValueError):
            result["unavailable"] += 1
            index += 1
            continue
        candidate = {**result, "apps": [*result["apps"], app], "next_cursor": index + 1}
        if len(encode(candidate)) > MAX_RESULT_BYTES - 16:
            if not result["apps"]:
                result["unavailable"] += 1
                index += 1
                continue
            break
        result["apps"].append(app)
        index += 1
    result["next_cursor"] = index if index < min(count, 4096) else None
    return result


if __name__ == "__main__":
    sys.stdout.buffer.write(encode(discover(AccessibilityBus(), int(sys.argv[1]))))
