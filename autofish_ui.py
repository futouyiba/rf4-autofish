# -*- coding: utf-8 -*-
"""Standalone light side panel for the autofish runtime.

The panel is deliberately read-mostly: it polls the authenticated loopback IPC
from a background thread, renders a copied snapshot, and only sends the small
set of commands exposed by :mod:`autofish_ipc`.  It never touches the game
window (FindWindow/GetWindowRect/IsIconic only), never sends input, and never
starts the bot unless the user presses the button.

Usage
-----
    python autofish_ui.py
    python autofish_ui.py --demo            # local fake snapshots, no bot
    python autofish_ui.py --headless-smoke  # no Tk, model smoke test

Importing this module must stay side-effect free: no Tk root, no socket, no
child process.  Tk is imported lazily inside :func:`main` / the panel class.
"""

from __future__ import annotations

import ctypes
import math
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import app_paths
import autofish_hotkeys as hotkeys
from autofish_ipc import RuntimeClient

PANEL_WIDTH = 320
PANEL_HEIGHT = 640
PANEL_MARGIN = 8
POLL_INTERVAL = 0.5
CUR_MAX = 29
GAME_WINDOW_TITLE = "Russian Fishing 4"
# A queued hotkey older than this is stale (panel restarted or Tk stalled).
HOTKEY_MAX_AGE = 1.5
# Resume waits for the whole combo to be released, without blocking Tk.
RESUME_RELEASE_TIMEOUT = 3.0
RESUME_RELEASE_POLL_MS = 50

# FSM states (not control modes) that gate the two manual confirmations.
CONFIRM_READY_STATES = frozenset({"PROBING", "READY"})
NET_STATES = frozenset(
    {
        "NET",
        "NETTING",
        "WAIT_NET",
        "WAIT_NET_CONFIRM",
        "NET_CONFIRM",
        "CONFIRM_NET",
    }
)
# A confirmation would be rejected by the runtime unless it runs in AUTO.
CONFIRM_MODES = frozenset({"AUTO"})
# 生产里 requested=AUTO 但 readiness 无法判定时，control_mode 会是 BLOCKED，
# 这是唯一允许人工确认的 BLOCKED 原因（不是预检故障/失焦）。
BLOCKED_CONFIRM_REASON = "readiness_unknown"
ACK_FINAL_STATUSES = frozenset({"executed", "rejected"})

BOT_DIR = app_paths.APP_DIR
BOT_CTL = BOT_DIR / "bot_ctl.py"
#: 打包后没有 bot_ctl.py 可读，按已知动作白名单判定（与 bot_ctl.main 的分支一致）。
FROZEN_CTL_ACTIONS = frozenset(
    {
        "start",
        "stop",
        "status",
        "observe",
        "resume",
        "confirm-ready",
        "confirm_ready",
        "confirm-net",
        "confirm_net",
        "stop-after-fish",
        "stop_after_fish",
        "emergency-stop",
        "emergency_stop",
        "release",
        "toggle",
    }
)
MISSING = "—"

RectTuple = Tuple[int, int, int, int]

_FIELD_ROWS = (
    ("模式", ("mode",), None),
    ("原因", ("reason",), None),
    ("状态/鱼 elapsed", ("state_elapsed", "fish_elapsed"), " / "),
    ("cur / 可信度", ("cur", "confidence"), "  ·  "),
    ("bar / tier", ("bar", "tier"), " / "),
    ("ring", ("ring",), None),
    ("desired", ("desired",), None),
    ("emitted", ("emitted",), None),
    ("kept", ("kept",), None),
    ("last_error", ("last_error",), None),
    ("NET待确认/心跳", ("net_pending", "heartbeat"), " / "),
)

_COMMAND_CONTEXT = {
    "resume": ("state_version", "epoch"),
    "observe": ("state_version", "epoch"),
    "confirm_ready": ("state_version", "epoch"),
    "confirm_net_retracted": ("fish_id", "state_version", "epoch"),
    "stop": (),
    "stop_after_fish": (),
}

_OFFLINE_HINTS = (
    "endpoint unavailable",
    "transport error",
    "timeout",
    "empty response",
    "offline",
    "not running",
    "connection",
)


# ---------------------------------------------------------------------------
# Pure model helpers
# ---------------------------------------------------------------------------


def _geometry_string(width: int, height: int, x: int, y: int) -> str:
    """Tk geometry with explicit signs, valid for negative monitor origins."""
    return "%dx%d%+d%+d" % (width, height, x, y)


def _bot_ctl_has_action(action: str) -> bool:
    """Avoid sending an unknown action to bot_ctl's toggle fallback.

    打包态没有 ``bot_ctl.py`` 文件可搜，用白名单判定（和 bot_ctl.main 的
    分支一一对应）；漏掉这里会让 Start 按钮静默失效。
    """
    if app_paths.is_frozen():
        return action in FROZEN_CTL_ACTIONS
    try:
        source = BOT_CTL.read_text(encoding="utf-8")
    except OSError:
        return False
    return action in source or action.replace("-", "_") in source


def _ellipsis(text: str, limit: int = 26) -> str:
    """Keep long reasons/errors inside the fixed-height panel."""
    value = str(text)
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def _clamp(value: int, low: int, high: int) -> int:
    """Clamp *value*; when the interval is empty return the lower bound."""
    if high < low:
        return low
    return max(low, min(value, high))


def _as_rect(value: Any) -> Optional[RectTuple]:
    """Accept tuples/lists/dicts/objects and return ``(l, t, r, b)``."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        if all(key in value for key in ("left", "top", "right", "bottom")):
            try:
                return (
                    int(round(float(value["left"]))),
                    int(round(float(value["top"]))),
                    int(round(float(value["right"]))),
                    int(round(float(value["bottom"]))),
                )
            except (TypeError, ValueError, OverflowError):
                return None
        if all(key in value for key in ("x", "y", "width", "height")):
            try:
                x = int(round(float(value["x"])))
                y = int(round(float(value["y"])))
                w = int(round(float(value["width"])))
                h = int(round(float(value["height"])))
            except (TypeError, ValueError, OverflowError):
                return None
            return (x, y, x + w, y + h)
        return None
    if all(hasattr(value, attr) for attr in ("left", "top", "right", "bottom")):
        try:
            return (
                int(round(float(getattr(value, "left")))),
                int(round(float(getattr(value, "top")))),
                int(round(float(getattr(value, "right")))),
                int(round(float(getattr(value, "bottom")))),
            )
        except (TypeError, ValueError, OverflowError):
            return None
    if isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        parts = list(value)
    except TypeError:
        return None
    if len(parts) < 4:
        return None
    try:
        return (
            int(round(float(parts[0]))),
            int(round(float(parts[1]))),
            int(round(float(parts[2]))),
            int(round(float(parts[3]))),
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _valid_rect(rect: Optional[RectTuple]) -> bool:
    return rect is not None and rect[2] > rect[0] and rect[3] > rect[1]


def _overlap_area(area: RectTuple, game: RectTuple) -> int:
    width = max(0, min(area[2], game[2]) - max(area[0], game[0]))
    height = max(0, min(area[3], game[3]) - max(area[1], game[1]))
    return width * height


def _center_distance_sq(area: RectTuple, game: RectTuple) -> float:
    ax = (area[0] + area[2]) / 2.0
    ay = (area[1] + area[3]) / 2.0
    gx = (game[0] + game[2]) / 2.0
    gy = (game[1] + game[3]) / 2.0
    return (ax - gx) ** 2 + (ay - gy) ** 2


def _best_area(game: RectTuple, areas: Sequence[RectTuple]) -> RectTuple:
    overlapping = [area for area in areas if _overlap_area(area, game) > 0]
    if overlapping:
        return max(overlapping, key=lambda area: _overlap_area(area, game))
    return min(areas, key=lambda area: _center_distance_sq(area, game))


def _fits(area: RectTuple, x: int, y: int, width: int, height: int, margin: int) -> bool:
    return (
        x >= area[0] + margin
        and y >= area[1] + margin
        and x + width + margin <= area[2]
        and y + height + margin <= area[3]
    )


def place_panel(
    game_rect: Any,
    work_areas: Any,
    panel_width: int = PANEL_WIDTH,
    panel_height: int = PANEL_HEIGHT,
    margin: int = PANEL_MARGIN,
) -> Tuple[int, int]:
    """Return a visible top-left for the panel.

    Preference order: right of the game, left of the game, then clamped inside
    the work area that overlaps the game most.  ``work_areas`` may contain
    negative-coordinate monitor work areas (left/upper monitors).  When the
    game is absent the panel floats at the top-right of the first work area.
    Only pure arithmetic is used: no Win32 calls, no window movement.
    """
    try:
        width = max(1, int(round(float(panel_width))))
        height = max(1, int(round(float(panel_height))))
        gap = max(0, int(round(float(margin))))
    except (TypeError, ValueError, OverflowError):
        width, height, gap = PANEL_WIDTH, PANEL_HEIGHT, PANEL_MARGIN

    areas: List[RectTuple] = []
    if work_areas is not None:
        single = _as_rect(work_areas)
        if _valid_rect(single):
            candidates = [single]
        else:
            try:
                candidates = list(work_areas)
            except TypeError:
                candidates = [work_areas]
        for candidate in candidates:
            rect = _as_rect(candidate)
            if _valid_rect(rect):
                areas.append(rect)  # type: ignore[arg-type]

    game = _as_rect(game_rect)
    if not _valid_rect(game):
        if not areas:
            return (gap, gap)
        area = areas[0]
        x = area[2] - width - gap
        y = area[1] + gap
        return (
            _clamp(x, area[0] + gap, area[2] - gap - width),
            _clamp(y, area[1] + gap, area[3] - gap - height),
        )

    assert game is not None
    if not areas:
        areas = [game]

    y0 = game[1] + gap

    right_x = game[2] + gap
    right_areas = [
        area for area in areas if _fits(area, right_x, y0, width, height, gap)
    ]
    if right_areas:
        area = max(right_areas, key=lambda item: _overlap_area(item, game))
        y = _clamp(y0, area[1] + gap, area[3] - gap - height)
        return (right_x, y)

    left_x = game[0] - gap - width
    left_areas = [
        area for area in areas if _fits(area, left_x, y0, width, height, gap)
    ]
    if left_areas:
        area = max(left_areas, key=lambda item: _overlap_area(item, game))
        y = _clamp(y0, area[1] + gap, area[3] - gap - height)
        return (left_x, y)

    area = _best_area(game, areas)
    x = _clamp(right_x, area[0] + gap, area[2] - gap - width)
    y = _clamp(y0, area[1] + gap, area[3] - gap - height)
    return (x, y)


def _path_get(mapping: Any, path: str) -> Any:
    current = mapping
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _first(mapping: Any, *paths: str) -> Any:
    """First non-None scalar/list value; mapping containers are skipped.

    Snapshot producers sometimes wrap a field in ``{"level": 3}``; callers use
    dotted paths to read those, so a bare container must not shadow the next
    alias.
    """
    if not isinstance(mapping, Mapping):
        return None
    for path in paths:
        value = _path_get(mapping, path)
        if value is not None and not isinstance(value, Mapping):
            return value
    return None


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _optional_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on", "是"):
            return True
        if lowered in ("0", "false", "no", "off", "否"):
            return False
    return None


def _format_scalar(value: Any) -> str:
    if value is None:
        return MISSING
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        if not math.isfinite(value):
            return MISSING
        text = ("%.3f" % value).rstrip("0").rstrip(".")
        return text or "0"
    if isinstance(value, (list, tuple, set, frozenset)):
        return _keys_text(value)
    if isinstance(value, Mapping):
        return MISSING
    text = str(value).strip()
    return text or MISSING


# 原因栏中文映射（只翻译已知的抄网相关 code，其余原样显示）。
# 自动抄网默认关闭，但 RF4_AUTO_NET=1 人工验收时会进入 NET；旧快照也要能读。
_REASON_TEXT_CN = {
    "near_net_disabled": "临岸候选，自动抄网待验证；继续收线提竿",
    "auto_net_disabled": "临岸候选，自动抄网待验证；继续收线提竿",
    "land": "临岸候选，继续收线提竿",
    "net": "抄网（已松收线键、保持提竿）",
    "net_deployed": "已发掏网指令（保持提竿，等待收网）",
    "net_retract_wait": "已发收网指令，等待切换动画",
    "net_fault": "抄网动作失败，保持提竿；请人工处理",
    "net_retracted": "收网完成，恢复搏鱼",
    "net_state_unknown": "网状态未知（中途接管），不自动抄网",
    "net_callback_missing": "自动抄网回调缺失，不掏网",
    "net_not_stowed": "网可能已掏出，不重复切换",
}


def _reason_text(value: Any) -> str:
    text = _format_scalar(value)
    if text == MISSING:
        return text
    return _REASON_TEXT_CN.get(str(text).strip().lower(), text)


def _elapsed_text(value: Any) -> str:
    if value is None:
        return MISSING
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return MISSING
        number = _number(text)
        if number is not None and number >= 0:
            return "%.1fs" % number
        return text
    number = _number(value)
    if number is None or number < 0:
        return MISSING
    return "%.1fs" % number


def _heartbeat_text(value: Any) -> str:
    if value is None:
        return MISSING
    number = _number(value)
    if number is not None and 0 <= number <= 600:
        return "%.2fs" % number
    return _format_scalar(value)


def _keys_text(value: Any) -> str:
    if value is None:
        return MISSING
    if isinstance(value, str):
        text = value.strip()
        return "[]" if not text else text
    if isinstance(value, (set, frozenset, list, tuple)):
        return "[" + ", ".join(sorted(str(item) for item in value)) + "]"
    if isinstance(value, Mapping):
        return MISSING
    return _format_scalar(value)


def _cur_text(snapshot: Mapping[str, Any]) -> str:
    raw = _first(snapshot, "cur", "friction_cur", "cur_value", "friction.cur")
    if raw is None:
        return "%s（上限 %d）" % (MISSING, CUR_MAX)
    number = _number(raw)
    if number is None or int(number) != number:
        return "%s（上限 %d）" % (MISSING, CUR_MAX)
    value = int(number)
    if 1 <= value <= CUR_MAX:
        return "%d / %d" % (value, CUR_MAX)
    return "%d（越界，上限 %d）" % (value, CUR_MAX)


def _confidence_text(snapshot: Mapping[str, Any]) -> str:
    raw = _first(snapshot, "confidence", "conf", "cap", "score")
    number = _number(raw)
    if number is None:
        return MISSING
    if 0.0 <= number <= 1.0:
        return "%.2f" % number
    return _format_scalar(number)


def _ring_text(snapshot: Mapping[str, Any]) -> str:
    raw = _first(snapshot, "ring", "ring_ratio", "observation.ring_ratio")
    if raw is None:
        return "unknown"
    number = _number(raw)
    if number is None:
        return _format_scalar(raw)
    return "%.2f" % number


def _kept_text(snapshot: Mapping[str, Any]) -> str:
    kept = _first(snapshot, "kept", "kept_total", "fish_kept")
    if kept is None:
        return MISSING
    total = _first(snapshot, "total", "total_fish", "fish_total")
    if total is not None:
        total_number = _number(total)
        kept_number = _number(kept)
        if (
            total_number is not None
            and kept_number is not None
            and total_number >= 0
            and kept_number >= 0
        ):
            return "%d / %d" % (int(kept_number), int(total_number))
    return _format_scalar(kept)


def _mode_raw(snapshot: Mapping[str, Any]) -> str:
    value = _first(
        snapshot,
        "control_mode",
        "states.mode",
        "mode.name",
        "mode.value",
        "mode",
        "runtime_mode",
    )
    if value is None:
        return ""
    return str(value).strip()


def _state_raw(snapshot: Mapping[str, Any]) -> str:
    """Raw FSM state text; the control mode is deliberately never used here."""
    value = _first(
        snapshot,
        "state.name",
        "state.value",
        "state",
        "phase",
        "fsm_state",
    )
    if value is None:
        return ""
    return str(value).strip()


def _upper_text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip().upper().replace("-", "_")
    return text


def is_net_state(state: Any) -> bool:
    """Whether *state* is the FSM NET state (the control mode is not a state)."""
    value = _upper_text(state)
    if not value:
        return False
    return value == "NET" or value.startswith("NET_") or value in NET_STATES


def is_net_mode(mode: Any) -> bool:
    """Deprecated alias: some callers used to pass the FSM state as *mode*."""
    return is_net_state(mode)


def is_confirm_ready_state(state: Any) -> bool:
    return _upper_text(state) in CONFIRM_READY_STATES


def mode_allows_confirm(mode: Any) -> bool:
    """AUTO may confirm; OBSERVE/MANUAL/BLOCKED must never enable the button.

    A missing mode is tolerated so snapshots from older runtimes keep working;
    it is never treated as a substitute for the state check.
    """
    value = _upper_text(mode)
    if not value:
        return True
    return value in CONFIRM_MODES


def net_pending(snapshot: Any) -> Optional[bool]:
    """Return the explicit NET-confirmation pending flag, if present."""
    if not isinstance(snapshot, Mapping):
        return None
    value = _first(
        snapshot,
        "net_pending",
        "net_pending_confirmation",
        "pending_net",
        "awaiting_net_confirmation",
        "net.pending",
        "net.confirm_pending",
    )
    return _optional_bool(value)


@dataclass(frozen=True)
class ViewModel:
    connected: bool
    stopped: bool
    stop_after_fish: bool
    state: str
    state_raw: str
    mode: str
    mode_raw: str
    reason: str
    foreground: Optional[bool]
    state_elapsed: str
    cur: str
    confidence: str
    bar: str
    tier: str
    ring: str
    desired: str
    emitted: str
    fish_elapsed: str
    kept: str
    last_error: str
    heartbeat: str
    net_pending: str
    net_pending_bool: Optional[bool]
    session: str
    status_message: str


def build_view(
    snapshot: Any = None, status: Any = None
) -> ViewModel:
    """Build the display model; missing values stay missing, never invented."""
    snap: Mapping[str, Any] = snapshot if isinstance(snapshot, Mapping) else {}
    stat: Mapping[str, Any] = status if isinstance(status, Mapping) else {}

    connected = _optional_bool(stat.get("ok"))
    if connected is None:
        connected = True

    stopped = _optional_bool(stat.get("stopped"))
    if stopped is None:
        stopped = _optional_bool(_first(snap, "stopped"))
    stopped = bool(stopped)

    stop_after = _optional_bool(stat.get("stop_after_fish"))
    if stop_after is None:
        stop_after = _optional_bool(_first(snap, "stop_after_fish"))
    stop_after = bool(stop_after)

    mode_raw = _mode_raw(snap)
    mode_text = _format_scalar(mode_raw)

    reason = _reason_text(
        _first(snap, "state.reason", "reason", "state_reason", "last_reason")
    )
    if stopped and reason == MISSING:
        reason = _reason_text(_first(snap, "stop_reason", "last_reason"))

    state_raw = _state_raw(snap)
    state_value = _format_scalar(state_raw)
    if not connected:
        state_text = "离线（未连接）"
    elif stopped:
        state_text = "已停止"
    else:
        state_text = state_value

    pending = net_pending(snap)
    if pending is True:
        pending_text = "是"
    elif pending is False:
        pending_text = "否"
    elif is_net_state(state_raw):
        pending_text = "未知"
    else:
        pending_text = MISSING

    session = _first(stat, "session")
    if session is None:
        session = _first(snap, "session", "session_id")
    session_text = _format_scalar(session)

    message = stat.get("message")
    message_text = "" if message is None else str(message).strip()

    return ViewModel(
        connected=bool(connected),
        stopped=stopped,
        stop_after_fish=stop_after,
        state=state_text,
        state_raw=state_raw,
        mode=mode_text,
        mode_raw=mode_raw,
        reason=reason,
        foreground=_optional_bool(
            _first(snap, "foreground", "foreground.active", "is_foreground")
        ),
        state_elapsed=_elapsed_text(
            _first(snap, "state_elapsed", "state_elapsed_s", "state_time", "elapsed")
        ),
        cur=_cur_text(snap),
        confidence=_confidence_text(snap),
        bar=_format_scalar(_first(snap, "bar.level", "bar", "bar_level")),
        tier=_format_scalar(_first(snap, "bar.tier", "tier", "bar_tier")),
        ring=_ring_text(snap),
        desired=_keys_text(
            _first(snap, "keys.desired", "desired", "desired_keys")
        ),
        emitted=_keys_text(
            _first(snap, "keys.emitted", "emitted", "emitted_keys")
        ),
        fish_elapsed=_elapsed_text(
            _first(snap, "fish_elapsed", "fish_elapsed_s", "fight_elapsed")
        ),
        kept=_kept_text(snap),
        last_error=_format_scalar(
            _first(snap, "last_error", "error.message", "error", "last_error_message")
        ),
        heartbeat=_heartbeat_text(
            _first(snap, "heartbeat", "heartbeat_s", "hb", "last_heartbeat")
        ),
        net_pending=pending_text,
        net_pending_bool=pending,
        session=session_text,
        status_message=message_text,
    )


def render_lines(view: ViewModel) -> List[str]:
    """Text rendering shared by the panel and the headless smoke test."""
    lines = [
        "状态：%s" % view.state,
        "模式：%s" % view.mode,
        "原因：%s" % view.reason,
        "状态 elapsed：%s" % view.state_elapsed,
        "cur（上限29）：%s" % view.cur,
        "可信度：%s" % view.confidence,
        "bar：%s" % view.bar,
        "tier：%s" % view.tier,
        "ring：%s" % view.ring,
        "desired：%s" % view.desired,
        "emitted：%s" % view.emitted,
        "fish_elapsed：%s" % view.fish_elapsed,
        "kept：%s" % view.kept,
        "last_error：%s" % view.last_error,
        "heartbeat：%s" % view.heartbeat,
        "NET pending：%s" % view.net_pending,
        "注意：desired/emitted 是发送账，不是游戏确认",
    ]
    if view.session != MISSING:
        lines.append("session：%s" % view.session)
    if view.status_message:
        lines.append("消息：%s" % view.status_message)
    return lines


# ---------------------------------------------------------------------------
# Button conditions (pure)
# ---------------------------------------------------------------------------


def can_resume(stopped: bool, connected: bool = True) -> bool:
    return bool(connected) and not bool(stopped)


def can_observe(stopped: bool, connected: bool = True) -> bool:
    return bool(connected) and not bool(stopped)


def can_stop(stopped: bool, connected: bool = True) -> bool:
    # Stop stays clickable while offline so a failed attempt can say "可能离线".
    return not bool(stopped)


def can_stop_after_fish(
    stopped: bool, stop_after_fish: bool = False, connected: bool = True
) -> bool:
    return bool(connected) and not bool(stopped) and not bool(stop_after_fish)


def can_confirm_ready(
    stopped: bool,
    connected: bool = True,
    *,
    state: Any = None,
    mode: Any = None,
    reason: Any = None,
    foreground: Any = None,
) -> bool:
    """Ready confirmation requires the FSM to be PROBING/READY and AUTO mode.

    Production exception: when ``requested=AUTO`` but readiness is unknown,
    the runtime reports ``control_mode=BLOCKED`` with
    ``reason=readiness_unknown``. That BLOCKED must stay manually
    confirmable, but only while the FSM is PROBING and the game is really in
    the foreground. Preflight faults and foreground loss look different
    (state/reason/foreground) and stay disabled.
    """
    if not bool(connected) or bool(stopped):
        return False
    if not is_confirm_ready_state(state):
        return False
    if _upper_text(mode) == "BLOCKED":
        return (
            _upper_text(state) == "PROBING"
            and _optional_bool(foreground) is True
            and _upper_text(reason) == BLOCKED_CONFIRM_REASON.upper()
        )
    return mode_allows_confirm(mode)


def can_confirm_net_retracted(
    stopped: bool,
    state: Any,
    pending: Any,
    connected: bool = True,
) -> bool:
    """Manual net confirmation is only legal in the FSM NET state while pending.

    *state* is the FSM state; reading the control mode here (``mode=AUTO``)
    permanently greyed the button out in production.
    """
    if not bool(connected) or bool(stopped):
        return False
    if not is_net_state(state):
        return False
    return _optional_bool(pending) is True


def can_start_background() -> bool:
    return True


def can_emergency_stop() -> bool:
    return True


@dataclass(frozen=True)
class ButtonStates:
    start_background: bool
    resume: bool
    observe: bool
    stop: bool
    stop_after_fish: bool
    confirm_ready: bool
    confirm_net_retracted: bool
    emergency_stop: bool


def button_states(
    view_or_snapshot: Any = None, status: Any = None
) -> ButtonStates:
    """Return enabled flags for every button.

    Accepts either a :class:`ViewModel` or a raw snapshot; raw snapshots are
    converted with :func:`build_view` (and optional status dict).
    """
    if isinstance(view_or_snapshot, ViewModel):
        view = view_or_snapshot
    else:
        view = build_view(view_or_snapshot, status)
    connected = view.connected
    stopped = view.stopped
    return ButtonStates(
        start_background=can_start_background(),
        resume=can_resume(stopped, connected),
        observe=can_observe(stopped, connected),
        stop=can_stop(stopped, connected),
        stop_after_fish=can_stop_after_fish(
            stopped, view.stop_after_fish, connected
        ),
        confirm_ready=can_confirm_ready(
            stopped,
            connected,
            state=view.state_raw,
            mode=view.mode_raw,
            reason=view.reason,
            foreground=view.foreground,
        ),
        confirm_net_retracted=can_confirm_net_retracted(
            stopped, view.state_raw, view.net_pending_bool, connected
        ),
        emergency_stop=can_emergency_stop(),
    )


# ---------------------------------------------------------------------------
# Command payload / acknowledgement helpers (pure)
# ---------------------------------------------------------------------------


def _context_value(snapshot: Mapping[str, Any], field: str) -> Any:
    if field == "fish_id":
        value = _first(
            snapshot,
            "fish_id",
            "current_fish_id",
            "fish.id",
            "fish.fish_id",
            "fight.fish_id",
        )
    elif field == "state_version":
        value = _first(
            snapshot,
            "state_version",
            "state_ver",
            "fsm_version",
            "state.version",
        )
    elif field == "epoch":
        value = _first(
            snapshot,
            "epoch",
            "session_epoch",
            "runtime_epoch",
            "session.epoch",
        )
    else:
        return None
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def command_payload(name: str, snapshot: Any = None) -> Dict[str, Any]:
    """Attach only the context fields the command actually needs."""
    snap: Mapping[str, Any] = snapshot if isinstance(snapshot, Mapping) else {}
    fields = _COMMAND_CONTEXT.get(str(name), ())
    payload: Dict[str, Any] = {}
    for field in fields:
        value = _context_value(snap, field)
        if value is not None:
            payload[field] = value
    return payload


def _looks_offline(message: str) -> bool:
    lowered = message.lower()
    return any(hint in lowered for hint in _OFFLINE_HINTS)


def ack_status(ack: Any) -> str:
    if not isinstance(ack, Mapping):
        return ""
    return str(ack.get("status") or "").strip().lower()


def ack_command_id(ack: Any) -> Optional[str]:
    """Command id carried by an ack (``id`` or legacy ``command_id``)."""
    if not isinstance(ack, Mapping):
        return None
    for key in ("id", "command_id"):
        value = ack.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def status_acks(status: Any) -> Optional[Dict[str, Mapping[str, Any]]]:
    """Extract ``{command_id: ack}`` from a status payload.

    ``None`` means the status exposes no ``acks`` field at all (the current
    RuntimeServer status response), so the panel must not claim a final result.
    Mapping and list forms are both accepted.
    """
    if not isinstance(status, Mapping):
        return None
    raw = status.get("acks")
    if raw is None:
        return None
    result: Dict[str, Mapping[str, Any]] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            if not isinstance(value, Mapping):
                continue
            command_id = ack_command_id(value)
            if not command_id and isinstance(key, str) and key.strip():
                command_id = key.strip()
            if command_id:
                result[command_id] = value
        return result
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            command_id = ack_command_id(item)
            if command_id:
                result[command_id] = item
        return result
    return None


def final_ack(status: Any, command_id: Any) -> Optional[Mapping[str, Any]]:
    """Final ``executed``/``rejected`` ack for *command_id*, when available."""
    if not command_id:
        return None
    acks = status_acks(status)
    if not acks:
        return None
    ack = acks.get(str(command_id))
    if isinstance(ack, Mapping) and ack_status(ack) in ACK_FINAL_STATUSES:
        return ack
    return None


def resolve_pending_acks(
    status: Any, pending: Any
) -> Tuple[Dict[str, Mapping[str, Any]], Dict[str, str]]:
    """Split ``{command_id: name}`` into resolved final acks and pending ids.

    Nothing is resolved without an ``acks`` field: the panel keeps showing
    "待确认" instead of inventing an executed/rejected outcome.
    """
    resolved: Dict[str, Mapping[str, Any]] = {}
    remaining: Dict[str, str] = {}
    if not isinstance(pending, Mapping) or not pending:
        return resolved, remaining
    acks = status_acks(status)
    for command_id, name in pending.items():
        ack = final_ack(status, command_id) if acks else None
        if ack is None:
            remaining[command_id] = name
        else:
            resolved[command_id] = ack
    return resolved, remaining


def ack_text(ack: Any, command: Optional[str] = None) -> str:
    """Render an IPC ack without overstating execution."""
    if not isinstance(ack, Mapping):
        return MISSING
    status = str(ack.get("status") or "").strip().lower()
    message = str(ack.get("message") or "").strip()
    if status == "executed":
        return "已执行" + ("：%s" % message if message else "")
    if status == "accepted":
        return "已接收待执行" + ("：%s" % message if message else "")
    if status == "rejected":
        if str(command or "").lower() == "stop" and _looks_offline(message):
            return "可能离线：%s" % (message or "后台未连接")
        return "未执行" + ("：%s" % message if message else "")
    if not bool(ack.get("ok", True)):
        if str(command or "").lower() == "stop" and _looks_offline(message):
            return "可能离线：%s" % (message or "后台未连接")
        return "未执行" + ("：%s" % message if message else "")
    return "未知回执" + ("：%s" % message if message else "")


# ---------------------------------------------------------------------------
# Demo data (never used by the production poller)
# ---------------------------------------------------------------------------


def demo_snapshot(step: int = 0) -> Dict[str, Any]:
    """Local fake snapshot for visual smoke tests.

    The control mode stays ``AUTO`` while the FSM state carries NET/FIGHT, i.e.
    the same shape the production snapshot has.  A fake ``mode="NET"`` used to
    hide the state-reading bug in the net-confirm button.
    """
    phase = (max(0, int(step)) // 6) % 4
    mode = "AUTO"
    if phase == 3:
        state = "NET"
        reason = "demo：等待确认收网"
        net_pending_flag = True
        desired = ["F6"]
        emitted = ["F6"]
    elif phase == 2:
        state = "LAND"
        reason = "demo：溜鱼"
        net_pending_flag = False
        desired = ["F5", "F6"]
        emitted = ["F5", "F6"]
    elif phase == 1:
        state = "FIGHT"
        reason = "demo：鱼在挣扎"
        net_pending_flag = False
        desired = ["F5"]
        emitted = ["F5"]
    else:
        state = "WAIT_BITE"
        reason = "demo：等待咬钩"
        net_pending_flag = False
        desired = []
        emitted = []

    return {
        "demo": True,
        "state": state,
        "control_mode": mode,
        "mode": mode,
        "reason": reason,
        "state_elapsed": 3.5 + (step % 6) * 0.5,
        "cur": 7,
        "confidence": 0.86,
        "bar": 4,
        "tier": 2,
        "ring": 0.42,
        "desired": desired,
        "emitted": emitted,
        "fish_elapsed": 1.25 + (step % 10) * 0.25,
        "kept": 3 + (step // 24),
        "total": 20,
        "last_error": "" if phase else "demo：无错误",
        "heartbeat": 0.4,
        "net_pending": net_pending_flag,
        "fish_id": "demo-fish-%04d" % (step % 7),
        "state_version": step,
        "epoch": "demo-epoch",
    }


def demo_status(step: int = 0) -> Dict[str, Any]:
    return {
        "ok": True,
        "session": "demo-session",
        "snapshot": demo_snapshot(step),
        "stopped": False,
        "stop_after_fish": False,
        "message": "demo",
    }


class DemoSource:
    """Thread-safe local status source; never opens a socket."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._step = 0

    def status(self) -> Dict[str, Any]:
        with self._lock:
            step = self._step
            self._step += 1
        return demo_status(step)


# ---------------------------------------------------------------------------
# Read-only Win32 helpers
# ---------------------------------------------------------------------------


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_ulong),
    ]


def enable_dpi_awareness() -> None:
    """Best-effort per-monitor DPI awareness before creating Tk."""
    if os.name != "nt":
        return
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def find_game_window(title: str = GAME_WINDOW_TITLE) -> int:
    """Read-only FindWindow lookup; returns 0 when absent."""
    if os.name != "nt":
        return 0
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        user32.FindWindowW.restype = ctypes.c_void_p
        hwnd = user32.FindWindowW(None, title)
        return int(hwnd or 0)
    except Exception:
        return 0


def read_game_rect(hwnd: int) -> Optional[RectTuple]:
    """Read-only GetWindowRect; returns None on failure."""
    if os.name != "nt" or not hwnd:
        return None
    try:
        rect = _RECT()
        user32 = ctypes.windll.user32
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
        user32.GetWindowRect.restype = ctypes.c_int
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        value = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        return value if _valid_rect(value) else None
    except Exception:
        return None


def is_game_minimized(hwnd: int) -> bool:
    if os.name != "nt" or not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.IsIconic.argtypes = [ctypes.c_void_p]
        user32.IsIconic.restype = ctypes.c_int
        return bool(user32.IsIconic(hwnd))
    except Exception:
        return False


def read_game_state(title: str = GAME_WINDOW_TITLE) -> Dict[str, Any]:
    """Read-only game window state.  Never moves, focuses or unminimizes it."""
    hwnd = find_game_window(title)
    if not hwnd:
        return {"hwnd": 0, "rect": None, "minimized": False}
    return {
        "hwnd": hwnd,
        "rect": read_game_rect(hwnd),
        "minimized": is_game_minimized(hwnd),
    }


def list_work_areas() -> List[RectTuple]:
    """Enumerate monitor work areas (supports negative coordinates)."""
    if os.name != "nt":
        return []
    areas: List[RectTuple] = []
    try:
        user32 = ctypes.windll.user32

        monitor_proc = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(_RECT),
            ctypes.c_void_p,
        )

        def _callback(hmonitor, _hdc, _rect, _data):
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            user32.GetMonitorInfoW.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_MONITORINFO),
            ]
            user32.GetMonitorInfoW.restype = ctypes.c_int
            if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                work = info.rcWork
                value = (
                    int(work.left),
                    int(work.top),
                    int(work.right),
                    int(work.bottom),
                )
                if _valid_rect(value):
                    areas.append(value)
            return 1

        callback = monitor_proc(_callback)
        user32.EnumDisplayMonitors.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            monitor_proc,
            ctypes.c_void_p,
        ]
        user32.EnumDisplayMonitors.restype = ctypes.c_int
        user32.EnumDisplayMonitors(None, None, callback, None)
    except Exception:
        areas = []

    if not areas:
        try:
            rect = _RECT()
            user32 = ctypes.windll.user32
            # SPI_GETWORKAREA = 0x0030
            if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                value = (
                    int(rect.left),
                    int(rect.top),
                    int(rect.right),
                    int(rect.bottom),
                )
                if _valid_rect(value):
                    areas.append(value)
        except Exception:
            pass
    return areas


# ---------------------------------------------------------------------------
# Own-window style: never touch the game window
# ---------------------------------------------------------------------------

WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
NOACTIVATE_EXSTYLE = WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
GWL_EXSTYLE = -20
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


def _user32() -> Optional[Any]:
    if os.name != "nt":
        return None
    try:
        return ctypes.windll.user32
    except Exception:
        return None


def tk_window_handle(root: Any) -> int:
    """Best-effort HWND of the panel's own Tk toplevel; 0 when unknown."""
    for getter in ("wm_frame", "winfo_id"):
        try:
            value = getattr(root, getter)()
        except Exception:
            continue
        try:
            hwnd = int(str(value), 0) if isinstance(value, str) else int(value)
        except (TypeError, ValueError):
            continue
        if hwnd:
            return hwnd
    return 0


def place_panel_noactivate(
    hwnd: int, *, show: bool = False, topmost: bool = True
) -> bool:
    """Show / raise *our own* window without activating it.

    Only ``hwnd`` (the panel itself) is passed to SetWindowPos; the game window
    is never moved, raised, focused or activated.  Returns False on non-Windows
    or on any failure so callers can simply continue.
    """
    if os.name != "nt" or not hwnd:
        return False
    user32 = _user32()
    if user32 is None:
        return False
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
    if show:
        flags |= SWP_SHOWWINDOW
    after: Any = HWND_TOPMOST if topmost else None
    try:
        if after is None:
            flags |= SWP_NOZORDER
        result = user32.SetWindowPos(
            ctypes.c_void_p(int(hwnd)),
            None if after is None else ctypes.c_void_p(after),
            0,
            0,
            0,
            0,
            flags,
        )
        return bool(result)
    except Exception:
        return False


def make_panel_non_activating(hwnd: int) -> bool:
    """Mark our own panel as ``WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW``.

    A non-activating window still receives mouse clicks (buttons and dragging
    keep working) but never takes foreground away from the game.  The game
    window is not inspected or modified in any way.
    """
    if os.name != "nt" or not hwnd:
        return False
    user32 = _user32()
    if user32 is None:
        return False
    try:
        get_long = getattr(user32, "GetWindowLongPtrW", None) or getattr(
            user32, "GetWindowLongW"
        )
        set_long = getattr(user32, "SetWindowLongPtrW", None) or getattr(
            user32, "SetWindowLongW"
        )
        handle = ctypes.c_void_p(int(hwnd))
        current = int(get_long(handle, GWL_EXSTYLE) or 0)
        wanted = current | NOACTIVATE_EXSTYLE
        if wanted != current:
            set_long(handle, GWL_EXSTYLE, wanted)
        place_panel_noactivate(hwnd, show=False, topmost=True)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Background polling
# ---------------------------------------------------------------------------


class StatusPoller(threading.Thread):
    """Poll a status source off the Tk thread and put results in a queue."""

    def __init__(
        self,
        source: Any,
        output: "queue.Queue[Tuple[str, Any]]",
        interval: float = POLL_INTERVAL,
    ) -> None:
        super().__init__(name="autofish-ui-poll", daemon=True)
        self._source = source
        self._output = output
        try:
            self._interval = max(0.05, float(interval))
        except (TypeError, ValueError):
            self._interval = POLL_INTERVAL
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                result = self._source.status()
            except Exception as exc:  # noqa: BLE001 - shown in the panel
                result = {
                    "ok": False,
                    "snapshot": None,
                    "message": "%s: %s" % (type(exc).__name__, exc),
                }
            if self._stop_event.is_set():
                break
            self._output.put(("status", result))
            if self._stop_event.wait(self._interval):
                break


# ---------------------------------------------------------------------------
# Tk panel
# ---------------------------------------------------------------------------


class AutoFishPanel:
    """Light 320px panel.  All socket work happens on worker threads."""

    BG = "#f7f7f5"
    CARD = "#ffffff"
    FG = "#1f2328"
    MUTED = "#6b7280"
    BORDER = "#d9dde3"
    OK = "#1a7f37"
    WARN = "#b45309"
    BAD = "#c62828"
    ACCENT = "#0b5cad"

    def __init__(
        self,
        root: Any,
        *,
        client: Optional[Any] = None,
        demo: bool = False,
        endpoint_path: Optional[Union[str, Path]] = None,
        hotkey_api: Optional[Any] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        import tkinter as tk

        self.tk = tk
        self.root = root
        self.demo = bool(demo)
        self._client = None
        if not self.demo:
            self._client = client or RuntimeClient(endpoint_path, timeout=0.9)

        self._output: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self._poller: Optional[StatusPoller] = None
        self._source: Any = DemoSource() if self.demo else self._client
        self._closing = False
        self._following = True
        self._hidden_for_minimize = False
        self._drag_origin: Optional[Tuple[int, int, int, int]] = None
        self._drag_moved = False
        self._last_snapshot: Mapping[str, Any] = {}
        self._last_view: Optional[ViewModel] = None
        self._buttons: Dict[str, Any] = {}
        self._pending_acks: Dict[str, str] = {}
        self._own_hwnd = 0
        self._noactivate_style = False
        self._style_checked = False
        self._clock = clock or time.monotonic
        self._hotkey_api = hotkey_api
        self._hotkeys: Optional[hotkeys.HotkeyListener] = None
        self._hotkey_lifecycle = 0
        self._hotkey_report: Mapping[str, Any] = {}
        self._pending_resume: Optional[Any] = None
        self._resume_deadline = 0.0
        self._resume_release_timeout = RESUME_RELEASE_TIMEOUT
        self._resume_poll_ms = RESUME_RELEASE_POLL_MS
        self._hotkey_max_age = HOTKEY_MAX_AGE

        self._build()
        self._apply_own_window_style()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_output)
        self.root.after(250, self._layout_tick)
        self._apply_follow()
        self._apply_view(
            build_view({}, {"ok": False, "message": "等待后台状态…"})
        )

    # -- construction ---------------------------------------------------
    def _build(self) -> None:
        tk = self.tk
        root = self.root
        root.title("RF4 自动钓鱼")
        root.configure(bg=self.BG)
        root.geometry(_geometry_string(PANEL_WIDTH, PANEL_HEIGHT, 120, 120))
        root.minsize(PANEL_WIDTH, 560)
        root.resizable(False, True)

        header = tk.Frame(root, bg=self.BG, padx=10, pady=3)
        header.pack(fill="x")
        title = tk.Label(
            header,
            text="RF4 自动钓鱼",
            bg=self.BG,
            fg=self.FG,
            font=("Microsoft YaHei UI", 14, "bold"),
            anchor="w",
        )
        title.pack(side="left")
        if self.demo:
            tk.Label(
                header,
                text="演示数据",
                bg=self.BG,
                fg=self.WARN,
                font=("Microsoft YaHei UI", 12),
            ).pack(side="right")
        for widget in (header, title):
            self._bind_drag(widget)

        self.status_var = tk.StringVar(value="状态：● 等待状态")
        self.status_label = tk.Label(
            root,
            textvariable=self.status_var,
            bg=self.BG,
            fg=self.MUTED,
            font=("Microsoft YaHei UI", 13, "bold"),
            anchor="w",
            padx=10,
        )
        self.status_label.pack(fill="x")
        self._bind_drag(self.status_label)

        body = tk.Frame(root, bg=self.CARD, padx=8, pady=4)
        body.pack(fill="x", padx=8, pady=(2, 0))
        body.grid_columnconfigure(1, weight=1)

        self._row_vars = []
        for row, (label_text, attrs, separator) in enumerate(_FIELD_ROWS):
            tk.Label(
                body,
                text=label_text,
                bg=self.CARD,
                fg=self.MUTED,
                font=("Microsoft YaHei UI", 12),
                anchor="w",
                width=12,
            ).grid(row=row, column=0, sticky="nw")
            variable = tk.StringVar(value=MISSING)
            self._row_vars.append((variable, attrs, separator))
            tk.Label(
                body,
                textvariable=variable,
                bg=self.CARD,
                fg=self.FG,
                font=("Microsoft YaHei UI", 12),
                anchor="w",
                justify="left",
                wraplength=195,
                height=2 if attrs == ("reason",) else 1,
            ).grid(row=row, column=1, sticky="we")

        tk.Label(
            root,
            text="发送账，非游戏确认",
            bg=self.BG,
            fg=self.MUTED,
            font=("Microsoft YaHei UI", 12),
            anchor="w",
            padx=10,
        ).pack(fill="x")

        self.ack_var = tk.StringVar(value="回执：—")
        tk.Label(
            root,
            textvariable=self.ack_var,
            bg=self.BG,
            fg=self.ACCENT,
            font=("Microsoft YaHei UI", 12),
            anchor="w",
            justify="left",
            wraplength=300,
            height=2,
            padx=10,
            pady=1,
        ).pack(fill="x")

        self.hotkey_var = tk.StringVar(value="热键：—")
        self.hotkey_label = tk.Label(
            root,
            textvariable=self.hotkey_var,
            bg=self.BG,
            fg=self.MUTED,
            font=("Microsoft YaHei UI", 12),
            anchor="w",
            justify="left",
            wraplength=300,
            padx=10,
            pady=0,
        )
        self.hotkey_label.pack(fill="x")

        buttons = tk.Frame(root, bg=self.BG, padx=6, pady=2)
        buttons.pack(fill="x")
        for column in (0, 1):
            buttons.grid_columnconfigure(column, weight=1)

        specs = (
            ("start_background", "启动后台（离线时）", self._start_background),
            ("resume", "开始/恢复自动", lambda: self._send_command("resume")),
            ("observe", "仅观察", lambda: self._send_command("observe")),
            ("stop", "立即停止", lambda: self._send_command("stop")),
            (
                "stop_after_fish",
                "本条结束后停",
                lambda: self._send_command("stop_after_fish"),
            ),
            ("confirm_ready", "确认钓具就绪", lambda: self._send_command("confirm_ready")),
            (
                "confirm_net_retracted",
                "我已手动收网",
                lambda: self._send_command("confirm_net_retracted"),
            ),
            ("emergency_stop", "紧急停止", self._emergency_stop),
        )
        for index, (name, text, command) in enumerate(specs):
            button = tk.Button(
                buttons,
                text=hotkeys.with_hotkey_label(name, text),
                command=command,
                font=("Microsoft YaHei UI", 12),
                bg=self.CARD,
                fg=self.BAD if name == "emergency_stop" else self.FG,
                activebackground="#e8eef7",
                activeforeground=self.FG,
                relief="groove",
                bd=1,
                padx=2,
                pady=2,
                highlightthickness=0,
            )
            button.grid(
                row=index // 2,
                column=index % 2,
                sticky="we",
                padx=2,
                pady=1,
            )
            self._buttons[name] = button

        footer = tk.Frame(root, bg=self.BG, padx=10, pady=0)
        footer.pack(fill="x")
        self.follow_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            footer,
            text="跟随游戏",
            variable=self.follow_var,
            command=self._on_follow_toggle,
            bg=self.BG,
            fg=self.FG,
            activebackground=self.BG,
            activeforeground=self.FG,
            selectcolor=self.CARD,
            font=("Microsoft YaHei UI", 12),
            anchor="w",
        ).pack(side="left")
        self.note_var = tk.StringVar(value="")
        tk.Label(
            footer,
            textvariable=self.note_var,
            bg=self.BG,
            fg=self.MUTED,
            font=("Microsoft YaHei UI", 12),
            anchor="e",
            wraplength=150,
        ).pack(side="right", fill="x", expand=True)

    def _bind_drag(self, widget: Any) -> None:
        widget.bind("<ButtonPress-1>", self._drag_press, add="+")
        widget.bind("<B1-Motion>", self._drag_motion, add="+")
        widget.bind("<ButtonRelease-1>", self._drag_release, add="+")

    # -- lifecycle ------------------------------------------------------
    def run(self) -> None:
        self._start_hotkeys()
        self._poller = StatusPoller(self._source, self._output, POLL_INTERVAL)
        self._poller.start()
        try:
            self.root.mainloop()
        finally:
            self._stop_hotkeys()

    def _on_close(self) -> None:
        self._closing = True
        self._cancel_pending_resume()
        self._stop_hotkeys()
        if self._poller is not None:
            self._poller.stop()
        # The bot is an independent process; closing the panel never stops it.
        self.root.destroy()

    # -- polling / rendering -------------------------------------------
    def _drain_output(self) -> None:
        if self._closing:
            return
        try:
            while True:
                kind, payload = self._output.get_nowait()
                if kind == "status":
                    self._handle_status(payload)
                elif kind == "ack":
                    name, ack = payload
                    self._handle_ack(name, ack)
                elif kind == "notice":
                    self._set_ack(str(payload))
                elif kind == "hotkeys":
                    self._apply_hotkey_report(payload)
                elif kind == "hotkey":
                    self._on_hotkey(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_output)

    def _handle_status(self, status: Any) -> None:
        if not isinstance(status, Mapping):
            status = {"ok": False, "message": "bad status"}
        snapshot = status.get("snapshot")
        ok = _optional_bool(status.get("ok"))
        if ok is None:
            ok = isinstance(snapshot, Mapping)
        if ok and isinstance(snapshot, Mapping):
            self._last_snapshot = snapshot
        else:
            snapshot = {}
            self._last_snapshot = {}
        view = build_view(snapshot, status)
        self._apply_view(view)
        self._resolve_pending_acks(status)

    def _apply_view(self, view: ViewModel) -> None:
        self._last_view = view
        for variable, attrs, separator in self._row_vars:
            values = [str(getattr(view, attr, MISSING)) for attr in attrs]
            if separator is None:
                text = values[0]
            else:
                text = separator.join(values)
            if attrs in (("reason",), ("last_error",)):
                text = _ellipsis(text)
            variable.set(text)

        if not view.connected:
            detail = view.status_message[:24] if view.status_message else "后台未运行"
            self.status_var.set("状态：▲ 离线（%s）" % detail)
            self.status_label.configure(fg=self.BAD)
        elif view.stopped:
            self.status_var.set("状态：■ 已停止")
            self.status_label.configure(fg=self.WARN)
        else:
            self.status_var.set("状态：● %s" % _ellipsis(view.state, 18))
            self.status_label.configure(fg=self.OK)

        states = button_states(view)
        self._refresh_buttons(states)

    def _refresh_buttons(self, states: Optional[ButtonStates] = None) -> None:
        """Apply the same button gate to clicks and hotkeys."""
        if states is None:
            view = (
                self._last_view
                if self._last_view is not None
                else build_view({}, {"ok": False})
            )
            states = button_states(view)
        for name, button in self._buttons.items():
            enabled = bool(getattr(states, name, False))
            button.configure(
                state=self.tk.NORMAL if enabled else self.tk.DISABLED
            )

    def _set_ack(self, text: str) -> None:
        self.ack_var.set("回执：%s" % _ellipsis(text or MISSING, 44))

    def _handle_ack(self, name: str, ack: Any) -> None:
        """Remember accepted commands until the status reports a final result."""
        command_id = ack_command_id(ack)
        if ack_status(ack) == "accepted" and command_id:
            self._pending_acks[command_id] = name
            self._set_ack(
                "%s（待确认 %d）" % (ack_text(ack, name), len(self._pending_acks))
            )
            return
        if command_id:
            self._pending_acks.pop(command_id, None)
        self._set_ack(ack_text(ack, name))

    def _resolve_pending_acks(self, status: Mapping[str, Any]) -> None:
        """Show executed/rejected once the status exposes an ``acks`` field.

        The current RuntimeServer status response has no ``acks`` entry, so
        pending commands stay labelled "待确认" and the panel never invents a
        final outcome.
        """
        has_acks = status_acks(status) is not None
        previous = dict(self._pending_acks)
        resolved, remaining = resolve_pending_acks(status, previous)
        self._pending_acks = remaining
        if resolved:
            command_id, ack = list(resolved.items())[-1]
            name = previous.get(command_id, "")
            text = ack_text(ack, name)
            self._set_ack(text if not name else "%s（%s）" % (text, name))
            return
        if previous and not has_acks:
            self._set_ack(
                "已接收待执行 %d 条；IPC status 无 acks 字段，无法确认最终结果"
                % len(remaining)
            )

    # -- commands -------------------------------------------------------
    def _send_command(self, name: str) -> None:
        if self.demo:
            self._set_ack("演示模式：未发送命令")
            return
        payload = command_payload(name, self._last_snapshot)
        self._set_ack("已发送 %s，等待回执…" % name)
        thread = threading.Thread(
            target=self._command_worker,
            args=(name, payload),
            name="autofish-ui-cmd-%s" % name,
            daemon=True,
        )
        thread.start()

    def _command_worker(self, name: str, payload: Dict[str, Any]) -> None:
        try:
            ack = self._client.command(name, **payload)
        except Exception as exc:  # noqa: BLE001 - surfaced as an ack
            ack = {
                "ok": False,
                "status": "rejected",
                "message": "%s: %s" % (type(exc).__name__, exc),
            }
        self._output.put(("ack", (name, ack)))

    def _start_background(self) -> None:
        if self.demo:
            self._set_ack("演示模式：不启动后台")
            return
        self._launch_ctl("start", "已请求启动后台（不等待）")

    def _emergency_stop(self) -> None:
        if self.demo:
            self._set_ack("演示模式：不发送紧急停止")
            return
        from tkinter import messagebox

        confirmed = messagebox.askyesno(
            "确认紧急停止",
            "将请求 bot_ctl emergency-stop，让后台立即停止并释放按键。\n\n"
            "请确认当前游戏画面安全。是否继续？",
            parent=self.root,
        )
        if not confirmed:
            self._set_ack("已取消紧急停止")
            return
        if not _bot_ctl_has_action("emergency-stop"):
            self._set_ack("bot_ctl 未支持紧急停止（未发送）")
            return
        self._launch_ctl(
            "emergency-stop", "已请求紧急停止（bot_ctl emergency-stop）"
        )

    def _launch_ctl(self, action: str, notice: str) -> None:
        # 打包态：同一个 exe 用 --ctl 切角色，cwd 必须是可写数据目录
        # （bot_ctl 会把 pid/日志/endpoint 写在 cwd 所属的数据目录里）。
        if app_paths.is_frozen():
            cmd = [sys.executable, "--ctl", action]
            cwd = str(app_paths.DATA_DIR)
        else:
            cmd = [sys.executable, str(BOT_CTL), action]
            cwd = str(BOT_DIR)

        def worker() -> None:
            try:
                subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:  # noqa: BLE001 - shown in panel
                self._output.put(
                    ("notice", "启动失败：%s: %s" % (type(exc).__name__, exc))
                )
            else:
                self._output.put(("notice", notice))

        threading.Thread(
            target=worker, name="autofish-ui-ctl-%s" % action, daemon=True
        ).start()

    # -- global hotkeys -------------------------------------------------
    def _start_hotkeys(self) -> None:
        """Start the listener; demo / non-Windows never registers anything."""
        self._hotkey_lifecycle = 0
        if self.demo:
            self._apply_hotkey_report(
                {"enabled": False, "reason": "演示模式", "items": []}
            )
            return
        if not hotkeys.registration_supported():
            self._apply_hotkey_report(
                {"enabled": False, "reason": "非 Windows", "items": []}
            )
            return
        try:
            api = (
                self._hotkey_api
                if self._hotkey_api is not None
                else hotkeys.Win32HotkeyApi()
            )
            listener = hotkeys.HotkeyListener(
                self._output, api=api, clock=self._clock
            )
        except Exception as exc:  # noqa: BLE001 - shown in the panel
            self._apply_hotkey_report(
                {
                    "enabled": False,
                    "reason": "初始化失败：%s" % exc,
                    "items": [],
                }
            )
            return
        self._hotkey_api = api
        self._hotkeys = listener
        self._hotkey_lifecycle = listener.lifecycle
        self._set_hotkey_text("热键：注册中…")
        try:
            listener.start()
        except Exception as exc:  # noqa: BLE001 - registration just stays off
            self._hotkeys = None
            self._hotkey_lifecycle = 0
            self._apply_hotkey_report(
                {
                    "enabled": False,
                    "reason": "启动失败：%s" % exc,
                    "items": [],
                }
            )

    def _stop_hotkeys(self) -> None:
        """Invalidate queued events and let the listener unregister itself."""
        listener = self._hotkeys
        self._hotkeys = None
        self._hotkey_lifecycle = 0
        self._cancel_pending_resume()
        if listener is not None:
            listener.stop()

    def _set_hotkey_text(self, text: str) -> None:
        variable = getattr(self, "hotkey_var", None)
        if variable is None:
            return
        try:
            variable.set(text)
        except Exception:
            pass

    def _apply_hotkey_report(self, report: Any) -> None:
        if not isinstance(report, Mapping):
            return
        self._hotkey_report = dict(report)
        self._set_hotkey_text(hotkeys.format_report(report))
        items = report.get("items")
        failed = isinstance(items, (list, tuple)) and any(
            isinstance(item, Mapping) and not item.get("ok") for item in items
        )
        label = getattr(self, "hotkey_label", None)
        if label is not None:
            try:
                label.configure(fg=self.BAD if failed else self.MUTED)
            except Exception:
                pass

    def _on_hotkey(self, event: Any) -> None:
        """Tk-thread entry point for an event posted by the listener."""
        if self._closing or not isinstance(event, hotkeys.HotkeyEvent):
            return
        if not self._hotkey_lifecycle or event.lifecycle != self._hotkey_lifecycle:
            self._set_ack("热键已失效，忽略")
            return
        age = self._clock() - event.monotonic
        if age < 0 or age > self._hotkey_max_age:
            self._set_ack("热键已过期，忽略")
            return
        name = event.name
        if name == "start_background":
            # 只在"后台没连上"时允许热键启动，避免对已在跑的实例重复拉起。
            if self._background_online():
                self._set_ack("热键 F6：后台已在运行，无需启动")
                return
            if not self.demo and not _bot_ctl_has_action("start"):
                self._set_ack("热键 F6：bot_ctl 不支持 start（未启动）")
                return
            self._start_background()
        elif name == "resume":
            self._begin_resume_release_wait()
        elif name == "stop":
            # Stop is immediate: never wait for key release.
            self._cancel_pending_resume()
            self._dispatch_hotkey("stop")
        elif name == "observe":
            self._cancel_pending_resume()
            self._dispatch_hotkey("observe")
        elif name == "stop_after_fish":
            self._dispatch_hotkey("stop_after_fish")

    def _background_online(self) -> bool:
        """后台是否已在运行（能被 IPC 连上）。"""
        view = self._last_view
        return bool(view is not None and view.connected)

    def _dispatch_hotkey(self, name: str) -> bool:
        """Route a hotkey through the same gate as the button click."""
        view = self._last_view
        if view is None:
            view = build_view({}, {"ok": False, "message": "等待后台状态…"})
        states = button_states(view)
        if not bool(getattr(states, name, False)):
            self._set_ack(
                "热键 %s：当前不可用" % hotkeys.HOTKEY_KEYS.get(name, name)
            )
            self._refresh_buttons(states)
            return False
        self._send_command(name)
        return True

    def _begin_resume_release_wait(self) -> None:
        """Wait (via ``after``) until Ctrl+Alt+Shift+F7 is fully released."""
        self._cancel_pending_resume()
        if self._last_view is not None and not button_states(self._last_view).resume:
            # Nothing to send: report through the same gate instead of waiting.
            self._dispatch_hotkey("resume")
            return
        if self._resume_keys_released():
            self._dispatch_hotkey("resume")
            return
        self._resume_deadline = self._clock() + self._resume_release_timeout
        self._pending_resume = self.root.after(
            self._resume_poll_ms, self._poll_resume_release
        )
        self._set_ack("热键 F7：等待 Ctrl+Alt+Shift+F7 释放…")

    def _poll_resume_release(self) -> None:
        self._pending_resume = None
        if self._closing:
            return
        if self._resume_keys_released():
            self._dispatch_hotkey("resume")
            return
        if self._clock() >= self._resume_deadline:
            self._set_ack("热键 F7：等待按键释放超时，已取消")
            self._refresh_buttons()
            return
        self._pending_resume = self.root.after(
            self._resume_poll_ms, self._poll_resume_release
        )

    def _cancel_pending_resume(self) -> None:
        pending = self._pending_resume
        self._pending_resume = None
        self._resume_deadline = 0.0
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except Exception:
                pass

    def _resume_keys_released(self) -> bool:
        if self._hotkey_api is None:
            return True
        try:
            return not hotkeys.resume_combo_down(self._hotkey_api)
        except Exception:
            return True

    # -- own window style ------------------------------------------------
    def _apply_own_window_style(self) -> None:
        """Make our own window non-activating so clicks never steal game focus.

        Only the panel HWND is touched; the game window keeps foreground,
        keyboard focus and its own styles untouched.  A no-op off Windows.
        """
        if os.name != "nt" or self._own_hwnd:
            self._style_checked = True
            return
        hwnd = tk_window_handle(self.root)
        if not hwnd:
            self._style_checked = False
            return
        self._own_hwnd = hwnd
        self._noactivate_style = make_panel_non_activating(hwnd)
        self._style_checked = True

    def _show_panel(self) -> None:
        """Show/raise the panel without activating it or the game."""
        self.root.deiconify()
        if self._own_hwnd:
            place_panel_noactivate(self._own_hwnd, show=True, topmost=True)

    # -- follow / drag --------------------------------------------------
    def _on_follow_toggle(self) -> None:
        self._following = bool(self.follow_var.get())
        if self._following:
            self._hidden_for_minimize = False
            self._apply_follow()
        else:
            if self._hidden_for_minimize:
                self._show_panel()
                self._hidden_for_minimize = False
            self.note_var.set("已取消跟随")

    def _layout_tick(self) -> None:
        if self._closing:
            return
        if not self._style_checked:
            self._apply_own_window_style()
        if self._following:
            self._apply_follow()
        self.root.after(500, self._layout_tick)

    def _apply_follow(self) -> None:
        if not self._following or self._closing:
            return
        try:
            state = read_game_state()
            areas = list_work_areas()
            if not state.get("hwnd"):
                if self._hidden_for_minimize:
                    self._show_panel()
                    self._hidden_for_minimize = False
                self._place(areas, None)
                self.note_var.set("游戏未运行")
                return
            if state.get("minimized"):
                if not self._hidden_for_minimize:
                    self.root.withdraw()
                    self._hidden_for_minimize = True
                self.note_var.set("游戏最小化")
                return
            if self._hidden_for_minimize:
                self._show_panel()
                self._hidden_for_minimize = False
            rect = state.get("rect")
            self._place(areas, rect)
            self.note_var.set("" if rect else "位置未知")
        except Exception as exc:  # noqa: BLE001 - layout must never kill Tk
            self.note_var.set("布局异常")

    def _place(self, areas: Sequence[RectTuple], rect: Any) -> None:
        x, y = place_panel(
            rect, areas, PANEL_WIDTH, PANEL_HEIGHT, PANEL_MARGIN
        )
        self.root.geometry(
            _geometry_string(PANEL_WIDTH, PANEL_HEIGHT, x, y)
        )

    def _drag_press(self, event: Any) -> None:
        self._drag_origin = (
            event.x_root,
            event.y_root,
            self.root.winfo_x(),
            self.root.winfo_y(),
        )
        self._drag_moved = False

    def _drag_motion(self, event: Any) -> None:
        if self._drag_origin is None:
            return
        start_x, start_y, window_x, window_y = self._drag_origin
        delta_x = event.x_root - start_x
        delta_y = event.y_root - start_y
        if abs(delta_x) + abs(delta_y) < 3 and not self._drag_moved:
            return
        self._drag_moved = True
        if self._following:
            self._following = False
            self.follow_var.set(False)
            self.note_var.set("已拖动，不跟随")
        x = window_x + delta_x
        y = window_y + delta_y
        self.root.geometry(
            _geometry_string(PANEL_WIDTH, PANEL_HEIGHT, x, y)
        )

    def _drag_release(self, _event: Any) -> None:
        self._drag_origin = None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> Any:
    import argparse

    parser = argparse.ArgumentParser(description="RF4 autofish side panel")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="use local fake snapshots; never opens IPC or starts the bot",
    )
    parser.add_argument(
        "--headless-smoke",
        action="store_true",
        help="exercise the display/button model without creating a window",
    )
    parser.add_argument(
        "--endpoint",
        default=None,
        help="optional path to autofish.endpoint.json",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def headless_smoke() -> int:
    """Model smoke test: no Tk, no socket, no child process."""
    status = demo_status(18)
    view = build_view(status["snapshot"], status)
    for line in render_lines(view):
        print(line)
    states = button_states(view)
    print(
        "buttons: resume=%s observe=%s stop=%s stop_after_fish=%s "
        "confirm_ready=%s confirm_net=%s"
        % (
            states.resume,
            states.observe,
            states.stop,
            states.stop_after_fish,
            states.confirm_ready,
            states.confirm_net_retracted,
        )
    )
    x, y = place_panel(
        (100, 100, 900, 700), [(0, 0, 1920, 1080)], PANEL_WIDTH, PANEL_HEIGHT, 8
    )
    print("panel: %d,%d" % (x, y))
    if "29" not in view.cur:
        raise AssertionError("cur cap 29 missing from view")
    if view.ring != "0.42":
        raise AssertionError("demo ring missing")
    if not states.confirm_net_retracted:
        raise AssertionError("NET state + pending should enable manual net confirm")
    if states.confirm_ready:
        raise AssertionError("confirm_ready must stay disabled outside PROBING/READY")
    if view.mode != "AUTO" or view.state_raw != "NET":
        raise AssertionError("demo must mirror the production NET/AUTO snapshot shape")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.headless_smoke:
        return headless_smoke()

    enable_dpi_awareness()
    try:
        import tkinter as tk
    except ImportError as exc:
        print(
            "无法加载 tkinter：%s。请改用带 tkinter 的 Python 运行。" % exc,
            file=sys.stderr,
        )
        return 2

    root = tk.Tk()
    panel = AutoFishPanel(
        root,
        demo=bool(args.demo),
        # 打包后模块目录是临时解包目录，绝不能用来找 endpoint；
        # 统一走数据目录，和 bot_ctl 写出的位置保持一致。
        endpoint_path=args.endpoint
        or str(app_paths.data_path("autofish.endpoint.json")),
    )
    panel.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
