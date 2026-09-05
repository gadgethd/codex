"""Synchronous, bounded portal transport with a private GLib signal context.

One transport belongs to one calling thread. A dedicated bus connection prevents
closing this client from interfering with other desktop integrations.
"""

import threading
import time
import uuid
from contextlib import contextmanager


class PortalError(RuntimeError):
    """A portal request failed, was denied, or outlived its session."""


class PortalBus:
    DESTINATION = "org.freedesktop.portal.Desktop"
    PATH = "/org/freedesktop/portal/desktop"

    def __init__(self):
        try:
            from gi.repository import Gio, GLib
        except ImportError as error:
            raise PortalError(
                "Install the distribution's Python GObject bindings (PyGObject)."
            ) from error
        self.Gio, self.GLib = Gio, GLib
        self.thread = threading.get_ident()
        self.context = GLib.MainContext.new()
        try:
            address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
            self.connection = Gio.DBusConnection.new_for_address_sync(
                address,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None,
                None,
            )
        except GLib.Error as error:
            raise PortalError(
                f"Cannot connect to the desktop session bus: {error.message}"
            ) from error
        self.closed = False
        self.cancel_event = None

    def variant(self, signature, value):
        return self.GLib.Variant(signature, value)

    def poll(self):
        if threading.get_ident() != self.thread:
            raise PortalError("The desktop session must be used on its owning thread.")
        if self.closed:
            raise PortalError("The desktop bus connection is closed.")
        self._check_cancelled()
        # Do not let an unrelated flood of bus events keep a tool call alive.
        for _ in range(64):
            if not self.context.pending():
                break
            self.context.iteration(False)

    def _check_cancelled(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise PortalError("The desktop operation was cancelled.")

    @contextmanager
    def cleanup(self):
        """Permit resource release after the requesting client has cancelled."""
        cancelled = self.cancel_event
        self.cancel_event = None
        try:
            yield
        finally:
            self.cancel_event = cancelled

    def call(
        self,
        interface,
        method,
        signature,
        values,
        *,
        path=None,
        receive_fd=False,
        before_send=None,
    ):
        self.poll()
        if before_send is not None:
            before_send()
        try:
            reply, fds = self.connection.call_with_unix_fd_list_sync(
                self.DESTINATION,
                path or self.PATH,
                interface,
                method,
                self.variant(signature, values),
                None,
                self.Gio.DBusCallFlags.NONE,
                10000,
                None,
                None,
            )
            if receive_fd:
                if fds is None:
                    raise PortalError(f"{method} did not return a file descriptor.")
                return fds.get(reply.unpack()[0])
            return reply.unpack()
        except self.GLib.Error as error:
            raise PortalError(f"{interface}.{method}: {error.message}") from error

    def subscribe(self, interface, signal, path, callback):
        self.poll()
        self.context.push_thread_default()
        try:
            return self.connection.signal_subscribe(
                self.DESTINATION,
                interface,
                signal,
                path,
                None,
                self.Gio.DBusSignalFlags.NONE,
                lambda _bus, _sender, _path, _interface, _signal, parameters: callback(
                    parameters.unpack()
                ),
            )
        finally:
            self.context.pop_thread_default()

    def unsubscribe(self, subscription):
        self.connection.signal_unsubscribe(subscription)

    def request(self, interface, method, signature, values, options, *, timeout=120):
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise ValueError(
                "Portal request timeout must be between 0 and 120 seconds."
            )
        token = "codex_" + uuid.uuid4().hex
        sender = self.connection.get_unique_name()[1:].replace(".", "_")
        path = f"{self.PATH}/request/{sender}/{token}"
        responses = []
        subscription = self.subscribe(
            "org.freedesktop.portal.Request", "Response", path, responses.append
        )
        options = {**options, "handle_token": self.variant("s", token)}
        deadline = time.monotonic() + timeout
        timed_out = []
        timer = self.GLib.timeout_source_new(max(1, int(timeout * 1000)))
        timer.set_callback(lambda *_args: timed_out.append(True) or False)
        timer.attach(self.context)
        cancel_timer = None
        if self.cancel_event is not None:
            # Wake a blocked signal iteration so an MCP cancellation can close
            # the permission prompt without waiting for the request timeout.
            cancel_timer = self.GLib.timeout_source_new(100)
            cancel_timer.set_callback(lambda *_args: True)
            cancel_timer.attach(self.context)
        try:
            (returned_path,) = self.call(
                interface, method, signature, (*values, options)
            )
            if returned_path != path:
                # A modern portal must honor handle_token so we can subscribe
                # before calling the method and not lose fast Response signals.
                self.call(
                    "org.freedesktop.portal.Request",
                    "Close",
                    "()",
                    (),
                    path=returned_path,
                )
                raise PortalError(
                    "The desktop portal did not honor the request handle token."
                )
            while not responses and not timed_out and time.monotonic() < deadline:
                self._check_cancelled()
                self.context.iteration(True)
            if not responses:
                self._check_cancelled()
                raise PortalError(f"{method} timed out waiting for desktop permission.")
            # Return successful handles even when cancellation races the reply.
            # The owner must retain them so its cleanup can close the session.
            code, result = responses[0]
            if code != 0:
                raise PortalError(
                    f"{method}: desktop permission {'cancelled' if code == 1 else 'denied or failed'}."
                )
            return result
        finally:
            timer.destroy()
            if cancel_timer is not None:
                cancel_timer.destroy()
            self.unsubscribe(subscription)
            if not responses:
                with self.cleanup():
                    try:
                        self.call(
                            "org.freedesktop.portal.Request",
                            "Close",
                            "()",
                            (),
                            path=path,
                        )
                    except PortalError:
                        pass

    def close(self):
        if not self.closed:
            with self.cleanup():
                self.poll()
                try:
                    self.connection.close_sync(None)
                except self.GLib.Error as error:
                    if not error.matches(
                        self.Gio.io_error_quark(), self.Gio.IOErrorEnum.CLOSED
                    ):
                        raise PortalError(
                            f"Cannot close the desktop bus: {error.message}"
                        ) from error
                finally:
                    self.closed = True
