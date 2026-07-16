"""Concurrent Python handler workers for recorder queues."""

from __future__ import annotations

import threading
from collections.abc import Callable
from queue import Empty, Queue
from typing import TYPE_CHECKING

from .errors import HandlerError, QueueClosedError
from .models import CapturedFrame

if TYPE_CHECKING:
    from .recorder import WindowRecorder


class HandlerPool:
    """Consume a recorder's queue with one or more Python handler threads."""

    def __init__(
        self,
        recorder: WindowRecorder,
        handler: Callable[[CapturedFrame], object],
        workers: int = 1,
    ) -> None:
        if workers <= 0:
            raise ValueError("workers must be greater than zero")
        self.recorder = recorder
        self.handler = handler
        self._stop_event = threading.Event()
        self._errors: Queue[BaseException] = Queue()
        self._threads = [
            threading.Thread(
                target=self._run,
                name=f"windowpulse-handler-{index}",
                daemon=True,
            )
            for index in range(workers)
        ]
        self._started = False
        self.start()

    def start(self) -> HandlerPool:
        """Start the consumer threads and return this pool."""
        if self._started:
            return self
        if self._stop_event.is_set():
            raise HandlerError("a stopped handler pool cannot be restarted")
        for thread in self._threads:
            thread.start()
        self._started = True
        return self

    def _run(self) -> None:
        queue = self.recorder.queue
        while not self._stop_event.is_set():
            try:
                packet = queue.get(timeout=0.1)
            except Empty:
                continue
            except QueueClosedError:
                return
            try:
                self.handler(packet)
            except BaseException as error:  # surfaced on the controlling thread
                self._errors.put(error)
                self._stop_event.set()
                recorder_stop = getattr(self.recorder, "_stop_event", None)
                if recorder_stop is not None:
                    recorder_stop.set()
                queue.discard_pending()
            finally:
                queue.task_done()

    def stop(self, *, drain: bool = True, timeout: float | None = None) -> None:
        if drain:
            self.recorder.queue.join(timeout=timeout)
        self._stop_event.set()
        if not drain:
            self.recorder.queue.discard_pending()
        for thread in self._threads:
            thread.join(timeout)
        self.raise_if_failed()

    def raise_if_failed(self) -> None:
        if not self._errors.empty():
            error = self._errors.get_nowait()
            raise HandlerError("a frame handler failed") from error

    @property
    def is_alive(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)

    def __enter__(self) -> HandlerPool:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.stop(drain=exc is None)
