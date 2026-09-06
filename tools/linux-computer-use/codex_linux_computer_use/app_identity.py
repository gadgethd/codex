"""Match authenticated live peers to unambiguous installed desktop launchers."""

import os
import select
import shlex
import shutil
import time
from contextlib import ExitStack


def matches(entry, executable, arguments, working_directory):
    """Return True/False for a proven match/mismatch, or None if uncertain."""
    command = entry.get_commandline()
    if not command:
        # D-Bus-only desktop services have no process launch command to match.
        return False
    if not isinstance(command, str) or len(command.encode()) > 8192:
        return None
    parts = shlex.split(command)
    if not parts or len(parts) > 64:
        return None
    program = shutil.which(parts[0])
    if program is None:
        return None
    info = os.stat(program)
    if (info.st_dev, info.st_ino) != executable:
        return False
    fixed = []
    dynamic = None
    for part in parts[1:]:
        if part in ("%f", "%F", "%u", "%U"):
            if dynamic is not None:
                return None
            dynamic = part
        elif dynamic or "%" in part:
            # Do not guess the expansion of localized labels, icons, or paths.
            return None
        else:
            if arguments[len(fixed) + 1 : len(fixed) + 2] != [part]:
                return False
            fixed.append(part)
    path = entry.get_string("Path")
    if path:
        if not os.path.isabs(path):
            return None
        info = os.stat(path)
        if (info.st_dev, info.st_ino) != working_directory:
            return False
    elif any(
        not os.path.isabs(part) and (not part.startswith("-") or "=" in part)
        for part in fixed
    ):
        return None
    remaining = arguments[len(fixed) + 1 :]
    if dynamic and "--" not in fixed and any(arg.startswith("-") for arg in remaining):
        # A field expansion cannot establish which launch mode an option selects.
        return None
    extra = len(remaining)
    return arguments[1 : len(fixed) + 1] == fixed and (
        extra == 0
        or extra == 1
        and dynamic is not None
        or extra > 1
        and dynamic in ("%F", "%U")
    )


def process_arguments(directory):
    descriptor = os.open("cmdline", os.O_RDONLY | os.O_CLOEXEC, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as command:
        raw = command.read(16385)
    if not raw.endswith(b"\0") or len(raw) > 16384:
        raise ValueError("Invalid process arguments")
    return raw[:-1].decode().split("\0")


def desktop_id(bus, owner):
    """Return unknown unless credentials and one installed launcher agree.

    Process handles prevent PID reuse from identifying a different process.
    Desktop entries describe launch identities, not isolation from hostile code
    running as the same Unix user. Missing process-handle support stays unknown.
    """
    try:
        if not bus.Gio.dbus_is_unique_name(owner):
            return None
        if time.monotonic() >= bus.deadline:
            raise TimeoutError("Application identity deadline exceeded")
        reply, descriptors = bus.bus.call_with_unix_fd_list_sync(
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
            return None
        with ExitStack() as cleanup:
            process = descriptors.get(handle)
            cleanup.callback(os.close, process)
            if select.select([process], [], [], 0)[0]:
                return None
            with open(f"/proc/self/fdinfo/{process}", "rb") as info:
                metadata = info.read(1025)
            if len(metadata) > 1024 or f"Pid:\t{pid}\n".encode() not in metadata:
                return None
            directory = os.open(
                f"/proc/{pid}", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC
            )
            cleanup.callback(os.close, directory)
            executable = os.stat("exe", dir_fd=directory)
            cwd = os.stat("cwd", dir_fd=directory)
            working_directory = cwd.st_dev, cwd.st_ino
            arguments = process_arguments(directory)
            entries = bus.Gio.AppInfo.get_all()
            if len(entries) > 4096:
                return None
            identities = set()
            key = executable.st_dev, executable.st_ino
            for entry in entries:
                if time.monotonic() >= bus.deadline:
                    raise TimeoutError("Application identity deadline exceeded")
                try:
                    match = matches(entry, key, arguments, working_directory)
                    if match is None:
                        return None
                    if not match:
                        continue
                    name = entry.get_id()
                    if (
                        not isinstance(name, str)
                        or not name.endswith(".desktop")
                        or "/" in name
                        or not 1 <= len(name.encode()) <= 512
                    ):
                        return None
                    identities.add(name)
                except (OSError, ValueError):
                    return None
            current = os.stat("exe", dir_fd=directory)
            cwd = os.stat("cwd", dir_fd=directory)
            if (
                select.select([process], [], [], 0)[0]
                or (current.st_dev, current.st_ino) != key
                or process_arguments(directory) != arguments
                or (cwd.st_dev, cwd.st_ino) != working_directory
                or len(identities) != 1
            ):
                return None
            return identities.pop()
    except TimeoutError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, bus.GLib.Error):
        return None
