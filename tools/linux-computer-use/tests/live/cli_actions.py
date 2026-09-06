"""Drive a bounded inspected control action through actual scripted CLI calls."""

import json


def advance_actions(state, calls, body, number):
    tool = calls[number - 1][0]
    if tool == "perform_action":
        return True
    if tool not in ("get_app_state", "get_actions"):
        return False
    assert len(calls) < 36, "Fixture control traversal exceeded its limit"
    output = next(
        item["output"]
        for item in body["input"]
        if item.get("type") == "function_call_output"
        and item.get("call_id") == f"call-{number - 1}"
    )
    page = json.loads(json.loads(output[output.index("{") :])["result"])
    if tool == "get_app_state":
        node = page["node"]
        if node["name"] == "Record activation":
            state["action_target"] = {
                "app_id": state["app_id"],
                "node_id": node["id"],
                "path": node["path"],
            }
            calls.append(("get_actions", state["action_target"]))
        else:
            paths = state.setdefault("action_paths", [])
            paths.extend(child["path"] for child in page["children"])
            assert paths and page["next_cursor"] is None, (
                "Incomplete fixture control tree"
            )
            calls.append(("get_app_state", {"path": paths.pop(0)}))
    else:
        action = page["actions"][0]
        calls.append(
            (
                "perform_action",
                {
                    **state["action_target"],
                    "action_index": action["index"],
                    "action_name": action["name"],
                },
            )
        )
    return False
