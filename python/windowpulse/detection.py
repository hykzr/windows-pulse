"""Visual difference and stable-frame debounce state machine."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from PIL import Image, ImageChops

from .models import CapturedFrame, ChangeDetectionOptions


@dataclass(slots=True)
class DetectionResult:
    frame: CapturedFrame | None
    difference: float


class FrameChangeDetector:
    """Turn sampled images into changed or newly stable frame events."""

    def __init__(self, options: ChangeDetectionOptions) -> None:
        self.options = options
        self._emitted_probe: Image.Image | bytes | None = None
        self._previous_probe: Image.Image | bytes | None = None
        self._pending: CapturedFrame | None = None
        self._pending_probe: Image.Image | bytes | None = None
        self._last_visual_change: float | None = None
        self._pending_difference = 0.0

    def _probe(self, image: Image.Image) -> Image.Image | bytes:
        rgb = image.convert("RGB")
        if self.options.threshold == 0.0:
            digest = hashlib.blake2b(digest_size=16)
            digest.update(rgb.width.to_bytes(4, "little"))
            digest.update(rgb.height.to_bytes(4, "little"))
            digest.update(rgb.tobytes())
            return digest.digest()
        return rgb.resize(self.options.comparison_size, Image.Resampling.BOX)

    @staticmethod
    def _difference(left: Image.Image | bytes, right: Image.Image | bytes) -> float:
        if isinstance(left, bytes) and isinstance(right, bytes):
            return 0.0 if left == right else 1.0
        if isinstance(left, bytes) or isinstance(right, bytes):
            return 1.0
        if left.size != right.size or left.mode != right.mode:
            return 1.0
        histogram = ImageChops.difference(left, right).histogram()
        channels = len(left.getbands())
        pixels = left.width * left.height * channels
        return sum((index % 256) * count for index, count in enumerate(histogram)) / (
            255.0 * pixels
        )

    def observe(
        self,
        timestamp: float,
        image: Image.Image,
        monotonic_time: float,
    ) -> DetectionResult:
        probe = self._probe(image)
        packet = CapturedFrame(timestamp, image)

        emitted_probe = self._emitted_probe
        if emitted_probe is None:
            self._emitted_probe = probe
            self._previous_probe = probe
            if self.options.emit_initial:
                return DetectionResult(packet, 1.0)
            return DetectionResult(None, 0.0)

        previous_probe = self._previous_probe
        assert previous_probe is not None
        difference = self._difference(emitted_probe, probe)
        step_difference = self._difference(previous_probe, probe)
        self._previous_probe = probe

        if difference <= self.options.threshold:
            self._pending = None
            self._pending_probe = None
            self._last_visual_change = None
            self._pending_difference = 0.0
            return DetectionResult(None, difference)

        if self.options.debounce_seconds is None:
            self._emitted_probe = probe
            return DetectionResult(packet, difference)

        if self._pending is None or step_difference > self.options.threshold:
            self._last_visual_change = monotonic_time
        self._pending = packet
        self._pending_probe = probe
        self._pending_difference = difference

        assert self._last_visual_change is not None
        stable_for = monotonic_time - self._last_visual_change
        if stable_for < self.options.debounce_seconds:
            return DetectionResult(None, difference)

        return self._emit_pending(difference)

    def poll(self, monotonic_time: float) -> DetectionResult:
        """Emit a due debounced candidate even when the backend has gone quiet."""
        debounce = self.options.debounce_seconds
        if (
            debounce is None
            or self._pending is None
            or self._last_visual_change is None
            or monotonic_time - self._last_visual_change < debounce
        ):
            return DetectionResult(None, self._pending_difference)
        return self._emit_pending(self._pending_difference)

    def _emit_pending(self, difference: float) -> DetectionResult:
        emitted = self._pending
        assert emitted is not None
        assert self._pending_probe is not None
        self._emitted_probe = self._pending_probe
        self._pending = None
        self._pending_probe = None
        self._last_visual_change = None
        self._pending_difference = 0.0
        return DetectionResult(emitted, difference)
