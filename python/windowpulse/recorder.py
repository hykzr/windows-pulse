"""High-level change-aware window recorder."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from queue import Empty, Full

from PIL import Image

from . import _native
from .detection import FrameChangeDetector
from .errors import (
    CapturePermissionError,
    QueueClosedError,
    RecorderStateError,
    RecorderTimeoutError,
    UnsupportedPlatformError,
)
from .models import (
    CapturedFrame,
    CaptureOptions,
    ChangeDetectionOptions,
    QueueOptions,
    RecorderState,
    RecorderStats,
    WindowInfo,
)
from .processing import HandlerPool
from .queue import FrameQueue
from .windows import get_window, has_permission, is_supported


class WindowRecorder:
    """Capture one window on a background worker and enqueue visual changes."""

    def __init__(
        self,
        window: WindowInfo | int,
        *,
        capture: CaptureOptions | None = None,
        change_detection: ChangeDetectionOptions | None = None,
        queue_options: QueueOptions | None = None,
    ) -> None:
        self.window = get_window(window) if isinstance(window, int) else window
        self.capture_options = capture or CaptureOptions()
        self.change_detection_options = change_detection or ChangeDetectionOptions()
        self.queue_options = queue_options or QueueOptions()
        self._validate_crop()

        self.queue: FrameQueue[CapturedFrame] = FrameQueue(
            self.queue_options.max_size,
            self.queue_options.full_policy,
        )
        self._state = RecorderState.CREATED
        self._state_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._captured = 0
        self._emitted = 0
        self._filtered = 0
        self._last_difference: float | None = None
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._error: BaseException | None = None
        self._backend: _native.NativeCapturer | None = None
        self._handler_pools: list[HandlerPool] = []

    def _validate_crop(self) -> None:
        crop = self.capture_options.crop
        bounds = self.window.bounds
        if crop is None or bounds is None:
            return
        local_bounds = type(bounds)(0, 0, bounds.width, bounds.height)
        if not local_bounds.contains(crop):
            raise ValueError(
                f"crop {crop!r} exceeds window's current size {bounds.width}x{bounds.height}"
            )

    @property
    def state(self) -> RecorderState:
        with self._state_lock:
            return self._state

    def _set_state(self, state: RecorderState) -> None:
        with self._state_lock:
            self._state = state

    @property
    def backend(self) -> _native.NativeCapturer | None:
        """The active private native backend, available after :meth:`start`."""
        return self._backend

    @property
    def is_running(self) -> bool:
        return self.state is RecorderState.RUNNING

    @property
    def stats(self) -> RecorderStats:
        with self._stats_lock:
            return RecorderStats(
                captured=self._captured,
                emitted=self._emitted,
                filtered=self._filtered,
                dropped=self.queue.dropped,
                last_difference=self._last_difference,
            )

    def start(self, *, timeout: float = 15.0) -> WindowRecorder:
        if not is_supported():
            raise UnsupportedPlatformError("the native window capture API is unsupported")
        if not has_permission():
            raise CapturePermissionError(
                "window capture permission is missing; call request_permission() first"
            )
        with self._state_lock:
            if self._state is not RecorderState.CREATED:
                raise RecorderStateError(f"cannot start recorder in state {self._state.value}")
            self._state = RecorderState.STARTING
        self._worker = threading.Thread(
            target=self._run,
            name=f"windowpulse-capture-{self.window.id}",
            daemon=True,
        )
        self._worker.start()
        if not self._ready_event.wait(timeout):
            self._stop_event.set()
            raise RecorderTimeoutError("timed out while starting the native capture backend")
        self.raise_if_failed()
        return self

    def _run(self) -> None:
        detector = FrameChangeDetector(self.change_detection_options)
        crop = self.capture_options.crop
        crop_tuple = None if crop is None else (crop.x, crop.y, crop.width, crop.height)
        try:
            self._backend = _native.NativeCapturer(
                self.window.id,
                self.capture_options.fps,
                self.capture_options.show_cursor,
                self.capture_options.show_highlight,
                crop_tuple,
                self.capture_options.output_resolution.value,
            )
            self._set_state(RecorderState.RUNNING)
            self._ready_event.set()
            while not self._stop_event.is_set():
                native_frame = self._backend.next_frame()
                if native_frame is None:
                    result = detector.poll(time.monotonic())
                    if result.frame is not None:
                        self._enqueue(result.frame)
                    continue
                timestamp, width, height, pixel_format, data = native_frame
                image = Image.frombytes("RGBA", (width, height), data, "raw", pixel_format)
                result = detector.observe(timestamp, image, time.monotonic())
                with self._stats_lock:
                    self._captured += 1
                    self._last_difference = result.difference
                    if result.frame is None:
                        self._filtered += 1
                if result.frame is not None:
                    self._enqueue(result.frame)
        except BaseException as error:
            self._error = error
            self._set_state(RecorderState.FAILED)
        finally:
            self._ready_event.set()
            if self._backend is not None:
                try:
                    self._backend.stop()
                except BaseException as stop_error:
                    if self._error is None:
                        self._error = stop_error
                        self._set_state(RecorderState.FAILED)
                finally:
                    self._backend = None
            if self.state is not RecorderState.FAILED:
                self._set_state(RecorderState.STOPPED)
            self.queue.close()

    def _enqueue(self, packet: CapturedFrame) -> None:
        while not self._stop_event.is_set():
            try:
                self.queue.put(packet, timeout=0.1)
                with self._stats_lock:
                    self._emitted += 1
                return
            except Full:
                continue
            except QueueClosedError:
                return

    def stop(self, *, timeout: float = 5.0, drain_handlers: bool = True) -> None:
        state = self.state
        if state is RecorderState.CREATED:
            self._set_state(RecorderState.STOPPED)
            self.queue.close()
            return
        if state is RecorderState.STOPPED:
            return
        if state is not RecorderState.FAILED:
            self._set_state(RecorderState.STOPPING)
        self._stop_event.set()
        worker = self._worker
        if worker is not None:
            worker.join(timeout)
            if worker.is_alive():
                raise RecorderTimeoutError(
                    "capture worker did not stop; the target may have stopped producing frames"
                )
        for pool in self._handler_pools:
            pool.stop(drain=drain_handlers, timeout=timeout)
        self.raise_if_failed()

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RecorderStateError("window capture worker failed") from self._error

    def get(self, timeout: float | None = None) -> CapturedFrame:
        return self.queue.get(timeout=timeout)

    def task_done(self) -> None:
        self.queue.task_done()

    def start_handler(
        self,
        handler: Callable[[CapturedFrame], object],
        *,
        workers: int = 1,
    ) -> HandlerPool:
        if self.state is RecorderState.CREATED:
            self.start()
        if self.state is not RecorderState.RUNNING:
            raise RecorderStateError(f"cannot add a handler in state {self.state.value}")
        pool = HandlerPool(self, handler, workers)
        self._handler_pools.append(pool)
        return pool

    def __iter__(self) -> Iterator[CapturedFrame]:
        while True:
            try:
                packet = self.get(timeout=0.1)
            except Empty:
                if (
                    self.state in (RecorderState.STOPPED, RecorderState.FAILED)
                    and self.queue.empty()
                ):
                    self.raise_if_failed()
                    return
                continue
            except QueueClosedError:
                self.raise_if_failed()
                return
            try:
                yield packet
            finally:
                self.task_done()

    def __enter__(self) -> WindowRecorder:
        return self.start()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.stop(drain_handlers=exc is None)
