# Native Linux computer use

This directory is the native Linux implementation being developed in
[gadgethd/codex#1](https://github.com/gadgethd/codex/issues/1). The stdio MCP service
exposes Wayland session creation, monitor screenshots and session cleanup. Native
input is implemented in the underlying library; MCP input tools, application
targeting and additional desktop backends are still in development.

The transport subscribes before sending requests so immediate permission replies
are not lost. Requests have bounded timeouts and close outstanding desktop
prompts on failure. Each client has its own connection and signal context and
must be used and closed on its owning thread.

For live use, run the graphical session's system Python 3.10 or newer with the
distribution's PyGObject package and a session bus. A virtual environment may not
have access to the system `gi` module. Permission dialogs remain controlled by the
desktop. Call `PortalBus.close()` in `finally` to release the connection.

`screenshot(stream)` returns PNG bytes and image dimensions, capped at a
2048-pixel longest edge and 16 MiB. Capture needs GStreamer 1.0/GstApp bindings,
PipeWire, video conversion/scaling and PNG plugins in the graphical session.

Create an environment with access to the distribution's desktop bindings and
install the MCP dependency:

```sh
cd tools/linux-computer-use
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
```

Run the service with `.venv/bin/python -m codex_linux_computer_use` from this
directory. The Codex host must support the `codex/linuxComputerUsePolicy` request
metadata used by this fork. Tools reject missing or invalid policy; supplying
policy as a tool argument cannot grant access. Each call combines user and
managed restrictions from the host's captured configuration. Full-monitor
capture is unavailable if application restrictions would require masking parts
of the desktop. Lock state is checked before and after an operation, and an
unknown state prevents access. `stop_session` remains available after policy
changes so the client can release desktop sharing.

Tests mock the desktop transport and do not open permission prompts:

```sh
cd tools/linux-computer-use
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
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

`move`, `button`, `keysym` and `scroll` send native portal input. Pointer
coordinates use display logical dimensions, which may differ from PNG pixels.
Presses are tracked and released on stop, including ambiguous transport failures.
Keysym input supports shortcuts and characters in the active keyboard layout;
arbitrary Unicode typing is tracked separately in [#7](https://github.com/gadgethd/codex/issues/7).

`DesktopRuntime.run(action)` lets asynchronous clients use the portal on a
dedicated owning thread. It rejects concurrent operations instead of queuing
input. Cancellation closes pending permission prompts and releases held input
before accepting another action; an in-flight D-Bus call or frame capture may
take up to its ten-second timeout to return. Await `close()` during client
shutdown to close the desktop session and connection on their owning thread.
