"""Thread-safe control state shared by the autofish loop and its IPC layer.

This module is intentionally free of game, screen, input and network code.  A
worker thread may call :meth:`ControlSession.request_stop` at any moment and the
fishing loop only has to call :meth:`ControlSession.checkpoint` or
:meth:`ControlSession.wait` at safe points.

Public API
----------
ControlSession(session_id=None)
    .request_stop(reason="")
    .request_stop_after_fish()
    .resume()
    .checkpoint()
    .wait(seconds, quantum=0.05)
    .stopped                 (property)
    .stop_after_fish         (property)
    .stop_reason             (property)
    .consume_commands()
    .enqueue_command(command)

AutoStop
    Exception raised by ``checkpoint()`` when a stop has been requested.
"""

from __future__ import annotations

import copy
import secrets
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

COMMAND_QUEUE_MAX = 128


class AutoStop(Exception):
    """Raised at a control checkpoint after ``request_stop``."""

    def __init__(self, reason: str = "") -> None:
        self.reason = reason or "stop requested"
        super().__init__(self.reason)

    @property
    def stop_reason(self) -> str:
        return self.reason


# Some callers prefer an explicit Error suffix; keep both names identical.
AutoStopError = AutoStop


class ControlSession:
    """Cooperative stop/command state for one autofish session.

    All mutable state is guarded by a re-entrant lock.  ``request_stop`` sets the
    stop event *before* returning, so ``wait`` and ``checkpoint`` observe it
    immediately even if the caller is on another thread.
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        *,
        command_capacity: int = COMMAND_QUEUE_MAX,
    ) -> None:
        self.session_id = str(session_id) if session_id else secrets.token_hex(8)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._stop_reason = ""
        self._stop_after_fish = False
        self._command_capacity = max(1, int(command_capacity))
        self._commands: Deque[Dict[str, Any]] = deque()

    # ------------------------------------------------------------------
    # Stop state
    # ------------------------------------------------------------------
    def request_stop(self, reason: str = "") -> None:
        """Request an immediate stop.  The first reason wins."""
        with self._lock:
            if not self._stop_event.is_set():
                self._stop_reason = str(reason or "stop requested")
                self._stop_event.set()

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    @property
    def stop_reason(self) -> str:
        with self._lock:
            return self._stop_reason

    def checkpoint(self) -> None:
        """Raise :class:`AutoStop` when a stop has been requested."""
        if self._stop_event.is_set():
            raise AutoStop(self.stop_reason)

    # ------------------------------------------------------------------
    # Stop-after-fish state
    # ------------------------------------------------------------------
    def request_stop_after_fish(self) -> None:
        with self._lock:
            self._stop_after_fish = True

    @property
    def stop_after_fish(self) -> bool:
        with self._lock:
            return self._stop_after_fish

    def resume(self) -> bool:
        """Clear the stop-after-fish request.

        A hard stop is terminal for this session and is not cleared.  Returns
        ``True`` when the session is still usable after the call.
        """
        with self._lock:
            self._stop_after_fish = False
            return not self._stop_event.is_set()

    # ------------------------------------------------------------------
    # Interruptible sleeping
    # ------------------------------------------------------------------
    def wait(self, seconds: float, quantum: float = 0.05) -> bool:
        """Sleep up to *seconds*, returning ``True`` if a stop interrupted it.

        The wait is implemented with ``threading.Event.wait`` so it is woken as
        soon as ``request_stop`` is called.  ``quantum`` only bounds how often
        the deadline is re-checked.
        """
        try:
            remaining = max(0.0, float(seconds))
        except (TypeError, ValueError):
            remaining = 0.0
        try:
            quantum = max(0.001, float(quantum))
        except (TypeError, ValueError):
            quantum = 0.05

        if remaining <= 0.0:
            return self._stop_event.is_set()

        deadline = time.monotonic() + remaining
        while True:
            if self._stop_event.is_set():
                return True
            left = deadline - time.monotonic()
            if left <= 0.0:
                return False
            if self._stop_event.wait(min(quantum, left)):
                return True

    # ------------------------------------------------------------------
    # Command queue (main thread consumes)
    # ------------------------------------------------------------------
    def enqueue_command(self, command: Dict[str, Any]) -> bool:
        """Append a command if the bounded queue has room.

        Returns ``False`` when the queue is full.  The command is copied so the
        producer cannot mutate queued state afterwards.
        """
        if not isinstance(command, dict):
            raise TypeError("command must be a dict")
        item = copy.deepcopy(command)
        with self._lock:
            if len(self._commands) >= self._command_capacity:
                return False
            self._commands.append(item)
            return True

    def consume_commands(self) -> List[Dict[str, Any]]:
        """Drain and return queued commands in FIFO order (deep copies)."""
        with self._lock:
            items = list(self._commands)
            self._commands.clear()
        return copy.deepcopy(items)

    @property
    def command_count(self) -> int:
        with self._lock:
            return len(self._commands)


__all__ = ["ControlSession", "AutoStop", "AutoStopError", "COMMAND_QUEUE_MAX"]
