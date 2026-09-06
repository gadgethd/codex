"""Resolve native control actions before requesting permission to dispatch one."""

import json
import sys

from .apps import MAX_RESULT_BYTES
from .apps_worker import AccessibilityBus, encode, identifier
from .state_worker import count, resolve

ACTION = "org.a11y.atspi.Action"


def action_name(bus, ref, index):
    (name,) = bus.call(*ref, ACTION, "GetName", "(i)", (index,))
    if not isinstance(name, str) or not 1 <= len(name.encode()) <= 96:
        raise ValueError("Invalid native action name")
    return name


def actions(bus, params, authorize=None):
    _, ref = resolve(bus, params["app_id"], params["path"])
    if identifier(bus, *ref) != params["node_id"]:
        raise ValueError("The control changed; inspect its parent again.")
    total = count(bus, ref, ACTION, "NActions")
    if authorize is not None:
        index = params["action_index"]
        if index >= total or action_name(bus, ref, index) != params["action_name"]:
            raise ValueError("The action changed; list its actions again.")
        authorize("prepare")
        if action_name(bus, ref, index) != params["action_name"]:
            raise ValueError("The action changed before dispatch.")
        authorize("dispatch")
        (accepted,) = bus.call(*ref, ACTION, "DoAction", "(i)", (index,))
        if type(accepted) is not bool:
            raise ValueError("Invalid native action result")
        return {"accepted": accepted}
    index = params["cursor"]
    result = {
        "actions": [],
        "next_cursor": None,
        "unavailable": 0,
        "limited": total > 4096,
    }
    while (
        index < min(total, 4096, params["cursor"] + 16) and len(result["actions"]) < 8
    ):
        try:
            item = {"index": index, "name": action_name(bus, ref, index)}
        except TimeoutError:
            if index == params["cursor"]:
                raise
            break
        except (OSError, TypeError, ValueError):
            result["unavailable"] += 1
            index += 1
            continue
        candidate = {**result, "actions": [*result["actions"], item]}
        if len(encode(candidate)) > MAX_RESULT_BYTES - 32:
            break
        result["actions"].append(item)
        index += 1
    result["next_cursor"] = index if index < min(total, 4096) else None
    return result


if __name__ == "__main__":

    def authorize(phase):
        sys.stdout.buffer.write(b"READY\n" if phase == "dispatch" else b"PREPARE\n")
        sys.stdout.buffer.flush()
        if sys.stdin.buffer.read(1) != b"y":
            raise ValueError("Action dispatch cancelled")

    params = json.loads(sys.argv[1])
    result = actions(
        AccessibilityBus(), params, authorize if "action_index" in params else None
    )
    sys.stdout.buffer.write(encode(result))
