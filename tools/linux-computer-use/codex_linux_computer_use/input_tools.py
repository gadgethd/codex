"""Bounded native input actions, registered behind the service's policy guard."""

import time
from contextlib import ExitStack
from typing import Annotated, Literal

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import Field, StrictInt

Coordinate = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
Steps = Annotated[int, Field(strict=True, ge=-100, le=100)]
ClickCount = Annotated[int, Field(strict=True, ge=1, le=3)]
Key = Annotated[str, Field(strict=True, min_length=1, max_length=32)]
Chord = Annotated[list[Key], Field(min_length=1, max_length=8)]

# X11 keysyms are also the RemoteDesktop portal's keyboard protocol.
KEYS = {
    "BACKSPACE": 0xFF08,
    "TAB": 0xFF09,
    "ENTER": 0xFF0D,
    "ESC": 0xFF1B,
    "ESCAPE": 0xFF1B,
    "DELETE": 0xFFFF,
    "HOME": 0xFF50,
    "LEFT": 0xFF51,
    "UP": 0xFF52,
    "RIGHT": 0xFF53,
    "DOWN": 0xFF54,
    "PAGEUP": 0xFF55,
    "PAGEDOWN": 0xFF56,
    "END": 0xFF57,
    "INSERT": 0xFF63,
    "SPACE": 0x20,
    "SHIFT": 0xFFE1,
    "CTRL": 0xFFE3,
    "CONTROL": 0xFFE3,
    "ALT": 0xFFE9,
    "SUPER": 0xFFEB,
    **{f"F{n}": 0xFFBD + n for n in range(1, 25)},
}
BUTTONS = {"left": 272, "right": 273, "middle": 274}


def register_input_tools(server, run):
    options = {
        "meta": {"codex/linuxComputerUse": True},
        "annotations": ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, openWorldHint=True
        ),
    }

    @server.tool(**options)
    async def move_pointer(
        stream: StrictInt, x: Coordinate, y: Coordinate, ctx: Context
    ) -> str:
        """Move the pointer on a shared display using its logical coordinates from start_session. Screenshot pixel dimensions can differ."""
        await run(ctx, lambda desktop, check_lock, policy: desktop.move(stream, x, y))
        return "Pointer moved."

    @server.tool(**options)
    async def click(
        stream: StrictInt,
        x: Coordinate,
        y: Coordinate,
        ctx: Context,
        button: Literal["left", "right", "middle"] = "left",
        count: ClickCount = 1,
    ) -> str:
        """Click at logical display coordinates. Supports one, two or three clicks and always releases the button."""

        def action(desktop, check_lock, policy):
            desktop.move(stream, x, y)
            for index in range(count):
                if index:
                    time.sleep(0.08)
                check_lock()
                try:
                    desktop.button(BUTTONS[button], pressed=True)
                finally:
                    desktop.button(BUTTONS[button], pressed=False)

        await run(ctx, action)
        return "Click completed."

    @server.tool(**options)
    async def drag(
        stream: StrictInt,
        start_x: Coordinate,
        start_y: Coordinate,
        end_x: Coordinate,
        end_y: Coordinate,
        ctx: Context,
    ) -> str:
        """Drag the left button between two logical positions on one shared display. The button is released on success or failure."""

        def action(desktop, check_lock, policy):
            desktop.check_open()
            display = next((d for d in desktop.displays if d.stream == stream), None)
            if display is None:
                raise ValueError("Unknown display stream.")
            if any(
                x >= display.width or y >= display.height
                for x, y in ((start_x, start_y), (end_x, end_y))
            ):
                raise ValueError(
                    "Drag positions must be inside the display's logical dimensions."
                )
            desktop.move(stream, start_x, start_y)
            check_lock()
            try:
                desktop.button(BUTTONS["left"], pressed=True)
                for step in range(1, 13):
                    check_lock()
                    desktop.move(
                        stream,
                        start_x + (end_x - start_x) * step / 12,
                        start_y + (end_y - start_y) * step / 12,
                    )
                    time.sleep(0.015)
            finally:
                desktop.button(BUTTONS["left"], pressed=False)

        await run(ctx, action)
        return "Drag completed."

    @server.tool(**options)
    async def scroll(
        stream: StrictInt,
        x: Coordinate,
        y: Coordinate,
        ctx: Context,
        vertical: Steps = 0,
        horizontal: Steps = 0,
    ) -> str:
        """Scroll at logical display coordinates by at most 100 steps per axis. Positive vertical scrolls down; positive horizontal scrolls right."""

        def action(desktop, check_lock, policy):
            desktop.move(stream, x, y)
            for axis in ({"vertical": vertical}, {"horizontal": horizontal}):
                check_lock()
                desktop.scroll(**axis)

        await run(ctx, action)
        return "Scroll completed."

    @server.tool(**options)
    async def press_key(keys: Chord, ctx: Context) -> str:
        """Press and release a keyboard chord in the listed order, such as ["CTRL", "a"] or ["ENTER"]. Supports printable ASCII keys, CTRL, SHIFT, ALT, SUPER, navigation keys, ESC, TAB, BACKSPACE, DELETE and F1-F24."""

        def action(desktop, check_lock, policy):
            symbols = []
            for key in keys:
                symbol = KEYS.get(key.upper())
                if symbol is None and len(key) == 1 and 0x20 <= ord(key) <= 0x7E:
                    symbol = ord(key)
                if symbol is None or symbol in symbols:
                    raise ValueError(f"Unsupported or repeated key: {key}")
                symbols.append(symbol)
            with ExitStack() as release:
                for symbol in symbols:
                    check_lock()
                    release.callback(desktop.keysym, symbol, pressed=False)
                    desktop.keysym(symbol, pressed=True)

        await run(ctx, action)
        return "Keyboard chord completed."
