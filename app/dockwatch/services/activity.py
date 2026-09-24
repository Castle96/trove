"""In-memory activity (audit) log.

Records actions performed through the dashboard so users can see recent
activity, similar to Arcane's "Activity & Events" feed. Not persisted —
kept intentionally simple; swap for a DB-backed table when you outgrow it.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

MAX_EVENTS = 200


class ActivityLog:
    """Thread-safe, bounded log of recent events."""

    def __init__(self, maxlen: int = MAX_EVENTS) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def record(
        self,
        actor_type: str,
        action: str,
        target: str,
        detail: str | None = None,
        endpoint_id: int | None = None,
        endpoint_name: str | None = None,
    ) -> None:
        """Append an event. ``actor_type`` is e.g. ``container`` or ``device``."""
        event = {
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "actor_type": actor_type,
            "action": action,
            "target": target,
            "detail": detail,
            "endpoint_id": endpoint_id,
            "endpoint_name": endpoint_name,
        }
        with self._lock:
            self._events.appendleft(event)

    def as_list(self) -> list[dict[str, Any]]:
        """Return all events, newest first."""
        with self._lock:
            return list(self._events)


activity_log = ActivityLog()
