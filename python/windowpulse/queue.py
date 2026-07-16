"""A task-aware bounded queue with safe oldest-pending eviction."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from queue import Empty, Full
from threading import Condition
from typing import Generic, TypeVar

from .errors import QueueClosedError
from .models import QueueFullPolicy

T = TypeVar("T")


class FrameQueue(Generic[T]):
    """Queue whose capacity includes both pending and currently processed items.

    Unlike ``queue.Queue``, a slot is released by :meth:`task_done`, not by
    :meth:`get`. This makes ``BLOCK`` mean "wait for processing to finish" and
    lets ``DROP_OLDEST`` guarantee it only evicts work nobody has started.
    """

    def __init__(
        self,
        max_size: int | None = None,
        full_policy: QueueFullPolicy = QueueFullPolicy.BLOCK,
    ) -> None:
        if max_size is not None and max_size <= 0:
            raise ValueError("max_size must be positive or None")
        self.max_size = max_size
        self.full_policy = QueueFullPolicy(full_policy)
        self._pending: deque[T] = deque()
        self._in_flight = 0
        self._unfinished = 0
        self._dropped = 0
        self._closed = False
        self._condition = Condition()

    def _occupied(self) -> int:
        return len(self._pending) + self._in_flight

    def put(self, item: T, block: bool = True, timeout: float | None = None) -> T | None:
        """Add an item, returning an evicted pending item when applicable."""
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self.max_size is not None and self._occupied() >= self.max_size:
                if self._closed:
                    raise QueueClosedError("frame queue is closed")
                if self.full_policy is QueueFullPolicy.DROP_OLDEST and self._pending:
                    evicted = self._pending.popleft()
                    self._unfinished -= 1
                    self._dropped += 1
                    self._pending.append(item)
                    self._unfinished += 1
                    self._condition.notify_all()
                    return evicted
                if not block:
                    raise Full
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise Full
                self._condition.wait(remaining)
            if self._closed:
                raise QueueClosedError("frame queue is closed")
            self._pending.append(item)
            self._unfinished += 1
            self._condition.notify_all()
            return None

    def put_nowait(self, item: T) -> T | None:
        return self.put(item, block=False)

    def get(self, block: bool = True, timeout: float | None = None) -> T:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative")
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while not self._pending:
                if self._closed:
                    raise QueueClosedError("frame queue is closed and empty")
                if not block:
                    raise Empty
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise Empty
                self._condition.wait(remaining)
            item = self._pending.popleft()
            self._in_flight += 1
            return item

    def get_nowait(self) -> T:
        return self.get(block=False)

    @contextmanager
    def claim(self, block: bool = True, timeout: float | None = None) -> Iterator[T]:
        """Get one item and always acknowledge it when the context exits."""
        item = self.get(block=block, timeout=timeout)
        try:
            yield item
        finally:
            self.task_done()

    def task_done(self) -> None:
        with self._condition:
            if self._in_flight <= 0:
                raise ValueError("task_done() called too many times")
            self._in_flight -= 1
            self._unfinished -= 1
            self._condition.notify_all()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for all queued work; return ``False`` if a timeout expires."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._unfinished:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self) -> None:
        """Prevent new puts while allowing pending work to be drained."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def discard_pending(self) -> list[T]:
        with self._condition:
            discarded = list(self._pending)
            self._pending.clear()
            self._unfinished -= len(discarded)
            self._dropped += len(discarded)
            self._condition.notify_all()
            return discarded

    def qsize(self) -> int:
        with self._condition:
            return len(self._pending)

    def occupied(self) -> int:
        with self._condition:
            return self._occupied()

    def in_flight(self) -> int:
        with self._condition:
            return self._in_flight

    @property
    def pending(self) -> int:
        return self.qsize()

    @property
    def outstanding(self) -> int:
        return self.occupied()

    def empty(self) -> bool:
        return self.qsize() == 0

    def full(self) -> bool:
        with self._condition:
            return self.max_size is not None and self._occupied() >= self.max_size

    @property
    def dropped(self) -> int:
        with self._condition:
            return self._dropped

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed
