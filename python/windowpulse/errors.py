"""WindowPulse exception hierarchy."""

from __future__ import annotations


class WindowPulseError(Exception):
    """Base class for all public WindowPulse errors."""


class UnsupportedPlatformError(WindowPulseError):
    """The current operating system or capture API is unsupported."""


class CapturePermissionError(WindowPulseError):
    """Window recording permission has not been granted."""


class WindowNotFoundError(WindowPulseError):
    """No window matched the requested selector."""


class AmbiguousWindowError(WindowPulseError):
    """More than one window matched a selector that requires one result."""


class RecorderStateError(WindowPulseError):
    """An operation is invalid for the recorder's current state."""


class RecorderTimeoutError(WindowPulseError):
    """A recorder worker did not stop before its timeout."""


class QueueClosedError(WindowPulseError):
    """A frame queue has been closed and contains no more pending items."""


class HandlerError(WindowPulseError):
    """A Python frame handler failed."""


class VideoDependencyError(WindowPulseError, ImportError):
    """The optional video encoder dependency is not installed."""
