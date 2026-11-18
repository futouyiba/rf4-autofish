# -*- coding: utf-8 -*-
"""面板侧收尾客户端：检查触发文件、拉起执行器、读结果。

纯协调逻辑；进程拉起与文件 IO 由调用方注入 runner/spawn/path，便于离线测试。
触发文件由 runtime 到时限时写（data/winddown_request.json）；
结果文件由执行器写（data/winddown_result.json）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional

__all__ = ["REQUEST_FILE", "RESULT_FILE", "WinddownClient"]

REQUEST_FILE = "winddown_request.json"
RESULT_FILE = "winddown_result.json"


class WinddownClient:
    """面板侧：一次性消费触发、拉起执行器、读结果。

    ``spawn`` 为 runner 拉起函数（返回 True 表示已拉起）；
    ``read`` / ``remove`` 为文件读写注入点。
    """

    def __init__(self, spawn: Callable[[], bool],
                 read: Callable[[Path], Optional[Dict[str, Any]]],
                 remove: Callable[[Path], None],
                 request_path: Path,
                 result_path: Path) -> None:
        self._spawn = spawn
        self._read = read
        self._remove = remove
        self._request = request_path
        self._result = result_path
        self._started = False     # 本次面板会话是否已消费过触发
        self._result_shown = False

    def poll(self) -> Optional[Dict[str, Any]]:
        """返回需要弹窗的结果 dict；否则 None。

        流程：见到触发文件且未拉起过 → 消费触发 + 拉起执行器；
        执行器结束后读到结果 → 返回一次结果（之后不再重复弹）。
        """
        # 先读结果（执行器可能已结束）
        if self._started and not self._result_shown:
            result = self._read(self._result)
            if result is not None and result.get("state") in ("DONE", "FAILED"):
                self._result_shown = True
                return result

        # 再查触发
        if not self._started:
            trigger = self._read(self._request)
            if trigger is not None:
                try:
                    self._remove(self._request)   # 一次性消费
                except Exception:
                    pass
                self._started = self._spawn()
        return None

    def summary_text(self, result: Dict[str, Any]) -> str:
        """把结果格式化成 message box 正文。"""
        steps = result.get("steps") or {}
        if not isinstance(steps, dict):
            steps = {}
        sell = "✓" if (steps.get("SELL") or {}).get("ok") else "✗"
        logout = "✓" if (steps.get("LOGOUT") or {}).get("ok") else "✗"
        wall = result.get("completed_wall") or "—"
        text = (
            "本次钓鱼已到时限，已钓完最后一条。\n\n"
            "收尾结果：\n"
            "  卖鱼  %s\n"
            "  小退  %s（停在登录界面）\n\n"
            "完成于 %s\n"
        ) % (sell, logout, wall)
        if result.get("state") == "FAILED":
            text += "\n第 %s 步未完成：%s\n请人工处理。" % (
                result.get("failed_step") or "?",
                result.get("failed_reason") or "未知原因")
        return text
