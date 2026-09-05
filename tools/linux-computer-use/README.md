# Native Linux computer use

This directory is the native Linux implementation being developed in
[gadgethd/codex#1](https://github.com/gadgethd/codex/issues/1). The stdio MCP service
exposes Wayland session creation, monitor screenshots, native input and session
cleanup. Application targeting and additional desktop backends are still in
development.

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
GStreamer runs in a separate process with a twelve-second deadline covering
startup, capture and shutdown. A stalled pipeline is terminated and reaped so
the desktop runtime can report an error and still close its session.

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

Add the following to the fork's Codex configuration, replacing both absolute
paths with your checkout location. The environment allowlist is required because
stdio MCP servers do not inherit desktop connection variables by default.

```toml
[mcp_servers.linux_computer_use]
command = "/absolute/path/to/codex/tools/linux-computer-use/.venv/bin/python"
args = ["-m", "codex_linux_computer_use"]
cwd = "/absolute/path/to/codex/tools/linux-computer-use"
tool_timeout_sec = 150
env_vars = [
  "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY",
  "XDG_RUNTIME_DIR", "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE", "PIPEWIRE_REMOTE",
  "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
]
```

Launch the Codex client from the graphical desktop session. A plain SSH session
does not supply that desktop's connection variables. The service has been
verified through the rebuilt Codex CLI on an isolated Fedora 44 GNOME desktop:
host policy, session creation, one image forwarded to the model, and cleanup.

Input tools use the logical display dimensions returned by `start_session`;
scale screenshot coordinates when its PNG dimensions differ. `move_pointer`,
`click` and `scroll` take a shared stream ID and positions within that
display. Clicks support left, right or middle buttons and counts from one to
three. Scrolling accepts up to 100 steps per axis, with positive values moving
down or right. `press_key` accepts a chord such as `["CTRL", "a"]` or `["ENTER"]`,
with up to eight printable ASCII or named keys. Buttons and keys are released
after each action, including failures; cancellation closes the sharing session.
The same host policy and desktop lock checks apply to all input tools.

Live MCP tests on the isolated Fedora 44 GNOME desktop verified button clicks,
ASCII entry, Ctrl+A replacement and both scroll axes against the
events received by a GTK test application. Arbitrary Unicode entry remains
tracked in [#7](https://github.com/gadgethd/codex/issues/7).

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
before accepting another action; an in-flight D-Bus call may take ten seconds,
and frame capture may take up to its twelve-second deadline. Await `close()` during client
shutdown to close the desktop session and connection on their owning thread.
