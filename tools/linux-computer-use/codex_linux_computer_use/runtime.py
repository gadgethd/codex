"""Serialize asynchronous clients onto the desktop connection's owning thread.

Only one operation may be outstanding. Cancellation keeps that slot occupied
until the worker has stopped and released held inputs; no actions are queued.
"""

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from .dbus import PortalError
from .portal import PortalDesktop

logger = logging.getLogger(__name__)


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
        self.idle_task = None
        self.idle_pending = None
        self.idle_cancel = None
        self.idle_error = None

    async def run(self, action):
        if self.closing:
            raise PortalError("The desktop runtime is closed.")
        if self.busy:
            raise PortalError("Another desktop operation is still running.")
        self.busy = True
        try:
            if self.idle_pending is not None:
                await asyncio.wait({self.idle_pending})
            if self.closing:
                raise PortalError("The desktop runtime is closed.")
            if self.idle_error is not None:
                error, self.idle_error = self.idle_error, None
                raise PortalError(f"Desktop background service failed: {error}")
        except BaseException:
            self.busy = False
            raise
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
            if not self.closing and (self.idle_task is None or self.idle_task.done()):
                self.idle_task = asyncio.create_task(self._idle_loop())

    async def _idle_loop(self):
        while not self.closing:
            await asyncio.sleep(0.05)
            if self.closing or self.busy or self.desktop is None:
                continue
            self.idle_cancel = threading.Event()
            self.idle_pending = asyncio.get_running_loop().run_in_executor(
                self.executor, self._idle, self.idle_cancel
            )
            await asyncio.wait({self.idle_pending})
            self.idle_pending.result()
            self.idle_pending = None

    def _idle(self, cancelled):
        try:
            self._invoke(lambda desktop: desktop.idle(), cancelled)
        except BaseException as error:
            logger.exception("Desktop background service failed")
            self.idle_error = str(error)[:512]
            try:
                if self.desktop is not None:
                    self.desktop.close()
            except BaseException as cleanup_error:
                logger.exception("Desktop background cleanup failed")
                self.idle_error = f"{self.idle_error}; cleanup failed: {cleanup_error}"[
                    :512
                ]
            finally:
                self.desktop = None

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
            if self.idle_cancel is not None:
                self.idle_cancel.set()
            self.close_task = asyncio.create_task(self._close())
        # Client disconnect must not cancel resource cleanup on the worker.
        await asyncio.shield(self.close_task)

    async def _close(self):
        try:
            if self.idle_task is not None:
                await self.idle_task
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
