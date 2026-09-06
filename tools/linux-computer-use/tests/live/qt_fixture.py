"""Qt editor with file-based observations for the private desktop smoke test."""

import os
import sys
from pathlib import Path

from PyQt6.QtCore import QMimeData, QTimer
from PyQt6.QtWidgets import QApplication, QPlainTextEdit

assert os.environ["XDG_RUNTIME_DIR"] == os.environ["CUA_PRIVATE_RUNTIME"]
assert not os.environ.get("DISPLAY")
root = Path(sys.argv[1])
copied = reading = False
app = QApplication([])
view = QPlainTextEdit()
view.setWindowTitle("Codex Qt paste fixture")
view.resize(600, 400)
view.textChanged.connect(lambda: (root / "text.txt").write_text(view.toPlainText()))
clipboard = app.clipboard()


def poll():
    global copied, reading
    if view.windowHandle().isExposed():
        (root / "ready").touch()
    if (root / "copy-before").exists() and not copied:
        copied = True
        data = QMimeData()
        data.setText("Existing clipboard — preserved")
        data.setHtml("<b>Previous rich clipboard</b>")
        clipboard.setMimeData(data)
        (root / "copied").touch()
    if (root / "read-clipboard").exists() and not reading:
        reading = True
        (root / "clipboard.txt").write_text(clipboard.text())


view.show()
timer = QTimer()
timer.timeout.connect(poll)
timer.start(100)
app.exec()
