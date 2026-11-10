# -*- coding: utf-8 -*-
"""正式全流程桥接入口（独立于上游 ``M.main()`` / ``Player.loop``）。

本模块把 :mod:`autofish_lifecycle` 的纯状态机接到真实客户端上：

* 抓帧：每次观测只调用 **一次** ``rf4s_adapter.capture_game()``，同一张图
  既做 ``observe_frame`` 又做 ``_fight_strong_capture(keep.png)``；
* 输入：唯一 owner 是协调器的 :class:`autofish_input.InputArbiter`。所有
  F5/F6/Shift/F7 都通过 ``set_desired`` + ``reconcile`` 发送，sink
  只负责 ``pydirectinput.keyDown/keyUp``；
* 摩擦：校准用安全的「先降到底、再升回 17」方案；动态控制暂时替换
  ``A._wheel_raw`` 为可取消版本，仍由 ``A._wheel`` 记账；
* 收线/入护/弹窗：全部通过注入回调执行，每个等待点都可被 stop 打断。

**导入本模块没有任何游戏副作用**：``rf4s_adapter`` 只在 :func:`main`
（或显式调用 :func:`_load_adapter`）里惰性导入。真实抓屏/按键/网络监听
只发生在 ``main()`` 运行之后。
"""

from __future__ import annotations

import argparse
import ctypes
import dataclasses
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import autofish_observation as aobs
from autofish_control import AutoStop, ControlSession

__all__ = [
    "Bridge",
    "BridgeFault",
    "RetrieveBlocked",
    "TARGET_W",
    "TARGET_H",
    "resolve_auto_net_enabled",
    "main",
]

BOT_DIR = Path(__file__).resolve().parent
STATIC_DIR = BOT_DIR / "static"
DEFAULT_PIDFILE = BOT_DIR / "run.pid"
READY_CN_PATH = STATIC_DIR / "ready_cn.png"

TARGET_W, TARGET_H = 1920, 1080

# 圆环 >= 该值、且 ready 模板不命中时，才敢认定"线还在杯上、不必收线"。
#
# 注意这里**故意比 lifecycle.PROBE_LINE_OUT_MAX_RING（0.97）更保守**：
# 两者语义不同，不能合并成一个数。
#   * lifecycle 用它判"线是不是已经出去了"——宁宽，短抛（0.983）也要算出去；
#   * retrieve 用它决定"要不要按 F5"——宁严，因为线在杯上时 F5 是蓄力键。
# 实测：入护收回 ≈1.0，短抛 ≈0.983。取 0.99 只拦"几乎满环"，短抛仍会正常收线。
LINE_ON_SPOOL_RING = 0.99


def resolve_auto_net_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    """生产自动抄网的**唯一开关来源**。

    默认 False。只有显式设置 ``RF4_AUTO_NET=1`` 才打开，且仅用于人工
    实机验收；未完成实机验收前不得无人值守启用。Bridge gate 与
    ``FightConfig.auto_net_enabled`` 必须由同一个返回值构造。
    """
    env = os.environ if environ is None else environ
    value = str(env.get("RF4_AUTO_NET", "")).strip()
    return value == "1"


F5 = "f5"
F6 = "f6"
SHIFT = "shift"
F7 = "f7"
# 正式抄网键：实机确认 F7 才是「使用/收起抄网」，PageDown 会放竿/切装备。
NET_KEY = F7
# 仅保留给旧版本残留键的显式清理（bot_ctl release / 文档）；正式 NET 动作
# 不再发送 pagedown，也不把 pagedown 纳入 bridge 可拥有的键。
PAGEDOWN = "pagedown"
ALLOWED_KEYS = frozenset({F5, F6, SHIFT, F7})

STRONG_CAPTURE_CONFIDENCE = 0.95
KEEPNET_CONFIDENCE = 0.90

# 底部固定 ROI + 中文“钓具准备好,可以抛掷一下”模板（1920x1080 实测坐标）。
# 模板来自 _fullflow_fault.png (532,1017)-(704,1044)；匹配只在该 ROI 内做，
# 位置偏离锚点视为未命中，避免满环/水花等相似纹理误报。
#
# 判据历次实测（Sobel 边缘 NCC）：
#   提示在：0.9837（正对水面） / 0.856~0.881（相机倾斜、文字淡入）
#   提示不在：0.17 / 0.00（黑屏）
# 所以阈值取 0.72：留足相机姿态带来的衰减余量，又远高于任何误报样本。
READY_ROI = (470, 995, 790, 1060)
READY_CN_ANCHOR = (532, 1017)
READY_CN_POS_TOL = (24, 12)
READY_CN_THRESHOLD = 0.72
READY_CN_MIN_STD = 8.0

WHEEL_DELTA = 120
WHEEL_GAP = 0.055
KEEP_SETTLE_WAIT = 0.8
DISMISS_MAX_CLICKS = 12
DISMISS_GAP = 0.8

RETRIEVE_TIMEOUT = 45.0
RETRIEVE_RING_MIN = 0.98
RETRIEVE_RING_FRAMES = 3
RETRIEVE_POLL = 0.1
RETRIEVE_READY_FRAMES = 2
STATUS_LOG_INTERVAL = 1.0

CAST_SHIFT_SETTLE = 0.08
CAST_CHARGE_HOLD = 1.0
# 释放后允许 ready 提示残留的最短时间：这可能只是抛竿动画帧，不能立刻判失败。
CAST_RELEASE_GRACE = 0.25
# 释放/关线杯后最多等这么久来确认“ready 持续存在”或“落水正证据”。
CAST_CLOSE_DELAY = 5.0
CAST_CONFIRM_TIMEOUT = 5.0
CAST_CONFIRM_FRAMES = 2
CAST_POLL = 0.1
# 抛竿后圆环相对抛前的变化量。实测短抛只出线 1.7%（1.0 → 0.983），
# 原阈值 0.02 会判成"没抛出去"进而 FAULT——但鱼其实已经咬口
# （2026-11-09 实机事故）。环读数的量化步长约 0.0067，取 0.012：
# 覆盖 1.4% 以上的短抛，同时不会被单帧量化噪声触发。
CAST_RING_DELTA = 0.012

# 前台恢复：只在 AUTO 请求下、每次动作/抓帧最多尝试一次，且两次尝试之间
# 至少间隔 1 秒。连续失败达到上限后不再抢窗口，留给 BLOCKED/人工处理。
FOREGROUND_RECOVERY_INTERVAL = 1.0
FOREGROUND_RECOVERY_LIMIT = 2
FOREGROUND_LOG_INTERVAL = 5.0

T = aobs.TriState


def _default_log(text: str) -> None:
    print(text, flush=True)


class BridgeFault(RuntimeError):
    """桥接层明确的故障（模板不可用、窗口尺寸不对等）。"""


class RetrieveBlocked(RuntimeError):
    """空线收线在限时内无法确认完成；上层必须停止而不是盲抛。"""


def _load_adapter():
    """惰性导入真实适配器。

    注意：``rf4s_adapter`` 导入时会 ``os.chdir(REF_DIR)``，所以调用方必须
    在导入前把所有路径解析成绝对路径。本模块只在 :func:`main` 里调用它。
    """
    import rf4s_adapter as adapter  # noqa: PLC0415 - 必须保持惰性导入

    return adapter


def _resolve_static_path(adapter: Any, name: str) -> Optional[Path]:
    """在 ``REF_DIR/static/<lang>`` 里找模板，优先 en，再任意语言。"""
    try:
        ref = Path(adapter.REF_DIR).resolve()
    except Exception:  # noqa: BLE001 - 适配器不可用时没有模板
        return None
    candidates = [ref / "static" / lang / name for lang in ("en", "ru")]
    try:
        candidates.extend(sorted((ref / "static").glob("*/" + name)))
    except Exception:  # noqa: BLE001
        pass
    seen = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _read_client_size(hwnd: int) -> Optional[tuple[int, int]]:
    """只读客户区尺寸，绝不移动/缩放窗口。"""
    try:
        from ctypes import wintypes

        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        return int(rect.right), int(rect.bottom)
    except Exception:  # noqa: BLE001 - 读不到 = 预检失败
        return None


class Bridge:
    """把纯运行层回调接到真实适配器；构造函数本身不抓屏、不按键。"""

    def __init__(
        self,
        adapter: Any = None,
        *,
        control: Optional[ControlSession] = None,
        pdi: Any = None,
        hwnd: Optional[int] = None,
        keep_path: Optional[Path] = None,
        ready_path: Optional[Path] = None,
        ready_cn_path: Optional[Path] = None,
        keepnet_path: Optional[Path] = None,
        preflight_error: Optional[str] = None,
        retrieve_lift: bool = False,
        auto_net_enabled: Optional[bool] = None,
        fight_auto_net_enabled: Optional[bool] = None,
        wheel_gap: float = WHEEL_GAP,
        log: Optional[Callable[[str], Any]] = None,
        foreground: Optional[Callable[[], bool]] = None,
        clock: Optional[Callable[[], float]] = None,
        wheel_sender: Optional[Callable[[int], Any]] = None,
        key_state: Optional[Callable[[str], Optional[bool]]] = None,
        focus: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.A = adapter
        self.control = control if control is not None else ControlSession()
        self.pdi = pdi
        self.hwnd = hwnd
        self.keep_path = Path(keep_path) if keep_path is not None else None
        self.ready_path = Path(ready_path) if ready_path is not None else None
        self.ready_cn_path = (
            Path(ready_cn_path) if ready_cn_path is not None else READY_CN_PATH
        )
        self.keepnet_path = (
            Path(keepnet_path) if keepnet_path is not None else None
        )
        self.retrieve_lift = bool(retrieve_lift)
        # 自动抄网：默认从 RF4_AUTO_NET 读取（默认关）。生产 main() 会把
        # 同一个值同时传给 Bridge 与 FightConfig，避免"bridge 开、状态机关"
        # 之类不一致；显式传入 fight_auto_net_enabled 时，不一致必须在任何
        # 按键之前变成 preflight 失败。
        resolved_auto_net = (
            resolve_auto_net_enabled()
            if auto_net_enabled is None
            else bool(auto_net_enabled)
        )
        self.auto_net_enabled = resolved_auto_net
        self._net_config_error: Optional[str] = None
        if (
            fight_auto_net_enabled is not None
            and bool(fight_auto_net_enabled) != resolved_auto_net
        ):
            self._net_config_error = (
                "net_config_mismatch:bridge=%s fight=%s"
                % (resolved_auto_net, bool(fight_auto_net_enabled))
            )
            if not preflight_error:
                preflight_error = self._net_config_error
        self.wheel_gap = max(0.0, float(wheel_gap))
        self._log_fn = log or _default_log
        self._clock = clock or time.monotonic
        self._foreground_fn = foreground
        self._wheel_sender = wheel_sender
        self._key_state = key_state
        self._focus_fn = focus
        self._preflight_error = str(preflight_error) if preflight_error else None
        self._preflight_raised = False

        self._last_focus_at: Optional[float] = None
        self._focus_failures = 0
        self._fg_log_ts = float("-inf")

        self._coordinator: Any = None
        self._arbiter_override: Any = None
        self._templates: dict[str, Any] = {}
        self._ready_cn_array: Any = None
        self._ready_cn_edges: Any = None
        # 协议搏斗判据缓存（0.5s）
        self._proto_cache: Any = None
        self._proto_cache_at = 0.0
        # 协议快照缓存（0.5s，retrieve/判活共用）
        self._proto_snap: Any = None
        self._proto_snap_at = 0.0
        self._ready_cn_loaded = False
        self._ready_cn_reject = ""
        self._ready_cache_ts: Optional[float] = None
        self._ready_cache_value: Optional[bool] = None
        self._ready_log_ts = float("-inf")
        self._last_image: Any = None
        self._last_ts: Optional[float] = None
        self._fric_win: list[tuple[float, int]] = []
        self._fric_fish_id: Optional[str] = None
        self._friction_calibrated = False
        self._orig_wheel_raw: Any = None

    # ------------------------------------------------------------------
    # 只读/工具
    # ------------------------------------------------------------------
    def _emit(self, text: str) -> None:
        try:
            self._log_fn(str(text))
        except Exception:  # noqa: BLE001 - 日志不能影响控制
            pass

    def attach(self, coordinator: Any) -> None:
        """运行层构造完成后注入协调器，以便复用它的唯一 arbiter。"""
        self._coordinator = coordinator

    def set_arbiter(self, arbiter: Any) -> None:
        """测试/外部宿主显式注入 arbiter（生产用 ``attach``）。"""
        self._arbiter_override = arbiter

    def _arbiter(self):
        if self._coordinator is not None:
            return self._coordinator.arbiter
        if self._arbiter_override is not None:
            return self._arbiter_override
        raise RuntimeError("input arbiter is not attached")

    @property
    def preflight_error(self) -> Optional[str]:
        return self._preflight_error

    def _require_adapter(self):
        if self.A is None:
            raise BridgeFault("adapter_unavailable")
        return self.A

    def _foreground(self) -> bool:
        if self._preflight_error:
            # 预检失败 = 没有可信的前台/模板/尺寸，任何动作都按失焦处理。
            return False
        if self._foreground_fn is not None:
            try:
                return bool(self._foreground_fn())
            except (Exception, SystemExit):  # noqa: BLE001
                return False
        if self.A is None:
            return False
        try:
            hwnd = self.hwnd if self.hwnd is not None else self.A.game_hwnd()
            return int(ctypes.windll.user32.GetForegroundWindow()) == int(hwnd)
        except (Exception, SystemExit):  # noqa: BLE001
            return False

    def _frame_valid(self, image: Any) -> bool:
        if self.A is None or image is None:
            return False
        try:
            return bool(self.A._fight_frame_valid(image))
        except (Exception, SystemExit):  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # 前台恢复（只在 AUTO 请求下、受控且可停止）
    # ------------------------------------------------------------------
    def _action_control(self):
        """动作/抓帧共用的控制会话：优先协调器持有的那一个。"""
        coord = self._coordinator
        if coord is not None:
            control = getattr(coord, "control", None)
            if control is not None:
                return control
        return self.control

    def _recovery_allowed(self, control: Any) -> bool:
        """恢复前的前置条件；任何一条不满足都绝不抢前台。

        * 控制会话已 stop（含 IPC 侧立即置位的 stop）；
        * 协调器没请求 AUTO（OBSERVE/MANUAL/初始 OBSERVE 都不允许）；
        * 状态已经是 FAULT/STOPPED（等待人工 resume/stop）；
        * 还有排队未消费的控制命令（observe/stop 可能就在里面）。
        """
        if control is not None and bool(getattr(control, "stopped", False)):
            return False
        session = self._action_control()
        if session is not None and bool(getattr(session, "stopped", False)):
            return False
        coord = self._coordinator
        if coord is None:
            return False
        if not bool(getattr(coord, "auto_requested", False)):
            return False
        state = getattr(coord, "state", None)
        name = getattr(state, "name", state)
        if isinstance(name, str) and name.upper() in {
            "FAULT",
            "STOPPED",
            "STOPPING",
        }:
            return False
        # lifecycle.step() 在抓帧前会先 drain 命令，正常情况下这里是 0；
        # 动作中途新到达的 observe/stop 还排在队列里时，宁可本 tick 不抢。
        if int(getattr(session, "command_count", 0) or 0) > 0:
            return False
        return True

    def _focus_game(self) -> bool:
        if self._preflight_error:
            return False
        fn = self._focus_fn
        if fn is None:
            A = self.A
            fn = getattr(A, "focus_game_quiet", None) if A is not None else None
        if not callable(fn):
            return False
        try:
            return bool(fn())
        except (Exception, SystemExit):  # noqa: BLE001 - 聚焦失败=保持失焦
            return False

    def _log_recovery(self, ok: bool, source: str) -> None:
        now = self._clock()
        if now - self._fg_log_ts < FOREGROUND_LOG_INTERVAL:
            return
        self._fg_log_ts = now
        self._emit(
            "[FG] foreground recovery %s (%s, failures=%d)"
            % ("ok" if ok else "failed", source, self._focus_failures)
        )

    def ensure_foreground(self, control: Any = None, *, action: bool = False) -> bool:
        """统一的前台恢复入口：最多一次真实聚焦，带节流与连续失败上限。

        返回 True 表示此刻前台已确认。调用方在拿到 True 后**必须重新抓帧**，
        不能用聚焦前的旧图做动作。
        """
        if self._foreground():
            self._focus_failures = 0
            return True
        if control is None:
            control = self._action_control()
        if not self._recovery_allowed(control):
            return False
        if self._focus_failures >= FOREGROUND_RECOVERY_LIMIT:
            return False
        now = self._clock()
        if (
            self._last_focus_at is not None
            and now - self._last_focus_at < FOREGROUND_RECOVERY_INTERVAL
        ):
            return False
        self._last_focus_at = now
        self._focus_game()
        # 聚焦期间可能刚好收到 stop：以最终状态为准，stop 优先。
        session = self._action_control()
        if (control is not None and bool(getattr(control, "stopped", False))) or (
            session is not None and bool(getattr(session, "stopped", False))
        ):
            self._log_recovery(False, "stopped")
            return False
        ok = self._foreground()
        if ok:
            self._focus_failures = 0
        else:
            self._focus_failures += 1
        self._log_recovery(ok, "action" if action else "capture")
        return ok

    def _template(self, path: Optional[Path]):
        if path is None:
            return None
        key = str(path)
        if key in self._templates:
            return self._templates[key]
        try:
            import cv2  # noqa: PLC0415 - 可选依赖，失败=模板不可用

            tpl = cv2.imread(key)
        except Exception:  # noqa: BLE001
            tpl = None
        self._templates[key] = tpl
        return tpl

    def _match_template_score(self, image: Any, path: Optional[Path]):
        tpl = self._template(path)
        if tpl is None or image is None:
            return None
        try:
            import cv2  # noqa: PLC0415
            import numpy as np  # noqa: PLC0415

            hay = cv2.cvtColor(
                np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR
            )
            th, tw = tpl.shape[:2]
            if th > hay.shape[0] or tw > hay.shape[1]:
                return 0.0
            result = cv2.matchTemplate(hay, tpl, cv2.TM_CCOEFF_NORMED)
            return 0.0 if result.size == 0 else float(result.max())
        except Exception:  # noqa: BLE001 - 读不出来 = UNKNOWN
            return None

    # ------------------------------------------------------------------
    # 中文 readiness 模板（底部固定 ROI，灰度 TM_CCOEFF_NORMED）
    # ------------------------------------------------------------------
    def _ready_template(self):
        """加载并校验 ``static/ready_cn.png``；无方差模板直接拒绝。"""
        if self._ready_cn_loaded:
            return self._ready_cn_array
        self._ready_cn_loaded = True
        path = self.ready_cn_path
        if path is None:
            self._ready_cn_reject = "path_missing"
            return None
        try:
            import numpy as np  # noqa: PLC0415
            from PIL import Image  # noqa: PLC0415

            with Image.open(path) as im:
                arr = np.asarray(im.convert("L"), dtype=np.float32)
            if arr.ndim != 2 or arr.size == 0:
                raise ValueError("empty template")
            std = float(arr.std())
            if std < READY_CN_MIN_STD:
                raise ValueError("low_variance std=%.2f" % std)
        except Exception as exc:  # noqa: BLE001 - 模板不可用 = UNKNOWN
            self._ready_cn_reject = "%s:%s" % (type(exc).__name__, exc)
            self._ready_cn_array = None
            self._emit("[READY] template_unavailable %s" % self._ready_cn_reject)
            return None
        self._ready_cn_array = arr
        self._ready_cn_reject = ""
        self._emit(
            "[READY] template %s %dx%d std=%.1f"
            % (path, arr.shape[1], arr.shape[0], std)
        )
        return arr

    @staticmethod
    def _ncc_best(hay: Any, tpl: Any):
        """灰度归一化互相关；有 cv2 用 cv2，否则用 numpy 离线兜底。"""
        import numpy as np  # noqa: PLC0415

        th, tw = tpl.shape[:2]
        if th > hay.shape[0] or tw > hay.shape[1]:
            return 0.0, (-1, -1)
        try:
            import cv2  # noqa: PLC0415

            res = cv2.matchTemplate(hay, tpl, cv2.TM_CCOEFF_NORMED)
            if res.size == 0:
                return 0.0, (-1, -1)
            _mn, mx, _ml, loc = cv2.minMaxLoc(res)
            return float(mx), (int(loc[0]), int(loc[1]))
        except Exception:  # noqa: BLE001 - cv2 缺失/失败时走 numpy 兜底
            pass

        t = tpl - float(tpl.mean())
        tss = float((t * t).sum())
        if tss <= 1e-6:
            return 0.0, (-1, -1)
        n = float(th * tw)
        best = -2.0
        best_xy = (-1, -1)
        for dy in range(hay.shape[0] - th + 1):
            block = hay[dy:dy + th]
            win = np.lib.stride_tricks.sliding_window_view(block, tw, axis=1)
            num = (win * t[:, None, :]).sum(axis=(0, 2))
            s = win.sum(axis=(0, 2))
            ss = (win * win).sum(axis=(0, 2))
            var = np.clip(ss - s * s / n, 0.0, None)
            den = np.sqrt(tss * var)
            with np.errstate(divide="ignore", invalid="ignore"):
                score = np.where(den > 1e-9, num / den, 0.0)
            k = int(np.argmax(score))
            if float(score[k]) > best:
                best = float(score[k])
                best_xy = (k, dy)
        return (best if best > -2.0 else 0.0), best_xy

    def _match_ready_cn(self, image: Any):
        """在底部固定 ROI 里匹配中文模板，返回 (score, x, y) 或 None。

        **2026-11-09 改边缘匹配**：提示文字是半透明的，叠在水面反光上。
        实测同一位置原图 NCC 只有 0.8215（阈值 0.93 → 误判 not_ready，
        bot 卡在 PROBING 不抛竿），而 Sobel 边缘图 NCC 是 0.9556、
        次优位置只有 0.5204 —— 背景亮度/波纹变化不再影响判分。
        """
        tpl = self._ready_template()
        if tpl is None or image is None:
            return None
        try:
            import numpy as np  # noqa: PLC0415

            hay = np.asarray(image.convert("L"), dtype=np.float32)
        except Exception:  # noqa: BLE001
            return None
        x0, y0, x1, y1 = READY_ROI
        x1 = min(int(x1), int(hay.shape[1]))
        y1 = min(int(y1), int(hay.shape[0]))
        if x1 <= x0 or y1 <= y0:
            return None
        roi = hay[y0:y1, x0:x1]
        tpl_e = self._ready_cn_edges
        if tpl_e is None:
            tpl_e = self._edge_map(tpl)
            self._ready_cn_edges = tpl_e
        score, (mx, my) = self._ncc_best(self._edge_map(roi), tpl_e)
        if mx < 0 or my < 0:
            return None
        return float(score), x0 + int(mx), y0 + int(my)

    @staticmethod
    def _edge_map(arr: Any):
        """Sobel 幅值图（背景亮度无关）。cv2 缺失时返回原图（降级）。"""
        try:
            import cv2  # noqa: PLC0415
            import numpy as np  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return arr
        a = np.asarray(arr, dtype=np.float32)
        gx = cv2.Sobel(a, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(a, cv2.CV_32F, 0, 1, ksize=3)
        mag = np.sqrt(gx * gx + gy * gy)
        return np.clip(mag * 0.125, 0.0, 255.0)

    def _ready_verdict(
        self, image: Any, obs: aobs.Observation
    ) -> Optional[bool]:
        """同一帧的 readiness：先否决，再做位置模板匹配。"""
        if obs is None or not obs.foreground or image is None:
            return None
        if obs.captured is T.TRUE:
            self._emit("[READY] false veto=captured")
            return False
        if obs.ok_modal is T.TRUE:
            self._emit("[READY] false veto=modal")
            return False
        if obs.fish_icon is T.TRUE:
            self._emit("[READY] false veto=fish")
            return False
        hit = self._match_ready_cn(image)
        if hit is None:
            return None
        score, x, y = hit
        ax, ay = READY_CN_ANCHOR
        dx, dy = READY_CN_POS_TOL
        in_place = abs(x - ax) <= dx and abs(y - ay) <= dy
        if score >= READY_CN_THRESHOLD and in_place:
            now = self._clock()
            if now - self._ready_log_ts >= STATUS_LOG_INTERVAL:
                self._emit(
                    "[READY] hit score=%.4f at=(%d,%d) roi=%s thr=%.2f"
                    % (score, x, y, READY_ROI, READY_CN_THRESHOLD)
                )
                self._ready_log_ts = now
            return True
        if score >= 0.75:
            self._emit(
                "[READY] miss score=%.4f at=(%d,%d) in_place=%s"
                % (score, x, y, in_place)
            )
        return False

    def _async_key_down(self, key: str) -> Optional[bool]:
        """可选按键状态后端：优先注入的 fake，否则走 adapter.ctypes。"""
        if self._key_state is not None:
            try:
                value = self._key_state(key)
            except (Exception, SystemExit):  # noqa: BLE001
                return None
            return None if value is None else bool(value)
        vk = {
            "ctrl": 0x11,
            "alt": 0x12,
            "shift": 0x10,
            "f5": 0x74,
            "f6": 0x75,
            "f7": 0x76,
        }.get(key)
        if vk is None:
            return None
        backend = getattr(self.A, "ctypes", None)
        if backend is None:
            backend = ctypes
        try:
            state = backend.windll.user32.GetAsyncKeyState(vk)
            return bool(int(state) & 0x8000)
        except (Exception, SystemExit):  # noqa: BLE001 - 可选后端不可用
            return None

    def _net_modifiers_held(self) -> list[str]:
        """NET F7 是裸键：Ctrl/Alt/Shift 任一被物理按住时都不发。

        全局恢复热键是 ``Ctrl+Alt+Shift+F7``。如果用户在按住这组修饰键时
        恰好走到 NET，裸 F7 会变成热键而非抄网，所以这里明确报错阻塞；
        只读探测，绝不替用户松开任何键。
        """
        return [
            key
            for key in ("ctrl", "alt", "shift")
            if self._async_key_down(key) is True
        ]

    def _coordinator_mode_name(self) -> Optional[str]:
        """协调器当前生效模式名；没有协调器/读不到时返回 None。"""
        coord = self._coordinator
        if coord is None:
            return None
        mode = getattr(coord, "mode", None)
        if callable(mode):
            try:
                mode = mode()
            except (Exception, SystemExit):  # noqa: BLE001
                return None
        elif mode is None:
            mode = getattr(coord, "control_mode", None)
        name = getattr(mode, "name", mode)
        return name.upper() if isinstance(name, str) else None

    def _cast_authorized(
        self, control: Any, obs: Optional[aobs.Observation]
    ) -> bool:
        """校准后的单次抛竿授权。

        只认 ``control.state`` **此刻**为 CASTING（ActionControl 实时读协调器
        状态，不是长期 token），并且协调器确实请求 AUTO、当前前台新鲜、本帧
        没有强捕获/弹窗。校准过程会临时弹出设置面板遮住底部中文 ready 提示，
        所以 readiness=False 不能在这里二次阻断；但第一下按键之前仍必须用
        本帧重新验证无鱼/无结算/无 modal（见 :meth:`_cast_pre_down_safe`）。
        """
        state = getattr(control, "state", None)
        name = getattr(state, "name", state)
        if not (isinstance(name, str) and name.upper() == "CASTING"):
            return False
        coord = self._coordinator
        if coord is None or not bool(getattr(coord, "auto_requested", False)):
            return False
        if self._coordinator_mode_name() != "AUTO":
            return False
        if not self._foreground():
            return False
        if obs is None or not obs.foreground:
            return False
        if obs.captured is T.TRUE or obs.ok_modal is T.TRUE:
            return False
        return True

    @staticmethod
    def _cast_pre_down_safe(obs: Optional[aobs.Observation]) -> bool:
        """第一下 down 之前的本帧安全门：无鱼、无结算、无 modal。

        三者都必须**明确 False**；UNKNOWN 一律不动作（宁可失败也不盲跳）。
        """
        if obs is None:
            return False
        return (
            obs.fish_icon is T.FALSE
            and obs.captured is T.FALSE
            and obs.ok_modal is T.FALSE
        )

    def _synthetic_observation(self, foreground: bool) -> aobs.Observation:
        return aobs.Observation(
            monotonic_ts=self._clock(),
            wall_ts=time.time(),
            foreground=bool(foreground),
            fish_icon=T.UNKNOWN,
            bar_level=None,
            bar_tier=None,
            bar_hue=None,
            bar_sat=None,
            ring_ratio=None,
            captured=T.UNKNOWN,
            ok_modal=T.UNKNOWN,
            hud_visible=T.UNKNOWN,
            evidence={"bridge": self._preflight_error or "unavailable"},
        )

    # ------------------------------------------------------------------
    # 抓帧 / 就绪
    # ------------------------------------------------------------------
    def capture_observation(self) -> aobs.Observation:
        if self._preflight_error:
            if not self._preflight_raised:
                self._preflight_raised = True
                raise BridgeFault(self._preflight_error)
            # 预检失败后永远报“非前台”的合成帧：即使 UI 发 resume/
            # confirm_ready，mode() 也会是 BLOCKED，不可能执行动作。
            return self._synthetic_observation(foreground=False)

        A = self._require_adapter()
        if not self._foreground():
            # 只有协调器明确请求 AUTO 才会尝试拉回；OBSERVE/STOP/FAULT 一律
            # 不抢前台。聚焦成功后必须继续走下面的 capture_game() 取新帧，
            # 绝不能拿聚焦前的旧图当证据。
            self.ensure_foreground(self._action_control())
        try:
            image, _pos = A.capture_game()
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            raise BridgeFault(
                "capture_error:%s:%s" % (type(exc).__name__, exc)
            ) from exc

        ts = self._clock()
        foreground = self._foreground()
        try:
            size = tuple(image.size)
        except Exception:  # noqa: BLE001
            size = (0, 0)
        if size != (TARGET_W, TARGET_H):
            raise BridgeFault(
                "frame_size_%dx%d_expected_%dx%d"
                % (size[0], size[1], TARGET_W, TARGET_H)
            )

        if self.keep_path is None:
            raise BridgeFault("capture_template_unavailable")
        score = A._fight_strong_capture(image, str(self.keep_path))
        if score is None:
            # 模板不可用时绝不能退回几何/猜黑屏。
            raise BridgeFault("capture_template_unavailable")

        obs = aobs.observe_frame(
            image,
            foreground=foreground,
            monotonic_ts=ts,
            wall_ts=time.time(),
        )
        captured = (
            T.TRUE
            if foreground and float(score) >= STRONG_CAPTURE_CONFIDENCE
            else T.FALSE
        )
        obs = dataclasses.replace(obs, captured=captured)
        self._last_image = image
        self._last_ts = ts
        if obs.ring_ratio is not None:
            # 岸上 ready 提示出现时，圆环会显示满 1.0；它不能当成“线在水里”。
            # 只有在可读圆环存在时才多算一次 readiness，并在命中时清空圆环，
            # 让生命周期优先走 readiness 分支（否则会被 ring_readable_probe
            # 直接送进 WAIT_BITE）。
            ready = self._ready_verdict(image, obs)
            self._ready_cache_ts = ts
            self._ready_cache_value = ready
            if ready is True:
                raw_ring = obs.ring_ratio
                self._emit(
                    "[READY] ring suppressed ring=%s (ready prompt wins)"
                    % (raw_ring,)
                )
                evidence = dict(obs.evidence)
                evidence["bridge_ready_ring"] = raw_ring
                obs = dataclasses.replace(
                    obs, ring_ratio=None, evidence=evidence
                )
        return obs

    def readiness(self, obs: aobs.Observation) -> Optional[bool]:
        """只认底部中文模板的同帧命中；强捕获/弹窗/鱼图标一律 False。

        不再读取英文 ``ready.png``，也不把“圆环满”当成就绪：满环 1.0 在
        岸上同样出现，唯一可信证据是底部固定 ROI 的中文提示。
        """
        if self.A is None:
            return None
        if obs is None or not obs.foreground:
            return None
        image = self._last_image
        if image is None or self._last_ts != obs.monotonic_ts:
            return None
        if not self._frame_valid(image):
            return None
        if self._ready_cache_ts == obs.monotonic_ts:
            return self._ready_cache_value
        return self._ready_verdict(image, obs)

    # ------------------------------------------------------------------
    # 输入
    # ------------------------------------------------------------------
    def sink(self, key: str, is_down: bool, reason: str) -> Any:
        """唯一按键 sink：只发 pydirectinput，不直接改游戏状态。"""
        if key not in ALLOWED_KEYS:
            raise ValueError("bridge sink does not own key %r" % (key,))
        if is_down:
            if self.control.stopped:
                raise RuntimeError("stop_requested")
            if not self._foreground() and not self.ensure_foreground(
                self._action_control()
            ):
                raise RuntimeError("foreground_required")
        if self.pdi is None:
            raise RuntimeError("input_unavailable")
        try:
            result = (
                self.pdi.keyDown(key) if is_down else self.pdi.keyUp(key)
            )
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - 保留原始 cause
            # pydirectinput 的 FAILSAFE（鼠标物理顶角）会抛 FailSafeException；
            # 这里不关闭 FAILSAFE，只把真实原因带进领域错误供上层 FAULT 展示。
            self._emit(
                "[KEY] pydirectinput %s %s failed: %s: %s"
                % (
                    key,
                    "down" if is_down else "up",
                    type(exc).__name__,
                    exc,
                )
            )
            raise RuntimeError(
                "pydirectinput %s %s failed: %s: %s"
                % (
                    key,
                    "down" if is_down else "up",
                    type(exc).__name__,
                    exc,
                )
            ) from exc
        if result is False:
            raise RuntimeError(
                "pydirectinput rejected %s %s"
                % (key, "down" if is_down else "up")
            )
        if key == F6 and self.A is not None:
            lift = getattr(self.A, "_lift", None)
            if isinstance(lift, dict):
                lift["held"] = bool(is_down)
        self._emit(
            "[KEY] %s %s (bridge:%s)"
            % (key, "按下" if is_down else "松开", reason)
        )

    def _set_desired(self, keys, reason: str):
        arbiter = self._arbiter()
        arbiter.set_desired(set(keys), reason)
        return arbiter.reconcile(reason)

    def _release_owned(self, reason: str) -> None:
        try:
            self._arbiter().release_owned(reason)
        except Exception as exc:  # noqa: BLE001 - 释放失败只记录
            self._emit("[KEY] release failed (%s): %s" % (reason, exc))

    def release_owned(self, reason: str = "bridge_release") -> None:
        """宿主退出时的兜底：只释放本进程确实成功按下的键。"""
        self._release_owned(reason)

    def _send_wheel_step(self, sign: int) -> None:
        if self._preflight_error:
            raise BridgeFault(self._preflight_error)
        delta = WHEEL_DELTA if sign > 0 else -WHEEL_DELTA
        if self._wheel_sender is not None:
            self._wheel_sender(delta)
            return
        ctypes.windll.user32.mouse_event(0x0800, 0, 0, delta, 0)

    # ------------------------------------------------------------------
    # 摩擦：校准 + 动态
    # ------------------------------------------------------------------
    def _friction_limits(self) -> tuple[int, int, int]:
        A = self.A
        if A is None:
            return 1, 29, 17
        return (
            int(getattr(A, "FRICTION_MIN", 1)),
            int(getattr(A, "FRICTION_MAX", 29)),
            int(getattr(A, "FRICTION_INITIAL", 17)),
        )

    def _friction_reset_observation(self, keep_last_action: bool) -> None:
        """清适配器的观察纪元；旧适配器没有该方法时静默跳过。"""
        reset = getattr(self.A, "friction_reset_observation", None)
        if not callable(reset):
            return
        try:
            reset(keep_last_action=keep_last_action)
        except Exception:  # noqa: BLE001 - 观测清理失败不能影响主流程
            pass

    def _invalidate_friction(self) -> None:
        self._friction_calibrated = False
        self._friction_reset_observation(keep_last_action=False)
        A = self.A
        if A is None:
            return
        try:
            A._fric["calibrated"] = False
            A._fric.pop("_restored", None)
        except Exception:  # noqa: BLE001
            pass
        try:
            A.friction_state.invalidate()
        except Exception:  # noqa: BLE001
            pass

    def _set_friction(self, cur: int, calibrated: bool) -> None:
        A = self.A
        if A is None:
            return
        try:
            A._fric["cur"] = int(cur)
            A._fric["t"] = time.time()
            A._fric["calibrated"] = bool(calibrated)
            A._fric.pop("_restored", None)
        except Exception:  # noqa: BLE001
            pass
        self._friction_calibrated = bool(calibrated)
        if calibrated:
            try:
                A.friction_state.set(int(cur), calibrated=True)
            except Exception:  # noqa: BLE001
                pass

    def _bump_friction(self, sign: int) -> None:
        A = self.A
        if A is None:
            return
        mn, mx, _initial = self._friction_limits()
        try:
            cur = int(A._fric.get("cur", mn))
        except Exception:  # noqa: BLE001
            cur = mn
        A._fric["cur"] = max(mn, min(mx, cur + sign))
        A._fric["t"] = time.time()

    def _account_prefix(self, sign: int, done: int) -> None:
        for _ in range(max(0, int(done))):
            self._bump_friction(sign)

    def _wheel_calibration(self, n: int, control: Any) -> bool:
        sign = 1 if n > 0 else -1
        for _ in range(abs(int(n))):
            control.checkpoint()
            if not self._foreground() and not self.ensure_foreground(
                control, action=True
            ):
                return False
            self._send_wheel_step(sign)
            self._bump_friction(sign)
            if control.wait(self.wheel_gap):
                return False
        return True

    def calibrate(self, control: Any, fight: bool = False) -> Optional[bool]:
        """每竿安全校准：先向下降到底，再向上回到 17 档。

        不走上游的「滚到 30 顶」方案：无鱼时虽然允许升，但到顶后一旦判断
        失误就可能把泄力打到最大。这里先降到底（只会更松），再升固定格数。
        """
        A = self.A
        if A is None or self._preflight_error:
            return False
        self._invalidate_friction()
        try:
            control.checkpoint()
            if not self._foreground() and not self.ensure_foreground(
                control, action=True
            ):
                self._invalidate_friction()
                return False
            _mn, _mx, initial = self._friction_limits()
            down = int(getattr(A, "FRICTION_ABS_TOP", 30))
            up = int(initial - self._friction_limits()[0])
            if down <= 0 or up < 0:
                self._invalidate_friction()
                return False
            if not self._wheel_calibration(-down, control):
                self._invalidate_friction()
                return False
            if not self._wheel_calibration(up, control):
                self._invalidate_friction()
                return False
            self._set_friction(initial, True)
            # 校准成功 = 新基线：清窗口、last_action/last_drop/observe_since，
            # 首次升档重新等满 10 秒，绝不继承校准前的旧时刻。
            self._fric_win.clear()
            self._friction_reset_observation(keep_last_action=False)
            self._emit(
                "[FRIC] calibrated -> %d (down %d / up %d, fight=%s)"
                % (initial, down, up, bool(fight))
            )
            return None
        except AutoStop:
            self._invalidate_friction()
            raise
        except Exception as exc:  # noqa: BLE001
            self._invalidate_friction()
            self._emit("[FRIC] calibration failed: %s" % (exc,))
            return False

    def wheel(self, steps: int) -> bool:
        """相对滚轮（供 ActionControl.wheel 透传）；未知时禁止加档。"""
        A = self.A
        if A is None or self._preflight_error:
            return False
        try:
            steps = int(steps)
        except (TypeError, ValueError):
            return False
        if steps == 0:
            return True
        mn, mx, _initial = self._friction_limits()
        try:
            cur = max(mn, min(mx, int(A._fric.get("cur", mn))))
        except Exception:  # noqa: BLE001
            cur = mn
        calibrated = bool(self._friction_calibrated and A._fric.get("calibrated"))
        if steps > 0 and not calibrated:
            self._invalidate_friction()
            return False
        room = (mx - cur) if steps > 0 else (cur - mn)
        steps = (1 if steps > 0 else -1) * min(abs(steps), max(0, room))
        if steps == 0:
            return True
        sign = 1 if steps > 0 else -1
        try:
            for _ in range(abs(steps)):
                self.control.checkpoint()
                if not self._foreground() and not self.ensure_foreground(
                    self._action_control(), action=True
                ):
                    self._invalidate_friction()
                    return False
                self._send_wheel_step(sign)
                self._bump_friction(sign)
                if self.control.wait(self.wheel_gap):
                    self._invalidate_friction()
                    return False
        except AutoStop:
            self._invalidate_friction()
            raise
        except Exception:  # noqa: BLE001
            self._invalidate_friction()
            return False
        new_cur = max(mn, min(mx, cur + steps))
        if calibrated:
            self._set_friction(new_cur, True)
        else:
            try:
                A._fric["cur"] = new_cur
            except Exception:  # noqa: BLE001
                pass
        return True

    def _bridge_wheel_raw(self, n: int) -> bool:
        """替代 ``A._wheel_raw`` 的可取消版本。

        ``A._wheel`` 只在本函数返回 True 后才记账，所以：
          * 完整成功 -> 不动 ``_fric["cur"]``，交给 A._wheel 加 n；
          * 中途失败 -> 只把**已成功发出的前缀**记进 cur，然后 invalidate。
        """
        A = self.A
        if A is None or self._preflight_error:
            return False
        try:
            n = int(n)
        except (TypeError, ValueError):
            return False
        if n == 0:
            return True
        mn, mx, _initial = self._friction_limits()
        try:
            cur = max(mn, min(mx, int(A._fric.get("cur", mn))))
        except Exception:  # noqa: BLE001
            cur = mn
        calibrated = bool(self._friction_calibrated and A._fric.get("calibrated"))
        if n > 0 and not calibrated:
            # 档位未知时只允许向松的方向调整。
            self._invalidate_friction()
            return False

        room = (mx - cur) if n > 0 else (cur - mn)
        steps = min(abs(n), max(0, room))
        if steps <= 0:
            return True
        sign = 1 if n > 0 else -1
        done = 0
        try:
            for _ in range(steps):
                try:
                    self.control.checkpoint()
                except AutoStop:
                    self._account_prefix(sign, done)
                    self._invalidate_friction()
                    return False
                if not self._foreground() and not self.ensure_foreground(
                    self._action_control(), action=True
                ):
                    self._account_prefix(sign, done)
                    self._invalidate_friction()
                    return False
                self._send_wheel_step(sign)
                done += 1
                if self.control.wait(self.wheel_gap):
                    self._account_prefix(sign, done)
                    self._invalidate_friction()
                    return False
        except Exception:  # noqa: BLE001
            self._account_prefix(sign, done)
            self._invalidate_friction()
            return False
        if done < steps:
            self._account_prefix(sign, done)
            self._invalidate_friction()
            return False
        return True

    def install(self) -> None:
        """接管 A._wheel_raw，并丢弃不可信的落盘档位。"""
        A = self.A
        self._invalidate_friction()
        if A is None:
            return
        try:
            self._orig_wheel_raw = A._wheel_raw
            A._wheel_raw = self._bridge_wheel_raw
        except Exception:  # noqa: BLE001
            self._orig_wheel_raw = None

    def restore(self) -> None:
        A = self.A
        if A is None or self._orig_wheel_raw is None:
            return
        try:
            A._wheel_raw = self._orig_wheel_raw
        except Exception:  # noqa: BLE001
            pass
        self._orig_wheel_raw = None

    def _friction_fish_key(self) -> Optional[str]:
        coord = self._coordinator
        if coord is None:
            return None
        try:
            value = getattr(coord, "fish_id", None)
        except Exception:  # noqa: BLE001
            return None
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def friction_step(self, level: Optional[int]) -> None:
        if self.A is None or self._preflight_error:
            return
        # 新鱼（确认中鱼后 fish_id 变化）：只重置观察窗，不动 cur、不发滚轮。
        # 用显式 fish_id，不依据“上次是否已校准”来猜会话边界。
        fish_id = self._friction_fish_key()
        if fish_id is not None and fish_id != self._fric_fish_id:
            self._fric_fish_id = fish_id
            self._fric_win.clear()
            self._friction_reset_observation(keep_last_action=False)
        if level is None:
            # 暂停/冻结：清有效观察纪元，但保留最后调整类型，恢复后仍按
            # rise 5s / drop 10s 等待；同鱼 LAND/NET 切换不清 last_action。
            self._fric_win.clear()
            self._friction_reset_observation(keep_last_action=True)
            return
        try:
            self._fric_win.append((time.time(), int(level)))
        except (TypeError, ValueError):
            self._fric_win.clear()
            return
        self.A.friction_controller_step(self._fric_win)

    def friction_snapshot(self) -> Mapping[str, Any]:
        A = self.A
        if A is None:
            return {"cur": None, "calibrated": False}
        try:
            cur = A._fric.get("cur")
        except Exception:  # noqa: BLE001
            cur = None
        calibrated = bool(
            self._friction_calibrated and A._fric.get("calibrated")
        )
        return {"cur": cur, "calibrated": calibrated}

    # ------------------------------------------------------------------
    # 抛竿 / 抄网
    # ------------------------------------------------------------------
    @staticmethod
    def _line_on_spool(obs: Any) -> bool:
        """线是否还完整地缠在线杯上（= 不需要收线）。

        圆环可读不代表线在水里：入护后线收回杯上，环照样可读（实测 0.975~1.0）。
        刚抛出去约 0.93，鱼会继续往下拉，所以只有**接近满环**且没有鱼、没有力度条
        时才算"线在杯上"。

        阈值与 ``autofish_lifecycle.PROBE_LINE_OUT_MAX_RING`` 必须一致
        （test_bridge 里有一条断言守着），否则状态机会以为线在水里、
        而 retrieve 又按 F5 蓄力，两边就会打架。
        """
        ring = obs.ring_ratio
        if ring is None or ring < LINE_ON_SPOOL_RING:
            return False
        if obs.hud_visible is not aobs.TriState.TRUE:
            return False
        if obs.fish_icon is aobs.TriState.TRUE:
            return False
        if (obs.bar_level or 0) > 0:
            return False
        return True

    def _probe(self, control: Any):
        """动作内的一帧新鲜前台观测；失败返回 None（调用方按未确认处理）。"""
        control.checkpoint()
        A = self.A
        if A is None:
            return None
        if not self._foreground() and not self.ensure_foreground(
            control, action=True
        ):
            return None
        try:
            image, _pos = A.capture_game()
        except (Exception, SystemExit):  # noqa: BLE001
            return None
        if not self._foreground() or not self._frame_valid(image):
            return None
        try:
            obs = aobs.observe_frame(
                image,
                foreground=True,
                monotonic_ts=self._clock(),
                wall_ts=time.time(),
            )
        except (Exception, SystemExit):  # noqa: BLE001
            return None
        # observe_frame 的 captured 是几何/按钮判据；这里再用强模板补一次，
        # 保证“强 capture TRUE 时 ready 必须 False”在动作内也成立。
        if self.keep_path is not None:
            try:
                score = A._fight_strong_capture(image, str(self.keep_path))
            except (Exception, SystemExit):  # noqa: BLE001
                score = None
            if score is not None:
                obs = dataclasses.replace(
                    obs,
                    captured=(
                        T.TRUE
                        if float(score) >= STRONG_CAPTURE_CONFIDENCE
                        else T.FALSE
                    ),
                )
        return image, obs

    @staticmethod
    def _cast_positive(obs: aobs.Observation, pre: aobs.Observation) -> bool:
        """抛竿后的正向证据：鱼、圆环相对抛前变化、或新出现的 HUD。"""
        if obs.fish_icon is T.TRUE:
            return True
        ring = obs.ring_ratio
        if ring is not None:
            base = pre.ring_ratio
            if base is None and hasattr(pre.evidence, "get"):
                # ready 命中时 ring 被清空；用原始读数当基线，避免把“岸上满环
                # 消失后仍是 1.0”误当成圆环变化。
                base = pre.evidence.get("bridge_ready_ring")
            if base is None or abs(float(ring) - float(base)) >= CAST_RING_DELTA:
                return True
            if (
                obs.hud_visible is T.TRUE
                and pre.hud_visible is not T.TRUE
            ):
                return True
        return False

    def _ready_persist_counts(
        self, now: float, release_at: float, obs: aobs.Observation
    ) -> bool:
        """本帧 ready 是否可计入“ready 持续 = 抛竿失败”。

        * 释放后 ``CAST_RELEASE_GRACE`` 内一律不算：可能只是抛竿动画帧；
        * 本帧已有鱼/强结算证据时也不算：那说明线已经在水里，不是没抛出去。
        """
        if now - release_at < CAST_RELEASE_GRACE:
            return False
        if obs.fish_icon is T.TRUE or obs.captured is T.TRUE:
            return False
        return True

    def _cast_precheck(
        self, control: Any, pre: aobs.Observation, release_at: float
    ) -> bool:
        """首次 F5 释放后：先确认离岸/飞行，再决定是否短按 F5 关线杯。"""
        deadline = release_at + CAST_CLOSE_DELAY
        absent = 0
        present = 0
        positive = False
        last_ring = pre.ring_ratio
        while True:
            control.checkpoint()
            probe = self._probe(control)
            if probe is None:
                absent = 0
                present = 0
            else:
                image, obs = probe
                ready = self._ready_verdict(image, obs)
                if ready is True:
                    if self._ready_persist_counts(
                        self._clock(), release_at, obs
                    ):
                        present += 1
                        absent = 0
                    else:
                        # 刚释放的动画帧：不累计、也不当作失败。
                        present = 0
                        absent = 0
                    if present >= CAST_CONFIRM_FRAMES:
                        self._emit(
                            "[CAST] failed original_cause=ready_persist "
                            "after_release (no F5 tap)"
                        )
                        return False
                elif ready is False:
                    absent += 1
                    present = 0
                else:
                    absent = 0
                    present = 0
                if self._cast_positive(obs, pre):
                    positive = True
                last_ring = obs.ring_ratio
            if self._clock() >= deadline:
                break
            if control.wait(CAST_POLL):
                control.checkpoint()
        if not positive or absent < CAST_CONFIRM_FRAMES:
            self._emit(
                "[CAST] failed original_cause=no_cast_evidence "
                "positive=%s ready_absent=%d ring=%s"
                % (positive, absent, last_ring)
            )
            return False
        self._emit(
            "[CAST] pre_close ready=False ring=%s positive=True -> F5 tap"
            % (last_ring,)
        )
        self._set_desired({F5}, "cast_close_reel")
        if control.wait(0.1):
            control.checkpoint()
        self._set_desired(set(), "cast_close_release")
        return True

    def _cast_confirm(
        self, control: Any, pre: aobs.Observation, release_at: float
    ) -> bool:
        """关线杯后 5s 内：连续 2 帧 ready 不存在 + 正向证据才算成功。"""
        deadline = self._clock() + CAST_CONFIRM_TIMEOUT
        absent = 0
        present = 0
        first = True
        while True:
            control.checkpoint()
            probe = self._probe(control)
            if probe is None:
                absent = 0
                present = 0
            else:
                image, obs = probe
                ready = self._ready_verdict(image, obs)
                if first:
                    self._emit(
                        "[CAST] post_close ready=%s ring=%s fish=%s"
                        % (ready, obs.ring_ratio, obs.fish_icon.value)
                    )
                    first = False
                if ready is True:
                    if self._ready_persist_counts(
                        self._clock(), release_at, obs
                    ):
                        present += 1
                        absent = 0
                    else:
                        present = 0
                    if present >= CAST_CONFIRM_FRAMES:
                        self._emit(
                            "[CAST] failed original_cause=ready_persist "
                            "after_close"
                        )
                        return False
                elif ready is False:
                    absent += 1
                    present = 0
                else:
                    absent = 0
                    present = 0
                if absent >= CAST_CONFIRM_FRAMES and self._cast_positive(
                    obs, pre
                ):
                    self._emit(
                        "[CAST] confirmed ready=False ring=%s fish=%s hud=%s"
                        % (
                            obs.ring_ratio,
                            obs.fish_icon.value,
                            obs.hud_visible.value,
                        )
                    )
                    return True
            if self._clock() >= deadline:
                self._emit(
                    "[CAST] failed original_cause=confirm_timeout "
                    "ready_absent=%d" % (absent,)
                )
                return False
            if control.wait(CAST_POLL):
                control.checkpoint()

    def cast(self, control: Any) -> Optional[bool]:
        """确认 ready → Shift(0.08s) → F5 蓄力 → 释放 → 取证 → F5 关线杯。

        未确认 ready 且 ``control.state`` 当前不是 CASTING（校准后的单次
        授权）时，一次按键都不发，直接返回 False。生命周期收到 False 会
        清掉 READY 许可并进入 FAULT，不会自动回 READY 重新校准。本函数最多
        短按一次 F5 关线杯，绝不在证据不足时补抛。
        """
        if self._preflight_error:
            return False
        try:
            control.checkpoint()
            if not self._foreground() and not self.ensure_foreground(
                control, action=True
            ):
                self._emit("[CAST] failed original_cause=foreground_lost")
                return False
            try:
                pre = self.capture_observation()
            except BridgeFault as exc:
                self._emit("[CAST] failed original_cause=capture:%s" % (exc,))
                return False
            ready = self.readiness(pre)
            authorized = self._cast_authorized(control, pre)
            if ready is not True and not authorized:
                self._emit(
                    "[CAST] blocked original_cause=not_ready "
                    "ready=%s authorized=%s" % (ready, authorized)
                )
                return False
            # 授权只说明“可以抛”，第一下 down 之前仍要本帧明确无鱼/无结算/
            # 无 modal；任何一项 UNKNOWN 都不盲跳。
            if not self._cast_pre_down_safe(pre):
                self._emit(
                    "[CAST] blocked original_cause=unsafe_pre_down "
                    "fish=%s captured=%s modal=%s authorized=%s"
                    % (
                        pre.fish_icon.value,
                        pre.captured.value,
                        pre.ok_modal.value,
                        authorized,
                    )
                )
                return False
            self._emit(
                "[CAST] start ready=%s authorized=%s ring=%s"
                % (ready, authorized, pre.ring_ratio)
            )
            # 先单独按 Shift，间隔 0.08s 再按 F5；并用系统键态确认 Shift 真按住。
            self._set_desired({SHIFT}, "cast_shift")
            if control.wait(CAST_SHIFT_SETTLE):
                control.checkpoint()
            shift_down = self._async_key_down(SHIFT)
            if shift_down is False:
                self._emit(
                    "[CAST] failed original_cause=shift_not_down "
                    "(GetAsyncKeyState)"
                )
                return False
            self._set_desired({SHIFT, F5}, "cast_charge")
            if control.wait(CAST_CHARGE_HOLD):
                control.checkpoint()
            self._set_desired(set(), "cast_release")
            release_at = self._clock()
            if not self._cast_precheck(control, pre, release_at):
                return False
            if not self._cast_confirm(control, pre, release_at):
                return False
            return None
        finally:
            self._release_owned("cast_finally")

    def _net_toggle(self, reason: str) -> None:
        """掏网/收网共用的唯一 F7 切换实现。

        * 同一个 arbiter 发送一次 ``f7 down/up``；
        * 全程保持 F6、绝不按 F5（进入 NET 时 F5 已由状态机松开）；
        * Ctrl/Alt/Shift 任一被物理按住时不发裸 F7（会撞全局恢复热键），
          明确抛错且不替用户松键；
        * 可被 stop 取消；动作前做前台恢复检查；
        * 无论中间成功与否，``finally`` 都把 F7 从 desired 移除，
          避免键卡住。
        """
        if not self.auto_net_enabled:
            raise BridgeFault("auto_net_disabled")
        if self._preflight_error:
            raise BridgeFault(self._preflight_error)

        control = self._action_control()
        if control is not None:
            control.checkpoint()

        held = self._net_modifiers_held()
        if held:
            raise BridgeFault("net_modifier_held:" + "+".join(held))

        if not self._foreground() and not self.ensure_foreground(
            control, action=True
        ):
            raise BridgeFault("net_foreground_required")

        arbiter = self._arbiter()
        previous = set(arbiter.desired)
        previous.discard(F5)
        previous.discard(NET_KEY)
        previous.add(F6)
        started = self._clock()
        try:
            self._set_desired(previous | {NET_KEY}, "net_%s_down" % reason)
            if control is not None:
                if control.wait(0.08):
                    # wait 返回 True = 停止请求；转换成 AutoStop 交给上层。
                    control.checkpoint()
            else:
                time.sleep(0.08)
        finally:
            try:
                self._set_desired(previous, "net_%s_release" % reason)
            except Exception as exc:  # noqa: BLE001 - 释放失败必须记录
                self._emit(
                    "[NET] %s release failed (%s): %s" % (NET_KEY, reason, exc)
                )
        self._emit(
            "[NET] %s F7 tap reason=%s mono=%.3f wall=%s "
            "keys=%s (keep f6, no f5)"
            % (
                "deploy" if reason == "deploy" else "retract",
                reason,
                started,
                datetime.now().isoformat(timespec="milliseconds"),
                sorted(arbiter.emitted),
            )
        )

    def net_action(self) -> None:
        """掏出抄网：一次 F7；生产默认关闭。"""
        self._net_toggle("deploy")

    def net_retract_action(self) -> None:
        """收回抄网：一次 F7；生产默认关闭。"""
        self._net_toggle("retract")

    # ------------------------------------------------------------------
    # 收线
    # ------------------------------------------------------------------
    def _hold_foreground(self, control: Any, desired: set[str]) -> None:
        """失焦时先松开，等重新前台；期间仍可按 stop。"""
        deadline = time.monotonic() + RETRIEVE_TIMEOUT
        while not self._foreground():
            self.ensure_foreground(control, action=True)
            if control.wait(RETRIEVE_POLL):
                control.checkpoint()
            if time.monotonic() > deadline:
                raise RetrieveBlocked("retrieve_timeout_foreground")
        self._set_desired(desired, "retrieve_resume")

    def _retrieve_status(
        self, tag: str, obs: aobs.Observation, ready: Optional[bool]
    ) -> None:
        self._emit(
            "[RETRIEVE] %s fish=%s ring=%s ready=%s"
            % (tag, obs.fish_icon.value, obs.ring_ratio, ready)
        )

    def retrieve(self, control: Any) -> bool:
        """收空线：先证明 ready 不存在再按 F5；ready 存在时一次按键都不发。

        任何一帧只要中文 readiness 命中（连续 2 帧）就视为线已收好并完成，
        即使圆环仍显示 .967；近满环 >=.90 永远不能单独作为完成证据，旧的
        .98 后备仍要求新鲜前台帧 + HUD 可见 + 无鱼。
        """
        A = self.A
        if A is None or self._preflight_error:
            return False
        desired = {F5}
        if self.retrieve_lift:
            desired.add(F6)
        deadline = self._clock() + RETRIEVE_TIMEOUT
        ring_frames = 0
        ready_frames = 0
        last_log = float("-inf")
        try:
            control.checkpoint()
            if not self._foreground() and not self.ensure_foreground(
                control, action=True
            ):
                raise RetrieveBlocked("retrieve_foreground_required")

            # 关键前置：先采一帧，ready 命中则立刻返回，绝不发 F5；
            # ready 读不清（None）也不盲按，宁可超时 FAULT。
            while True:
                control.checkpoint()
                if not self._foreground() and not self.ensure_foreground(
                    control, action=True
                ):
                    raise RetrieveBlocked("retrieve_foreground_required")
                probe = self._probe(control)
                if probe is not None:
                    image, obs = probe
                    ready = self._ready_verdict(image, obs)
                    now = self._clock()
                    if now - last_log >= STATUS_LOG_INTERVAL:
                        self._retrieve_status("pre", obs, ready)
                        last_log = now
                    if ready is True:
                        self._emit(
                            "[RETRIEVE] ready template hit before F5; "
                            "already reeled, no key sent"
                        )
                        return True
                    if ready is False:
                        # 线已经在杯上就不要按 F5：线未出去时 F5 是**抛竿蓄力**键，
                        # 盲按会把力度一路攒到 130% 卡住（2026-11-09 实机事故）。
                        #
                        # 但环有歧义：短抛后环也读 1.0（实测），线其实还在水里
                        # （0e07 每秒一条）。协议说在流就必须收线，否则永远收不
                        # 回来 → WAIT_BITE 超时 → retrieve → 又判"在杯上" 死循环。
                        if self._line_on_spool(obs) and not self._protocol_line_in_water():
                            self._emit(
                                "[RETRIEVE] 线已在杯上（ring=%.2f），不发 F5"
                                % (obs.ring_ratio or 0.0)
                            )
                            return True
                        break
                if self._clock() > deadline:
                    raise RetrieveBlocked("retrieve_no_frame_evidence")
                if control.wait(RETRIEVE_POLL):
                    control.checkpoint()

            self._set_desired(desired, "retrieve_hold")
            while True:
                control.checkpoint()
                if not self._foreground():
                    self._set_desired(set(), "retrieve_focus_lost")
                    self._hold_foreground(control, desired)
                probe = self._probe(control)
                if probe is None:
                    ring_frames = 0
                    ready_frames = 0
                else:
                    image, obs = probe
                    ready = self._ready_verdict(image, obs)
                    now = self._clock()
                    if now - last_log >= STATUS_LOG_INTERVAL:
                        self._retrieve_status("loop", obs, ready)
                        last_log = now
                    if obs.fish_icon is T.TRUE:
                        # 鱼回来了：立刻松手，把控制权交回协调器，绝不继续收。
                        self._set_desired(set(), "retrieve_fish_seen")
                        return True
                    if ready is True:
                        ready_frames += 1
                        if ready_frames >= RETRIEVE_READY_FRAMES:
                            self._emit(
                                "[RETRIEVE] ready x%d ring=%s -> complete"
                                % (ready_frames, obs.ring_ratio)
                            )
                            self._set_desired(set(), "retrieve_ready_done")
                            return True
                    else:
                        ready_frames = 0
                    # .98 后备：必须本帧 ready 明确为 False、无鱼、HUD 可见。
                    #
                    # 2026-11-09 加协议否决：短抛后环也在 0.98 附近，但线其实
                    # 还在水里（0e07 每秒一条）。只看环会把"短抛等口"判成
                    # "已收完"，然后 PROBING 又当短抛 → 死循环。
                    if (
                        ready is False
                        and obs.fish_icon is T.FALSE
                        and obs.hud_visible is T.TRUE
                        and obs.ring_ratio is not None
                        and float(obs.ring_ratio) >= RETRIEVE_RING_MIN
                    ):
                        if self._protocol_line_in_water():
                            ring_frames = 0
                            self._emit(
                                "[RETRIEVE] ring says done but protocol says "
                                "line still in water -> keep reeling")
                            continue
                        ring_frames += 1
                        if ring_frames >= RETRIEVE_RING_FRAMES:
                            self._emit(
                                "[RETRIEVE] ring_fallback %.3f x%d -> complete"
                                % (float(obs.ring_ratio), ring_frames)
                            )
                            self._set_desired(set(), "retrieve_ring_done")
                            return True
                    else:
                        ring_frames = 0
                # 先消费本帧证据，再判 deadline：最后一帧永远有机会完成。
                if self._clock() > deadline:
                    raise RetrieveBlocked("retrieve_timeout")
                if control.wait(RETRIEVE_POLL):
                    control.checkpoint()
        finally:
            self._release_owned("retrieve_finally")

    # ------------------------------------------------------------------
    # 弹窗 / 入护
    # ------------------------------------------------------------------
    def _click(self, control: Any, cx: int, cy: int) -> None:
        """点击前确认前台；即使中途异常也补发一次 left-up，防卡住。"""
        if self._preflight_error:
            raise BridgeFault(self._preflight_error)
        control.checkpoint()
        if not self._foreground() and not self.ensure_foreground(
            control, action=True
        ):
            raise RuntimeError("foreground_required")
        A = self._require_adapter()
        try:
            A._click_client(int(cx), int(cy))
        finally:
            try:
                A._send_mouse(getattr(A, "_MOUSEEVENTF_LEFTUP", 0x0004))
            except Exception:  # noqa: BLE001 - best effort 防卡键
                pass
        if control.wait(0.05):
            control.checkpoint()

    # ------------------------------------------------------------------
    # 小退重登
    # ------------------------------------------------------------------
    def _relogin_io(self) -> Any:
        """给小退模块的最小 IO 适配层（抓图 / 点击 / 按键）。"""

        bridge = self

        class _Io:
            def capture(self) -> Any:
                A = bridge._require_adapter()
                return A.capture_game()[0]

            def client_size(self) -> Any:
                try:
                    return self.capture().size
                except Exception:  # noqa: BLE001 - 取不到尺寸就按 1080p
                    return (TARGET_W, TARGET_H)

            def click_client(self, cx: int, cy: int) -> None:
                A = bridge._require_adapter()
                A._click_client(int(cx), int(cy))

            def key(self, name: str) -> None:
                self.key_down(name)
                self.key_up(name)

            def key_down(self, name: str) -> None:
                if bridge.pdi is None:
                    raise RuntimeError("input_unavailable")
                bridge.pdi.keyDown(name)

            def key_up(self, name: str) -> None:
                if bridge.pdi is None:
                    raise RuntimeError("input_unavailable")
                bridge.pdi.keyUp(name)

        return _Io()

    def protocol_fight(self) -> Optional[bool]:
        """协议侧"现在是否在搏斗"（读抓包器 state.json，0.5s 缓存）。

        * True  = phase=fighting 且事件新鲜（0e07/0e08 在流或刚结束）
        * False = 明确不在搏斗（idle/等口/已结束）
        * None  = 抓包器没跑 / 快照过期 —— 调用方回退视觉判据

        只用于**补盲区**（鱼贴岸边、咬钩图标不亮），绝不能替代视觉主链路：
        没协议信息时必须返回 None 而不是 False，否则会让视觉判据失效。
        """
        now = time.time()
        if self._proto_cache is not None and now - self._proto_cache_at < 0.5:
            return self._proto_cache
        value: Optional[bool] = None
        try:
            import autofish_handoff as hf  # noqa: PLC0415
            snap = hf.read_snapshot()
            if snap is not None:
                ages = []
                if isinstance(snap.get("event_age"), (int, float)):
                    ages.append(float(snap["event_age"]))
                beat = snap.get("beat") or {}
                for key in ("step_age", "load_age"):
                    if isinstance(beat.get(key), (int, float)):
                        ages.append(float(beat[key]))
                alive = bool(ages) and min(ages) <= 10.0
                value = bool(snap.get("phase") == "fighting" and alive)
        except Exception:  # noqa: BLE001 - 协议只是补充判据
            value = None
        self._proto_cache = value
        self._proto_cache_at = now
        return value

    def protocol_line_in_water(self) -> Optional[bool]:
        """协议侧"线还在水里"（0e07 在流 / 0e08 在流）。无快照返 None。"""
        snap = self._protocol_snapshot()
        if snap is None:
            return None
        beat = snap.get("beat") or {}
        step_age = beat.get("step_age")
        load_age = beat.get("load_age")
        alive = False
        if isinstance(step_age, (int, float)) and float(step_age) <= 2.5:
            alive = True
        if isinstance(load_age, (int, float)) and float(load_age) <= 4.0:
            alive = True
        return alive

    def _protocol_line_in_water(self) -> bool:
        """协议侧是否"线还在水里"（0e07 在流，或 0e08 在流）。

        没协议信息一律 False（不影响原有环判据）。
        """
        snap = self._protocol_snapshot()
        if snap is None:
            return False
        now = time.time()
        beat = snap.get("beat") or {}
        step_age = beat.get("step_age")
        load_age = beat.get("load_age")
        if isinstance(step_age, (int, float)) and float(step_age) <= 2.5:
            return True
        if isinstance(load_age, (int, float)) and float(load_age) <= 4.0:
            return True
        return False

    def _protocol_snapshot(self) -> Any:
        """读抓包器快照（0.5s 缓存）。meta 与 protocol_fight 共用一份缓存。"""
        now = time.time()
        if now - self._proto_snap_at < 0.5:
            return self._proto_snap
        try:
            import autofish_handoff as hf  # noqa: PLC0415
            self._proto_snap = hf.read_snapshot()
        except Exception:  # noqa: BLE001
            self._proto_snap = None
        self._proto_snap_at = now
        return self._proto_snap

    def relogin(self, control: Any) -> bool:
        """线快被拉空时的小退重登：Esc → 退出 → 是 → 登录 → 回世界。

        成功返回 True；任何一步超时返回 False（由内核转 FAULT）。抓图/点击
        期间持续 ``control.checkpoint()``，所以停止请求仍然生效。
        """
        A = self.A
        if A is None or self._preflight_error:
            return False
        control.checkpoint()
        if not self._foreground() and not self.ensure_foreground(control, action=True):
            raise RuntimeError("foreground_required")

        from autofish_relogin import ReloginError  # noqa: PLC0415
        from autofish_relogin import ReloginConfig  # noqa: PLC0415
        from autofish_relogin import relogin as run_relogin  # noqa: PLC0415

        def _sleep(seconds: float) -> None:
            # 用 control.wait 代替 sleep，停止请求会立刻中断整个小退。
            if control.wait(seconds):
                control.checkpoint()

        try:
            summary = run_relogin(
                self._relogin_io(),
                control=control,
                cfg=ReloginConfig(),
                log=self._emit,
                sleep=_sleep,
                now=self._clock,
            )
        except ReloginError as exc:
            self._emit("[relogin] 失败：%s" % (exc,))
            return False
        self._emit("[relogin] 完成：%s" % (summary,))
        return True

    def dismiss(self, control: Any) -> bool:
        """最多 12 次「结构确认存在」的确定按钮点击；每次等待可取消。"""
        A = self.A
        if A is None or self._preflight_error:
            return False
        clicked = False
        try:
            for _ in range(DISMISS_MAX_CLICKS):
                control.checkpoint()
                if not self._foreground() and not self.ensure_foreground(
                    control, action=True
                ):
                    return False
                image, _pos = A.capture_game()
                if not self._foreground():
                    return False
                if not self._frame_valid(image):
                    return False
                state, _ev = aobs.detect_ok_modal(image)
                if state is not T.TRUE:
                    return clicked
                self._click(control, *A.OK_BTN_CENTER)
                clicked = True
                if control.wait(DISMISS_GAP):
                    control.checkpoint()
            control.checkpoint()
            if not self._foreground() and not self.ensure_foreground(
                control, action=True
            ):
                return False
            image, _pos = A.capture_game()
            if not self._foreground():
                return False
            if not self._frame_valid(image):
                return False
            state, _ev = aobs.detect_ok_modal(image)
            return bool(clicked and state is not T.TRUE)
        except AutoStop:
            raise
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            self._emit("[DISMISS] failed: %s" % (exc,))
            return False

    def _settle_present(self, control: Any) -> bool:
        """给 ``click_keep_until_settled`` 的 detect：强模板必须同帧。"""
        control.checkpoint()
        if not self._foreground() and not self.ensure_foreground(
            control, action=True
        ):
            raise RuntimeError("foreground_required")
        A = self._require_adapter()
        image, _pos = A.capture_game()
        if not self._foreground():
            raise RuntimeError("foreground_lost")
        if not self._frame_valid(image):
            raise RuntimeError("invalid_frame")
        obs = aobs.observe_frame(
            image,
            foreground=True,
            monotonic_ts=self._clock(),
            wall_ts=time.time(),
        )
        if obs.ok_modal is T.TRUE:
            return True  # 弹窗盖住结算：先当仍存在，让流程尝试清弹窗
        score = A._fight_strong_capture(image, str(self.keep_path))
        if score is None:
            raise RuntimeError("capture_template_unavailable")
        return float(score) >= STRONG_CAPTURE_CONFIDENCE

    def _locate_keep(self, control: Any):
        control.checkpoint()
        if not self._foreground() and not self.ensure_foreground(
            control, action=True
        ):
            raise RuntimeError("foreground_required")
        return self._require_adapter().find_capture_keep_btn()

    def _settle_wait(self, control: Any) -> None:
        if control.wait(KEEP_SETTLE_WAIT):
            control.checkpoint()

    def _verify_kept(self, control: Any) -> bool:
        """点击成功后的独立复核：新鲜、前台、有效帧、强模板消失、无弹窗。"""
        A = self.A
        if A is None or self.keep_path is None or self._preflight_error:
            return False
        try:
            control.checkpoint()
            if not self._foreground() and not self.ensure_foreground(
                control, action=True
            ):
                return False
            image, _pos = A.capture_game()
            ts = self._clock()
            if not self._foreground():
                return False
            if not self._frame_valid(image):
                return False
            if self._clock() - ts > 1.5:
                return False
            obs = aobs.observe_frame(
                image,
                foreground=True,
                monotonic_ts=ts,
                wall_ts=time.time(),
            )
            if obs.ok_modal is not T.FALSE:
                return False
            score = A._fight_strong_capture(image, str(self.keep_path))
            if score is None:
                return False
            return float(score) < STRONG_CAPTURE_CONFIDENCE
        except AutoStop:
            raise
        except (Exception, SystemExit):  # noqa: BLE001
            return False

    def keep(self, control: Any) -> bool:
        """沿用 ``click_keep_until_settled``，注入可取消的抓帧/定位/点击/等待。"""
        A = self.A
        if A is None or self.keep_path is None or self._preflight_error:
            return False
        try:
            control.checkpoint()
            if not self._foreground() and not self.ensure_foreground(
                control, action=True
            ):
                return False
            A.click_keep_until_settled(
                detect=lambda: self._settle_present(control),
                locate=lambda: self._locate_keep(control),
                click=lambda btn: self._click(control, int(btn[0]), int(btn[1])),
                wait=lambda: self._settle_wait(control),
                dismiss=lambda: self.dismiss(control),
            )
        except AutoStop:
            raise
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - 未确认入护 = 失败
            # click_keep_until_settled 会吞掉 detect/dismiss 里的一些异常；
            # 如果这期间已经请求 stop，必须把它还原成 AutoStop，别做新的动作。
            if self.control.stopped:
                raise AutoStop(self.control.stop_reason or "stop") from exc
            self._emit("[KEEP] failed: %s: %s" % (type(exc).__name__, exc))
            return False
        return self._verify_kept(control)

    def keepnet_full(self) -> bool:
        """满护模板检测（实际文件名为 ``keepnet_is_full.png``），不删物品。"""
        A = self.A
        if A is None or self.keepnet_path is None or self._preflight_error:
            return False
        try:
            if not self._foreground() and not self.ensure_foreground(
                self._action_control(), action=True
            ):
                return False
            image, _pos = A.capture_game()
            if not self._foreground():
                return False
            if not self._frame_valid(image):
                return False
            score = self._match_template_score(image, self.keepnet_path)
            return bool(
                score is not None and float(score) >= KEEPNET_CONFIDENCE
            )
        except (Exception, SystemExit):  # noqa: BLE001 - 探针失败不改变已确认的入护
            return False

    # ------------------------------------------------------------------
    # 运行层回调
    # ------------------------------------------------------------------
    def callbacks(self):
        from autofish_lifecycle import RuntimeCallbacks  # noqa: PLC0415

        return RuntimeCallbacks(
            capture_observation=self.capture_observation,
            input_sink=self.sink,
            readiness=self.readiness,
            calibrate=self.calibrate,
            cast=self.cast,
            keep=self.keep,
            dismiss=self.dismiss,
            retrieve=self.retrieve,
            wheel=self.wheel,
            net_action=self.net_action,
            net_retract_action=self.net_retract_action,
            friction_step=self.friction_step,
            friction_snapshot=self.friction_snapshot,
            keepnet_full=self.keepnet_full,
            relogin=self.relogin,
            protocol_fight=self.protocol_fight,
            protocol_line_in_water=self.protocol_line_in_water,
            log=self._emit,
            clock=self._clock,
        )


# ----------------------------------------------------------------------
# 进程入口
# ----------------------------------------------------------------------
def _write_pid(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _remove_pid(path: Path) -> None:
    try:
        if path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink()
    except Exception:  # noqa: BLE001
        pass


def _bot_absolute(value: str) -> Path:
    """相对路径一律落到 bot 目录，避免适配器 chdir 后漂移。"""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = BOT_DIR / path
    return path.resolve()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="RF4 autofish real bridge (does not enter upstream main loop)"
    )
    parser.add_argument(
        "--endpoint",
        default=None,
        help="IPC endpoint JSON (absolute; default: bot/autofish.endpoint.json)",
    )
    parser.add_argument(
        "--pidfile",
        default=str(DEFAULT_PIDFILE),
        help="PID file (absolute; default: bot/run.pid)",
    )
    parser.add_argument(
        "--retrieve-lift",
        action="store_true",
        help="also hold F6 while retrieving (only if the profile requires it)",
    )
    return parser


def _preflight(adapter: Any):
    """返回 (hwnd, keep_path, ready_path, keepnet_path, error)。"""
    if adapter is None:
        return None, None, None, None, "adapter_unavailable"
    try:
        hwnd = int(adapter.game_hwnd())
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        return None, None, None, None, "window_unavailable:%s" % (exc,)
    size = _read_client_size(hwnd)
    if size != (TARGET_W, TARGET_H):
        got = "%sx%s" % (size if size else ("?", "?"))
        return (
            hwnd,
            None,
            None,
            None,
            "window_size_%s_expected_%dx%d" % (got, TARGET_W, TARGET_H),
        )
    keep = _resolve_static_path(adapter, "keep.png")
    if keep is None:
        return hwnd, None, None, None, "capture_template_unavailable"
    try:
        template_ok = adapter._fight_keep_template(str(keep)) is not None
    except Exception as exc:  # noqa: BLE001
        template_ok = False
        keep = None
        return (
            hwnd,
            None,
            None,
            None,
            "capture_template_unavailable:%s" % (exc,),
        )
    if not template_ok:
        return hwnd, None, None, None, "capture_template_unavailable"
    ready = _resolve_static_path(adapter, "ready.png")
    keepnet = _resolve_static_path(adapter, "keepnet_is_full.png")
    if not READY_CN_PATH.is_file():
        # 中文 readiness 模板是抛竿/收线前置条件；缺失时只报 FAULT，
        # 绝不退回英文模板或“满环即就绪”的旧判据。
        return hwnd, keep, ready, keepnet, "ready_template_unavailable"
    return hwnd, keep, ready, keepnet, None


def main(argv: Optional[list[str]] = None) -> int:
    """正式入口：只在 main() 运行后才产生真实副作用。"""
    args = _build_parser().parse_args(argv)

    # 所有路径必须在导入适配器（会 chdir）之前解析成绝对路径。
    pidfile = _bot_absolute(args.pidfile)
    endpoint = _bot_absolute(args.endpoint) if args.endpoint else None
    _write_pid(pidfile)
    try:
        import bot_ctl
        bot_ctl.write_process_identity(os.getpid(), "bridge")
    except Exception:
        pass

    bridge: Optional[Bridge] = None
    server = None
    runtime = None
    rc = 0
    try:
        adapter = None
        load_error = None
        try:
            adapter = _load_adapter()
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            load_error = "adapter_unavailable:%s:%s" % (
                type(exc).__name__,
                exc,
            )

        if load_error:
            hwnd, keep, ready, keepnet = None, None, None, None
            preflight_error = load_error
        else:
            hwnd, keep, ready, keepnet, preflight_error = _preflight(adapter)

        control = ControlSession()
        # 单源生产配置：Bridge gate 与 FightConfig 必须使用同一个结果。
        auto_net_enabled = resolve_auto_net_enabled()
        from autofish_fight_machine import FightConfig  # noqa: PLC0415
        fight_config = FightConfig(auto_net_enabled=auto_net_enabled)
        input_backend = None
        if preflight_error is None:
            import pydirectinput as input_backend
            # 只去掉库内固定停顿；FAILSAFE 保持默认开启（鼠标物理顶角仍会
            # 抛 FailSafeException，由 sink 记录真实 cause 后交给上层 FAULT）。
            input_backend.PAUSE = 0.0
        bridge = Bridge(
            adapter,
            control=control,
            pdi=input_backend,
            hwnd=hwnd,
            keep_path=keep,
            ready_path=ready,
            keepnet_path=keepnet,
            preflight_error=preflight_error,
            retrieve_lift=bool(args.retrieve_lift),
            auto_net_enabled=auto_net_enabled,
            fight_auto_net_enabled=fight_config.auto_net_enabled,
        )
        bridge_preflight = getattr(bridge, "preflight_error", None)
        if not isinstance(bridge_preflight, str):
            bridge_preflight = preflight_error
        net_config_error = getattr(bridge, "_net_config_error", None)
        if preflight_error is None and not (
            isinstance(net_config_error, str) and net_config_error
        ):
            # 预检失败时连摩擦状态都不碰：只启动 IPC 展示 FAULT。
            bridge.install()

        from autofish_ipc import RuntimeServer  # noqa: PLC0415
        from autofish_lifecycle import LifecycleConfig  # noqa: PLC0415
        from autofish_runtime import AutoFishRuntime  # noqa: PLC0415

        server = RuntimeServer(control, endpoint)
        server.start()
        runtime = AutoFishRuntime(
            bridge.callbacks(),
            control=control,
            # 只从 RF4_AUTO_NET 单源读取；默认 False，实机验收前不启用。
            config=LifecycleConfig(fight=fight_config),
            initial_auto=False,   # 生产必须由 UI 显式 resume/confirm_ready
            server=server,
        )
        # ---- 协议半途接手：抓包器活着时按其状态快照选起点状态 ----
        # 顺序：runtime 起来后、主循环开跑前，一次性应用。抓包器没跑/
        # 快照过期时 resolve() 返回 unknown → 走默认 BOOT→PROBING。
        try:
            import autofish_handoff  # noqa: PLC0415
            handoff = autofish_handoff.resolve()
            applied = runtime.coordinator.apply_protocol_handoff(handoff)
            print(
                "[BRIDGE] handoff phase=%s enter=%s keys=%s applied=%s (%s)"
                % (handoff.get("phase"), handoff.get("enter"),
                   handoff.get("keys"), applied, handoff.get("reason")),
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - 接手失败不阻塞启动
            print("[BRIDGE] handoff skipped: %s" % exc, flush=True)
        bridge.attach(runtime.coordinator)

        print(
            "[BRIDGE] pid=%d endpoint=%s window=%s preflight=%s auto_net=%s"
            % (
                os.getpid(),
                getattr(server, "endpoint_path", endpoint),
                "%sx%s" % (TARGET_W, TARGET_H),
                bridge_preflight or "ok",
                auto_net_enabled,
            ),
            flush=True,
        )
        if bridge_preflight:
            print(
                "[BRIDGE][FAULT] %s（窗口只读检查，不 resize/move）"
                % bridge_preflight,
                flush=True,
            )

        runtime.run()
    except KeyboardInterrupt:
        try:
            if bridge is not None:
                bridge.control.request_stop("keyboard_interrupt")
            if runtime is not None:
                runtime.shutdown()
        except Exception:  # noqa: BLE001
            pass
        rc = 130
    except Exception as exc:  # noqa: BLE001
        print(
            "[BRIDGE][FATAL] %s: %s" % (type(exc).__name__, exc),
            flush=True,
        )
        rc = 1
    finally:
        if runtime is not None:
            try:
                runtime.close()   # shutdown + server.close
            except Exception:  # noqa: BLE001
                pass
        elif server is not None:
            try:
                server.close()
            except Exception:  # noqa: BLE001
                pass
        if bridge is not None:
            bridge.release_owned("bridge_finally")
            bridge.restore()
        _remove_pid(pidfile)
    return rc


if __name__ == "__main__":
    sys.exit(main())
