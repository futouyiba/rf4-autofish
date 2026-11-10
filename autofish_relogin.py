"""小退重登：Esc → 「退出」 → 「是」 → 登录界面 → 「登录」 → 回到世界。

## 为什么不用 OCR

小退是**界面状态迁移**问题，不是"读文本"问题。每一步要回答的都是"现在在哪个
界面"，而界面元素的位置和形状是固定的：

* 世界：整屏亮度均值很高（场景是亮的）；
* 加载 / 菜单：整屏压暗；
* 登录界面：亮度均值落在窄区间，且"登录按钮带"只有一小部分像素是亮的。

参考实现（中文版 ``stages/relogin.py``）同样全程模板匹配，虽然 ``import`` 了
OCR 但一次都没调用。我们连模板图都不需要，用亮度结构判据就够，因此这个模块
不依赖 OCR 服务、不依赖 1920×1080 的模板截图。

## 坐标从哪来

``(1418,743)``「退出」、``(858,565)``「是」、``(956,723)``「登录」来自
``rf4-client-ops`` 技能里对本客户端实测的坐标表（1920×1080 客户区）。本模块
按实际客户区尺寸等比缩放，窗口不是 1080p 也能用。

## 失败怎么办

每一步都有单步超时，整体还有总超时。**任何一步失败都抛 :class:`ReloginError`，
绝不盲目重试到无限**：状态机会把它转成 FAULT 交还人工。唯一的重试是"点了退出
但画面没变"这种明确可观测的失败——退一步重来一次，且计入次数上限。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, Tuple

# ---------------------------------------------------------------- 坐标（1920×1080 基准）
EXIT_BTN = (1418, 743)      # ESC 主菜单里的「退出」
CONFIRM_BTN = (858, 565)    # 「确定要退出游戏吗」的「是」
LOGIN_BTN = (956, 723)      # 登录界面的「登录」

BASE_SIZE = (1920, 1080)

# ---------------------------------------------------------------- 四态判据阈值
# 与 rf4-client-ops/scripts/rf4win.py 的实测值一致：登录界面 mean≈69、
# 按钮带亮度占比≈5.3%；水下鱼群会出现 mean≈49、bright≈30% 的白块，
# 所以登录判据必须卡**区间**，不能只看单一指标。
WORLD_MEAN_MIN = 90.0
DARK_MEAN_MAX = 48.0
LOGIN_MEAN_RANGE = (60.0, 80.0)
LOGIN_BRIGHT_RANGE = (2.0, 12.0)
# 登录按钮带 ROI：y0, y1, x0, x1（按 1080p 标定，按实际尺寸换算）
LOGIN_BAND = (695, 750, 820, 1090)

KIND_WORLD = "world"
KIND_DARK = "dark"
KIND_LOGIN = "login"
KIND_ERROR = "error"


class ReloginError(RuntimeError):
    """小退流程失败；``stage`` 指出卡在哪一步，供日志与 UI 展示。"""

    def __init__(self, stage: str, detail: str = "") -> None:
        self.stage = stage
        self.detail = detail
        super().__init__("%s:%s" % (stage, detail) if detail else stage)


class ReloginIo(Protocol):
    """小退需要的最小副作用集合（由 bridge 注入，测试可替换）。"""

    def capture(self) -> Any:
        """返回当前游戏客户区画面（PIL.Image 或 HxWx3 ndarray）。"""

    def click_client(self, cx: int, cy: int) -> None:
        """在客户区坐标 (cx, cy) 点击一次。"""

    def key(self, name: str) -> None:
        """按一次键（down+up）。"""

    def key_down(self, name: str) -> None:
        ...

    def key_up(self, name: str) -> None:
        ...


@dataclass(frozen=True)
class ReloginConfig:
    """时间与重试上限。所有时间单位为秒。"""

    # 按 Esc 之后等菜单画出来的时间
    esc_settle: float = 0.9
    # 点「退出」之后等确认框
    confirm_settle: float = 0.8
    # 点「是」之后等登录界面出现
    login_timeout: float = 75.0
    # 点「登录」之后等回到世界
    world_timeout: float = 150.0
    # 轮询间隔
    poll: float = 0.4
    # 退出菜单点击是否先按住 Shift（本客户端需要；实测失效会回退普通点击）
    shift_exit: bool = True
    # "点了退出但画面没变" 时最多重来几次
    exit_retries: int = 1
    # "点了登录但还在登录界面" 时最多补点几次
    login_retries: int = 2
    # 总超时（含所有重试）
    total_timeout: float = 240.0
    exit_btn: Tuple[int, int] = EXIT_BTN
    confirm_btn: Tuple[int, int] = CONFIRM_BTN
    login_btn: Tuple[int, int] = LOGIN_BTN
    base_size: Tuple[int, int] = BASE_SIZE


def _gray(frame: Any) -> Any:
    import numpy as np

    a = np.asarray(frame)
    if a.ndim == 3:
        a = a[:, :, :3].mean(axis=2)
    return a.astype(float)


def screen_kind(image: Any) -> Tuple[str, float, float]:
    """返回 (kind, mean, bright_pct)。kind ∈ world/dark/login/error。

    判据顺序很重要：世界必须**先**判，因为世界里的 UI 也会把登录按钮带点亮。
    全黑（截图失败）会落到 dark，由调用方按"加载中"继续等，不会误判成登录。
    """
    a = _gray(image)
    if a.ndim != 2 or a.size == 0:
        return KIND_ERROR, 0.0, 0.0
    h, w = a.shape
    mean = float(a.mean())

    y0, y1, x0, x1 = LOGIN_BAND
    by0, by1 = int(y0 * h / 1080), int(y1 * h / 1080)
    bx0, bx1 = int(x0 * w / 1920), int(x1 * w / 1920)
    band = a[by0:by1, bx0:bx1]
    bright = float((band > 150).mean() * 100.0) if band.size else 0.0

    if mean > WORLD_MEAN_MIN:
        return KIND_WORLD, mean, bright
    if mean < DARK_MEAN_MAX:
        return KIND_DARK, mean, bright
    lo, hi = LOGIN_MEAN_RANGE
    blo, bhi = LOGIN_BRIGHT_RANGE
    if lo <= mean <= hi and blo <= bright <= bhi:
        return KIND_LOGIN, mean, bright
    return KIND_ERROR, mean, bright


def scale_point(point: Tuple[int, int], size: Tuple[int, int],
                base: Tuple[int, int] = BASE_SIZE) -> Tuple[int, int]:
    """把 1920×1080 基准坐标换算到实际客户区尺寸。"""
    bw, bh = base
    w, h = size
    if bw <= 0 or bh <= 0:
        raise ValueError("base size must be positive")
    return (int(round(point[0] * w / bw)), int(round(point[1] * h / bh)))


def _noop(_msg: str) -> None:
    return


class _Clock:
    def __init__(self, sleep: Optional[Callable[[float], None]] = None,
                 now: Optional[Callable[[], float]] = None) -> None:
        self.sleep = sleep or time.sleep
        self.now = now or time.monotonic


def _checkpoint(control: Any) -> None:
    """响应停止请求；没有 control 时什么也不做。"""
    if control is None:
        return
    fn = getattr(control, "checkpoint", None)
    if callable(fn):
        fn()


def _wait_until(io: Any, control: Any, cfg: ReloginConfig,
                clock: _Clock, deadline: float,
                want: Tuple[str, ...], label: str) -> Tuple[str, float]:
    """轮询到画面进入 want 之一；超时抛 ReloginError。返回 (kind, mean)。"""
    last_kind, last_mean = KIND_ERROR, 0.0
    while True:
        _checkpoint(control)
        if clock.now() > deadline:
            raise ReloginError(label + "_timeout",
                               "last=%s mean=%.1f" % (last_kind, last_mean))
        try:
            image = io.capture()
        except Exception as exc:  # noqa: BLE001 - 抓图失败不改变流程，继续等
            last_kind, last_mean = KIND_ERROR, 0.0
            clock.sleep(cfg.poll)
            continue
        last_kind, last_mean, _bright = screen_kind(image)
        if last_kind in want:
            return last_kind, last_mean
        clock.sleep(cfg.poll)


def _click(io: Any, cfg: ReloginConfig, point: Tuple[int, int],
           size: Tuple[int, int], *, shift: bool = False) -> None:
    cx, cy = scale_point(point, size, cfg.base_size)
    if shift:
        io.key_down("shift")
        try:
            io.click_client(cx, cy)
        finally:
            io.key_up("shift")
    else:
        io.click_client(cx, cy)


def _client_size(io: Any) -> Tuple[int, int]:
    fn = getattr(io, "client_size", None)
    if callable(fn):
        try:
            w, h = fn()
            if w > 0 and h > 0:
                return int(w), int(h)
        except Exception:  # noqa: BLE001
            pass
    return BASE_SIZE


def relogin(io: Any, control: Any = None, cfg: Optional[ReloginConfig] = None,
            log: Optional[Callable[[str], None]] = None,
            *, sleep: Optional[Callable[[float], None]] = None,
            now: Optional[Callable[[], float]] = None) -> dict:
    """执行一次小退重登，成功返回摘要 dict，失败抛 :class:`ReloginError`。

    ``io`` 提供抓图/点击/按键；``control`` 只用于响应停止请求。调用方**必须**
    已经松开所有按键（本函数只发 Esc / Shift / 鼠标点击）。
    """
    cfg = cfg or ReloginConfig()
    emit = log or _noop
    clock = _Clock(sleep, now)
    started = clock.now()
    deadline_total = started + cfg.total_timeout
    size = _client_size(io)

    def _budget(step: float) -> float:
        return min(clock.now() + step, deadline_total)

    emit("relogin: 开始（窗口 %dx%d）" % size)

    # ---- 1~3. Esc → 点「退出」→ 点「是」→ 等登录界面 ----
    for attempt in range(cfg.exit_retries + 1):
        _checkpoint(control)
        io.key("esc")
        clock.sleep(cfg.esc_settle)
        kind, mean, _b = _read(io, KIND_ERROR)
        emit("relogin: 菜单 %s mean=%.1f" % (kind, mean))

        _click(io, cfg, cfg.exit_btn, size, shift=cfg.shift_exit)
        clock.sleep(cfg.confirm_settle)
        _click(io, cfg, cfg.confirm_btn, size)
        try:
            kind, mean = _wait_until(io, control, cfg, clock,
                                     _budget(cfg.login_timeout),
                                     (KIND_LOGIN,), "login")
            emit("relogin: 到登录界面 mean=%.1f（第 %d 次）" % (mean, attempt + 1))
            break
        except ReloginError as exc:
            if attempt >= cfg.exit_retries:
                raise
            emit("relogin: %s（%s），重开菜单再试一次" % (exc.stage, exc.detail))

    # ---- 4. 点「登录」，等回到世界 ----
    clicks = 0
    login_at = clock.now()
    while True:
        _checkpoint(control)
        if clicks > cfg.login_retries:
            raise ReloginError("world_timeout", "登录点击 %d 次仍未进入世界" % clicks)
        _click(io, cfg, cfg.login_btn, size)
        clicks += 1
        emit("relogin: 已点登录（第 %d 次），等世界" % clicks)
        try:
            kind, mean = _wait_until(io, control, cfg, clock,
                                     _budget(cfg.world_timeout), (KIND_WORLD,), "world")
            break
        except ReloginError:
            # 还在登录界面 ⇒ 补点一次；其它情况（加载/菜单）同样补点一次后放弃
            if clicks > cfg.login_retries:
                raise
            continue

    elapsed = clock.now() - started
    emit("relogin: 完成，用时 %.1fs" % elapsed)
    return {
        "ok": True,
        "elapsed": round(elapsed, 3),
        "login_clicks": clicks,
        "mean": round(mean, 1),
        "stage": "world",
    }


def _read(io: Any, default: str) -> Tuple[str, float, float]:
    try:
        return screen_kind(io.capture())
    except Exception:  # noqa: BLE001
        return default, 0.0, 0.0
