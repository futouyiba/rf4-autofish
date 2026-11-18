# -*- coding: utf-8 -*-
"""控制权归属（纯离线逻辑，不依赖 Tk / Win32 / 输入库）。

把「前台状态 + 用户接管信号」翻译成四态归属，以及面向人的告知文案。
所有函数都是纯函数，可在无显示环境单测。

四态（见 bot/docs/CONTROL-OWNERSHIP-PLAN.md）：
  TOOL       工具独占：正常注入
  YIELDING   让出中：瞬时过渡
  USER       用户独占：只观察，不注入、不聚焦
  RETURNING  待恢复：等用户确认后才续

判据来源：低级钩子的 injected 位（实测可靠，见计划 §2.1）。
本模块不关心事件从哪来，只消费「最近一次物理输入的时刻」。
"""

from __future__ import annotations

import time
from typing import Any, Optional

__all__ = [
    "TOOL", "YIELDING", "USER", "RETURNING",
    "ownership_state", "ownership_text", "human_duration", "notice_text",
]

TOOL = "TOOL"
YIELDING = "YIELDING"
USER = "USER"
RETURNING = "RETURNING"


def _optional_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


def ownership_state(
    foreground: Any,
    *,
    last_physical_at: Optional[float] = None,
    now: Optional[float] = None,
    physical_window: float = 2.0,
    returning: bool = False,
    stopped: bool = False,
) -> str:
    """推导归属态。纯函数，不触发任何动作。

    * ``stopped`` → 工具已停，归属无意义，仍返回 TOOL（面板据此隐藏告知）；
    * ``returning`` 显式优先：用户已回游戏但需确认，处于 RETURNING；
    * 前台丢失 → USER（工具不可能注入，控制权事实归用户）；
    * 前台在，且最近 ``physical_window`` 秒内有物理键鼠 → USER（用户在手动接管）；
    * 否则 → TOOL。

    ``YIELDING`` 是瞬时过渡态，由调用方在「检测到接管、让出动作尚未完成」
    的那一帧显式传入，本函数不自行产生它（避免纯函数带时序副作用）。
    """
    if returning:
        return RETURNING
    if stopped:
        return TOOL
    if _optional_bool(foreground) is False:
        return USER
    if last_physical_at is not None:
        current = time.time() if now is None else now
        try:
            if 0.0 <= current - float(last_physical_at) <= physical_window:
                return USER
        except (TypeError, ValueError):
            pass
    return TOOL


def ownership_text(state: Any) -> str:
    """归属态的中文展示，未知值不猜。"""
    key = str(state or "").strip().upper()
    return {
        TOOL: "工具控制中",
        YIELDING: "正在让出控制",
        USER: "你控制中（工具不动作）",
        RETURNING: "等你确认是否继续",
    }.get(key, "归属未知")


def human_duration(seconds: Any) -> str:
    """把秒数格式化成人类易读的短时长。

    ``刚停止`` / ``12 秒`` / ``3 分 12 秒`` / ``1 小时 5 分``。
    负数/非法值返回 ``—``，不编造。整天不换算成天（钓鱼场景用不到）。
    """
    try:
        value = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return "—"
    total = int(value)
    if total < 2:
        return "刚停止"
    if total < 60:
        return "%d 秒" % total
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return "%d 分 %d 秒" % (minutes, secs) if secs else "%d 分" % minutes
    hours, minutes = divmod(minutes, 60)
    return "%d 小时 %d 分" % (hours, minutes) if minutes else "%d 小时" % hours


def notice_text(since: Optional[float], now: Optional[float] = None) -> str:
    """显著告知窗正文：说明控制权已交接，并显示绝对时刻 + 已停止时长。

    ``since`` 是让出发生的 epoch 秒。绝对时刻回答「什么时候停的」，
    时长回答「停了多久」，两者都要，人类易读。
    """
    current = time.time() if now is None else now
    try:
        start = float(since)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "工具已停止输入，控制权交还给你。\n已停止：—"
    clock = time.strftime("%H:%M:%S", time.localtime(start))
    duration = human_duration((current if current is not None else time.time()) - start)
    return (
        "工具已停止输入，控制权交还给你。\n"
        "停止于 %s  ·  已停止 %s"
    ) % (clock, duration)
