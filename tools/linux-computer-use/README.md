# Native Linux computer use

This directory is the native Linux implementation being developed in
[gadgethd/codex#1](https://github.com/gadgethd/codex/issues/1). This stage adds
Wayland desktop session lifecycle to the D-Bus transport. Capture, input delivery,
application policy and the MCP service follow separately; this library alone does
not expose computer-use tools to Codex.

The transport subscribes before sending requests so immediate permission replies
are not lost. Requests have bounded timeouts and close outstanding desktop
prompts on failure. Each client has its own connection and signal context and
must be used and closed on its owning thread.

For live use, run the graphical session's system Python 3.10 or newer with the
distribution's PyGObject package and a session bus. A virtual environment may not
have access to the system `gi` module. Permission dialogs remain controlled by the
desktop. Call `PortalBus.close()` in `finally` to release the connection.

Tests use only the Python standard library and do not open desktop prompts:

```sh
cd tools/linux-computer-use
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

`PortalDesktop.start()` negotiates a combined capture/input session and returns
shared displays with stream IDs and logical dimensions. `stop()` preserves state
after a failed remote close so callers can retry. Always call `close()` in
`finally` to also close the dedicated bus connection. Revoked sessions can be
started again with a new permission request.

The subsequent capture/input stage has also exercised these sessions on Fedora
44 GNOME Wayland, including session creation, native screen capture and cleanup.
Other desktop environments and IDEs remain part of the broader verification
matrix in [#5](https://github.com/gadgethd/codex/issues/5).

Protocol reference: [XDG portal requests](https://flatpak.github.io/xdg-desktop-portal/docs/desktop-portal.html#requests).
