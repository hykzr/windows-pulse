from __future__ import annotations

import base64
import io
import json
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import windowpulse.cli as cli_module
from PIL import Image
from windowpulse.models import (
    CapturedFrame,
    CaptureOptions,
    ChangeDetectionOptions,
    OutputResolution,
    QueueFullPolicy,
    QueueOptions,
    Region,
    WindowInfo,
)
from windowpulse.video import WindowVideoRecorder


class FakeWriter:
    def __init__(self) -> None:
        self.primed = False
        self.frames: list[bytes] = []
        self.closed = False

    def send(self, value: bytes | None) -> None:
        if value is None:
            self.primed = True
        else:
            self.frames.append(value)

    def close(self) -> None:
        self.closed = True


def install_fake_imageio(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], list[FakeWriter]]:
    calls: list[dict[str, Any]] = []
    writers: list[FakeWriter] = []

    def write_frames(output: str, size: tuple[int, int], **kwargs: Any) -> FakeWriter:
        writer = FakeWriter()
        calls.append({"output": output, "size": size, **kwargs})
        writers.append(writer)
        return writer

    monkeypatch.setitem(
        sys.modules,
        "imageio_ffmpeg",
        SimpleNamespace(write_frames=write_frames),
    )
    return calls, writers


def install_fake_cli_recorder(
    monkeypatch: pytest.MonkeyPatch,
    frames: Sequence[CapturedFrame],
) -> list[Any]:
    instances: list[Any] = []

    class FakeRecorder:
        def __init__(self, window: WindowInfo, **kwargs: object) -> None:
            self.window = window
            self.kwargs = kwargs
            self.started = False
            self.stop_calls: list[dict[str, object]] = []
            instances.append(self)

        def start(self) -> FakeRecorder:
            self.started = True
            return self

        def __iter__(self) -> Iterator[CapturedFrame]:
            return iter(frames)

        def stop(self, **kwargs: object) -> None:
            self.stop_calls.append(dict(kwargs))

    monkeypatch.setattr(cli_module, "WindowRecorder", FakeRecorder)
    return instances


def png_records(data: bytes) -> list[Image.Image]:
    records: list[Image.Image] = []
    offset = 0
    while offset < len(data):
        header = data[offset : offset + 8]
        assert len(header) == 8
        offset += 8
        length = int.from_bytes(header, "big")
        payload = data[offset : offset + length]
        assert len(payload) == length
        offset += length
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            records.append(image.copy())
    return records


def test_video_duplicates_last_changed_frame_across_timeline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls, writers = install_fake_imageio(monkeypatch)
    video = WindowVideoRecorder(
        WindowInfo(id=7, title="Deck"),
        tmp_path / "deck.mp4",
        video_fps=2,
    )
    red = Image.new("RGBA", (2, 2), (255, 0, 0, 255))
    blue = Image.new("RGBA", (2, 2), (0, 0, 255, 255))

    video._handle_frame(CapturedFrame(10.0, red))
    video._handle_frame(CapturedFrame(11.5, blue))

    assert calls == [
        {
            "output": str(tmp_path / "deck.mp4"),
            "size": (2, 2),
            "fps": 2.0,
            "codec": "libx264",
            "pix_fmt_in": "rgb24",
            "pix_fmt_out": "yuv420p",
            "output_params": [],
        }
    ]
    assert writers[0].primed
    assert writers[0].frames == [red.convert("RGB").tobytes()] * 3 + [blue.convert("RGB").tobytes()]
    assert video._next_frame_index == 4


def test_video_pads_odd_dimensions_with_black_pixels(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls, writers = install_fake_imageio(monkeypatch)
    image = Image.new("RGBA", (3, 1))
    image.putdata(
        [
            (10, 20, 30, 255),
            (40, 50, 60, 255),
            (70, 80, 90, 255),
        ]
    )
    video = WindowVideoRecorder(WindowInfo(id=7, title="Deck"), tmp_path / "odd.mp4")

    video._handle_frame(CapturedFrame(10.0, image))

    assert calls[0]["size"] == (4, 2)
    encoded = Image.frombytes("RGB", (4, 2), writers[0].frames[0])
    assert [encoded.getpixel((x, 0)) for x in range(4)] == [
        (10, 20, 30),
        (40, 50, 60),
        (70, 80, 90),
        (0, 0, 0),
    ]
    assert [encoded.getpixel((x, 1)) for x in range(4)] == [(0, 0, 0)] * 4


def test_video_ctrl_c_stops_and_finalizes_recording(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _calls, writers = install_fake_imageio(monkeypatch)
    video = WindowVideoRecorder(WindowInfo(id=7, title="Deck"), tmp_path / "deck.mp4")

    class FakeRecorder:
        is_running = True

        def __init__(self) -> None:
            self.stop_calls: list[dict[str, object]] = []

        def stop(self, **kwargs: object) -> None:
            self.stop_calls.append(dict(kwargs))

        def raise_if_failed(self) -> None:
            pass

    recorder = FakeRecorder()
    video.recorder = recorder  # type: ignore[assignment]

    def start() -> WindowVideoRecorder:
        video._started = True
        video._start_monotonic = time.monotonic()
        video._start_wall_time = time.time()
        video._handle_frame(CapturedFrame(time.time(), Image.new("RGBA", (2, 2), "red")))
        return video

    monkeypatch.setattr(video, "start", start)

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("windowpulse.video.time.sleep", interrupt)

    assert video.record() == tmp_path / "deck.mp4"
    assert recorder.stop_calls == [{"timeout": 5.0, "drain_handlers": True}]
    assert writers[0].closed


def test_watch_jsonl_framing_and_title_config_forwarding(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    window = WindowInfo(id=8, title="Quarterly Review")
    selected: list[dict[str, object]] = []

    def find_window(**kwargs: object) -> WindowInfo:
        selected.append(dict(kwargs))
        return window

    monkeypatch.setattr(cli_module, "find_window", find_window)
    image = Image.new("RGBA", (2, 1), (1, 2, 3, 255))
    instances = install_fake_cli_recorder(
        monkeypatch,
        [CapturedFrame(123.25, image)],
    )

    result = cli_module.watch_main(
        [
            "--title",
            "Quarterly Review",
            "--exact-title",
            "--fps",
            "12",
            "--crop",
            "1,2,3,4",
            "--output-resolution",
            "720p",
            "--threshold",
            "0.1",
            "--compare-size",
            "10x20",
            "--debounce-ms",
            "250",
            "--cursor",
            "--highlight",
            "--max-queue",
            "2",
            "--queue-full",
            "drop_oldest",
            "--clear-queue-on-window-close",
            "--format",
            "jsonl",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    assert selected == [{"title": "Quarterly Review", "exact": True}]
    assert instances[0].started
    assert instances[0].stop_calls == [{"drain_handlers": False}]
    assert instances[0].kwargs == {
        "capture": CaptureOptions(
            fps=12,
            show_cursor=True,
            show_highlight=True,
            crop=Region(1, 2, 3, 4),
            output_resolution=OutputResolution.P720,
        ),
        "change_detection": ChangeDetectionOptions(
            threshold=0.1,
            comparison_size=(10, 20),
            debounce_seconds=0.25,
        ),
        "queue_options": QueueOptions(
            max_size=2,
            full_policy=QueueFullPolicy.DROP_OLDEST,
            clear_on_window_close=True,
        ),
    }

    event = json.loads(captured.out)
    assert event == {
        "timestamp": 123.25,
        "width": 2,
        "height": 1,
        "mode": "RGBA",
        "encoding": "png",
        "png_base64": event["png_base64"],
    }
    png = base64.b64decode(event["png_base64"], validate=True)
    with Image.open(io.BytesIO(png)) as decoded:
        assert decoded.size == image.size
        assert decoded.mode == image.mode
        assert decoded.getpixel((0, 0)) == (1, 2, 3, 255)


def test_watch_png_stream_has_big_endian_lengths_and_png_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = WindowInfo(id=9, title="Deck")
    selected: list[int] = []

    def get_window(window_id: int) -> WindowInfo:
        selected.append(window_id)
        return window

    monkeypatch.setattr(cli_module, "get_window", get_window)
    frames = [
        CapturedFrame(1.0, Image.new("RGB", (1, 1), "red")),
        CapturedFrame(2.0, Image.new("RGB", (2, 1), "blue")),
    ]
    instances = install_fake_cli_recorder(monkeypatch, frames)
    stdout = SimpleNamespace(buffer=io.BytesIO())
    monkeypatch.setattr(cli_module.sys, "stdout", stdout)

    result = cli_module.watch_main(["--window-id", "9", "--format", "png-stream"])

    assert result == 0
    assert selected == [9]
    assert instances[0].stop_calls == [{"drain_handlers": False}]
    decoded = png_records(stdout.buffer.getvalue())
    assert [image.size for image in decoded] == [(1, 1), (2, 1)]
    assert decoded[0].getpixel((0, 0)) == (255, 0, 0)
    assert decoded[1].getpixel((0, 0)) == (0, 0, 255)


def test_video_cli_forwards_id_capture_and_encoder_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    window = WindowInfo(id=77, title="Deck")
    selected: list[int] = []
    instances: list[Any] = []

    def get_window(window_id: int) -> WindowInfo:
        selected.append(window_id)
        return window

    class FakeVideoRecorder:
        def __init__(self, selected_window: WindowInfo, output: str, **kwargs: object) -> None:
            self.window = selected_window
            self.output = output
            self.kwargs = kwargs
            self.durations: list[float | None] = []
            instances.append(self)

        def record(self, duration: float | None = None) -> Path:
            self.durations.append(duration)
            return Path(self.output)

    monkeypatch.setattr(cli_module, "get_window", get_window)
    monkeypatch.setattr(cli_module, "WindowVideoRecorder", FakeVideoRecorder)

    result = cli_module.video_main(
        [
            "movie.mp4",
            "--window-id",
            "77",
            "--fps",
            "24",
            "--crop",
            "2,3,640,360",
            "--output-resolution",
            "1080p",
            "--threshold",
            "0.2",
            "--compare-size",
            "40x30",
            "--debounce-ms",
            "125",
            "--max-queue",
            "5",
            "--queue-full",
            "drop_oldest",
            "--duration",
            "2.5",
            "--video-fps",
            "12.5",
            "--codec",
            "libvpx-vp9",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == ""
    assert captured.err == (
        "Recording started. Press Ctrl+C to stop.\nRecording stopped: movie.mp4\n"
    )
    assert selected == [77]
    assert instances[0].window is window
    assert instances[0].output == "movie.mp4"
    assert instances[0].durations == [2.5]
    assert instances[0].kwargs == {
        "capture": CaptureOptions(
            fps=24,
            crop=Region(2, 3, 640, 360),
            output_resolution=OutputResolution.P1080,
        ),
        "change_detection": ChangeDetectionOptions(
            threshold=0.2,
            comparison_size=(40, 30),
            debounce_seconds=0.125,
        ),
        "queue_options": QueueOptions(
            max_size=5,
            full_policy=QueueFullPolicy.DROP_OLDEST,
        ),
        "video_fps": 12.5,
        "codec": "libvpx-vp9",
    }


@pytest.mark.parametrize(
    "entrypoint",
    [cli_module.watch_main, cli_module.video_main],
)
def test_cli_help_exits_without_constructing_consumers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entrypoint: Callable[[Sequence[str] | None], int],
) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("--help must exit before constructing a consumer")

    monkeypatch.setattr(cli_module, "WindowRecorder", unexpected)
    monkeypatch.setattr(cli_module, "WindowVideoRecorder", unexpected)

    with pytest.raises(SystemExit) as caught:
        entrypoint(["--help"])

    captured = capsys.readouterr()
    assert caught.value.code == 0
    assert "usage:" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize(
    "entrypoint",
    [cli_module.watch_main, cli_module.video_main],
)
def test_cli_list_windows_needs_no_selector_or_video_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entrypoint: Callable[[Sequence[str] | None], int],
) -> None:
    monkeypatch.setattr(
        cli_module,
        "list_windows",
        lambda: [
            WindowInfo(
                id=42,
                pid=123,
                title="Quarterly Review",
                app_name="Slides",
                bounds=Region(10, 20, 1280, 720),
            ),
            WindowInfo(
                id=45,
                pid=456,
                title="Invisible helper",
                app_name="Helper",
                bounds=Region(0, 0, 1, 1),
            ),
            WindowInfo(
                id=46,
                pid=768,
                title="WLAN",
                app_name="Kontrollzentrum",
                bounds=Region(0, 0, 38, 29),
                bundle_id="com.apple.controlcenter",
            ),
        ],
    )

    result = entrypoint(["--list-windows"])

    captured = capsys.readouterr()
    assert result == 0
    assert "ID" in captured.out
    assert "PID" in captured.out
    assert "Size" in captured.out
    assert "Application" in captured.out
    assert "Title" in captured.out
    assert "42" in captured.out
    assert "123" in captured.out
    assert "1280x720" in captured.out
    assert "Slides" in captured.out
    assert "Quarterly Review" in captured.out
    assert "Invisible helper" not in captured.out
    assert "Kontrollzentrum" not in captured.out
    assert "WLAN" not in captured.out
    assert captured.err == ""
