"""Native Linux computer-use MCP service, using host policy on every call."""

from contextlib import asynccontextmanager
from dataclasses import asdict

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Context, Image
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .dbus import PortalError
from .input_tools import register_input_tools
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
    async def start_session(ctx: Context) -> list[dict]:
        """Ask the Linux desktop to share monitors and allow native input. The user controls the desktop permission dialog. Returns stream IDs and logical dimensions for subsequent calls."""
        displays = await run(ctx, lambda desktop, check_lock: desktop.start())
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

    register_input_tools(server, run)
    return server
