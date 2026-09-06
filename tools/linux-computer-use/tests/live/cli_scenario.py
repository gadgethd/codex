"""Script Responses calls while exercising the real CLI and native MCP server."""

import asyncio
import base64
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEXT = "Codex CLI paste — café Ελληνικά 日本語 🐧\nSecond line: naïve مرحبا 한국어"
PREVIOUS = "Existing clipboard — preserved"


async def wait_for_cli(proc, *, timeout=150, termination_timeout=10):
    """Reap the test client even when its deadline or cancellation interrupts it."""
    try:
        return await asyncio.wait_for(proc.wait(), timeout)
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), termination_timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()


def wait_file(path, expected=None):
    for _ in range(150):
        if path.exists() and (expected is None or path.read_text() == expected):
            return
        time.sleep(0.1)
    raise AssertionError(f"Expected fixture output missing or incorrect: {path}")


def images_in(value):
    if isinstance(value, dict):
        if value.get("type") == "input_image":
            yield value["image_url"]
        for item in value.values():
            yield from images_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from images_in(item)


async def exercise(output, codex, spawn):
    gtk = output / "gtk"
    gtk.mkdir()
    spawn("gtk", [sys.executable, str(HERE / "gtk_fixture.py"), str(gtk)])
    await asyncio.to_thread(wait_file, gtk / "ready")
    steps = [
        ("start_session", {"clipboard": True}),
        ("screenshot", {}),
        ("click", {"x": 534, "y": 578}),
        ("press_key", {"keys": ["ESC"]}),
        ("click", {"x": 600, "y": 350}),
        ("screenshot", {}),
        ("paste_text", {"text": TEXT}),
        ("screenshot", {}),
        ("stop_session", {}),
    ]
    for policy in ("deny", "allow"):
        run = output / policy
        run.mkdir()
        if policy == "allow":
            spawn("consent", [sys.executable, str(HERE / "consent.py")])
        await run_policy(
            run, codex, gtk, steps if policy == "allow" else steps[:1], policy
        )


async def run_policy(run, codex, gtk, calls, policy):
    state = {"requests": 0, "images": 0, "error": None, "stream": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                self.connection.settimeout(20)
                length = int(self.headers["Content-Length"])
                assert 0 < length < 16 * 1024 * 1024
                assert self.headers.get("Content-Encoding") is None
                body = json.loads(self.rfile.read(length))
                number = state["requests"]
                assert number <= len(calls)
                state["requests"] += 1
                encoded = json.dumps(body, ensure_ascii=False)
                assert all(
                    marker not in encoded
                    for marker in ("Existing clipboard", "Previous rich clipboard")
                )
                (run / f"request-{number}.json").write_text(encoded)
                if number == 0:
                    namespace = next(
                        t
                        for t in body["tools"]
                        if t.get("name") == "mcp__linux_computer_use"
                    )
                    assert "paste_text" in {t["name"] for t in namespace["tools"]}
                if number == 1:
                    result = next(
                        i["output"]
                        for i in body["input"]
                        if i.get("type") == "function_call_output"
                    )
                    if policy == "deny":
                        assert "under this application policy" in str(result)
                    else:
                        state["stream"] = json.loads(result[result.index("{") :])[
                            "result"
                        ][0]["stream"]
                images = list(images_in(body.get("input", [])))
                for i, url in enumerate(images[state["images"] :], state["images"]):
                    (run / f"image-{i}.png").write_bytes(
                        base64.b64decode(url.split(",", 1)[1])
                    )
                state["images"] = len(images)
                if number == 6:
                    (gtk / "copy-before").touch()
                    wait_file(gtk / "copied")
                    time.sleep(0.3)
                if number == 7:
                    wait_file(gtk / "text.txt", TEXT)
                    (gtk / "read-clipboard").touch()
                    wait_file(gtk / "clipboard.txt", PREVIOUS)
                if number < len(calls):
                    name, args = calls[number]
                    args = dict(args)
                    if name in ("click", "screenshot"):
                        args["stream"] = state["stream"]
                    item = {
                        "type": "function_call",
                        "call_id": f"call-{number}",
                        "namespace": "mcp__linux_computer_use",
                        "name": name,
                        "arguments": json.dumps(args),
                    }
                    time.sleep(0.5)
                else:
                    item = {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Native CLI smoke completed.",
                            }
                        ],
                    }
                response = {"id": f"smoke-{number}"}
                events = [
                    {"type": "response.created", "response": response},
                    {"type": "response.output_item.done", "item": item},
                    {"type": "response.completed", "response": response},
                ]
                data = "".join(
                    "data: " + json.dumps(event) + "\n\n" for event in events
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (
                AssertionError,
                KeyError,
                ValueError,
                IndexError,
                StopIteration,
                OSError,
                TypeError,
            ) as error:
                state["error"] = repr(error)
                self.send_error(500)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {
        "model_provider": "smoke",
        "model_providers.smoke.name": "Local smoke",
        "model_providers.smoke.base_url": f"http://127.0.0.1:{server.server_port}/v1",
        "model_providers.smoke.wire_api": "responses",
        "model_providers.smoke.requires_openai_auth": False,
        "model_providers.smoke.supports_websockets": False,
        "model_providers.smoke.request_max_retries": 0,
        "model_providers.smoke.stream_max_retries": 0,
        "features.code_mode": False,
        "features.apps": False,
        "computer_use.default_app_access": policy,
        "mcp_servers.linux_computer_use.command": sys.executable,
        "mcp_servers.linux_computer_use.args": ["-m", "codex_linux_computer_use"],
        "mcp_servers.linux_computer_use.cwd": str(HERE.parents[1]),
        "mcp_servers.linux_computer_use.env_vars": [
            "DBUS_SESSION_BUS_ADDRESS",
            "WAYLAND_DISPLAY",
            "XDG_RUNTIME_DIR",
            "XDG_CURRENT_DESKTOP",
            "XDG_SESSION_TYPE",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
        ],
        "mcp_servers.linux_computer_use.omit_tools_from": ["deferred"],
        "mcp_servers.linux_computer_use.tool_timeout_sec": 150,
        **{
            f"mcp_servers.linux_computer_use.tools.{name}.approval_mode": "approve"
            for name in ("click", "press_key", "paste_text")
        },
    }
    args = [
        str(codex),
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C",
        str(run),
        "--json",
        "--color",
        "never",
        "-m",
        "gpt-5",
    ]
    for key, value in config.items():
        args.extend(["-c", key + "=" + json.dumps(value)])
    args.append("Exercise the scripted native Linux smoke test in the private desktop.")
    try:
        with (run / "cli.jsonl").open("w") as log:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=log, stderr=asyncio.subprocess.STDOUT
            )
            code = await wait_for_cli(proc)
        assert code == 0 and state["error"] is None, state
        assert state["requests"] == len(calls) + 1, state
        assert state["images"] == (3 if policy == "allow" else 0), state
        if policy == "allow":
            completed = []
            for line in (run / "cli.jsonl").read_text().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                item = event.get("item", {})
                if (
                    event.get("type") == "item.completed"
                    and item.get("type") == "mcp_tool_call"
                ):
                    assert item["status"] == "completed" and item["error"] is None, (
                        item["tool"]
                    )
                    completed.append(item["tool"])
            assert completed == [name for name, _ in calls], completed
        (run / "result.json").write_text(
            json.dumps({"status": "passed", "policy": policy, **state}, indent=2)
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
