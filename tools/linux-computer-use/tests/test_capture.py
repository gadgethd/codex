import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from codex_linux_computer_use.capture import capture_png
from codex_linux_computer_use.dbus import PortalError


class PipelineError(Exception):
    message = "no element pipewiresrc"


class Sink:
    pass


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.png = b"\x89PNG\r\n\x1a\nframe"
        self.buffer = Mock()
        self.buffer.get_size.return_value = len(self.png)
        self.buffer.extract_dup.return_value = self.png
        self.sink = Sink()
        self.sink.try_pull_sample = Mock(
            return_value=SimpleNamespace(get_buffer=lambda: self.buffer)
        )
        self.pipeline = Mock()
        self.source = Mock()
        self.pipeline.get_by_name.side_effect = {
            "source": self.source,
            "sink": self.sink,
        }.get
        self.pipeline.get_bus.return_value.pop_filtered.return_value = None
        self.gst = SimpleNamespace(
            init=Mock(),
            parse_launch=Mock(return_value=self.pipeline),
            State=SimpleNamespace(PLAYING="playing", NULL="null"),
            StateChangeReturn=SimpleNamespace(FAILURE="failure"),
            MessageType=SimpleNamespace(ERROR="error"),
            SECOND=1_000_000_000,
        )
        self.modules = patch.dict(
            sys.modules,
            {
                "gi": SimpleNamespace(require_version=lambda *args: None),
                "gi.repository": SimpleNamespace(
                    GLib=SimpleNamespace(Error=PipelineError),
                    Gst=self.gst,
                    GstApp=SimpleNamespace(AppSink=Sink),
                ),
            },
        )
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_capture_scales_frame_and_releases_pipeline(self):
        self.assertEqual(
            capture_png(12, 42, 4000, 2000),
            {
                "png": self.png,
                "width": 2048,
                "height": 1024,
            },
        )
        self.assertIn("width=2048,height=1024", self.gst.parse_launch.call_args.args[0])
        self.assertEqual(
            self.source.set_property.call_args_list,
            [
                unittest.mock.call("fd", 12),
                unittest.mock.call("path", "42"),
            ],
        )
        self.assertEqual(
            self.pipeline.set_state.call_args_list,
            [
                unittest.mock.call("playing"),
                unittest.mock.call("null"),
            ],
        )

    def test_missing_plugin_has_backend_error_contract(self):
        self.gst.parse_launch.side_effect = PipelineError()
        with self.assertRaisesRegex(PortalError, "pipewiresrc"):
            capture_png(12, 42, 100, 100)

    def test_capture_failures_always_stop_pipeline(self):
        for failure, expected in (
            ("start", "Failed to start"),
            ("timeout", "Timed out"),
            ("oversized", "exceeds the 16 MiB"),
            ("invalid PNG", "invalid PNG"),
        ):
            with self.subTest(failure=failure):
                self.pipeline.set_state.reset_mock()
                self.pipeline.set_state.return_value = (
                    "failure" if failure == "start" else "ok"
                )
                self.sink.try_pull_sample.return_value = (
                    None
                    if failure == "timeout"
                    else SimpleNamespace(get_buffer=lambda: self.buffer)
                )
                self.buffer.get_size.return_value = (
                    17 * 1024 * 1024 if failure == "oversized" else 8
                )
                self.buffer.extract_dup.return_value = (
                    b"invalid" if failure == "invalid PNG" else self.png
                )
                with self.assertRaisesRegex(PortalError, expected):
                    capture_png(12, 42, 100, 100)
                self.assertEqual(self.pipeline.set_state.call_args.args, ("null",))


if __name__ == "__main__":
    unittest.main()
