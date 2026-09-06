"""Check text through the native MCP control tree, with bounded traversal."""

import json


async def verify_text(call, app_id, expected):
    paths = [[]]
    for _ in range(32):
        assert paths, "The editor text is missing from accessibility"
        path = paths.pop(0)
        args = {"app_id": app_id, "path": path}
        page = json.loads((await call("get_app_state", args)).content[0].text)
        if page["text"] is not None:
            text = page["text"]
            for _ in range(8):
                if page["next_text_offset"] is None:
                    break
                args["text_offset"] = page["next_text_offset"]
                page = json.loads((await call("get_app_state", args)).content[0].text)
                text += page["text"]
            if text == expected:
                assert page["next_text_offset"] is None
                assert "editable" in page["node"]["states"]
                assert not page["node"]["password"]
                return
        assert page["next_cursor"] is None, "Unexpectedly broad fixture tree"
        paths.extend(child["path"] for child in page["children"])
    raise AssertionError("The editor tree exceeded the fixture node limit")
