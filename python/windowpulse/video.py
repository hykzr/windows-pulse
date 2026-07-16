"""Video-file consumer for change-aware window capture."""

from __future__ import annotations

import threading
import time
from collections.abc import Generator, Sequence
from contextlib import suppress
from math import ceil
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Protocol, cast

from PIL import Image

from .errors import RecorderStateError, VideoDependencyError
from .models import (
    CapturedFrame,
    CaptureOptions,
    ChangeDetectionOptions,
    QueueOptions,
    WindowInfo,
)
from .recorder import WindowRecorder

if TYPE_CHECKING:
    from .processing import HandlerPool

_FrameWriter = Generator[None, bytes | None, None]


class _WriterFactory(Protocol):
    def __call__(
        self,
        path: str,
        size: tuple[int, int],
        *,
        fps: float,
        codec: str,
        pix_fmt_in: str,
        pix_fmt_out: str,
        output_params: list[str],
    ) -> _FrameWriter: ...


class WindowVideoRecorder:
    """Encode a single window to a constant-frame-rate video without audio.

    WindowPulse still captures only changed frames. This consumer holds the last
    accepted image across unchanged periods when reconstructing the video timeline.
    """

    def __init__(
        self,
        window: WindowInfo | int,
        output: str | Path,
        *,
        capture: CaptureOptions | None = None,
        change_detection: ChangeDetectionOptions | None = None,
        queue_options: QueueOptions | None = None,
        video_fps: float | None = None,
        codec: str = "libx264",
        ffmpeg_output_params: Sequence[str] = (),
    ) -> None:
        self.capture_options = capture or CaptureOptions()
        self.video_fps = float(self.capture_options.fps) if video_fps is None else float(video_fps)
        if self.video_fps <= 0:
            raise ValueError("video_fps must be greater than zero")
        if not codec:
            raise ValueError("codec must not be empty")

        self.output = Path(output)
        self.codec = codec
        self.ffmpeg_output_params = tuple(ffmpeg_output_params)
        self.recorder = WindowRecorder(
            window,
            capture=self.capture_options,
            change_detection=change_detection,
            queue_options=queue_options,
        )
        self._writer: _FrameWriter | None = None
        self._handler_pool: HandlerPool | None = None
        self._first_timestamp: float | None = None
        self._last_image: Image.Image | None = None
        self._next_frame_index = 0
        self._encode_size: tuple[int, int] | None = None
        self._started = False
        self._stopped = False
        self._start_monotonic: float | None = None
        self._start_wall_time: float | None = None
        self._stop_error: BaseException | None = None
        self._lock = threading.Lock()

    def _open_writer(self, image: Image.Image) -> None:
        try:
            import imageio_ffmpeg
        except ImportError as error:
            raise VideoDependencyError(
                "video recording needs the optional dependency; install windowpulse[video]"
            ) from error

        width, height = image.size
        self._encode_size = (width + width % 2, height + height % 2)
        try:
            # imageio-ffmpeg is untyped and Pyright infers ``fps`` from its integer
            # default even though the function documents and accepts float values.
            write_frames = cast(_WriterFactory, imageio_ffmpeg.write_frames)
            writer = write_frames(
                str(self.output),
                self._encode_size,
                fps=self.video_fps,
                codec=self.codec,
                pix_fmt_in="rgb24",
                pix_fmt_out="yuv420p",
                output_params=list(self.ffmpeg_output_params),
            )
            writer.send(None)
            self._writer = writer
        except BaseException:
            self._writer = None
            raise

    def _pixels(self, image: Image.Image) -> bytes:
        rgb = image.convert("RGB")
        assert self._encode_size is not None
        if rgb.size == self._encode_size:
            return rgb.tobytes()
        if rgb.width > self._encode_size[0] or rgb.height > self._encode_size[1]:
            raise RecorderStateError(
                f"captured window size changed from {self._encode_size} to {rgb.size}"
            )
        padded = Image.new("RGB", self._encode_size)
        padded.paste(rgb, (0, 0))
        return padded.tobytes()

    def _send(self, image: Image.Image) -> None:
        if self._writer is None:
            self._open_writer(image)
        assert self._writer is not None
        self._writer.send(self._pixels(image))

    def _handle_frame(self, frame: CapturedFrame) -> None:
        with self._lock:
            if self._first_timestamp is None:
                self._first_timestamp = frame.timestamp
                self._last_image = frame.image.copy()
                self._send(self._last_image)
                self._next_frame_index = 1
                return

            assert self._last_image is not None
            if frame.image.size != self._last_image.size:
                raise RecorderStateError(
                    f"captured window size changed from {self._last_image.size} "
                    f"to {frame.image.size}; start a new video after resizing"
                )
            target_index = max(
                self._next_frame_index,
                ceil((frame.timestamp - self._first_timestamp) * self.video_fps),
            )
            while self._next_frame_index < target_index:
                self._send(self._last_image)
                self._next_frame_index += 1
            self._last_image = frame.image.copy()
            self._send(self._last_image)
            self._next_frame_index += 1

    def start(self) -> WindowVideoRecorder:
        if self._started:
            raise RecorderStateError("video recorder has already been started")
        if self.output.exists() and self.output.is_dir():
            raise IsADirectoryError(self.output)
        if not self.output.parent.exists():
            raise FileNotFoundError(self.output.parent)
        self._started = True
        self._start_monotonic = time.monotonic()
        self._start_wall_time = time.time()
        try:
            self.recorder.start()
            self._handler_pool = self.recorder.start_handler(self._handle_frame)
        except BaseException:
            self._started = False
            with suppress(BaseException):
                self.recorder.stop(drain_handlers=False)
            raise
        return self

    def _fill_to_stop_time(self) -> None:
        if self._last_image is None or self._first_timestamp is None:
            return
        assert self._start_monotonic is not None
        assert self._start_wall_time is not None
        end_timestamp = self._start_wall_time + (time.monotonic() - self._start_monotonic)
        final_count = max(1, ceil((end_timestamp - self._first_timestamp) * self.video_fps))
        while self._next_frame_index < final_count:
            self._send(self._last_image)
            self._next_frame_index += 1

    def stop(self, *, timeout: float | None = None) -> Path:
        if not self._started:
            raise RecorderStateError("video recorder has not been started")
        if self._stopped:
            if self._stop_error is not None:
                raise self._stop_error
            return self.output
        self._stopped = True
        stop_timeout = 5.0 if timeout is None else timeout
        error: BaseException | None = None
        try:
            self.recorder.stop(timeout=stop_timeout, drain_handlers=True)
            with self._lock:
                self._fill_to_stop_time()
        except BaseException as caught:
            error = caught
        finally:
            if self._writer is not None:
                try:
                    self._writer.close()
                except BaseException as close_error:
                    if error is None:
                        error = close_error
                finally:
                    self._writer = None

        if self._first_timestamp is None:
            error = error or RecorderStateError(
                "the window capture ended before producing a video frame"
            )
        if error is not None:
            self._stop_error = error
            raise error
        return self.output

    def record(self, duration: float | None = None) -> Path:
        if duration is not None and duration <= 0:
            raise ValueError("duration must be greater than zero or None")
        self.start()
        try:
            if duration is None:
                while self.recorder.is_running:
                    time.sleep(0.1)
                    self.recorder.raise_if_failed()
            else:
                deadline = time.monotonic() + duration
                while self.recorder.is_running and time.monotonic() < deadline:
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                    self.recorder.raise_if_failed()
        except KeyboardInterrupt:
            pass
        return self.stop()

    def __enter__(self) -> WindowVideoRecorder:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()
