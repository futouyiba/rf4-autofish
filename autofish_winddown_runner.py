# -*- coding: utf-8 -*-
"""收尾执行器：独立进程，到时限后串行执行「去鱼市→卖鱼→小退停住」。

由面板在 bot 已停止后拉起。绝不 import lifecycle/bridge，不碰钓鱼状态机。
每步结果写 data/winddown_result.json，面板读到后弹窗。

卖鱼的能力边界（见 SESSION-LIMIT-PLAN.md §4）：
  没有鱼市模板/OCR，无法可靠确认"商店已打开/已卖出"。本执行器按参考坐标
  操作，用"画面是否变化"做弱校验；任何一步不满足即记失败并停止，转人工。
  坐标可用环境变量覆盖（RF4_SELL_*），便于实机调整。

用法（面板/CLI）：
    python autofish_winddown_runner.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

BOT = Path(__file__).resolve().parent
sys.path.insert(0, str(BOT))

import autofish_winddown as wd  # noqa: E402
import autofish_session_store as store  # noqa: E402

RESULT_FILE = "winddown_result.json"
LOG_FILE = "winddown.log"

def _env_point(name: str, default: tuple) -> tuple:
    raw = os.environ.get(name)
    if raw:
        try:
            a, b = raw.split(",")
            return int(a), int(b)
        except Exception:
            pass
    return default


# 卖鱼参考坐标（1920×1080 客户区基准，来自 refs/rf4s-cn/stages/sell_fish.py）
# 可用环境变量覆盖；这些是【待实机验证】的初值，不是已验证坐标。
SELL_SELECT = _env_point("RF4_SELL_SELECT", (172, 254))   # 全选区域中心
SELL_BUTTON = _env_point("RF4_SELL_BUTTON", (226, 382))   # 出售按钮中心


def _log_path() -> Path:
    try:
        import app_paths
        return app_paths.data_path(LOG_FILE)
    except Exception:
        return BOT / "data" / LOG_FILE


def _result_path() -> Path:
    try:
        import app_paths
        return app_paths.data_path(RESULT_FILE)
    except Exception:
        return BOT / "data" / RESULT_FILE


def emit(msg: str) -> None:
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        p = _log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def write_result(state: wd.WinddownState) -> None:
    payload = state.to_dict()
    payload["completed_wall"] = time.strftime("%H:%M:%S")
    try:
        p = _result_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError as exc:
        emit("结果写入失败：%s" % exc)


def _live_io():
    """复用小退实机的 LiveIo（抓图/点击/按键）。"""
    from _test_relogin_live import LiveIo  # noqa: PLC0415
    return LiveIo()


def step_goto(state: wd.WinddownState, io: Any) -> bool:
    """寻路：小退回 home → 重放录制的鱼市路线（可靠部分）。"""
    import autofish_goto as ag  # noqa: PLC0415
    emit("寻路：小退回 home + 重放「鱼市」录制路线")
    ok = ag.goto("鱼市", relogin=True, log=lambda m: emit("  " + m))
    state.record(wd.GOTO, ok, "已到鱼市" if ok else "路线重放失败/鱼市路线未录制",
                 time.time())
    return ok


def step_sell(state: wd.WinddownState, io: Any) -> bool:
    """卖鱼：按 E 进店 → 固定等待 → 全选 → 出售。弱校验画面变化。

    无法确认"一定是鱼市商店"：只检查按 E 后画面从 WORLD 变了（弱证据），
    其余靠固定等待与坐标。任何异常即失败转人工。
    """
    import autofish_relogin as arl  # noqa: PLC0415
    try:
        before, _, _ = arl.screen_kind(io.capture())
        emit("卖鱼：按 E 进店（当前画面 %s）" % before)
        io.key("e")
        time.sleep(1.6)
        after, _, _ = arl.screen_kind(io.capture())
        # 弱校验：画面必须变化，否则视为"没反应"→转人工
        if after == before:
            state.record(wd.SELL, False,
                         "按 E 后画面未变化（%s），无法确认进店" % after,
                         time.time())
            return False
        emit("卖鱼：画面已变化（%s→%s），按参考坐标全选/出售" % (before, after))
        cx, cy = SELL_SELECT
        io.click_client(int(cx), int(cy))
        time.sleep(0.4)
        cx, cy = SELL_BUTTON
        io.click_client(int(cx), int(cy))
        time.sleep(1.2)
        io.key("e")
        io.key("e")
        # 无可靠"已卖出"判据 → 记为"已尝试"，由用户实机核对
        state.record(wd.SELL, True,
                     "已按参考坐标尝试出售（未验证结果，请核对收入）",
                     time.time())
        emit("卖鱼：已尝试出售；无自动判据，结果需人工核对")
        return True
    except Exception as exc:  # noqa: BLE001 - 任何异常转人工
        state.record(wd.SELL, False, "卖鱼异常：%s" % type(exc).__name__,
                     time.time())
        return False


def step_logout(state: wd.WinddownState, io: Any) -> bool:
    """小退停住：Esc→退出→是→停在登录界面，不点登录。"""
    import autofish_relogin as arl  # noqa: PLC0415
    try:
        emit("小退：退出到登录界面并停住")
        result = arl.logout(io, log=emit)
        state.record(wd.LOGOUT, bool(result.get("ok")),
                     "已停在登录界面" if result.get("ok") else "小退未确认",
                     time.time())
        return bool(result.get("ok"))
    except Exception as exc:  # noqa: BLE001
        state.record(wd.LOGOUT, False, "小退失败：%s" % exc, time.time())
        return False


def run() -> int:
    state = wd.WinddownState()
    state.start(time.time())
    emit("收尾开始：去鱼市 → 卖鱼 → 小退停住")
    write_result(state)
    try:
        io = _live_io()
    except Exception as exc:
        state.record(wd.GOTO, False, "实机 IO 初始化失败：%s" % exc, time.time())
        write_result(state)
        return 1

    steps: tuple = (
        (wd.GOTO, step_goto),
        (wd.SELL, step_sell),
        (wd.LOGOUT, step_logout),
    )
    for name, fn in steps:
        ok = fn(state, io)
        write_result(state)
        if not ok:
            emit("收尾在第 %s 步失败，已停止（转人工）：%s"
                 % (name, state.failed_reason))
            return 1

    emit("收尾完成：%s" % state.state)
    return 0


if __name__ == "__main__":
    sys.exit(run())
