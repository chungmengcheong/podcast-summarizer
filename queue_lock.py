"""Single-writer locking for the atomically replaced episode queue.

``queue.json`` is replaced on every durable update, so locking that file would
lock an old inode after the first write. This module locks a stable sibling
sidecar file for the lifetime of a command invocation instead.
"""

from __future__ import annotations

import fcntl
from pathlib import Path
from types import TracebackType
from typing import Self


class QueueLockUnavailable(RuntimeError):
    """Raised when another process already owns the queue's writer lock."""


def lock_path_for(queue_path: Path) -> Path:
    """Return the stable sidecar path used to serialize queue writers."""
    return queue_path.with_name(f".{queue_path.name}.lock")


class QueueLock:
    """A non-blocking, process-scoped exclusive lock for one queue file."""

    def __init__(self, queue_path: Path):
        self.queue_path = queue_path
        self.path = lock_path_for(queue_path)
        self._handle = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._handle.close()
            self._handle = None
            raise QueueLockUnavailable(
                f"Another podcast summarizer run is already using {self.queue_path.name}."
            ) from error
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
