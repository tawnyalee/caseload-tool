"""Action Queue — data model.

The queue holds BATCH actions the user has reviewed but not yet run. Review
happens at ADD time (the reviewed/edited payload is stored on the item); the
actual send/save happens only when the queue is run. See the QueuePanel in
scripts/launcher.py for the UI and the coordinator that drives execution.

This module is intentionally pure (no Tk / no browser) so it stays testable:
it's just an ordered, de-duplicated list of QueueItem records plus small
queries the UI and coordinator use.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator, Optional


class QueueStatus(str, Enum):
    """Lifecycle of a queued action.

    PENDING  reviewed and waiting (the resting state; also where an item
             returns after a Cancel so it can be re-run).
    RUNNING  currently executing (frozen — cannot be edited/removed).
    DONE     finished, no errors (green check).
    ERROR    finished but something failed, e.g. a note didn't save (red mark);
             `error_detail` says what.
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"

    @property
    def is_terminal(self) -> bool:
        return self in (QueueStatus.DONE, QueueStatus.ERROR)

    @property
    def can_remove(self) -> bool:
        """Any row except the one currently executing can be removed."""
        return self is not QueueStatus.RUNNING

    @property
    def can_check(self) -> bool:
        """Rows the user can (un)check: not-yet-run (PENDING) or failed (ERROR,
        so it can be retried). RUNNING is locked; DONE can't be re-sent."""
        return self in (QueueStatus.PENDING, QueueStatus.ERROR)

    @property
    def is_runnable(self) -> bool:
        """Eligible to run (or retry) when checked — PENDING or ERROR."""
        return self in (QueueStatus.PENDING, QueueStatus.ERROR)


@dataclass
class QueueItem:
    """One queued action.

    action_name is the unique key (scenarios are unique by name, and the queue
    forbids duplicates). `payload` holds the reviewed/edited data captured at
    add time (populated in stage 2); `results` holds per-item outcome after a
    run (stage 3).
    """

    action_name: str
    display_name: str
    color: Optional[str] = None            # group color hex, or None if ungrouped
    checked: bool = True                   # will run on Start when True
    status: QueueStatus = QueueStatus.PENDING
    payload: Any = None                    # reviewed payload (stage 2)
    results: Any = None                    # execution outcome (stage 3)
    error_detail: str = ""                 # what failed, for ERROR rows (stage 5)


class ActionQueue:
    """Ordered, de-duplicated (by action_name) list of QueueItem."""

    def __init__(self) -> None:
        self._items: list[QueueItem] = []

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[QueueItem]:
        return iter(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    @property
    def items(self) -> list[QueueItem]:
        """A shallow copy of the items in order (safe to iterate while editing)."""
        return list(self._items)

    def has(self, action_name: str) -> bool:
        return any(it.action_name == action_name for it in self._items)

    def get(self, action_name: str) -> Optional[QueueItem]:
        for it in self._items:
            if it.action_name == action_name:
                return it
        return None

    def add(self, item: QueueItem) -> bool:
        """Append an item. Returns False (no-op) if that action is already
        queued — the queue never holds duplicates of the same action."""
        if self.has(item.action_name):
            return False
        self._items.append(item)
        return True

    def remove(self, action_name: str) -> Optional[QueueItem]:
        """Remove and return the item, or None if not present."""
        for i, it in enumerate(self._items):
            if it.action_name == action_name:
                return self._items.pop(i)
        return None

    def clear(self) -> list[QueueItem]:
        """Empty the queue, returning what was removed."""
        gone, self._items = self._items, []
        return gone

    def checked_items(self) -> list[QueueItem]:
        """Items the user has checked (the run set), in order."""
        return [it for it in self._items if it.checked]

    def pending_items(self) -> list[QueueItem]:
        return [it for it in self._items if it.status == QueueStatus.PENDING]
