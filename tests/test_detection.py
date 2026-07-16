from __future__ import annotations

import pytest
from PIL import Image
from windowpulse.detection import FrameChangeDetector
from windowpulse.models import ChangeDetectionOptions


def solid(value: int, *, size: tuple[int, int] = (2, 2)) -> Image.Image:
    return Image.new("RGBA", size, (value, value, value, 255))


def test_exact_mode_filters_identical_frames_and_detects_one_pixel_change() -> None:
    detector = FrameChangeDetector(ChangeDetectionOptions(threshold=0.0))
    initial = solid(0)

    first = detector.observe(1.0, initial, 10.0)
    assert first.frame is not None
    assert first.frame.timestamp == 1.0
    assert first.frame.image is initial
    assert first.difference == 1.0

    unchanged = detector.observe(2.0, initial.copy(), 11.0)
    assert unchanged.frame is None
    assert unchanged.difference == 0.0

    changed_image = initial.copy()
    changed_image.putpixel((1, 1), (1, 0, 0, 255))
    changed = detector.observe(3.0, changed_image, 12.0)
    assert changed.frame is not None
    assert changed.frame.image is changed_image
    assert changed.difference == 1.0


def test_alpha_only_change_is_not_a_visual_change() -> None:
    detector = FrameChangeDetector(ChangeDetectionOptions(threshold=0.0))
    opaque = Image.new("RGBA", (1, 1), (10, 20, 30, 255))
    transparent = Image.new("RGBA", (1, 1), (10, 20, 30, 0))

    detector.observe(1.0, opaque, 1.0)
    changed_alpha = detector.observe(2.0, transparent, 2.0)
    assert changed_alpha.frame is None
    assert changed_alpha.difference == 0.0


def test_visual_threshold_is_measured_against_last_emitted_frame() -> None:
    detector = FrameChangeDetector(ChangeDetectionOptions(threshold=0.05, comparison_size=(1, 1)))
    detector.observe(1.0, solid(0), 1.0)

    below = detector.observe(2.0, solid(10), 2.0)
    assert below.frame is None
    assert below.difference == pytest.approx(10 / 255)

    # This is compared with the last emitted black frame, not the suppressed gray-10 frame.
    cumulative = detector.observe(3.0, solid(20), 3.0)
    assert cumulative.frame is not None
    assert cumulative.difference == pytest.approx(20 / 255)


def test_debounce_poll_emits_latest_candidate_after_quiet_period() -> None:
    detector = FrameChangeDetector(
        ChangeDetectionOptions(
            threshold=0.0,
            debounce_seconds=0.2,
        )
    )
    detector.observe(1.0, solid(0), 10.0)
    assert detector.observe(2.0, solid(100), 10.0).frame is None
    assert detector.observe(3.0, solid(200), 10.1).frame is None

    assert detector.poll(10.299).frame is None
    stable = detector.poll(10.301)
    assert stable.frame is not None
    assert stable.frame.timestamp == 3.0
    assert stable.frame.image.getpixel((0, 0)) == (200, 200, 200, 255)

    # A pending candidate is emitted once, not once per subsequent poll.
    assert detector.poll(11.0).frame is None


def test_new_change_at_debounce_deadline_resets_quiet_period() -> None:
    detector = FrameChangeDetector(ChangeDetectionOptions(threshold=0.0, debounce_seconds=0.2))
    detector.observe(1.0, solid(0), 10.0)
    detector.observe(2.0, solid(100), 10.0)
    assert detector.poll(10.199).frame is None

    assert detector.observe(3.0, solid(200), 10.2).frame is None
    assert detector.poll(10.399).frame is None
    stable = detector.poll(10.401)
    assert stable.frame is not None
    assert stable.frame.timestamp == 3.0


def test_return_to_last_emitted_image_cancels_pending_debounce() -> None:
    detector = FrameChangeDetector(ChangeDetectionOptions(threshold=0.0, debounce_seconds=0.1))
    baseline = solid(0)
    detector.observe(1.0, baseline, 1.0)
    detector.observe(2.0, solid(255), 1.01)
    returned = detector.observe(3.0, baseline.copy(), 1.02)

    assert returned.frame is None
    assert returned.difference == 0.0
    assert detector.poll(2.0).frame is None


def test_poll_without_pending_debounce_is_a_noop() -> None:
    detector = FrameChangeDetector(ChangeDetectionOptions(debounce_seconds=None))
    assert detector.poll(1.0).frame is None
    detector.observe(1.0, solid(0), 1.0)
    assert detector.poll(2.0).frame is None


def test_emit_initial_false_still_uses_first_frame_as_baseline() -> None:
    detector = FrameChangeDetector(ChangeDetectionOptions(threshold=0.0, emit_initial=False))
    assert detector.observe(1.0, solid(0), 1.0).frame is None
    assert detector.observe(2.0, solid(0), 2.0).frame is None
    assert detector.observe(3.0, solid(1), 3.0).frame is not None
