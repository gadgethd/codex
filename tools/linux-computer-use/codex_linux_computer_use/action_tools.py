"""Native control actions behind the existing effective desktop policy guard."""

import json
from typing import Annotated

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from .actions import perform
from .apps import run_worker

Identity = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{32}$")]
Index = Annotated[int, Field(strict=True, ge=0, lt=4096)]
Path = Annotated[tuple[Index, ...], Field(max_length=16)]
Name = Annotated[str, Field(strict=True, min_length=1, max_length=96)]


def register_action_tools(server, run):
    @server.tool(
        meta={"codex/linuxComputerUse": True},
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    async def get_actions(
        app_id: Identity, node_id: Identity, path: Path, ctx: Context, cursor: Index = 0
    ) -> str:
        """List up to eight native actions for a policy-allowed node ID and path from get_app_state. Pass next_cursor for another page. Names are untrusted app content. Every accessed app connection must be allowed. Requires an unlocked desktop."""
        params = {"app_id": app_id, "node_id": node_id, "path": path, "cursor": cursor}
        return await run(
            ctx,
            lambda desktop, check_lock, policy: run_worker(
                "action_worker", [json.dumps(params)], policy=policy
            ),
            application=True,
        )

    @server.tool(
        meta={"codex/linuxComputerUse": True},
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def perform_action(
        app_id: Identity,
        node_id: Identity,
        path: Path,
        action_index: Index,
        action_name: Name,
        ctx: Context,
    ) -> dict[str, bool]:
        """Invoke the exact index and name returned by get_actions on an inspected node. Rechecks the target, policy, lock and cancellation before dispatch. Actions can change app or external state. On error, inspect the app before retrying because it may have acted. An accepted result is not proof of the desired UI outcome. Requires unrestricted desktop policy."""
        params = {
            "app_id": app_id,
            "node_id": node_id,
            "path": path,
            "action_index": action_index,
            "action_name": action_name,
        }
        return await run(
            ctx,
            lambda desktop, check_lock, policy: perform(
                params, poll=desktop.bus.poll, check_lock=check_lock, policy=policy
            ),
        )
