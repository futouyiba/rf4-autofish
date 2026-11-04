"""RF4 自动钓鱼 —— 纯 shadow 状态推断（阶段 1，零输入、零抓帧）。

输入只有 `autofish_observation.Observation`（同一帧的全部可读事实）加一个
**动作上下文**（`source`，例如 wait_bite / pull / lift），输出只有一份
`StateEstimate`。本模块不碰窗口、不抓帧、不派发键鼠事件，也不加载任何
注入/输入库；它只回答"画面看起来处于什么状态"，不回答"下一步按什么"。

推断哲学与观测层一致 —— **宁可 UNKNOWN，不许猜**：

1. 单帧只产生候选：`classify_candidate` 不保存任何状态；稳定状态由
   `ShadowStateTracker` 的连续帧计数决定。
2. **同样的像素在不同上下文里含义不同**（2026-11-04 真实日志）：
   * 等口/待抛竿时，背景里的黄色长条（体力条、竿身反光）也能被力度条
     读成 tier 2，此时 `bar tier + ring>=0.9` 不足以判 LAND；
   * 搏鱼 HUD 上的高光会在固定「确定」按钮框里画出两条亮行，纯 OK
     检测会假阳性 —— 上下文为 pull/lift 且搏鱼证据成立时直接屏蔽 MODAL。
   因此 LAND/FIGHT 的力度条证据只在 pull/lift 成立；等口/抛竿只有
   `fish_icon=TRUE`（真实咬钩）才可升 FIGHT，LAND 必须 fish + ring>=0.9。
3. 结算（SETTLE）的几何判据是新推断、样本少：即使连续两帧成立也只给中等
   confidence。`captured=TRUE` **只在 settle_before / settle_after**，或
   tracker 确认过 FIGHT/LAND 后的短捕获过渡窗口（且上下文仍是 pull/lift）
   才允许升级；wait_bite / cast_before / cast_after 的 captured 几何一律
   丢弃，避免"入护后残留 20 帧"把状态反复拉回 SETTLE。
4. **HUD 不可见 ≠ 菜单**：夜晚、侧光、面板遮挡都会让 `hud_visible=FALSE`。
   没有可靠的菜单模板，本模块永不推断 READY / MENU，只返回 UNKNOWN 并在
   reason 里说明需要动作上下文或模板。
5. `ring_ratio=None` 永远不参与 LAND：读不到圆环只说明图标没画出来。
6. `bar_level=0` 不是 1 档；`bar_tier=None` 时力度条不构成搏鱼证据。
7. 冷色 level 1（`tier=None`）不参与 FIGHT / 摩擦确认。

状态集合刻意小于设计文档：CASTING / NET 主要靠动作上下文，MENU 缺模板，
READY 缺可靠纯视觉证据 —— 这四类当前只可能是 UNKNOWN。
"""

from __future__ import annotations

import enum
from collections import deque
from dataclasses import dataclass

from autofish_observation import Observation, TriState

__all__ = [
    "GameState",
    "StateEstimate",
    "ShadowStateTracker",
    "classify_candidate",
    "normalize_context",
    "ACTIVE_CONTEXTS",
    "IDLE_CONTEXTS",
    "SETTLE_CONTEXTS",
    "LAND_RING_MIN",
    "CONFIRM_FRAMES",
    "CAPTURE_TRANSITION_FRAMES",
    "HISTORY_LIMIT",
]


# ==================== 模型 ====================


class GameState(enum.Enum):
    """shadow 可推断的状态。MENU / CASTING / NET 不在此列（见模块 docstring）。"""

    UNKNOWN = "unknown"
    READY = "ready"
    WAIT_BITE = "wait_bite"
    FIGHT = "fight"
    LAND = "land"
    SETTLE = "settle"
    MODAL = "modal"


@dataclass(frozen=True)
class StateEstimate:
    """一次 update 的结论。frozen：拿到就代表那一刻的快照。"""

    state: GameState
    confidence: float          # 0..1，shadow 证据强度，不是控制授权
    reason: str
    evidence: dict             # JSON 友好，仅用于日志解释
    changed: bool              # 本次 update 是否改变了稳定状态
    stable_frames: int         # 当前稳定状态被候选连续支持的帧数（pending 帧不计）


# ==================== 阈值 ====================

# 圆环点亮 >=90% 才算"到岸/收线完成"的唯一视觉证据。低于此值最多是 FIGHT。
LAND_RING_MIN = 0.90

# ---- 动作上下文（source）----
# 由适配层在**调用点**给出，不是从像素里猜的。未知/拼错的 source 归一化为 ""
# 并按最保守的 idle 规则处理（不认力度条、不认 captured）。
ACTIVE_CONTEXTS = frozenset({"pull", "lift"})
IDLE_CONTEXTS = frozenset({"wait_bite", "cast_before", "cast_after"})
SETTLE_CONTEXTS = frozenset({"settle_before", "settle_after"})
KNOWN_CONTEXTS = ACTIVE_CONTEXTS | IDLE_CONTEXTS | SETTLE_CONTEXTS

# FIGHT/LAND 确认后允许多少帧"捕获过渡"：settle 界面出现前 source 可能还是
# pull/lift。按默认 2Hz 采样，8 帧 ≈ 4 秒；超过后 captured 几何不再算证据。
CAPTURE_TRANSITION_FRAMES = 8

# 进入稳定状态所需的连续帧数。数值不是拍脑袋：SETTLE 判据证据少所以 2 帧且
# confidence 打折；LAND/WAIT_BITE 误判代价更高所以 3 帧；UNKNOWN 5 帧是为了
# 抵抗一瞬间的 HUD 遮挡/侧光。
CONFIRM_FRAMES = {
    GameState.UNKNOWN: 5,
    GameState.READY: 2,        # 占位：当前没有任何分支会产生 READY 候选
    GameState.WAIT_BITE: 3,
    GameState.FIGHT: 2,
    GameState.LAND: 3,
    GameState.SETTLE: 2,
    GameState.MODAL: 2,
}

# shadow 置信度上限：即使判据齐全，也不宣称"确定"。SETTLE 最低，因为几何
# 判据与模板不同源；WAIT_BITE 只是"HUD 在、没鱼"，信息量本来就少。
CONFIDENCE_CAP = {
    GameState.UNKNOWN: 0.0,
    GameState.READY: 0.40,
    GameState.WAIT_BITE: 0.55,
    GameState.FIGHT: 0.80,
    GameState.LAND: 0.75,
    GameState.SETTLE: 0.70,
    GameState.MODAL: 0.90,
}

HISTORY_LIMIT = 120


def normalize_context(context: object) -> str:
    """把 source 归一化成已知上下文；未知/非字符串一律 ""（最保守）。"""
    if not isinstance(context, str):
        return ""
    ctx = context.strip().lower()
    return ctx if ctx in KNOWN_CONTEXTS else ""


# ==================== 单帧候选 ====================


def classify_candidate(
    obs: Observation, context: str = ""
) -> tuple[GameState, str]:
    """把一帧事实 + 动作上下文映射成**候选**状态，返回 (状态, 理由)。

    本函数不记忆稳定状态；"FIGHT/LAND 之后还能认 captured"的过渡窗口由
    `ShadowStateTracker` 依据 stable 状态补上（见模块 docstring §3）。

    优先级：

    1. `ok_modal=TRUE` -> MODAL（**pull/lift 中直接屏蔽**：搏鱼 HUD 的
       高光会让旧版上下边框判据假阳性；context 抑制写进 reason）。
    2. `captured=TRUE` 且 context in settle_* -> SETTLE；
       其他 context 的 captured 几何不参与，继续往下走。
    3. `fish_icon=TRUE`（任何非结算上下文）：
       * ring 可读且 >= LAND_RING_MIN -> LAND
       * 否则 -> FIGHT（ring=None 仍可 FIGHT，但绝不 LAND）
    4. `bar_tier in 1..3` **且 context in {pull,lift}** -> FIGHT。
       等口/抛竿的力度条带可能只是体力条/竿身反光，单独不足（真实日志 A）。
    5. idle：`hud_visible=TRUE` 且 `fish_icon=FALSE`：
       * ring 可读 或 `bar_level==0` -> WAIT_BITE
       （此时即使 bar_tier 有值也按"忽略力度条证据"处理）
    6. 其余 -> UNKNOWN（含 READY：纯视觉没有可靠证据）

    注意：本函数不防抖，单帧 TRUE 绝不能被当成结论。
    """
    ctx = normalize_context(context)

    if obs.ok_modal is TriState.TRUE:
        if ctx in ACTIVE_CONTEXTS:
            # 不返回候选；继续按搏鱼证据判定，reason 里留下抑制痕迹。
            modal_suppressed = True
        else:
            return GameState.MODAL, f"ok_modal_true@{ctx or 'no_ctx'}"

    else:
        modal_suppressed = False

    if obs.captured is TriState.TRUE and ctx in SETTLE_CONTEXTS:
        return GameState.SETTLE, f"captured_true@{ctx}"

    fish = obs.fish_icon is TriState.TRUE
    tier = obs.bar_tier
    ring = obs.ring_ratio

    if fish:
        if ring is not None and ring >= LAND_RING_MIN:
            return GameState.LAND, f"fish_true+ring_full({ring:.3f})"
        return GameState.FIGHT, "fish_icon_true"

    if ctx in ACTIVE_CONTEXTS and tier in (1, 2, 3):
        return GameState.FIGHT, f"bar_tier_{tier}@{ctx}"

    if (ctx not in ACTIVE_CONTEXTS
            and obs.hud_visible is TriState.TRUE
            and obs.fish_icon is TriState.FALSE):
        if ring is not None or obs.bar_level == 0:
            if tier is not None:
                return GameState.WAIT_BITE, (
                    f"hud_idle_ring_readable(bar_tier_{tier}_ignored"
                    f"@{ctx or 'no_ctx'})")
            return GameState.WAIT_BITE, "hud_idle_ring_readable"

    if modal_suppressed:
        # pull/lift 里既没鱼也没档：保持 UNKNOWN，绝不因为 OK 边框就切 MODAL。
        return GameState.UNKNOWN, f"ok_modal_suppressed@{ctx}"

    if obs.captured is TriState.TRUE:
        # 等口/抛竿里的 captured 几何不作数（真实日志 C：入护后残留 20 帧）。
        return GameState.UNKNOWN, f"captured_ignored@{ctx or 'no_ctx'}"

    # 没有可靠纯视觉证据。READY 缺"岸边/待抛竿"模板；MENU 缺菜单模板；
    # 夜晚 hud_visible=FALSE 也不足以判菜单。都必须交给动作上下文。
    if obs.hud_visible is TriState.FALSE:
        return GameState.UNKNOWN, "hud_absent_needs_action_context_or_template"
    return GameState.UNKNOWN, "no_visual_state_needs_action_context_or_template"


# ==================== 防抖跟踪器 ====================


class ShadowStateTracker:
    """连续帧防抖的 shadow 状态机。

    只读、纯内存、单线程使用。`update()` 每次吃一条 `Observation` 和调用点
    的 `context`（source），返回当前稳定状态；候选帧数不够时保持旧状态，并在
    reason/evidence 里写明 pending。

    这里额外承担两条**依赖稳定状态**的 guard（纯 classifier 不做隐式记忆）：

    * pull/lift 中 `captured=TRUE` 只有在当前稳定状态是 FIGHT/LAND，或刚
      离开 FIGHT/LAND 的 `CAPTURE_TRANSITION_FRAMES` 帧内，才升级为 SETTLE；
    * 其余 context 的 captured 已被 classifier 丢弃，不会反复拉回 SETTLE。
    """

    def __init__(self) -> None:
        self._history: deque[Observation] = deque(maxlen=HISTORY_LIMIT)
        self._stable = GameState.UNKNOWN
        self._stable_frames = 0
        self._cand = GameState.UNKNOWN
        self._cand_frames = 0
        # 距离上一次稳定在 FIGHT/LAND 过了多少帧；None = 从未进入过。
        self._frames_since_active: int | None = None

    @property
    def state(self) -> GameState:
        """当前稳定状态（不触发推断）。"""
        return self._stable

    @property
    def observations(self) -> tuple[Observation, ...]:
        """最近吃进来的观测（只读快照，供日志/复盘）。"""
        return tuple(self._history)

    def _in_capture_transition(self) -> bool:
        """当前是否处在 FIGHT/LAND 之后的捕获过渡窗口内。"""
        if self._stable in (GameState.FIGHT, GameState.LAND):
            return True
        return (self._frames_since_active is not None
                and self._frames_since_active <= CAPTURE_TRANSITION_FRAMES)

    def update(self, obs: Observation, context: str = "") -> StateEstimate:
        """吃一帧（带动作上下文），返回估计。状态只在候选连续够帧时才改变。"""
        self._history.append(obs)
        ctx = normalize_context(context)

        cand, cand_reason = classify_candidate(obs, ctx)

        # 依赖 stable 的过渡规则：settle 界面出现时 source 可能还停在 pull/lift。
        # 只对 ACTIVE 上下文开放；wait_bite/cast 永远不会把 captured 升级成 SETTLE。
        if (cand is not GameState.SETTLE
                and obs.captured is TriState.TRUE
                and ctx in ACTIVE_CONTEXTS
                and self._in_capture_transition()):
            cand = GameState.SETTLE
            cand_reason = (
                f"captured_true_in_capture_transition"
                f"(after_{self._stable.value}@{ctx})")

        if cand is self._cand:
            self._cand_frames += 1
        else:
            self._cand = cand
            self._cand_frames = 1

        need = CONFIRM_FRAMES[cand]
        changed = False
        if cand is self._stable:
            self._stable_frames += 1
        elif self._cand_frames >= need:
            # MODAL/SETTLE 的 need=2，因此它们一旦满帧就能打断任意旧状态；
            # 这正是"紧急高优先级"的落点，不需要额外分支。
            self._stable = cand
            self._stable_frames = self._cand_frames
            changed = True
        # else: 候选还没攒够帧。稳定结论仍然成立（所以不改 state、不归零），
        # 但它没有获得这一帧的支持，stable_frames 保持不变 —— 该字段的语义是
        # "当前稳定状态被候选连续支持的帧数"，pending 的其他候选不算支持。

        # 过渡窗口只在"刚离开 FIGHT/LAND"时继续计数，其他时候冻结在 0/None。
        if self._stable in (GameState.FIGHT, GameState.LAND):
            self._frames_since_active = 0
        elif self._frames_since_active is not None:
            self._frames_since_active += 1

        pending = None
        if not changed and cand is not self._stable:
            pending = {
                "state": cand.value,
                "frames": self._cand_frames,
                "need": need,
            }

        if changed:
            reason = (f"{self._stable.value}_confirmed"
                      f"({self._cand_frames}/{need}, {cand_reason})")
        elif pending is not None:
            reason = (f"hold {self._stable.value}: pending {cand.value} "
                      f"{self._cand_frames}/{need} ({cand_reason})")
        else:
            reason = f"hold {self._stable.value}: {cand_reason}"

        cap = CONFIDENCE_CAP[self._stable]
        if self._stable is cand:
            ratio = min(1.0, self._cand_frames / need)
        elif self._stable is GameState.UNKNOWN:
            ratio = 0.0
        else:
            # 旧状态正在被挑战，但还没攒够帧：保留结论、下调置信度。
            ratio = 0.5

        return StateEstimate(
            state=self._stable,
            confidence=round(cap * ratio, 3),
            reason=reason,
            evidence={
                "candidate": cand.value,
                "candidate_reason": cand_reason,
                "candidate_frames": self._cand_frames,
                "need_frames": need,
                "pending": pending,
                "context": ctx,
                "source": context if isinstance(context, str) else "",
                "frames_since_active": self._frames_since_active,
                "observation": {
                    "fish_icon": obs.fish_icon.value,
                    "bar_level": obs.bar_level,
                    "bar_tier": obs.bar_tier,
                    "ring_ratio": (None if obs.ring_ratio is None
                                   else round(obs.ring_ratio, 3)),
                    "captured": obs.captured.value,
                    "ok_modal": obs.ok_modal.value,
                    "hud_visible": obs.hud_visible.value,
                },
            },
            changed=changed,
            stable_frames=self._stable_frames,
        )
