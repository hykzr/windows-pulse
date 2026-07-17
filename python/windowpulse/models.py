"""Public data models and configuration for WindowPulse."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple

from PIL import Image


class OutputResolution(str, Enum):
    """Output resolutions supported by the native stream backend."""

    CAPTURED = "captured"
    P480 = "480p"
    P720 = "720p"
    P1080 = "1080p"
    P1440 = "1440p"
    P2160 = "2160p"
    P4320 = "4320p"


class QueueFullPolicy(str, Enum):
    """How a bounded queue behaves when all capacity is occupied."""

    BLOCK = "block"
    DROP_OLDEST = "drop_oldest"


class RecorderState(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Region:
    """A rectangle. Capture crop coordinates are relative to the window."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (self.x, self.y, self.width, self.height)
        ):
            raise TypeError("region coordinates and dimensions must be integers")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("region width and height must be greater than zero")

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def contains(self, other: Region) -> bool:
        return (
            other.x >= self.x
            and other.y >= self.y
            and other.right <= self.right
            and other.bottom <= self.bottom
        )


@dataclass(frozen=True, slots=True)
class CaptureOptions:
    """Options passed to the system capture backend."""

    fps: int = 30
    show_cursor: bool = False
    show_highlight: bool = False
    crop: Region | None = None
    output_resolution: OutputResolution = OutputResolution.CAPTURED

    def __post_init__(self) -> None:
        if not isinstance(self.fps, int) or isinstance(self.fps, bool) or self.fps <= 0:
            raise ValueError("fps must be a positive integer")
        if self.crop is not None and (self.crop.x < 0 or self.crop.y < 0):
            raise ValueError("capture crop x and y must be non-negative window coordinates")
        if not isinstance(self.output_resolution, OutputResolution):
            object.__setattr__(self, "output_resolution", OutputResolution(self.output_resolution))


@dataclass(frozen=True, slots=True)
class ChangeDetectionOptions:
    """Visual change filtering and optional stable-frame debouncing."""

    threshold: float = 0.0
    comparison_size: tuple[int, int] = (64, 64)
    debounce_seconds: float | None = None
    emit_initial: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.threshold, bool) or not 0.0 <= self.threshold <= 1.0:
            raise ValueError("threshold must be between 0.0 and 1.0")
        if len(self.comparison_size) != 2 or not all(
            isinstance(value, int) and value > 0 for value in self.comparison_size
        ):
            raise ValueError("comparison_size must contain two positive integers")
        if self.debounce_seconds is not None and self.debounce_seconds <= 0:
            raise ValueError("debounce_seconds must be greater than zero or None")


@dataclass(frozen=True, slots=True)
class QueueOptions:
    """Capacity and overflow behavior for the public frame queue."""

    max_size: int | None = None
    full_policy: QueueFullPolicy = QueueFullPolicy.BLOCK
    clear_on_window_close: bool = False

    def __post_init__(self) -> None:
        if self.max_size is not None and (
            not isinstance(self.max_size, int)
            or isinstance(self.max_size, bool)
            or self.max_size <= 0
        ):
            raise ValueError("max_size must be a positive integer or None")
        if not isinstance(self.full_policy, QueueFullPolicy):
            object.__setattr__(self, "full_policy", QueueFullPolicy(self.full_policy))
        if not isinstance(self.clear_on_window_close, bool):
            raise TypeError("clear_on_window_close must be a boolean")


class CapturedFrame(NamedTuple):
    """A queue item: Unix capture time and an owned Pillow image."""

    timestamp: float
    image: Image.Image


@dataclass(frozen=True, slots=True)
class WindowInfo:
    """Metadata for a captureable top-level window."""

    id: int
    title: str
    app_name: str = ""
    pid: int | None = None
    bounds: Region | None = None
    is_minimized: bool | None = None
    is_maximized: bool | None = None
    is_focused: bool | None = None
    bundle_id: str = ""

    @property
    def position(self) -> tuple[int, int] | None:
        if self.bounds is None:
            return None
        return (self.bounds.x, self.bounds.y)

    @property
    def size(self) -> tuple[int, int] | None:
        if self.bounds is None:
            return None
        return (self.bounds.width, self.bounds.height)

    def screen_to_window(self, x: int, y: int) -> tuple[int, int]:
        """Convert screen coordinates to coordinates local to this window."""
        if self.bounds is None:
            raise ValueError("screen position is unavailable for this window")
        return (x - self.bounds.x, y - self.bounds.y)

    def window_to_screen(self, x: int, y: int) -> tuple[int, int]:
        """Convert coordinates local to this window into screen coordinates."""
        if self.bounds is None:
            raise ValueError("screen position is unavailable for this window")
        return (x + self.bounds.x, y + self.bounds.y)

    def refresh(self) -> WindowInfo:
        """Return newly queried metadata for the same native window id."""
        from .windows import get_window

        return get_window(self.id)


@dataclass(frozen=True, slots=True)
class RecorderStats:
    captured: int = 0
    emitted: int = 0
    filtered: int = 0
    dropped: int = 0
    last_difference: float | None = None
