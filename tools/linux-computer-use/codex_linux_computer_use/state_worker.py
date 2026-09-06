"""Inspect a bounded page of one app's accessible controls and text."""

import json
import sys

from .apps import MAX_RESULT_BYTES
from .apps_worker import (
    ACCESSIBLE,
    REGISTRY,
    ROOT,
    AccessibilityBus,
    encode,
    identifier,
    label,
)

TEXT = "org.a11y.atspi.Text"
STATES = {
    4: "checked",
    6: "defunct",
    7: "editable",
    8: "enabled",
    10: "expanded",
    11: "focusable",
    12: "focused",
    23: "selected",
    24: "sensitive",
    25: "showing",
    30: "visible",
    43: "readOnly",
}


def count(bus, ref, interface=ACCESSIBLE, name="ChildCount"):
    value = bus.property(*ref, interface, name)
    if type(value) is not int or not 0 <= value <= 2147483647:
        raise ValueError("Invalid accessibility count")
    return value


def node(bus, ref, path):
    (role,) = bus.call(*ref, ACCESSIBLE, "GetRole")
    if type(role) is not int or not 0 <= role <= 4294967295:
        raise ValueError("Invalid accessible role")
    try:
        (role_name,) = bus.call(*ref, ACCESSIBLE, "GetRoleName")
        role_name = label(role_name, 48)
    except TimeoutError:
        raise
    except OSError:
        role_name = str(role)
    (words,) = bus.call(*ref, ACCESSIBLE, "GetState")
    if len(words) != 2 or any(
        type(word) is not int or not 0 <= word <= 4294967295 for word in words
    ):
        raise ValueError("Invalid accessibility state set")
    bits = words[0] | (words[1] << 32)
    return {
        "id": identifier(bus, *ref),
        "path": path,
        "role": role_name,
        "name": label(bus.property(*ref, ACCESSIBLE, "Name"), 96),
        "states": [name for bit, name in STATES.items() if bits & (1 << bit)],
        "child_count": count(bus, ref),
        "password": role == 40,
    }


def resolve(bus, app_id, path):
    app = None
    for index in range(min(count(bus, (REGISTRY, ROOT)), 4096)):
        try:
            candidate = bus.child(REGISTRY, ROOT, index)
            if identifier(bus, *candidate) == app_id:
                app = candidate
                break
        except TimeoutError:
            raise
        except (OSError, TypeError, ValueError):
            continue
    if app is None:
        raise ValueError("Application is no longer registered; call list_apps again.")
    ref = app
    for index in path:
        if index >= count(bus, ref):
            raise ValueError("The accessible path changed; inspect its parent again.")
        ref = bus.child(*ref, index)
    return app, ref


def inspect_app(bus, app_id, path, cursor, text_offset):
    app, ref = resolve(bus, app_id, path)
    current = node(bus, ref, path)
    result = {
        "node": current,
        "children": [],
        "next_cursor": None,
        "unavailable": 0,
        "limited": current["child_count"] > 4096 or len(path) == 16,
        "text": None,
        "next_text_offset": None,
    }
    (interfaces,) = bus.call(*ref, ACCESSIBLE, "GetInterfaces")
    if not isinstance(interfaces, (list, tuple)) or len(interfaces) > 32:
        raise ValueError("Invalid accessibility interfaces")
    if not current["password"] and TEXT in interfaces:
        length = count(bus, ref, TEXT, "CharacterCount")
        if text_offset > length:
            raise ValueError("Text changed; restart at text_offset zero.")
        end = min(length, text_offset + 129)
        (text,) = bus.call(*ref, TEXT, "GetText", "(ii)", (text_offset, end))
        if not isinstance(text, str) or len(text) > end - text_offset:
            raise ValueError("Invalid accessible text range")
        # Qt uses UTF-16 offsets. Keep a lookahead character so a split final
        # surrogate cannot be emitted, and advance by the text actually retained.
        application = app if ref[0] == app[0] else (ref[0], ROOT)
        toolkit = bus.property(
            *application, "org.a11y.atspi.Application", "ToolkitName"
        )
        text = (text[:-1] if end < length else text)[:128]
        units = len(text.encode("utf-16-le")) // 2 if toolkit == "Qt" else len(text)
        if not units and text_offset < length or units > end - text_offset:
            raise ValueError("Text changed; restart at text_offset zero.")
        result["text"] = text
        following = text_offset + units
        result["next_text_offset"] = following if following < length else None
    index = cursor
    ceiling = min(current["child_count"], 4096) if len(path) < 16 else 0
    while index < min(ceiling, cursor + 16) and len(result["children"]) < 8:
        try:
            child = node(bus, bus.child(*ref, index), [*path, index])
        except TimeoutError:
            if index == cursor:
                raise
            break
        except (OSError, TypeError, ValueError):
            result["unavailable"] += 1
            index += 1
            continue
        candidate = {**result, "children": [*result["children"], child]}
        if len(encode(candidate)) > MAX_RESULT_BYTES - 32:
            break
        result["children"].append(child)
        index += 1
    result["next_cursor"] = index if index < ceiling else None
    return result


if __name__ == "__main__":
    sys.stdout.buffer.write(
        encode(
            inspect_app(
                AccessibilityBus(),
                sys.argv[1],
                json.loads(sys.argv[2]),
                int(sys.argv[3]),
                int(sys.argv[4]),
            )
        )
    )
