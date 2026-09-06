"""Run a private KDE Wayland desktop inside the rootless test container."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTPUT = Path("/output")


async def desktop():
    from gi.repository import Gio, GLib

    children, logs = [], []

    def spawn(name, args):
        log = (OUTPUT / f"{name}.log").open("w")
        logs.append(log)
        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
        children.append(proc)
        return proc

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    async def ready(name):
        for _ in range(200):
            reply = bus.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                GLib.Variant("(s)", (name,)),
                None,
                Gio.DBusCallFlags.NONE,
                1000,
                None,
            )
            if reply.unpack()[0]:
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(f"Desktop service did not start: {name}")

    try:
        await asyncio.to_thread(
            subprocess.run,
            [
                "dbus-update-activation-environment",
                "XDG_RUNTIME_DIR",
                "XDG_CURRENT_DESKTOP",
                "XDG_SESSION_TYPE",
                "WAYLAND_DISPLAY",
                "XDG_CONFIG_HOME",
                "XDG_DATA_HOME",
                "XDG_CACHE_HOME",
                "QT_QUICK_BACKEND",
                "QT_LINUX_ACCESSIBILITY_ALWAYS_ON",
            ],
            check=True,
            timeout=10,
        )
        spawn("pipewire", ["pipewire"])
        spawn("atspi", ["/usr/libexec/at-spi-bus-launcher", "--launch-immediately"])
        await ready("org.a11y.Bus")
        spawn("atspi-registry", ["/usr/libexec/at-spi2-registryd"])
        spawn(
            "kwin",
            [
                "kwin_wayland",
                "--virtual",
                "--width",
                "1280",
                "--height",
                "720",
                "--socket",
                "wayland-test",
                "--no-global-shortcuts",
                "--no-kactivities",
            ],
        )
        await ready("org.kde.KWin")
        spawn("wireplumber", ["wireplumber", "-p", "policy"])
        spawn("portal-kde", ["/usr/libexec/xdg-desktop-portal-kde"])
        spawn("portal", ["/usr/libexec/xdg-desktop-portal"])
        await ready("org.freedesktop.portal.Desktop")
        versions = await asyncio.to_thread(
            subprocess.run,
            [
                "rpm",
                "-q",
                "kwin",
                "xdg-desktop-portal-kde",
                "pipewire",
                "gtk4",
                "python3-pyqt6-base",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        (OUTPUT / "versions.txt").write_text(versions.stdout)
        from kde_scenario import exercise

        await exercise(OUTPUT, spawn)
        (OUTPUT / "result.json").write_text(json.dumps({"status": "passed"}))
    finally:
        for proc in reversed(children):
            if proc.poll() is None:
                proc.terminate()
        for proc in children:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for log in logs:
            log.close()


def main():
    if not Path("/run/.containerenv").exists() or not OUTPUT.is_mount():
        raise SystemExit(
            "Use the rootless test image with a new directory mounted at /output"
        )
    if sys.argv[1:] == ["--child"]:
        assert os.environ["XDG_RUNTIME_DIR"] == os.environ["CUA_PRIVATE_RUNTIME"]
        assert os.environ["XDG_RUNTIME_DIR"].startswith("/tmp/cua-")
        assert not os.environ.get("DISPLAY")
        asyncio.run(desktop())
        return
    if sys.argv[1:] or any(OUTPUT.iterdir()):
        raise SystemExit("The test accepts no arguments and /output must be empty")
    env = {
        key: os.environ[key]
        for key in ("HOME", "PATH", "USER", "LOGNAME", "PYTHONPATH")
        if key in os.environ
    }
    runtime = tempfile.mkdtemp(prefix="cua-", dir="/tmp")
    env.update(
        XDG_RUNTIME_DIR=runtime,
        CUA_PRIVATE_RUNTIME=runtime,
        XDG_CURRENT_DESKTOP="KDE",
        XDG_SESSION_TYPE="wayland",
        WAYLAND_DISPLAY="wayland-test",
        GDK_BACKEND="wayland",
        GTK_A11Y="atspi",
        QT_QUICK_BACKEND="software",
        QT_LINUX_ACCESSIBILITY_ALWAYS_ON="1",
        QT_FORCE_STDERR_LOGGING="1",
        LANG="C.UTF-8",
        LC_ALL="C.UTF-8",
    )
    for key, name in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        (OUTPUT / name).mkdir()
        env[key] = str(OUTPUT / name)
    result = subprocess.run(
        [
            "dbus-run-session",
            "--",
            sys.executable,
            str(HERE / "kde_smoke.py"),
            "--child",
        ],
        env=env,
        check=False,
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
