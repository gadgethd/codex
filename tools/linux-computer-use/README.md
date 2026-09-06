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
`click`, `drag` and `scroll` take a shared stream ID and positions within that
display. Clicks support left, right or middle buttons and counts from one to
three. Scrolling accepts up to 100 steps per axis, with positive values moving
down or right. `press_key` accepts a chord such as `["CTRL", "a"]` or `["ENTER"]`,
with up to eight printable ASCII or named keys. Buttons and keys are released
after each action, including failures; cancellation closes the sharing session.
The same host policy and desktop lock checks apply to all input tools.

Live MCP tests on the isolated Fedora 44 GNOME desktop verified button clicks,
ASCII entry, Ctrl+A replacement, both scroll axes and a pointer drag against the
events received by a GTK test application. Unicode entry uses `paste_text`,
described below.

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
use `paste_text` for Unicode beyond that layout.

`DesktopRuntime.run(action)` lets asynchronous clients use the portal on a
dedicated owning thread. It rejects concurrent operations instead of queuing
input. Cancellation closes pending permission prompts and releases held input
before accepting another action; an in-flight D-Bus call may take ten seconds,
and frame capture may take up to its twelve-second deadline. Await `close()` during client
shutdown to close the desktop session and connection on their owning thread.

Direct backend callers can request clipboard permission with
`PortalDesktop.start(clipboard=True)`. The desktop must explicitly grant it;
adding clipboard access to an existing session requires stopping and starting
sharing again. `desktop.clipboard.offer()` retains a nonempty mapping of MIME
types to bytes, with at most 32 formats and 1 MiB total. `DesktopRuntime` services
those offers between actions on the same owning thread. External ownership
changes, revocation and shutdown discard retained bytes. Abandoned clipboard
consumers do not close a healthy sharing session.

For Unicode text, call `start_session` with `clipboard: true`, focus the intended
editor, then call `paste_text`. It accepts up to 8192 characters and 16 KiB of
UTF-8 without NUL characters. The default shortcut is `ctrl+v`; terminals commonly
need `ctrl+shift+v`, and `shift+insert` is also available. Confirm the target text
with a screenshot before retrying: clipboard requests can come from clipboard
managers and do not prove insertion into the intended application.

Paste captures all advertised clipboard formats within 1 MiB and restores them
while sharing remains open. An oversized, unreadable or changing backup prevents
the paste. A newer external clipboard owner is preserved. Some portals do not
report their initial selection; in that case the tool reports that preservation
was unavailable and leaves the paste text on the clipboard. Clipboard persistence
after sharing ends depends on the desktop's clipboard manager. Previous clipboard
contents are never included in tool results.

Live stdio MCP tests on Fedora 44 GNOME Wayland verified exact multiline Unicode
insertion into GTK 4 and Qt 6 editors and a saved VS Code file. GTK and Qt also
read back the restored clipboard text. Broader desktop and Codex client validation
remains tracked in [#7](https://github.com/gadgethd/codex/issues/7).

An opt-in desktop fixture smoke test is available for Fedora 44 GNOME Wayland.
It requires GNOME Shell's headless mode, PipeWire, WirePlumber, the GNOME and
frontend desktop portals, D-Bus, FUSE utilities, and Python GI bindings for GTK 4
and AT-SPI. It uses Fedora's `/usr/libexec` service paths and an English locale;
it is not yet the cross-distribution verification matrix.

From the repository root, using the environment described above:

```sh
tools/linux-computer-use/.venv/bin/python \
  tools/linux-computer-use/tests/live/gnome_smoke.py \
  --output /tmp/codex-linux-fixture-results
```

The output directory must be new. The script creates a private session bus,
GNOME compositor, PipeWire and portal services, verifies that a GTK window maps,
and stops its test processes. Logs and `result.json` remain in the output
directory. It preserves only basic user/path environment values so inherited
accessibility or PipeWire addresses cannot select the host desktop. A companion
consent helper is restricted to this private session for subsequent input tests.

To exercise a built fork CLI as well, add `--codex` and use a new output path:

```sh
tools/linux-computer-use/.venv/bin/python \
  tools/linux-computer-use/tests/live/gnome_smoke.py \
  --codex codex-rs/target/debug/codex \
  --output /tmp/codex-linux-cli-results
```

This uses a local scripted Responses provider and the real stdio MCP service.
It checks host default-deny, then explicitly approves the private test server's
input tools and verifies exact Unicode in GTK, restored clipboard text, three
screenshots at the model-input boundary, and successful sharing cleanup. Both
seeded text and HTML clipboard markers must stay out of model requests. Results,
CLI events and screenshot evidence are saved under `deny/` and `allow/`. The
script ignores user configuration and retains the desktop's consent dialog;
the test helper operates that dialog only within its private session.

This verifies the client integration, not autonomous model behavior. The fixture
coordinates match the fresh Fedora 44 GNOME desktop; other desktops still need
separate live coverage. In ordinary interactive sessions, Codex can request
approval for native input tools. Noninteractive runs must explicitly configure
approved tools according to their intended scope; the smoke test supplies this
configuration only to its disposable client invocation.

The KDE smoke test runs in a rootless Podman container with its own Wayland,
D-Bus, accessibility, PipeWire and portal services. It checks service policy
denial, native screenshots, exact Unicode in GTK and Qt, restored clipboard text,
and sharing cleanup. It supplies test policy directly to MCP; the GNOME CLI test
above covers the actual host boundary. KDE cancellation, revocation and other
client configurations remain tracked in [#49](https://github.com/gadgethd/codex/issues/49).

Build from this directory:

```sh
podman build -t codex-linux-kde -f tests/live/Containerfile.kde .
mkdir /tmp/codex-kde-results
podman run --rm --userns=keep-id \
  --device nvidia.com/gpu=0 --security-opt label=disable \
  -v /tmp/codex-kde-results:/output:Z codex-linux-kde
```

This invocation was verified on Fedora 44 with an NVIDIA GPU configured for
Podman's CDI support. The SELinux label option applies only to that disposable
container and permits its GPU access. Other GPU setups need their own device
arguments: KWin's virtual backend requires working OpenGL rendering for capture;
its QPainter fallback cannot stream the desktop. No host display or session-bus
sockets should be mounted. The output directory must be empty. Logs, package
versions, exact editor/clipboard observations and four screenshots remain there
after the container exits. The permission helper approves only this private test
desktop and disables future-session restoration. This is a Fedora KDE test,
not a claim of coverage for every distribution or GPU.

`list_apps` discovers apps registered with the current session's AT-SPI
accessibility bus, including their name, toolkit and first window title. It
returns up to eight records within 4096 UTF-8 bytes; pass `next_cursor` to get
another page. Restart from zero when apps open or close. Each page scans at most
16 registry entries, with a total ceiling of 4096 entries. Unresponsive entries
are counted as `unavailable`; `limited` reports the registry ceiling. The worker
has an eight-second lifetime, including native connection setup and shutdown.

The opaque app IDs are tied to the accessibility bus, connection and root
object. They are not desktop-file IDs or authorization evidence. App-provided
names and titles are untrusted content. Discovery uses the same host policy and
real lock checks as capture, including a second lock check before returning
results, and currently requires unrestricted desktop access. It does not require
an open sharing session. Apps without accessibility registration still need
screenshots; per-app policy enforcement and targeted actions remain in #4.
The KDE smoke checks GTK/Qt discovery and stable IDs, and the GNOME CLI smoke
checks that the real client forwards the discovered GTK editor to the model.

Discovery also reports `desktop_id` when authenticated process credentials,
the live executable and fixed launch arguments match one installed desktop
entry. D-Bus-activated entries can also match through their registered session-bus
name when authenticated process handles prove that its owner is the same live
process as the accessibility peer. Ownership is checked again before returning;
conflicting D-Bus and Exec identities remain ambiguous. The service only queries
existing owners and never activates applications during discovery. Missing
process-handle support, stale processes, ambiguous launchers and unsupported
launch expansions produce `null`. Inherited desktop groups and
app-provided labels are not used to establish this identity. Desktop entries
describe launch identities, not isolation from hostile programs running as the
same Unix user. This evidence does not yet relax the full-desktop policy check.

`get_app_state` inspects an ID returned by `list_apps`. Start with `path=[]`,
then use a returned child's path to descend into its controls. Each call returns
the selected node's role, name, relevant states, and up to eight children. Use
`next_cursor` to page through children and `next_text_offset` to read subsequent
128-character text ranges. Password-role text is not read. Results share the
4096-byte output cap and eight-second worker lifetime used by discovery.

Paths contain at most 16 child indices; each level is capped at 4096 children.
`limited` reports a depth or child-count limit, and `unavailable` counts failed
child reads. Refresh from the app root after the UI changes: child indices are
navigation hints, not stable action targets. Node IDs identify accessible objects
on this bus; neither app nor node IDs establish authorization. Inspection retains
the full-desktop policy and lock checks. The KDE smoke verifies exact GTK/Qt text
through accessibility, and the GNOME CLI smoke forwards an inspected window.

Use `get_actions` with an inspected app ID, node ID and path to list a control's
native actions. Pass the exact returned index and name to `perform_action`.
The service resolves the target again and checks for changed action names.
Before dispatch, its worker waits for fresh lock and cancellation checks.
Calls retain the existing full-desktop policy requirement and bounded worker
lifetime; an error after dispatch reports an uncertain outcome. Inspect the app
before retrying: acceptance does not establish the desired UI result, and an
action can affect external state. The KDE smoke activates real GTK/Qt buttons
and requires an independent observation from each app.
