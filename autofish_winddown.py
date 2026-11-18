# -*- coding: utf-8 -*-
"""收尾编排状态机（纯离线逻辑，动作由调用方注入）。

串行执行四步：goto 鱼市 → 卖鱼 → 小退停登录界面 → 完成。
任何一步失败即进入 FAILED 并停止，绝不盲目继续、绝不假报成功。

状态（见 bot/docs/SESSION-LIMIT-PLAN.md §3）：
  IDLE → GOTO → SELL → LOGOUT → DONE
                 └─ 任一步失败 → FAILED(step, reason)

本模块不碰游戏、不 import Win32/OCR；只管状态迁移与结果记录。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

__all__ = [
    "IDLE", "GOTO", "SELL", "LOGOUT", "DONE", "FAILED",
    "STEPS", "WinddownState", "winddown_next",
]

IDLE = "IDLE"
GOTO = "GOTO"
SELL = "SELL"
LOGOUT = "LOGOUT"
DONE = "DONE"
FAILED = "FAILED"

STEPS = (GOTO, SELL, LOGOUT)


class WinddownState:
    """收尾编排的可序列化状态。"""

    def __init__(self) -> None:
        self.state = IDLE
        self.step_results: Dict[str, Dict[str, Any]] = {}
        self.failed_step: Optional[str] = None
        self.failed_reason: Optional[str] = None
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None

    def start(self, now: float) -> None:
        self.state = GOTO
        self.started_at = now
        self.step_results = {}
        self.failed_step = self.failed_reason = None
        self.finished_at = None

    def record(self, step: str, ok: bool, detail: str, now: float) -> None:
        self.step_results[step] = {
            "ok": bool(ok), "detail": str(detail)[:200], "at": now,
        }
        if not ok:
            self.failed_step = step
            self.failed_reason = str(detail)[:200]
            self.state = FAILED
            self.finished_at = now
        elif step == LOGOUT:
            self.state = DONE
            self.finished_at = now

    @property
    def done(self) -> bool:
        return self.state in (DONE, FAILED)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "steps": self.step_results,
            "failed_step": self.failed_step,
            "failed_reason": self.failed_reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "WinddownState":
        w = cls()
        if not isinstance(data, dict):
            return w
        w.state = str(data.get("state", IDLE))
        sr = data.get("steps")
        w.step_results = sr if isinstance(sr, dict) else {}
        w.failed_step = data.get("failed_step")
        w.failed_reason = data.get("failed_reason")
        w.started_at = data.get("started_at")
        w.finished_at = data.get("finished_at")
        return w


def winddown_next(current: str) -> Optional[str]:
    """下一步；DONE/FAILED/未知返回 None。"""
    try:
        i = STEPS.index(current)
    except ValueError:
        return None
    return STEPS[i + 1] if i + 1 < len(STEPS) else None
