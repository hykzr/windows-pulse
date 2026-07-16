"""WindowPulse: fast, change-aware capture of one background window."""

from .errors import (
    AmbiguousWindowError,
    CapturePermissionError,
    HandlerError,
    QueueClosedError,
    RecorderStateError,
    RecorderTimeoutError,
    UnsupportedPlatformError,
    VideoDependencyError,
    WindowNotFoundError,
    WindowPulseError,
)
from .models import (
    CapturedFrame,
    CaptureOptions,
    ChangeDetectionOptions,
    OutputResolution,
    QueueFullPolicy,
    QueueOptions,
    RecorderState,
    RecorderStats,
    Region,
    WindowInfo,
)
from .queue import FrameQueue
from .recorder import WindowRecorder
from .video import WindowVideoRecorder
from .windows import (
    backend_name,
    find_window,
    find_windows,
    get_window,
    has_permission,
    is_supported,
    list_windows,
    request_permission,
)

__all__ = [
    "AmbiguousWindowError",
    "CaptureOptions",
    "CapturePermissionError",
    "CapturedFrame",
    "ChangeDetectionOptions",
    "FrameQueue",
    "HandlerError",
    "OutputResolution",
    "QueueClosedError",
    "QueueFullPolicy",
    "QueueOptions",
    "RecorderState",
    "RecorderStateError",
    "RecorderStats",
    "RecorderTimeoutError",
    "Region",
    "UnsupportedPlatformError",
    "VideoDependencyError",
    "WindowInfo",
    "WindowNotFoundError",
    "WindowPulseError",
    "WindowRecorder",
    "WindowVideoRecorder",
    "backend_name",
    "find_window",
    "find_windows",
    "get_window",
    "has_permission",
    "is_supported",
    "list_windows",
    "request_permission",
]

__version__ = "0.1.0"
