"""In-process pub/sub bus for cross-page UI sync.

A publisher (model signal handler, process tracker, etc.) calls
``publish(event_type, payload)`` from whichever thread does the mutation.
A subscriber (the SSE view) receives events on a thread-safe ``Queue``
and streams them to the connected browser.

Design notes:

* In-memory only. Reset on app restart — that's fine for a single-user
  desktop app. Once the page reconnects EventSource it'll get fresh
  state via the next change.
* Each subscriber gets its own bounded queue. If a slow consumer
  doesn't drain it the publisher drops events for that subscriber
  rather than blocking other subscribers or the calling thread.
* Subscribe/unsubscribe is guarded by a single lock — publish itself
  iterates a snapshot list so it never holds the lock while putting.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger(__name__)

# Bounded so a stuck consumer can't grow memory unbounded. Most events
# are tiny dicts, so 256 still covers a burst (e.g. bulk import).
_MAX_QUEUE_DEPTH = 256

_lock = threading.Lock()
_subscribers: set[queue.Queue] = set()


def subscribe() -> queue.Queue:
    """Register a new subscriber. Caller is responsible for unsubscribing."""
    q: queue.Queue = queue.Queue(maxsize=_MAX_QUEUE_DEPTH)
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _lock:
        _subscribers.discard(q)


@contextmanager
def subscriber() -> Iterator[queue.Queue]:
    """Context manager wrapper around subscribe/unsubscribe."""
    q = subscribe()
    try:
        yield q
    finally:
        unsubscribe(q)


def publish(event_type: str, payload: dict[str, Any] | None = None) -> None:
    """Push an event to every connected subscriber. Never blocks."""
    event = {
        'type': event_type,
        'ts': time.time(),
        'data': payload or {},
    }
    with _lock:
        snapshot = list(_subscribers)
    for q in snapshot:
        try:
            q.put_nowait(event)
        except queue.Full:
            # Drop oldest to make room — recent state matters more than
            # ancient history for a UI sync feed.
            try:
                q.get_nowait()
                q.put_nowait(event)
            except queue.Empty:
                pass
            except queue.Full:
                log.warning('event_bus: subscriber queue still full after evict; dropping')


def subscriber_count() -> int:
    with _lock:
        return len(_subscribers)
