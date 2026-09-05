"""Unicode paste using a bounded temporary clipboard offer and native shortcuts."""

import time
from contextlib import ExitStack
from functools import partial
from typing import Annotated, Literal

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .clipboard_preservation import capture_clipboard, restore_clipboard
from .dbus import PortalError

Text = Annotated[str, Field(strict=True, min_length=1, max_length=8192)]
Shortcut = Literal["ctrl+v", "ctrl+shift+v", "shift+insert"]
SHORTCUTS = {
    "ctrl+v": (0xFFE3, ord("v")),
    "ctrl+shift+v": (0xFFE3, 0xFFE1, ord("v")),
    "shift+insert": (0xFFE1, 0xFF63),
}


def register_paste_tools(server, run):
    @server.tool(
        meta={"codex/linuxComputerUse": True},
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, openWorldHint=True
        ),
    )
    async def paste_text(
        text: Text, ctx: Context, shortcut: Shortcut = "ctrl+v"
    ) -> str:
        """Request Unicode paste into the focused app, at most 8192 characters and 16 KiB UTF-8. Start with clipboard=true first. Choose ctrl+shift+v for terminals. Restores known clipboard formats while sharing stays open; an unavailable initial clipboard leaves paste text in place. Verify target text before retrying: clipboard transfer cannot prove insertion."""
        try:
            data = text.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ToolError("Paste text must contain valid Unicode.") from error
        if len(data) > 16384 or "\0" in text:
            raise ToolError("Paste text must fit 16 KiB UTF-8 and contain no NUL.")

        def action(desktop, check_lock):
            desktop.check_open()
            content = desktop.clipboard
            if content is None:
                raise PortalError(
                    "Start a new session with clipboard=true before pasting."
                )
            snapshot = capture_clipboard(content, check_lock)
            check_lock()
            generation = None
            try:
                content.offer(
                    {"text/plain;charset=utf-8": data, "text/plain": data},
                    expected_generation=snapshot.generation,
                )
                generation = content.generation
                with ExitStack() as release:
                    for symbol in SHORTCUTS[shortcut]:
                        check_lock()
                        content.transport.check_generation(generation)
                        release.callback(desktop.keysym, symbol, pressed=False)
                        desktop.keysym(
                            symbol,
                            pressed=True,
                            before_send=partial(
                                content.transport.check_generation, generation
                            ),
                        )
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    check_lock()
                    content.serve()
                    time.sleep(0.01)
            finally:
                if generation is not None:
                    restoration = restore_clipboard(content, snapshot, generation)
                elif content.transport.closed:
                    desktop.stop()
            return f"Paste requested; verify the target text. {restoration}"

        return await run(ctx, action)
