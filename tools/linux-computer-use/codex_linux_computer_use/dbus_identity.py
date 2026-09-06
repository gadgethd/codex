"""Authenticate application names across the desktop's two message buses."""

import os
import select
import time
from contextlib import contextmanager


@contextmanager
def process_handle(bus, connection, owner):
    """Keep an authenticated live process handle open while its caller compares it."""
    if time.monotonic() >= bus.deadline:
        raise TimeoutError("Application identity deadline exceeded")
    reply, descriptors = connection.call_with_unix_fd_list_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "GetConnectionCredentials",
        bus.GLib.Variant("(s)", (owner,)),
        None,
        bus.Gio.DBusCallFlags.NO_AUTO_START,
        1000,
        None,
        None,
    )
    (credentials,) = reply.unpack()
    pid, uid, handle = (
        credentials.get(key) for key in ("ProcessID", "UnixUserID", "ProcessFD")
    )
    if (
        type(pid) is not int
        or pid <= 0
        or type(uid) is not int
        or uid != os.getuid()
        or type(handle) is not int
        or descriptors is None
        or not 0 <= handle < descriptors.get_length() <= 16
    ):
        raise ValueError("Invalid process credentials")
    process = descriptors.get(handle)
    try:
        if select.select([process], [], [], 0)[0]:
            raise ValueError("Application process exited")
        with open(f"/proc/self/fdinfo/{process}", "rb") as info:
            metadata = info.read(1025)
        if len(metadata) > 1024 or f"Pid:\t{pid}\n".encode() not in metadata:
            raise ValueError("Mismatched process handle")
        yield pid, process
    finally:
        os.close(process)


def session_identities(bus, entries, pid, process):
    """Return matching IDs and ownership evidence, without activating services."""
    candidates = {}
    for entry in entries:
        name = entry.get_id()
        if not entry.get_boolean("DBusActivatable") or not isinstance(name, str):
            continue
        service = name.removesuffix(".desktop")
        if (
            not name.endswith(".desktop")
            or not bus.Gio.dbus_is_name(service)
            or bus.Gio.dbus_is_unique_name(service)
        ):
            continue
        candidates[service] = name
    if not candidates:
        return set(), {}

    def query(method, name=None):
        if time.monotonic() >= bus.deadline:
            raise TimeoutError("Application identity deadline exceeded")
        return bus.session.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            method,
            bus.GLib.Variant("(s)", (name,)) if name else None,
            None,
            bus.Gio.DBusCallFlags.NO_AUTO_START,
            1000,
            None,
        ).unpack()[0]

    names = query("ListNames")
    if not isinstance(names, (list, tuple)) or len(names) > 4096:
        raise ValueError("Invalid session name registry")
    if any(not isinstance(name, str) or len(name.encode()) > 255 for name in names):
        raise ValueError("Invalid session name")
    identities, owners = set(), {}
    expected = os.fstat(process)
    for name in sorted(candidates.keys() & set(names)):
        owner = query("GetNameOwner", name)
        if not isinstance(owner, str) or not bus.Gio.dbus_is_unique_name(owner):
            raise ValueError("Invalid session owner")
        owners[name] = owner
        with process_handle(bus, bus.session, owner) as (other_pid, other_process):
            actual = os.fstat(other_process)
            if other_pid == pid and (actual.st_dev, actual.st_ino) == (
                expected.st_dev,
                expected.st_ino,
            ):
                identities.add(candidates[name])
    return identities, owners
