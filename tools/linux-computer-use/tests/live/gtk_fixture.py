import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk

assert os.environ["XDG_RUNTIME_DIR"] == os.environ[
    "CUA_PRIVATE_RUNTIME"
] and not os.environ.get("DISPLAY")
root = Path(sys.argv[1])
copied = False
reading = False
app = Gtk.Application(application_id="com.example.CodexPasteFixture")


def activate(app):
    window = Gtk.ApplicationWindow(application=app, title="Codex GTK paste fixture")
    window.set_default_size(600, 400)
    view = Gtk.TextView()
    window.set_child(view)
    buffer = view.get_buffer()
    buffer.connect(
        "changed",
        lambda buffer: (root / "text.txt").write_text(
            buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        ),
    )
    clipboard = Gdk.Display.get_default().get_clipboard()

    def received(clipboard, result):
        (root / "clipboard.txt").write_text(clipboard.read_text_finish(result))

    def poll():
        global copied, reading
        if (root / "copy-before").exists() and not copied:
            copied = True
            clipboard.set_content(
                Gdk.ContentProvider.new_union(
                    [
                        Gdk.ContentProvider.new_for_bytes(
                            "text/plain;charset=utf-8",
                            GLib.Bytes.new("Existing clipboard — preserved".encode()),
                        ),
                        Gdk.ContentProvider.new_for_bytes(
                            "text/plain",
                            GLib.Bytes.new("Existing clipboard — preserved".encode()),
                        ),
                        Gdk.ContentProvider.new_for_bytes(
                            "text/html",
                            GLib.Bytes.new(b"<b>Previous rich clipboard</b>"),
                        ),
                    ]
                )
            )
            (root / "copied").touch()
        if (root / "read-clipboard").exists() and not reading:
            reading = True
            clipboard.read_text_async(None, received)
        return True

    GLib.timeout_add(100, poll)
    window.connect("map", lambda window: (root / "ready").touch())
    window.present()
    if "--smoke" in sys.argv[2:]:
        GLib.timeout_add(1000, app.quit)


app.connect("activate", activate)
app.run([])
