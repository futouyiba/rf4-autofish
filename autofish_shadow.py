"""RF4 shadow 状态观测（阶段 1 收尾）：只读、只打日志、不参与控制。

分工：
  * 画面由调用方（适配层）抓好后传进来，本模块**不碰窗口、不碰帧源**；
  * `autofish_observation.observe_frame` 把同一帧拆成事实；
  * `autofish_state.ShadowStateTracker` 做连续帧防抖，给稳定状态；
    调用点的 `source` 会作为动作上下文（wait_bite/pull/lift/cast_*/settle_*）
    原样传给它 —— 同样的像素在不同阶段含义不同，source 不是日志装饰；
  * 本模块只决定"这一帧要不要打日志"，以及"打什么"。

三条硬约束（改动前请先读）：

1. `update()` 的返回值只供日志/调试，**任何调用方都不许拿它做判断**。
2. 任何异常都吞掉并降级：同一类异常前 3 次各打一条 `[STATE][warn]`，
   之后只累加计数、静默返回 None。
3. 不写文件、不起线程、不派发键鼠、不读环境以外的外部状态。
   环境变量 `RF4_SHADOW_STATE=0` 可整体关闭（默认开启）。

日志节流：只在三种情况输出 ——
  1. 稳定状态发生切换（changed）；
  2. pending 候选或其帧数变化；
  3. heartbeat（默认 10 秒，`RF4_SHADOW_HEARTBEAT_S` 或构造参数可调）。

采样间隔：本模块只负责解析 `RF4_SHADOW_SAMPLE_S`（默认 0.5 秒），
真正的全局限频在适配层 `rf4s_adapter._shadow_observe` 里做（抓图之前判断）。
"""

from __future__ import annotations

import math
import os
import time

from autofish_observation import observe_frame
from autofish_state import ShadowStateTracker

__all__ = [
    "PREFIX",
    "DEFAULT_HEARTBEAT",
    "DEFAULT_SAMPLE",
    "WARN_LIMIT",
    "shadow_enabled",
    "heartbeat_from_env",
    "sample_interval_from_env",
    "ShadowObserver",
]

PREFIX = "[STATE]"

ENV_SWITCH = "RF4_SHADOW_STATE"
ENV_HEARTBEAT = "RF4_SHADOW_HEARTBEAT_S"
ENV_SAMPLE = "RF4_SHADOW_SAMPLE_S"

DEFAULT_HEARTBEAT = 10.0
DEFAULT_SAMPLE = 0.5          # 全局限频：最多 2Hz 采样
WARN_LIMIT = 3

_FALSY = frozenset({"0", "false", "off", "no", "disable", "disabled"})


def _stdout(line: str) -> None:
    print(line, flush=True)


def shadow_enabled(environ=None) -> bool:
    """读 `RF4_SHADOW_STATE`。缺省开启；只有明确写成假值才关闭。"""
    env = os.environ if environ is None else environ
    raw = env.get(ENV_SWITCH)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSY


def _positive_float_from_env(env, key: str, default: float) -> float:
    """读环境变量里的有限正数秒数。缺失/非法/NaN/Inf/非正数一律回落 default。"""
    raw = env.get(key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return value


def heartbeat_from_env(environ=None) -> float:
    """心跳间隔（秒）。非法值/非正数一律回落到默认 10 秒。"""
    env = os.environ if environ is None else environ
    return _positive_float_from_env(env, ENV_HEARTBEAT, DEFAULT_HEARTBEAT)


def sample_interval_from_env(environ=None) -> float:
    """采样最小间隔（秒）。非法值/非正数一律回落到默认 0.5 秒（全局限 2Hz）。"""
    env = os.environ if environ is None else environ
    return _positive_float_from_env(env, ENV_SAMPLE, DEFAULT_SAMPLE)


class ShadowObserver:
    """把一帧图像变成一行日志。所有失败都降级为 None。

    `clock` / `sink` 只为测试注入：默认取 `time.monotonic` 和标准输出。
    """

    def __init__(self, *, enabled=None, heartbeat=None, clock=None, sink=None):
        self._clock = clock or time.monotonic
        self._sink = sink or _stdout
        self.enabled = shadow_enabled() if enabled is None else bool(enabled)
        self.heartbeat = (heartbeat_from_env() if heartbeat is None
                          else float(heartbeat))
        if not self.heartbeat > 0:
            self.heartbeat = DEFAULT_HEARTBEAT

        self._tracker = ShadowStateTracker() if self.enabled else None
        self._pending_key = None          # (候选名, 帧数) 或 None
        self._last_log_ts = None
        self._errors = 0
        self._warned = {}                 # 异常类型名 -> 已打印次数
        self._suppressed = {}             # 异常类型名 -> 被压掉的次数
        self.updates = 0                  # 成功吃进去的帧数（只读统计）

    # ---------------- 对外唯一入口 ----------------

    def update(self, image, foreground: bool, source: str = ""):
        """吃一帧，必要时打一行日志。返回 StateEstimate 或 None。

        `source` 是**调用点的动作上下文**（wait_bite/pull/lift/cast_*/settle_*），
        原样交给状态机参与候选判定，并写进日志。它不是像素推断出来的。

        返回值**仅供调用方记录/调试**；不要用它改变按键、等待或阶段。
        """
        if not self.enabled:
            return None

        try:
            obs = observe_frame(image, foreground=bool(foreground))
            estimate = self._tracker.update(obs, context=source)
        except BaseException as exc:              # noqa: BLE001 - 必须全吞
            if isinstance(exc, KeyboardInterrupt):
                raise
            self._warn(exc)
            return None

        self.updates += 1
        try:
            self._log(estimate, source)
        except BaseException as exc:              # noqa: BLE001 - 日志也不能炸
            if isinstance(exc, KeyboardInterrupt):
                raise
            self._warn(exc)
        return estimate

    # ---------------- 内部：日志 ----------------

    def _log(self, estimate, source: str) -> None:
        now = self._clock()
        evidence = estimate.evidence
        pending = evidence.get("pending")
        key = None if pending is None else (pending.get("state"),
                                            pending.get("frames"))

        changed = bool(estimate.changed)
        first = self._last_log_ts is None
        heartbeat = first or (now - self._last_log_ts) >= self.heartbeat
        fire = changed or heartbeat or key != self._pending_key

        self._pending_key = key
        if not fire:
            return
        self._last_log_ts = now
        self._emit(self._format(estimate, source, key, changed))

    @staticmethod
    def _format(estimate, source: str, key, changed: bool) -> str:
        evidence = estimate.evidence or {}
        obs = evidence.get("observation") or {}
        ring = obs.get("ring_ratio")
        ring_s = "-" if ring is None else f"{ring:.2f}"
        tag = "changed" if changed else ("pending" if key is not None else "hold")
        return (
            f"{PREFIX} {tag} stable={estimate.state.value} "
            f"cand={evidence.get('candidate', '?')} "
            f"{evidence.get('candidate_frames', '?')}/{evidence.get('need_frames', '?')} "
            f"conf={estimate.confidence:.2f} "
            f"fish={obs.get('fish_icon', '?')} "
            f"bar={obs.get('bar_level')}/{obs.get('bar_tier')} "
            f"ring={ring_s} "
            f"source={source or '-'} "
            f"ctx={evidence.get('context') or '-'} | "
            f"reason={estimate.reason}"
        )

    # ---------------- 内部：异常降级 ----------------

    def _warn(self, exc: BaseException) -> None:
        self._errors += 1
        kind = type(exc).__name__
        seen = self._warned.get(kind, 0)
        if seen >= WARN_LIMIT:
            self._suppressed[kind] = self._suppressed.get(kind, 0) + 1
            return
        self._warned[kind] = seen + 1
        try:
            self._sink(f"{PREFIX}[warn] {kind}: {exc} "
                       f"（第 {seen + 1}/{WARN_LIMIT} 次，之后同类静默）")
        except Exception:
            pass

    def _emit(self, line: str) -> None:
        try:
            self._sink(line)
        except Exception:
            pass

    # ---------------- 只读统计（调试/测试用） ----------------

    @property
    def errors(self) -> int:
        return self._errors

    @property
    def suppressed(self) -> dict:
        return dict(self._suppressed)
