"""全流程协调器的纯离线测试：无截图、无窗口、无真实输入、无网络。

运行：
    python -m unittest tests.test_lifecycle -v
"""

from __future__ import annotations

import pathlib
import sys
import unittest

BOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT))

import autofish_fight_machine as afm  # noqa: E402
import autofish_lifecycle as alf  # noqa: E402
import autofish_observation as aobs  # noqa: E402

T = aobs.TriState
S = alf.AutoFishState
M = alf.ControlMode
F5 = "f5"
F6 = "f6"


class Clock:
    """严格递增的假时钟；观测时间戳与 FSM 使用同一时间轴。"""

    def __init__(self, now: float = 1000.0, step: float = 0.02):
        self.now = now
        self.step = step

    def __call__(self) -> float:
        self.now = round(self.now + self.step, 9)
        return self.now


class FakeSink:
    def __init__(self, fail_ups: int = 0):
        self.calls: list[tuple[str, bool, str]] = []
        self.fail_ups = fail_ups
        self.up_attempts = 0

    def __call__(self, key, is_down, reason):
        if key not in (F5, F6):
            raise AssertionError("unexpected key %r" % (key,))
        self.calls.append((key, is_down, reason))
        if not is_down:
            self.up_attempts += 1
            if self.fail_ups:
                self.fail_ups -= 1
                raise RuntimeError("keyUp failed")
        return None

    def held(self) -> list[str]:
        held: list[str] = []
        for key, down, _ in self.calls:
            if down and key not in held:
                held.append(key)
            if not down and key in held:
                held.remove(key)
        return held


def make_obs(clock: Clock, **fields):
    values = {
        "foreground": True,
        "fish_icon": T.UNKNOWN,
        "bar_level": None,
        "bar_tier": None,
        "ring_ratio": None,
        "captured": T.UNKNOWN,
        "ok_modal": T.UNKNOWN,
        "hud_visible": T.UNKNOWN,
    }
    values.update(fields)
    return aobs.Observation(
        monotonic_ts=clock(),
        wall_ts=0.0,
        bar_hue=None,
        bar_sat=None,
        evidence={},
        **values,
    )


class Harness:
    def __init__(
        self,
        *,
        initial_auto: bool = True,
        readiness=True,
        config: alf.LifecycleConfig | None = None,
        sink: FakeSink | None = None,
        trust_calibration: bool = True,
    ):
        self.clock = Clock()
        self.sink = sink or FakeSink()
        self.frame: dict = {}
        self.calls: list[tuple] = []
        self.logs: list[str] = []
        self.errors: dict[str, BaseException] = {}
        self.readiness_value = readiness
        self.calibrate_result = None
        self.cast_result = None
        self.cast_stop = False
        self.keep_result = True
        self.dismiss_result = True
        self.retrieve_result = None
        self.keepnet_full = False
        self.friction = {"cur": 17, "calibrated": False}
        self.trust_calibration = trust_calibration
        self.friction_levels: list[int | None] = []
        self.net_actions = 0
        self.net_retract_actions = 0
        self.cast_control_states: list[object] = []

        self.callbacks = alf.RuntimeCallbacks(
            capture_observation=self._capture,
            input_sink=self.sink,
            readiness=self._readiness,
            calibrate=self._calibrate,
            cast=self._cast,
            keep=self._keep,
            dismiss=self._dismiss,
            retrieve=self._retrieve,
            net_action=self._net_action,
            net_retract_action=self._net_retract_action,
            friction_step=self._friction_step,
            friction_snapshot=lambda: dict(self.friction),
            keepnet_full=lambda: self.keepnet_full,
            log=self.logs.append,
            clock=self.clock,
        )
        self.coord = alf.LifecycleCoordinator(
            self.callbacks,
            config=config or alf.LifecycleConfig(),
            initial_auto=initial_auto,
            epoch="test-epoch",
        )

    # ---- 回调 ----
    def _capture(self):
        return make_obs(self.clock, **self.frame)

    def _readiness(self, obs):
        value = self.readiness_value
        return value(obs) if callable(value) else value

    def _fail(self, name: str) -> None:
        error = self.errors.get(name)
        if error is not None:
            raise error

    def _calibrate(self, control, fight=False):
        self.calls.append(("calibrate", bool(fight)))
        self._fail("calibrate")
        result = self.calibrate_result
        if result is not False and self.trust_calibration:
            # 模拟外部适配层在校准成功后自己报告 calibrated=True；
            # 协调器不得在内部把 False 覆盖成 True。
            self.friction["calibrated"] = True
        return result

    def _cast(self, control):
        self.calls.append(("cast",))
        self.cast_control_states.append(control.state)
        self._fail("cast")
        if self.cast_stop:
            self.coord.control.request_stop("operator")
        return self.cast_result

    def _keep(self, control):
        self.calls.append(("keep",))
        self._fail("keep")
        return self.keep_result

    def _dismiss(self, control):
        self.calls.append(("dismiss",))
        self._fail("dismiss")
        return self.dismiss_result

    def _retrieve(self, control):
        self.calls.append(("retrieve",))
        self._fail("retrieve")
        return self.retrieve_result

    def _net_action(self):
        self.net_actions += 1
        self._fail("net")

    def _net_retract_action(self):
        self.net_retract_actions += 1
        self._fail("net_retract")

    def _friction_step(self, level):
        self.friction_levels.append(level)

    # ---- 驱动 ----
    def set_frame(self, **fields) -> None:
        self.frame = dict(fields)

    def step(self, **fields) -> dict:
        if fields:
            self.set_frame(**fields)
        return self.coord.step()

    def count(self, name: str) -> int:
        return sum(1 for call in self.calls if call[0] == name)

    def advance_to_ready(self) -> None:
        self.step()  # BOOT -> PROBING
        self.step(fish_icon=T.FALSE)
        assert self.coord.state is S.READY, self.coord.state

    def advance_to_cast(self) -> None:
        self.advance_to_ready()
        self.step()  # READY -> CALIBRATING
        self.step()  # calibrate -> CASTING
        self.step()  # cast -> WAIT_BITE
        assert self.coord.state is S.WAIT_BITE, self.coord.state

    def advance_to_fight(self) -> None:
        self.advance_to_cast()
        self.step(fish_icon=T.TRUE, ring_ratio=0.5)
        self.step(fish_icon=T.TRUE, ring_ratio=0.5)
        assert self.coord.state is S.FIGHT, self.coord.state


class WholeFishTests(unittest.TestCase):
    def test_whole_fish_sequence_and_single_keep(self) -> None:
        h = Harness()
        h.advance_to_cast()
        self.assertEqual(h.count("calibrate"), 1)
        self.assertEqual(h.calls[0], ("calibrate", False))  # 每竿先校准
        self.assertEqual(h.count("cast"), 1)
        self.assertEqual(h.coord.fish_id is not None, True)

        h.step(fish_icon=T.TRUE, ring_ratio=0.5)
        h.step(fish_icon=T.TRUE, ring_ratio=0.5)
        self.assertIs(h.coord.state, S.FIGHT)
        self.assertEqual(sorted(h.coord.arbiter.emitted), [F5, F6])

        # 强捕获覆盖 HUD 未知：2 帧后停子机转 SETTLE。
        h.step(captured=T.TRUE, hud_visible=T.UNKNOWN)
        self.assertIs(h.coord.state, S.FIGHT)
        h.step(captured=T.TRUE, hud_visible=T.UNKNOWN)
        self.assertIs(h.coord.state, S.SETTLE)
        self.assertEqual(h.coord.arbiter.emitted, frozenset())

        snap = h.step(captured=T.TRUE, hud_visible=T.UNKNOWN)
        self.assertIs(h.coord.state, S.PROBING)
        self.assertEqual(snap["kept"], 1)
        self.assertEqual(h.count("keep"), 1)

        # 结算画面持续存在也不能重复点击/重复计数，也不能立刻重抛。
        for _ in range(4):
            h.step(captured=T.TRUE, hud_visible=T.UNKNOWN)
        self.assertEqual(h.count("keep"), 1)
        self.assertEqual(h.count("cast"), 1)
        self.assertEqual(h.coord.kept, 1)
        self.assertEqual(h.coord.arbiter.emitted, frozenset())
        self.assertEqual(h.coord.arbiter.desired, frozenset())

    def test_fight_takeover_calibrates_fight_true_when_baseline_unknown(self) -> None:
        h = Harness()
        h.step()  # BOOT
        h.step(fish_icon=T.FALSE, ring_ratio=0.42)  # 线已在水里 -> WAIT_BITE
        self.assertIs(h.coord.state, S.WAIT_BITE)

        h.step(fish_icon=T.TRUE, ring_ratio=0.42)
        h.step(fish_icon=T.TRUE, ring_ratio=0.42)
        self.assertIs(h.coord.state, S.CALIBRATING)  # 未知基线，先安全校准
        h.step(fish_icon=T.TRUE, ring_ratio=0.42)
        self.assertIs(h.coord.state, S.FIGHT)
        self.assertIn(("calibrate", True), h.calls)
        self.assertEqual(h.count("calibrate"), 1)

    def test_friction_controller_only_when_machine_allows(self) -> None:
        h = Harness()
        h.advance_to_fight()
        # HUD 不可见 -> 子机冻结 -> 传 None 清窗口。
        h.step(fish_icon=T.TRUE, ring_ratio=0.5, hud_visible=T.FALSE, bar_level=2)
        self.assertEqual(h.friction_levels[-1], None)
        # 新鲜可见 + 键已确认 -> 才允许喂档位。
        h.step(fish_icon=T.TRUE, ring_ratio=0.5, hud_visible=T.TRUE, bar_level=1)
        self.assertEqual(h.friction_levels[-1], 1)

    def test_external_friction_false_never_reports_calibrated(self) -> None:
        h = Harness(trust_calibration=False)
        h.advance_to_cast()
        snap = h.coord.snapshot()
        self.assertFalse(snap["friction"]["calibrated"])
        self.assertEqual(snap["confidence"], 0.0)
        self.assertEqual(h.count("calibrate"), 1)  # 内部校准成功也不覆盖外部 False

    def test_friction_trust_loss_in_fight_faults_without_recalibrating(self) -> None:
        h = Harness()
        h.advance_to_fight()
        calibrations = h.count("calibrate")

        h.friction = {"cur": 17, "calibrated": False}
        snap = h.step(fish_icon=T.TRUE, ring_ratio=0.5)
        self.assertIs(h.coord.state, S.FAULT)
        self.assertIn("friction_untrusted", snap["last_error"])
        self.assertEqual(snap["control_mode"], "BLOCKED")
        self.assertEqual(h.friction_levels[-1], None)  # 立即清控制窗口

        for _ in range(4):
            h.step(fish_icon=T.TRUE, ring_ratio=0.5)
        self.assertIs(h.coord.state, S.FAULT)
        self.assertEqual(h.count("calibrate"), calibrations)  # 不在鱼中循环校准

    def test_keep_opens_single_after_keep_channel_without_readiness(self) -> None:
        # readiness 始终 Unknown：只有"成功入护"这一明确 token 能自动进下一竿。
        h = Harness(readiness=None)
        h.step()  # BOOT -> PROBING
        h.step(fish_icon=T.FALSE, ring_ratio=0.4)  # WAIT_BITE
        h.step(fish_icon=T.TRUE, ring_ratio=0.5)
        h.step(fish_icon=T.TRUE, ring_ratio=0.5)  # -> CALIBRATING
        h.step(fish_icon=T.TRUE, ring_ratio=0.5)  # calibrate(fight=True) -> FIGHT

        h.step(captured=T.TRUE)
        h.step(captured=T.TRUE)  # -> SETTLE
        h.step(captured=T.TRUE)  # keep -> PROBING + after_keep pending
        self.assertIs(h.coord.state, S.PROBING)
        self.assertEqual(h.coord.kept, 1)

        h.step(fish_icon=T.FALSE, captured=T.FALSE, ok_modal=T.FALSE)
        self.assertIs(h.coord.state, S.READY)  # confirmed_after_keep 单次许可
        casts_before = h.count("cast")
        h.step()  # -> CALIBRATING（消费许可）
        h.step()  # calibrate -> CASTING
        h.step()  # cast -> WAIT_BITE
        self.assertIs(h.coord.state, S.WAIT_BITE)
        self.assertEqual(h.count("cast"), casts_before + 1)
        self.assertEqual(h.count("calibrate"), 2)

    def test_observe_cancels_after_keep_channel(self) -> None:
        h = Harness(readiness=None)
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.4)
        h.step(fish_icon=T.TRUE, ring_ratio=0.5)
        h.step(fish_icon=T.TRUE, ring_ratio=0.5)
        h.step(fish_icon=T.TRUE, ring_ratio=0.5)
        h.step(captured=T.TRUE)
        h.step(captured=T.TRUE)
        h.step(captured=T.TRUE)  # keep -> PROBING
        casts_before = h.count("cast")
        self.assertEqual(casts_before, 0)  # 首鱼由 ring 接管，没有抛竿

        self.assertEqual(
            h.coord.handle_command({"id": "o", "name": "observe"})["status"], "executed"
        )
        h.coord.handle_command({"id": "r", "name": "resume"})
        for _ in range(6):
            h.step(fish_icon=T.FALSE, captured=T.FALSE, ok_modal=T.FALSE)
        self.assertIs(h.coord.state, S.PROBING)
        self.assertEqual(h.count("cast"), casts_before)  # 人工接管后不许复用旧许可


class StopTests(unittest.TestCase):
    def test_stop_in_fight_releases_and_terminates(self) -> None:
        h = Harness()
        h.advance_to_fight()
        h.coord.control.request_stop("operator")
        snap = h.step(fish_icon=T.TRUE, ring_ratio=0.5)
        self.assertIs(h.coord.state, S.STOPPED)
        self.assertTrue(snap["stopped"])
        self.assertEqual(h.coord.arbiter.emitted, frozenset())
        self.assertEqual(h.sink.held(), [])

    def test_stop_from_fight_retries_release_at_most_three_times(self) -> None:
        sink = FakeSink(fail_ups=99)
        h = Harness(sink=sink)
        h.advance_to_fight()
        h.coord.control.request_stop("operator")
        h.step(fish_icon=T.TRUE, ring_ratio=0.5)
        self.assertIs(h.coord.state, S.STOPPED)
        self.assertEqual(sink.up_attempts, 3)  # 不无限重试

    def test_stop_in_wait_bite_keeps_zero_input(self) -> None:
        h = Harness()
        h.advance_to_cast()
        h.coord.control.request_stop("operator")
        h.step()
        self.assertIs(h.coord.state, S.STOPPED)
        self.assertEqual(h.sink.calls, [])

    def test_stop_after_fish_finishes_current_fish_then_stops(self) -> None:
        h = Harness()
        h.advance_to_fight()
        h.coord.control.request_stop_after_fish()
        h.step(captured=T.TRUE, hud_visible=T.UNKNOWN)
        h.step(captured=T.TRUE, hud_visible=T.UNKNOWN)
        self.assertIs(h.coord.state, S.SETTLE)
        h.step(captured=T.TRUE, hud_visible=T.UNKNOWN)  # keep
        self.assertTrue(h.coord.control.stopped)
        snap = h.step(captured=T.TRUE, hud_visible=T.UNKNOWN)
        self.assertIs(h.coord.state, S.STOPPED)
        self.assertEqual(snap["kept"], 1)
        self.assertEqual(h.count("cast"), 1)  # 停止后不重抛
        self.assertEqual(h.coord.arbiter.emitted, frozenset())

    def test_keepnet_full_stops_without_recast(self) -> None:
        h = Harness()
        h.advance_to_fight()
        h.keepnet_full = True
        h.step(captured=T.TRUE)
        h.step(captured=T.TRUE)
        h.step(captured=T.TRUE)  # keep -> keepnet_full
        snap = h.step(captured=T.TRUE)
        self.assertIs(h.coord.state, S.STOPPED)
        self.assertEqual(snap["kept"], 1)
        self.assertEqual(h.count("cast"), 1)


class ProbeTests(unittest.TestCase):
    def test_black_frame_never_ready(self) -> None:
        h = Harness(readiness=None)
        h.step()
        snap = h.step(fish_icon=T.UNKNOWN, hud_visible=T.UNKNOWN)
        self.assertIs(h.coord.state, S.PROBING)
        self.assertIs(h.coord.mode(), M.BLOCKED)
        self.assertEqual(snap["control_mode"], "BLOCKED")
        for _ in range(4):
            h.step(fish_icon=T.UNKNOWN, hud_visible=T.UNKNOWN)
        self.assertIs(h.coord.state, S.PROBING)
        self.assertEqual(h.count("cast"), 0)

    def test_readiness_false_does_not_cast(self) -> None:
        h = Harness(readiness=False)
        h.step()
        h.step(fish_icon=T.FALSE, hud_visible=T.TRUE)
        self.assertIs(h.coord.state, S.PROBING)
        self.assertEqual(h.count("cast"), 0)

    def test_ring_readable_without_fish_waits_for_bite(self) -> None:
        h = Harness(readiness=True)
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.4, hud_visible=T.TRUE)
        self.assertIs(h.coord.state, S.WAIT_BITE)
        self.assertEqual(h.count("cast"), 0)

    def test_foreground_lost_produces_no_input(self) -> None:
        h = Harness(readiness=True)
        h.step()
        h.step(fish_icon=T.FALSE, foreground=False)
        self.assertIs(h.coord.state, S.PROBING)
        self.assertIs(h.coord.mode(), M.BLOCKED)
        self.assertEqual(h.count("cast"), 0)
        self.assertEqual(h.sink.calls, [])

    def test_strong_captured_overrides_unknown_hud(self) -> None:
        h = Harness()
        h.step()
        h.step(captured=T.TRUE, hud_visible=T.UNKNOWN)
        h.step(captured=T.TRUE, hud_visible=T.UNKNOWN)
        self.assertIs(h.coord.state, S.SETTLE)
        h.step(captured=T.TRUE, hud_visible=T.UNKNOWN)
        self.assertIs(h.coord.state, S.PROBING)
        self.assertEqual(h.coord.kept, 1)

    def test_modal_is_dismissed_then_reprobed(self) -> None:
        h = Harness()
        h.step()
        h.step(ok_modal=T.TRUE)
        h.step(ok_modal=T.TRUE)
        self.assertIs(h.coord.state, S.MODAL)
        h.step(ok_modal=T.TRUE)
        self.assertIs(h.coord.state, S.PROBING)
        self.assertEqual(h.count("dismiss"), 1)

    def test_modal_from_wait_bite_reprobes_instead_of_resuming_old_actions(self) -> None:
        h = Harness()
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.4)  # WAIT_BITE
        h.step(ok_modal=T.TRUE)
        h.step(ok_modal=T.TRUE)
        self.assertIs(h.coord.state, S.MODAL)
        h.step(ok_modal=T.TRUE)  # dismiss
        self.assertIs(h.coord.state, S.PROBING)
        self.assertIsNone(h.coord.fish_id)
        self.assertEqual(h.count("retrieve"), 0)


class WaitBiteTimeoutTests(unittest.TestCase):
    def test_timeout_retrieves_then_reprobes(self) -> None:
        config = alf.LifecycleConfig(wait_bite_timeout=0.0)
        h = Harness(config=config)
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.4)  # -> WAIT_BITE
        snap = h.step(fish_icon=T.FALSE, ring_ratio=0.4)
        self.assertEqual(h.count("retrieve"), 1)
        self.assertIs(h.coord.state, S.PROBING)
        self.assertIsNone(snap["fish_id"])

    def test_missing_retrieve_never_blind_recasts(self) -> None:
        config = alf.LifecycleConfig(wait_bite_timeout=0.0)
        h = Harness(config=config)
        h.callbacks.retrieve = None
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.4)
        h.step(fish_icon=T.FALSE, ring_ratio=0.4)
        h.step(fish_icon=T.FALSE, ring_ratio=0.4)
        self.assertIs(h.coord.state, S.WAIT_BITE)
        self.assertIs(h.coord.mode(), M.BLOCKED)
        self.assertEqual(h.count("cast"), 0)


class FightStallTests(unittest.TestCase):
    """挂底看门狗：圆环长时间不动就收线重抛，别无限拉扯。"""

    def _enter_fight(self) -> "Harness":
        # 默认配置（240s）先把状态推进到 FIGHT，再换成 0 秒触发看门狗，
        # 避免推进过程中就被看门狗打断。
        h = Harness()
        h.advance_to_fight()
        # 0 表示"关闭"，所以这里用 1ms 让它立刻到点。
        h.coord._config = alf.LifecycleConfig(fight_stall_timeout=0.001)
        return h

    def test_stalled_fight_retrieves_and_reprobes(self) -> None:
        h = self._enter_fight()
        # 第一帧建立基线，第二帧才判定（ring 恒为 0.5 = 挂底没动）
        h.step(fish_icon=T.TRUE, ring_ratio=0.5)
        snap = h.step(fish_icon=T.TRUE, ring_ratio=0.5)
        self.assertEqual(h.count("retrieve"), 1)
        self.assertIs(h.coord.state, S.PROBING)
        self.assertIsNone(snap["fish_id"])

    def test_progressing_fight_is_left_alone(self) -> None:
        h = self._enter_fight()
        # 圆环持续改善（0.50 -> 0.60），说明鱼在往回收，不能当挂底
        h.step(fish_icon=T.TRUE, ring_ratio=0.50)
        h.step(fish_icon=T.TRUE, ring_ratio=0.55)
        snap = h.step(fish_icon=T.TRUE, ring_ratio=0.60)
        self.assertEqual(h.count("retrieve"), 0)
        self.assertIsNotNone(snap["fish_id"])

    def test_disabled_watchdog_never_retrieves(self) -> None:
        h = Harness(config=alf.LifecycleConfig(fight_stall_timeout=0.0))
        h.callbacks  # noqa: B018 - 保持与原测试一致的构造方式
        h.coord._config = alf.LifecycleConfig(
            fight_stall_timeout=0.0, fight_stall_ring_gain=0.0
        )
        h.advance_to_fight()
        h.step(fish_icon=T.TRUE, ring_ratio=0.5)
        self.assertEqual(h.count("retrieve"), 0)


class NetConfirmTests(unittest.TestCase):
    def _in_net(self):
        config = alf.LifecycleConfig(
            fight=afm.FightConfig(
                land_frames=1,
                near_grace=0.0,
                net_window=0.0,
                # 显式打开历史自动抄网语义；生产默认 False，见 AutoNetGateTests。
                auto_net_enabled=True,
            )
        )
        h = Harness(config=config)
        h.friction = {"cur": 17, "calibrated": True}
        h.advance_to_fight()
        h.step(fish_icon=T.TRUE, ring_ratio=0.985, hud_visible=T.TRUE)
        self.assertIs(h.coord.state, S.LAND)
        snap = h.step(fish_icon=T.TRUE, ring_ratio=0.985, hud_visible=T.TRUE)
        self.assertIs(h.coord.state, S.NET)
        self.assertEqual(snap["net_pending"], True)
        self.assertEqual(h.net_actions, 1)
        return h, snap

    def test_confirm_requires_matching_context(self) -> None:
        h, snap = self._in_net()
        base = {
            "epoch": snap["epoch"],
            "state_version": snap["state_version"],
            "fish_id": snap["fish_id"],
        }

        for field in ("epoch", "state_version", "fish_id"):
            bad = dict(base)
            bad[field] = "stale" if field != "state_version" else base[field] + 1
            ack = h.coord.handle_command(
                {"id": "bad-" + field, "name": "confirm_net_retracted", "payload": bad}
            )
            self.assertEqual(ack["status"], "rejected", field)
            self.assertIs(h.coord.state, S.NET)

        ack = h.coord.handle_command(
            {"id": "ok", "name": "confirm_net_retracted", "payload": base}
        )
        self.assertEqual(ack["status"], "executed")
        self.assertIs(h.coord.state, S.FIGHT)
        self.assertIs(h.coord.machine.state, afm.FightState.FIGHT)
        self.assertEqual(h.net_actions, 1)  # 不自动再发 F7

    def test_net_never_times_out_to_stop(self) -> None:
        h, _snap = self._in_net()
        for _ in range(20):
            h.step(fish_icon=T.FALSE, ring_ratio=None, hud_visible=T.TRUE)
        self.assertIs(h.coord.state, S.NET)
        self.assertIsNot(h.coord.state, S.STOPPED)
        self.assertEqual(h.net_actions, 1)

    def _context(self, h):
        snap = h.coord.snapshot()
        return {
            "epoch": snap["epoch"],
            "state_version": snap["state_version"],
            "fish_id": snap["fish_id"],
        }

    def test_confirm_rejected_when_foreground_lost(self) -> None:
        h, _snap = self._in_net()
        h.step(fish_icon=T.TRUE, ring_ratio=0.95, foreground=False)
        self.assertIs(h.coord.state, S.NET)
        self.assertIs(h.coord.mode(), M.BLOCKED)

        calls_before = list(h.sink.calls)
        ack = h.coord.handle_command(
            {
                "id": "bg",
                "name": "confirm_net_retracted",
                "payload": self._context(h),
            }
        )
        self.assertEqual(ack["status"], "rejected")
        self.assertEqual(h.sink.calls, calls_before)  # 不补发 key-down
        self.assertIs(h.coord.state, S.NET)

    def test_confirm_rejected_while_modal_pending(self) -> None:
        h, _snap = self._in_net()
        h.step(fish_icon=T.TRUE, ring_ratio=0.95, ok_modal=T.TRUE)
        ack = h.coord.handle_command(
            {
                "id": "modal",
                "name": "confirm_net_retracted",
                "payload": self._context(h),
            }
        )
        self.assertEqual(ack["status"], "rejected")
        self.assertEqual(ack["message"], "modal_pending")
        self.assertIs(h.coord.state, S.NET)


class AutoNetGateTests(unittest.TestCase):
    """生产默认：临岸 60s 也不进 NET、不发 F7、不松 F5，且不刷日志。"""

    def _production_harness(self):
        # 生产 bridge 用的就是显式 False；这里再走 LifecycleConfig 默认值路径。
        config = alf.LifecycleConfig(fight=afm.FightConfig(auto_net_enabled=False))
        h = Harness(config=config)
        h.clock.step = 0.3
        h.friction = {"cur": 17, "calibrated": True}
        h.advance_to_fight()
        return h

    def test_constant_ring_985_for_60s_stays_land_without_net(self) -> None:
        h = self._production_harness()
        start = h.clock.now
        ups_before = [c for c in h.sink.calls if not c[1]]
        h.step(fish_icon=T.TRUE, ring_ratio=0.985, hud_visible=T.TRUE)
        # 至少跑满 60 秒的注入时钟（每 step 内 clock 会被调用多次）。
        while h.clock.now - start < 60.0:
            h.step(fish_icon=T.TRUE, ring_ratio=0.985, hud_visible=T.TRUE)

        self.assertIs(h.coord.state, S.LAND)
        self.assertEqual(h.net_actions, 0)
        self.assertEqual(h.coord.machine.net_attempted, False)
        self.assertEqual(sorted(h.coord.arbiter.desired), [F5, F6])
        self.assertEqual(sorted(h.coord.arbiter.emitted), [F5, F6])
        # 进 FIGHT 之后没有任何 key-up（F5 从未被松开）。
        self.assertEqual(
            [c for c in h.sink.calls if not c[1]][len(ups_before):], []
        )

        snap = h.coord.snapshot()
        self.assertEqual(
            snap["reason"], "临岸候选，自动抄网待验证；继续收线提竿"
        )
        self.assertEqual(snap["reason_code"], "near_net_disabled")
        self.assertEqual(snap["desired_keys"], [F5, F6])

    def test_reason_and_logs_do_not_repeat_every_tick(self) -> None:
        h = self._production_harness()
        start = h.clock.now
        while h.clock.now - start < 12.0:
            h.step(fish_icon=T.TRUE, ring_ratio=0.985, hud_visible=T.TRUE)
        self.assertIs(h.coord.state, S.LAND)
        # 原因已切换，但没有任何 "near_net_disabled" 日志；LAND 迁移只记一次。
        self.assertEqual(
            [line for line in h.logs if "near_net_disabled" in line], []
        )
        self.assertEqual(
            len([line for line in h.logs if "FIGHT -> LAND" in line]), 1
        )
        logs_before = len(h.logs)
        for _ in range(100):
            h.step(fish_icon=T.TRUE, ring_ratio=0.985, hud_visible=T.TRUE)
        self.assertEqual(len(h.logs), logs_before)

    def test_stop_from_disabled_land_releases_both_keys(self) -> None:
        h = self._production_harness()
        for _ in range(100):
            h.step(fish_icon=T.TRUE, ring_ratio=0.985, hud_visible=T.TRUE)
            if h.coord.machine.auto_net_blocked:
                break
        self.assertIs(h.coord.state, S.LAND)
        self.assertTrue(h.coord.machine.auto_net_blocked)
        h.coord.stop_now("test_stop")
        self.assertIs(h.coord.state, S.STOPPED)
        self.assertEqual(h.coord.arbiter.emitted, frozenset())


class CommandTests(unittest.TestCase):
    def test_auto_requested_stays_true_while_foreground_blocks_mode(self) -> None:
        h = Harness(initial_auto=True)
        h.step()  # BOOT -> PROBING
        h.step(foreground=False)
        self.assertIs(h.coord.mode(), M.BLOCKED)
        self.assertTrue(h.coord.auto_requested)
        self.assertIs(h.coord.requested_mode, M.AUTO)

    def test_auto_requested_follows_explicit_resume_and_observe(self) -> None:
        h = Harness(initial_auto=False)
        h.step()  # BOOT -> PROBING
        h.step()  # 采一帧，resume 才能确认新鲜前台
        self.assertFalse(h.coord.auto_requested)

        ack = h.coord.handle_command({"id": "r", "name": "resume"})
        self.assertEqual(ack["status"], "executed")
        self.assertTrue(h.coord.auto_requested)

        ack = h.coord.handle_command({"id": "o", "name": "observe"})
        self.assertEqual(ack["status"], "executed")
        self.assertFalse(h.coord.auto_requested)
        self.assertIs(h.coord.requested_mode, M.OBSERVE)

    def test_resume_waits_for_foreground_then_acks_executed(self) -> None:
        h = Harness(initial_auto=False, readiness=True)
        h.step()  # BOOT -> PROBING
        h.step(foreground=False)
        self.assertIs(h.coord.mode(), M.OBSERVE)

        ack = h.coord.handle_command({"id": "r1", "name": "resume"})
        self.assertEqual(ack["status"], "accepted")
        self.assertIs(h.coord.mode(), M.BLOCKED)  # 已请求 AUTO，但前台未确认

        h.step(foreground=True)
        acks = h.coord.take_acks()
        statuses = [(a["id"], a["status"]) for a in acks]
        self.assertIn(("r1", "accepted"), statuses)
        self.assertIn(("r1", "executed"), statuses)
        self.assertIs(h.coord.mode(), M.AUTO)

    def test_observe_releases_and_switches_mode(self) -> None:
        h = Harness()
        h.advance_to_fight()
        ack = h.coord.handle_command({"id": "o1", "name": "observe"})
        self.assertEqual(ack["status"], "executed")
        self.assertIs(h.coord.state, S.PROBING)
        self.assertIs(h.coord.mode(), M.OBSERVE)
        self.assertEqual(h.coord.arbiter.emitted, frozenset())
        self.assertEqual(h.sink.held(), [])

    def test_confirm_ready_is_gated_by_fresh_foreground(self) -> None:
        h = Harness(initial_auto=False)
        h.step()
        h.step(fish_icon=T.TRUE)
        ack = h.coord.handle_command({"id": "c1", "name": "confirm_ready"})
        self.assertEqual(ack["status"], "rejected")
        self.assertIn(ack["message"], {"fish_present", "foreground_required"})

        h.step(fish_icon=T.FALSE, ring_ratio=None)
        ack = h.coord.handle_command({"id": "c2", "name": "confirm_ready"})
        self.assertEqual(ack["status"], "executed")
        self.assertIs(h.coord.state, S.READY)

    def test_confirm_ready_rejects_readable_line(self) -> None:
        h = Harness(initial_auto=False)
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.3)
        ack = h.coord.handle_command({"id": "c3", "name": "confirm_ready"})
        self.assertEqual(ack["status"], "rejected")
        self.assertEqual(ack["message"], "line_out")

    def test_single_readiness_frame_does_not_survive_observe_resume(self) -> None:
        h = Harness(initial_auto=True, readiness=True)
        h.step()  # BOOT -> PROBING
        h.step(fish_icon=T.FALSE)  # 仅本帧 readiness=True -> READY
        self.assertIs(h.coord.state, S.READY)

        self.assertEqual(
            h.coord.handle_command({"id": "o", "name": "observe"})["status"], "executed"
        )
        self.assertIs(h.coord.state, S.PROBING)
        h.readiness_value = None  # 重新探针未知
        self.assertEqual(
            h.coord.handle_command({"id": "r", "name": "resume"})["status"], "executed"
        )
        for _ in range(6):
            h.step(fish_icon=T.FALSE, captured=T.FALSE, ok_modal=T.FALSE)
        self.assertIs(h.coord.state, S.PROBING)
        self.assertEqual(h.count("cast"), 0)
        self.assertEqual(h.count("calibrate"), 0)

    def test_confirm_ready_is_single_use_not_permanent(self) -> None:
        config = alf.LifecycleConfig(wait_bite_timeout=0.0)
        h = Harness(config=config, readiness=None)
        h.step()  # BOOT -> PROBING
        h.step(fish_icon=T.FALSE)
        ack = h.coord.handle_command({"id": "c", "name": "confirm_ready"})
        self.assertEqual(ack["status"], "executed")
        self.assertIs(h.coord.state, S.READY)

        h.step()  # READY -> CALIBRATING（许可被消费）
        h.step()  # calibrate -> CASTING
        h.step()  # cast -> WAIT_BITE
        self.assertIs(h.coord.state, S.WAIT_BITE)
        self.assertEqual(h.count("cast"), 1)

        h.step(fish_icon=T.FALSE, ring_ratio=0.4)  # 超时收线 -> PROBING
        self.assertIs(h.coord.state, S.PROBING)
        for _ in range(6):
            h.step(fish_icon=T.FALSE, captured=T.FALSE, ok_modal=T.FALSE)
        self.assertIs(h.coord.state, S.PROBING)
        self.assertEqual(h.count("cast"), 1)  # 一次确认不会永久授权

    def test_unknown_command_rejected(self) -> None:
        h = Harness()
        ack = h.coord.handle_command({"id": "x", "name": "teleport"})
        self.assertEqual(ack["status"], "rejected")


    def test_require_confirm_blocks_automatic_ready(self) -> None:
        config = alf.LifecycleConfig(require_confirm=True)
        h = Harness(config=config, readiness=True)
        h.step()
        h.step(fish_icon=T.FALSE)
        self.assertIs(h.coord.state, S.PROBING)
        self.assertIs(h.coord.mode(), M.BLOCKED)
        self.assertEqual(h.count("cast"), 0)

        ack = h.coord.handle_command({"id": "c", "name": "confirm_ready"})
        self.assertEqual(ack["status"], "executed")
        self.assertIs(h.coord.state, S.READY)


class FaultTests(unittest.TestCase):
    def test_callback_exception_faults_and_releases(self) -> None:
        h = Harness()
        h.advance_to_ready()
        h.step()  # READY -> CALIBRATING
        h.errors["calibrate"] = RuntimeError("boom")
        snap = h.step()
        self.assertIs(h.coord.state, S.FAULT)
        self.assertIn("calibration_error", snap["last_error"])
        self.assertEqual(h.coord.arbiter.emitted, frozenset())
        self.assertEqual(h.coord.arbiter.desired, frozenset())
        # FAULT 之后仍然零输入，只有 resume 能重新探针。
        h.step(fish_icon=T.FALSE)
        self.assertIs(h.coord.state, S.FAULT)
        ack = h.coord.handle_command({"id": "r", "name": "resume"})
        self.assertEqual(ack["status"], "executed")
        self.assertIs(h.coord.state, S.PROBING)
        h.step(fish_icon=T.FALSE)
        self.assertIs(h.coord.state, S.READY)

    def test_cast_exception_does_not_keep_keys(self) -> None:
        h = Harness()
        h.advance_to_ready()
        h.step()  # -> CALIBRATING
        h.step()  # calibrate -> CASTING
        h.errors["cast"] = RuntimeError("cast failed")
        snap = h.step()
        self.assertIs(h.coord.state, S.FAULT)
        self.assertEqual(h.coord.arbiter.emitted, frozenset())
        self.assertIn("cast_error", snap["last_error"])

    def test_cast_false_faults_without_ready_recalibration(self) -> None:
        h = Harness()
        h.advance_to_ready()
        h.step()  # READY -> CALIBRATING
        h.step()  # calibrate -> CASTING
        h.cast_result = False
        snap = h.step()  # cast False
        self.assertIs(h.coord.state, S.FAULT)
        self.assertEqual(snap["last_error"], "cast_failed")
        self.assertIsNone(h.coord._ready_grant)
        self.assertEqual(h.count("cast"), 1)
        self.assertEqual(h.count("calibrate"), 1)

        # 失败后不能自动回 READY 再校准/重抛。
        for _ in range(8):
            h.step(fish_icon=T.FALSE, ring_ratio=None)
        self.assertIs(h.coord.state, S.FAULT)
        self.assertEqual(h.count("cast"), 1)
        self.assertEqual(h.count("calibrate"), 1)

    def test_cast_false_with_stop_request_terminates_stopped(self) -> None:
        h = Harness()
        h.advance_to_ready()
        h.step()  # -> CALIBRATING
        h.step()  # calibrate -> CASTING
        h.cast_stop = True
        h.cast_result = False
        snap = h.step()
        self.assertIs(h.coord.state, S.STOPPED)
        self.assertTrue(snap["stopped"])
        self.assertEqual(h.coord.arbiter.emitted, frozenset())
        self.assertEqual(h.sink.held(), [])

    def test_stop_after_cast_failure_still_terminates(self) -> None:
        h = Harness()
        h.advance_to_ready()
        h.step()
        h.step()
        h.cast_result = False
        h.step()
        self.assertIs(h.coord.state, S.FAULT)

        h.coord.control.request_stop("operator")
        h.step()
        self.assertIs(h.coord.state, S.STOPPED)
        self.assertEqual(h.coord.arbiter.emitted, frozenset())
        self.assertEqual(h.sink.held(), [])

    def test_keep_false_is_fault_and_not_counted(self) -> None:
        h = Harness()
        h.step()
        h.step(captured=T.TRUE)
        h.step(captured=T.TRUE)
        h.keep_result = False
        snap = h.step(captured=T.TRUE)
        self.assertIs(h.coord.state, S.FAULT)
        self.assertEqual(snap["kept"], 0)
        self.assertEqual(snap["last_error"], "keep_failed")

    def test_fault_release_failure_retries_bounded_without_key_down(self) -> None:
        sink = FakeSink(fail_ups=99)
        h = Harness(sink=sink)
        h.advance_to_fight()
        downs_before = sum(1 for _key, down, _reason in sink.calls if down)

        h.friction = {"cur": 17, "calibrated": False}
        snap = h.step(fish_icon=T.TRUE, ring_ratio=0.5)
        self.assertIs(h.coord.state, S.FAULT)
        self.assertIn("friction_untrusted", snap["last_error"])
        self.assertIn("release_failed", snap["last_error"])

        for _ in range(8):
            h.step(fish_icon=T.TRUE, ring_ratio=0.5)
        self.assertIs(h.coord.state, S.FAULT)
        self.assertEqual(
            sum(1 for _key, down, _reason in sink.calls if down), downs_before
        )  # 故障释放重试绝不再按 key-down
        self.assertIn("release_failed", h.coord.last_error)


class ActionControlTests(unittest.TestCase):
    def test_state_is_live_readonly_coordinator_view(self) -> None:
        from autofish_control import ControlSession

        current = {"state": S.CASTING}
        action = alf.ActionControl(
            ControlSession("s"),
            lambda: 1.0,
            None,
            lambda: current["state"],
        )
        self.assertIs(action.state, S.CASTING)
        current["state"] = S.PROBING
        self.assertIs(action.state, S.PROBING)  # 每次读取都取当前值，不粘滞

        with self.assertRaises(AttributeError):
            action.state = S.READY  # 只读：外部不能伪造授权

    def test_coordinator_hands_cast_action_its_casting_state(self) -> None:
        h = Harness()
        h.advance_to_cast()
        self.assertEqual(h.cast_control_states, [S.CASTING])
        self.assertIs(h.coord.state, S.WAIT_BITE)

    def test_wheel_passthrough_and_stop_cancels(self) -> None:
        from autofish_control import AutoStop, ControlSession

        control = ControlSession("s")
        seen: list[int] = []
        action = alf.ActionControl(control, lambda: 1.0, seen.append)
        action.wheel(-30)
        self.assertEqual(seen, [-30])

        control.request_stop("operator")
        with self.assertRaises(AutoStop):
            action.wheel(16)
        self.assertTrue(action.wait(0.01))


class SnapshotTests(unittest.TestCase):
    def test_snapshot_has_ui_contract_fields(self) -> None:
        h = Harness()
        h.advance_to_cast()
        snap = h.coord.snapshot()
        for field in alf.SNAPSHOT_FIELDS:
            self.assertIn(field, snap, field)
        self.assertEqual(snap["epoch"], "test-epoch")
        self.assertEqual(snap["state"], "WAIT_BITE")
        self.assertEqual(snap["control_mode"], "AUTO")
        self.assertIsInstance(snap["state_version"], int)
        self.assertIsInstance(snap["desired_keys"], list)
        self.assertIsInstance(snap["emitted_keys"], list)
        self.assertIsInstance(snap["friction"], dict)
        self.assertIsNotNone(snap["fish_id"])
        # 状态迁移才增加 version。
        version = snap["state_version"]
        h.step()
        self.assertEqual(h.coord.snapshot()["state_version"], version)


class ProbingRingTests(unittest.TestCase):
    """PROBING 判"线是否在水里"：圆环可读 ≠ 线出去了。

    2026-11-09 实机事故：入护后线收回杯上（ring≈1.0），PROBING 仅凭"环可读"
    就进了 WAIT_BITE，状态机以为在等口、实际没抛竿；50 秒后超时 retrieve 又
    按 F5 变成抛竿蓄力，力度攒到 130% 卡死，最后 FAULT。
    """

    def test_full_ring_with_unknown_readiness_stays_probing(self):
        # 环满 + readiness 读不清 = 什么都不能确定，停在 PROBING
        h = Harness(readiness=None)
        h.step()                                   # BOOT -> PROBING
        h.step(fish_icon=T.FALSE, ring_ratio=1.0)
        self.assertIs(h.coord.state, S.PROBING)

    def test_near_full_ring_with_unknown_readiness_stays_probing(self):
        h = Harness(readiness=None)
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.99)
        self.assertIs(h.coord.state, S.PROBING)

    def test_near_full_ring_with_ready_absent_is_short_throw(self):
        # 短抛：出线不到 3%，但"钓具准备好"提示已经不在了 = 正在等口。
        # 只按环判会一直以为"还没抛"，空转到超时。
        h = Harness(readiness=False)
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.983)
        self.assertIs(h.coord.state, S.WAIT_BITE)

    def test_ring_below_threshold_goes_wait_bite(self):
        h = Harness(readiness=False)
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.93)
        self.assertIs(h.coord.state, S.WAIT_BITE)

    def test_keep_chain_beats_full_ring(self):
        # 刚入护：线上杯、环可读，但必须走 READY 重新抛竿
        h = Harness(readiness=False)
        h.step()
        h.coord._keep_chain_pending = True
        h.step(fish_icon=T.FALSE, ring_ratio=1.0,
               captured=T.FALSE, ok_modal=T.FALSE)
        self.assertIs(h.coord.state, S.READY)

    def test_threshold_stricter_than_bridge_spool_rule(self):
        """两处阈值语义不同，不能合并。

        lifecycle 判"线是不是出去了"（宁宽：短抛 0.983 也算出去）；
        retrieve 判"要不要按 F5"（宁严：只有 ≥0.99 才敢信"在杯上"）。
        """
        import autofish_bridge as ab
        self.assertLess(alf.PROBE_LINE_OUT_MAX_RING, ab.LINE_ON_SPOOL_RING)


    def test_full_ring_in_wait_bite_returns_to_probing(self):
        """等口时圆环贴在满环 = 线其实还在杯上，必须回去重新抛。

        否则会死循环：WAIT_BITE → 50s 超时 → retrieve 收线 → 环回 1.0
        → 仍判"线在水里" → 再 WAIT_BITE……
        """
        h = Harness()
        h.advance_to_cast()
        h.clock.step = 0.2
        for _ in range(25):
            h.step(fish_icon=T.FALSE, ring_ratio=1.0)
        # 会一路走到 PROBING -> READY -> CALIBRATING -> CASTING，总之必须离开 WAIT_BITE
        self.assertIsNot(h.coord.state, S.WAIT_BITE)

    def test_low_ring_in_wait_bite_stays(self):
        # 线真在水里（环低）就该继续等，不能因为"等太久"就跑
        h = Harness()
        h.advance_to_cast()
        h.clock.step = 0.5
        for _ in range(10):
            h.step(fish_icon=T.FALSE, ring_ratio=0.6)
        self.assertIs(h.coord.state, S.WAIT_BITE)


class ProtocolHandoffTests(unittest.TestCase):
    """apply_protocol_handoff：BOOT 时按协议快照跳起点状态。"""

    def test_fighting_enters_fight_without_release(self):
        h = Harness()
        # 先让 arbiter 有一枚"已按下"的键，验证 hold 语义不会把它松掉
        h.coord._arbiter.set_desired({alf.F5}, "fighting")
        h.coord._arbiter.reconcile("fighting")
        applied = h.coord.apply_protocol_handoff(
            {"phase": "fighting", "enter": "FIGHT", "keys": "hold",
             "reason": "protocol_fighting"})
        self.assertTrue(applied)
        self.assertIs(h.coord.state, S.FIGHT)
        # hold = 不动键：F5 仍应按住
        self.assertIn("f5", h.coord._arbiter.emitted)

    def test_line_in_water_enters_wait_bite(self):
        h = Harness()
        applied = h.coord.apply_protocol_handoff(
            {"phase": "line_in_water", "enter": "WAIT_BITE", "keys": "release",
             "reason": "protocol_line_in_water"})
        self.assertTrue(applied)
        self.assertIs(h.coord.state, S.WAIT_BITE)

    def test_casting_taps_f5_then_probing(self):
        h = Harness()
        applied = h.coord.apply_protocol_handoff(
            {"phase": "casting", "enter": "PROBING", "keys": "tap_f5",
             "reason": "protocol_casting"})
        self.assertTrue(applied)
        self.assertIs(h.coord.state, S.PROBING)
        # tap 序列：按下→(等待)→释放；结束后不应残留按住的键
        f5_events = [(c[0], c[1]) for c in h.sink.calls if c[0] == "f5"]
        self.assertEqual(f5_events, [("f5", True), ("f5", False)])
        self.assertEqual(h.coord._arbiter.emitted, frozenset())

    def test_unknown_returns_false_and_boot_tick_proceeds(self):
        h = Harness()
        applied = h.coord.apply_protocol_handoff(
            {"phase": "unknown", "enter": "PROBING", "keys": "release",
             "reason": "handoff_unknown"})
        self.assertFalse(applied)
        # 默认路径照走：BOOT → PROBING
        h.step()
        self.assertIs(h.coord.state, S.PROBING)
        # 元数据仍被记录（诊断用）
        self.assertEqual(h.coord._protocol_handoff["phase"], "unknown")

    def test_only_from_boot(self):
        h = Harness()
        h.step()   # 已离开 BOOT
        applied = h.coord.apply_protocol_handoff(
            {"phase": "fighting", "enter": "FIGHT", "keys": "hold",
             "reason": "protocol_fighting"})
        self.assertFalse(applied)
        self.assertIsNot(h.coord.state, S.FIGHT)

    def test_bad_action_dict_is_ignored(self):
        h = Harness()
        self.assertFalse(h.coord.apply_protocol_handoff(None))
        self.assertFalse(h.coord.apply_protocol_handoff({}))
        self.assertIs(h.coord.state, S.BOOT)

    def test_snapshot_exposes_handoff(self):
        h = Harness()
        h.coord.apply_protocol_handoff(
            {"phase": "fighting", "enter": "FIGHT", "keys": "hold",
             "reason": "protocol_fighting"})
        snap = h.coord.snapshot()
        self.assertEqual(snap["protocol_handoff"]["phase"], "fighting")
        self.assertEqual(snap["protocol_handoff"]["reason"], "protocol_fighting")



class ProtocolFightFallbackTests(unittest.TestCase):
    """protocol_fight 补盲区：图标不亮时也必须进 FIGHT。"""

    def _harness(self, verdict):
        h = Harness()
        h.callbacks.protocol_fight = lambda: verdict
        return h

    def test_probing_enters_fight_on_protocol(self):
        h = self._harness(True)
        h.step(fish_icon=T.FALSE, ring_ratio=0.95)   # BOOT -> PROBING
        h.step(fish_icon=T.FALSE, ring_ratio=0.95)
        # 未校准 → 先 CALIBRATING（reason 会被 fight_needs_calibration 覆盖，
        # 来源看日志里的 (protocol_fight)）
        self.assertIn(h.coord.state, (S.CALIBRATING, S.FIGHT))
        self.assertTrue(any("protocol_fight" in line for line in h.logs), h.logs)

    def test_wait_bite_enters_fight_on_protocol(self):
        h = self._harness(False)
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.93)   # → WAIT_BITE
        self.assertIs(h.coord.state, S.WAIT_BITE)
        h.callbacks.protocol_fight = lambda: True
        h.step(fish_icon=T.FALSE, ring_ratio=0.93)
        self.assertIn(h.coord.state, (S.CALIBRATING, S.FIGHT))
        self.assertTrue(any("protocol_fight" in line for line in h.logs), h.logs)

    def test_no_protocol_keeps_visual_behavior(self):
        h = self._harness(None)
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.93)
        self.assertIs(h.coord.state, S.WAIT_BITE)

    def test_callback_error_is_swallowed(self):
        h = Harness()
        def boom():
            raise RuntimeError("snap broken")
        h.callbacks.protocol_fight = boom
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.93)
        self.assertIs(h.coord.state, S.WAIT_BITE)

    def test_quiet_window_blocks_protocol_fight(self):
        """静默期内 protocol_fight 一律 False（防止入护后立刻重新开战）。"""
        h = Harness()
        h.callbacks.protocol_fight = lambda: True
        h.coord._protocol_quiet_until = h.coord._now() + 4.0
        self.assertFalse(h.coord._protocol_fight())

    def test_leaving_fight_starts_quiet_window(self):
        h = Harness()
        h.friction["calibrated"] = True      # 已校准 → 直接进 FIGHT
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.93)
        h.step(fish_icon=T.TRUE)
        h.step(fish_icon=T.TRUE)
        self.assertIn(h.coord.state, (S.FIGHT, S.CALIBRATING))
        before = h.coord._protocol_quiet_until
        # 结算也要连续 confirm_frames 帧才成立
        h.step(fish_icon=T.TRUE, captured=T.TRUE)
        h.step(fish_icon=T.FALSE, captured=T.TRUE)
        self.assertNotIn(h.coord.state, (S.FIGHT, S.LAND, S.NET))
        self.assertGreater(h.coord._protocol_quiet_until, before)

    def test_quiet_period_expires(self):
        h = Harness()
        h.callbacks.protocol_fight = lambda: True
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.93)
        h.coord._protocol_quiet_until = 0.0     # 静默期已过
        h.step(fish_icon=T.FALSE, ring_ratio=0.93)
        self.assertIn(h.coord.state, (S.CALIBRATING, S.FIGHT))

    def test_observe_mode_does_not_take_over(self):
        h = Harness(initial_auto=False)
        h.callbacks.protocol_fight = lambda: True
        h.step(fish_icon=T.FALSE, ring_ratio=0.95)
        h.step(fish_icon=T.FALSE, ring_ratio=0.95)
        self.assertIsNot(h.coord.state, S.FIGHT)



class ProtocolLineHomeVetoTests(unittest.TestCase):
    """短抛时环满但线在水里：不能回 PROBING 抖动（会重置超时）。"""

    def test_line_home_vetoed_by_protocol(self):
        h = Harness()
        h.callbacks.protocol_line_in_water = lambda: True
        h.callbacks.protocol_fight = lambda: False
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.93)      # WAIT_BITE
        h.clock.step = 0.5                              # 满环要连续 3 秒
        for _ in range(8):
            h.step(fish_icon=T.FALSE, ring_ratio=1.0)
        self.assertIs(h.coord.state, S.WAIT_BITE)

    def test_line_home_without_protocol_still_returns(self):
        h = Harness()
        h.callbacks.protocol_line_in_water = lambda: None
        h.callbacks.protocol_fight = lambda: None
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.93)
        h.clock.step = 0.5
        for _ in range(10):
            h.step(fish_icon=T.FALSE, ring_ratio=1.0)
        self.assertIs(h.coord.state, S.PROBING)


if __name__ == "__main__":
    unittest.main()


class AutoNetSessionTests(unittest.TestCase):
    """中鱼确认后的 NET 会话：掏网一次、低环自动收网、回 FIGHT 双键。"""

    def _h(self, **fight):
        values = dict(
            land_frames=1,
            near_grace=0.0,
            net_exit_frames=3,
            net_retract_anim=0.1,
            net_cooldown=0.0,
            auto_net_enabled=True,
        )
        values.update(fight)
        h = Harness(config=alf.LifecycleConfig(
            fight=afm.FightConfig(**values)
        ))
        h.friction = {"cur": 17, "calibrated": True}
        h.advance_to_fight()
        return h

    def _enter_net(self, h, ring=0.985):
        before = h.net_actions
        if h.coord.state is S.FIGHT:
            h.step(fish_icon=T.TRUE, ring_ratio=ring, hud_visible=T.TRUE)
            self.assertIs(h.coord.state, S.LAND)
        if h.coord.state is S.LAND:
            snap = h.step(fish_icon=T.TRUE, ring_ratio=ring, hud_visible=T.TRUE)
        else:
            snap = h.coord.snapshot()
        self.assertIs(h.coord.state, S.NET)
        self.assertEqual(h.net_actions, before + 1)
        return snap

    def test_wait_bite_ring_933_never_lands_or_nets(self):
        h = Harness()
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.933, hud_visible=T.TRUE)
        self.assertIs(h.coord.state, S.WAIT_BITE)
        for _ in range(50):
            h.step(fish_icon=T.FALSE, ring_ratio=0.933, hud_visible=T.TRUE)
        self.assertIs(h.coord.state, S.WAIT_BITE)
        self.assertEqual(h.net_actions, 0)
        self.assertEqual(h.net_retract_actions, 0)

    def test_fish_confirmed_land_net_and_auto_retract_restores_f5(self):
        h = self._h()
        self.assertIs(h.coord.state, S.FIGHT)
        snap = h.step(fish_icon=T.TRUE, ring_ratio=0.985, hud_visible=T.TRUE)
        self.assertIs(h.coord.state, S.LAND)
        self.assertEqual(snap["net_state"], "stowed")
        self.assertEqual(snap["net_state_basis"], "assumed_new_session")

        snap = self._enter_net(h)
        self.assertEqual(snap["net_state"], "deployed")
        self.assertEqual(snap["net_state_basis"], "commanded_deploy")
        self.assertEqual(sorted(h.coord.arbiter.emitted), [F6])
        self.assertIn((F5, False), [(k, d) for k, d, _ in h.sink.calls])

        calibrations = h.count("calibrate")
        for ring in (0.85, 0.65):
            with self.subTest(ring=ring):
                retracts_before = h.net_retract_actions
                for _ in range(3):
                    h.step(fish_icon=T.TRUE, ring_ratio=ring, hud_visible=T.TRUE)
                self.assertEqual(h.net_retract_actions, retracts_before + 1, ring)
                self.assertIs(h.coord.state, S.NET)
                self.assertEqual(sorted(h.coord.arbiter.emitted), [F6])

                for _ in range(20):
                    h.step(fish_icon=T.TRUE, ring_ratio=ring, hud_visible=T.TRUE)
                    if h.coord.state is S.FIGHT:
                        break
                self.assertIs(h.coord.state, S.FIGHT, ring)
                self.assertEqual(sorted(h.coord.arbiter.emitted), [F5, F6])
                self.assertEqual(h.coord.snapshot()["net_state"], "stowed")
                self.assertEqual(
                    h.coord.snapshot()["net_state_basis"], "commanded_retract"
                )
                # 收网不是重新校准：cur 保持 17，没有新增 calibrate。
                self.assertEqual(h.coord.snapshot()["friction"]["cur"], 17)
                self.assertEqual(h.count("calibrate"), calibrations)
                self.assertEqual(h.coord.snapshot()["net_pending"], False)

                # 为下一轮重新进 NET。
                self._enter_net(h, ring=0.985)

    def test_capture_strong_confirmation_wins_over_retraction(self):
        h = self._h()
        self._enter_net(h)
        h.step(fish_icon=T.TRUE, ring_ratio=0.85, hud_visible=T.TRUE)
        h.step(fish_icon=T.TRUE, ring_ratio=0.85, hud_visible=T.TRUE)
        # 第一帧 captured 只是候选：不得抢先收网。
        h.step(fish_icon=T.TRUE, ring_ratio=0.85, hud_visible=T.TRUE,
               captured=T.TRUE)
        self.assertEqual(h.net_retract_actions, 0)
        # 第二帧强捕获确认 -> 直接 SETTLE，不收网。
        h.step(fish_icon=T.TRUE, ring_ratio=0.85, hud_visible=T.TRUE,
               captured=T.TRUE)
        self.assertIs(h.coord.state, S.SETTLE)
        self.assertEqual(h.net_retract_actions, 0)
        self.assertEqual(h.coord.arbiter.emitted, frozenset())

    def test_retract_failure_keeps_f6_and_never_restores_f5(self):
        h = self._h()
        self._enter_net(h)
        h.errors["net_retract"] = RuntimeError("net stuck")
        for _ in range(3):
            h.step(fish_icon=T.TRUE, ring_ratio=0.85, hud_visible=T.TRUE)
        self.assertIs(h.coord.state, S.NET)
        self.assertEqual(h.coord.snapshot()["net_state"], "unknown")
        self.assertEqual(h.coord.snapshot()["net_state_basis"], "unknown_fault")
        self.assertEqual(sorted(h.coord.arbiter.emitted), [F6])
        ups = [c for c in h.sink.calls if not c[1] and c[0] == F5]
        self.assertEqual(len(ups), 1)   # 只有进 NET 时那一次
        for _ in range(10):
            h.step(fish_icon=T.TRUE, ring_ratio=0.30, hud_visible=T.TRUE)
        self.assertEqual(h.net_retract_actions, 1)   # 不重复盲 toggle
        self.assertEqual(sorted(h.coord.arbiter.emitted), [F6])

    def test_deploy_failure_keeps_net_f6_unknown(self):
        h = self._h()
        h.errors["net"] = RuntimeError("net jammed")
        h.step(fish_icon=T.TRUE, ring_ratio=0.985, hud_visible=T.TRUE)
        self.assertIs(h.coord.state, S.LAND)
        h.step(fish_icon=T.TRUE, ring_ratio=0.985, hud_visible=T.TRUE)
        self.assertIs(h.coord.state, S.NET)
        self.assertEqual(h.coord.snapshot()["net_state"], "unknown")
        self.assertEqual(h.coord.snapshot()["net_state_basis"], "unknown_fault")
        self.assertEqual(sorted(h.coord.arbiter.emitted), [F6])

    def test_mid_takeover_never_blind_toggles_net(self):
        values = dict(
            land_frames=1,
            near_grace=0.0,
            auto_net_enabled=True,
        )
        h = Harness(config=alf.LifecycleConfig(
            fight=afm.FightConfig(**values)
        ))
        h.friction = {"cur": 17, "calibrated": True}
        h.step()
        h.step(fish_icon=T.FALSE, ring_ratio=0.4, hud_visible=T.TRUE)
        self.assertIs(h.coord.state, S.WAIT_BITE)
        h.step(fish_icon=T.TRUE, ring_ratio=0.5, hud_visible=T.TRUE)
        h.step(fish_icon=T.TRUE, ring_ratio=0.5, hud_visible=T.TRUE)
        self.assertIs(h.coord.state, S.FIGHT)
        self.assertEqual(h.coord.snapshot()["net_state"], "unknown")
        self.assertEqual(
            h.coord.snapshot()["net_state_basis"], "unknown_mid_takeover"
        )
        h.step(fish_icon=T.TRUE, ring_ratio=0.985, hud_visible=T.TRUE)
        self.assertIs(h.coord.state, S.LAND)
        for _ in range(5):
            h.step(fish_icon=T.TRUE, ring_ratio=0.985, hud_visible=T.TRUE)
        self.assertIs(h.coord.state, S.LAND)
        self.assertEqual(h.net_actions, 0)
        self.assertEqual(sorted(h.coord.arbiter.emitted), [F5, F6])
        self.assertEqual(h.coord.snapshot()["reason_code"], "net_state_unknown")

    def test_hooked_timer_starts_at_fish_not_cast(self):
        h = self._h()
        h.step(fish_icon=T.TRUE, ring_ratio=0.5, hud_visible=T.TRUE)
        h.step(fish_icon=T.TRUE, ring_ratio=0.5, hud_visible=T.TRUE)
        snap = h.coord.snapshot()
        self.assertIsNotNone(snap["hooked_elapsed"])
        self.assertIsNotNone(snap["cast_elapsed"])
        self.assertGreaterEqual(snap["cast_elapsed"], snap["hooked_elapsed"])
