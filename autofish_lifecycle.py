"""全流程协调器（纯离线）：状态 + 控制模式 + 注入回调。

本模块把"一条鱼的完整生命周期"做成一个显式状态机，自己不抓屏、不按键、
不联网、不启动游戏。全部副作用通过 :class:`RuntimeCallbacks` 注入，适配层
（adapter bridge）只负责把这些回调接到真实的捕获/输入/摩擦实现上。

设计要点
--------
* ``step()`` 是唯一推进入口：主线程每调用一次吃一帧观测并推进状态。
  ``autofish_runtime`` 负责循环、IPC 发布/命令与退出；本模块不 sleep、
  不读真实时间。
* 阻塞动作（校准/抛竿/入护）只通过回调执行，且每个状态**只执行一次**，
  回调拿到 :class:`ActionControl`，用 ``wait()/checkpoint()`` 保证停止请求
  可以打断；执行完统一 ``finally`` 释放脚本自己的按键。
* 唯一的按键 owner 是一个 :class:`autofish_input.InputArbiter`；FIGHT/LAND/NET
  由 :class:`autofish_fight_machine.FightMachine` 在同一 arbiter 上驱动。
* 所有"未知"都不许被当成确定值：黑帧不能推断 READY，失焦不动作，
  强捕获可以覆盖 HUD 未知但必须是新鲜前台连续 2 帧。
* READY 只在持有**单次**许可时才能进入 CALIBRATING。许可来源只有三种：
  人工 ``confirm_ready``、本帧 AUTO/新鲜前台下的 readiness=True、以及
  成功入护后的 ``confirmed_after_keep`` 链（不跨人工接管）。许可在进入
  CALIBRATING 前即被消费；OBSERVE/STOP/FAULT/重新探针未知一律清除。
* 摩擦是否可控制只看 ``friction_snapshot()`` 的外部可信度；内部校准成功
  不得覆盖外部 ``calibrated=False``。鱼中失去可信度只清窗口并 FAULT，
  不在每个 tick 自动重复校准。
* 自动抄网默认关闭（``FightConfig.auto_net_enabled=False``）：LAND 到点后
  保持 F5+F6 与 LAND，原因栏显示「临岸候选，自动抄网待验证；继续收线提竿」。
  关闭必须发生在 LAND -> NET 之前；只置 ``net_action=None`` 仍会松 F5 进 NET。
* 打开后 NET 不是永久人工挂起：NET 中新鲜低环连续帧会自动发收网切换，
  成功后等待 1.2s 动画再回 FIGHT 恢复 F5。掏网/收网都只发一次 F7；
  动作失败保持 F6 + UNKNOWN fault，不恢复 F5、不重复盲 toggle。
* 只有正常新钓鱼会话才向 FightMachine 假定 ``NetState.STOWED``；启动时鱼
  已经在线（中途接管）一律 ``UNKNOWN``，禁止盲 toggle。
* ``fish_elapsed`` 从**确认中鱼**开始算；``cast_elapsed`` 保留从抛竿起算。

状态机
------
BOOT -> PROBING -> READY -> CALIBRATING -> CASTING -> WAIT_BITE
                                   |            |
                                   v            v
                                 FIGHT <-> LAND -> NET
                                   |
                                   v
                                 SETTLE -> PROBING
        任意状态 --captured/modal--> SETTLE / MODAL
        任意动作 --STOP----------> STOPPING -> STOPPED
        任意异常 ----------------> FAULT（保持 IPC，可 stop）
"""

from __future__ import annotations

import enum
import math
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Deque, Iterable, Mapping, Optional

import autofish_fight_machine as afm
import autofish_input as ain
import autofish_observation as aobs
from autofish_control import AutoStop, ControlSession

__all__ = [
    "AutoFishState",
    "ControlMode",
    "LifecycleConfig",
    "RuntimeCallbacks",
    "ActionControl",
    "LifecycleCoordinator",
    "SNAPSHOT_FIELDS",
]

T = aobs.TriState
F5 = ain.F5
F6 = ain.F6


def _wall_now() -> str:
    """本地墙钟时间；只用于日志可读性，不参与任何控制判定。"""
    try:
        return datetime.now().isoformat(timespec="milliseconds")
    except Exception:  # noqa: BLE001 - 日志格式失败不能影响控制
        return "?"

# 快照「原因」栏的中文展示。内部 `_reason` 与日志仍保留原始 code，便于检索：
#   * near_net_disabled  LAND 已到临岸宽限，但自动抄网默认关闭/待验证
#   * net                NET 保持态（已松 F5、仍按 F6）
#   * net_retracted      收网成功/人工兜底后回到 FIGHT
REASON_TEXT_CN = {
    "auto_net_disabled": "临岸候选，自动抄网待验证；继续收线提竿",
    "near_net_disabled": "临岸候选，自动抄网待验证；继续收线提竿",
    "land": "临岸候选，继续收线提竿",
    "net": "抄网（已松收线键、保持提竿）",
    "net_deployed": "已发掏网指令（保持提竿，等待收网）",
    "net_retract_wait": "已发收网指令，等待切换动画",
    "net_fault": "抄网动作失败，保持提竿；请人工处理",
    "net_retracted": "收网完成，恢复搏鱼",
    "net_state_unknown": "网状态未知（中途接管），不自动抄网；继续收线提竿",
    "net_callback_missing": "自动抄网回调缺失，不掏网；继续收线提竿",
    "net_not_stowed": "网可能已掏出，不重复切换；继续收线提竿",
    "line_low_relogin": "线快被拉空，正在小退重登",
    "line_low": "线杯剩线过低，准备小退重登",
    "relogin_done": "小退完成，重新校准后继续",
    "relogin_failed": "小退失败，已停机待人工处理",
}

# UI 直接读取的字段；测试据此防止快照漂移。
SNAPSHOT_FIELDS = (
    "state",
    "control_mode",
    "reason",
    "reason_code",
    "state_elapsed",
    "friction",
    "bar_tier",
    "ring_ratio",
    "desired_keys",
    "emitted_keys",
    "fish_elapsed",
    "cast_elapsed",
    "hooked_elapsed",
    "kept",
    "last_error",
    "net_pending",
    "net_state",
    "net_state_basis",
    "fish_id",
    "state_version",
    "epoch",
)


class AutoFishState(enum.Enum):
    BOOT = "BOOT"
    PROBING = "PROBING"
    READY = "READY"
    CALIBRATING = "CALIBRATING"
    CASTING = "CASTING"
    WAIT_BITE = "WAIT_BITE"
    FIGHT = "FIGHT"
    LAND = "LAND"
    NET = "NET"
    SETTLE = "SETTLE"
    MODAL = "MODAL"
    # 线快被拉空时的小退重登（Esc → 退出 → 登录）。这是一次**阻塞长动作**，
    # 期间不产生任何搏鱼动作；结束后必须重新校准摩擦，再从 CASTING 开始。
    RELOGIN = "RELOGIN"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAULT = "FAULT"


class ControlMode(enum.Enum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"
    BLOCKED = "BLOCKED"
    OBSERVE = "OBSERVE"


# PROBING 阶段判"线是否已经在水里"的圆环阈值。
#
# 圆环可读**不等于**线在水里：入护后线收回杯上，环照样可读（实测 0.975~1.0）。
# 2026-11-09 实机事故：入护后 1 秒，PROBING 因为"环可读"直接判成 WAIT_BITE，
# 状态机以为在等口、实际根本没抛竿；50 秒后超时去 retrieve，按 F5 又变成了
# 抛竿蓄力（线在杯上时 F5 是蓄力键），力度一路攒到 130% 卡住，最后 FAULT。
# 刚抛出去约 0.93，鱼会继续往下拉，所以只有**低于**该阈值才算"线出去了"。
PROBE_LINE_OUT_MAX_RING = 0.97

# 等口时如果圆环一直贴在满环，说明线其实收在杯上（不是"线在水里等鱼"）。
# 短抛是 0.983 这种"出线一点点"的值，和真正的 1.0 能分开，所以阈值取 0.995。
# 没有这条会死循环：短抛 → WAIT_BITE → 超时 → retrieve 收线 → 环回 1.0
# → 仍判"线在水里" → 再 WAIT_BITE……（2026-11-09 实机）。
PROBE_LINE_HOME_RING = 0.995
PROBE_LINE_HOME_SECS = 3.0

FIGHT_STATES = frozenset({AutoFishState.FIGHT, AutoFishState.LAND, AutoFishState.NET})
ACTION_STATES = FIGHT_STATES | frozenset(
    {
        AutoFishState.READY,
        AutoFishState.CALIBRATING,
        AutoFishState.CASTING,
        AutoFishState.WAIT_BITE,
        AutoFishState.SETTLE,
        AutoFishState.MODAL,
        AutoFishState.RELOGIN,
    }
)
ENTRY_STATES = frozenset(
    {
        AutoFishState.CALIBRATING,
        AutoFishState.CASTING,
        AutoFishState.SETTLE,
        AutoFishState.MODAL,
        AutoFishState.RELOGIN,
    }
)


@dataclass(frozen=True)
class LifecycleConfig:
    """时间阈值与帧防抖。所有时间使用注入 clock 的同一时间轴（秒）。"""

    tick_interval: float = 0.2
    # 等口超时。到点必须先 retrieve 收空线再重新探针，不能盲重抛。
    wait_bite_timeout: float = 50.0
    # 离开搏斗（FIGHT/LAND/NET）后的一段静默期：收鱼时 0e05 比 bot 点击晚到，
    # 协议快照会残留 fighting，PROBING 若立刻相信就会重新开战空打
    # （2026-11-09 实机踩过）。这段时间内 protocol_fight 一律按 False 处理。
    protocol_fight_guard: float = 4.0
    # READY 至少停留多久才允许抛竿（给"落点稳定"留一点余量）。
    ready_hold: float = 0.0
    # 抛竿明确失败后的重试间隔，防止每个 tick 都重抛。
    cast_retry_delay: float = 2.0
    max_observation_age: float = 1.5
    # 连续确认帧数（captured / modal / fish）。
    confirm_frames: int = 2
    # 退出时释放按键的最大尝试次数（不无限重试）。
    shutdown_retries: int = 3
    # 实机联调：强制"抛竿前必须人工 confirm_ready"，忽略 readiness 的 True。
    require_confirm: bool = False
    # 线杯剩线低于该比例就小退重登（用户 2026-11-06 指定 15%）。圆环读数
    # 是"线杯上还剩多少线"，刚抛出去约 0.93；大鱼持续出线会一路掉。
    # 读不到（None）或明显不可信时**不触发**，避免把"没测到"当成"没线了"。
    relogin_line_low: float = 0.15
    # 连续多少帧都低于阈值才动作；防止单帧误读直接退游戏。
    relogin_line_frames: int = 2
    # 搏斗僵死看门狗。挂底时协议会一直报 fighting、圆环和出线都不再变化，
    # 状态机会无限拉扯（2026-11-10 实机：连续多条 20 分钟+ 才被人工打断）。
    # 超过这个秒数、圆环又没有实质改善，就当挂底处理：收线、回 PROBING 重抛。
    fight_stall_timeout: float = 240.0
    # 圆环（线杯剩线比例）要改善多少才算"有进展"。0.01 ≈ 1%。
    fight_stall_ring_gain: float = 0.01
    fight: afm.FightConfig = field(default_factory=afm.FightConfig)

    def __post_init__(self) -> None:
        for name in ("wait_bite_timeout", "cast_retry_delay", "ready_hold"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError("%s must be a finite non-negative number" % name)
        if not (0.0 < float(self.relogin_line_low) < 1.0):
            raise ValueError("relogin_line_low must be within (0, 1)")
        if int(self.relogin_line_frames) < 1:
            raise ValueError("relogin_line_frames must be >= 1")
        if self.tick_interval <= 0.0:
            raise ValueError("tick_interval must be > 0")
        if self.max_observation_age <= 0.0:
            raise ValueError("max_observation_age must be > 0")
        if int(self.confirm_frames) < 1:
            raise ValueError("confirm_frames must be >= 1")
        if int(self.shutdown_retries) < 1:
            raise ValueError("shutdown_retries must be >= 1")
        if not isinstance(self.fight, afm.FightConfig):
            raise TypeError("fight must be an autofish_fight_machine.FightConfig")


@dataclass
class RuntimeCallbacks:
    """运行层全部副作用的注入点；本模块只调这些函数。

    ``capture_observation()`` 每次返回**同一帧**的 :class:`Observation`，并且
    已经用强捕获模板替换过 ``captured``（模板不可用时必须是 UNKNOWN，禁止
    几何兜底）。``foreground`` 直接读 ``Observation.foreground``，没有再单独
    的焦点回调。

    阻塞动作（``calibrate`` / ``cast`` / ``keep`` / ``dismiss`` / ``retrieve``）
    收到 :class:`ActionControl`，应周期性调用 ``control.wait(...)`` 或
    ``control.checkpoint()``；收到 :class:`AutoStop` 时直接向上抛，不要
    吞掉。其余回调失败按领域错误处理（记录 / FAULT）。

    ``calibrate(control, fight=...)``：``fight=True`` 表示鱼已经在线，必须
    走"只向安全端饱和"的降档校准；``fight=False`` 用于每竿抛竿前校准。
    返回 ``False`` 表示明确失败，``None`` 表示成功。**校准成功不等于可信**：
    是否允许控制摩擦只由 ``friction_snapshot()['calibrated']`` 决定。

    ``keep(control)`` 只有**确认入护成功**时才返回 ``True``；``False``/``None``
    一律视为失败（FAULT）。

    ``net_action`` / ``net_retract_action``：打开自动抄网后分别掏网、收网一次。
    两者缺任一都不进入 NET。回调抛异常或返回 ``False`` 视为失败，内核保持
    NET + F6 并标记 UNKNOWN fault；``None`` 表示成功。
    """

    capture_observation: Callable[[], aobs.Observation]
    input_sink: Callable[[str, bool, str], Any]
    readiness: Optional[Callable[[aobs.Observation], Optional[bool]]] = None
    calibrate: Optional[Callable[..., Any]] = None
    cast: Optional[Callable[..., Any]] = None
    keep: Optional[Callable[..., Any]] = None
    dismiss: Optional[Callable[..., Any]] = None
    retrieve: Optional[Callable[..., Any]] = None
    wheel: Optional[Callable[[int], Any]] = None
    net_action: Optional[Callable[[], Any]] = None
    net_retract_action: Optional[Callable[[], Any]] = None
    friction_step: Optional[Callable[[Optional[int]], Any]] = None
    friction_snapshot: Optional[Callable[[], Mapping[str, Any]]] = None
    keepnet_full: Optional[Callable[[], bool]] = None
    # 小退重登：阻塞执行，收到 ActionControl 以便响应停止；返回 False/None 视为
    # 失败（FAULT）。缺这个回调时线长过低只会如实报错，不会自己动手退游戏。
    relogin: Optional[Callable[..., Any]] = None
    # 协议侧“现在是否在搏斗”（读抓包器的 state.json）。返回 True/False/None：
    # None = 无协议信息，回退视觉判据。用于补“鱼贴岸边、咬钩图标不亮”的盲区：
    # 0e0b/0e08 还在流时即使 fish_icon 为 false 也必须进 FIGHT。
    protocol_fight: Optional[Callable[[], Optional[bool]]] = None
    # 协议侧“线还在水里” （0e07 在流）。用于 OBSERVE 弹回 PROBING 后恢复：
    # 线已经在水里就不该重抛，直接回 WAIT_BITE 等口。
    protocol_line_in_water: Optional[Callable[[], Optional[bool]]] = None
    log: Optional[Callable[[str], Any]] = None
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        for name in ("capture_observation", "input_sink", "clock"):
            if not callable(getattr(self, name)):
                raise TypeError("RuntimeCallbacks.%s must be callable" % name)
        for name in (
            "readiness",
            "calibrate",
            "cast",
            "keep",
            "dismiss",
            "retrieve",
            "wheel",
            "net_action",
            "net_retract_action",
            "friction_step",
            "friction_snapshot",
            "keepnet_full",
            "relogin",
            "protocol_fight",
            "protocol_line_in_water",
            "log",
        ):
            value = getattr(self, name)
            if value is not None and not callable(value):
                raise TypeError("RuntimeCallbacks.%s must be callable or None" % name)
        if self.log is None:
            self.log = lambda _text: None


class ActionControl:
    """交给阻塞回调的可取消把手。

    ``control.wait(seconds)`` 在停止请求时立即返回 True；``checkpoint()``
    在停止请求时抛 :class:`AutoStop`。回调应把长动作切成小段并在每段前
    ``checkpoint()`` / 用 ``wait()`` 代替 ``time.sleep``。

    ``state`` 是**调用瞬间**协调器状态的只读视图（由 ``state_provider``
    实时读取，不缓存）。它只用于给当前这次动作授权，绝不代表"以后都可以
    动作"：动作返回后再读就可能是新状态，外部不得把它当成长期 token。
    """

    def __init__(
        self,
        control: ControlSession,
        clock: Callable[[], float] = time.monotonic,
        wheel: Optional[Callable[[int], Any]] = None,
        state_provider: Optional[Callable[[], Any]] = None,
    ):
        self._control = control
        self._clock = clock
        self._wheel = wheel
        self._state_provider = state_provider

    @property
    def state(self) -> Any:
        """协调器当前状态的只读快照；未注入时返回 None。"""
        if self._state_provider is None:
            return None
        try:
            return self._state_provider()
        except Exception:  # noqa: BLE001 - 只读授权信息，读不到=未知
            return None

    @property
    def stopped(self) -> bool:
        return bool(self._control.stopped)

    @property
    def session_id(self) -> str:
        return self._control.session_id

    def now(self) -> float:
        return float(self._clock())

    def wheel(self, steps: int) -> Any:
        """可选的滚轮透传（档位校准用）；未注入时返回 None。"""
        if self._wheel is None:
            return None
        self.checkpoint()
        return self._wheel(int(steps))

    def checkpoint(self) -> None:
        if self._control.stopped:
            raise AutoStop(self._control.stop_reason)

    def wait(self, seconds: float, quantum: float = 0.05) -> bool:
        """睡一小段；返回 True 表示应尽快退出（被停止请求打断）。"""
        if self._control.stopped:
            return True
        try:
            interrupted = bool(self._control.wait(seconds, quantum=quantum))
        except TypeError:  # 兼容只接受一个参数的假 control
            interrupted = bool(self._control.wait(seconds))
        return interrupted or bool(self._control.stopped)

    # 常见别名：回调里写 control.sleep(...) 也能用。
    sleep = wait


class _Fatal(RuntimeError):
    pass


class LifecycleCoordinator:
    """一条鱼的完整生命周期协调器（单线程，非线程安全，主线程调用）。"""

    def __init__(
        self,
        callbacks: RuntimeCallbacks,
        control: Optional[ControlSession] = None,
        *,
        config: Optional[LifecycleConfig] = None,
        initial_auto: bool = False,
        epoch: Optional[str] = None,
    ) -> None:
        if not isinstance(callbacks, RuntimeCallbacks):
            raise TypeError("LifecycleCoordinator requires RuntimeCallbacks")
        self._cb = callbacks
        self._control = control if control is not None else ControlSession()
        self._config = config or LifecycleConfig()
        self._epoch = str(epoch) if epoch else secrets.token_hex(8)

        self._arbiter = ain.InputArbiter(callbacks.input_sink, key_order=(F5, F6))
        self._machine = afm.FightMachine(
            self._arbiter,
            clock=callbacks.clock,
            net_action=callbacks.net_action,
            net_retract_action=callbacks.net_retract_action,
            config=self._config.fight,
        )
        self._action = ActionControl(
            self._control,
            callbacks.clock,
            callbacks.wheel,
            state_provider=lambda: self._state,
        )

        self._state = AutoFishState.BOOT
        self._reason = "boot"
        self._state_since = self._now()
        self._state_version = 0
        # 僵死看门狗：以"进入当前状态时的 _state_since"为纪元，
        # 换了状态就自动重置，不需要在每个 _enter 分支里手工清。
        self._stall_state_ts: Optional[float] = None
        self._stall_ring_start: Optional[float] = None
        self._stall_ring_best: Optional[float] = None
        self._last_step_at: Optional[float] = None
        self._heartbeat = 0.0
        # 协议半途接手元数据（apply_protocol_handoff 填写；snapshot 透出）
        self._protocol_handoff: Optional[dict] = None
        # 协议搏斗判据的静默截止时刻（离开搏斗后一段时间内不信 fighting）
        self._protocol_quiet_until = 0.0

        self._requested_mode = ControlMode.AUTO if initial_auto else ControlMode.OBSERVE
        self._blocked_reason: Optional[str] = None
        # 单次 READY 许可：confirmed / readiness / confirmed_after_keep / cast_retry。
        # 只在 PROBING->READY 与 READY->CALIBRATING 使用，进入 CALIBRATING 即消费。
        self._ready_grant: Optional[str] = None
        # 成功入护后等待"干净下一帧"再生成 confirmed_after_keep（不跨人工接管）。
        self._keep_chain_pending = False
        self._pending_resume: Optional[str] = None

        self._obs: Optional[aobs.Observation] = None
        self._frame_ok = False
        self._last_frame_ts: Optional[float] = None
        self._foreground = False

        self._capture_frames = 0
        self._modal_frames = 0
        self._fish_frames = 0
        # 线长过低只作为"连续帧"证据累计，单帧不动作。
        self._line_low_frames = 0
        # 等口时"环贴在满环"的起点时间；用来识别"线其实还在杯上"。
        self._line_home_since: Optional[float] = None

        self._friction_cache: dict[str, Any] = {"cur": None, "calibrated": False}
        self._calib_fight = False

        self._fish_id: Optional[str] = None
        # cast 时间：从抛竿成功起算（兼容旧 cast_elapsed / 收线计时）。
        self._fish_since: Optional[float] = None
        # 搏鱼时间：只在确认中鱼、FightMachine.start() 时起算，不抛竿起算。
        self._hooked_since: Optional[float] = None
        # 本会话可向 FightMachine 假定的抄网初始状态。只有正常新钓鱼会话
        # （本进程成功抛竿、未掏出过网）才允许 STOWED；其余一律 UNKNOWN。
        self._net_assumption = afm.NetState.UNKNOWN
        # 一旦本会话掏过网/动作失败/从 NET 结算，就不再把下一次 cast 当成
        # "网肯定收好"；保持 UNKNOWN 直到人工确认或明确收网成功。
        self._net_assumption_tainted = False
        self._fish_seq = 0
        self._kept_tokens: Deque[str] = deque(maxlen=64)
        self._kept = 0

        self._net_pending = False
        self._entry_pending = False
        self._cast_failed_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._acks: Deque[dict[str, Any]] = deque(maxlen=64)
        self._settle_token_value: Optional[str] = None
        # 一次入护只点击一次：captured 必须至少消失一帧才允许再次 SETTLE。
        self._settle_suppressed = False

        # FAULT 释放失败后的有界、无 key-down 重试状态。
        self._fault_reason: Optional[str] = None
        self._fault_release_pending = False
        self._fault_release_attempts = 0

        self._handlers: dict[
            AutoFishState, Callable[[aobs.Observation, float], None]
        ] = {
            AutoFishState.PROBING: self._tick_probing,
            AutoFishState.READY: self._tick_ready,
            AutoFishState.CALIBRATING: self._tick_idle,
            AutoFishState.CASTING: self._tick_idle,
            AutoFishState.WAIT_BITE: self._tick_wait_bite,
            AutoFishState.FIGHT: self._tick_fight,
            AutoFishState.LAND: self._tick_fight,
            AutoFishState.NET: self._tick_fight,
            AutoFishState.SETTLE: self._tick_idle,
            AutoFishState.MODAL: self._tick_idle,
            AutoFishState.RELOGIN: self._tick_idle,
            AutoFishState.STOPPING: self._tick_idle,
            AutoFishState.STOPPED: self._tick_idle,
            AutoFishState.FAULT: self._tick_fault,
        }

    # ------------------------------------------------------------------
    # 只读视图
    # ------------------------------------------------------------------
    @property
    def state(self) -> AutoFishState:
        return self._state

    @property
    def control(self) -> ControlSession:
        return self._control

    @property
    def machine(self) -> afm.FightMachine:
        return self._machine

    @property
    def arbiter(self) -> ain.InputArbiter:
        return self._arbiter

    @property
    def epoch(self) -> str:
        return self._epoch

    @property
    def state_version(self) -> int:
        return self._state_version

    @property
    def kept(self) -> int:
        return self._kept

    @property
    def fish_id(self) -> Optional[str]:
        return self._fish_id

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def requested_mode(self) -> ControlMode:
        """UI/调用方最后请求的模式（不会因失焦变成 BLOCKED）。"""
        return self._requested_mode

    @property
    def auto_requested(self) -> bool:
        """是否明确请求 AUTO。

        适配层恢复前台时必须看这个属性，而不是 :meth:`mode`：``mode()`` 在
        失焦时会返回 BLOCKED，若用它判断就永远无法触发恢复。
        """
        return self._requested_mode is ControlMode.AUTO

    def mode(self) -> ControlMode:
        """当前生效的控制模式（BLOCKED 表示此刻不允许动作）。"""
        if self._requested_mode in (
            ControlMode.OBSERVE,
            ControlMode.MANUAL,
            ControlMode.BLOCKED,
        ):
            return self._requested_mode
        if not self._foreground:
            return ControlMode.BLOCKED
        if self._blocked_reason:
            return ControlMode.BLOCKED
        return ControlMode.AUTO

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def step(self, commands: Optional[Iterable[Mapping[str, Any]]] = None) -> dict[str, Any]:
        """推进一个 tick；返回本 tick 的快照（IPC 直接 publish 这个 dict）。

        ``commands`` 为 None 时从 ControlSession 队列取；IPC 宿主应传入
        ``RuntimeServer.drain_commands()`` 的结果，避免重复消费。
        """
        now = self._now()
        self._heartbeat = (
            0.0 if self._last_step_at is None else max(0.0, now - self._last_step_at)
        )
        self._last_step_at = now
        self._refresh_friction()

        try:
            if commands is None:
                commands = self._control.consume_commands()
            for command in commands:
                self._handle_one(command)

            if self._state is AutoFishState.STOPPED:
                return self.snapshot(now)

            if self._control.stopped:
                self._stop_now(now, self._control.stop_reason or "stop_requested")
                return self.snapshot(now)

            self._tick(now)
        except AutoStop as exc:
            self._on_abort(str(exc), now)
        except _Fatal as exc:
            self._fault_now(str(exc), now)
        except Exception as exc:  # noqa: BLE001 - 任何回调异常都不能让 run 退出
            self._fault_now("%s: %s" % (type(exc).__name__, exc), now)

        return self.snapshot(now)

    def snapshot(self, now: Optional[float] = None) -> dict[str, Any]:
        """UI/IPC 快照；字段集合见 :data:`SNAPSHOT_FIELDS`。"""
        now = self._now() if now is None else now
        obs = self._obs
        fric = dict(self._friction_cache)
        calibrated = bool(fric.get("calibrated"))
        elapsed = max(0.0, now - self._state_since)
        hooked_elapsed = (
            None
            if self._hooked_since is None
            else max(0.0, now - self._hooked_since)
        )
        cast_elapsed = (
            None if self._fish_since is None else max(0.0, now - self._fish_since)
        )
        desired = sorted(self._arbiter.desired)
        emitted = sorted(self._arbiter.emitted)
        state = self._state
        mode = self.mode()
        # 原因栏给中文；原始 code 单独保留，日志（_log）从不翻译。
        reason_text = REASON_TEXT_CN.get(self._reason, self._reason)
        machine = self._machine

        return {
            # ---- 契约字段 ----
            "state": state.value,
            "control_mode": mode.value,
            "reason": reason_text,
            "reason_code": self._reason,
            "state_elapsed": elapsed,
            "friction": fric,
            "bar_tier": None if obs is None else obs.bar_tier,
            "ring_ratio": None if obs is None else obs.ring_ratio,
            "desired_keys": desired,
            "emitted_keys": emitted,
            "fish_elapsed": hooked_elapsed,
            "cast_elapsed": cast_elapsed,
            "hooked_elapsed": hooked_elapsed,
            "kept": self._kept,
            "last_error": self._last_error,
            "net_pending": self._net_pending,
            "net_state": machine.net_state.value,
            "net_state_basis": machine.net_state_basis,
            "fish_id": self._fish_id,
            "state_version": self._state_version,
            "epoch": self._epoch,
            # 协议半途接手诊断（非契约；无接手时为 None）
            "protocol_handoff": self._protocol_handoff,
            # ---- UI 兼容别名（后续 UI 可删） ----
            "session": self._control.session_id,
            "state_name": state.name,
            "phase": state.value,
            "state_elapsed_s": elapsed,
            "state_reason": self._reason,
            "mode": mode.value,
            "fight_mode": self._machine.state.value,
            "cur": fric.get("cur"),
            # UI 的「可信度」只跟随外部 friction_snapshot，不编造内部成功值。
            "confidence": 1.0 if calibrated else 0.0,
            "bar": None if obs is None else obs.bar_level,
            "bar_level": None if obs is None else obs.bar_level,
            "tier": None if obs is None else obs.bar_tier,
            "ring": None if obs is None else obs.ring_ratio,
            "desired": desired,
            "emitted": emitted,
            "heartbeat": self._heartbeat,
            "stopped": bool(self._control.stopped),
            "stop_after_fish": bool(self._control.stop_after_fish),
            "foreground": self._foreground,
            "fish_icon": None if obs is None else obs.fish_icon.value,
            "captured": None if obs is None else obs.captured.value,
            "ok_modal": None if obs is None else obs.ok_modal.value,
            "hud_visible": None if obs is None else obs.hud_visible.value,
        }

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------
    def handle_command(self, command: Mapping[str, Any]) -> dict[str, Any]:
        """处理一条命令，返回 ``{id,status,message}``（同时并入 take_acks）。"""
        return self._handle_one(command)

    def take_acks(self) -> list[dict[str, Any]]:
        """取走待回执（宿主转给 RuntimeServer.acknowledge）。"""
        items = list(self._acks)
        self._acks.clear()
        return items

    def _handle_one(self, command: Any) -> dict[str, Any]:
        if not isinstance(command, Mapping):
            return self._ack(None, "rejected", "bad command")
        name = str(command.get("name") or "")
        cid = command.get("id")
        if not isinstance(cid, str) or not cid:
            cid = None
        payload = command.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}
        now = self._now()

        if name == "stop":
            reason = payload.get("reason")
            reason = str(reason)[:200] if isinstance(reason, str) and reason else "stop"
            self._control.request_stop(reason)
            return self._ack(cid, "executed", "stopping")

        if name == "stop_after_fish":
            self._control.request_stop_after_fish()
            return self._ack(cid, "executed", "stop_after_fish latched")

        if self._state is AutoFishState.STOPPED:
            return self._ack(cid, "rejected", "session stopped")

        if name == "resume":
            if self._control.stopped:
                return self._ack(cid, "rejected", "session stopped")
            self._control.resume()  # 取消 stop_after_fish 锁存
            self._requested_mode = ControlMode.AUTO
            if self._foreground_fresh(now):
                self._resume_now(now)
                return self._ack(cid, "executed", "auto")
            self._pending_resume = cid
            return self._ack(cid, "accepted", "waiting for foreground")

        if name == "observe":
            self._requested_mode = ControlMode.OBSERVE
            self._pending_resume = None
            self._leave_to_observe(now)
            return self._ack(cid, "executed", "observe")

        if name == "confirm_ready":
            return self._confirm_ready(cid, now)

        if name == "confirm_net_retracted":
            return self._confirm_net(cid, payload, now)

        return self._ack(cid, "rejected", "unknown command")

    def _ack(self, cid: Optional[str], status: str, message: str = "") -> dict[str, Any]:
        ack = {"id": cid, "status": status, "message": str(message or "")}
        if cid:
            self._acks.append(ack)
        return ack

    def _confirm_ready(self, cid: Optional[str], now: float) -> dict[str, Any]:
        if self._state is not AutoFishState.PROBING:
            return self._ack(cid, "rejected", "state_not_probing")
        if not self._foreground_fresh(now) or self._obs is None:
            return self._ack(cid, "rejected", "foreground_required")
        obs = self._obs
        if obs.fish_icon is T.TRUE or self._fish_frames > 0:
            return self._ack(cid, "rejected", "fish_present")
        if obs.captured is T.TRUE or self._capture_frames > 0:
            return self._ack(cid, "rejected", "captured_pending")
        if obs.ok_modal is T.TRUE:
            return self._ack(cid, "rejected", "modal_pending")
        if obs.ring_ratio is not None:
            return self._ack(cid, "rejected", "line_out")
        # 人工确认同样只是**单次**许可，进入 CALIBRATING 前消费。
        self._keep_chain_pending = False
        self._ready_grant = "confirmed"
        self._blocked_reason = None
        self._enter(AutoFishState.READY, "ready_confirmed", now)
        return self._ack(cid, "executed", "ready")

    def _confirm_net(
        self, cid: Optional[str], payload: Mapping[str, Any], now: float
    ) -> dict[str, Any]:
        if self._state is not AutoFishState.NET:
            return self._ack(cid, "rejected", "state_not_net")
        # 必须看真实生效模式：requested=AUTO 但失焦时 mode() 是 BLOCKED。
        if self._requested_mode is not ControlMode.AUTO or self.mode() is not ControlMode.AUTO:
            return self._ack(cid, "rejected", "mode_not_auto")
        if not self._foreground_fresh(now) or not self._frame_ok or self._obs is None:
            return self._ack(cid, "rejected", "foreground_required")
        obs = self._obs
        if obs.ok_modal is T.TRUE or self._modal_frames > 0:
            return self._ack(cid, "rejected", "modal_pending")
        if str(payload.get("epoch")) != self._epoch:
            return self._ack(cid, "rejected", "stale_epoch")
        try:
            version = int(payload.get("state_version"))
        except (TypeError, ValueError):
            return self._ack(cid, "rejected", "bad_state_version")
        if version != self._state_version:
            return self._ack(cid, "rejected", "stale_state_version")
        fish_id = payload.get("fish_id")
        if self._fish_id is None or str(fish_id) != self._fish_id:
            return self._ack(cid, "rejected", "stale_fish_id")
        try:
            confirmed = self._machine.confirm_net_retracted()
        except Exception as exc:  # noqa: BLE001
            return self._ack(cid, "rejected", "confirm_failed:%s" % type(exc).__name__)
        if not confirmed:
            return self._ack(cid, "rejected", "machine_not_net")
        self._net_pending = False
        self._blocked_reason = None
        self._net_assumption_tainted = False
        self._enter(AutoFishState.FIGHT, "net_retracted", now)
        return self._ack(cid, "executed", "net_retracted")

    def _resume_now(self, now: float) -> None:
        self._pending_resume = None
        self._blocked_reason = None
        # resume 只恢复 AUTO，不隐式确认 READY；旧许可/入护链一律作废。
        self._ready_grant = None
        self._keep_chain_pending = False
        if self._state is AutoFishState.FAULT:
            self._enter(AutoFishState.PROBING, "resumed", now)
        else:
            self._set_reason("resumed")

    def _apply_pending_resume(self, now: float) -> None:
        if self._pending_resume is None:
            return
        cid = self._pending_resume
        self._resume_now(now)
        self._ack(cid, "executed", "auto")

    def _leave_to_observe(self, now: float) -> None:
        # 人工接管/观察：任何单次许可都不能跨过去继续用。
        self._ready_grant = None
        self._keep_chain_pending = False
        if self._state in ACTION_STATES:
            self._stop_machine("observe")
            self._release_keys("observe")
            self._clear_fish()
            self._enter(AutoFishState.PROBING, "observe", now)

    # ------------------------------------------------------------------
    # tick
    # ------------------------------------------------------------------
    def apply_protocol_handoff(self, action: dict) -> bool:
        """半途接手：按协议快照的动作表直接从 BOOT 跳到目标状态。

        在 runtime.run() 之前调用（构造后一次）。动作表来自
        ``autofish_handoff.resolve()``：

        * ``enter="FIGHT"``    → 直接进 FIGHT，**不 release**（松键跑鱼）；
          摩擦档位若不可信（无落盘存档），_tick_fight 首帧会以
          ``friction_untrusted`` FAULT——这是既有的正确防线，不在这里绕过。
        * ``enter="WAIT_BITE"``→ 进 WAIT_BITE（松键由 _enter 统一做）；
        * ``enter="PROBING"``  + ``keys="tap_f5"`` → 短按 F5 再松（casting 接手：
          蓄力中追加后释放，确保抛出而非无限蓄力），然后进 PROBING；
        * 其余 → PROBING（BOOT 自己也会到，这里只为把 reason 记清楚）。

        返回是否真的应用了接手（False = 快照无效/已是运行中状态，走默认路径）。
        """
        if self._state is not AutoFishState.BOOT:
            return False
        if not isinstance(action, dict):
            return False
        enter = str(action.get("enter") or "")
        keys = str(action.get("keys") or "")
        reason = str(action.get("reason") or "protocol_handoff")
        phase = str(action.get("phase") or "unknown")
        now = self._now()

        enter_map = {
            "FIGHT": AutoFishState.FIGHT,
            "WAIT_BITE": AutoFishState.WAIT_BITE,
        }
        target = enter_map.get(enter)
        if target is None:
            # PROBING 族：BOOT tick 也会自然到 PROBING；这里仅当需要
            # tap_f5 时才先做动作，否则直接返回让默认路径走（reason 留给诊断）。
            if keys == "tap_f5":
                # casting 接手：短按 F5 再松——若正在蓄力（键被按住），
                # 追加后释放确保抛出；若没在蓄力，轻按形成一次短抛。
                # arbiter 是"期望集合"语义，tap 手动做：按下→等→收回期望。
                try:
                    self._arbiter.set_desired({F5}, "handoff_casting")
                    self._arbiter.reconcile("handoff_casting")
                    if self._control.wait(0.25):
                        # 被停止请求打断：立刻收键，让上层走停止路径
                        self._arbiter.set_desired(set(), "handoff_casting_stop")
                        self._arbiter.reconcile("handoff_casting_stop")
                    else:
                        self._arbiter.set_desired(set(), "handoff_casting")
                        self._arbiter.reconcile("handoff_casting_release")
                except Exception as exc:  # noqa: BLE001
                    self._log("handoff tap_f5 failed: %s" % exc)
                self._enter(AutoFishState.PROBING, reason, now)
                self._protocol_handoff = {"phase": phase, "reason": reason}
                return True
            self._protocol_handoff = {"phase": phase, "reason": reason}
            return False
        if target is AutoFishState.WAIT_BITE:
            # 等口没有战斗要跟踪；给一个会话内鱼号，避免 fish_id=None 拒绝 tick。
            self._ensure_fish(now)
        self._enter(target, reason, now, release=(keys != "hold"))
        self._protocol_handoff = {"phase": phase, "reason": reason}
        return True

    def _tick(self, now: float) -> None:
        start_state = self._state
        if start_state is AutoFishState.BOOT:
            self._enter(AutoFishState.PROBING, "boot", now)
            return

        obs = self._capture()
        self._obs = obs
        self._foreground = bool(obs.foreground)
        self._frame_ok = self._accept_frame(obs, now)
        if self._frame_ok:
            self._last_frame_ts = float(obs.monotonic_ts)

        if not obs.foreground:
            self._on_background(obs)
            return

        if self._pending_resume is not None:
            self._apply_pending_resume(now)

        if self._requested_mode in (
            ControlMode.OBSERVE,
            ControlMode.MANUAL,
            ControlMode.BLOCKED,
        ) and self._state in ACTION_STATES:
            self._leave_to_observe(now)
            return

        self._handlers[self._state](obs, now)

        # 状态进入动作放到"进入后的下一个 tick"，保证 CALIBRATING/CASTING 等
        # 至少被发布一次；同时严格保证每个状态只执行一次。
        if self._state is start_state and self._entry_pending:
            self._run_entry(now)

    def _capture(self) -> aobs.Observation:
        try:
            obs = self._cb.capture_observation()
        except Exception as exc:  # noqa: BLE001
            raise _Fatal("capture_error:%s: %s" % (type(exc).__name__, exc))
        if not isinstance(obs, aobs.Observation):
            raise _Fatal("capture_not_observation")
        return obs

    def _accept_frame(self, obs: aobs.Observation, now: float) -> bool:
        """新鲜、时间戳严格递增才算新证据；重复/过时帧不改连续计数。"""
        try:
            ts = float(obs.monotonic_ts)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(ts):
            return False
        if now - ts > self._config.max_observation_age or ts > now + 1.0:
            return False
        if self._last_frame_ts is not None and ts <= self._last_frame_ts:
            return False
        return True

    def _on_background(self, obs: aobs.Observation) -> None:
        """失焦：不产生新动作。FIGHT 子机按遮挡帧冻结（保留已按下的键）。"""
        self._capture_frames = 0
        self._modal_frames = 0
        self._fish_frames = 0
        # 失焦后旧 READY 许可与入护链都视为过期，回来必须重新取证。
        self._ready_grant = None
        self._keep_chain_pending = False
        if self._state in FIGHT_STATES:
            try:
                self._machine.tick(obs)
            except Exception as exc:  # noqa: BLE001 - 冻结路径不改变状态
                self._last_error = "background_tick:%s" % type(exc).__name__
            self._net_pending = self._state is AutoFishState.NET
        else:
            self._set_reason("foreground_lost")

    # ---------- PROBING ----------
    def _tick_probing(self, obs: aobs.Observation, now: float) -> None:
        self._blocked_reason = None
        self._bump_streaks(obs)
        if not self._auto():
            # OBSERVE/MANUAL 下不评估也不保存 readiness 许可。
            self._ready_grant = None
            self._keep_chain_pending = False

        if (
            self._capture_frames >= self._config.confirm_frames
            and not self._settle_suppressed
        ):
            self._begin_settle("captured_probe", now, obs)
            return
        if self._modal_frames >= self._config.confirm_frames:
            self._begin_modal(now)
            return
        if self._fish_frames >= self._config.confirm_frames:
            self._keep_chain_pending = False
            if self._auto():
                self._begin_fight("fish_probe", now)
            else:
                self._set_reason("fish_observed")
            return
        # 协议兜底：鱼在钩上但咬钩图标不亮（贴岸边 / 被 UI 遮住）。
        # 0e0b+0e08 仍在流 → 必须进 FIGHT，否则空等到超时丢鱼。
        if self._auto() and self._protocol_fight():
            self._keep_chain_pending = False
            self._begin_fight("protocol_fight", now)
            return
        # 协议兜底：线已经在水里（0e07 在流）。典型场景是 OBSERVE->AUTO 恢复、
        # 或 bot 重启时抓包器说还在等口——此时不能重抛，只能等。
        if self._auto() and self._protocol_line_in_water():
            self._keep_chain_pending = False
            self._ensure_fish(now)
            self._enter(AutoFishState.WAIT_BITE,
                        "protocol_line_in_water", now)
            return
        if self._settle_suppressed or obs.captured is T.TRUE or obs.ok_modal is T.TRUE:
            # 结算/弹窗还没确认完（或刚入护还没消失），不做任何新动作。
            self._set_reason("settlement_pending_clear")
            return
        if not self._frame_ok:
            return

        # 入护后的链式许可优先于圆环判据：刚收完线，环仍然可读（≈1.0），
        # 不能因为"环可读"就当成线还在水里。这条必须在 ring 判断之前。
        if self._keep_chain_pending and self._auto():
            if (
                obs.fish_icon is not T.TRUE
                and obs.captured is not T.TRUE
                and obs.ok_modal is not T.TRUE
            ):
                self._keep_chain_pending = False
                self._ready_grant = "confirmed_after_keep"
                self._enter(AutoFishState.READY, "confirmed_after_keep", now)
                return
            self._keep_chain_pending = False

        if obs.ring_ratio is not None and obs.ring_ratio < PROBE_LINE_OUT_MAX_RING:
            # 环可读且明显低于满环 = 线确实被拉出去了，只能等口，不能猜 READY。
            self._keep_chain_pending = False
            if not self._auto():
                self._set_reason("line_out_observe")
                return
            self._ensure_fish(now)
            self._enter(AutoFishState.WAIT_BITE, "ring_readable_probe", now)
            return

        if not self._auto():
            self._keep_chain_pending = False
            self._set_reason(
                "line_out_observe" if obs.ring_ratio is not None else "observe_waiting"
            )
            return

        if self._ready_grant is not None:
            self._enter(AutoFishState.READY, "ready_grant:%s" % self._ready_grant, now)
            return

        # readiness=True 只在本帧 AUTO、新鲜前台、未强制人工确认时有效；
        # 不作为跨帧/跨模式状态保存。
        verdict = None if self._config.require_confirm else self._readiness(obs)

        if (obs.ring_ratio is not None
                and obs.ring_ratio < PROBE_LINE_HOME_RING
                and verdict is False):
            # **短抛**：出线一点点（0.97~0.995），"钓具准备好"提示又明确不在了，
            # 说明已经不在待抛状态——只能是在等口。只按环判会把这种情况当成
            # "还没抛"，然后空转到超时去 retrieve（2026-11-09 实机卡死）。
            # 上限卡在 HOME_RING：贴满环时更可能是"线在杯上但提示没画出来"，
            # 那种情况宁可停在 PROBING 报 blocked，也不要假进 WAIT_BITE 空转。
            self._keep_chain_pending = False
            self._ensure_fish(now)
            self._enter(AutoFishState.WAIT_BITE, "short_throw_probe", now)
            return

        if verdict is True:
            self._ready_grant = "readiness"
            self._enter(AutoFishState.READY, "readiness_true", now)
            return
        if self._config.require_confirm:
            self._blocked_reason = "confirm_required"
        else:
            self._blocked_reason = "readiness_unknown" if verdict is None else "not_ready"
        self._set_reason(self._blocked_reason)

    # ---------- READY ----------
    def _tick_ready(self, obs: aobs.Observation, now: float) -> None:
        if not self._auto() or not self._frame_ok:
            return
        if self._control.stop_after_fish:
            self._control.request_stop("stop_after_fish")
            return
        if self._ready_grant is None:
            # 许可已被消费/清除：不能凭 READY 状态本身继续抛竿。
            self._set_reason("ready_unconfirmed")
            self._enter(AutoFishState.PROBING, "ready_unconfirmed", now)
            return
        if now - self._state_since < self._config.ready_hold:
            return
        failed = self._cast_failed_at
        if failed is not None and now - failed < self._config.cast_retry_delay:
            self._set_reason("cast_retry_wait")
            return
        # 单次许可在这里消费；之后无论校准/抛竿结果如何都不能复用。
        self._ready_grant = None
        self._calib_fight = False
        self._enter(AutoFishState.CALIBRATING, "cast_ready", now)

    # ---------- WAIT_BITE ----------
    def _tick_wait_bite(self, obs: aobs.Observation, now: float) -> None:
        self._bump_streaks(obs)
        if (
            self._capture_frames >= self._config.confirm_frames
            and not self._settle_suppressed
        ):
            self._begin_settle("captured_wait", now, obs)
            return
        if self._modal_frames >= self._config.confirm_frames:
            self._begin_modal(now)
            return
        if self._fish_frames >= self._config.confirm_frames:
            if self._auto():
                self._blocked_reason = None
                self._begin_fight("fish_confirmed", now)
            else:
                self._set_reason("fish_observed")
            return
        # 协议兜底（同 PROBING）：等口期间鱼已中钩，但图标没亮。
        if self._auto() and self._protocol_fight():
            self._blocked_reason = None
            self._begin_fight("protocol_fight", now)
            return
        if obs.captured is T.TRUE or obs.ok_modal is T.TRUE:
            return

        if not self._frame_ok or not self._auto():
            return

        # 环贴在满环且持续几秒 = 线其实在杯上，不是在等鱼咬钩。
        # 回 PROBING 重新判定，避免"WAIT_BITE → 超时 → retrieve → 还是 WAIT_BITE"死循环。
        #
        # 2026-11-09 协议否决：短抛时环也在 0.98（满环）但线确实在水里
        # （0e07 每秒一条）。此时回 PROBING 会立刻又被 protocol_line_in_water
        # 拉回 WAIT_BITE，来回抖动还会重置 50s 超时 → 永不收线。
        ring = obs.ring_ratio
        if ring is not None and ring >= PROBE_LINE_HOME_RING:
            if self._protocol_line_in_water():
                self._line_home_since = None
            elif self._line_home_since is None:
                self._line_home_since = now
            elif now - self._line_home_since >= PROBE_LINE_HOME_SECS:
                self._line_home_since = None
                self._enter(AutoFishState.PROBING, "line_home_in_wait", now)
                return
        else:
            self._line_home_since = None

        if now - self._state_since < self._config.wait_bite_timeout:
            return

        # 超时不盲抛：先收空线，再让 PROBING 用正证据重新判定。
        if self._cb.retrieve is None:
            self._blocked_reason = "retrieve_unavailable"
            self._set_reason("retrieve_unavailable")
            return

        try:
            result = self._cb.retrieve(self._action)
        except AutoStop:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fault_now("retrieve_failed:%s: %s" % (type(exc).__name__, exc), now)
            return
        finally:
            self._release_keys("after_retrieve")
        if result is False:
            self._fault_now("retrieve_failed", now)
            return
        self._clear_fish()
        self._enter(AutoFishState.PROBING, "retrieved_timeout", now)

    # ---------- 僵死看门狗 ----------
    def _fight_stalled(self, obs: aobs.Observation, now: float) -> bool:
        """搏斗僵死（挂底/缠草）检测；命中时收线并回 PROBING。

        判据刻意保守：**只有圆环能读到、且超时时间内没有实质改善**才动作。
        圆环读不到时一律不动——"没测到"不等于"没进展"，宁可多拉一会儿，
        也不要把正在上岸的大鱼误判成挂底。
        """
        cfg = self._config
        limit = float(getattr(cfg, "fight_stall_timeout", 0.0) or 0.0)
        if limit <= 0:
            return False

        ring = getattr(obs, "ring_ratio", None)
        ring_ok = isinstance(ring, (int, float)) and math.isfinite(float(ring))
        ring = float(ring) if ring_ok else None

        # 状态纪元：一旦换了状态（_state_since 变），基线自动重置。
        if self._stall_state_ts != self._state_since:
            # 新纪元的第一帧只建立基线，不参与判定（否则一进 FIGHT 就到点）
            self._stall_state_ts = self._state_since
            self._stall_ring_start = ring
            self._stall_ring_best = ring
            return False
        if ring is not None:
            if self._stall_ring_best is None or ring > self._stall_ring_best:
                self._stall_ring_best = ring

        if now - self._state_since < limit:
            return False

        start, best = self._stall_ring_start, self._stall_ring_best
        if start is None or best is None:
            return False
        gain_needed = float(getattr(cfg, "fight_stall_ring_gain", 0.01))
        if best - start >= gain_needed:
            return False

        if self._cb.retrieve is None:
            self._blocked_reason = "retrieve_unavailable"
            self._set_reason("retrieve_unavailable")
            return False
        try:
            result = self._cb.retrieve(self._action)
        except AutoStop:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fault_now(
                "retrieve_failed:%s: %s" % (type(exc).__name__, exc), now
            )
            return True
        finally:
            self._release_keys("fight_stalled")
        if result is False:
            self._fault_now("retrieve_failed", now)
            return True
        self._clear_fish()
        self._enter(AutoFishState.PROBING, "fight_stalled", now)
        return True

    # ---------- FIGHT / LAND / NET ----------
    def _tick_fight(self, obs: aobs.Observation, now: float) -> None:
        # 线杯快被拉空：先停手小退，不再继续拉。放在最前面——比"强捕获"
        # 更优先，因为线断了这条鱼和这套线组都会一起没了。
        if self._line_low_hit(obs):
            self._begin_relogin(now, obs)
            return

        # 僵死看门狗：挂底/缠草时协议会一直报 fighting，但圆环和出线都不再
        # 变化，状态机会无限拉扯下去（实测每条 20 分钟以上）。超时且圆环
        # 没有实质改善 → 当挂底处理：收线、回 PROBING 重新抛。
        if self._fight_stalled(obs, now):
            return

        # 强捕获优先且不看 HUD：新鲜前台连续 2 帧即可停子机转 SETTLE。
        self._bump_streaks(obs)
        if (
            self._capture_frames >= self._config.confirm_frames
            and not self._settle_suppressed
        ):
            self._begin_settle("captured_strong", now, obs)
            return

        try:
            mstate = self._machine.tick(obs)
        except ain.InputDispatchError as exc:
            self._fault_now("input_error: %s" % exc, now)
            return
        except Exception as exc:  # noqa: BLE001
            self._fault_now("fight_error:%s: %s" % (type(exc).__name__, exc), now)
            return

        if self._machine.fault:
            # 抄网动作失败后位置未知；即使之后 resume/重新抛竿，也不再把
            # 下一次 cast 当成"网肯定收好"，避免盲 toggle。
            self._net_assumption_tainted = True

        calibrated = self._friction_calibrated()
        if mstate is afm.FightState.FIGHT and not calibrated:
            # 外部可信度失效：清掉控制窗口，不发新档位，也不自动循环校准。
            self._apply_friction(None)
            self._blocked_reason = "friction_untrusted"
            self._set_reason("friction_untrusted")
            self._fault_now("friction_untrusted", now)
            return

        allowed = self._machine.control_allowed and calibrated
        self._apply_friction(obs.bar_level if allowed else None)

        if mstate is afm.FightState.SETTLE:
            self._begin_settle("machine_settle", now, obs)
        elif mstate is afm.FightState.LOST:
            self._stop_machine("lost")
            self._clear_fish()
            self._enter(AutoFishState.PROBING, "lost", now)
        elif mstate is afm.FightState.NET:
            self._net_pending = True
            if self._machine.fault:
                reason = "net_fault"
            elif self._machine.net_retract_pending:
                reason = "net_retract_wait"
            elif self._machine.net_state is afm.NetState.DEPLOYED:
                reason = "net_deployed"
            else:
                reason = "net"
            self._enter(AutoFishState.NET, reason, now)
            self._set_reason(reason)
        elif mstate is afm.FightState.LAND:
            self._enter(AutoFishState.LAND, "land", now)
            if self._machine.auto_net_blocked:
                # 临岸宽限已到，但自动抄网关闭/回调缺失/网状态未知：保持
                # LAND 与 F5+F6。_set_reason 只在变化时写入且不写日志。
                blocked = self._machine.auto_net_blocked_reason or "near_net_disabled"
                if blocked == "auto_net_disabled":
                    # 兼容旧快照/UI 原因码；语义相同。
                    blocked = "near_net_disabled"
                self._set_reason(blocked)
        else:
            # 从 NET 自动收网回来时，状态机事件是 net_retracted；
            # 从 LAND 圆环丢失回来则是 ring_lost/fight。
            reason = "fight"
            if self._state is AutoFishState.NET:
                reason = "net_retracted"
            elif self._machine.events:
                last = self._machine.events[-1]
                if last.to_state is afm.FightState.FIGHT and last.reason in (
                    "ring_lost",
                    "net_retracted",
                ):
                    reason = last.reason
            self._enter(AutoFishState.FIGHT, reason, now)

    def _line_low_hit(self, obs: aobs.Observation) -> bool:
        """线杯剩线是否已连续低到需要小退。

        只认**可读且成立**的读数：``None``（图标没画/被挡）与负值都不累计，
        也不清零已有计数以外的任何东西——"没测到"不等于"没线了"。
        """
        ratio = obs.ring_ratio
        if ratio is None:
            self._line_low_frames = 0
            return False
        try:
            value = float(ratio)
        except (TypeError, ValueError):
            self._line_low_frames = 0
            return False
        if not math.isfinite(value) or value < 0.0:
            self._line_low_frames = 0
            return False
        if value >= float(self._config.relogin_line_low):
            self._line_low_frames = 0
            return False
        self._line_low_frames += 1
        return self._line_low_frames >= int(self._config.relogin_line_frames)

    def _begin_relogin(self, now: float, obs: aobs.Observation) -> None:
        ratio = obs.ring_ratio
        self._set_reason(
            "line_low%s" % ("" if ratio is None else ":%.0f%%" % (float(ratio) * 100))
        )
        self._log("线杯剩线 %s 连续 %d 帧低于 %.0f%%，停止拉鱼并小退重登"
                       % ("?" if ratio is None else "%.0f%%" % (float(ratio) * 100),
                          self._line_low_frames,
                          float(self._config.relogin_line_low) * 100))
        # 这条鱼作废：清掉 fish_id 与阶段计时，重登后按新会话重新开始。
        self._clear_fish()
        self._line_low_frames = 0
        self._enter(AutoFishState.RELOGIN, "line_low", now)

    def _tick_fault(self, obs: aobs.Observation, now: float) -> None:
        # 故障只展示；动作/状态恢复必须由 resume 显式触发。
        # 释放失败时每 tick 有限重试，只做 key-up/空集合，绝不再发 key-down。
        if not self._fault_release_pending:
            return
        if self._fault_release_attempts >= self._config.shutdown_retries:
            return
        self._fault_release_attempts += 1
        if self._release_keys("fault_retry"):
            self._fault_release_pending = False
            self._last_error = self._fault_reason
        else:
            self._last_error = "%s|release_failed" % (self._fault_reason or "fault")

    def _tick_idle(self, obs: aobs.Observation, now: float) -> None:
        return

    # ---------- 进入动作（每个状态一次） ----------
    def _run_entry(self, now: float) -> None:
        self._entry_pending = False
        state = self._state
        if state is AutoFishState.CALIBRATING:
            self._do_calibrate(now)
        elif state is AutoFishState.CASTING:
            self._do_cast(now)
        elif state is AutoFishState.SETTLE:
            self._do_keep(now)
        elif state is AutoFishState.MODAL:
            self._do_dismiss(now)
        elif state is AutoFishState.RELOGIN:
            self._do_relogin(now)

    def _do_relogin(self, now: float) -> None:
        """小退重登。成功后必须回到 CALIBRATING 重新校准 17 再抛竿。

        重登会丢掉本竿的一切状态：鱼、线杯、圆环基线、档位账本都不可信，
        所以**不允许**直接回 CASTING 或 WAIT_BITE。
        """
        callback = self._cb.relogin
        if callback is None:
            self._fault_now("relogin_missing", now)
            return
        try:
            result = callback(self._action)
        except AutoStop:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fault_now("relogin_error:%s: %s" % (type(exc).__name__, exc), now)
            return
        finally:
            self._release_keys("after_relogin")
        if result is False:
            self._fault_now("relogin_failed", now)
            return
        # 重登后线杯、圆环基线、档位全部重新取证；摩擦必须重新校准。
        self._friction_cache = {"cur": None, "calibrated": False}
        # 重登后手上没有鱼：走"抛竿前绝对校准"，绝不再走 fight=True 的安全降档。
        self._calib_fight = False
        self._net_assumption = afm.NetState.UNKNOWN
        self._settle_suppressed = False
        self._last_error = None
        self._set_reason("relogin_done")
        self._enter(AutoFishState.CALIBRATING, "relogin_done", now)

    def _do_calibrate(self, now: float) -> None:
        callback = self._cb.calibrate
        if callback is None:
            self._fault_now("calibrate_missing", now)
            return
        fight = self._calib_fight
        target = AutoFishState.FIGHT if fight else AutoFishState.CASTING
        try:
            result = callback(self._action, fight=fight)
        except AutoStop:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fault_now("calibration_error:%s: %s" % (type(exc).__name__, exc), now)
            return
        finally:
            self._release_keys("after_calibrate")
        if result is False:
            self._fault_now("calibration_failed", now)
            return
        # 校准动作成功只说明"做完了"；摩擦是否可信必须等外部快照说话。
        if fight:
            if not self._start_machine("fight_calibrated", now):
                return
        self._enter(target, "calibrated" if not fight else "fight_calibrated", now)

    def _do_cast(self, now: float) -> None:
        callback = self._cb.cast
        if callback is None:
            self._fault_now("cast_missing", now)
            return
        try:
            result = callback(self._action)
        except AutoStop:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fault_now("cast_error:%s: %s" % (type(exc).__name__, exc), now)
            return
        finally:
            self._release_keys("after_cast")
        if result is False:
            # 明确失败绝不能自动回 READY 再校准：那会把"没抛出去"变成
            # READY->CALIBRATING->CASTING 的无限循环。清除单次许可并停在
            # FAULT，必须由人工 resume 重新取证。停止请求优先于 FAULT。
            self._ready_grant = None
            self._cast_failed_at = now
            if self._control.stopped:
                self._stop_now(now, self._control.stop_reason or "stop_requested")
                return
            self._fault_now("cast_failed", now)
            return
        self._cast_failed_at = None
        # 抛竿成功 = 正常新钓鱼会话；若此前没有"网可能还在外面"的污染，
        # 才允许对本条鱼假定 STOWED。
        self._net_assumption = (
            afm.NetState.UNKNOWN
            if self._net_assumption_tainted
            else afm.NetState.STOWED
        )
        self._ensure_fish(now)
        self._enter(AutoFishState.WAIT_BITE, "cast", now)

    def _do_keep(self, now: float) -> None:
        if not self._auto():
            self._enter(AutoFishState.PROBING, "observe_settle", now)
            return
        callback = self._cb.keep
        if callback is None:
            self._fault_now("keep_missing", now)
            return
        token = self._settle_token()
        try:
            result = callback(self._action)
        except AutoStop:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fault_now("keep_error:%s: %s" % (type(exc).__name__, exc), now)
            return
        finally:
            self._release_keys("after_keep")
        if result is not True:
            self._fault_now("keep_failed", now)
            return

        self._settle_suppressed = True
        if token not in self._kept_tokens:
            self._kept_tokens.append(token)
            self._kept += 1
        self._last_error = None

        if self._keepnet_full():
            self._control.request_stop("keepnet_full")
            return
        if self._control.stop_after_fish:
            self._control.request_stop("stop_after_fish")
            return

        self._clear_fish()
        self._enter(AutoFishState.PROBING, "kept", now)
        # 成功入护后才允许自动进入下一竿；等"干净下一帧"再生成单次许可。
        self._keep_chain_pending = True

    def _do_dismiss(self, now: float) -> None:
        if not self._auto():
            self._enter(AutoFishState.PROBING, "observe_modal", now)
            return
        callback = self._cb.dismiss
        if callback is None:
            self._fault_now("dismiss_missing", now)
            return
        try:
            result = callback(self._action)
        except AutoStop:
            raise
        except Exception as exc:  # noqa: BLE001
            self._fault_now("dismiss_error:%s: %s" % (type(exc).__name__, exc), now)
            return
        finally:
            self._release_keys("after_dismiss")
        if result is not True:
            self._fault_now("dismiss_failed", now)
            return
        # MODAL 之后重新探针，不重放旧场景的动作。
        self._clear_fish()
        self._enter(AutoFishState.PROBING, "modal_dismissed", now)

    # ------------------------------------------------------------------
    # 迁移辅助
    # ------------------------------------------------------------------
    def _enter(
        self, new: AutoFishState, reason: str, now: float, *, release: bool = True
    ) -> bool:
        if self._state is new:
            return False
        old = self._state
        if release and old in FIGHT_STATES and new not in FIGHT_STATES:
            self._stop_machine("leave_%s" % new.value.lower())
        if (old in FIGHT_STATES or old is AutoFishState.WAIT_BITE) and (
            new not in FIGHT_STATES and new is not AutoFishState.WAIT_BITE
        ):
            # 刚离开搏斗 / 刚收完线：协议可能还残留 fighting 或"线在水里"
            # （0e07 最后一条 2 秒内仍算新鲜），先静默一会儿再做协议判定。
            #
            # 用实时时钟而不是 tick 传入的 now：retrieve 是阻塞几十秒的回调，
            # 拿 tick 开始时的 now 算截止时间会立刻过期（2026-11-09 实机踩过）。
            self._protocol_quiet_until = (
                self._now() + float(self._config.protocol_fight_guard))
        self._state = new
        self._state_since = now
        self._state_version += 1
        self._reason = reason
        self._entry_pending = new in ENTRY_STATES
        self._capture_frames = 0
        self._modal_frames = 0
        self._fish_frames = 0
        if new is AutoFishState.PROBING:
            # 重新探针视为新取证周期：旧 READY 许可一律作废。
            self._ready_grant = None
        if new in (AutoFishState.STOPPED, AutoFishState.FAULT):
            self._ready_grant = None
            self._keep_chain_pending = False
        if new in (AutoFishState.PROBING, AutoFishState.READY, AutoFishState.WAIT_BITE):
            self._release_keys("enter_%s" % new.value.lower())
        if new is not AutoFishState.NET:
            self._net_pending = False
        if new in (
            AutoFishState.READY,
            AutoFishState.FIGHT,
            AutoFishState.SETTLE,
            AutoFishState.MODAL,
        ):
            self._blocked_reason = None
        obs = self._obs
        self._log(
            "%s -> %s (%s) mono=%.3f wall=%s ring=%s fish=%s tier=%s"
            % (
                old.value,
                new.value,
                reason,
                now,
                _wall_now(),
                None if obs is None else obs.ring_ratio,
                None if obs is None else obs.fish_icon.value,
                None if obs is None else obs.bar_tier,
            )
        )
        return True

    def _begin_fight(self, reason: str, now: float) -> None:
        self._blocked_reason = None
        self._keep_chain_pending = False
        self._ensure_fish(now)
        self._ensure_hooked(now)
        if self._friction_calibrated():
            if self._start_machine(reason, now):
                self._enter(AutoFishState.FIGHT, reason, now)
            return
        # 未知基线首次接管：按既定安全协议 calibrate(fight=True)，只做一次，
        # 不在鱼中每个 tick 循环校准。reason 带上触发来源（fish_probe /
        # fish_confirmed / protocol_fight），排查时能看出是视觉还是协议发现的。
        self._calib_fight = True
        self._enter(AutoFishState.CALIBRATING,
                    "fight_needs_calibration:%s" % reason, now)

    def _begin_settle(self, reason: str, now: float, obs: aobs.Observation) -> None:
        self._settle_seen(obs)
        # 在 NET 里被强捕获：我们无法证明网已收回（可能还在手上）。下次
        # 会话不得再假定 STOWED，避免盲 toggle 把网收回。
        if self._state is AutoFishState.NET and self._machine.net_state in (
            afm.NetState.DEPLOYED,
            afm.NetState.UNKNOWN,
        ):
            self._net_assumption_tainted = True
        self._stop_machine("settle")
        self._net_pending = False
        self._enter(AutoFishState.SETTLE, reason, now)

    def _begin_modal(self, now: float) -> None:
        self._keep_chain_pending = False
        self._stop_machine("modal")
        self._clear_fish()
        self._enter(AutoFishState.MODAL, "modal_confirmed", now)

    def _start_machine(self, reason: str, now: float) -> bool:
        # 只有正常新钓鱼会话（本进程成功抛竿且本会话未掏过网）才假定 STOWED；
        # 中途接管默认 UNKNOWN，绝不能盲 toggle。
        initial = (
            afm.NetState.UNKNOWN
            if self._net_assumption_tainted
            else self._net_assumption
        )
        try:
            self._machine.start(net_state=initial)
        except Exception as exc:  # noqa: BLE001
            self._fault_now("fight_start_failed:%s: %s" % (type(exc).__name__, exc), now)
            return False
        self._set_reason(reason)
        return True

    def _stop_machine(self, reason: str) -> bool:
        try:
            self._machine.stop()
            return True
        except Exception as exc:  # noqa: BLE001 - 释放失败只记录，仍尽力松键
            self._last_error = "stop_failed:%s:%s" % (type(exc).__name__, exc)
            self._log("stop failed (%s): %s" % (reason, exc))
            self._release_keys(reason)
            return False

    def _release_keys(self, reason: str) -> bool:
        try:
            self._arbiter.set_desired(set(), reason)
            self._arbiter.reconcile(reason)
        except ain.InputDispatchError as exc:
            self._last_error = "release_failed:%s" % exc
            return False
        return True

    def _fault_now(self, message: str, now: float) -> None:
        self._fault_reason = str(message)
        self._last_error = self._fault_reason
        self._log("fault: %s" % message)
        stopped_ok = self._stop_machine("fault")
        released_ok = self._release_keys("fault")
        self._fault_release_pending = not released_ok
        self._fault_release_attempts = 0
        self._ready_grant = None
        self._keep_chain_pending = False
        self._net_pending = False
        if not released_ok:
            self._last_error = "%s|release_failed" % self._fault_reason
        elif not stopped_ok:
            self._last_error = "%s|stop_failed" % self._fault_reason
        else:
            self._last_error = self._fault_reason
        # 本函数已经 stop/release 过；不要再让 _enter 以 leave_fault 再释放一次，
        # 否则会把刚记录的准确错误覆盖成二次释放错误。
        self._enter(
            AutoFishState.FAULT, self._fault_reason[:200], now, release=False
        )

    def _on_abort(self, message: str, now: float) -> None:
        if self._control.stopped:
            self._stop_now(now, self._control.stop_reason or message)
            return
        self._stop_machine("abort")
        self._release_keys("abort")
        self._enter(AutoFishState.PROBING, message[:200] or "aborted", now)

    def _stop_now(self, now: float, reason: str) -> None:
        if self._state is AutoFishState.STOPPED:
            return
        self._ready_grant = None
        self._keep_chain_pending = False
        if self._state is not AutoFishState.STOPPING:
            # 释放统一交给 _shutdown_actions，避免这里多算一次重试。
            self._enter(AutoFishState.STOPPING, reason, now, release=False)
        ok = self._shutdown_actions()
        self._enter(
            AutoFishState.STOPPED,
            "stopped" if ok else "stopped_release_failed",
            now,
        )

    def _shutdown_actions(self, attempts: Optional[int] = None) -> bool:
        total = max(1, int(attempts if attempts is not None else self._config.shutdown_retries))
        for _ in range(total):
            try:
                self._machine.stop()
            except Exception as exc:  # noqa: BLE001
                self._last_error = "release_failed:%s:%s" % (type(exc).__name__, exc)
                continue
            if self._release_keys("shutdown"):
                return True
        return False

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    def _now(self) -> float:
        try:
            return float(self._cb.clock())
        except Exception:  # noqa: BLE001 - 注入时钟坏了也不能崩
            return time.monotonic()

    def _log(self, text: str) -> None:
        try:
            self._cb.log("[LIFE] %s" % text)
        except Exception:  # noqa: BLE001
            pass

    def _set_reason(self, text: str) -> None:
        if text and text != self._reason:
            self._reason = text

    def _auto(self) -> bool:
        return self.mode() is ControlMode.AUTO

    def _bump_streaks(self, obs: aobs.Observation) -> None:
        if not self._frame_ok:
            self._capture_frames = 0
            self._modal_frames = 0
            self._fish_frames = 0
            return
        self._capture_frames = self._capture_frames + 1 if obs.captured is T.TRUE else 0
        self._modal_frames = self._modal_frames + 1 if obs.ok_modal is T.TRUE else 0
        self._fish_frames = self._fish_frames + 1 if obs.fish_icon is T.TRUE else 0
        if obs.captured is not T.TRUE:
            # 结算画面消失后才允许下一次 keep（防同一帧重复点击/重复计数）。
            self._settle_suppressed = False

    def _readiness(self, obs: aobs.Observation) -> Optional[bool]:
        callback = self._cb.readiness
        if callback is None:
            return None
        try:
            value = callback(obs)
        except Exception as exc:  # noqa: BLE001 - 黑帧/模板缺失 = 不可判定
            self._log("readiness error: %s" % type(exc).__name__)
            return None
        if value is True:
            return True
        if value is False:
            return False
        return None

    def _foreground_fresh(self, now: float) -> bool:
        obs = self._obs
        if obs is None or not obs.foreground:
            return False
        try:
            ts = float(obs.monotonic_ts)
        except (TypeError, ValueError):
            return False
        return math.isfinite(ts) and -1.0 <= now - ts <= self._config.max_observation_age

    def _refresh_friction(self) -> None:
        data: dict[str, Any] = {}
        callback = self._cb.friction_snapshot
        if callback is not None:
            try:
                raw = callback()
            except Exception as exc:  # noqa: BLE001 - 诊断信息，失败不算故障
                self._log("friction_snapshot error: %s" % type(exc).__name__)
                raw = None
            if isinstance(raw, Mapping):
                data.update(raw)
        # 只认外部可信度；内部 calibration 回调成功不能把它覆盖成 True。
        data["calibrated"] = bool(data.get("calibrated"))
        cur = data.get("cur")
        if cur is not None:
            try:
                data["cur"] = int(cur)
            except (TypeError, ValueError):
                data["cur"] = None
        data.setdefault("cur", None)
        self._friction_cache = data

    def _friction_calibrated(self) -> bool:
        return bool(self._friction_cache.get("calibrated"))

    def _protocol_line_in_water(self) -> bool:
        """协议侧是否"线还在水里"。失败/缺失一律 False（不影响视觉主链路）。"""
        callback = self._cb.protocol_line_in_water
        if callback is None:
            return False
        if self._now() < self._protocol_quiet_until:
            return False
        try:
            return bool(callback() is True)
        except Exception as exc:  # noqa: BLE001
            self._log("protocol_line_in_water failed: %s"
                      % type(exc).__name__)
            return False

    def _protocol_fight(self) -> bool:
        """协议侧是否确认"正在搏斗"。None（无抓包器）一律当 False。

        回调失败也当 False——协议只是**补充**判据，绝不能因为它报错
        就把钓鱼流程带崩。
        """
        callback = self._cb.protocol_fight
        if callback is None:
            return False
        if self._now() < self._protocol_quiet_until:
            return False
        try:
            return bool(callback() is True)
        except Exception as exc:  # noqa: BLE001
            self._log("protocol_fight failed: %s" % type(exc).__name__)
            return False

    def _apply_friction(self, level: Optional[int]) -> None:
        callback = self._cb.friction_step
        if callback is None:
            return
        try:
            callback(level)
        except Exception as exc:  # noqa: BLE001 - 摩擦失败不打断搏鱼
            self._log("friction_step failed: %s" % type(exc).__name__)

    def _ensure_fish(self, now: float) -> str:
        if self._fish_id is None:
            self._fish_seq += 1
            self._fish_id = "%s-f%d" % (self._epoch, self._fish_seq)
        if self._fish_since is None:
            # 从抛竿/接管确认鱼存在的时间起算 cast_elapsed。
            self._fish_since = now
        return self._fish_id

    def _ensure_hooked(self, now: float) -> str:
        """确认中鱼后才启动搏鱼计时；不把抛竿时刻混进 fish_elapsed。"""
        fish_id = self._ensure_fish(now)
        if self._hooked_since is None:
            self._hooked_since = now
        return fish_id

    def _clear_fish(self) -> None:
        self._fish_id = None
        self._fish_since = None
        self._hooked_since = None

    def _settle_seen(self, obs: aobs.Observation) -> None:
        token = None
        evidence = obs.evidence
        if isinstance(evidence, Mapping):
            for key in ("settlement_id", "settle_id", "capture_id"):
                value = evidence.get(key)
                if value is not None and str(value).strip():
                    token = str(value)
                    break
        self._settle_token_value = token

    def _settle_token(self) -> str:
        token = self._settle_token_value
        if token:
            return token
        return self._fish_id or "%s-v%d" % (self._epoch, self._state_version)

    def _keepnet_full(self) -> bool:
        callback = self._cb.keepnet_full
        if callback is None:
            return False
        try:
            return bool(callback())
        except Exception as exc:  # noqa: BLE001 - 可选探针失败不改变已确认的入护
            self._log("keepnet_full error: %s" % type(exc).__name__)
            return False

    # ------------------------------------------------------------------
    # 外部生命周期
    # ------------------------------------------------------------------
    def stop_now(self, reason: str = "shutdown", attempts: Optional[int] = None) -> bool:
        """请求并立即执行停止动作（宿主退出时用），返回是否完整释放。"""
        self._control.request_stop(reason)
        now = self._now()
        if self._state is AutoFishState.STOPPED:
            return True
        self._ready_grant = None
        self._keep_chain_pending = False
        if self._state is not AutoFishState.STOPPING:
            # 释放统一交给 _shutdown_actions，避免这里多算一次重试。
            self._enter(AutoFishState.STOPPING, reason, now, release=False)
        if attempts is not None:
            ok = self._shutdown_actions(attempts)
        else:
            ok = self._shutdown_actions()
        self._enter(AutoFishState.STOPPED, "stopped" if ok else "stopped_release_failed", now)
        return ok

    def set_mode(self, mode: ControlMode | str, reason: str = "mode") -> None:
        """适配层显式切换控制模式（MANUAL 用于人工接管）。"""
        target = mode if isinstance(mode, ControlMode) else ControlMode(str(mode).upper())
        self._requested_mode = target
        # 切模式不是 confirm_ready；任何旧单次许可都作废。
        self._ready_grant = None
        self._keep_chain_pending = False
        if target in (ControlMode.OBSERVE, ControlMode.MANUAL):
            self._leave_to_observe(self._now())
        self._set_reason(reason)
