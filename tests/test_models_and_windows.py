from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image
from windowpulse import windows
from windowpulse.models import (
    CapturedFrame,
    CaptureOptions,
    ChangeDetectionOptions,
    OutputResolution,
    QueueOptions,
    Region,
    WindowInfo,
)


def test_region_edges_and_containment() -> None:
    outer = Region(-10, -20, 100, 80)
    touching_edges = Region(-10, -20, 100, 80)
    inner = Region(0, 0, 10, 10)

    assert outer.right == 90
    assert outer.bottom == 60
    assert outer.contains(touching_edges)
    assert outer.contains(inner)
    assert not outer.contains(Region(89, 59, 2, 2))


@pytest.mark.parametrize(
    "args, error",
    [
        ((0, 0, 0, 1), ValueError),
        ((0, 0, 1, -1), ValueError),
        ((True, 0, 1, 1), TypeError),
        ((0, 0, 1.5, 1), TypeError),
    ],
)
def test_region_rejects_invalid_coordinates(
    args: tuple[object, object, object, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        Region(*args)  # type: ignore[arg-type]


def test_window_coordinate_helpers_support_negative_desktop_origins() -> None:
    window = WindowInfo(
        id=42,
        title="Deck",
        bounds=Region(-1920, -100, 1280, 720),
    )

    assert window.position == (-1920, -100)
    assert window.size == (1280, 720)
    assert window.screen_to_window(-1900, -90) == (20, 10)
    assert window.window_to_screen(20, 10) == (-1900, -90)


def test_window_coordinate_helpers_fail_when_geometry_is_unavailable() -> None:
    window = WindowInfo(id=42, title="Portal window")
    assert window.position is None
    assert window.size is None
    with pytest.raises(ValueError, match="unavailable"):
        window.screen_to_window(0, 0)
    with pytest.raises(ValueError, match="unavailable"):
        window.window_to_screen(0, 0)


def test_window_refresh_queries_the_same_native_id(monkeypatch: pytest.MonkeyPatch) -> None:
    original = WindowInfo(id=42, title="Old")
    refreshed = WindowInfo(id=42, title="New", bounds=Region(1, 2, 3, 4))
    seen: list[int] = []

    def fake_get_window(window_id: int) -> WindowInfo:
        seen.append(window_id)
        return refreshed

    monkeypatch.setattr(windows, "get_window", fake_get_window)
    assert original.refresh() is refreshed
    assert seen == [42]


def test_list_windows_converts_native_geometry_and_filters_untitled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_windows = [
        SimpleNamespace(
            id=1,
            pid=10,
            title="Deck",
            app_name="Slides",
            bundle_id="com.example.slides",
            x=-20,
            y=30,
            width=800,
            height=600,
            is_minimized=False,
            is_maximized=True,
            is_focused=False,
        ),
        SimpleNamespace(
            id=2,
            pid=None,
            title="   ",
            app_name="",
            bundle_id="",
            x=None,
            y=None,
            width=None,
            height=None,
            is_minimized=None,
            is_maximized=None,
            is_focused=None,
        ),
    ]
    monkeypatch.setattr(windows._native, "list_windows_native", lambda: native_windows)

    assert windows.list_windows() == [
        WindowInfo(
            id=1,
            pid=10,
            title="Deck",
            app_name="Slides",
            bundle_id="com.example.slides",
            bounds=Region(-20, 30, 800, 600),
            is_minimized=False,
            is_maximized=True,
            is_focused=False,
        )
    ]
    assert len(windows.list_windows(include_untitled=True)) == 2


def test_configuration_models_coerce_enums_and_validate_boundaries() -> None:
    assert CaptureOptions(output_resolution="720p").output_resolution is OutputResolution.P720
    with pytest.raises(ValueError, match="fps"):
        CaptureOptions(fps=True)
    with pytest.raises(ValueError, match="non-negative"):
        CaptureOptions(crop=Region(-1, 0, 1, 1))
    with pytest.raises(ValueError, match="threshold"):
        ChangeDetectionOptions(threshold=float("nan"))
    with pytest.raises(ValueError, match="max_size"):
        QueueOptions(max_size=0)
    with pytest.raises(TypeError, match="clear_on_window_close"):
        QueueOptions(clear_on_window_close=1)  # type: ignore[arg-type]


def test_captured_frame_is_an_unpackable_timestamp_and_pillow_image() -> None:
    image = Image.new("RGB", (1, 1), "red")
    timestamp, queued_image = CapturedFrame(123.5, image)
    assert timestamp == 123.5
    assert queued_image is image
