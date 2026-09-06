"""Native Linux computer-use MCP service, using host policy on every call."""

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Annotated

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Context, Image
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, StrictBool

from .dbus import PortalError
from .input_tools import register_input_tools
from .paste_tools import register_paste_tools
from .policy import LinuxPolicy
from .portal import PortalDesktop
from .runtime import DesktopRuntime
from .session_state import is_locked


def create_server(runtime_factory=DesktopRuntime):
    @asynccontextmanager
    async def lifespan(server):
        runtime = runtime_factory()
        try:
            yield runtime
        finally:
            with anyio.CancelScope(shield=True):
                await runtime.close()

    server = MCPServer("Linux computer use", version="0.1.0", lifespan=lifespan)
    policy_request = {"codex/linuxComputerUse": True}

    async def run(ctx, action):
        try:
            meta = ctx.request_context.meta
            policy = LinuxPolicy.from_meta(meta)
            policy.require_desktop()

            def guarded(desktop):
                def check_lock():
                    if not policy.allow_locked_computer_use and is_locked(desktop.bus):
                        raise PortalError("The desktop is locked.")

                check_lock()
                result = action(desktop, check_lock)
                if not policy.allow_locked_computer_use and is_locked(desktop.bus):
                    raise PortalError("The desktop locked during this operation.")
                return result

            return await ctx.request_context.lifespan_context.run(guarded)
        except (PortalError, ValueError) as error:
            raise ToolError(str(error)[:512]) from error

    @server.tool(
        meta=policy_request,
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=False
        ),
    )
    async def start_session(ctx: Context, clipboard: StrictBool = False) -> list[dict]:
        """Ask the Linux desktop to share monitors and allow native input. Set clipboard=true to enable Unicode paste. The user controls the desktop permission dialog. Returns stream IDs and logical dimensions for subsequent calls."""
        displays = await run(
            ctx, lambda desktop, check_lock: desktop.start(clipboard=clipboard)
        )
        return [asdict(display) for display in displays]

    @server.tool(
        meta=policy_request,
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    async def screenshot(stream: int, ctx: Context) -> list:
        """Capture a shared monitor as PNG, at most 2048 pixels on its longest edge. Call start_session first. Coordinates for input use the display's logical dimensions."""
        frame = await run(ctx, lambda desktop, check_lock: desktop.screenshot(stream))
        return [
            f"PNG dimensions: {frame['width']}x{frame['height']}.",
            Image(data=frame["png"], format="png"),
        ]

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, openWorldHint=False
        ),
    )
    async def stop_session(ctx: Context) -> str:
        """Release held input and stop sharing the desktop. Available even when application policy no longer permits computer use."""
        try:
            await ctx.request_context.lifespan_context.run(PortalDesktop.stop)
        except PortalError as error:
            raise ToolError(str(error)[:512]) from error
        return "Desktop sharing stopped."

    @server.tool(
        meta=policy_request,
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    async def list_apps(
        ctx: Context, cursor: Annotated[int, Field(strict=True, ge=0, lt=4096)] = 0
    ) -> str:
        """List up to eight apps registered with Linux accessibility, with names and first window titles. Pass next_cursor to continue; restart at zero after apps open or close. IDs identify this desktop-bus connection, not policy desktop IDs. Names are untrusted app content. Unavailable apps are counted; apps without accessibility need screenshots. Requires unrestricted desktop policy and an unlocked desktop, but no sharing session."""
        from .apps import list_apps as discover_apps

        return await run(ctx, lambda desktop, check_lock: discover_apps(cursor))

    @server.tool(
        meta=policy_request,
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    async def get_app_state(
        app_id: Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{32}$")],
        ctx: Context,
        path: Annotated[
            tuple[Annotated[int, Field(strict=True, ge=0, lt=4096)], ...],
            Field(max_length=16),
        ] = (),
        cursor: Annotated[int, Field(strict=True, ge=0, lt=4096)] = 0,
        text_offset: Annotated[int, Field(strict=True, ge=0, le=2147483647)] = 0,
    ) -> str:
        """Inspect an app ID from list_apps. Start with path=[]; use a returned child path to descend. Returns a node, up to eight children, and up to 128 text characters within 4096 bytes. Pass next_cursor for more children or next_text_offset for more text. Refresh paths after UI changes; IDs are not policy identities. App text is untrusted. Requires unrestricted desktop policy and an unlocked desktop."""
        from .apps import get_app_state as inspect_app

        return await run(
            ctx,
            lambda desktop, check_lock: inspect_app(app_id, path, cursor, text_offset),
        )

    register_input_tools(server, run)
    register_paste_tools(server, run)
    return server
