import ctypes
import os
import select
import shlex
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codex_linux_computer_use.app_identity import desktop_id, matches


def entry(name, command, working_directory=None):
    return SimpleNamespace(
        get_id=lambda: name,
        get_commandline=lambda: command,
        get_string=lambda key: working_directory,
    )


def process_handle(pid):
    # Some Python builds omit os.pidfd_open; production receives this handle
    # from D-Bus. Exercise the same real kernel object via libc in these tests.
    function = ctypes.CDLL(None, use_errno=True).pidfd_open
    function.argtypes = [ctypes.c_int, ctypes.c_uint]
    function.restype = ctypes.c_int
    descriptor = function(pid, 0)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "Cannot open test process handle")
    return descriptor


class IdentityTests(unittest.TestCase):
    def setUp(self):
        with open("/proc/self/cmdline", "rb") as command:
            self.arguments = command.read().decode().rstrip("\0").split("\0")
        self.command = shlex.join(self.arguments)
        self.pidfd = process_handle(os.getpid())
        self.addCleanup(os.close, self.pidfd)
        self.credentials = {
            "ProcessID": os.getpid(),
            "UnixUserID": os.getuid(),
            "ProcessFD": 0,
        }
        self.duplicates = []

        def duplicate(index):
            descriptor = os.dup(self.pidfd)
            self.duplicates.append(descriptor)
            return descriptor

        self.entries = [entry("fixture.desktop", self.command, os.getcwd())]
        self.bus = SimpleNamespace(
            deadline=time.monotonic() + 5,
            Gio=SimpleNamespace(
                dbus_is_unique_name=lambda name: name.startswith(":"),
                DBusCallFlags=SimpleNamespace(NO_AUTO_START=1),
                AppInfo=SimpleNamespace(get_all=Mock(side_effect=lambda: self.entries)),
            ),
            GLib=SimpleNamespace(Variant=lambda *args: None, Error=RuntimeError),
            bus=SimpleNamespace(
                call_with_unix_fd_list_sync=Mock(
                    return_value=(
                        SimpleNamespace(unpack=lambda: (self.credentials,)),
                        SimpleNamespace(get_length=lambda: 1, get=duplicate),
                    )
                )
            ),
        )

    def tearDown(self):
        for descriptor in self.duplicates:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_authenticated_process_matches_installed_launch_arguments(self):
        self.assertEqual(desktop_id(self.bus, ":1.2"), "fixture.desktop")
        self.entries = [entry("other.desktop", self.command + " --other-app")]
        self.assertIsNone(desktop_id(self.bus, ":1.2"))
        self.entries.append(entry("fixture.desktop", self.command, os.getcwd()))
        self.assertEqual(desktop_id(self.bus, ":1.2"), "fixture.desktop")
        self.entries.append(entry("alias.desktop", self.command, os.getcwd()))
        self.assertIsNone(desktop_id(self.bus, ":1.2"))

    def test_credentials_missing_mismatched_or_without_process_handle_are_unknown(self):
        for changes in [
            {"ProcessFD": None},
            {"ProcessFD": True},
            {"ProcessFD": 1},
            {"ProcessID": os.getpid() + 1},
            {"UnixUserID": os.getuid() + 1},
        ]:
            with self.subTest(changes=changes):
                original = self.credentials.copy()
                self.credentials.update(changes)
                self.assertIsNone(desktop_id(self.bus, ":1.2"))
                self.credentials = original
        self.assertIsNone(desktop_id(self.bus, "application.claimed.Name"))

    def test_dead_peer_and_changed_executable_are_unknown(self):
        with subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        ) as child:
            descriptor = process_handle(child.pid)
            try:
                child.terminate()
                child.wait(timeout=5)
                self.credentials["ProcessID"] = child.pid
                with patch(
                    "codex_linux_computer_use.app_identity.os.dup",
                    return_value=os.dup(descriptor),
                ):
                    self.assertIsNone(desktop_id(self.bus, ":1.2"))
            finally:
                os.close(descriptor)
        self.credentials["ProcessID"] = os.getpid()
        original, reads = os.stat, []

        def changed(path, **kwargs):
            info = original(path, **kwargs)
            if path == "exe":
                reads.append(1)
                if len(reads) > 1:
                    return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1)
            return info

        with patch(
            "codex_linux_computer_use.app_identity.os.stat", side_effect=changed
        ):
            self.assertIsNone(desktop_id(self.bus, ":1.2"))
        with patch(
            "codex_linux_computer_use.app_identity.process_arguments",
            side_effect=[self.arguments, [*self.arguments, "--other-app"]],
        ):
            self.assertIsNone(desktop_id(self.bus, ":1.2"))

    def test_registry_and_deadline_limits_do_not_guess_identity(self):
        self.entries *= 4097
        self.assertIsNone(desktop_id(self.bus, ":1.2"))
        self.bus.deadline = 0
        with self.assertRaises(TimeoutError):
            desktop_id(self.bus, ":1.2")

    def test_same_executable_exec_with_changed_arguments_is_unknown(self):
        replacement = 'import time; print("changed", flush=True); time.sleep(60)'
        code = (
            'import os,sys; print("ready", flush=True); sys.stdin.readline(); '
            f'os.execv(sys.executable, [sys.executable, "-c", {replacement!r}])'
        )
        arguments = [sys.executable, "-c", code]
        with subprocess.Popen(
            arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        ) as child:
            try:
                self.assertTrue(select.select([child.stdout], [], [], 5)[0])
                self.assertEqual(child.stdout.readline(), b"ready\n")
                descriptor = process_handle(child.pid)
                self.addCleanup(os.close, descriptor)
                self.credentials["ProcessID"] = child.pid
                self.entries = [
                    entry("original.desktop", shlex.join(arguments), os.getcwd())
                ]

                def replace():
                    child.stdin.write(b"\n")
                    child.stdin.flush()
                    self.assertTrue(select.select([child.stdout], [], [], 5)[0])
                    self.assertEqual(child.stdout.readline(), b"changed\n")
                    return self.entries

                self.bus.Gio.AppInfo.get_all.side_effect = replace
                with patch.object(self, "pidfd", descriptor):
                    self.assertIsNone(desktop_id(self.bus, ":1.2"))
            finally:
                child.terminate()
                child.wait(timeout=5)

    def test_launch_matching_checks_executable_and_rejects_unknown_expansions(self):
        info = os.stat(sys.executable)
        executable = info.st_dev, info.st_ino
        args = [sys.executable, "/fixture.py", "document.txt"]
        directory = os.stat(".")
        cwd = directory.st_dev, directory.st_ino
        for command, expected in [
            (f"{sys.executable} /fixture.py %F", True),
            (f"{sys.executable} /fixture.py", False),
            (f"{sys.executable} /other.py %F", False),
            (f"{sys.executable} /fixture.py %F --other", False),
            (f"{sys.executable} /fixture.py %c", False),
            ("/missing-program /fixture.py", False),
            ("/bin/true /fixture.py", False),
        ]:
            with self.subTest(command=command):
                self.assertEqual(
                    matches(entry("fixture.desktop", command), executable, args, cwd),
                    expected,
                )
        for field, expected in [
            ("%f", False),
            ("%u", False),
            ("%F", True),
            ("%U", True),
        ]:
            self.assertEqual(
                matches(
                    entry("fixture.desktop", f"{sys.executable} /fixture.py {field}"),
                    executable,
                    [*args, "second.txt"],
                    cwd,
                ),
                expected,
            )
        relative = [sys.executable, "fixture.py"]
        command = f"{sys.executable} fixture.py"
        for path, expected in [(None, False), ("/", False), (os.getcwd(), True)]:
            self.assertEqual(
                matches(
                    entry("fixture.desktop", command, path), executable, relative, cwd
                ),
                expected,
            )
