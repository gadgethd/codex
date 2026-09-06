"""Exercise the installed distribution without a checkout on the import path."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from mcp import Client, StdioServerParameters


async def exercise():
    import codex_linux_computer_use

    package = Path(codex_linux_computer_use.__file__).resolve()
    assert package.is_relative_to(Path(sys.prefix).resolve()), package
    env = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    with tempfile.TemporaryDirectory() as directory:
        # A project with a colliding module must not replace the console service.
        Path(directory, "codex_linux_computer_use.py").write_text(
            "raise RuntimeError('Imported the working directory instead of the wheel')"
        )
        for command, args in (
            (str(Path(sys.executable).with_name("codex-linux-computer-use")), []),
            (sys.executable, ["-I", "-m", "codex_linux_computer_use"]),
        ):
            server = StdioServerParameters(
                command=command, args=args, cwd=directory, env=env
            )
            async with Client(server) as client:
                tools = await client.list_tools()
                assert tools.tools, "Installed server did not expose tools"
                result = await client.call_tool("start_session")
                assert result.is_error, "Missing host policy must prevent desktop use"
                assert any(
                    "requires Linux policy metadata" in getattr(block, "text", "")
                    for block in result.content
                ), result
    print("Installed console/module startup and missing-policy denial passed.")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(exercise(), 30))
