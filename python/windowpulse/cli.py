"""Console consumers bundled with WindowPulse."""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
from collections.abc import Sequence

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .errors import WindowPulseError
from .models import (
    CaptureOptions,
    ChangeDetectionOptions,
    OutputResolution,
    QueueFullPolicy,
    QueueOptions,
    Region,
    WindowInfo,
)
from .recorder import WindowRecorder
from .video import WindowVideoRecorder
from .windows import find_window, get_window, list_windows

_CONTROL_CENTER_BUNDLE_ID = "com.apple.controlcenter"


def _region(value: str) -> Region:
    try:
        x, y, width, height = (int(part.strip()) for part in value.split(","))
        return Region(x, y, width, height)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("expected X,Y,W,H with integer values") from error


def _size(value: str) -> tuple[int, int]:
    try:
        width, height = (int(part.strip()) for part in value.lower().split("x"))
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT") from error
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("comparison dimensions must be positive")
    return (width, height)


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return number


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--window-id", type=int, help="native id from --list-windows")
    selector.add_argument("--title", help="case-insensitive window title selector")
    parser.add_argument("--exact-title", action="store_true", help="require an exact title")
    parser.add_argument("--list-windows", action="store_true", help="list targets and exit")
    parser.add_argument("--fps", type=int, default=30, help="capture sampling rate (default: 30)")
    parser.add_argument("--crop", type=_region, metavar="X,Y,W,H", help="window-local crop")
    parser.add_argument(
        "--output-resolution",
        choices=[item.value for item in OutputResolution],
        default=OutputResolution.CAPTURED.value,
        help="maximum standard output size",
    )
    parser.add_argument("--threshold", type=float, default=0.0, help="difference in [0, 1]")
    parser.add_argument(
        "--compare-size",
        type=_size,
        default=(64, 64),
        metavar="WIDTHxHEIGHT",
        help="similarity thumbnail size",
    )
    parser.add_argument("--debounce-ms", type=float, help="stable trailing debounce")
    parser.add_argument("--cursor", action="store_true", help="include the cursor if supported")
    parser.add_argument(
        "--highlight", action="store_true", help="show the system capture border if supported"
    )
    parser.add_argument("--max-queue", type=int, help="maximum pending plus in-flight frames")
    parser.add_argument(
        "--queue-full",
        choices=[item.value for item in QueueFullPolicy],
        default=QueueFullPolicy.BLOCK.value,
        help="bounded queue overflow policy",
    )
    parser.add_argument(
        "--clear-queue-on-window-close",
        action="store_true",
        help="discard pending frames when the target window closes",
    )


def _is_hidden_window(window: WindowInfo) -> bool:
    if window.size == (1, 1):
        return True
    return window.bundle_id.casefold() == _CONTROL_CENTER_BUNDLE_ID


def _print_windows() -> None:
    windows = list_windows()

    table = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    table.add_column("ID", min_width=5, justify="right", style="cyan", no_wrap=True)
    table.add_column("PID", min_width=5, justify="right", no_wrap=True)
    table.add_column("Size", min_width=9, justify="right", no_wrap=True)
    table.add_column("Application", min_width=8, max_width=20, overflow="ellipsis")
    table.add_column("Title", min_width=8, overflow="ellipsis")

    for window in windows:
        if _is_hidden_window(window):
            continue
        size = "?x?" if window.size is None else f"{window.size[0]}x{window.size[1]}"
        table.add_row(
            str(window.id),
            str(window.pid or "-"),
            size,
            Text(window.app_name, no_wrap=True, overflow="ellipsis"),
            Text(window.title, no_wrap=True, overflow="ellipsis"),
        )

    Console(file=sys.stdout, highlight=False).print(table)


def _select_window(parser: argparse.ArgumentParser, args: argparse.Namespace) -> WindowInfo:
    if args.window_id is not None:
        return get_window(args.window_id)
    if args.title is not None:
        return find_window(title=args.title, exact=args.exact_title)
    parser.error("select one window with --window-id or --title")


def _configs(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> tuple[CaptureOptions, ChangeDetectionOptions, QueueOptions]:
    if args.fps <= 0:
        parser.error("--fps must be greater than zero")
    if not 0.0 <= args.threshold <= 1.0:
        parser.error("--threshold must be between 0 and 1")
    if args.debounce_ms is not None and args.debounce_ms <= 0:
        parser.error("--debounce-ms must be greater than zero")
    if args.max_queue is not None and args.max_queue <= 0:
        parser.error("--max-queue must be greater than zero")
    return (
        CaptureOptions(
            fps=args.fps,
            show_cursor=args.cursor,
            show_highlight=args.highlight,
            crop=args.crop,
            output_resolution=OutputResolution(args.output_resolution),
        ),
        ChangeDetectionOptions(
            threshold=args.threshold,
            comparison_size=args.compare_size,
            debounce_seconds=None if args.debounce_ms is None else args.debounce_ms / 1000.0,
        ),
        QueueOptions(
            max_size=args.max_queue,
            full_policy=QueueFullPolicy(args.queue_full),
            clear_on_window_close=args.clear_queue_on_window_close,
        ),
    )


def _watch_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="windowpulse-watch",
        description="Emit changed or newly stable frames from one window to stdout.",
    )
    _add_common_options(parser)
    parser.add_argument(
        "--format", choices=("jsonl", "png-stream"), default="jsonl", help="output framing"
    )
    return parser


def watch_main(argv: Sequence[str] | None = None) -> int:
    parser = _watch_parser()
    args = parser.parse_args(argv)
    if args.list_windows:
        _print_windows()
        return 0
    try:
        window = _select_window(parser, args)
        capture, changes, queue = _configs(parser, args)
        recorder = WindowRecorder(
            window,
            capture=capture,
            change_detection=changes,
            queue_options=queue,
        )
        recorder.start()
        try:
            for frame in recorder:
                payload = io.BytesIO()
                frame.image.save(payload, format="PNG")
                png = payload.getvalue()
                if args.format == "png-stream":
                    sys.stdout.buffer.write(len(png).to_bytes(8, "big"))
                    sys.stdout.buffer.write(png)
                    sys.stdout.buffer.flush()
                else:
                    event = {
                        "timestamp": frame.timestamp,
                        "width": frame.image.width,
                        "height": frame.image.height,
                        "mode": frame.image.mode,
                        "encoding": "png",
                        "png_base64": base64.b64encode(png).decode("ascii"),
                    }
                    print(json.dumps(event, separators=(",", ":")), flush=True)
        except (BrokenPipeError, KeyboardInterrupt):
            pass
        finally:
            recorder.stop(drain_handlers=False)
        return 0
    except (WindowPulseError, OSError, ValueError) as error:
        print(f"windowpulse-watch: {error}", file=sys.stderr)
        return 1


def _video_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="windowpulse-video",
        description="Record one window to a video file without audio.",
    )
    parser.add_argument("output", nargs="?", help="output video path")
    _add_common_options(parser)
    parser.add_argument("--duration", type=_positive_float, help="recording length in seconds")
    parser.add_argument("--video-fps", type=_positive_float, help="encoded frame rate")
    parser.add_argument("--codec", default="libx264", help="FFmpeg video codec")
    return parser


def video_main(argv: Sequence[str] | None = None) -> int:
    parser = _video_parser()
    args = parser.parse_args(argv)
    if args.list_windows:
        _print_windows()
        return 0
    if args.output is None:
        parser.error("the output path is required unless --list-windows is used")
    try:
        window = _select_window(parser, args)
        capture, changes, queue = _configs(parser, args)
        output = WindowVideoRecorder(
            window,
            args.output,
            capture=capture,
            change_detection=changes,
            queue_options=queue,
            video_fps=args.video_fps,
            codec=args.codec,
        ).record(duration=args.duration)
        print(output, file=sys.stderr)
        return 0
    except (WindowPulseError, OSError, ValueError) as error:
        print(f"windowpulse-video: {error}", file=sys.stderr)
        return 1
