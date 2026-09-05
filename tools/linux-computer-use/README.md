# Native Linux computer use

This directory is the native Linux implementation being developed in
[gadgethd/codex#1](https://github.com/gadgethd/codex/issues/1). This first stage
provides a synchronous D-Bus transport for XDG desktop portal requests. Desktop
capture/input, application policy, and the MCP service follow in separate stages;
this transport alone does not expose computer-use tools to Codex.

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

The subsequent capture/input stage has also exercised this transport on Fedora
44 GNOME Wayland, including session creation, native screen capture and cleanup.
Other desktop environments and IDEs remain part of the broader verification
matrix in [#5](https://github.com/gadgethd/codex/issues/5).

Protocol reference: [XDG portal requests](https://flatpak.github.io/xdg-desktop-portal/docs/desktop-portal.html#requests).
