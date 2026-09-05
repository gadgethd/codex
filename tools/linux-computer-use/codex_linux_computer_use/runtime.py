"""Serialize asynchronous clients onto the desktop connection's owning thread.

Only one operation may be outstanding. Cancellation keeps that slot occupied
until the worker has stopped and released held inputs; no actions are queued.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from .dbus import PortalError
from .portal import PortalDesktop


class DesktopRuntime:
    def __init__(self, factory=PortalDesktop):
        self.factory = factory
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="desktop")
        self.desktop = None
        self.pending = None
        self.busy = False
        self.cancel_event = None
        self.closing = False
        self.close_task = None

    async def run(self, action):
        if self.closing:
            raise PortalError("The desktop runtime is closed.")
        if self.busy:
            raise PortalError("Another desktop operation is still running.")
        self.busy = True
        cancelled = threading.Event()
        self.cancel_event = cancelled
        pending = asyncio.get_running_loop().run_in_executor(
            self.executor, self._invoke, action, cancelled
        )
        self.pending = pending
        # Retrieve late exceptions even when the caller has already cancelled.
        pending.add_done_callback(lambda future: future.exception())
        try:
            await asyncio.wait({pending})
            return pending.result()
        except asyncio.CancelledError:
            cancelled.set()
            if not self.closing:
                # Cancellation can arrive after the worker returned. Reserve
                # the slot until cleanup has run on the same owning thread.
                def stop():
                    if self.desktop is not None:
                        self.desktop.bus.cancel_event = None
                        try:
                            self.desktop.stop()
                        except BaseException:
                            try:
                                self.desktop.close()
                            finally:
                                self.desktop = None
                            raise

                def finished(future):
                    future.exception()
                    self.busy = False

                self.pending = asyncio.get_running_loop().run_in_executor(
                    self.executor, stop
                )
                self.pending.add_done_callback(finished)
            raise
        finally:
            if not cancelled.is_set():
                self.busy = False

    def _invoke(self, action, cancelled):
        if cancelled.is_set():
            raise PortalError("The desktop operation was cancelled.")
        if self.desktop is None:
            self.desktop = self.factory()
        self.desktop.bus.cancel_event = cancelled
        try:
            self.desktop.bus.poll()
            result = action(self.desktop)
            if cancelled.is_set():
                raise PortalError("The desktop operation was cancelled.")
            return result
        except BaseException:
            self.desktop.bus.cancel_event = None
            self.desktop.release_inputs()
            raise
        finally:
            self.desktop.bus.cancel_event = None

    async def close(self):
        if self.close_task is None:
            self.closing = True
            if self.cancel_event is not None:
                self.cancel_event.set()
            self.close_task = asyncio.create_task(self._close())
        # Client disconnect must not cancel resource cleanup on the worker.
        await asyncio.shield(self.close_task)

    async def _close(self):
        try:
            if self.pending is not None:
                await asyncio.wait({self.pending})
                # The action's caller receives its error; shutdown still closes
                # the connection after failed or cancelled work.
                self.pending.exception()
            if self.desktop is not None:
                await asyncio.get_running_loop().run_in_executor(
                    self.executor, self.desktop.close
                )
        finally:
            self.executor.shutdown(wait=False, cancel_futures=True)
