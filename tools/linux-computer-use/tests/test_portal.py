import unittest
from unittest.mock import patch

from codex_linux_computer_use.dbus import PortalError
from codex_linux_computer_use.portal import (
    CAST,
    REMOTE,
    SESSION,
    Display,
    PortalDesktop,
)


class FakeBus:
    def __init__(self):
        self.calls = []
        self.callbacks = {}
        self.results = {
            "CreateSession": {"session_handle": "/session/codex"},
            "SelectDevices": {},
            "SelectSources": {},
            "Start": {"devices": 3, "streams": [(42, {"size": (1920, 1080)})]},
        }
        self.fail_method = None
        self.closed = False

    def variant(self, signature, value):
        return signature, value

    def call(self, interface, method, signature, values, **kwargs):
        self.calls.append((interface, method, signature, values, kwargs))
        if method == self.fail_method:
            raise PortalError("transport failed")
        return (3,) if method == "Get" else ()

    def request(self, interface, method, signature, values, options, **kwargs):
        self.call(interface, method, signature, (*values, options), **kwargs)
        return self.results[method]

    def subscribe(self, interface, signal, path, callback):
        self.callbacks[1] = callback
        return 1

    def unsubscribe(self, subscription):
        del self.callbacks[subscription]

    def poll(self):
        pass

    def close(self):
        self.closed = True


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.desktop = PortalDesktop(self.bus)

    def tearDown(self):
        self.desktop.close()

    def test_session_combines_capture_and_input_with_logical_dimensions(self):
        self.bus.results["Start"]["streams"] = [
            (42, {"size": (3840, 2160), "logical_size": (1920, 1080)}),
            (43, {"size": (1280, 720)}),
        ]
        self.assertEqual(
            self.desktop.start(), [Display(42, 1920, 1080), Display(43, 1280, 720)]
        )
        self.assertEqual(
            [(call[0], call[1]) for call in self.bus.calls],
            [
                (REMOTE, "CreateSession"),
                (REMOTE, "SelectDevices"),
                (CAST, "SelectSources"),
                (REMOTE, "Start"),
            ],
        )

    def test_start_reuses_existing_session(self):
        displays = self.desktop.start()
        calls = list(self.bus.calls)
        self.assertEqual(self.desktop.start(), displays)
        self.assertEqual(self.bus.calls, calls)

    def test_failed_start_closes_session_and_allows_retry(self):
        self.bus.fail_method = "Start"
        with self.assertRaises(PortalError):
            self.desktop.start()
        self.assertEqual(
            self.bus.calls[-1], (SESSION, "Close", "()", (), {"path": "/session/codex"})
        )
        self.assertEqual((self.desktop.session, self.bus.callbacks), (None, {}))
        self.bus.fail_method = None
        self.assertEqual(self.desktop.start(), [Display(42, 1920, 1080)])

    def test_partial_input_permission_and_missing_dimensions_are_rejected(self):
        for result in [
            {"devices": 1, "streams": [(42, {"size": (100, 100)})]},
            {"devices": 3, "streams": []},
            {"devices": 3, "streams": [(42, {})]},
            {"devices": 3, "streams": [(42, {"size": (-1, 100)})]},
        ]:
            with self.subTest(result=result):
                self.bus.results["Start"] = result
                with self.assertRaises(PortalError):
                    self.desktop.start()
                self.assertIsNone(self.desktop.session)

    def test_revoked_session_rejects_operations(self):
        self.desktop.start()
        self.bus.callbacks[1](({},))
        calls = list(self.bus.calls)
        with self.assertRaises(PortalError):
            self.desktop.check_open()
        self.assertEqual(self.bus.calls, calls)

    def test_start_after_revocation_creates_fresh_session(self):
        self.desktop.start()
        self.bus.callbacks[1](({},))
        self.bus.results["CreateSession"] = {"session_handle": "/session/new"}
        self.assertEqual(self.desktop.start(), [Display(42, 1920, 1080)])
        self.assertEqual(self.desktop.session, "/session/new")
        self.assertEqual([call[1] for call in self.bus.calls].count("CreateSession"), 2)

    def test_failed_stop_preserves_session_for_retry(self):
        self.desktop.start()
        self.bus.fail_method = "Close"
        try:
            with self.assertRaises(PortalError):
                self.desktop.stop()
            self.assertEqual(self.desktop.session, "/session/codex")
            self.assertEqual(len(self.bus.callbacks), 1)
        finally:
            self.bus.fail_method = None
        self.desktop.stop()
        self.assertEqual((self.desktop.session, self.bus.callbacks), (None, {}))

    def test_failed_start_and_close_are_not_reused_as_ready_session(self):
        self.bus.fail_method = "Start"
        original_call = self.bus.call

        def fail_close(*args, **kwargs):
            if args[1] == "Close":
                raise PortalError("close failed")
            return original_call(*args, **kwargs)

        with patch.object(self.bus, "call", side_effect=fail_close):
            with self.assertRaisesRegex(PortalError, "close failed"):
                self.desktop.start()
        self.bus.fail_method = None
        self.assertEqual(self.desktop.start(), [Display(42, 1920, 1080)])
        self.assertEqual([call[1] for call in self.bus.calls].count("CreateSession"), 2)

    def test_revocation_received_during_close_completes_cleanup(self):
        self.desktop.start()

        def revoked_close(*args, **kwargs):
            self.bus.callbacks[1](({},))
            raise PortalError("session already closed")

        with patch.object(self.bus, "call", side_effect=revoked_close):
            self.desktop.stop()
        self.assertEqual((self.desktop.session, self.bus.callbacks), (None, {}))


if __name__ == "__main__":
    unittest.main()
