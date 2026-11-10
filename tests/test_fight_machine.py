"""FightMachine 的纯离线测试：合成 Observation + fake clock，无截图/无输入库。

运行：
    python -m unittest discover -s tests -p "test_fight_machine.py" -v
"""

from __future__ import annotations

import math
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import autofish_fight_machine as afm  # noqa: E402
import autofish_input as ain  # noqa: E402
import autofish_observation as aobs  # noqa: E402

T = aobs.TriState
S = afm.FightState
F5 = ain.F5
F6 = ain.F6


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.now

    def advance(self, dt: float) -> None:
        # 取整到纳秒量级，避免 4.006/12.006 这类差值出现 7.999999 的假边界。
        self.now = round(self.now + dt, 9)


class FakeSink:
    def __init__(self):
        self.calls: list[tuple[str, bool, str]] = []
        self.fail = None

    def __call__(self, key, is_down, reason):
        if self.fail is not None and self.fail(key, is_down):
            raise RuntimeError(f"boom:{key}:{'down' if is_down else 'up'}")
        self.calls.append((key, is_down, reason))

    @property
    def downs(self):
        return [k for k, d, _ in self.calls if d]

    @property
    def ups(self):
        return [k for k, d, _ in self.calls if not d]


def mk(
    ts: float,
    *,
    fish=T.FALSE,
    bar=2,
    ring=None,
    captured=T.FALSE,
    foreground=True,
    hud=T.TRUE,
    ok=T.FALSE,
) -> aobs.Observation:
    """合成观测。默认 bar=2，避免测试无意中触发脱钩。"""
    return aobs.Observation(
        monotonic_ts=ts,
        wall_ts=ts,
        foreground=foreground,
        fish_icon=fish,
        bar_level=bar,
        bar_tier=None,
        bar_hue=None,
        bar_sat=None,
        ring_ratio=ring,
        captured=captured,
        ok_modal=ok,
        hud_visible=hud,
        evidence={},
    )


# 每帧至少推进一点点 clock，保证相邻帧的 monotonic_ts 严格递增。
FRAME_STEP = 0.001


class MachineCase(unittest.TestCase):
    def build(self, net_action="default", net_retract_action="default", **cfg):
        self.sink = FakeSink()
        self.arb = ain.InputArbiter(self.sink)
        self.clock = FakeClock()
        self.net_calls = 0
        self.net_retract_calls = 0
        if net_action == "default":
            def net():
                self.net_calls += 1
        else:
            net = net_action
        if net_retract_action == "default":
            def retract():
                self.net_retract_calls += 1
        else:
            retract = net_retract_action
        # 底层默认已改为 False（生产安全）；历史 NET 语义测试必须显式打开，
        # 关闭路径由 TestAutoNetGate 单独覆盖。
        cfg.setdefault("auto_net_enabled", True)
        self.machine = afm.FightMachine(
            self.arb,
            clock=self.clock,
            net_action=net,
            net_retract_action=retract,
            config=afm.FightConfig(**cfg),
        )
        self.ts = 0.0
        return self.machine

    def start(self, net_state=afm.NetState.STOWED):
        """大多数测试模拟正常新会话；中途接管用例显式传 UNKNOWN。"""
        return self.machine.start(net_state=net_state)

    def tick(
        self,
        dt=0.0,
        *,
        fish=T.FALSE,
        bar=2,
        ring=None,
        captured=T.FALSE,
        foreground=True,
        hud=T.TRUE,
        ok=T.FALSE,
    ):
        """推进 fake clock，并按不超过 1s 的连续帧喂观测。

        观测时间戳与 clock 落在同一条 monotonic 轴；大跨度 dt 会被拆成多帧，
        避免测试自己制造超过 max_frame_gap 的稀疏帧（那会改变语义）。
        """
        remaining = float(dt)
        state = self.machine.state
        while True:
            step = min(remaining, 1.0)
            self.clock.advance(step)
            if self.clock.now <= self.ts:
                self.clock.advance(FRAME_STEP)
            self.ts = self.clock.now
            state = self.machine.tick(
                mk(self.ts, fish=fish, bar=bar, ring=ring, captured=captured,
                   foreground=foreground, hud=hud, ok=ok)
            )
            remaining -= step
            if remaining <= 0.0:
                return state

    def frame(
        self,
        ts,
        *,
        fish=T.FALSE,
        bar=2,
        ring=None,
        captured=T.FALSE,
        foreground=True,
        hud=T.TRUE,
        ok=T.FALSE,
    ):
        """按显式帧时间戳喂一帧；fake clock 同步且不回退到该时间戳。"""
        if ts > self.clock.now:
            self.clock.now = ts
        self.ts = ts
        return self.machine.tick(
            mk(ts, fish=fish, bar=bar, ring=ring, captured=captured,
               foreground=foreground, hud=hud, ok=ok)
        )

    def observe(
        self,
        ts,
        *,
        fish=T.FALSE,
        bar=2,
        ring=None,
        captured=T.FALSE,
        foreground=True,
        hud=T.TRUE,
        ok=T.FALSE,
    ):
        """不改 fake clock，直接喂一个可能过时/未来的观测。"""
        return self.machine.tick(
            mk(ts, fish=fish, bar=bar, ring=ring, captured=captured,
               foreground=foreground, hud=hud, ok=ok)
        )

    def enter_land(self, *, ring=0.985, fish=T.TRUE, bar=2):
        for _ in range(3):
            self.tick(0.0, fish=fish, bar=bar, ring=ring)
        self.assertIs(self.machine.state, S.LAND)
        return self.machine.state

    def enter_net(self):
        self.enter_land()
        self.tick(8.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.NET)
        return self.machine.state


class TestLifecycle(MachineCase):
    def test_constructor_has_no_side_effects(self):
        self.build()
        self.assertEqual(self.sink.calls, [])
        self.assertEqual(self.clock.calls, 0)
        self.assertIs(self.machine.state, S.STOPPED)
        self.assertFalse(self.machine.control_allowed)

    def test_start_declares_f5_f6_but_waits_for_first_fresh_obs(self):
        self.build()
        self.start()
        self.assertIs(self.machine.state, S.FIGHT)
        self.assertEqual(self.sink.downs, [F5, F6])
        self.assertEqual(self.arb.desired, frozenset({F5, F6}))
        # start 是显式动作，但没有观测就不得授权摩擦控制。
        self.assertFalse(self.machine.control_allowed)

        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.50)
        self.assertTrue(self.machine.control_allowed)

    def test_start_is_idempotent_while_running(self):
        self.build()
        self.start()
        before = len(self.sink.calls)
        self.start()
        self.assertEqual(len(self.sink.calls), before)
        self.assertEqual(len(self.machine.events), 1)

    def test_tick_before_start_is_inert(self):
        self.build()
        for _ in range(5):
            self.tick(0.0, fish=T.TRUE, ring=0.99)
        self.assertEqual(self.sink.calls, [])
        self.assertIs(self.machine.state, S.STOPPED)

    def test_stop_releases_all_and_is_idempotent(self):
        self.build()
        self.start()
        self.machine.stop()
        self.assertIs(self.machine.state, S.STOPPED)
        self.assertEqual(self.sink.ups, [F5, F6])
        self.assertEqual(self.arb.emitted, frozenset())

        before = len(self.sink.calls)
        self.machine.stop()
        self.assertEqual(len(self.sink.calls), before)
        self.assertEqual(self.sink.ups, [F5, F6])

    def test_tick_after_stop_never_inputs(self):
        self.build()
        self.start()
        self.machine.stop()
        before = len(self.sink.calls)
        for _ in range(10):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.99)
        self.assertEqual(len(self.sink.calls), before)
        self.assertIs(self.machine.state, S.STOPPED)

    def test_stopped_tick_does_not_retry_release_but_stop_does(self):
        """STOPPED 保持零输入；未完成的释放只能由显式 stop() 重试。"""
        self.build()
        self.start()
        self.sink.fail = lambda key, is_down: not is_down
        with self.assertRaises(ain.InputDispatchError):
            self.machine.stop()
        self.assertIs(self.machine.state, S.STOPPED)
        self.assertEqual(self.arb.owned, frozenset({F5, F6}))

        before = len(self.sink.calls)
        for _ in range(5):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.99)
        self.assertEqual(len(self.sink.calls), before)
        self.assertEqual(self.arb.owned, frozenset({F5, F6}))

        self.sink.fail = None
        self.machine.stop()
        self.assertEqual(self.arb.owned, frozenset())

    def test_control_allowed_only_in_fight(self):
        self.build()
        self.assertFalse(self.machine.control_allowed)   # STOPPED
        self.start()
        self.assertFalse(self.machine.control_allowed)   # 尚无新鲜观测
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.50)
        self.assertTrue(self.machine.control_allowed)
        self.enter_land()
        self.assertFalse(self.machine.control_allowed)
        self.tick(8.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.NET)
        self.assertFalse(self.machine.control_allowed)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985, captured=T.TRUE)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985, captured=T.TRUE)
        self.assertIs(self.machine.state, S.SETTLE)
        self.assertFalse(self.machine.control_allowed)

    def test_control_allowed_requires_complete_f5_f6_both_ways(self):
        self.build()
        self.start()
        self.assertFalse(self.machine.control_allowed)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.50)
        self.assertTrue(self.machine.control_allowed)

        # arbiter 的 desired 被外部改动：即使仍在 FIGHT 也不得授权。
        self.arb.set_desired({F5}, "external")
        self.assertFalse(self.machine.control_allowed)
        self.arb.set_desired({F5, F6}, "external")

        # emitted 与 desired 不一致同样不得授权（这里多按了一个键）。
        self.arb.set_desired({F5, F6, "f7"}, "external")
        self.assertFalse(self.machine.control_allowed)

    def test_start_partial_failure_does_not_allow_friction(self):
        """start 只按下了 F5 就失败：FIGHT 但 control_allowed 必须为 False。"""
        self.build()
        self.sink.fail = lambda key, is_down: key == F6 and is_down
        with self.assertRaises(ain.InputDispatchError):
            self.start()

        self.assertIs(self.machine.state, S.FIGHT)
        self.assertIsNotNone(self.machine.input_error)
        self.assertEqual(self.arb.emitted, frozenset({F5}))
        self.assertEqual(self.arb.desired, frozenset({F5, F6}))
        self.assertFalse(self.machine.control_allowed)

        # 恢复后下一次 tick 完成差集，才重新授权。
        self.sink.fail = None
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.50)
        self.assertIsNone(self.machine.input_error)
        self.assertTrue(self.machine.control_allowed)

    def test_start_failure_before_any_press_does_not_allow_friction(self):
        self.build()
        self.sink.fail = lambda key, is_down: key == F5 and is_down
        with self.assertRaises(ain.InputDispatchError):
            self.start()
        self.assertEqual(self.arb.emitted, frozenset())
        self.assertFalse(self.machine.control_allowed)


class TestFightToLand(MachineCase):
    def test_default_ring_min_is_098(self):
        self.assertEqual(afm.FightConfig().ring_min, 0.98)
        self.assertEqual(afm.FightConfig().net_exit_ring, 0.90)

    def test_0973_below_default_never_enters_land(self):
        """默认 0.98：0.973 连续多帧也仍是 FIGHT。"""
        self.build()
        self.start()
        for _ in range(6):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.973)
        self.assertIs(self.machine.state, S.FIGHT)
        self.assertEqual(self.sink.downs, [F5, F6])
        self.assertEqual(self.sink.ups, [])

    def test_0985_three_frames_enter_land(self):
        self.build()
        self.start()
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.FIGHT)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)

    def test_three_near_frames_enter_land(self):
        self.build()
        self.start()
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.FIGHT)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)
        # FIGHT -> LAND 不松键、不按键。
        self.assertEqual(self.sink.downs, [F5, F6])
        self.assertEqual(self.sink.ups, [])

    def test_near_must_be_consecutive(self):
        self.build()
        self.start()
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.40)   # 打断
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.FIGHT)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)

    def test_fish_must_be_true(self):
        self.build()
        self.start()
        for _ in range(6):
            self.tick(0.0, fish=T.FALSE, bar=2, ring=0.99)
        self.assertIs(self.machine.state, S.FIGHT)

    def test_unknown_ring_never_lands(self):
        self.build()
        self.start()
        for _ in range(20):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=None)
        self.assertIs(self.machine.state, S.FIGHT)

    def test_non_finite_and_out_of_range_ring_never_lands(self):
        self.build()
        self.start()
        for bad in (math.nan, math.inf, -math.inf, -0.01, 1.01):
            for _ in range(4):
                self.tick(0.0, fish=T.TRUE, bar=2, ring=bad)
            self.assertIs(self.machine.state, S.FIGHT)

    def test_duplicate_frame_does_not_accumulate(self):
        self.build()
        self.start()
        same = mk(5.0, fish=T.TRUE, bar=2, ring=0.985)
        self.frame(5.0, fish=T.TRUE, bar=2, ring=0.985)
        for _ in range(2):
            self.machine.tick(same)      # 重复帧：整帧忽略
        self.assertIs(self.machine.state, S.FIGHT)
        # ts=5 那一帧只算 1 次，后面再补 2 个独立帧。
        self.frame(6.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.FIGHT)
        self.frame(6.5, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)

    def test_out_of_order_frame_is_ignored(self):
        self.build()
        self.start()
        self.frame(10.0, fish=T.TRUE, bar=2, ring=0.985)
        self.frame(9.0, fish=T.TRUE, bar=2, ring=0.985)
        self.frame(10.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.FIGHT)
        self.frame(11.0, fish=T.TRUE, bar=2, ring=0.985)
        self.frame(12.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)

    def test_sync_failure_aborts_tick_before_state_migration(self):
        """按键同步失败时，本 tick 不得继续累计 near 或迁移状态。"""
        self.build()
        self.sink.fail = lambda key, is_down: key == F6 and is_down
        with self.assertRaises(ain.InputDispatchError):
            self.start()
        self.assertIs(self.machine.state, S.FIGHT)

        for _ in range(6):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        # 每一帧都因同步失败提前返回：near 帧数没有累积。
        self.assertIs(self.machine.state, S.FIGHT)
        self.assertEqual(self.net_calls, 0)
        self.assertIsNotNone(self.machine.input_error)

        self.sink.fail = None
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)   # 仅完成同步
        self.assertIs(self.machine.state, S.FIGHT)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)


class TestLandAndNet(MachineCase):
    """NET 是新会话：掏网一次、低环连续帧自动收网、再回 FIGHT。"""

    def test_net_waits_near_grace_and_deploys_once(self):
        self.build()
        self.start()
        self.enter_land()
        self.assertEqual(self.net_calls, 0)
        self.assertEqual(self.machine.net_state, afm.NetState.STOWED)
        self.assertEqual(self.machine.net_state_basis, "assumed_new_session")

        self.tick(7.9, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)
        self.assertEqual(self.net_calls, 0)

        self.tick(0.1, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.net_calls, 1)
        self.assertEqual(self.machine.net_state, afm.NetState.DEPLOYED)
        self.assertEqual(self.machine.net_state_basis, "commanded_deploy")

    def test_net_entry_releases_only_f5(self):
        self.build()
        self.start()
        self.enter_land()
        before = len(self.sink.calls)
        self.tick(8.0, fish=T.TRUE, bar=2, ring=0.985)
        new_calls = self.sink.calls[before:]
        self.assertEqual(new_calls, [(F5, False, "net")])
        self.assertEqual(self.arb.owned, frozenset({F6}))

    def test_low_ring_three_frames_retracts_then_restores_f5_after_anim(self):
        self.build()
        self.start()
        self.enter_net()
        self.assertEqual(self.net_retract_calls, 0)

        # 前两帧只累计，第三帧才发收网切换。
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.net_retract_calls, 0)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.net_retract_calls, 1)
        # 收网命令发出后仍只按 F6，绝不提前恢复 F5。
        self.assertEqual(self.arb.owned, frozenset({F6}))
        self.assertEqual(self.sink.ups, [F5])
        self.assertTrue(self.machine.net_retract_pending)

        # 1.2s 动画等待期内不恢复 F5；到期后回 FIGHT 并补按 F5。
        self.tick(1.1, fish=T.TRUE, bar=2, ring=0.85)
        self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.arb.owned, frozenset({F6}))
        self.tick(0.1, fish=T.TRUE, bar=2, ring=0.85)
        self.assertIs(self.machine.state, S.FIGHT)
        self.assertEqual(self.arb.owned, frozenset({F5, F6}))
        self.assertEqual(self.machine.net_state, afm.NetState.STOWED)
        self.assertEqual(self.machine.net_state_basis, "commanded_retract")
        self.assertEqual(self.net_retract_calls, 1)

    def test_slow_retract_callback_still_waits_full_anim_after_return(self):
        """回调耗时 2s：动画等待从回调返回的 clock 起算，仍完整 1.2s。

        回归点：旧实现用回调前的 tick 时刻算 wait_until，慢回调返回时该
        截止时刻已过期，下一帧立刻补 F5，动画等待被吞掉。
        """
        def slow_retract():
            self.net_retract_calls += 1
            self.clock.advance(2.0)  # 模拟切前台/注入耗时 2s

        self.build(net_retract_action=slow_retract)
        self.start()
        self.enter_net()
        for _ in range(3):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.assertEqual(self.net_retract_calls, 1)
        self.assertIs(self.machine.state, S.NET)
        # 回调返回时的 clock 已经比决策时刻晚 2s；等待窗口从这里重新起算。
        returned = self.clock.now
        self.assertTrue(self.machine.net_retract_pending)
        self.assertEqual(self.arb.owned, frozenset({F6}))

        self.frame(returned + 1.1, fish=T.TRUE, bar=2, ring=0.85)
        self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.arb.owned, frozenset({F6}))
        self.assertEqual(self.sink.ups, [F5])

        self.frame(returned + 1.19, fish=T.TRUE, bar=2, ring=0.85)
        self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.arb.owned, frozenset({F6}))

        self.frame(returned + 1.2, fish=T.TRUE, bar=2, ring=0.85)
        self.assertIs(self.machine.state, S.FIGHT)
        self.assertEqual(self.arb.owned, frozenset({F5, F6}))
        self.assertEqual(self.machine.net_state, afm.NetState.STOWED)
        self.assertEqual(self.machine.net_state_basis, "commanded_retract")
        self.assertEqual(self.net_retract_calls, 1)

    def test_default_net_exit_089_retracts_091_does_not(self):
        """98 入 / 90 出：默认 NET 退出阈值独立为 0.90。"""
        self.build()
        self.start()
        self.enter_net()
        for _ in range(3):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.91)
        self.assertEqual(self.net_retract_calls, 0)
        for _ in range(3):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.89)
        self.assertEqual(self.net_retract_calls, 1)

    def test_low_ring_065_also_retracts(self):
        self.build()
        self.start()
        self.enter_net()
        for _ in range(3):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.65)
        self.assertEqual(self.net_retract_calls, 1)
        self.tick(1.2, fish=T.TRUE, bar=2, ring=0.65)
        self.assertIs(self.machine.state, S.FIGHT)

    def test_net_exit_ring_is_configurable(self):
        self.build(net_exit_ring=0.70)
        self.start()
        self.enter_net()
        for _ in range(3):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.75)
        self.assertEqual(self.net_retract_calls, 0)
        for _ in range(3):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.65)
        self.assertEqual(self.net_retract_calls, 1)

    def test_low_ring_must_be_consecutive(self):
        self.build()
        self.start()
        self.enter_net()
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)  # 打断
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.assertEqual(self.net_retract_calls, 0)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.assertEqual(self.net_retract_calls, 1)

    def test_unknown_ring_never_retracts_or_restores_f5(self):
        self.build()
        self.start()
        self.enter_net()
        for _ in range(20):
            self.tick(0.5, fish=T.TRUE, bar=2, ring=None)
        self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.net_retract_calls, 0)
        self.assertEqual(self.arb.owned, frozenset({F6}))
        self.assertEqual(self.sink.ups, [F5])
        self.assertFalse(self.machine.net_retract_pending)

    def test_non_finite_and_out_of_range_ring_never_retracts(self):
        self.build()
        self.start()
        self.enter_net()
        for bad in (math.nan, math.inf, -math.inf, -0.01, 1.01):
            for _ in range(5):
                self.tick(0.0, fish=T.TRUE, bar=2, ring=bad)
            self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.net_retract_calls, 0)

    def test_duplicate_frames_do_not_accumulate_low_ring(self):
        self.build()
        self.start()
        self.enter_net()
        same = mk(20.0, fish=T.TRUE, bar=2, ring=0.85)
        self.frame(20.0, fish=T.TRUE, bar=2, ring=0.85)
        self.machine.tick(same)
        self.machine.tick(same)
        self.assertEqual(self.net_retract_calls, 0)
        self.frame(20.5, fish=T.TRUE, bar=2, ring=0.85)
        self.assertEqual(self.net_retract_calls, 0)
        self.frame(21.0, fish=T.TRUE, bar=2, ring=0.85)
        self.assertEqual(self.net_retract_calls, 1)

    def test_unfocused_or_stale_frames_clear_low_ring_continuity(self):
        self.build()
        self.start()
        self.enter_net()
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85, foreground=False)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.assertEqual(self.net_retract_calls, 0)

        # 过时帧同样整帧拒绝，不累计、不动作。
        self.clock.now = 100.0
        self.observe(1.0, fish=T.TRUE, bar=2, ring=0.85)
        self.assertEqual(self.net_retract_calls, 0)
        self.assertEqual(self.arb.owned, frozenset({F6}))

    def test_retract_failure_keeps_net_f6_and_never_restores_f5(self):
        def boom():
            self.net_retract_calls += 1
            raise RuntimeError("net stuck")

        self.build(net_retract_action=boom)
        self.start()
        self.enter_net()
        for _ in range(3):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.assertIs(self.machine.state, S.NET)
        self.assertTrue(self.machine.fault)
        self.assertIn("net stuck", self.machine.fault_reason)
        self.assertEqual(self.machine.net_state, afm.NetState.UNKNOWN)
        self.assertEqual(self.machine.net_state_basis, "unknown_fault")
        self.assertEqual(self.arb.owned, frozenset({F6}))
        self.assertEqual(self.sink.ups, [F5])

        # 不重复盲 toggle，不自动恢复 F5；继续低环也一样。
        for _ in range(20):
            self.tick(1.0, fish=T.TRUE, bar=2, ring=0.30)
        self.assertEqual(self.net_retract_calls, 1)
        self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.arb.owned, frozenset({F6}))

    def test_retract_callback_missing_blocks_entry_before_releasing_f5(self):
        self.build(net_retract_action=None)
        self.start()
        self.enter_land()
        self.tick(8.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)
        self.assertTrue(self.machine.auto_net_blocked)
        self.assertEqual(self.machine.auto_net_blocked_reason, "net_callback_missing")
        self.assertEqual(self.net_calls, 0)
        self.assertEqual(self.sink.ups, [])
        self.assertEqual(self.arb.owned, frozenset({F5, F6}))

    def test_deploy_callback_failure_keeps_net_f6_unknown(self):
        def boom():
            self.net_calls += 1
            raise RuntimeError("net jammed")

        self.build(net_action=boom)
        self.start()
        self.enter_land()
        self.tick(8.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.NET)
        self.assertTrue(self.machine.fault)
        self.assertEqual(self.machine.net_state, afm.NetState.UNKNOWN)
        self.assertEqual(self.arb.owned, frozenset({F6}))
        self.assertFalse(self.machine.control_allowed)

    def test_captured_has_priority_over_retraction(self):
        self.build()
        self.start()
        self.enter_net()
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85, captured=T.TRUE)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85, captured=T.TRUE)
        self.assertIs(self.machine.state, S.SETTLE)
        self.assertEqual(self.net_retract_calls, 0)
        self.assertEqual(self.arb.owned, frozenset())

    def test_captured_waits_for_animation_without_retract_retry(self):
        self.build()
        self.start()
        self.enter_net()
        for _ in range(3):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.assertEqual(self.net_retract_calls, 1)
        # 等待动画期间 captured 仍然优先，且不会补发第二次收网。
        self.tick(0.0, captured=T.TRUE)
        self.tick(0.0, captured=T.TRUE)
        self.assertIs(self.machine.state, S.SETTLE)
        self.assertEqual(self.net_retract_calls, 1)

    def test_window_expiry_only_reminds_and_never_retracts_on_high_ring(self):
        self.build()
        self.start()
        self.enter_net()
        self.tick(14.9, fish=T.TRUE, bar=2, ring=0.985)
        self.assertFalse(self.machine.net_retract_pending)
        self.tick(0.1, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.net_retract_calls, 0)
        self.assertEqual(self.arb.owned, frozenset({F6}))
        reminders = [
            e for e in self.machine.events
            if e.reason == "net_window_expired_reminder"
        ]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0].desired_keys, (F6,))

        for _ in range(300):
            self.tick(1.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.net_retract_calls, 0)
        self.assertEqual(
            len([e for e in self.machine.events
                 if e.reason == "net_window_expired_reminder"]), 1
        )

    def test_retracted_fight_reaccumulates_land_and_cooldown(self):
        self.build()
        self.start()
        self.enter_net()
        for _ in range(3):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.tick(1.2, fish=T.TRUE, bar=2, ring=0.85)
        self.assertIs(self.machine.state, S.FIGHT)

        # 冷却期内即使临岸也不重新 LAND。
        for _ in range(20):
            self.tick(0.1, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.FIGHT)

        # 冷却结束后重新 3 帧 + 8s，不能继承收网前的计时。
        self.tick(5.0, fish=T.TRUE, bar=2, ring=0.50)
        for _ in range(3):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)
        self.tick(7.9, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)
        self.tick(0.1, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.net_calls, 2)
        self.assertEqual(self.net_retract_calls, 1)

    def test_manual_confirm_still_available_as_fallback(self):
        self.build()
        self.start()
        self.enter_net()
        self.assertTrue(self.machine.confirm_net_retracted())
        self.assertIs(self.machine.state, S.FIGHT)
        self.assertEqual(self.machine.net_state, afm.NetState.STOWED)
        self.assertEqual(self.machine.net_state_basis, "operator_confirmed")
        self.assertEqual(self.arb.owned, frozenset({F5, F6}))

    def test_reset_net_attempt_does_not_confirm_retraction(self):
        self.build()
        self.start()
        self.enter_net()
        before = list(self.sink.calls)
        self.assertTrue(self.machine.reset_net_attempt())
        self.assertEqual(self.sink.calls, before)
        self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.arb.owned, frozenset({F6}))

    def test_stop_during_retract_wait_releases_f6(self):
        self.build()
        self.start()
        self.enter_net()
        for _ in range(3):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.85)
        self.assertEqual(self.net_retract_calls, 1)
        self.machine.stop()
        self.assertIs(self.machine.state, S.STOPPED)
        self.assertEqual(self.arb.owned, frozenset())
        self.assertIn(F6, self.sink.ups)

    def test_takeover_unknown_net_never_blind_toggles(self):
        self.build()
        self.start(net_state=afm.NetState.UNKNOWN)
        self.enter_land()
        for _ in range(10):
            self.tick(1.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)
        self.assertEqual(self.net_calls, 0)
        self.assertEqual(self.machine.auto_net_blocked_reason, "net_state_unknown")
        self.assertEqual(self.arb.owned, frozenset({F5, F6}))


    def test_manual_confirm_only_in_net_and_reaccumulates_near(self):
        self.build()
        self.assertFalse(self.machine.confirm_net_retracted())
        self.start()
        self.assertFalse(self.machine.confirm_net_retracted())
        self.enter_land()
        self.assertFalse(self.machine.confirm_net_retracted())
        self.enter_net()
        self.assertTrue(self.machine.confirm_net_retracted())
        self.assertIs(self.machine.state, S.FIGHT)
        # 人工确认同样进入收网冷却；冷却后重新累计临岸。
        self.tick(5.1, fish=T.TRUE, bar=2, ring=0.50)
        for _ in range(3):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)

    def test_new_start_after_stop_allows_one_toggle_again(self):
        self.build()
        self.start()
        self.enter_net()
        self.machine.stop()
        self.start()
        self.enter_land()
        self.tick(8.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.net_calls, 2)

    def test_near_grace_zero_does_not_loop_actions_in_one_tick(self):
        self.build(near_grace=0.0, net_window=0.0)
        self.start()
        self.enter_land()
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.net_calls, 1)
        self.assertEqual(self.net_retract_calls, 0)
        reminders = [
            e for e in self.machine.events
            if e.reason == "net_window_expired_reminder"
        ]
        self.assertLessEqual(len(reminders), 1)
        self.assertEqual(self.net_calls, 1)


class TestAutoNetGate(MachineCase):
    """生产默认 auto_net_enabled=False：LAND -> NET 之前就必须拦住。"""

    def test_production_default_keeps_land_f5_f6_for_60s_at_ring_985(self):
        self.build(auto_net_enabled=False)
        self.start()
        self.enter_land(ring=0.985)
        self.assertEqual(self.sink.ups, [])   # start 之后没有松过键

        # 恒定 0.985 连续 60s：远超 near_grace，但绝不进 NET。
        for _ in range(60):
            self.tick(1.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)
        self.assertTrue(self.machine.auto_net_blocked)
        self.assertFalse(self.machine.net_attempted)
        self.assertEqual(self.net_calls, 0)
        self.assertEqual(self.sink.ups, [])   # 没有松 F5（也没有松 F6）
        self.assertEqual(self.arb.desired, frozenset({F5, F6}))
        self.assertEqual(self.arb.emitted, frozenset({F5, F6}))
        self.assertFalse(self.machine.control_allowed)  # 只有 FIGHT 才授权摩擦

    def test_disabled_net_action_none_still_cannot_release_f5(self):
        """仅 net_action=None 是旧 bug：即使没有动作也必须先被开关拦住。"""
        self.build(net_action=None, auto_net_enabled=False)
        self.start()
        self.enter_land(ring=0.985)
        self.tick(20.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)
        self.assertEqual(self.sink.ups, [])
        self.assertEqual(self.arb.emitted, frozenset({F5, F6}))

    def test_explicit_enable_still_reaches_legacy_net(self):
        self.build(auto_net_enabled=True)
        self.start()
        self.enter_land(ring=0.985)
        self.tick(8.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.net_calls, 1)
        self.assertEqual(self.sink.ups, [F5])   # 旧语义：只松 F5
        self.assertEqual(self.arb.emitted, frozenset({F6}))

    def test_stop_from_blocked_land_still_releases_both_keys(self):
        self.build(auto_net_enabled=False)
        self.start()
        self.enter_land(ring=0.985)
        self.tick(9.0, fish=T.TRUE, bar=2, ring=0.985)
        self.machine.stop()
        self.assertIs(self.machine.state, S.STOPPED)
        self.assertEqual(self.sink.ups, [F5, F6])
        self.assertEqual(self.arb.owned, frozenset())

    def test_captured_from_blocked_land_still_settles_and_releases(self):
        self.build(auto_net_enabled=False)
        self.start()
        self.enter_land(ring=0.985)
        self.tick(9.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertTrue(self.machine.auto_net_blocked)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985, captured=T.TRUE)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985, captured=T.TRUE)
        self.assertIs(self.machine.state, S.SETTLE)
        self.assertEqual(self.sink.ups, [F5, F6])
        self.assertEqual(self.net_calls, 0)

    def test_config_rejects_non_bool_auto_net(self):
        with self.assertRaises(TypeError):
            afm.FightConfig(auto_net_enabled=1)


class TestNetFault(MachineCase):
    def test_net_action_error_freezes_net_and_keeps_f6(self):
        def boom():
            self.net_calls += 1
            raise RuntimeError("net jammed")

        self.build(net_action=boom)
        self.start()
        self.enter_land()
        self.tick(8.0, fish=T.TRUE, bar=2, ring=0.985)

        self.assertIs(self.machine.state, S.NET)
        self.assertTrue(self.machine.fault)
        self.assertIn("net jammed", self.machine.fault_reason)
        self.assertEqual(self.net_calls, 1)
        self.assertEqual(self.arb.owned, frozenset({F6}))
        self.assertFalse(self.machine.control_allowed)
        self.assertTrue(self.machine.net_attempted)

    def test_fault_does_not_resend_or_switch_automatically(self):
        def boom():
            self.net_calls += 1
            raise RuntimeError("net jammed")

        self.build(net_action=boom)
        self.start()
        self.enter_land()
        self.tick(8.0, fish=T.TRUE, bar=2, ring=0.985)

        before = len(self.sink.calls)
        self.tick(1.0, fish=T.TRUE, bar=2, ring=0.985)
        self.tick(100.0, fish=T.TRUE, bar=2, ring=0.10)
        self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.net_calls, 1)
        self.assertEqual(len(self.sink.calls), before)
        self.assertEqual(self.arb.owned, frozenset({F6}))

    def test_clear_fault_is_not_retraction_confirmation(self):
        def boom():
            self.net_calls += 1
            raise RuntimeError("net jammed")

        self.build(net_action=boom)
        self.start()
        self.enter_land()
        self.tick(8.0, fish=T.TRUE, bar=2, ring=0.985)

        self.assertTrue(self.machine.clear_fault())
        self.assertFalse(self.machine.fault)
        self.assertEqual(self.net_calls, 1)
        self.tick(15.0, fish=T.TRUE, bar=2, ring=0.985)
        # 解除冻结不等于网已收回：仍然 NET + F6，只请求上层确认。
        self.assertIs(self.machine.state, S.NET)
        self.assertTrue(self.machine.net_retract_pending)
        self.assertEqual(self.net_calls, 1)
        self.assertEqual(self.arb.owned, frozenset({F6}))
        self.assertEqual(self.sink.downs, [F5, F6])

        self.assertTrue(self.machine.confirm_net_retracted())
        self.assertIs(self.machine.state, S.FIGHT)
        self.assertEqual(self.arb.owned, frozenset({F5, F6}))

    def test_captured_still_settles_during_fault(self):
        def boom():
            self.net_calls += 1
            raise RuntimeError("net jammed")

        self.build(net_action=boom)
        self.start()
        self.enter_land()
        self.tick(8.0, fish=T.TRUE, bar=2, ring=0.985)

        self.tick(0.0, captured=T.TRUE)
        self.tick(0.0, captured=T.TRUE)
        self.assertIs(self.machine.state, S.SETTLE)
        self.assertEqual(self.arb.owned, frozenset())
        self.assertEqual(self.sink.ups, [F5, F6])

    def test_stop_still_works_during_fault(self):
        def boom():
            self.net_calls += 1
            raise RuntimeError("net jammed")

        self.build(net_action=boom)
        self.start()
        self.enter_land()
        self.tick(8.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertTrue(self.machine.fault)

        self.machine.stop()
        self.assertIs(self.machine.state, S.STOPPED)
        self.assertFalse(self.machine.fault)
        self.assertEqual(self.arb.owned, frozenset())
        self.assertEqual(self.sink.ups, [F5, F6])


class TestCaptured(MachineCase):
    def test_two_captured_frames_settle_and_release_all(self):
        self.build()
        self.start()
        self.tick(0.0, captured=T.TRUE)
        self.assertIs(self.machine.state, S.FIGHT)
        self.tick(0.0, captured=T.TRUE)
        self.assertIs(self.machine.state, S.SETTLE)
        self.assertEqual(self.sink.ups, [F5, F6])
        self.assertEqual(self.arb.emitted, frozenset())

    def test_single_captured_frame_is_not_enough(self):
        self.build()
        self.start()
        self.tick(0.0, captured=T.TRUE)
        self.assertIs(self.machine.state, S.FIGHT)
        self.assertEqual(self.sink.ups, [])

    def test_duplicate_captured_frame_is_not_counted_twice(self):
        self.build()
        self.start()
        same = mk(3.0, captured=T.TRUE)
        self.frame(3.0, captured=T.TRUE)
        self.machine.tick(same)
        self.assertIs(self.machine.state, S.FIGHT)
        self.frame(4.0, captured=T.TRUE)
        self.assertIs(self.machine.state, S.SETTLE)

    def test_captured_unknown_resets_confirmation(self):
        self.build()
        self.start()
        self.tick(0.0, captured=T.TRUE)
        self.tick(0.0, captured=T.UNKNOWN)
        self.tick(0.0, captured=T.TRUE)
        self.assertIs(self.machine.state, S.FIGHT)
        self.tick(0.0, captured=T.TRUE)
        self.assertIs(self.machine.state, S.SETTLE)

    def test_captured_confirmation_survives_ring_loss_transition(self):
        """结算瞬间圆环不可读会触发 LAND->FIGHT，但 captured 确认不能被清掉。"""
        self.build()
        self.start()
        self.enter_land()
        self.tick(0.0, fish=T.TRUE, bar=2, ring=None, captured=T.TRUE)
        self.assertIs(self.machine.state, S.FIGHT)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=None, captured=T.TRUE)
        self.assertIs(self.machine.state, S.SETTLE)

    def test_settle_from_net_releases_f6_only(self):
        self.build()
        self.start()
        self.enter_net()
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985, captured=T.TRUE)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985, captured=T.TRUE)
        self.assertIs(self.machine.state, S.SETTLE)
        self.assertEqual(self.sink.ups, [F5, F6])  # F5 在进 NET 时已松
        self.assertEqual(self.arb.emitted, frozenset())

    def test_settle_tick_retries_release_without_repeating_other_actions(self):
        self.build()
        self.start()

        self.sink.fail = lambda key, is_down: key == F5 and not is_down
        self.tick(0.0, captured=T.TRUE)
        self.tick(0.5, captured=T.TRUE)
        self.assertIs(self.machine.state, S.SETTLE)
        self.assertEqual(self.arb.owned, frozenset({F5, F6}))

        self.sink.fail = None
        self.tick(0.5, captured=T.TRUE)
        self.assertEqual(self.arb.owned, frozenset())

    def test_settle_release_is_retried_even_without_a_new_frame(self):
        """SETTLE 的释放重试在帧门之前；重复帧也能继续松键。"""
        self.build()
        self.start()
        self.sink.fail = lambda key, is_down: key == F5 and not is_down
        self.tick(0.0, captured=T.TRUE)
        self.tick(0.5, captured=T.TRUE)
        self.assertIs(self.machine.state, S.SETTLE)
        self.assertEqual(self.arb.owned, frozenset({F5, F6}))

        self.sink.fail = None
        same = mk(self.ts, captured=T.TRUE)
        self.machine.tick(same)
        self.machine.tick(same)     # 重复帧也应触发释放重试
        self.assertEqual(self.arb.owned, frozenset())

    def test_captured_cannot_be_forced_by_source_keyword(self):
        self.build()
        self.start()
        with self.assertRaises(TypeError):
            self.machine.tick(mk(0.0), source="pull")


class TestLost(MachineCase):
    def test_lost_requires_full_two_seconds(self):
        self.build()
        self.start()
        self.tick(0.0, fish=T.FALSE, bar=0)
        self.tick(1.0, fish=T.FALSE, bar=0)
        self.tick(0.99, fish=T.FALSE, bar=0)
        self.assertIs(self.machine.state, S.FIGHT)
        self.assertEqual(self.sink.ups, [])
        self.tick(0.01, fish=T.FALSE, bar=0)
        self.assertIs(self.machine.state, S.LOST)
        self.assertEqual(self.sink.ups, [F5, F6])

    def test_unknown_breaks_lost_continuity(self):
        self.build()
        self.start()
        self.tick(0.0, fish=T.FALSE, bar=0)
        self.tick(1.5, fish=T.FALSE, bar=0)
        self.tick(0.0, fish=T.UNKNOWN, bar=0)
        self.tick(1.0, fish=T.FALSE, bar=0)
        self.assertIs(self.machine.state, S.FIGHT)
        self.tick(1.0, fish=T.FALSE, bar=0)
        self.assertIs(self.machine.state, S.FIGHT)
        self.tick(1.0, fish=T.FALSE, bar=0)
        self.assertIs(self.machine.state, S.LOST)

    def test_bar_unknown_breaks_lost_continuity(self):
        self.build()
        self.start()
        self.tick(0.0, fish=T.FALSE, bar=0)
        self.tick(1.5, fish=T.FALSE, bar=None)
        self.tick(1.5, fish=T.FALSE, bar=0)
        self.assertIs(self.machine.state, S.FIGHT)

    def test_nonzero_bar_is_not_detached(self):
        self.build()
        self.start()
        for _ in range(10):
            self.tick(1.0, fish=T.FALSE, bar=2)
        self.assertIs(self.machine.state, S.FIGHT)
        self.assertEqual(self.sink.ups, [])

    def test_single_unknown_never_releases(self):
        self.build()
        self.start()
        self.tick(0.0, fish=T.UNKNOWN, bar=None)
        self.assertIs(self.machine.state, S.FIGHT)
        self.assertEqual(self.sink.ups, [])

    def test_lost_is_terminal_for_tick(self):
        self.build()
        self.start()
        self.tick(0.0, fish=T.FALSE, bar=0)
        self.tick(1.0, fish=T.FALSE, bar=0)
        self.tick(1.0, fish=T.FALSE, bar=0)
        self.assertIs(self.machine.state, S.LOST)
        before = len(self.sink.calls)
        for _ in range(5):
            self.tick(1.0, fish=T.FALSE, bar=0)
        self.assertEqual(len(self.sink.calls), before)

    def test_lost_release_failure_is_retried_without_repress(self):
        """进入 LOST 时释放失败：后续 tick 重试空集合释放，绝不重按。"""
        self.build()
        self.start()
        self.tick(0.0, fish=T.FALSE, bar=0)
        self.tick(1.0, fish=T.FALSE, bar=0)
        self.sink.fail = lambda key, is_down: key == F5 and not is_down
        self.tick(1.0, fish=T.FALSE, bar=0)

        self.assertIs(self.machine.state, S.LOST)
        self.assertEqual(self.arb.owned, frozenset({F5, F6}))
        downs_before = len(self.sink.downs)

        self.sink.fail = None
        self.tick(1.0, fish=T.FALSE, bar=0)
        self.assertEqual(self.arb.owned, frozenset())
        self.assertEqual(len(self.sink.downs), downs_before)   # 没有重新按下
        self.assertEqual(self.sink.ups, [F5, F6])

        before = len(self.sink.calls)
        self.tick(1.0, fish=T.FALSE, bar=0)
        self.assertEqual(len(self.sink.calls), before)

    def test_lost_release_is_retried_even_without_a_new_frame(self):
        """LOST 的释放重试不依赖新帧门；重复帧也不会让键永远卡住。"""
        self.build()
        self.start()
        self.tick(0.0, fish=T.FALSE, bar=0)
        self.tick(1.0, fish=T.FALSE, bar=0)
        self.sink.fail = lambda key, is_down: key == F6 and not is_down
        self.tick(1.0, fish=T.FALSE, bar=0)
        self.assertIs(self.machine.state, S.LOST)
        self.assertEqual(self.arb.owned, frozenset({F6}))

        self.sink.fail = None
        same = mk(self.ts, fish=T.FALSE, bar=0)
        self.machine.tick(same)
        self.machine.tick(same)     # 重复帧也应触发释放重试
        self.assertEqual(self.arb.owned, frozenset())


class TestLongFight(MachineCase):
    def test_600_seconds_without_timeout_release(self):
        self.build()
        self.start()
        for _ in range(600):
            self.tick(1.0, fish=T.TRUE, bar=2, ring=0.50)
        self.assertIs(self.machine.state, S.FIGHT)
        self.assertEqual(self.sink.ups, [])
        self.assertEqual(self.arb.owned, frozenset({F5, F6}))
        self.assertTrue(self.machine.control_allowed)

    def test_600_seconds_unknown_ring_without_release(self):
        self.build()
        self.start()
        for _ in range(600):
            self.tick(1.0, fish=T.TRUE, bar=None, ring=None)
        self.assertIs(self.machine.state, S.FIGHT)
        self.assertEqual(self.sink.ups, [])


class TestSparseFrames(MachineCase):
    """帧间隔超过 max_frame_gap 时不得把稀疏帧当成连续观测。"""

    def test_captured_confirmation_does_not_span_a_gap(self):
        self.build(max_frame_gap=1.5)
        self.start()
        self.frame(0.0, captured=T.TRUE)
        self.frame(2.0, captured=T.TRUE)    # 间隔 2.0 > 1.5：计数重新开始
        self.assertIs(self.machine.state, S.FIGHT)
        self.frame(2.5, captured=T.TRUE)
        self.assertIs(self.machine.state, S.SETTLE)

    def test_land_frames_do_not_span_a_gap(self):
        self.build(max_frame_gap=1.5)
        self.start()
        self.frame(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.frame(2.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.FIGHT)
        self.frame(2.5, fish=T.TRUE, bar=2, ring=0.985)
        self.frame(3.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)

    def test_land_grace_restarts_after_a_gap_without_releasing(self):
        self.build(max_frame_gap=1.5, near_grace=8.0)
        self.start()
        self.frame(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.frame(0.1, fish=T.TRUE, bar=2, ring=0.985)
        self.frame(0.2, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)

        self.frame(2.0, fish=T.TRUE, bar=2, ring=0.985)   # 间隔 1.8 > 1.5：稀疏
        self.assertIs(self.machine.state, S.LAND)
        self.assertEqual(self.net_calls, 0)
        self.assertEqual(self.sink.ups, [])          # 稀疏帧不松键
        self.assertEqual(self.arb.owned, frozenset({F5, F6}))

        # near 宽限从稀疏帧那一刻重新计时：持续 1 秒/帧，满 8 秒才进 NET。
        for ts in range(3, 10):
            self.frame(float(ts), fish=T.TRUE, bar=2, ring=0.985)
            self.assertIs(self.machine.state, S.LAND)
            self.assertEqual(self.net_calls, 0)
        self.frame(10.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.NET)
        self.assertEqual(self.net_calls, 1)

    def test_lost_timer_restarts_after_a_gap_without_releasing(self):
        self.build(max_frame_gap=1.5, lost_hold=2.0)
        self.start()
        self.frame(0.0, fish=T.FALSE, bar=0)
        self.frame(1.0, fish=T.FALSE, bar=0)
        self.assertIs(self.machine.state, S.FIGHT)

        # 间隔 1.6 > 1.5：lost 计时清零，本帧重新起算，不能直接判丢。
        self.frame(2.6, fish=T.FALSE, bar=0)
        self.assertIs(self.machine.state, S.FIGHT)
        self.assertEqual(self.sink.ups, [])
        self.assertEqual(self.arb.owned, frozenset({F5, F6}))

        self.frame(3.6, fish=T.FALSE, bar=0)
        self.assertIs(self.machine.state, S.FIGHT)
        self.frame(4.7, fish=T.FALSE, bar=0)
        self.assertIs(self.machine.state, S.LOST)

    def test_duplicate_and_out_of_order_frames_still_ignored(self):
        self.build(max_frame_gap=1.5)
        self.start()
        self.frame(5.0, fish=T.TRUE, bar=2, ring=0.985)
        self.frame(5.0, fish=T.TRUE, bar=2, ring=0.985)   # 重复
        self.frame(4.0, fish=T.TRUE, bar=2, ring=0.985)   # 倒序
        self.assertIs(self.machine.state, S.FIGHT)
        self.frame(5.1, fish=T.TRUE, bar=2, ring=0.985)
        self.frame(5.2, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)


class TestFrameFreshness(MachineCase):
    """过时/未来帧不得累计证据、发动作或授权控制。"""

    def test_stale_frame_is_rejected_and_clears_continuity(self):
        self.build()
        self.start()
        self.frame(0.0, fish=T.TRUE, bar=2, ring=0.985)   # near 1/3

        self.clock.now = 2.5
        self.observe(0.5, fish=T.TRUE, bar=2, ring=0.985)  # age 2.0 > 1.5
        self.assertIs(self.machine.state, S.FIGHT)

        # 连续性已被清空：必须重新凑够 3 帧。
        self.frame(2.5, fish=T.TRUE, bar=2, ring=0.985)
        self.frame(2.6, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.FIGHT)
        self.frame(2.7, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)

    def test_future_frame_is_rejected(self):
        self.build()
        self.start()
        self.observe(1.0, fish=T.TRUE, bar=2, ring=0.985)  # ts 超前 1.0s
        self.assertIs(self.machine.state, S.FIGHT)
        self.assertFalse(self.machine.control_allowed)

        self.frame(0.0, fish=T.TRUE, bar=2, ring=0.50)
        self.assertTrue(self.machine.control_allowed)

    def test_future_within_tolerance_is_accepted(self):
        self.build()
        self.start()
        self.observe(0.04, fish=T.TRUE, bar=2, ring=0.50)
        self.assertIs(self.machine.state, S.FIGHT)
        self.assertTrue(self.machine.control_allowed)

    def test_stale_frame_disallows_control_until_fresh(self):
        self.build()
        self.start()
        self.frame(0.0, fish=T.TRUE, bar=2, ring=0.50)
        self.assertTrue(self.machine.control_allowed)

        self.clock.now = 2.0
        self.observe(0.1, fish=T.TRUE, bar=2, ring=0.50)  # age 1.9
        self.assertFalse(self.machine.control_allowed)

        self.frame(2.0, fish=T.TRUE, bar=2, ring=0.50)
        self.assertTrue(self.machine.control_allowed)

    def test_receive_clock_gap_breaks_continuity_even_if_obs_gap_small(self):
        self.build(max_frame_gap=1.5)
        self.start()
        self.frame(0.0, fish=T.TRUE, bar=2, ring=0.985)    # 1/3

        # 观测戳只走了 1.0s，但接收时钟走了 1.7s：同样算稀疏帧。
        self.clock.now = 1.7
        self.observe(1.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.FIGHT)

        self.clock.now = 1.8
        self.observe(1.1, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.FIGHT)
        self.clock.now = 1.9
        self.observe(1.2, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)

    def test_stale_frame_in_land_resets_grace(self):
        self.build(near_grace=8.0)
        self.start()
        self.enter_land()

        self.clock.now = 10.0
        self.observe(0.5, fish=T.TRUE, bar=2, ring=0.985)  # 过时帧
        self.assertIs(self.machine.state, S.LAND)
        self.assertEqual(self.net_calls, 0)
        self.assertEqual(self.arb.owned, frozenset({F5, F6}))

        self.frame(10.0, fish=T.TRUE, bar=2, ring=0.985)   # 新起点
        self.tick(7.9, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)
        self.tick(0.1, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.NET)


class TestOcclusion(MachineCase):
    """失焦 / HUD 不可见 / 确定弹窗：只冻结控制意图，不松键不发新动作。"""

    def test_foreground_false_freezes_without_releasing(self):
        self.build()
        self.start()
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.50)
        self.assertTrue(self.machine.control_allowed)
        before = list(self.sink.calls)

        for _ in range(5):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985, foreground=False)
        self.assertEqual(self.sink.calls, before)
        self.assertIs(self.machine.state, S.FIGHT)
        self.assertFalse(self.machine.control_allowed)

    def test_hud_not_true_freezes_without_releasing(self):
        self.build()
        self.start()
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.50)
        before = list(self.sink.calls)
        for hud in (T.UNKNOWN, T.FALSE):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985, hud=hud)
            self.assertIs(self.machine.state, S.FIGHT)
        self.assertEqual(self.sink.calls, before)

    def test_ok_modal_true_freezes_without_releasing(self):
        self.build()
        self.start()
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.50)
        before = list(self.sink.calls)
        for _ in range(3):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985, ok=T.TRUE)
        self.assertEqual(self.sink.calls, before)
        self.assertIs(self.machine.state, S.FIGHT)

    def test_occlusion_clears_near_evidence(self):
        self.build()
        self.start()
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)          # 1/3
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985,
                  foreground=False)                             # 清空
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)          # 1/3
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)          # 2/3
        self.assertIs(self.machine.state, S.FIGHT)
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)

    def test_occlusion_in_land_resets_grace(self):
        self.build(near_grace=8.0)
        self.start()
        self.enter_land()                                       # land_since≈0.003
        self.tick(7.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.LAND)

        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985,
                  foreground=False)                             # 计时重置
        for _ in range(7):
            self.tick(1.0, fish=T.TRUE, bar=2, ring=0.985)
            self.assertIs(self.machine.state, S.LAND)
            self.assertEqual(self.net_calls, 0)
        self.tick(1.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertIs(self.machine.state, S.NET)

    def test_occlusion_does_not_trigger_net_action(self):
        self.build(near_grace=0.0)
        self.start()
        self.enter_land()
        self.tick(1.0, fish=T.TRUE, bar=2, ring=0.985,
                  foreground=False)
        self.assertIs(self.machine.state, S.LAND)
        self.assertEqual(self.net_calls, 0)
        self.assertEqual(self.sink.ups, [])

    def test_control_allowed_recovers_after_fresh_unoccluded_frame(self):
        self.build()
        self.start()
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.50)
        self.assertTrue(self.machine.control_allowed)

        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.50, hud=T.UNKNOWN)
        self.assertFalse(self.machine.control_allowed)

        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.50)
        self.assertTrue(self.machine.control_allowed)

    def test_captured_is_not_counted_while_occluded(self):
        self.build()
        self.start()
        self.tick(0.0, captured=T.TRUE, foreground=False)
        self.tick(0.0, captured=T.TRUE, hud=T.UNKNOWN)
        self.assertIs(self.machine.state, S.FIGHT)

        self.tick(0.0, captured=T.TRUE)
        self.assertIs(self.machine.state, S.FIGHT)
        self.tick(0.0, captured=T.TRUE)
        self.assertIs(self.machine.state, S.SETTLE)

    def test_settle_release_retry_not_gated_by_occlusion(self):
        self.build()
        self.start()
        self.sink.fail = lambda key, is_down: key == F5 and not is_down
        self.tick(0.0, captured=T.TRUE)
        self.tick(0.5, captured=T.TRUE)
        self.assertIs(self.machine.state, S.SETTLE)
        self.assertEqual(self.arb.owned, frozenset({F5, F6}))

        self.sink.fail = None
        # 遮挡帧也必须能完成终态释放（终态释放重试在帧门之前）。
        self.tick(0.5, foreground=False)
        self.assertEqual(self.arb.owned, frozenset())

    def test_lost_release_retry_not_gated_by_occlusion(self):
        self.build()
        self.start()
        self.tick(0.0, fish=T.FALSE, bar=0)
        self.tick(1.0, fish=T.FALSE, bar=0)
        self.sink.fail = lambda key, is_down: key == F5 and not is_down
        self.tick(1.0, fish=T.FALSE, bar=0)
        self.assertIs(self.machine.state, S.LOST)

        self.sink.fail = None
        self.tick(1.0, foreground=False)
        self.assertEqual(self.arb.owned, frozenset())


class _ReconcileNoopArbiter(ain.InputArbiter):
    """模拟“reconcile 声称成功、实际没有按住任何键”的异常 arbiter。"""

    def reconcile(self, reason=None):
        return ()


class TestNetActionConfirmation(unittest.TestCase):
    def test_missing_retract_callback_blocks_land_without_releasing_f5(self):
        sink = FakeSink()
        arb = _ReconcileNoopArbiter(sink)
        clock = FakeClock()
        net_calls = []

        machine = afm.FightMachine(
            arb,
            clock=clock,
            net_action=lambda: net_calls.append(1),
            net_retract_action=None,
            config=afm.FightConfig(auto_net_enabled=True),
        )
        machine.start(net_state=afm.NetState.STOWED)
        self.assertIs(machine.state, S.FIGHT)

        ts = 0.0
        for _ in range(3):
            clock.now = ts
            machine.tick(mk(ts, fish=T.TRUE, bar=2, ring=0.985))
            ts += 0.5
        self.assertIs(machine.state, S.LAND)

        # 用连续 1s 帧跨过 near_grace（避免自己造出稀疏帧）。
        for _ in range(8):
            ts += 1.0
            clock.now = ts
            machine.tick(mk(ts, fish=T.TRUE, bar=2, ring=0.985))
        self.assertIs(machine.state, S.LAND)
        self.assertEqual(net_calls, [])
        self.assertTrue(machine.auto_net_blocked)
        self.assertEqual(machine.auto_net_blocked_reason, "net_callback_missing")
        self.assertEqual(arb.desired, frozenset({F5, F6}))
        self.assertEqual(arb.emitted, frozenset())


class TestConfigValidation(unittest.TestCase):
    def test_time_fields_must_be_finite_non_negative(self):
        for field in ("near_grace", "net_window", "lost_hold"):
            for bad in (math.nan, math.inf, -math.inf, -0.001, True, None, "1"):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValueError):
                        afm.FightConfig(**{field: bad})
            self.assertEqual(
                getattr(afm.FightConfig(**{field: 0}), field), 0
            )

    def test_max_frame_gap_must_be_positive_finite(self):
        self.assertEqual(afm.FightConfig().max_frame_gap, 1.5)
        for bad in (0, 0.0, -0.1, math.nan, math.inf, -math.inf, True, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    afm.FightConfig(max_frame_gap=bad)
        self.assertEqual(afm.FightConfig(max_frame_gap=2).max_frame_gap, 2)

    def test_max_observation_age_must_be_positive_finite(self):
        self.assertEqual(afm.FightConfig().max_observation_age, 1.5)
        for bad in (0, 0.0, -0.1, math.nan, math.inf, -math.inf, True, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    afm.FightConfig(max_observation_age=bad)
        self.assertEqual(
            afm.FightConfig(max_observation_age=2).max_observation_age, 2
        )

    def test_counts_must_be_positive_integers_not_bool(self):
        for field in ("captured_frames", "land_frames", "event_limit"):
            for bad in (0, -1, 1.5, 2.0, True, False, "2", None):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValueError):
                        afm.FightConfig(**{field: bad})
            self.assertEqual(
                getattr(afm.FightConfig(**{field: 1}), field), 1
            )

    def test_ring_min_must_be_finite_within_unit_interval(self):
        for bad in (math.nan, math.inf, -math.inf, -0.01, 1.01, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    afm.FightConfig(ring_min=bad)
        self.assertEqual(afm.FightConfig(ring_min=0).ring_min, 0)
        self.assertEqual(afm.FightConfig(ring_min=1).ring_min, 1)


class TestEvents(MachineCase):
    def test_event_has_required_fields(self):
        self.build()
        self.start()
        ev = self.machine.events[-1]
        self.assertIs(ev.from_state, S.STOPPED)
        self.assertIs(ev.to_state, S.FIGHT)
        self.assertEqual(ev.reason, "start")
        self.assertEqual(ev.time, 0.0)
        self.assertEqual(ev.desired_keys, (F5, F6))

    def test_land_and_net_events_record_desired_keys(self):
        self.build()
        self.start()
        self.enter_land()
        self.assertEqual(self.machine.events[-1].to_state, S.LAND)
        self.assertEqual(self.machine.events[-1].desired_keys, (F5, F6))
        self.tick(8.0, fish=T.TRUE, bar=2, ring=0.985)
        self.assertEqual(self.machine.events[-1].to_state, S.NET)
        self.assertEqual(self.machine.events[-1].desired_keys, (F6,))

    def test_events_are_bounded(self):
        self.build(event_limit=2)
        self.start()                 # 1
        self.enter_land()                    # 2
        self.tick(0.0, fish=T.TRUE, bar=2, ring=0.10)  # 3 -> FIGHT
        for _ in range(3):
            self.tick(0.0, fish=T.TRUE, bar=2, ring=0.985)  # 4 -> LAND
        self.assertEqual(len(self.machine.events), 2)
        self.assertIs(self.machine.events[-1].to_state, S.LAND)
        self.assertIs(self.machine.events[-1].from_state, S.FIGHT)

    def test_stop_event_has_empty_desired(self):
        self.build()
        self.start()
        self.machine.stop()
        ev = self.machine.events[-1]
        self.assertIs(ev.to_state, S.STOPPED)
        self.assertEqual(ev.desired_keys, ())
        self.assertEqual(ev.reason, "stop")


if __name__ == "__main__":
    unittest.main()
