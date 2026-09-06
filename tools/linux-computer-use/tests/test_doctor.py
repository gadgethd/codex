import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_linux_computer_use import doctor, doctor_worker


class DoctorTests(unittest.TestCase):
    def test_failure_hints_do_not_expose_native_errors(self):
        results = []
        with doctor_worker.checked(results, "session", "Start the desktop service."):
            raise RuntimeError("private bus address and credential data")
        self.assertEqual(
            results,
            [
                {
                    "check": "session",
                    "status": "unavailable",
                    "detail": "Start the desktop service.",
                }
            ],
        )

    def test_malformed_and_oversized_worker_results_are_inconclusive(self):
        for payload in (b"", b"[]", b"[null]", b"[{}]", b"x" * (doctor.MAX_BYTES + 1)):
            with self.subTest(payload=payload[:10]):

                def run(*args, payload=payload, **kwargs):
                    kwargs["stdout"].write(payload)

                with patch.object(doctor.subprocess, "run", side_effect=run):
                    result = doctor.probe("session")
                self.assertEqual([item["status"] for item in result], ["unknown"])

    def test_stalled_probe_is_killed_and_reaped(self):
        run = subprocess.run
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory, "pid")

            def stalled(args, **kwargs):
                return run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import os,sys,time; from pathlib import Path; "
                            "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
                        ),
                        str(pid_file),
                    ],
                    **kwargs,
                )

            with patch.object(doctor.subprocess, "run", side_effect=stalled):
                result = doctor.probe("session", timeout=0.5)
            self.assertEqual([item["status"] for item in result], ["unknown"])
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)

    def test_exit_status_preserves_unknown_checks(self):
        records = [{"check": "session", "status": "unknown", "detail": "Probe failed."}]
        with (
            patch.object(doctor, "probe", return_value=records),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(doctor.main(), 1)
        self.assertEqual(json.loads(output.getvalue())["checks"], records * 2)

    def test_session_queries_do_not_activate_or_mutate_services(self):
        for devices, locked in ((3, False), (1, True)):
            with self.subTest(devices=devices, locked=locked):
                calls, closed = [], []

                def call(
                    name,
                    path,
                    interface,
                    method,
                    parameters,
                    reply,
                    flags,
                    timeout,
                    cancel,
                    *,
                    calls=calls,
                    devices=devices,
                    locked=locked,
                ):
                    self.assertEqual(flags, 4)
                    self.assertLessEqual(timeout, 1000)
                    calls.append(method)
                    values = {
                        "Get": devices,
                        "GetActive": locked,
                        "NameHasOwner": True,
                    }
                    return SimpleNamespace(unpack=lambda: (values[method],))

                connection = SimpleNamespace(
                    call_sync=call,
                    get_unique_name=lambda: ":1.0",
                    close_sync=lambda _, closed=closed: closed.append(True),
                )
                gio = SimpleNamespace(
                    DBusConnection=SimpleNamespace(
                        new_for_address_sync=lambda *args, connection=connection: (
                            connection
                        )
                    ),
                    DBusConnectionFlags=SimpleNamespace(
                        AUTHENTICATION_CLIENT=1, MESSAGE_BUS_CONNECTION=2
                    ),
                    DBusCallFlags=SimpleNamespace(NO_AUTO_START=4),
                )
                glib = SimpleNamespace(Variant=lambda *args: args, Error=RuntimeError)
                with (
                    patch.dict(
                        sys.modules,
                        {"gi.repository": SimpleNamespace(Gio=gio, GLib=glib)},
                    ),
                    patch.object(
                        doctor_worker,
                        "process_handle",
                        side_effect=lambda *args: io.StringIO(),
                    ),
                ):
                    result = doctor_worker.session("private")
                statuses = {item["check"]: item["status"] for item in result}
                self.assertEqual(
                    statuses["portal_input"], "unavailable" if devices == 1 else "ok"
                )
                self.assertEqual(
                    statuses["unlocked_desktop"], "unavailable" if locked else "ok"
                )
                self.assertEqual(set(calls), {"Get", "GetActive", "NameHasOwner"})
                self.assertEqual(closed, [True])
