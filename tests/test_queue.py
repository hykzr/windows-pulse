from __future__ import annotations

import threading
from queue import Empty, Full

import pytest
from windowpulse.errors import QueueClosedError
from windowpulse.models import QueueFullPolicy
from windowpulse.queue import FrameQueue


def _start_put(
    queue: FrameQueue[str], item: str
) -> tuple[threading.Event, threading.Event, dict[str, object]]:
    started = threading.Event()
    finished = threading.Event()
    outcome: dict[str, object] = {}

    def put() -> None:
        started.set()
        try:
            outcome["evicted"] = queue.put(item)
        except BaseException as error:  # recorded for assertion on the test thread
            outcome["error"] = error
        finally:
            finished.set()

    threading.Thread(target=put, daemon=True).start()
    assert started.wait(1.0)
    return started, finished, outcome


def test_block_capacity_is_released_by_task_done_not_get() -> None:
    queue = FrameQueue[str](max_size=1, full_policy=QueueFullPolicy.BLOCK)
    queue.put("first")

    assert queue.get_nowait() == "first"
    assert queue.qsize() == 0
    assert queue.in_flight() == 1
    assert queue.occupied() == 1

    _, finished, outcome = _start_put(queue, "second")
    assert not finished.wait(0.05), "get() must not free bounded capacity"

    queue.task_done()
    assert finished.wait(1.0)
    assert outcome == {"evicted": None}
    assert queue.get_nowait() == "second"
    queue.task_done()
    assert queue.join(timeout=0.1)


def test_nonblocking_block_policy_raises_full_while_item_is_in_flight() -> None:
    queue = FrameQueue[str](max_size=1)
    queue.put_nowait("first")
    assert queue.get_nowait() == "first"

    with pytest.raises(Full):
        queue.put_nowait("second")

    queue.task_done()


def test_drop_oldest_evicts_only_oldest_pending_item() -> None:
    queue = FrameQueue[str](max_size=2, full_policy=QueueFullPolicy.DROP_OLDEST)
    queue.put("first")
    queue.put("second")

    assert queue.put("third") == "first"
    assert queue.dropped == 1
    assert queue.qsize() == 2
    assert queue.get_nowait() == "second"
    queue.task_done()
    assert queue.get_nowait() == "third"
    queue.task_done()
    assert queue.join(timeout=0.1)


def test_drop_oldest_waits_when_every_slot_is_already_in_flight() -> None:
    queue = FrameQueue[str](max_size=1, full_policy=QueueFullPolicy.DROP_OLDEST)
    queue.put("in-flight")
    assert queue.get_nowait() == "in-flight"

    _, finished, outcome = _start_put(queue, "next")
    assert not finished.wait(0.05), "in-flight work must never be evicted"
    assert queue.dropped == 0

    queue.task_done()
    assert finished.wait(1.0)
    assert outcome == {"evicted": None}
    assert queue.get_nowait() == "next"
    queue.task_done()


def test_claim_marks_item_done_even_when_consumer_raises() -> None:
    queue = FrameQueue[str](max_size=1)
    queue.put("frame")

    with pytest.raises(RuntimeError, match="handler failed"), queue.claim(timeout=0.1) as item:
        assert item == "frame"
        assert queue.in_flight() == 1
        raise RuntimeError("handler failed")

    assert queue.in_flight() == 0
    assert queue.occupied() == 0
    assert queue.join(timeout=0.1)


def test_close_rejects_puts_but_allows_pending_items_to_drain() -> None:
    queue = FrameQueue[str]()
    queue.put("pending")
    queue.close()

    with pytest.raises(QueueClosedError):
        queue.put("late")

    assert queue.get_nowait() == "pending"
    queue.task_done()
    with pytest.raises(QueueClosedError):
        queue.get_nowait()
    assert queue.join(timeout=0.1)


def test_close_wakes_a_blocked_consumer() -> None:
    queue = FrameQueue[str]()
    started = threading.Event()
    finished = threading.Event()
    outcome: dict[str, object] = {}

    def consume() -> None:
        started.set()
        try:
            queue.get()
        except BaseException as error:
            outcome["error"] = error
        finally:
            finished.set()

    threading.Thread(target=consume, daemon=True).start()
    assert started.wait(1.0)
    assert not finished.wait(0.05)
    queue.close()
    assert finished.wait(1.0)
    assert isinstance(outcome.get("error"), QueueClosedError)


def test_close_wakes_a_producer_blocked_by_in_flight_work() -> None:
    queue = FrameQueue[str](max_size=1)
    queue.put("in-flight")
    assert queue.get_nowait() == "in-flight"

    _, finished, outcome = _start_put(queue, "late")
    assert not finished.wait(0.05)
    queue.close()
    assert finished.wait(1.0)
    assert isinstance(outcome.get("error"), QueueClosedError)
    queue.task_done()


def test_get_timeout_and_excess_task_done_match_queue_conventions() -> None:
    queue = FrameQueue[str]()
    with pytest.raises(Empty):
        queue.get(timeout=0.001)
    with pytest.raises(ValueError, match="too many"):
        queue.task_done()
