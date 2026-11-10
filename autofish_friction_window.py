"""摩擦片升/降判据（纯逻辑，无 I/O、无时钟读取）。

这个模块只回答一个问题：**当前这一帧力度条读数，是否应该升档或降档？**
它不滚轮、不抓图、不读系统时间；调用方把统一时钟的 ``now`` 传进来。
适配器、bridge 和离线测试共用同一套判据，避免两处实现各说各话。

## 为什么不能把窗口起点设成 ``win[0][0]``

``win`` 会被裁剪成最近 10 秒。如果把观察起点取成窗口首帧：

* 窗口满 10 秒后首帧永远只比 ``now`` 早不到 10 秒；
* ``now - win[0][0] >= 10`` 永远不成立，首次升档永远不会触发。

所以观察起点必须是**独立的持久纪元** ``observe_since``：第一次真实读到
``level > 0`` 时设置，之后只在下述情况重置，绝不跟随滑动窗口首项移动。

## 规则口径（2026-11-05 修复）

* 第一次有效观察：连续 ``first_observe``（默认 10s）且窗口内真实读数
  全部为 1 档，才允许第一次升档。**校准时刻不参与计时**——校准可能发生在
  600 秒前，不能因此让中鱼后 2 秒就升档。
* 之后的升档：距上次成功升档 ``rise_gap``（5s）；距上次成功降档
  ``recover_gap``（10s，红事件后谨慎恢复）。
* 降档仍是紧急事件：最近 3 帧里 ≥2 帧红就降，**不等 10 秒**；两次降档
  之间保持 ``drop_gap``（6s）冷却。
* 当前帧必须真实为 1 档（``level > 0`` 且 tier==1）。``level == 0``
  只表示“这一瞬间没读到/鱼在歇”，不能证明负载低，也不能触发升档。
* 有效样本之间间隔超过 ``max_sample_gap``（默认 1.8s）视为观察中断：
  丢弃旧样本，从当前帧重新累计，避免“3 帧低 → 等 10 秒没数据 → 升档”。
* 出现 2 档（橙）或 3 档（红）时不允许升档；2 档会一直阻断到它从最近
  10 秒窗口里被淘汰。
* 只有调用方**实际成功**完成滚轮后才调用 :func:`commit`；失败不要推进
  纪元或记录 last_action/last_drop。

时钟约定：所有时间都必须是同一个单调递增的墙钟（当前适配器用
``time.time()``）。**不要在这里混入 ``time.monotonic``**。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import MutableSequence

TIER_RED = 3
TIER_TARGET = 2
TIER_LOW = 1

ACTION_RISE = "rise"
ACTION_DROP = "drop"

Sample = tuple[float, int]


def tier(level: int) -> int:
    """四色等级 -> 玩家三档口径；与适配器 ``_tier`` 保持一致。"""
    if level >= 4:
        return TIER_RED
    if level == 3:
        return TIER_TARGET
    return TIER_LOW


@dataclass(frozen=True)
class FrictionWindowConfig:
    """判据参数。比例、29 上限等调整量不在这里，仍由适配器负责。"""

    window: float = 10.0
    first_observe: float = 10.0
    rise_gap: float = 5.0
    recover_gap: float = 10.0
    drop_gap: float = 6.0
    lock: float = 0.8
    min_reads: int = 3
    max_sample_gap: float = 1.8
    drop_tail: int = 3
    drop_tail_hits: int = 2

    def __post_init__(self) -> None:
        if self.window <= 0:
            raise ValueError("window must be > 0")
        if self.first_observe < 0 or self.rise_gap < 0 or self.recover_gap < 0:
            raise ValueError("observation gaps must be >= 0")
        if self.drop_gap < 0 or self.lock < 0:
            raise ValueError("drop_gap/lock must be >= 0")
        if self.min_reads < 1:
            raise ValueError("min_reads must be >= 1")
        if self.max_sample_gap <= 0:
            raise ValueError("max_sample_gap must be > 0")
        if self.drop_tail < 1 or not (1 <= self.drop_tail_hits <= self.drop_tail):
            raise ValueError("invalid drop tail configuration")


@dataclass
class FrictionObservationState:
    """跨帧持久状态；样本窗口本身由调用方持有（adapter 的 win / bridge 的列表）。"""

    observe_since: float | None = None
    last_valid_ts: float | None = None
    last_action: str | None = None
    last_adjust: float | None = None
    last_drop: float | None = None

    def reset(self, *, keep_last_action: bool = True) -> None:
        """清观察纪元与连续观察状态。

        * ``keep_last_action=True``：暂停/冻结（``friction_step(None)``）用。
          保留最后调整类型，恢复后仍按 rise 5s / drop 10s 等待。
        * ``keep_last_action=False``：新鱼、校准成功、跨会话用。连
          last_action / last_drop 一起清掉。
        """
        self.observe_since = None
        self.last_valid_ts = None
        if not keep_last_action:
            self.last_action = None
            self.last_adjust = None
            self.last_drop = None


@dataclass(frozen=True)
class FrictionDecision:
    action: str
    reason: str
    reads: int
    observed: float
    tail: tuple[int, ...] = ()


def _prune(samples: MutableSequence[Sample], now: float, window: float) -> None:
    while samples and now - samples[0][0] > window:
        samples.pop(0)


def _required_gap(state: FrictionObservationState, cfg: FrictionWindowConfig) -> float:
    if state.last_action is None:
        return cfg.first_observe
    if state.last_action == ACTION_DROP:
        return cfg.recover_gap
    return cfg.rise_gap


def evaluate(
    samples: MutableSequence[Sample],
    state: FrictionObservationState,
    now: float,
    cfg: FrictionWindowConfig | None = None,
) -> FrictionDecision | None:
    """评估当前窗口，返回决策或 ``None``。

    调用方应先把当前帧 ``(now, level)`` 追加到 ``samples``。本函数会原地
    裁剪窗口；观察中断时也会原地丢弃旧样本（只保留当前帧）。
    """
    cfg = cfg or FrictionWindowConfig()
    _prune(samples, now, cfg.window)
    if not samples:
        return None

    cur_t, cur_level = samples[-1]
    # 当前帧不是真实读数：不升、不降、不建立观察纪元，也不更新连续观察时间。
    if cur_level <= 0:
        return None

    # ---- 连续观察：长空档/缺帧把旧观察作废 ----
    if (
        state.last_valid_ts is not None
        and cur_t - state.last_valid_ts > cfg.max_sample_gap
    ):
        samples[:] = [(cur_t, cur_level)]
        state.observe_since = cur_t
    state.last_valid_ts = cur_t
    if state.observe_since is None:
        state.observe_since = cur_t

    # 刚滚过轮，先等生效。last_adjust 只在成功 commit 后更新。
    if state.last_adjust is not None and now - state.last_adjust < cfg.lock:
        return None

    # ---- 降档：红事件只看尾部，保持紧急，不等 10 秒 ----
    tail = tuple(tier(level) for _, level in samples[-cfg.drop_tail:])
    if (
        tail.count(TIER_RED) >= cfg.drop_tail_hits
        and (state.last_drop is None or now - state.last_drop >= cfg.drop_gap)
    ):
        return FrictionDecision(
            ACTION_DROP,
            "red_tail",
            reads=sum(1 for _, level in samples if level > 0),
            observed=max(0.0, now - state.observe_since),
            tail=tail,
        )

    # ---- 升档：只统计 observation epoch 之后的真实低档读数 ----
    since_epoch = max(0.0, now - state.observe_since)
    valid = [
        (t, level)
        for t, level in samples
        if level > 0 and t >= state.observe_since - 1e-9
    ]
    if not valid:
        return None
    valid_tiers = [tier(level) for _, level in valid]
    # 橙（目标）阻断到它被最近窗口淘汰；红也先交给降档路径处理。
    if any(value != TIER_LOW for value in valid_tiers):
        return None

    reads = len(valid)
    if reads < cfg.min_reads:
        return None
    if since_epoch < _required_gap(state, cfg):
        return None

    return FrictionDecision(
        ACTION_RISE,
        "continuous_low",
        reads=reads,
        observed=since_epoch,
        tail=tail,
    )


def commit(
    state: FrictionObservationState,
    decision: FrictionDecision,
    now: float,
) -> None:
    """只在滚轮**实际成功**后调用：推进 last_action / last_drop / 新纪元。"""
    if decision.action not in (ACTION_RISE, ACTION_DROP):
        raise ValueError("unknown friction action: %r" % (decision.action,))
    state.last_action = decision.action
    state.last_adjust = now
    if decision.action == ACTION_DROP:
        state.last_drop = now
    # 新纪元从成功调整的这一刻开始；旧样本由调用方清空。
    state.observe_since = now
    state.last_valid_ts = None
