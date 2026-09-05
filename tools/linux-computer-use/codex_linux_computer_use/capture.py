"""Read one bounded PNG frame from a portal-authorized PipeWire stream."""

from .dbus import PortalError


def capture_png(fd, stream, width, height):
    try:
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GstApp", "1.0")
        from gi.repository import GLib, Gst, GstApp
    except (ImportError, ValueError) as error:
        raise PortalError(
            "Install GStreamer Python bindings, PipeWire and PNG plugins."
        ) from error
    Gst.init(None)
    scale = min(1, 2048 / max(width, height))
    width, height = max(1, round(width * scale)), max(1, round(height * scale))
    try:
        pipeline = Gst.parse_launch(
            "pipewiresrc name=source do-timestamp=true ! videoconvert ! videoscale ! "
            f"video/x-raw,width={width},height={height},pixel-aspect-ratio=1/1 ! "
            "pngenc snapshot=true ! appsink name=sink max-buffers=1 drop=true sync=false"
        )
    except GLib.Error as error:
        raise PortalError(
            f"Cannot create the desktop capture pipeline: {error.message}"
        ) from error
    source = pipeline.get_by_name("source")
    source.set_property("fd", fd)
    source.set_property("path", str(stream))
    sink = pipeline.get_by_name("sink")
    assert isinstance(sink, GstApp.AppSink)
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise PortalError("Failed to start the PipeWire capture stream.")
        sample = sink.try_pull_sample(10 * Gst.SECOND)
        if sample is None:
            message = pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR)
            if message:
                error, _debug = message.parse_error()
                raise PortalError(f"PipeWire capture failed: {error.message}")
            raise PortalError("Timed out waiting for a desktop frame.")
        buffer = sample.get_buffer()
        if not 0 < buffer.get_size() <= 16 * 1024 * 1024:
            raise PortalError("Desktop frame exceeds the 16 MiB limit.")
        png = buffer.extract_dup(0, buffer.get_size())
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise PortalError("The capture pipeline returned an invalid PNG.")
        return {"png": png, "width": width, "height": height}
    finally:
        pipeline.set_state(Gst.State.NULL)
