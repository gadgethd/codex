"""Native Wayland capture/input through the desktop's RemoteDesktop portal.

This low-level backend performs only operations authorized by the portal. The
client-facing service must additionally enforce Codex application policy.
"""

from dataclasses import dataclass
import uuid

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

    def check_open(self):
        self.bus.poll()
        if self.session is None or self.revoked:
            raise PortalError(
                "The desktop session is closed. Start a new sharing session."
            )

    def stop(self):
        if self.session and not self.revoked:
            try:
                self.bus.call(SESSION, "Close", "()", (), path=self.session)
            except PortalError:
                if not self.revoked:
                    raise
        if self.subscription is not None:
            self.bus.unsubscribe(self.subscription)
        self.session = self.subscription = None
        self.displays = []

    def close(self):
        try:
            self.stop()
        finally:
            self.bus.close()
