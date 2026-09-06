"""Exercise live app-read permissions without restarting the MCP service."""

import json
from functools import partial

from accessibility import button_action, verify_text


async def verify_reads(client, call, app, launcher, text, policy):
    allowed = {
        **policy,
        "defaultAppAccess": "deny",
        "desktopIds": {app["desktop_id"]: "allow"},
    }
    page = json.loads((await call("list_apps", policy=allowed)).content[0].text)
    assert [item["id"] for item in page["apps"]] == [app["id"]], page
    await verify_text(partial(call, policy=allowed), app["id"], text)
    action = await button_action(partial(call, policy=allowed), app["id"])
    result = await client.call_tool(
        "perform_action", action, meta={"codex/linuxComputerUsePolicy": allowed}
    )
    assert result.is_error
    denied = {**allowed, "desktopIds": {app["desktop_id"]: "deny"}}
    page = (await call("list_apps", policy=denied)).content[0].text
    assert not json.loads(page)["apps"] and app["window"] not in page

    async def blocked(rule):
        result = await client.call_tool(
            "get_app_state",
            {"app_id": app["id"]},
            meta={"codex/linuxComputerUsePolicy": rule},
        )
        assert result.is_error and (text[:127] or app["window"]) not in str(
            result.content
        )

    await blocked(denied)
    await call("get_app_state", {"app_id": app["id"]}, policy=allowed)
    original = launcher.read_text()
    try:
        launcher.unlink()
        await blocked(allowed)
        await blocked({**policy, "desktopIds": {"unrelated.desktop": "deny"}})
    finally:
        launcher.write_text(original)
    await call("get_app_state", {"app_id": app["id"]}, policy=allowed)
