"""Create and check a disposable Fedora GNOME Wayland test desktop."""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


async def desktop(output, codex):
    from gi.repository import Gio, GLib

    children, logs = [], []

    def spawn(name, args):
        log = (output / f"{name}.log").open("w")
        logs.append(log)
        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
        children.append(proc)
        return proc

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    async def ready(name):
        for _ in range(200):
            result = bus.call_sync(
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
            if result.unpack()[0]:
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
                "GDK_BACKEND",
                "GTK_A11Y",
                "GSETTINGS_BACKEND",
            ],
            check=True,
        )
        spawn("pipewire", ["pipewire"])
        spawn("atspi", ["/usr/libexec/at-spi-bus-launcher", "--launch-immediately"])
        await ready("org.a11y.Bus")
        spawn("atspi-registry", ["/usr/libexec/at-spi2-registryd"])
        spawn(
            "gnome",
            [
                "gnome-shell",
                "--headless",
                "--wayland",
                "--no-x11",
                "--virtual-monitor=1280x720",
                "--wayland-display=codex-test",
            ],
        )
        await ready("org.gnome.Mutter.RemoteDesktop")
        spawn("wireplumber", ["wireplumber", "-p", "policy"])
        spawn("portal-gnome", ["/usr/libexec/xdg-desktop-portal-gnome"])
        spawn("portal", ["/usr/libexec/xdg-desktop-portal"])
        await ready("org.freedesktop.portal.Desktop")
        from doctor import verify

        await verify(output)
        if codex is not None:
            from cli_scenario import exercise

            await exercise(output, codex, spawn)
            return
        fixture = output / "gtk"
        fixture.mkdir()
        proc = spawn(
            "gtk",
            [sys.executable, str(HERE / "gtk_fixture.py"), str(fixture), "--smoke"],
        )
        await asyncio.to_thread(proc.wait, timeout=10)
        assert proc.returncode == 0 and (fixture / "ready").exists()
        (output / "result.json").write_text(
            json.dumps(
                {
                    "status": "passed",
                    "desktop": "private GNOME",
                    "fixture": "GTK 4 mapped",
                }
            )
        )
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="New evidence directory"
    )
    parser.add_argument("--codex", type=Path, help="Also verify this built Codex CLI")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = args.output.resolve()
    codex = args.codex.resolve() if args.codex else None
    if codex is not None and not os.access(codex, os.X_OK):
        parser.error("--codex must name an executable fork build")
    if args.child:
        assert os.environ["XDG_RUNTIME_DIR"] == os.environ["CUA_PRIVATE_RUNTIME"]
        assert os.environ["XDG_RUNTIME_DIR"].startswith(
            "/tmp/cua-"
        ) and not os.environ.get("DISPLAY")
        asyncio.run(desktop(output, codex))
        return
    if sys.platform != "linux":
        parser.error("A Linux desktop is required")
    output.mkdir(parents=True, exist_ok=False)
    runtime = tempfile.mkdtemp(prefix="cua-", dir="/tmp")
    # Desktop routing variables must never escape the host session into this one.
    env = {
        key: os.environ[key]
        for key in ("HOME", "PATH", "USER", "LOGNAME", "SHELL")
        if key in os.environ
    }
    env.update(
        XDG_RUNTIME_DIR=runtime,
        CUA_PRIVATE_RUNTIME=runtime,
        XDG_CURRENT_DESKTOP="GNOME",
        XDG_SESSION_TYPE="wayland",
        WAYLAND_DISPLAY="codex-test",
        GDK_BACKEND="wayland",
        GTK_A11Y="atspi",
        GSETTINGS_BACKEND="memory",
        LANG="C.UTF-8",
        LC_ALL="C.UTF-8",
    )
    for key, name in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        (output / name).mkdir()
        env[key] = str(output / name)
    try:
        result = subprocess.run(
            [
                "dbus-run-session",
                "--",
                sys.executable,
                str(HERE / "gnome_smoke.py"),
                "--child",
                "--output",
                str(output),
                *(["--codex", str(codex)] if codex is not None else []),
            ],
            env=env,
            check=False,
        )
    finally:
        for name in ("gvfs", "doc"):
            subprocess.run(
                ["fusermount3", "-u", str(Path(runtime) / name)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        if not any(os.path.ismount(Path(runtime) / name) for name in ("gvfs", "doc")):
            shutil.rmtree(runtime)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
