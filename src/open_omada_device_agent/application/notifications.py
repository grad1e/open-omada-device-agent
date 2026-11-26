"""Device-initiated NOTIFY messages queued by background work for the session loop."""
from __future__ import annotations

from dataclasses import dataclass
import queue
from typing import Any

# Controller 6.3.0.111 NotifySubjectEnum values (``sub`` in a NOTIFY body).
NOTIFY_SUBJECT_TROUBLESHOOTING = 5


@dataclass(frozen=True)
class OutboundNotification:
    subject: int
    content: dict[str, Any]


class OutboundNotifications:
    """Thread-safe FIFO; background threads put, the managed session drains."""

    def __init__(self, max_pending: int = 1000) -> None:
        self._queue: queue.Queue[OutboundNotification] = queue.Queue(maxsize=max_pending)

    def put(self, subject: int, content: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(OutboundNotification(subject, content))
        except queue.Full:
            pass  # No session is draining; drop rather than grow without bound.

    def drain(self) -> list[OutboundNotification]:
        items = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                return items

    def clear(self) -> None:
        self.drain()
