"""Read desktop lock state without activating another screen saver service."""

from .dbus import PortalError


def is_locked(bus):
    known = False
    for name, path, interface in (
        (
            "org.gnome.Shell.ScreenShield",
            "/org/gnome/ScreenSaver",
            "org.gnome.ScreenSaver",
        ),
        ("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver"),
        ("org.freedesktop.ScreenSaver", "/ScreenSaver", "org.freedesktop.ScreenSaver"),
        (
            "org.freedesktop.ScreenSaver",
            "/org/freedesktop/ScreenSaver",
            "org.freedesktop.ScreenSaver",
        ),
        (
            "org.cinnamon.ScreenSaver",
            "/org/cinnamon/ScreenSaver",
            "org.cinnamon.ScreenSaver",
        ),
        ("org.mate.ScreenSaver", "/org/mate/ScreenSaver", "org.mate.ScreenSaver"),
        ("org.xfce.ScreenSaver", "/org/xfce/ScreenSaver", "org.xfce.ScreenSaver"),
    ):
        bus.poll()
        try:
            (active,) = bus.connection.call_sync(
                name,
                path,
                interface,
                "GetActive",
                None,
                None,
                bus.Gio.DBusCallFlags.NO_AUTO_START,
                1000,
                None,
            ).unpack()
        except bus.GLib.Error:
            continue
        if type(active) is bool:
            known = True
            if active:
                return True
    if not known:
        raise PortalError("Cannot determine whether this desktop is locked.")
    return False
