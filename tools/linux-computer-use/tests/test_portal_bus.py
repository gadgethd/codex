import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from codex_linux_computer_use.dbus import PortalBus, PortalError


class PortalRequestTests(unittest.TestCase):
    """Exercise request ordering and cleanup without requiring a login session."""

    def setUp(self):
        self.events = []
        self.response = (0, {"session_handle": "/session/test"})
        self.timer = SimpleNamespace(
            set_callback=lambda callback: setattr(self, "expire", callback),
            attach=lambda context: None,
            destroy=lambda: self.events.append("timer destroyed"),
        )
        self.bus = PortalBus.__new__(PortalBus)
        self.bus.cancel_event = None
        self.bus.connection = SimpleNamespace(get_unique_name=lambda: ":1.42")
        self.bus.GLib = SimpleNamespace(
            timeout_source_new=lambda milliseconds: self.timer
        )
        self.bus.variant = lambda signature, value: value
        self.bus.context = SimpleNamespace(iteration=self.iteration)
        self.bus.subscribe = self.subscribe
        self.bus.unsubscribe = lambda subscription: self.events.append("unsubscribed")
        self.bus.call = Mock(side_effect=self.call)

    def subscribe(self, interface, signal, path, callback):
        self.events.append("subscribed")
        self.path = path
        self.callback = callback
        return 1

    def call(self, interface, method, signature, values, **kwargs):
        self.bus._check_cancelled()
        self.events.append(method)
        if method == "CreateSession":
            self.assertEqual(self.events[:2], ["subscribed", "CreateSession"])
            self.assertEqual(
                self.path,
                f"/org/freedesktop/portal/desktop/request/1_42/{values[-1]['handle_token']}",
            )
            return (self.path,)
        return ()

    def iteration(self, may_block):
        if self.response is None:
            self.expire()
        else:
            self.callback(self.response)

    def request(self):
        return self.bus.request(
            "org.freedesktop.portal.RemoteDesktop", "CreateSession", "(a{sv})", (), {}
        )

    def test_success_subscribes_before_request_and_cleans_up(self):
        self.assertEqual(self.request(), {"session_handle": "/session/test"})
        self.assertEqual(
            self.events,
            ["subscribed", "CreateSession", "timer destroyed", "unsubscribed"],
        )

    def test_immediate_response_is_not_lost(self):
        original = self.bus.call.side_effect

        def immediate(*args, **kwargs):
            result = original(*args, **kwargs)
            self.callback(self.response)
            return result

        self.bus.call.side_effect = immediate
        self.assertEqual(self.request(), {"session_handle": "/session/test"})

    def test_timeout_closes_outstanding_desktop_prompt(self):
        self.response = None
        with self.assertRaisesRegex(PortalError, "timed out"):
            self.request()
        self.assertEqual(
            self.events,
            ["subscribed", "CreateSession", "timer destroyed", "unsubscribed", "Close"],
        )
        self.bus.call.assert_called_with(
            "org.freedesktop.portal.Request", "Close", "()", (), path=self.path
        )

    def test_cancel_and_denial_are_errors(self):
        for code, message in [(1, "cancelled"), (2, "denied")]:
            with self.subTest(code=code):
                self.response = code, {}
                self.events.clear()
                with self.assertRaisesRegex(PortalError, message):
                    self.request()
                self.assertEqual(self.events[-2:], ["timer destroyed", "unsubscribed"])

    def test_client_cancellation_closes_pending_prompt(self):
        cancelled = threading.Event()
        self.bus.cancel_event = cancelled
        self.bus.context.iteration = lambda may_block: cancelled.set()
        with self.assertRaisesRegex(PortalError, "operation was cancelled"):
            self.request()
        self.assertEqual(
            self.events,
            [
                "subscribed",
                "CreateSession",
                "timer destroyed",
                "timer destroyed",
                "unsubscribed",
                "Close",
            ],
        )
        self.assertIs(self.bus.cancel_event, cancelled)

    def test_successful_session_handle_is_retained_when_cancellation_races_reply(self):
        self.bus.cancel_event = threading.Event()

        def respond_and_cancel(may_block):
            self.callback(self.response)
            self.bus.cancel_event.set()

        self.bus.context.iteration = respond_and_cancel
        self.assertEqual(self.request(), {"session_handle": "/session/test"})

    def test_transport_failure_cleans_up_even_if_request_close_fails(self):
        self.bus.call.side_effect = PortalError("bus disconnected")
        with self.assertRaisesRegex(PortalError, "bus disconnected"):
            self.request()
        self.assertEqual(self.events, ["subscribed", "timer destroyed", "unsubscribed"])
        self.assertEqual(self.bus.call.call_count, 2)

    def test_unexpected_handle_is_closed_instead_of_waiting_for_lost_signal(self):
        self.bus.call.side_effect = lambda *args, **kwargs: ("/unexpected",)
        with self.assertRaisesRegex(PortalError, "handle token"):
            self.request()
        self.assertEqual(
            self.bus.call.call_args_list[1].kwargs, {"path": "/unexpected"}
        )
        self.assertEqual(self.events, ["subscribed", "timer destroyed", "unsubscribed"])

    def test_invalid_timeout_does_not_subscribe_or_open_request(self):
        for timeout in (0, -1, 121, float("nan"), float("inf"), True, "120"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.bus.request(
                    "test", "CreateSession", "(a{sv})", (), {}, timeout=timeout
                )
        self.assertEqual(self.events, [])
        self.bus.call.assert_not_called()

    def test_subscription_restores_callers_thread_context_even_on_error(self):
        self.bus.poll = Mock()
        self.bus.Gio = SimpleNamespace(DBusSignalFlags=SimpleNamespace(NONE=0))
        self.bus.context = SimpleNamespace(
            push_thread_default=lambda: self.events.append("push"),
            pop_thread_default=lambda: self.events.append("pop"),
        )
        self.bus.connection.signal_subscribe = Mock(
            side_effect=RuntimeError("disconnected")
        )
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            PortalBus.subscribe(
                self.bus, "interface", "signal", "/path", lambda value: None
            )
        self.assertEqual(self.events, ["push", "pop"])

    def test_disconnected_bus_can_be_closed_repeatedly(self):
        error = RuntimeError("The connection is closed")
        error.matches = Mock(return_value=True)
        self.bus.GLib.Error = RuntimeError
        self.bus.Gio = SimpleNamespace(
            io_error_quark=lambda: "gio", IOErrorEnum=SimpleNamespace(CLOSED=18)
        )
        self.bus.poll = Mock()
        self.bus.closed = False
        self.bus.connection.close_sync = Mock(side_effect=error)
        self.bus.close()
        self.bus.close()
        error.matches.assert_called_once_with("gio", 18)
        self.bus.connection.close_sync.assert_called_once_with(None)
        self.assertTrue(self.bus.closed)


if __name__ == "__main__":
    unittest.main()
