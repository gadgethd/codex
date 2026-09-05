import unittest
from types import SimpleNamespace

from codex_linux_computer_use.dbus import PortalError
from codex_linux_computer_use.session_state import is_locked


class BusError(Exception):
    pass


class LockBus:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.polls = 0
        self.connection = self
        self.GLib = SimpleNamespace(Error=BusError)
        self.Gio = SimpleNamespace(DBusCallFlags=SimpleNamespace(NO_AUTO_START=2))

    def poll(self):
        self.polls += 1

    def call_sync(self, *args):
        self.calls.append(args)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(unpack=lambda: (response,))


class LockStateTests(unittest.TestCase):
    def test_any_active_service_blocks_even_after_an_inactive_response(self):
        bus = LockBus([False, BusError(), True])
        self.assertTrue(is_locked(bus))
        self.assertEqual(
            (bus.polls, bus.calls),
            (
                3,
                [
                    (name, path, name, "GetActive", None, None, 2, 1000, None)
                    for name, path in [
                        ("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver"),
                        ("org.freedesktop.ScreenSaver", "/ScreenSaver"),
                        ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver"),
                    ]
                ],
            ),
        )

    def test_inactive_desktop_is_known_despite_unavailable_services(self):
        bus = LockBus(
            [BusError(), False, BusError(), BusError(), BusError(), BusError()]
        )
        self.assertFalse(is_locked(bus))
        self.assertEqual(bus.polls, 6)

    def test_missing_and_malformed_responses_cannot_grant_access(self):
        for responses in ([BusError()] * 6, [0, 1, "false", None, [], {}]):
            with (
                self.subTest(responses=responses),
                self.assertRaisesRegex(PortalError, "Cannot determine"),
            ):
                is_locked(LockBus(responses))


if __name__ == "__main__":
    unittest.main()
