# -*- coding: utf-8 -*-
"""本次会话时限计时（纯离线逻辑，不依赖 Tk / IPC / 系统时钟源）。

设计要点（见 bot/docs/SESSION-LIMIT-PLAN.md §5）：

* 计的是**累积运行时长**，不是墙钟：暂停/停止期间不计时；
* 默认 8 小时，范围 [1 分钟, 24 小时]，由用户改；
* 状态 running / paused / expired，可序列化到 session.json。

所有方法接受注入的 ``now``（epoch 秒），便于离线测试。本模块只算状态，
不发命令、不碰游戏、不读写文件。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = [
    "SessionTimer", "DEFAULT_LIMIT_SECONDS", "MIN_LIMIT_SECONDS",
    "MAX_LIMIT_SECONDS", "RUNNING", "PAUSED", "EXPIRED", "clamp_limit",
    "remaining_text",
]

RUNNING = "running"
PAUSED = "paused"
EXPIRED = "expired"

DEFAULT_LIMIT_SECONDS = 8 * 3600
MIN_LIMIT_SECONDS = 60                 # 1 分钟
MAX_LIMIT_SECONDS = 24 * 3600          # 24 小时


def clamp_limit(seconds: Any) -> float:
    """把用户输入的秒数夹到合法范围；非法值回退默认。"""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return float(DEFAULT_LIMIT_SECONDS)
    if value != value or value in (float("inf"), float("-inf")):
        return float(DEFAULT_LIMIT_SECONDS)
    return float(min(MAX_LIMIT_SECONDS, max(MIN_LIMIT_SECONDS, value)))


def remaining_text(seconds: Any) -> str:
    """剩余时长的人类易读展示：8 小时 / 2 时 15 分 / 45 秒 / 已到时。"""
    try:
        value = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return "—"
    total = int(value)
    if total <= 0:
        return "已到时"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "%d 小时 %d 分" % (hours, minutes) if minutes else "%d 小时" % hours
    if minutes:
        return "%d 分 %d 秒" % (minutes, secs) if secs else "%d 分" % minutes
    return "%d 秒" % secs


class SessionTimer:
    """累积运行时长计时器。纯逻辑，now 由调用方注入。"""

    def __init__(
        self,
        limit_seconds: Any = DEFAULT_LIMIT_SECONDS,
        *,
        state: str = PAUSED,
        elapsed: float = 0.0,
        started_at: Optional[float] = None,
    ) -> None:
        self.limit = clamp_limit(limit_seconds)
        self.state = state if state in (RUNNING, PAUSED, EXPIRED) else PAUSED
        self.elapsed = max(0.0, float(elapsed))
        self.started_at = started_at if self.state == RUNNING else None

    # ---------- 状态迁移 ----------
    def start(self, now: float) -> None:
        """从暂停开始（或重新开始）计时；expired 需先 reset。"""
        if self.state == EXPIRED:
            return
        if self.state != RUNNING:
            self.state = RUNNING
            self.started_at = now

    def pause(self, now: float) -> None:
        """暂停并把这一段运行时长累加进去。"""
        if self.state == RUNNING:
            if self.started_at is not None:
                self.elapsed += max(0.0, float(now) - float(self.started_at))
            self.started_at = None
            self.state = PAUSED

    def resume(self, now: float) -> None:
        """开钓：若已到期则不复活；否则等价于 start。"""
        if self.state == EXPIRED:
            return
        self.start(now)

    def reset(self, *, limit_seconds: Any = None) -> None:
        """清除本次会话，回到暂停态、零累计。可选同时改时限。"""
        if limit_seconds is not None:
            self.limit = clamp_limit(limit_seconds)
        self.state = PAUSED
        self.elapsed = 0.0
        self.started_at = None

    def set_limit(self, seconds: Any) -> None:
        """改时限。不影响已累计时长（elapsed_seconds 用 started_at 实时算）。"""
        self.limit = clamp_limit(seconds)

    # ---------- 查询 ----------
    def elapsed_seconds(self, now: float) -> float:
        """截至 now 的累计运行时长。"""
        total = self.elapsed
        if self.state == RUNNING and self.started_at is not None:
            total += max(0.0, float(now) - float(self.started_at))
        return total

    def remaining(self, now: float) -> float:
        return max(0.0, self.limit - self.elapsed_seconds(now))

    def expired(self, now: float) -> bool:
        """是否到时；到期时把状态固化为 expired，避免重复触发。"""
        if self.state == EXPIRED:
            return True
        if self.elapsed_seconds(now) >= self.limit:
            self.state = EXPIRED
            self.started_at = None
            return True
        return False

    # ---------- 持久化 ----------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "limit_seconds": self.limit,
            "state": self.state,
            "elapsed": self.elapsed,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "SessionTimer":
        if not isinstance(data, dict):
            return cls()
        started = data.get("started_at")
        if not isinstance(started, (int, float)):
            started = None
        return cls(
            data.get("limit_seconds", DEFAULT_LIMIT_SECONDS),
            state=str(data.get("state", PAUSED)),
            elapsed=float(data.get("elapsed", 0.0) or 0.0),
            started_at=started,
        )
