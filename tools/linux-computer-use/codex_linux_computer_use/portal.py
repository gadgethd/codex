"""Native Wayland capture/input through the desktop's RemoteDesktop portal.

This low-level backend performs only operations authorized by the portal. The
client-facing service must additionally enforce Codex application policy.
"""

import math
import os
import uuid
from dataclasses import dataclass

from .dbus import PortalBus, PortalError

REMOTE = "org.freedesktop.portal.RemoteDesktop"
CAST = "org.freedesktop.portal.ScreenCast"
SESSION = "org.freedesktop.portal.Session"


@dataclass(frozen=True)
class Display:
    stream: int
    width: int
    height: int


class PortalDesktop:
    def __init__(self, bus=None):
        self.bus = bus if bus is not None else PortalBus()
        self.session = None
        self.displays = []
        self.subscription = None
        self.revoked = False
        self.pressed = {}

    def capabilities(self):
        (devices,) = self.bus.call(
            "org.freedesktop.DBus.Properties",
            "Get",
            "(ss)",
            (REMOTE, "AvailableDeviceTypes"),
        )
        (sources,) = self.bus.call(
            "org.freedesktop.DBus.Properties",
            "Get",
            "(ss)",
            (CAST, "AvailableSourceTypes"),
        )
        return {
            "keyboard": bool(devices & 1),
            "pointer": bool(devices & 2),
            "monitors": bool(sources & 1),
        }

    def move(self, stream, x, y):
        self.check_open()
        display = next(
            (display for display in self.displays if display.stream == stream), None
        )
        if display is None:
            raise ValueError("Unknown display stream.")
        if any(
            type(n) not in (int, float) or not math.isfinite(n) for n in (x, y)
        ) or not (0 <= x < display.width and 0 <= y < display.height):
            raise ValueError(
                "Pointer coordinates must be inside the display's logical dimensions."
            )
        self.bus.call(
            REMOTE,
            "NotifyPointerMotionAbsolute",
            "(oa{sv}udd)",
            (self.session, {}, stream, x, y),
        )

    def button(self, button, *, pressed):
        if type(button) is not int or button not in (272, 273, 274):
            raise ValueError(
                "Supported buttons are left (272), right (273), and middle (274)."
            )
        self._input("NotifyPointerButton", button, pressed)

    def keysym(self, keysym, *, pressed):
        if type(keysym) is not int or not 0 < keysym <= 0x1FFFFFFF:
            raise ValueError("Invalid keyboard keysym.")
        self._input("NotifyKeyboardKeysym", keysym, pressed)

    def _input(self, method, code, pressed):
        self.check_open()
        if type(pressed) is not bool:
            raise ValueError("pressed must be a boolean.")
        # Track before sending: on an ambiguous transport failure, still attempt
        # release rather than leaving the user's modifier or mouse button down.
        if pressed:
            self.pressed[(method, code)] = None
        self.bus.call(
            REMOTE, method, "(oa{sv}iu)", (self.session, {}, code, int(pressed))
        )
        if not pressed:
            self.pressed.pop((method, code), None)

    def scroll(self, *, horizontal=0, vertical=0):
        self.check_open()
        if any(type(n) is not int or abs(n) > 100 for n in (horizontal, vertical)):
            raise ValueError(
                "Scroll distances must be integer steps between -100 and 100."
            )
        for axis, steps in enumerate((vertical, horizontal)):
            if steps:
                self.bus.call(
                    REMOTE,
                    "NotifyPointerAxisDiscrete",
                    "(oa{sv}ui)",
                    (self.session, {}, axis, steps),
                )

    def release_inputs(self):
        for method, code in reversed(list(self.pressed)):
            try:
                self._input(method, code, False)
            except PortalError:
                pass

    def start(self, *, timeout=120):
        if self.session:
            self.bus.poll()
            if not self.revoked and self.displays:
                self.check_open()
                return list(self.displays)
            self.stop()
        try:
            result = self.bus.request(
                REMOTE,
                "CreateSession",
                "(a{sv})",
                (),
                {
                    "session_handle_token": self.bus.variant(
                        "s", "codex_" + uuid.uuid4().hex
                    ),
                },
                timeout=timeout,
            )
            self.session = result["session_handle"]
            self.revoked = False
            self.subscription = self.bus.subscribe(
                SESSION, "Closed", self.session, self._on_closed
            )
            self.bus.request(
                REMOTE,
                "SelectDevices",
                "(oa{sv})",
                (self.session,),
                {
                    "types": self.bus.variant("u", 3),
                },
                timeout=timeout,
            )
            self.bus.request(
                CAST,
                "SelectSources",
                "(oa{sv})",
                (self.session,),
                {
                    "types": self.bus.variant("u", 1),
                    "multiple": self.bus.variant("b", True),
                },
                timeout=timeout,
            )
            result = self.bus.request(
                REMOTE, "Start", "(osa{sv})", (self.session, ""), {}, timeout=timeout
            )
            if result.get("devices", 0) & 3 != 3:
                raise PortalError(
                    "The desktop did not grant both keyboard and pointer control."
                )
            streams = result.get("streams", [])
            if not 1 <= len(streams) <= 16:
                raise PortalError("The desktop must share between 1 and 16 displays.")
            displays = []
            for stream, properties in streams:
                size = properties.get("logical_size", properties.get("size"))
                if (
                    not size
                    or len(size) != 2
                    or any(type(n) is not int or not 0 < n <= 32768 for n in size)
                ):
                    raise PortalError(
                        "The desktop did not provide valid display dimensions."
                    )
                displays.append(Display(stream, *size))
            self.displays = displays
            self.check_open()
            return list(displays)
        except BaseException:
            self.stop()
            raise

    def _on_closed(self, _parameters):
        self.revoked = True
        self.pressed.clear()

    def check_open(self):
        self.bus.poll()
        if self.session is None or self.revoked:
            raise PortalError(
                "The desktop session is closed. Start a new sharing session."
            )

    def screenshot(self, stream):
        self.check_open()
        display = next(
            (display for display in self.displays if display.stream == stream), None
        )
        if display is None:
            raise ValueError("Unknown display stream.")
        from .capture import capture_png

        fd = self.bus.call(
            CAST, "OpenPipeWireRemote", "(oa{sv})", (self.session, {}), receive_fd=True
        )
        try:
            screenshot = capture_png(fd, stream, display.width, display.height)
            self.check_open()
            return screenshot
        finally:
            os.close(fd)

    def stop(self):
        with self.bus.cleanup():
            if self.session and not self.revoked:
                self.release_inputs()
                try:
                    self.bus.call(SESSION, "Close", "()", (), path=self.session)
                except PortalError:
                    if not self.revoked:
                        raise
            if self.subscription is not None:
                self.bus.unsubscribe(self.subscription)
            self.session = self.subscription = None
            self.displays = []
            self.pressed.clear()

    def close(self):
        try:
            self.stop()
        finally:
            self.bus.close()
