"""RF4 自动钓鱼 —— 新搏鱼内核的 feature-flag 运行层。

本模块把 `FightMachine` / `InputArbiter` / 同帧观测接到真实流程上，但**不
import 适配器**：所有会产生副作用的东西（抓帧、前台判断、按键、滚轮、模板
匹配、日志）都通过 `FightCallbacks` 注入。离线测试用 fake 回调即可跑完整
会话，不需要启动游戏、截图或真实输入。

开关（适配器在打完全部旧补丁后调用 `install_player_patches`）：

    RF4_FIGHT_FSM=1   -> 启用
    其它/未设置       -> 保持 legacy（默认关闭，bot_ctl 不变）

接入点刻意在 `Player` 而不是 `Tackle`：

* `Player.pull_fish` 整个替换成新内核，不再走 `hold_keys` / `error_handler`
  那套旧输入 owner（它们会在异常路径强制松键再重入）；同时保留原实现的
  `cur_coffee=0` 与 `save_bite_screenshot(save=True)` 语义；
* `Player.retrieve_line` **完全重写**，绝不回退到原函数（原函数会在自己的
  `hold_keys` 里调用 `pull_fish`，造成新旧两个输入 owner）：
  先判 `is_fish_hooked()`，有鱼直接进新 `pull_fish`；否则在
  `hold_keys(mouse=True, shift=False)` 里调用 `tackle.retrieve`。若收线途中
  抛 `FishHookedError`，先退出 context（收线输入 owner 完整结束），再调用
  新 `pull_fish`；正常收完同样退出 context 后再判一次咬钩。这里不使用原
  `error_handler`，避免它捕获异常后在旧 owner 里重入；
* `Player.lift_fish` 在新内核刚判 LOST、或本轮已经进入过新会话时直接返回，
  不再跑旧的 30s/600s 抬竿等待；只有"收线完成且没有新会话"才保留旧 lift。

捕获确认是**强确认**：运行层要求适配器对**同一帧**做 keep.png 模板匹配
（阈值 0.95 + `CAPTURE_POS_BOX`），连续两个独立、新鲜、前台的帧命中才确认。
强信号是 UNKNOWN（模板加载失败/画面不可读）时 captured 一律降为 UNKNOWN，
**绝不回退到弱几何判据**。确认后调用 `machine.stop()` 并返回 CAPTURED，由
包装层抛 `FishCapturedError` 交给现有 `loop_restart_handler` 入护。

会话起点：

* 已校准、`_restored` 为假、且 `cur` 落在 1..29 的合法整数区间：保留当前
  档位（这是**同一条鱼重入**的场景）。注意每竿 `cast` 仍然由旧
  `friction_absolute(17)` 回到 17，本内核不会在抛竿后再滚一次；
* 未知 / `_restored` / `cur` 越界或非有限值（30、NaN 等）：一律走安全校准
  `wheel_raw(-30)` 再 `+16`，成功后提交 17 并落盘；失败立即终止新会话，
  **不回退 legacy**；
* 每次滚轮前都重新确认前台，任何一步失焦立即 BLOCKED；
* 校准前先确认前台 + 1920x1080 有效帧，否则返回 BLOCKED，包装层抛受控
  `RuntimeError` 停止（不静默重抛竿）；
* 强结算模板不可用时 preflight 直接 BLOCKED，绝不先按住键再永远等不到
  捕获路径；
* `machine.start()` 在校准之前，并用 try/finally 保证任何路径都会 stop 释放。

**legacy 隔离**：本模块的 `FightCallbacks` 只有 `net_action`，没有
`net_retract_action`；`machine.start()` 也按中途接管使用 `NetState.UNKNOWN`。
因此即使显式打开 `auto_net_enabled`，这里也不会盲 toggle/自动抄网。正式
自动抄网链路只在 `autofish_bridge.py` + `autofish_lifecycle.py`，本模块不得
作为抄网验收入口。

日志策略：状态迁移立即打一行，另加 10s 心跳（cur/ring/bar/desired/emitted/
捕获 score/elapsed，elapsed 用 monotonic）。本模块不引入线程或队列：每帧
最多一行，抓帧与模板匹配才是耗时项，同步 `print(flush=True)` 不会改变控制
节奏；这也是“输出不阻塞策略”的实现说明。
"""

from __future__ import annotations

import dataclasses
import enum
import math
import os
import time
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable

import autofish_fight_machine as afm
import autofish_input as ain
import autofish_observation as aobs

__all__ = [
    "CalibrationResult",
    "FightCallbacks",
    "FightOutcome",
    "FightReleaseError",
    "FightRuntime",
    "FightSessionAbort",
    "FightSessionResult",
    "REFERENCE_SIZE",
    "STRONG_CAPTURE_CONFIDENCE",
    "STRONG_CAPTURE_FRAMES",
    "feature_enabled",
    "install_player_patches",
]

REFERENCE_SIZE = (1920, 1080)
STRONG_CAPTURE_CONFIDENCE = 0.95
STRONG_CAPTURE_FRAMES = 2    # 两个独立新鲜帧，硬编码为安全规则
ENV_FLAG = "RF4_FIGHT_FSM"
DEFAULT_INTERVAL = 0.25      # 4Hz，落在 2~5Hz 的合理区间
DEFAULT_HEARTBEAT = 10.0
SAFE_FRICTION_MIN = 1        # 与适配器的 FRICTION_MIN 对齐
SAFE_FRICTION_MAX = 29       # 与适配器的 FRICTION_MAX 对齐
STOP_RELEASE_ATTEMPTS = 3    # 收尾松键最多重试次数，绝不无限重试


class FightReleaseError(RuntimeError):
    """收尾释放按键失败：外层流程必须停止，不能再入护/recast。"""


class FightSessionAbort(RuntimeError):
    """新会话返回 CANCELLED/BLOCKED/REENTERED 时的受控中止错误。

    这三类结果都不能再回到 legacy 的 `lift_fish` / 重抛竿路径；包装层统一
    抛这个错误让外层流程停下（`KeyboardInterrupt` 仍按原样传播）。
    """


def feature_enabled(environ: dict[str, str] | None = None) -> bool:
    """只有显式 `RF4_FIGHT_FSM=1` 才启用新内核；默认 legacy。"""
    env = os.environ if environ is None else environ
    return str(env.get(ENV_FLAG, "")).strip() == "1"


# ==================== 注入的回调 ====================


@dataclass
class FightCallbacks:
    """运行层需要的全部副作用，由适配器实现、测试用 fake 实现。

    约定：

    * `capture()` 每次只抓**一张**图；运行层保证同一帧的观测/模板匹配都用
      这一张。
    * `observe(image, foreground, monotonic_ts)` 必须把同一个 `monotonic_ts`
      写进 Observation，且该时间戳在调用**之前**取（适配器负责）。
    * `strong_capture(image)` 返回 keep.png 在 `CAPTURE_POS_BOX` 内的最高
      模板分（0..1）；模板不可用时返回 `None`（= UNKNOWN，禁止几何兜底）。
    * `sink(key, is_down, reason)` 只允许 f5/f6；失败必须抛异常或返回 False。
    * `friction_status()` -> (cur, calibrated, restored)。
    * `wheel_raw(n)` 只发滚轮不记账，返回是否送达。
    * `commit_friction(cur)` 记账 + 落盘。
    * `friction_step(level)`：level 为 int 时把本帧读数喂给摩擦控制器；
      为 None 时表示冻结（清空窗口，不调控制器）。
    * `friction_begin()`：可选；每次 `run()` 确认新会话后、校准前调用一次，
      只清摩擦观察窗/epoch，**不动当前档位、不发滚轮**。
    * `cancel()` 是显式取消入口（离线测试用），生产靠 KeyboardInterrupt。
    * `preflight_error()` 在 `machine.start()` 之前调用；返回非空字符串即
      BLOCKED（例如强结算模板加载失败），此时绝不按下任何键。
    """

    capture: Callable[[], Any]
    observe: Callable[[Any, bool, float], aobs.Observation]
    foreground: Callable[[], bool]
    strong_capture: Callable[[Any], float | None]
    sink: Callable[[str, bool, str], Any]
    net_action: Callable[[], Any]
    friction_status: Callable[[], tuple[int, bool, bool]]
    wheel_raw: Callable[[int], bool]
    commit_friction: Callable[[int], Any]
    friction_step: Callable[[int | None], Any]
    friction_begin: Callable[[], Any] | None = None
    clock: Callable[[], float] = time.monotonic
    log: Callable[[str], Any] = print
    frame_valid: Callable[[Any], bool] | None = None
    cancel: Callable[[], bool] | None = None
    # 会话能力检查：返回 None 表示可开始；返回字符串 = preflight BLOCKED 原因。
    # 适配器用它在持键前校验强结算模板、ELECTRO/POST_ACCELERATION 等不支持项。
    preflight_error: Callable[[], str | None] | None = None


class FightOutcome(enum.Enum):
    CAPTURED = "captured"
    LOST = "lost"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    REENTERED = "reentered"


@dataclass(frozen=True)
class CalibrationResult:
    ok: bool
    cur: int
    wheeled: tuple[int, ...]
    reason: str


@dataclass(frozen=True)
class FightSessionResult:
    outcome: FightOutcome
    reason: str
    elapsed: float
    frames: int
    max_capture_score: float | None
    calibration: CalibrationResult | None = None
    events: tuple[afm.FightEvent, ...] = field(default_factory=tuple)


def _default_frame_valid(image: Any) -> bool:
    """默认只校验参考尺寸；适配器会再叠一层非全黑判断。"""
    if image is None:
        return False
    size = getattr(image, "size", None)
    if isinstance(size, (tuple, list)) and len(size) == 2:
        return tuple(int(v) for v in size) == REFERENCE_SIZE
    shape = getattr(image, "shape", None)
    if shape is not None and len(shape) >= 2:
        return (int(shape[1]), int(shape[0])) == REFERENCE_SIZE
    return False


# ==================== 运行层 ====================


class FightRuntime:
    """一次进程内可复用的搏鱼会话执行器。

    构造只建 arbiter/machine，不读 clock、不碰任何回调；`run()` 才产生副作用。
    同一个实例被重复进入时返回 REENTERED，**不会**再建第二个 arbiter，也不会
    多发一次 keyDown（双 owner 的根因就是两个 owner 各自记一份 emitted）。
    """

    def __init__(
        self,
        callbacks: FightCallbacks,
        *,
        config: afm.FightConfig | None = None,
        interval: float = DEFAULT_INTERVAL,
        heartbeat: float = DEFAULT_HEARTBEAT,
    ) -> None:
        if not isinstance(callbacks, FightCallbacks):
            raise TypeError("FightRuntime requires FightCallbacks")
        if interval <= 0:
            raise ValueError("interval must be > 0")
        self._cb = callbacks
        self._config = config or afm.FightConfig()
        self._interval = float(interval)
        self._heartbeat = float(heartbeat)
        self._arbiter = ain.InputArbiter(callbacks.sink, key_order=(ain.F5, ain.F6))
        self._machine = afm.FightMachine(
            self._arbiter,
            clock=callbacks.clock,
            net_action=callbacks.net_action,
            config=self._config,
        )
        self._busy = False
        self._calibrated = False
        self._calibration: CalibrationResult | None = None
        self._capture_frames = 0
        self._capture_last_ts: float | None = None
        self._max_capture_score: float | None = None
        self._net_hint_printed = False
        self._release_failed = False

    # ---------- 只读视图（测试/诊断） ----------

    @property
    def machine(self) -> afm.FightMachine:
        return self._machine

    @property
    def arbiter(self) -> ain.InputArbiter:
        return self._arbiter

    @property
    def calibrated(self) -> bool:
        return self._calibrated

    def _log(self, text: str) -> None:
        try:
            self._cb.log(f"[FIGHT] {text}")
        except Exception:
            pass

    def _friction_cur(self) -> str:
        try:
            return str(self._cb.friction_status()[0])
        except Exception:
            return "?"

    def _frame_valid(self, image: Any) -> bool:
        check = self._cb.frame_valid or _default_frame_valid
        try:
            return bool(check(image))
        except Exception:
            return False

    def _is_cancelled(self) -> bool:
        if self._cb.cancel is None:
            return False
        try:
            return bool(self._cb.cancel())
        except Exception:
            return False

    # ---------- 会话起点 ----------

    def _preflight(self) -> str | None:
        """前台 + 参考尺寸有效帧；任何一项失败都返回原因（不 start）。"""
        try:
            if not self._cb.foreground():
                return "foreground_lost"
        except Exception as exc:  # noqa: BLE001 - 窗口层失败等同失焦
            return f"foreground_error:{type(exc).__name__}"
        try:
            image = self._cb.capture()
        except Exception as exc:  # noqa: BLE001
            return f"capture_error:{type(exc).__name__}"
        if not self._frame_valid(image):
            return "invalid_frame"
        if self._cb.preflight_error is not None:
            try:
                reason = self._cb.preflight_error()
            except Exception as exc:  # noqa: BLE001 - 能力检查失败=BLOCKED
                return f"preflight_error:{type(exc).__name__}"
            if reason:
                return str(reason)
        return None

    def _calibrate(self) -> CalibrationResult:
        """会话起点的摩擦片校准。

        只有"已校准 + 非 `_restored` + cur 是 1..29 的合法整数"才保留 cur
        （同一条鱼重入的场景）；每竿 `cast` 仍由旧 `friction_absolute` 回到
        17，本内核不会二次滚动。cur 越界/NaN/非整数一律视为未知，走安全
        校准 -30 -> +16；失败终止（不回退 legacy）。
        每次滚轮前都重检前台；全程不会向上滚 30，提交值也走 29 护栏。
        """
        try:
            raw_cur, raw_calibrated, raw_restored = self._cb.friction_status()
        except Exception as exc:  # noqa: BLE001
            return CalibrationResult(
                False, 0, (), f"friction_status_error:{type(exc).__name__}"
            )

        calibrated = bool(raw_calibrated)
        restored = bool(raw_restored)
        cur = 0
        valid_cur = False
        try:
            value = float(raw_cur)
            if (
                math.isfinite(value)
                and value.is_integer()
                and SAFE_FRICTION_MIN <= int(value) <= SAFE_FRICTION_MAX
            ):
                cur = int(value)
                valid_cur = True
        except (TypeError, ValueError):
            pass
        if not valid_cur:
            # 30 / NaN / 字符串等一律不接受，强制走安全校准。
            calibrated = False

        if calibrated and not restored:
            return CalibrationResult(True, cur, (), "already_calibrated")

        wheeled: list[int] = []
        for n, why in ((-30, "calibrate_down"), (16, "calibrate_up")):
            # 每一步滚轮前都重新确认前台：_wheel_raw 会主动置前抢焦点。
            try:
                if not self._cb.foreground():
                    return CalibrationResult(
                        False, cur, tuple(wheeled), "foreground_lost"
                    )
            except Exception as exc:  # noqa: BLE001
                return CalibrationResult(
                    False, cur, tuple(wheeled),
                    f"foreground_error:{type(exc).__name__}",
                )
            wheeled.append(n)
            try:
                ok = bool(self._cb.wheel_raw(n))
            except Exception as exc:  # noqa: BLE001
                return CalibrationResult(
                    False, cur, tuple(wheeled),
                    f"{why}_error:{type(exc).__name__}",
                )
            if not ok:
                return CalibrationResult(
                    False, cur, tuple(wheeled), f"{why}_not_delivered"
                )

        # 只提交 17；适配器 _commit_friction 里还有 FRICTION_MAX=29 的硬护栏。
        target = 17
        try:
            self._cb.commit_friction(target)
        except Exception as exc:  # noqa: BLE001
            return CalibrationResult(
                False, cur, tuple(wheeled),
                f"commit_error:{type(exc).__name__}",
            )
        return CalibrationResult(True, target, tuple(wheeled), "calibrated")

    # ---------- 主循环 ----------

    def run(self) -> FightSessionResult:
        """执行一次搏鱼会话。所有异常路径都会先 stop 释放按键再抛出。"""
        if self._busy:
            return FightSessionResult(
                FightOutcome.REENTERED, "already_running", 0.0, 0, None
            )

        self._busy = True
        self._release_failed = False
        t0 = self._now()
        try:
            blocked = self._preflight()
            if blocked is not None:
                self._log(f"不启动新会话：{blocked}（不抢焦点、不重抛竿）")
                return FightSessionResult(
                    FightOutcome.BLOCKED, blocked, 0.0, 0, None
                )

            try:
                self._machine.start()
            except BaseException:
                self._stop_quiet("start_failed")
                raise

            # 新会话（确认中鱼）先清摩擦观察窗；不碰当前档位、不发滚轮。
            # 跨鱼残留的 last_action / last_drop / observe_since 必须在这里断掉。
            if self._cb.friction_begin is not None:
                try:
                    self._cb.friction_begin()
                except Exception as exc:  # noqa: BLE001 - 清理失败不阻断搏鱼
                    self._log(f"摩擦观察窗重置失败：{type(exc).__name__}: {exc}")

            try:
                self._calibration = self._calibrate()
                if not self._calibration.ok:
                    self._log(
                        "摩擦片校准失败，终止新会话（不回退 legacy）："
                        f"{self._calibration.reason}"
                    )
                    return FightSessionResult(
                        FightOutcome.BLOCKED,
                        f"calibration_failed:{self._calibration.reason}",
                        self._elapsed(t0), 0, None, self._calibration,
                        tuple(self._machine.events),
                    )
                self._calibrated = True
                self._log(
                    f"会话开始 cur={self._calibration.cur} "
                    f"calibration={self._calibration.reason}"
                )
                return self._loop(t0)
            finally:
                # `_loop` 的每个终止路径已经 stop 过；若那里因释放失败耗尽
                # 3 次重试，就不能在 finally 里再开一轮，避免无限重试。
                if not self._release_failed:
                    self._stop_quiet("session_end")
        finally:
            self._busy = False

    def _loop(self, t0: float) -> FightSessionResult:
        last_state = self._machine.state
        last_log = self._now()
        frames = 0
        self._capture_frames = 0
        self._capture_last_ts = None
        self._max_capture_score = None
        self._net_hint_printed = False

        while True:
            if self._is_cancelled():
                self._log("收到显式取消，释放按键")
                return self._result(FightOutcome.CANCELLED, "cancelled", t0, frames)

            try:
                image = self._cb.capture()
            except Exception as exc:  # noqa: BLE001 - 抓帧失败不能静默继续
                self._log(f"抓帧失败：{type(exc).__name__}: {exc}")
                raise

            try:
                foreground = bool(self._cb.foreground())
            except Exception:
                foreground = False

            # 适配器契约：observe_frame 之前取 monotonic，同一个 ts 写进观测。
            frame_ts = self._now()
            obs = self._cb.observe(image, foreground, frame_ts)
            frames += 1

            score = self._strong_score(image)
            obs = self._apply_capture_evidence(obs, score, foreground)

            confirmed = self._capture_confirmed(obs, foreground)
            if confirmed:
                self._log(
                    f"捕获强确认（keep.png 连续 {self._capture_frames} 帧，"
                    f"score={self._fmt_score(score)}），停止内核"
                )
                self._stop_quiet("captured")
                return self._result(
                    FightOutcome.CAPTURED, "captured_strong", t0, frames
                )

            state = self._machine.tick(obs)

            # 动态摩擦只在内核明确授权且校准可信时进行；否则冻结并清窗口。
            allowed = self._machine.control_allowed and self._calibrated
            level = obs.bar_level if allowed else None
            try:
                self._cb.friction_step(level)
            except Exception as exc:  # noqa: BLE001 - 摩擦失败不能打断搏鱼
                self._log(f"摩擦控制失败（保持按住）：{type(exc).__name__}: {exc}")

            now = self._now()
            if state is not last_state:
                last_log = now
                self._log_state_change(last_state, state, obs, score, t0)
            if self._machine.net_retract_pending and not self._net_hint_printed:
                self._net_hint_printed = True
                self._log(
                    "legacy fight-runtime 不支持自动收网：继续按住 F6 等待捕获"
                    "（正式 bridge 才接 net_retract_action）"
                )
            if now - last_log >= self._heartbeat:
                last_log = now
                self._log_heartbeat(state, obs, score, t0, frames)

            last_state = state

            if state is afm.FightState.SETTLE:
                self._log("内核确认捕获（SETTLE），停止")
                self._stop_quiet("settle")
                return self._result(FightOutcome.CAPTURED, "settle", t0, frames)
            if state is afm.FightState.LOST:
                self._log("内核判定脱钩（LOST），松开并交回外层")
                self._stop_quiet("lost")
                return self._result(FightOutcome.LOST, "lost", t0, frames)

            time.sleep(self._interval)

    # ---------- 捕获强信号 ----------

    def _strong_score(self, image: Any) -> float | None:
        try:
            score = self._cb.strong_capture(image)
        except Exception as exc:  # noqa: BLE001 - 模板失败 = UNKNOWN，不兜底
            self._log(f"强捕获模板失败，本帧 UNKNOWN：{type(exc).__name__}")
            return None
        if score is None:
            return None
        try:
            score = float(score)
        except (TypeError, ValueError):
            return None
        self._max_capture_score = (
            score if self._max_capture_score is None
            else max(self._max_capture_score, score)
        )
        return score

    def _apply_capture_evidence(
        self,
        obs: aobs.Observation,
        score: float | None,
        foreground: bool,
    ) -> aobs.Observation:
        """用同帧强信号覆盖 captured；强信号缺失时绝不采用弱几何。"""
        if score is None:
            captured = aobs.TriState.UNKNOWN
        elif score >= STRONG_CAPTURE_CONFIDENCE and foreground:
            captured = aobs.TriState.TRUE
        else:
            captured = aobs.TriState.FALSE
        if obs.captured is captured:
            return obs
        return dataclasses.replace(obs, captured=captured)

    def _capture_confirmed(self, obs: aobs.Observation, foreground: bool) -> bool:
        """连续 2 个独立、新鲜、前台的强命中才确认。"""
        strong = obs.captured is aobs.TriState.TRUE
        if not strong or not foreground:
            self._capture_frames = 0
            self._capture_last_ts = None
            return False
        ts = float(obs.monotonic_ts)
        now = self._now()
        if now - ts > self._config.max_observation_age:
            self._capture_frames = 0
            self._capture_last_ts = None
            return False
        if self._capture_last_ts is not None and ts <= self._capture_last_ts:
            return False
        self._capture_last_ts = ts
        self._capture_frames += 1
        return self._capture_frames >= STRONG_CAPTURE_FRAMES

    # ---------- 日志 ----------

    @staticmethod
    def _fmt_score(score: float | None) -> str:
        return "?" if score is None else f"{score:.3f}"

    @staticmethod
    def _fmt(value: Any) -> str:
        return "?" if value is None else str(value)

    def _key_text(self) -> str:
        return (
            f"desired={sorted(self._arbiter.desired)} "
            f"emitted={sorted(self._arbiter.emitted)}"
        )

    def _log_state_change(
        self,
        old: afm.FightState,
        new: afm.FightState,
        obs: aobs.Observation,
        score: float | None,
        t0: float,
    ) -> None:
        reason = self._machine.events[-1].reason if self._machine.events else ""
        self._log(
            f"迁移 {old.value}->{new.value} reason={reason} "
            f"t={self._elapsed(t0):.1f}s cur={self._friction_cur()} "
            f"ring={self._fmt(obs.ring_ratio)} bar={self._fmt(obs.bar_level)} "
            f"cap={self._fmt_score(score)} {self._key_text()}"
        )

    def _log_heartbeat(
        self,
        state: afm.FightState,
        obs: aobs.Observation,
        score: float | None,
        t0: float,
        frames: int,
    ) -> None:
        self._log(
            f"心跳 state={state.value} t={self._elapsed(t0):.1f}s "
            f"frames={frames} cur={self._friction_cur()} "
            f"ring={self._fmt(obs.ring_ratio)} bar={self._fmt(obs.bar_level)} "
            f"cap={self._fmt_score(score)} {self._key_text()}"
        )

    # ---------- 收尾 ----------

    def _now(self) -> float:
        try:
            return float(self._cb.clock())
        except Exception:
            return time.monotonic()

    def _elapsed(self, t0: float) -> float:
        return max(0.0, self._now() - t0)

    def _stop_quiet(self, reason: str) -> None:
        """任何退出路径都尝试 stop；释放失败最多重试 3 次后抛错停止外层。

        `machine.stop()` 失败时 `owned_keys` 仍保留未松开的键。绝不能把它
        完全吞掉然后返回 CAPTURED 让流程继续入护/recast —— 那样下一个 owner
        会在"键还按着"的错误前提下继续。这里只重试有限次数（3 次），仍然
        owned 非空就抛 `FightReleaseError`，由外层中止。
        """
        if self._release_failed:
            raise FightReleaseError(
                "上一轮 stop 已耗尽释放重试，拒绝继续（仍有按键未释放："
                f"{sorted(self._machine.owned_keys)}）"
            )
        last_exc: BaseException | None = None
        for attempt in range(1, STOP_RELEASE_ATTEMPTS + 1):
            try:
                self._machine.stop()
            except Exception as exc:  # noqa: BLE001 - 记录后按 owned 判断
                last_exc = exc
                self._log(
                    f"stop({reason}) 第 {attempt}/{STOP_RELEASE_ATTEMPTS} 次"
                    f"释放失败：{type(exc).__name__}: {exc}"
                )
            if not self._machine.owned_keys:
                self._calibrated = False
                return
            if attempt < STOP_RELEASE_ATTEMPTS:
                self._log(
                    f"stop({reason}) 仍有按键未释放："
                    f"{sorted(self._machine.owned_keys)}，重试 "
                    f"{attempt + 1}/{STOP_RELEASE_ATTEMPTS}"
                )
        self._calibrated = False
        self._release_failed = True
        raise FightReleaseError(
            f"stop({reason}) 重试 {STOP_RELEASE_ATTEMPTS} 次后仍有按键未释放："
            f"{sorted(self._machine.owned_keys)}；中止外层流程，不再入护/recast"
        ) from last_exc

    def _result(
        self, outcome: FightOutcome, reason: str, t0: float, frames: int
    ) -> FightSessionResult:
        return FightSessionResult(
            outcome=outcome,
            reason=reason,
            elapsed=self._elapsed(t0),
            frames=frames,
            max_capture_score=self._max_capture_score,
            calibration=self._calibration,
            events=tuple(self._machine.events),
        )


# ==================== Player 包装（适配器调用，离线可测） ====================


def install_player_patches(
    player_cls: type,
    *,
    enabled: bool,
    runtime_factory: Callable[[Any], FightRuntime],
    captured_error: type[BaseException],
    hooked_error: type[BaseException] | None = None,
    lost_attr: str = "_rf4_fight_lost",
    session_attr: str = "_rf4_fight_session_active",
) -> bool:
    """把新内核装到 `Player` 上。返回是否安装。

    `enabled=False` 时**一个方法都不改**（默认 legacy）。同一个 player 实例
    复用同一个 `FightRuntime`，保证重复进入时不会出现第二个 arbiter/owner。

    `hooked_error` 必须传入上游的 `FishHookedError` 类型：新
    `retrieve_line` 用它判断"收线途中鱼咬钩"，先退出 `hold_keys` context
    再进入新内核，绝不回到原 `error_handler`。
    """
    if not enabled:
        return False
    if hooked_error is None or not (
        isinstance(hooked_error, type) and issubclass(hooked_error, BaseException)
    ):
        raise ValueError(
            "install_player_patches 需要注入 hooked_error（FishHookedError 类型）"
        )
    if getattr(player_cls, "_rf4_fight_fsm_installed", False):
        return False

    orig_lift = player_cls.lift_fish
    runtimes: "weakref.WeakKeyDictionary[Any, FightRuntime]" = (
        weakref.WeakKeyDictionary()
    )

    def _runtime_for(player: Any) -> FightRuntime:
        runtime = runtimes.get(player)
        if runtime is None:
            runtime = runtime_factory(player)
            runtimes[player] = runtime
        return runtime

    def _is_hooked(player: Any) -> bool:
        try:
            return bool(player.detection.is_fish_hooked())
        except Exception:  # noqa: BLE001 - 读不到咬钩按"没有"处理
            return False

    def _unsupported_feature(player: Any) -> str | None:
        """新内核只接管 F5/F6，不支持 ELECTRO / POST_ACCELERATION。"""
        cfg = getattr(player, "cfg", None)
        if cfg is None:
            return None
        if bool(getattr(getattr(cfg, "ARGS", None), "ELECTRO", False)):
            return "ELECTRO"
        profile = getattr(cfg, "PROFILE", None)
        mode = getattr(profile, "MODE", "")
        if mode != "telescopic" and bool(
            getattr(profile, "POST_ACCELERATION", False)
        ):
            return "POST_ACCELERATION"
        return None

    def pull_fish(self, save: bool = True) -> None:
        # 每次进入先清掉上一轮的标记，保证 lift_fish 只认"刚结束的这一场"。
        if getattr(self, lost_attr, False):
            setattr(self, lost_attr, False)
        # 已进入新会话：之后无论结果如何都不允许旧 lift 再接管。
        setattr(self, session_attr, True)

        # 保留原 pull 的两项语义（新版不再走原 pull_fish）。
        try:
            self.cur_coffee = 0
        except Exception:  # noqa: BLE001 - 早期 fake/裁剪对象没有该字段
            pass
        if save:
            self.save_bite_screenshot()

        unsupported = _unsupported_feature(self)
        if unsupported is not None:
            raise FightSessionAbort(
                f"[fight-fsm] 不支持 {unsupported}（BLOCKED）："
                "新内核只接管 F5/F6，拒绝开始持键"
            )

        result = _runtime_for(self).run()
        if result.outcome is FightOutcome.CAPTURED:
            # 不吞：交给现有 loop_restart_handler 入护。
            raise captured_error()
        if result.outcome is FightOutcome.LOST:
            setattr(self, lost_attr, True)
            return None
        if result.outcome is FightOutcome.CANCELLED:
            raise FightSessionAbort(
                f"[fight-fsm] 会话已取消（{result.reason}）；"
                "已进入新会话，禁止回退 legacy lift/recast"
            )
        if result.outcome is FightOutcome.REENTERED:
            raise FightSessionAbort(
                f"[fight-fsm] 会话重入被拒绝（{result.reason}）；"
                "中止外层流程，不触发重抛竿"
            )
        if result.outcome is FightOutcome.BLOCKED:
            raise FightSessionAbort(
                f"[fight-fsm] 新内核拒绝本次会话：{result.reason}"
            )
        return None

    def retrieve_line(self) -> None:
        # 新会话完全重写收线，绝不调用原 retrieve_line（否则原函数会在
        # 自己的 hold_keys 里再调 pull_fish，出现两个输入 owner）。
        setattr(self, session_attr, False)

        if _is_hooked(self):
            self.pull_fish()
            return None

        hooked_during = False
        with self.hold_keys(mouse=True, shift=False):
            try:
                self.tackle.retrieve()
            except BaseException as exc:  # noqa: BLE001 - 只识别咬钩异常
                if hooked_error is not None and isinstance(exc, hooked_error):
                    hooked_during = True
                else:
                    raise

        # 关键：必须先完整退出 hold_keys context，再进入新 pull。
        if hooked_during or _is_hooked(self):
            self.pull_fish()
        return None

    def lift_fish(self) -> None:
        # LOST 后不再跑旧 30/600s；已经进过新会话的 CANCELLED/REENTERED
        # 也绝不能落到 legacy lift（否则会静默接上重抛竿）。
        if getattr(self, lost_attr, False):
            setattr(self, lost_attr, False)
            setattr(self, session_attr, False)
            return None
        if getattr(self, session_attr, False):
            setattr(self, session_attr, False)
            return None
        return orig_lift(self)

    player_cls.pull_fish = pull_fish
    player_cls.retrieve_line = retrieve_line
    player_cls.lift_fish = lift_fish
    player_cls._rf4_fight_fsm_installed = True
    return True
