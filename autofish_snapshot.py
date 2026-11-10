"""Thread-safe latest-snapshot holder for the autofish runtime.

The runtime publishes plain dicts; readers (the IPC status handler, UI, tests)
always receive an independent deep copy.  This module has no game, UI or I/O
dependencies.
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Dict, Optional


class SnapshotStore:
    """Keep the latest snapshot with atomic publish and copied reads."""

    def __init__(self, initial: Optional[Dict[str, Any]] = None) -> None:
        if initial is not None and not isinstance(initial, dict):
            raise TypeError("snapshot must be a dict")
        self._lock = threading.RLock()
        self._snapshot: Dict[str, Any] = (
            copy.deepcopy(initial) if initial is not None else {}
        )

    def publish(self, snapshot_dict: Dict[str, Any]) -> None:
        """Replace the current snapshot.  The caller's dict is copied."""
        if not isinstance(snapshot_dict, dict):
            raise TypeError("snapshot must be a dict")
        value = copy.deepcopy(snapshot_dict)
        with self._lock:
            self._snapshot = value

    def snapshot(self) -> Dict[str, Any]:
        """Return a deep copy of the latest snapshot."""
        with self._lock:
            return copy.deepcopy(self._snapshot)

    # Convenient read aliases for callers that prefer these names.
    def read(self) -> Dict[str, Any]:
        return self.snapshot()

    def get(self) -> Dict[str, Any]:
        return self.snapshot()

    def clear(self) -> None:
        self.publish({})

    def __len__(self) -> int:
        with self._lock:
            return len(self._snapshot)


# Common alternative names; all refer to the same implementation.
SnapshotPublisher = SnapshotStore
SnapshotHolder = SnapshotStore

__all__ = ["SnapshotStore", "SnapshotPublisher", "SnapshotHolder"]
