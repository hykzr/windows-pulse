from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import pytest
import windowpulse.recorder as recorder_module
from PIL import Image
from windowpulse.errors import (
    CapturePermissionError,
    RecorderStateError,
    UnsupportedPlatformError,
)
from windowpulse.models import (
    CaptureOptions,
    ChangeDetectionOptions,
    OutputResolution,
    RecorderState,
    Region,
    WindowInfo,
)
from windowpulse.recorder import WindowRecorder

NativeFrame = tuple[float, int, int, str, bytes]
NativeResult = NativeFrame | None


def rgba_frame(timestamp: float, value: int, size: tuple[int, int] = (2, 2)) -> NativeFrame:
    width, height = size
    pixel = bytes((value, value, value, 255))
    return (timestamp, width, height, "RGBA", pixel * width * height)


def install_fake_backend(
    monkeypatch: pytest.MonkeyPatch,
    frames: Sequence[NativeResult],
    *,
    constructor_error: BaseException | None = None,
) -> tuple[type[Any], list[Any]]:
    instances: list[Any] = []

    class FakeNativeCapturer:
        def __init__(self, *args: object) -> None:
            if constructor_error is not None:
                raise constructor_error
            self.args = args
            self.frames = list(frames)
            self.index = 0
            self.stopped = False
            instances.append(self)

        def next_frame(self, *args: object, **kwargs: object) -> NativeResult:
            del args, kwargs
            if self.index < len(self.frames):
                frame = self.frames[self.index]
                self.index += 1
            else:
                frame = self.frames[-1]
            # Keep the worker responsive without busy-spinning on quiet-backend polls.
            time.sleep(0.002 if frame is None else 0.005)
            return frame

        def stop(self) -> None:
            self.stopped = True

        @property
        def is_stopped(self) -> bool:
            return self.stopped

    monkeypatch.setattr(recorder_module._native, "NativeCapturer", FakeNativeCapturer)
    monkeypatch.setattr(recorder_module, "is_supported", lambda: True)
    monkeypatch.setattr(recorder_module, "has_permission", lambda: True)
    return FakeNativeCapturer, instances


def test_recorder_worker_filters_frames_and_enqueues_owned_pillow_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, instances = install_fake_backend(
        monkeypatch,
        [rgba_frame(1.0, 0), rgba_frame(2.0, 0), rgba_frame(3.0, 255)],
    )
    window = WindowInfo(id=42, title="Deck", bounds=Region(10, 20, 4, 4))
    recorder = WindowRecorder(
        window,
        capture=CaptureOptions(
            fps=60,
            show_cursor=True,
            show_highlight=True,
            crop=Region(1, 1, 2, 2),
            output_resolution=OutputResolution.P720,
        ),
        change_detection=ChangeDetectionOptions(threshold=0.0),
    )

    try:
        recorder.start(timeout=1.0)
        assert recorder.state is RecorderState.RUNNING
        assert recorder.backend is instances[0]

        first = recorder.get(timeout=1.0)
        recorder.task_done()
        changed = recorder.get(timeout=1.0)
        recorder.task_done()

        assert (first.timestamp, changed.timestamp) == (1.0, 3.0)
        assert isinstance(first.image, Image.Image)
        assert first.image.mode == "RGBA"
        assert first.image.size == (2, 2)
        assert first.image.getpixel((0, 0)) == (0, 0, 0, 255)
        assert changed.image.getpixel((0, 0)) == (255, 255, 255, 255)

        # The fake reuses immutable source bytes; queued Pillow images own usable data.
        changed.image.putpixel((0, 0), (1, 2, 3, 4))
        assert first.image.getpixel((0, 0)) == (0, 0, 0, 255)
    finally:
        recorder.stop(timeout=1.0)

    assert instances[0].args == (42, 60, True, True, (1, 1, 2, 2), "720p")
    assert instances[0].stopped
    assert recorder.state is RecorderState.STOPPED
    assert recorder.queue.closed
    assert recorder.stats.captured >= 3
    assert recorder.stats.emitted == 2
    assert recorder.stats.filtered >= 1
    assert recorder.stats.dropped == 0


def test_recorder_surfaces_native_constructor_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_backend(
        monkeypatch,
        [rgba_frame(1.0, 0)],
        constructor_error=RuntimeError("native build failed"),
    )
    recorder = WindowRecorder(WindowInfo(id=7, title="Broken"))

    with pytest.raises(RecorderStateError, match="worker failed") as caught:
        recorder.start(timeout=1.0)

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == "native build failed"
    assert recorder.state is RecorderState.FAILED
    assert recorder.queue.closed


def test_recorder_polls_detector_when_native_backend_goes_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_backend(
        monkeypatch,
        [rgba_frame(1.0, 0), rgba_frame(2.0, 255), None],
    )
    recorder = WindowRecorder(
        WindowInfo(id=42, title="Deck"),
        change_detection=ChangeDetectionOptions(
            threshold=0.0,
            debounce_seconds=0.02,
        ),
    )

    try:
        recorder.start(timeout=1.0)
        initial = recorder.get(timeout=1.0)
        recorder.task_done()
        stable = recorder.get(timeout=1.0)
        recorder.task_done()
    finally:
        recorder.stop(timeout=1.0)

    assert initial.timestamp == 1.0
    assert stable.timestamp == 2.0
    assert stable.image.getpixel((0, 0)) == (255, 255, 255, 255)


def test_recorder_validates_crop_against_window_local_size() -> None:
    window = WindowInfo(id=42, title="Small", bounds=Region(100, 200, 10, 10))
    with pytest.raises(ValueError, match="exceeds"):
        WindowRecorder(window, capture=CaptureOptions(crop=Region(9, 9, 2, 2)))


def test_start_checks_support_and_permission_before_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = WindowRecorder(WindowInfo(id=42, title="Deck"))

    monkeypatch.setattr(recorder_module, "is_supported", lambda: False)
    with pytest.raises(UnsupportedPlatformError):
        recorder.start()
    assert recorder.state is RecorderState.CREATED

    monkeypatch.setattr(recorder_module, "is_supported", lambda: True)
    monkeypatch.setattr(recorder_module, "has_permission", lambda: False)
    with pytest.raises(CapturePermissionError):
        recorder.start()
    assert recorder.state is RecorderState.CREATED


def test_stopping_an_unstarted_recorder_is_idempotent() -> None:
    recorder = WindowRecorder(WindowInfo(id=42, title="Deck"))
    recorder.stop()
    recorder.stop()
    assert recorder.state is RecorderState.STOPPED
    assert recorder.queue.closed
