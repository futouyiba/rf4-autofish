"""RF4 自动钓鱼 —— 搏鱼阶段离线状态机（状态机阶段 2 内核，**不接入**）。

本模块是纯逻辑：输入只有 `Observation`，输出只有状态、事件和通过
`InputArbiter` 声明的按键不变量。它没有截图、OCR、窗口、焦点、GUI 或真实
输入入口，也不 import 适配器。Shadow 尚未通过第二轮实机验收，所以本内核
只在 tests 里使用，**不得接进运行主流程**。

状态集合刻意只有六个：

    FIGHT   搏鱼：期望按住 F5+F6；唯一允许动态摩擦控制的状态
    LAND    临岸：期望按住 F5+F6；进入时启动 near_grace 计时
    NET     抄网：期望只按住 F6；掏网/收网各只发一次 F7 切换
    SETTLE  结算：全松；释放失败的差集在后续 tick 重试
    LOST    脱钩：全松，终态（释放失败的差集在后续 tick 重试）
    STOPPED 未启动/已停止：零输入（释放只由显式 stop 重试）

设计约束（与 docs/state-machine.md §5-§8 对齐）：

1. 构造无副作用。只有显式 `start()` 才进入 FIGHT 并声明 F5/F6。start 本身
   不会让 `control_allowed` 变 True：必须等**第一帧新鲜且无遮挡**的观测。
2. 每次 `tick()` **先**做终态释放重试（SETTLE / LOST 在帧门之前），再判断
   帧是否新鲜。SETTLE / LOST 的重复帧也能继续重试松键；STOPPED 永远零输入。
3. `captured` 只能用 Observation 的事实，接口里没有 source/context 参数，
   因此不存在"用 source 强伪造 captured"的路径。捕获几何本身尚未可靠，
   本内核只用于测试。
4. 脱钩 = `fish_icon=False` 且 `bar_level==0` **连续** `lost_hold` 秒。
   `UNKNOWN` 不等于 False：任一未知/可读为真的帧都会清零连续计时。单个
   未知帧、单个超时都不会松键。没有总体 120/180 秒限制。
5. FIGHT -> LAND 需要 fish=True 且 ring 是**有效有限值**（[0,1]）且
   >= ring_min，连续 `land_frames` 个**独立帧**。F5/F6 集合不变，不产生
   任何按键事件。帧时间间隔超过 `max_frame_gap` 时不假装连续：清空
   captured / land / lost 连续计数，并在 LAND 中重新起 near 宽限；不松键、
   也不擅自判丢鱼。
6. LAND 计时从进入 LAND 的那一 tick 算。ring 变为 < ring_min / None /
   NaN / Inf -> 立即回 FIGHT，near 计时销毁。达到 near_grace -> NET：
   只释放 F5、保留 F6，并调用一次 net_action（每次 start 最多一次）。
   `auto_net_enabled=False`（默认）时在**任何按键变更之前**拦截：保持
   LAND 与 {F5,F6}，只置 `auto_net_blocked`；仅把 net_action 设为 None
   不能阻止旧路径松 F5 进 NET。
7. **帧新鲜度**：`clock()` 与 `Observation.monotonic_ts` 必须同为
   monotonic 时间轴。`now - obs.ts > max_observation_age`（过时）或
   `obs.ts > now + FUTURE_TOLERANCE`（未来）的帧整帧拒绝：不累计任何连续
   证据、不发新动作、清 near/captured/lost、LAND 计时重置，`control_allowed`
   保持 False，直到重新收到新鲜有效观测。`max_frame_gap` 同时检查观测
   时间戳间隔和接收时钟间隔 —— 任一超限都视为稀疏帧。
8. **遮挡冻结**：`foreground=False`、`hud_visible != TRUE` 或
   `ok_modal == TRUE` 时只冻结控制意图：不松键、不发新 keyDown、不调用
   net_action；清空 near/captured/lost 连续证据、LAND 计时重置、
   `control_allowed=False`。状态本身不变，终态释放重试不受此门限制。恢复
   新鲜无遮挡后，连续证据必须重新累积。
9. **NET 是可持续的抄网会话，不是永久人工挂起态。**
   * 我方维护 `NetState.STOWED/DEPLOYED/UNKNOWN`，这只是**动作账**，不是
     视觉确认；snapshot 用 `net_state_basis` 标明 assumed / commanded。
   * 初始 `STOWED` 只允许用于"本次是正常新钓鱼会话"（由 Lifecycle 在成功
     抛竿后显式传入）。中途接管必须传 `UNKNOWN`，未知时绝不盲 toggle。
   * LAND -> NET 需要 STOWED，且掏网、收网两个回调都存在；否则保持 LAND
     与 {F5,F6}，只记录阻断原因。进入 NET 后先确认 F6 按住，再调用一次
     `net_action`；**回调成功才置 DEPLOYED**。回调缺失/异常/返回 False
     则置 UNKNOWN + fault，保持 NET 与 F6，不恢复 F5、不重复盲 toggle。
   * NET 中，新鲜有效帧的 ring 连续 `net_exit_frames` 帧 < `net_exit_ring`
     时调用一次 `net_retract_action`；成功后等待 `net_retract_anim` 秒
     （从回调返回后的 clock 时刻起算，可被 stop 取消），再置 STOWED、回
     FIGHT 并恢复 F5。收网失败/取消
     同样标 UNKNOWN + fault，保持 NET 与 F6，绝不跳过收网直接按 F5。
   * `None/NaN/失焦/过时/重复/倒序` 帧不算低环，也不收网、不恢复 F5；连续
     性一旦中断就清零。收网后从 FIGHT 重新累计临岸 3 帧 + 8s，不继承旧
     计时，并用 `net_cooldown` 抑制来回收网的振荡。
   * `net_window` 只保留"提醒"，不再是放弃/人工确认的常规出口。
10. `control_allowed` 只在 FIGHT、有新鲜无遮挡观测、无 fault、无
    input_error，且 arbiter 的 emitted 与 desired 完全一致、desired 恰为
    {F5,F6} 时为 True。start 的部分失败（例如只按下了 F5）不会授权摩擦控制。
11. 状态迁移事件保存在有界队列里，含 from/to/reason/time/desired_keys 及
    当前帧的 ring/fish/tier。状态退出时销毁该状态局部的计时器与连续帧计数。
    captured 连续确认是跨状态的安全确认，只由非 TRUE 帧、start()、stop()
    等清零。
12. `stop()` 用户优先：先进入 STOPPED、清 desired，再释放 bot 自己
    owned 的键；可重入。停止后的 tick 不再产生任何输入。

已知局限：

* captured 的几何判据尚未可靠，本内核不据此接管实机。
* 没有 OCR、没有摩擦校准、没有 29 硬顶实现，只有 `control_allowed` 边界。
* `ring_min=0.98` 是进入 LAND/NET 的默认值：用户实机确认 90% 门槛偏低，
  会过早把正常收线误当临岸；97~98 才是可靠的收线到位读数。圆环是**线杯
  剩余线量比例**，刚抛竿时约 93% 属于正常，不代表"收线完成"。
* `net_exit_ring=0.90` 是 NET 退出阈值默认值，与进入阈值独立形成回差
  （98 入、90 出）；NET 中 ring 连续低于它才收网，避免临岸/出线之间振荡。
  60~70 只是用户举例，不把任何单一读数写死成唯一真值。
* 抄网的真实按键由注入的 `net_action` / `net_retract_action` 决定；内核
  只记自己的动作账，无法证明游戏侧真的掏出/收回了网。
* arbiter 的 `emitted` 只是**本进程成功发出的按键账本**，不是游戏侧
  "确实收到按键 / 确实按住"的确认；本内核无法证明后者。
"""

from __future__ import annotations

import enum
import math
import time
from collections import deque
from dataclasses import dataclass

from autofish_input import F5, F6, InputArbiter, InputDispatchError
from autofish_observation import Observation, TriState

__all__ = [
    "FightEvent",
    "FightConfig",
    "FightMachine",
    "FightState",
    "NetState",
    "State",
]


class FightState(enum.Enum):
    """离线搏鱼状态机可处于的状态。"""

    FIGHT = "fight"
    LAND = "land"
    NET = "net"
    SETTLE = "settle"
    LOST = "lost"
    STOPPED = "stopped"


# 便于 `from autofish_fight_machine import State` 的调用方。
State = FightState


class NetState(enum.Enum):
    """我方对"网现在在哪"的**动作账**，不是视觉确认。

    * STOWED   已收好/假定未掏出（只有正常新钓鱼会话才可假定）
    * DEPLOYED 本会话成功发过掏网切换（commanded）
    * UNKNOWN  中途接管、动作失败或部分完成；不得盲 toggle
    """

    STOWED = "stowed"
    DEPLOYED = "deployed"
    UNKNOWN = "unknown"



def _require_finite(name: str, value: object) -> float:
    """有限实数；bool、NaN、Inf、非数值一律拒绝。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _require_seconds(name: str, value: object) -> float:
    """时间字段：有限且非负。"""
    number = _require_finite(name, value)
    if number < 0.0:
        raise ValueError(f"{name} must be a finite number >= 0")
    return number


def _require_count(name: str, value: object) -> int:
    """数量字段：正整数，bool（它是 int 的子类）不算。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


# 观测时间戳允许比接收时钟超前的小容差（调度抖动，不是"未来帧"）。
FUTURE_TOLERANCE = 0.05


@dataclass(frozen=True)
class FightConfig:
    """时间与连续帧阈值。全部时间单位为秒，使用注入 clock 的同一时间轴。

    `clock()` 返回的接收时刻必须与 `Observation.monotonic_ts` 同属一个
    monotonic 时间轴，否则新鲜度/帧间隔判据没有意义。
    """

    # captured 连续确认帧数（必须是独立、递增时间戳的帧）。
    captured_frames: int = 2
    # FIGHT -> LAND 的 ring 连续帧数。
    land_frames: int = 3
    # ring 到达这个值才算临岸；NaN/Inf/越界一律视为不可读。
    # 进入 LAND/NET 的 ring 门槛。用户实机确认 90% 偏低，改 0.98；
    # 与 net_exit_ring=0.90 形成 98 入 / 90 出的独立回差。
    ring_min: float = 0.98
    # LAND 进入后等多久才进 NET。
    near_grace: float = 8.0
    # 是否允许自动抄网（LAND -> NET，松 F5、保留 F6、调用一次 net_action）。
    #
    # 生产默认必须保持 False，直到实机验收后再显式启用；`RF4_AUTO_NET=1`
    # 仅供人工验收。关闭时 LAND 到点后**保持 F5+F6 与 LAND**，不松 F5、
    # 不进 NET、不调用 net_action；仅置 auto_net_blocked 供上层显示原因。
    auto_net_enabled: bool = False
    # NET 中 ring 低于此值（连续 net_exit_frames 帧）触发收网。
    # 0.90 与进入阈值 0.98 独立，形成明确回差；60~70 只是用户举例，不在此写死。
    net_exit_ring: float = 0.90
    # 收网触发的连续帧数；同一时间戳的帧不会重复累计。
    net_exit_frames: int = 3
    # 收网成功后等待切换动画的秒数（内核按 clock 计时，stop 可立即取消）。
    net_retract_anim: float = 1.2
    # 收网回 FIGHT 后的冷却，抑制临岸/出线之间的来回 toggle。
    net_cooldown: float = 5.0
    # NET 观察窗口；到期只发一次提醒，不再要求人工确认、不放弃。
    net_window: float = 15.0
    # 脱钩判据的连续持续时间。
    lost_hold: float = 2.0
    # 允许的最大帧间隔；观测时间戳或接收时钟任一超限即视为稀疏帧。
    max_frame_gap: float = 1.5
    # 观测允许的最大年龄；超过即视为过时帧（整帧拒绝）。
    max_observation_age: float = 1.5
    # 迁移事件队列上限（只保留最近 N 条）。
    event_limit: int = 256

    def __post_init__(self) -> None:
        _require_count("captured_frames", self.captured_frames)
        _require_count("land_frames", self.land_frames)
        _require_count("net_exit_frames", self.net_exit_frames)
        _require_count("event_limit", self.event_limit)
        if not isinstance(self.auto_net_enabled, bool):
            raise TypeError("auto_net_enabled must be a bool")

        ring_min = _require_finite("ring_min", self.ring_min)
        if not (0.0 <= ring_min <= 1.0):
            raise ValueError("ring_min must be in [0, 1]")

        exit_ring = _require_finite("net_exit_ring", self.net_exit_ring)
        if not (0.0 <= exit_ring <= 1.0):
            raise ValueError("net_exit_ring must be in [0, 1]")

        _require_seconds("near_grace", self.near_grace)
        _require_seconds("net_window", self.net_window)
        _require_seconds("net_retract_anim", self.net_retract_anim)
        _require_seconds("net_cooldown", self.net_cooldown)
        _require_seconds("lost_hold", self.lost_hold)

        gap = _require_seconds("max_frame_gap", self.max_frame_gap)
        if gap <= 0.0:
            raise ValueError("max_frame_gap must be > 0")

        age = _require_seconds("max_observation_age", self.max_observation_age)
        if age <= 0.0:
            raise ValueError("max_observation_age must be > 0")


@dataclass(frozen=True)
class FightEvent:
    """一次状态迁移的快照。desired_keys 是迁移完成后声明的按键集合。

    ring/fish/tier 是**触发迁移的那一帧**的读数（start/stop 等无帧迁移为
    None）。它们只是当帧证据，不代表迁移后仍然成立。
    """

    time: float
    from_state: FightState
    to_state: FightState
    reason: str
    desired_keys: tuple[str, ...]
    ring_ratio: float | None = None
    fish_icon: str | None = None
    bar_tier: int | None = None

    # 常见别名，方便日志/UI 读取；不改变存储字段。
    @property
    def from_(self) -> FightState:
        return self.from_state

    @property
    def to(self) -> FightState:
        return self.to_state

    @property
    def desired(self) -> tuple[str, ...]:
        return self.desired_keys


def _valid_ring(value: float | None) -> float | None:
    """只接受有限且在 [0,1] 内的 ring；其余（None/NaN/Inf/越界）都是不可读。"""
    if value is None:
        return None
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ratio) or not (0.0 <= ratio <= 1.0):
        return None
    return ratio


class FightMachine:
    """纯离线搏鱼状态机。

    典型用法：

        arb = InputArbiter(sink)
        machine = FightMachine(arb, clock=clock, net_action=do_net)
        machine.start()
        for obs in stream:
            machine.tick(obs)

    构造函数不读 clock、不碰 arbiter、不产生事件。
    """

    def __init__(
        self,
        arbiter: InputArbiter,
        clock=time.monotonic,
        net_action=None,
        net_retract_action=None,
        config: FightConfig | None = None,
    ) -> None:
        if not isinstance(arbiter, InputArbiter):
            raise TypeError("FightMachine requires an InputArbiter")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if net_action is not None and not callable(net_action):
            raise TypeError("net_action must be callable or None")
        if net_retract_action is not None and not callable(net_retract_action):
            raise TypeError("net_retract_action must be callable or None")

        self._arbiter = arbiter
        self._clock = clock
        self._net_action = net_action
        self._net_retract_action = net_retract_action
        self._config = config or FightConfig()

        self._state = FightState.STOPPED
        self._events: deque[FightEvent] = deque(maxlen=self._config.event_limit)

        # 帧身份：只有严格递增的 monotonic_ts 且满足新鲜度才算一帧新观测。
        self._last_frame_ts: float | None = None
        # 上一帧被接受时的接收时钟读数，用于 max_frame_gap 的第二条判据。
        self._last_recv_now: float | None = None
        # 是否已经收到过一帧新鲜且无遮挡的观测。start 后为 False，直到首帧。
        self._fresh_obs = False
        # 最近一帧新鲜观测，供迁移事件记录当帧证据；start/stop 时为 None。
        self._last_obs: Observation | None = None

        # 各状态局部的计时/计数（退出状态即销毁）。
        self._captured_frames = 0
        self._land_frames = 0
        self._land_since: float | None = None
        self._net_deadline: float | None = None
        self._lost_since: float | None = None

        # 我方抄网动作账。STOWED 只由 start(net_state=...) 的正常新会话假定，
        # 或由成功收网/人工确认给出；失败一律 UNKNOWN。
        self._net_state = NetState.UNKNOWN
        self._net_state_basis = "unknown_mid_takeover"
        self._net_exit_frames = 0
        self._net_retract_attempted = False
        self._net_retract_wait_until: float | None = None
        self._net_cooldown_until: float | None = None
        self._net_window_reminded = False

        # 本次 start 是否已经发过掏网切换键。
        self._net_attempted = False
        # LAND 已到 near_grace，但被禁用/回调缺失/网状态未知拦住。
        # 只用于上层显示原因，不改变状态/按键；每次 LAND tick 重算。
        self._auto_net_blocked = False
        self._auto_net_blocked_reason: str | None = None
        # NET 中是否已请求/已发出收网切换（同状态事件只记一次）。
        self._net_retract_pending = False
        # 当前帧与上一帧的时间差是否超过 max_frame_gap（由 _accept_frame 置位）。
        self._last_gap_exceeded = False

        # 诊断：输入失败与 NET 故障。fault 由掏网/收网动作异常设置。
        self._input_error: str | None = None
        self._fault = False
        self._fault_reason: str | None = None

    # ==================== 只读视图 ====================

    @property
    def state(self) -> FightState:
        return self._state

    @property
    def config(self) -> FightConfig:
        return self._config

    @property
    def events(self) -> tuple[FightEvent, ...]:
        """最近的迁移事件（旧 -> 新，按发生顺序）。队列有界。"""
        return tuple(self._events)

    @property
    def control_allowed(self) -> bool:
        """是否允许动态摩擦控制。

        必须同时满足：FIGHT、已经收到过新鲜无遮挡的观测、无 fault、最近一次
        输入同步没有失败，并且 arbiter 实际按住（emitted）与期望（desired）
        完全一致且恰为 {F5, F6}。start 本身不授权；部分失败（例如只按下
        F5）也不会授权。
        """
        desired = self._arbiter.desired
        return (
            self._state is FightState.FIGHT
            and self._fresh_obs
            and not self._fault
            and self._input_error is None
            and desired == frozenset({F5, F6})
            and self._arbiter.emitted == desired
        )

    @property
    def fault(self) -> bool:
        """net_action 失败后置位，冻结 NET，直到 clear_fault()/stop()。"""
        return self._fault

    @property
    def faulted(self) -> bool:
        return self._fault

    @property
    def fault_reason(self) -> str | None:
        return self._fault_reason

    @property
    def input_error(self) -> str | None:
        """最近一次按键 sink 失败的信息；成功后清空。"""
        return self._input_error

    @property
    def desired_keys(self) -> frozenset[str]:
        return self._arbiter.desired

    @property
    def owned_keys(self) -> frozenset[str]:
        return self._arbiter.owned

    @property
    def net_attempted(self) -> bool:
        """本次 start 是否已经进入过 NET（进入即 True，与回调是否成功无关）。"""
        return self._net_attempted

    @property
    def net_state(self) -> NetState:
        """我方抄网动作账；不是视觉确认。"""
        return self._net_state

    @property
    def net_state_basis(self) -> str:
        """net_state 的依据：assumed_new_session / unknown_mid_takeover /
        commanded_deploy / commanded_retract / unknown_fault / operator_confirmed。
        """
        return self._net_state_basis

    @property
    def auto_net_blocked(self) -> bool:
        """LAND 已到临岸宽限，但被禁用/回调缺失/网状态未知拦住。

        True 时按键不变量仍是 {F5, F6}，状态仍是 LAND：不松收线键、不进 NET、
        不调用 net_action。上层据此显示原因。
        """
        return self._auto_net_blocked

    @property
    def auto_net_blocked_reason(self) -> str | None:
        """LAND 无法自动抄网的具体原因（auto_net_disabled / callback_missing /
        net_state_unknown / net_not_stowed）。"""
        return self._auto_net_blocked_reason

    @property
    def net_retract_pending(self) -> bool:
        """NET 中收网切换已发出/在等待动画，或动作失败待人工处理。"""
        return self._net_retract_pending

    # ==================== 生命周期 ====================

    def start(self, net_state: NetState | str | None = None) -> FightState:
        """显式启动：进入 FIGHT 并声明 F5+F6。

        ``net_state`` 是**调用方对本会话抄网位置的动作账假定**：
        * 正常新钓鱼会话（本进程成功抛竿、尚未掏过网）传 ``STOWED``；
        * 中途接管（启动时鱼已经在线）必须传 ``UNKNOWN``，不能盲 toggle。
        缺省 ``None`` 等同于 ``UNKNOWN``（fail-safe）。

        已经在 FIGHT/LAND/NET 中时是幂等的；从 STOPPED/SETTLE/LOST 启动会
        开始新一轮并清空全部局部计时。构造本对象本身没有任何输入副作用。
        """
        if self._state in (FightState.FIGHT, FightState.LAND, FightState.NET):
            return self._state

        old = self._state
        now = self._clock()
        self._state = FightState.FIGHT
        self._reset_state_locals()
        self._captured_frames = 0
        self._last_frame_ts = None
        self._last_recv_now = None
        self._fresh_obs = False
        self._last_obs = None
        self._last_gap_exceeded = False
        self._net_attempted = False
        self._net_retract_pending = False
        self._net_exit_frames = 0
        self._net_retract_attempted = False
        self._net_retract_wait_until = None
        self._net_cooldown_until = None
        self._net_window_reminded = False
        self._set_initial_net_state(net_state)
        self._fault = False
        self._fault_reason = None

        self._arbiter.set_desired({F5, F6}, "start")
        self._record(now, old, FightState.FIGHT, "start")
        # 启动是显式动作，失败直接抛给调用方；下一次 tick 的同步会重试
        # 未完成的差集（arbiter 保留真实 emitted）。
        try:
            self._arbiter.reconcile("start")
            self._input_error = None
        except InputDispatchError as exc:
            self._input_error = str(exc)
            raise
        return self._state

    def _set_initial_net_state(self, value: NetState | str | None) -> None:
        if value is None:
            self._net_state = NetState.UNKNOWN
            self._net_state_basis = "unknown_mid_takeover"
            return
        if isinstance(value, str):
            try:
                value = NetState(value.lower())
            except ValueError as exc:
                raise ValueError("unknown net_state %r" % (value,)) from exc
        if not isinstance(value, NetState):
            raise TypeError("net_state must be NetState or str")
        if value is NetState.STOWED:
            self._net_state = NetState.STOWED
            self._net_state_basis = "assumed_new_session"
        elif value is NetState.UNKNOWN:
            self._net_state = NetState.UNKNOWN
            self._net_state_basis = "unknown_mid_takeover"
        else:
            # 外部不能声称网已掏出但不给出"本次已 command"的证据。
            raise ValueError("initial net_state must be STOWED or UNKNOWN")

    def stop(self) -> FightState:
        """用户优先的停止：进入 STOPPED，释放 bot 自己 owned 的键。

        先改状态、清局部计时，再释放按键，因此即使 sink 失败也不会让 tick
        继续输入。可重入；已停止且没有 owned 键时不做任何事。释放失败会抛
        `InputDispatchError`，但状态已是 STOPPED，再次 stop() 会重试剩余键。
        """
        old = self._state
        if old is FightState.STOPPED and not self._arbiter.owned:
            return self._state

        now = self._clock()
        self._state = FightState.STOPPED
        self._reset_state_locals()
        self._captured_frames = 0
        self._fresh_obs = False
        self._last_obs = None
        self._fault = False
        self._fault_reason = None
        # 停止只证明键已释放，不证明网收回了；下次直接 start 不得假定 STOWED。
        self._net_state = NetState.UNKNOWN
        self._net_state_basis = "stopped"
        self._arbiter.set_desired(set(), "stop")
        if old is not FightState.STOPPED:
            self._record(now, old, FightState.STOPPED, "stop")
        try:
            self._arbiter.release_owned("stop")
            self._input_error = None
        except InputDispatchError as exc:
            self._input_error = str(exc)
            raise
        return self._state

    def clear_fault(self, reason: str = "manual") -> bool:
        """人工解除 NET 故障冻结。

        只清 fault 标志，**不会**重发任何切换键，也**不**代表网已收回：
        状态仍是 NET、F6 仍按住，`net_state` 仍是 UNKNOWN；后续不会自动
        再次 toggle。要重新离开 NET，必须由实际收网回调成功（正常路径）
        或人工显式调用 `confirm_net_retracted()`。返回是否真的清除了故障。
        """
        if not self._fault:
            return False
        self._fault = False
        self._fault_reason = None
        return True

    def reset_net_attempt(self) -> bool:
        """只清除"本次 start 已尝试过掏网切换"标记。

        **不能替代实际收网**：不改状态、不改 desired/emitted、不发送任何
        按键事件，也不会让 NET 自动回到 FIGHT。返回是否真的清除了标记。
        """
        if not self._net_attempted:
            return False
        self._net_attempted = False
        return True

    def confirm_net_retracted(self, reason: str = "net_retracted") -> bool:
        """人工显式确认"网已收回"：NET -> FIGHT，并重新累积 near。

        正常自动路径由 `net_retract_action` 成功 + 动画等待完成，**不需要**
        本方法。本方法只保留给人工兜底：它不发送任何按键，只是把动作账
        写成 operator_confirmed/STOWED，然后恢复 F5。只有当前处于 NET 时
        生效；其他状态返回 False。`clear_fault()` / `reset_net_attempt()`
        都无法替代本调用。
        """
        if self._state is not FightState.NET:
            return False
        now = self._clock()
        old = self._state
        self._state = FightState.FIGHT
        self._reset_state_locals()
        self._land_frames = 0
        self._net_state = NetState.STOWED
        self._net_state_basis = "operator_confirmed"
        self._net_cooldown_until = now + self._config.net_cooldown
        # 收网确认是一个新的控制纪元：要等下一帧新鲜无遮挡观测才重新授权。
        self._fresh_obs = False
        self._fault = False
        self._fault_reason = None
        self._net_retract_pending = False
        self._net_attempted = False
        self._net_retract_attempted = False
        self._net_retract_wait_until = None
        self._sync_desired(reason)
        self._record(now, old, FightState.FIGHT, reason)
        return True

    # ==================== 帧门 ====================

    def _accept_frame(self, obs: Observation, now: float) -> bool:
        """时间戳必须有限、新鲜且严格递增；过时/未来/重复/倒序帧整帧拒绝。

        新鲜度用接收时钟 `now` 与 `obs.monotonic_ts` 比较（两者必须同轴）：
        过时超过 `max_observation_age` 或超前超过 `FUTURE_TOLERANCE` 都不算
        有效观测；这类帧还会清空连续证据并让 `control_allowed` 保持 False。

        接受新帧时同时用**观测时间戳间隔**和**接收时钟间隔**检查
        `max_frame_gap`，任一超限都置位 `_last_gap_exceeded`，由 tick 清空
        连续计数（不把稀疏帧当连续）。
        """
        try:
            ts = float(obs.monotonic_ts)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(ts):
            return False
        if now - ts > self._config.max_observation_age or ts > now + FUTURE_TOLERANCE:
            # 过时或来自未来的帧：不累计、不动作、断开连续性。
            self._apply_stale_frame(now)
            return False
        prev = self._last_frame_ts
        if prev is not None and ts <= prev:
            return False

        prev_now = self._last_recv_now
        ts_gap = None if prev is None else ts - prev
        clock_gap = None if prev_now is None else now - prev_now
        self._last_frame_ts = ts
        self._last_recv_now = now
        self._last_gap_exceeded = any(
            gap is not None and gap > self._config.max_frame_gap
            for gap in (ts_gap, clock_gap)
        )
        return True

    def _apply_sparse_frame(self, now: float) -> None:
        """帧间隔超限：清空连续计数，但不松键、不判丢鱼。

        LAND 中额外把 near 宽限从现在重新计时，避免稀疏帧让"临岸已持续
        8 秒"变成假证据。
        """
        self._captured_frames = 0
        self._land_frames = 0
        self._net_exit_frames = 0
        self._lost_since = None
        if self._state is FightState.LAND:
            self._land_since = now

    def _apply_stale_frame(self, now: float) -> None:
        """过时/未来帧：断开全部连续证据，且不授权控制。"""
        self._fresh_obs = False
        self._apply_sparse_frame(now)

    @staticmethod
    def _is_occluded(obs: Observation) -> bool:
        """本帧是否不可信（失焦/HUD 不可见/有确定弹窗）。"""
        return (
            not obs.foreground
            or obs.hud_visible is not TriState.TRUE
            or obs.ok_modal is TriState.TRUE
        )

    def _apply_occluded_frame(self, now: float) -> None:
        """遮挡帧：只冻结控制意图，不松键、不发新动作。

        清空 near/captured/lost 连续证据并重置 LAND 计时；状态保持不变，
        终态的释放重试在帧门之前处理，不受影响。
        """
        self._fresh_obs = False
        self._apply_sparse_frame(now)

    # ==================== 主 tick ====================

    def tick(self, obs: Observation) -> FightState:
        """吃一帧观测，推进状态。返回当前状态。

        STOPPED 零输入；LOST / SETTLE 只重试释放（在帧门之前，重复帧也
        可以重试）。过时/未来/重复/倒序帧整帧忽略；失焦、HUD 不可见或有
        确定弹窗的帧只冻结控制意图。按键同步失败时本 tick 立即结束，不做
        任何状态迁移。
        """
        if self._state is FightState.STOPPED:
            # 用户已停止：零输入；释放只由显式 stop() 重试。
            return self._state
        if self._state is FightState.LOST:
            # 终态：不再响应遥测、绝不重按，只重试空集合释放。
            self._sync_desired("lost")
            return self._state
        if self._state is FightState.SETTLE:
            # 结算态只做一件事：重试未完成的释放；不再响应其他信号。
            # 放在帧门之前：重复帧/过时帧也必须能继续松键。
            self._sync_desired("settle")
            return self._state

        now = self._clock()
        if not self._accept_frame(obs, now):
            return self._state
        if self._is_occluded(obs):
            self._apply_occluded_frame(now)
            return self._state

        # 新鲜且无遮挡：从这里开始才允许控制（仍需 FIGHT / 无 fault 等）。
        self._fresh_obs = True
        self._last_obs = obs
        if self._last_gap_exceeded:
            self._apply_sparse_frame(now)

        # 1) captured 连续确认优先。只认 Observation.captured 的事实，
        #    没有 source/context 参数，无法被"来源上下文"强伪造。
        if obs.captured is TriState.TRUE:
            self._captured_frames += 1
            if self._captured_frames >= self._config.captured_frames:
                self._enter_settle(now, "captured_confirmed")
                return self._state
        else:
            self._captured_frames = 0

        if self._state is FightState.NET and self._fault:
            # 故障冻结：保持 F6，不自动重发 net_action、不自动切网。
            return self._state

        # 每次 tick 重放一次不变量，只发差集；成功时通常 0 事件，
        # 同时给上一次失败的输入一次重试机会（不会无限循环）。
        # 同步失败时本 tick 立即结束：绝不能在 F6 未按住的情况下继续迁移
        # 状态或执行 net_action。
        if not self._sync_desired("tick"):
            return self._state

        if self._state is FightState.FIGHT:
            self._tick_fight(obs, now)
        elif self._state is FightState.LAND:
            self._tick_land(obs, now)
        elif self._state is FightState.NET:
            self._tick_net(obs, now)
        else:  # pragma: no cover - 状态集合已穷尽
            raise AssertionError(f"unexpected state {self._state!r}")
        return self._state

    # ==================== 各状态 ====================

    def _tick_fight(self, obs: Observation, now: float) -> None:
        if self._update_lost(obs, now):
            self._enter_terminal(
                FightState.LOST, now, "detached", "lost"
            )
            return

        # 刚收网回来：冷却期内不重新累计临岸证据，避免网/竿来回切换。
        # 冷却只抑制 LAND，不改变用户既定的"3 帧 + 8s"控制律。
        cooldown_until = self._net_cooldown_until
        if cooldown_until is not None and now < cooldown_until:
            self._land_frames = 0
            return

        ring = _valid_ring(obs.ring_ratio)
        near = (
            obs.fish_icon is TriState.TRUE
            and ring is not None
            and ring >= self._config.ring_min
        )
        if not near:
            self._land_frames = 0
            return

        self._land_frames += 1
        if self._land_frames >= self._config.land_frames:
            # F5/F6 集合不变；不产生按键事件（arbiter 差集为空）。
            self._enter_land(now, "near_confirmed")

    def _tick_land(self, obs: Observation, now: float) -> None:
        ring = _valid_ring(obs.ring_ratio)
        if ring is None or ring < self._config.ring_min:
            # 临岸证据消失：立即回 FIGHT，near 计时销毁。
            self._enter_fight(now, "ring_lost")
            return

        if self._update_lost(obs, now):
            self._enter_terminal(
                FightState.LOST, now, "detached", "lost"
            )
            return

        since = self._land_since
        if since is None:
            self._land_since = now
            return
        if now - since >= self._config.near_grace:
            self._try_enter_net(now)

    def _tick_net(self, obs: Observation, now: float) -> None:
        """NET：掏网后持续观察，收网由 ring 低环连续帧自动触发。

        1. 强捕获在进入本函数前已优先处理（SETTLE）。
        2. 动作失败/fault：保持 NET + F6，不再盲 toggle，也不恢复 F5。
        3. 已发出收网切换：等待 ``net_retract_anim``；到期后回 FIGHT 并
           重新按 F5。等待期间 stop 可立即取消。
        4. 正常观察：只有新鲜有效帧的 ring < ``net_exit_ring`` 连续 N 帧
           才收网；None/NaN/失焦/过时/重复帧都不算低环，并中断连续计数。
        5. ``net_window`` 只发提醒，不放弃、不要求人工确认。
        """
        if self._net_retract_wait_until is not None:
            if now >= self._net_retract_wait_until:
                self._complete_net_retract(now)
            return

        if self._fault or self._net_state is not NetState.DEPLOYED:
            # 无法安全收网：保持 F6，不按 F5，也不重复 toggle。
            self._maybe_remind_net_window(now)
            return

        # 强捕获优先：已有 captured 候选时先等它凑够确认帧，绝不在结算
        # 动画期间抢发收网切换。候选消失后低环计数继续按新帧累计。
        if self._captured_frames > 0:
            return

        ring = _valid_ring(obs.ring_ratio)
        if ring is not None and ring < self._config.net_exit_ring:
            self._net_exit_frames += 1
        else:
            # 未知/退远/不可读都清连续性：不能把"读不到"当成低环。
            self._net_exit_frames = 0

        if self._net_exit_frames >= self._config.net_exit_frames:
            self._command_net_retract(now)
            return

        self._maybe_remind_net_window(now)

    def _maybe_remind_net_window(self, now: float) -> None:
        """NET 观察窗口到期只提醒一次；不是放弃/人工确认出口。"""
        if self._net_window_reminded:
            return
        deadline = self._net_deadline
        if deadline is None or now < deadline:
            return
        self._net_window_reminded = True
        self._record(now, FightState.NET, FightState.NET, "net_window_expired_reminder")

    def _mark_net_fault(self, now: float, reason: str, detail: str = "") -> None:
        """掏网/收网动作失败：UNKNOWN + fault，保持 NET/F6，不恢复 F5。"""
        self._fault = True
        self._fault_reason = detail or reason
        self._net_state = NetState.UNKNOWN
        self._net_state_basis = "unknown_fault"
        self._net_retract_pending = True
        self._record(now, FightState.NET, FightState.NET, reason)

    def _command_net_retract(self, now: float) -> None:
        """低环确认后发一次收网切换；成功只进入等待动画，先不恢复 F5。

        动画等待的起点是**回调返回后重新读取的 clock**，不是本次 tick 的开始
        时刻：回调本身可能很慢（切前台/注入耗时），用回调前的 now 会让
        `wait_until` 在回调返回时就已经过期，下一帧立刻补 F5，动画等待被吞掉。
        """
        if self._net_retract_attempted:
            return
        self._net_retract_attempted = True
        self._net_retract_pending = True

        if self._net_retract_action is None:
            self._mark_net_fault(now, "net_retract_action_missing")
            return
        try:
            result = self._net_retract_action()
        except Exception as exc:  # noqa: BLE001 - 领域故障，转为显式 fault
            self._mark_net_fault(
                now, "net_retract_failed", f"{type(exc).__name__}: {exc}"
            )
            return
        if result is False:
            self._mark_net_fault(now, "net_retract_returned_false")
            return

        self._net_retract_wait_until = self._clock() + self._config.net_retract_anim
        self._record(now, FightState.NET, FightState.NET, "net_retract_commanded")

    def _complete_net_retract(self, now: float) -> None:
        """收网动画等待结束：STOWED，恢复 FIGHT/F5，并重新累计临岸。"""
        old = self._state
        self._state = FightState.FIGHT
        self._net_state = NetState.STOWED
        self._net_state_basis = "commanded_retract"
        self._net_retract_wait_until = None
        self._net_retract_pending = False
        self._net_attempted = False
        self._net_retract_attempted = False
        self._net_cooldown_until = now + self._config.net_cooldown
        self._reset_state_locals()
        self._land_frames = 0
        # 收网完成是一个新的控制纪元：等下一帧新鲜无遮挡观测再授权。
        self._fresh_obs = False
        self._fault = False
        self._fault_reason = None
        self._sync_desired("net_retracted")
        self._record(now, old, FightState.FIGHT, "net_retracted")

    # ==================== 迁移 ====================

    def _enter_fight(self, now: float, reason: str) -> None:
        old = self._state
        self._state = FightState.FIGHT
        self._reset_state_locals()
        self._fault = False
        self._fault_reason = None
        self._sync_desired(reason)
        self._record(now, old, FightState.FIGHT, reason)

    def _enter_land(self, now: float, reason: str) -> None:
        old = self._state
        self._state = FightState.LAND
        self._reset_state_locals()
        self._land_since = now
        self._sync_desired(reason)
        self._record(now, old, FightState.LAND, reason)

    def _try_enter_net(self, now: float) -> bool:
        """LAND -> NET：先释放 F5（保留 F6），确认成功后才调用一次掏网动作。

        进入 NET 的**全部前置条件在任何按键变更之前**检查：
        * ``auto_net_enabled`` 打开；
        * 掏网、收网两个回调都存在（只有掏网回调、没有收网回调时绝不进入，
          否则会卡在网已掏出却不敢恢复 F5 的状态）；
        * 我方 net 动作账是 STOWED（正常新会话）；UNKNOWN 中途接管不盲 toggle。

        释放失败或按键未确认时只把 desired **恢复为 LAND 的 {F5,F6}**，
        不在同一 tick 里再 reconcile。掏网回调成功才置 DEPLOYED；异常/
        返回 False 置 UNKNOWN + fault，保持 NET 与 F6。返回是否进入 NET。
        """
        if not self._config.auto_net_enabled:
            # 生产默认路径：临岸只观察，继续收线提竿。
            self._auto_net_blocked = True
            self._auto_net_blocked_reason = "auto_net_disabled"
            return False
        if self._net_action is None or self._net_retract_action is None:
            # 缺任一回调都不能开始掏网：无法安全收尾就不松 F5。
            self._auto_net_blocked = True
            self._auto_net_blocked_reason = "net_callback_missing"
            return False
        if self._net_state is NetState.UNKNOWN:
            self._auto_net_blocked = True
            self._auto_net_blocked_reason = "net_state_unknown"
            return False
        if self._net_state is not NetState.STOWED:
            self._auto_net_blocked = True
            self._auto_net_blocked_reason = "net_not_stowed"
            return False
        if self._net_attempted:
            return False

        old = self._state
        self._arbiter.set_desired({F6}, "net")
        try:
            self._arbiter.reconcile("net")
        except InputDispatchError as exc:
            # 只恢复声明，不同 tick 再 reconcile。
            self._arbiter.set_desired({F5, F6}, "net_aborted")
            self._input_error = str(exc)
            return False

        if self._arbiter.emitted != frozenset({F6}):
            # 没有确认 F6 真正按住时绝不能调用 net_action。
            self._arbiter.set_desired({F5, F6}, "net_aborted")
            self._input_error = (
                "net keys not confirmed: "
                f"emitted={sorted(self._arbiter.emitted)}"
            )
            return False
        self._input_error = None

        self._state = FightState.NET
        self._reset_state_locals()
        self._net_deadline = now + self._config.net_window
        self._net_attempted = True
        self._net_retract_pending = False
        self._net_window_reminded = False
        self._net_exit_frames = 0
        self._net_retract_attempted = False
        self._net_retract_wait_until = None
        self._record(now, old, FightState.NET, "near_grace_elapsed")

        try:
            result = self._net_action()
        except Exception as exc:  # noqa: BLE001 - 领域故障，转为显式 fault
            self._mark_net_fault(
                now, "net_deploy_failed", f"{type(exc).__name__}: {exc}"
            )
            return True
        if result is False:
            self._mark_net_fault(now, "net_deploy_returned_false")
            return True

        # 只有掏网回调成功才把动作账写成 DEPLOYED；仍不是视觉确认。
        self._net_state = NetState.DEPLOYED
        self._net_state_basis = "commanded_deploy"
        self._record(now, FightState.NET, FightState.NET, "net_deployed")
        return True

    def _enter_settle(self, now: float, reason: str) -> None:
        self._enter_terminal(FightState.SETTLE, now, reason, "settle")

    def _enter_terminal(
        self, target: FightState, now: float, reason: str, sync_reason: str
    ) -> None:
        """进入 SETTLE / LOST：改状态、销毁局部计时、全松。"""
        old = self._state
        if old is target:
            return
        self._state = target
        self._reset_state_locals()
        self._fault = False
        self._fault_reason = None
        self._sync_desired(sync_reason)
        self._record(now, old, target, reason)

    # ==================== 计时/计数 ====================

    def _update_lost(self, obs: Observation, now: float) -> bool:
        """脱钩：fish=False 且 bar_level==0 连续 lost_hold 秒。

        任一 UNKNOWN / 非 false / 非 0 都清零连续计时。单帧不可以松键。
        NET 不评估：F5 已松开，bar_level 天然为 0，不能把"没张力"当脱钩。
        """
        detached_frame = (
            obs.fish_icon is TriState.FALSE
            and obs.bar_level is not None
            and obs.bar_level == 0
        )
        if not detached_frame:
            self._lost_since = None
            return False
        if self._lost_since is None:
            self._lost_since = now
        return (now - self._lost_since) >= self._config.lost_hold

    def _reset_state_locals(self) -> None:
        """销毁状态局部的计时器与连续帧计数。

        `captured` 连续确认刻意不在这里清零：它是跨状态的安全确认，结算框
        出现时圆环往往同时变得不可读，如果因 LAND/NET -> FIGHT 的迁移把
        计数清掉，就永远凑不够 2 帧。它只在"非 TRUE 帧"、start()、stop()
        时归零。
        """
        self._land_frames = 0
        self._land_since = None
        self._net_deadline = None
        self._net_retract_pending = False
        self._net_exit_frames = 0
        self._net_retract_attempted = False
        self._net_retract_wait_until = None
        self._net_window_reminded = False
        self._lost_since = None
        self._auto_net_blocked = False
        self._auto_net_blocked_reason = None

    # ==================== 不变量同步 ====================

    def _desired_for_state(self) -> set[str]:
        if self._state in (FightState.FIGHT, FightState.LAND):
            return {F5, F6}
        if self._state is FightState.NET:
            return {F6}
        return set()

    def _sync_desired(self, reason: str) -> bool:
        """声明当前状态的不变量并发送差集。失败只记录，不抛给 tick。

        无论集合是否与上次相同都会 reconcile：相同 desired 且 emitted 已
        一致时 arbiter 产生 0 事件；若有上次失败留下的差集则在此重试。
        """
        self._arbiter.set_desired(self._desired_for_state(), reason)
        try:
            self._arbiter.reconcile(reason)
            self._input_error = None
            return True
        except InputDispatchError as exc:
            self._input_error = str(exc)
            return False

    def _record(
        self,
        now: float,
        old: FightState,
        new: FightState,
        reason: str,
    ) -> None:
        obs = self._last_obs
        ring = None
        fish = None
        tier = None
        if obs is not None:
            ring = _valid_ring(obs.ring_ratio)
            fish = obs.fish_icon.value
            tier = obs.bar_tier
        self._events.append(
            FightEvent(
                time=now,
                from_state=old,
                to_state=new,
                reason=reason,
                desired_keys=tuple(sorted(self._arbiter.desired)),
                ring_ratio=ring,
                fish_icon=fish,
                bar_tier=tier,
            )
        )
