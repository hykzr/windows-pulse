"""Window enumeration, selection, and capture permission helpers."""

from __future__ import annotations

from . import _native
from .errors import AmbiguousWindowError, WindowNotFoundError
from .models import Region, WindowInfo


def list_windows(*, include_untitled: bool = False) -> list[WindowInfo]:
    """Return captureable windows; display/monitor targets are never included."""
    result: list[WindowInfo] = []
    for item in _native.list_windows_native():
        if not include_untitled and not item.title.strip():
            continue
        bounds = None
        x, y, width, height = item.x, item.y, item.width, item.height
        if x is not None and y is not None and width is not None and height is not None:
            if width <= 0 or height <= 0:
                continue
            bounds = Region(x, y, width, height)
        result.append(
            WindowInfo(
                id=item.id,
                pid=item.pid,
                title=item.title,
                app_name=item.app_name,
                bounds=bounds,
                is_minimized=item.is_minimized,
                is_maximized=item.is_maximized,
                is_focused=item.is_focused,
            )
        )
    return result


def get_window(window_id: int) -> WindowInfo:
    """Return metadata for one native window id."""
    matches = [window for window in list_windows(include_untitled=True) if window.id == window_id]
    if not matches:
        raise WindowNotFoundError(f"window {window_id} was not found")
    return matches[0]


def find_windows(
    *,
    title: str | None = None,
    app_name: str | None = None,
    pid: int | None = None,
    exact: bool = False,
    include_untitled: bool = False,
) -> list[WindowInfo]:
    """Filter windows by title, application name, and/or process id."""

    def text_matches(actual: str, expected: str | None) -> bool:
        if expected is None:
            return True
        actual_folded = actual.casefold()
        expected_folded = expected.casefold()
        return actual_folded == expected_folded if exact else expected_folded in actual_folded

    return [
        window
        for window in list_windows(include_untitled=include_untitled)
        if text_matches(window.title, title)
        and text_matches(window.app_name, app_name)
        and (pid is None or window.pid == pid)
    ]


def find_window(
    *,
    title: str | None = None,
    app_name: str | None = None,
    pid: int | None = None,
    exact: bool = False,
    include_untitled: bool = False,
) -> WindowInfo:
    """Return exactly one matching window, rejecting ambiguous selectors."""
    matches = find_windows(
        title=title,
        app_name=app_name,
        pid=pid,
        exact=exact,
        include_untitled=include_untitled,
    )
    selector = f"title={title!r}, app_name={app_name!r}, pid={pid!r}"
    if not matches:
        raise WindowNotFoundError(f"no window matched {selector}")
    if len(matches) > 1:
        choices = ", ".join(f"{window.id}: {window.title!r}" for window in matches[:8])
        suffix = " ..." if len(matches) > 8 else ""
        raise AmbiguousWindowError(f"{len(matches)} windows matched {selector}: {choices}{suffix}")
    return matches[0]


def is_supported() -> bool:
    return _native.is_supported()


def has_permission() -> bool:
    return _native.has_permission()


def request_permission() -> bool:
    """Ask the OS for capture permission where the platform supports a prompt."""
    return _native.request_permission()


def backend_name() -> str:
    return _native.backend_name()
