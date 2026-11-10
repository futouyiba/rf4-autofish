"""新搏鱼内核运行层的纯离线测试：无截图、无窗口、无真实输入。

运行：
    python -m unittest discover -s tests -p "test_fight_runtime.py" -v
"""

from __future__ import annotations

import ast
import pathlib
import sys
import unittest
from collections import deque
from contextlib import contextmanager
from types import SimpleNamespace

BOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT))

import autofish_fight_machine as afm  # noqa: E402
import autofish_fight_runtime as afr  # noqa: E402
import autofish_input as ain  # noqa: E402
import autofish_observation as aobs  # noqa: E402

T = aobs.TriState
S = afm.FightState
F5 = ain.F5
F6 = ain.F6
OUT = afr.FightOutcome


class Clock:
    """每次读都前进一点，保证观测时间戳严格递增、且永远"新鲜"。"""

    def __init__(self, now: float = 0.0, step: float = 0.02):
        self.now = now
        self.step = step

    def __call__(self) -> float:
        self.now = round(self.now + self.step, 9)
        return self.now


class FakeSink:
    def __init__(self):
        self.calls: list[tuple[str, bool, str]] = []
        self.fail_ups: dict[str, int] = {}

    def __call__(self, key, is_down, reason):
        if key not in (F5, F6):
            raise AssertionError(f"unexpected key {key!r}")
        if not is_down and self.fail_ups.get(key, 0) > 0:
            self.fail_ups[key] -= 1
            self.calls.append((key, is_down, reason))
            raise RuntimeError(f"keyUp failed: {key}")
        self.calls.append((key, is_down, reason))

    @property
    def downs(self):
        return [k for k, d, _ in self.calls if d]

    @property
    def ups(self):
        return [k for k, d, _ in self.calls if not d]


class Frame:
    def __init__(
        self,
        *,
        fish=T.FALSE,
        bar=2,
        ring=None,
        captured=T.FALSE,
        foreground=True,
        hud=T.TRUE,
        ok=T.FALSE,
        score=0.0,
        size=(1920, 1080),
    ):
        self.fish = fish
        self.bar = bar
        self.ring = ring
        self.captured = captured
        self.foreground = foreground
        self.hud = hud
        self.ok = ok
        self.score = score
        self.size = size


class Image:
    def __init__(self, frame: Frame):
        self.frame = frame
        self.size = frame.size


class Harness:
    def __init__(self, frames=None):
        self.frames = deque(frames or [Frame()])
        self.current = self.frames[0]
        self.images = 0
        self.sink = FakeSink()
        self.clock = Clock()
        self.status = (17, True, False)
        self.wheel_ok = True
        self.wheel_calls: list[int] = []
        self.commits: list[int] = []
        self.friction_steps: list[int | None] = []
        self.friction_begins = 0
        self.net_calls = 0
        self.logs: list[str] = []
        self.cancel_after: int | None = None
        self.fail_observe_at: int | None = None
        self.observe_count = 0
        self.on_observe = None
        self.force_foreground: bool | None = None
        self.on_wheel = None
        self.player = None

    def _next(self) -> Frame:
        if len(self.frames) > 1:
            self.current = self.frames.popleft()
        else:
            self.current = self.frames[0]
        return self.current

    # ---- callbacks ----
    def capture(self):
        self.images += 1
        return Image(self._next())

    def foreground(self):
        if self.force_foreground is not None:
            return self.force_foreground
        return self.current.foreground

    def observe(self, image, foreground, ts):
        self.observe_count += 1
        if (self.fail_observe_at is not None
                and self.observe_count >= self.fail_observe_at):
            raise RuntimeError("observe boom")
        f = image.frame
        if self.on_observe is not None:
            self.on_observe(self, self.observe_count)
        return aobs.Observation(
            monotonic_ts=ts,
            wall_ts=ts,
            foreground=foreground,
            fish_icon=f.fish,
            bar_level=f.bar,
            bar_tier=None,
            bar_hue=None,
            bar_sat=None,
            ring_ratio=f.ring,
            captured=f.captured,
            ok_modal=f.ok,
            hud_visible=f.hud,
            evidence={},
        )

    def strong(self, image):
        return image.frame.score

    def wheel(self, n):
        self.wheel_calls.append(n)
        if self.on_wheel is not None:
            self.on_wheel(self, n)
        return self.wheel_ok

    def commit(self, cur):
        self.commits.append(cur)

    def step(self, level):
        self.friction_steps.append(level)

    def begin(self):
        self.friction_begins += 1

    def net(self):
        self.net_calls += 1

    def cancel(self):
        return (self.cancel_after is not None
                and self.images >= self.cancel_after)

    def callbacks(self) -> afr.FightCallbacks:
        return afr.FightCallbacks(
            capture=self.capture,
            observe=self.observe,
            foreground=self.foreground,
            strong_capture=self.strong,
            sink=self.sink,
            net_action=self.net,
            friction_status=lambda: self.status,
            wheel_raw=self.wheel,
            commit_friction=self.commit,
            friction_step=self.step,
            friction_begin=self.begin,
            clock=self.clock,
            log=self.logs.append,
            frame_valid=lambda image: tuple(image.size) == (1920, 1080),
            cancel=self.cancel,
        )

    def runtime(self, **cfg) -> afr.FightRuntime:
        return afr.FightRuntime(
            self.callbacks(),
            config=afm.FightConfig(**cfg) if cfg else None,
            interval=0.0005,
            heartbeat=0.01,
        )


# ==================== 开关 / 包装 ====================


class FakeDetection:
    def __init__(self, hooked=False):
        self.hooked = hooked
        self.finished_calls = 0

    def is_fish_hooked(self):
        return self.hooked

    def is_retrieval_finished(self):
        self.finished_calls += 1
        raise AssertionError("legacy path must not be reached")


class FakeHookedError(Exception):
    pass


class DummyRuntime:
    def __init__(self, result, events=None):
        self.result = result
        self.runs = 0
        self.events = events

    def run(self):
        self.runs += 1
        if self.events is not None:
            self.events.append(("runtime.run",))
        return self.result


def loose_result(outcome=OUT.LOST) -> afr.FightSessionResult:
    return afr.FightSessionResult(outcome, "test", 0.0, 0, None)


def make_player_cls():
    class FakeTackle:
        def __init__(self, player):
            self.player = player
            self.retrieve_calls = 0
            self.raise_hooked = False

        def retrieve(self):
            self.retrieve_calls += 1
            self.player.events.append(("tackle.retrieve",))
            if self.raise_hooked:
                raise FakeHookedError()

    class FakePlayer:
        def __init__(self):
            self.detection = FakeDetection()
            self.mouse_pressed = False
            self.pull_calls = 0
            self.orig_retrieve_calls = 0
            self.orig_lift_calls = 0
            self.cur_coffee = 13
            self.save_calls = 0
            self.events: list[tuple] = []
            self.tackle = FakeTackle(self)
            self.cfg = None

        @contextmanager
        def hold_keys(self, mouse, shift, reset=False):
            self.events.append(("hold_enter", mouse, shift, reset))
            try:
                yield
            finally:
                self.events.append(("hold_exit", mouse, shift, reset))

        def pull_fish(self, save=True):
            self.pull_calls += 1

        def retrieve_line(self):
            self.orig_retrieve_calls += 1

        def lift_fish(self):
            self.orig_lift_calls += 1

        def save_bite_screenshot(self):
            self.save_calls += 1

    return FakePlayer


class TestFlagAndInstall(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(afr.feature_enabled({}))
        self.assertFalse(afr.feature_enabled({"RF4_FIGHT_FSM": "0"}))
        self.assertTrue(afr.feature_enabled({"RF4_FIGHT_FSM": "1"}))

    def test_docstring_clarifies_per_cast_17_vs_same_fish_reentry(self):
        doc = afr.__doc__ or ""
        # 每竿 cast 仍回到 17；保留 cur 只适用于同一条鱼重入
        self.assertIn("同一条鱼重入", doc)
        self.assertIn("每竿", doc)
        self.assertNotIn("保留当前档位，不回 17", doc)

    def test_disabled_keeps_original_methods(self):
        P = make_player_cls()
        before = (P.pull_fish, P.retrieve_line, P.lift_fish)
        installed = afr.install_player_patches(
            P,
            enabled=False,
            runtime_factory=lambda p: DummyRuntime(loose_result()),
            captured_error=RuntimeError,
        )
        self.assertFalse(installed)
        self.assertIs(P.pull_fish, before[0])
        self.assertIs(P.retrieve_line, before[1])
        self.assertIs(P.lift_fish, before[2])

    def test_retrieve_line_prefers_hooked_and_bypasses_finished(self):
        P = make_player_cls()
        dummy = DummyRuntime(loose_result(OUT.LOST))
        self.assertTrue(afr.install_player_patches(
            P, enabled=True, runtime_factory=lambda p: dummy,
            captured_error=RuntimeError, hooked_error=FakeHookedError))
        p = P()
        p.detection.hooked = True
        p.retrieve_line()
        self.assertEqual(dummy.runs, 1)
        self.assertEqual(p.detection.finished_calls, 0)
        self.assertEqual(p.orig_retrieve_calls, 0)
        # 原 pull 的两项语义必须保留
        self.assertEqual(p.cur_coffee, 0)
        self.assertEqual(p.save_calls, 1)
        # LOST 之后 lift_fish 必须直接 return，不跑旧 30/600s。
        p.lift_fish()
        self.assertEqual(p.orig_lift_calls, 0)
        p.lift_fish()
        self.assertEqual(p.orig_lift_calls, 1)

    def test_no_fish_retrieves_without_original_fallback(self):
        P = make_player_cls()
        dummy = DummyRuntime(loose_result())
        afr.install_player_patches(
            P, enabled=True, runtime_factory=lambda p: dummy,
            captured_error=RuntimeError, hooked_error=FakeHookedError)
        p = P()
        p.detection.hooked = False
        p.retrieve_line()
        self.assertEqual(dummy.runs, 0)
        self.assertEqual(p.orig_retrieve_calls, 0)   # 绝不 fallback 原收线
        self.assertEqual(p.tackle.retrieve_calls, 1)
        self.assertIn(("hold_enter", True, False, False), p.events)
        self.assertIn(("hold_exit", True, False, False), p.events)
        # 没有进入新会话 -> 旧 lift 正常逻辑保留
        p.lift_fish()
        self.assertEqual(p.orig_lift_calls, 1)

    def test_hooked_error_exits_hold_keys_before_runtime_run(self):
        P = make_player_cls()
        dummy = DummyRuntime(loose_result(OUT.LOST))
        afr.install_player_patches(
            P, enabled=True, runtime_factory=lambda p: dummy,
            captured_error=RuntimeError, hooked_error=FakeHookedError)
        p = P()
        p.detection.hooked = False
        p.tackle.raise_hooked = True
        dummy.events = p.events
        p.retrieve_line()

        names = [event[0] for event in p.events]
        self.assertEqual(dummy.runs, 1)
        self.assertEqual(p.orig_retrieve_calls, 0)
        # 完整 enter/exit 记录必须都在 runtime.run 之前
        self.assertLess(names.index("hold_enter"), names.index("tackle.retrieve"))
        self.assertLess(names.index("tackle.retrieve"), names.index("hold_exit"))
        self.assertLess(names.index("hold_exit"), names.index("runtime.run"))
        self.assertEqual(names.count("hold_enter"), 1)
        self.assertEqual(names.count("hold_exit"), 1)

    def test_cancelled_aborts_and_never_runs_legacy_lift(self):
        P = make_player_cls()
        dummy = DummyRuntime(loose_result(OUT.CANCELLED))
        afr.install_player_patches(
            P, enabled=True, runtime_factory=lambda p: dummy,
            captured_error=RuntimeError, hooked_error=FakeHookedError)
        p = P()
        p.detection.hooked = True
        with self.assertRaises(RuntimeError):
            p.retrieve_line()
        self.assertEqual(dummy.runs, 1)
        # 已进入新会话的 CANCELLED 结果严禁后续 orig_lift
        p.lift_fish()
        self.assertEqual(p.orig_lift_calls, 0)

    def test_blocked_aborts_without_legacy_lift(self):
        P = make_player_cls()
        dummy = DummyRuntime(loose_result(OUT.BLOCKED))
        afr.install_player_patches(
            P, enabled=True, runtime_factory=lambda p: dummy,
            captured_error=RuntimeError, hooked_error=FakeHookedError)
        p = P()
        p.detection.hooked = True
        with self.assertRaises(RuntimeError):
            p.retrieve_line()
        p.lift_fish()
        self.assertEqual(p.orig_lift_calls, 0)

    def test_reentered_is_not_silently_recast(self):
        P = make_player_cls()
        dummy = DummyRuntime(loose_result(OUT.REENTERED))
        afr.install_player_patches(
            P, enabled=True, runtime_factory=lambda p: dummy,
            captured_error=RuntimeError, hooked_error=FakeHookedError)
        p = P()
        p.detection.hooked = True
        with self.assertRaises(RuntimeError):
            p.retrieve_line()
        # REENTERED 也不能静默返回、让外层继续 lift + recast
        p.lift_fish()
        self.assertEqual(p.orig_lift_calls, 0)

    def test_unsupported_electro_blocks_before_runtime(self):
        P = make_player_cls()
        dummy = DummyRuntime(loose_result())
        afr.install_player_patches(
            P, enabled=True, runtime_factory=lambda p: dummy,
            captured_error=RuntimeError, hooked_error=FakeHookedError)
        p = P()
        p.cfg = SimpleNamespace(
            ARGS=SimpleNamespace(ELECTRO=True),
            PROFILE=SimpleNamespace(MODE="bottom", POST_ACCELERATION=False),
        )
        with self.assertRaises(RuntimeError) as ctx:
            p.pull_fish()
        self.assertIn("ELECTRO", str(ctx.exception))
        self.assertIn("BLOCKED", str(ctx.exception))
        self.assertEqual(dummy.runs, 0)
        self.assertEqual(p.cur_coffee, 0)       # 原 pull 语义先执行
        self.assertEqual(p.save_calls, 1)

    def test_unsupported_post_acceleration_blocks_before_runtime(self):
        P = make_player_cls()
        dummy = DummyRuntime(loose_result())
        afr.install_player_patches(
            P, enabled=True, runtime_factory=lambda p: dummy,
            captured_error=RuntimeError, hooked_error=FakeHookedError)
        p = P()
        p.cfg = SimpleNamespace(
            ARGS=SimpleNamespace(ELECTRO=False),
            PROFILE=SimpleNamespace(MODE="bottom", POST_ACCELERATION=True),
        )
        with self.assertRaises(RuntimeError) as ctx:
            p.pull_fish()
        self.assertIn("POST_ACCELERATION", str(ctx.exception))
        self.assertEqual(dummy.runs, 0)

    def test_telescopic_ignores_post_acceleration(self):
        P = make_player_cls()
        dummy = DummyRuntime(loose_result(OUT.LOST))
        afr.install_player_patches(
            P, enabled=True, runtime_factory=lambda p: dummy,
            captured_error=RuntimeError, hooked_error=FakeHookedError)
        p = P()
        p.cfg = SimpleNamespace(
            ARGS=SimpleNamespace(ELECTRO=False),
            PROFILE=SimpleNamespace(MODE="telescopic", POST_ACCELERATION=True),
        )
        p.pull_fish()
        self.assertEqual(dummy.runs, 1)

    def test_pull_save_flag_and_coffee_reset(self):
        P = make_player_cls()
        dummy = DummyRuntime(loose_result(OUT.LOST))
        afr.install_player_patches(
            P, enabled=True, runtime_factory=lambda p: dummy,
            captured_error=RuntimeError, hooked_error=FakeHookedError)
        p = P()
        p.pull_fish(save=True)
        self.assertEqual(p.cur_coffee, 0)
        self.assertEqual(p.save_calls, 1)
        p.cur_coffee = 9
        p.pull_fish(save=False)
        self.assertEqual(p.cur_coffee, 0)
        self.assertEqual(p.save_calls, 1)

    def test_repeated_entry_reuses_runtime_and_has_no_double_owner(self):
        h = Harness([Frame(), Frame()])
        h.cancel_after = 10
        P = make_player_cls()
        made = []

        def factory(p):
            rt = afr.FightRuntime(h.callbacks(), interval=0.0005)
            made.append(rt)
            return rt

        afr.install_player_patches(
            P, enabled=True, runtime_factory=factory,
            captured_error=RuntimeError, hooked_error=FakeHookedError)
        p = P()
        h.player = p

        def reenter(_h, n):
            if n == 1:
                p.pull_fish()          # 第一次观测里重入

        h.on_observe = reenter
        # REENTERED 现在受控中止外层流程，而不是静默继续 lift/recast。
        with self.assertRaises(RuntimeError):
            p.pull_fish()
        self.assertEqual(len(made), 1)
        self.assertEqual(h.sink.downs.count(F5), 1)
        self.assertEqual(h.sink.downs.count(F6), 1)


# ==================== 会话起点 / 校准 ====================


class TestCalibration(unittest.TestCase):
    def run_once(self, status, wheel_ok=True, foreground=True):
        h = Harness([Frame(foreground=foreground)])
        h.status = status
        h.wheel_ok = wheel_ok
        h.cancel_after = 2
        rt = h.runtime()
        result = rt.run()
        return h, rt, result

    def test_unknown_only_down30_then_up16(self):
        h, rt, result = self.run_once((20, False, False))
        self.assertEqual(h.wheel_calls, [-30, 16])
        self.assertEqual(h.commits, [17])
        self.assertEqual(result.calibration.cur, 17)
        self.assertTrue(result.calibration.ok)
        self.assertNotIn(30, h.wheel_calls)

    def test_calibrated_keeps_cur_and_never_wheels(self):
        h, rt, result = self.run_once((20, True, False))
        self.assertEqual(h.wheel_calls, [])
        self.assertEqual(h.commits, [])
        self.assertEqual(result.calibration.cur, 20)
        self.assertEqual(result.calibration.reason, "already_calibrated")

    def test_cur_30_though_calibrated_is_recalibrated(self):
        h, rt, result = self.run_once((30, True, False))
        self.assertEqual(h.wheel_calls, [-30, 16])
        self.assertEqual(h.commits, [17])
        self.assertTrue(result.calibration.ok)
        self.assertNotIn(30, h.wheel_calls)

    def test_nan_though_calibrated_is_recalibrated(self):
        h, rt, result = self.run_once((float("nan"), True, False))
        self.assertEqual(h.wheel_calls, [-30, 16])
        self.assertEqual(h.commits, [17])
        self.assertTrue(result.calibration.ok)

    def test_foreground_is_rechecked_before_each_wheel(self):
        h = Harness([Frame()])
        h.status = (20, False, False)
        h.force_foreground = True

        def drop_after_first(_h, _n):
            _h.force_foreground = False

        h.on_wheel = drop_after_first
        rt = h.runtime()
        result = rt.run()
        self.assertIs(result.outcome, OUT.BLOCKED)
        self.assertIn("foreground_lost", result.reason)
        self.assertEqual(h.wheel_calls, [-30])     # 第二步滚轮前已经失焦
        self.assertEqual(h.commits, [])

    def test_restored_is_recalibrated(self):
        h, rt, result = self.run_once((20, True, True))
        self.assertEqual(h.wheel_calls, [-30, 16])
        self.assertEqual(h.commits, [17])
        self.assertEqual(result.calibration.cur, 17)

    def test_wheel_failure_blocks_session_without_legacy(self):
        h, rt, result = self.run_once((20, False, False), wheel_ok=False)
        self.assertIs(result.outcome, OUT.BLOCKED)
        self.assertIn("calibration_failed", result.reason)
        self.assertEqual(h.commits, [])
        # machine.start 已按下的键必须被 finally 释放
        self.assertIn(F5, h.sink.ups)
        self.assertIn(F6, h.sink.ups)

    def test_second_wheel_failure_sends_no_other_wheel(self):
        h = Harness([Frame()])
        h.status = (20, False, False)
        h.wheel = lambda n: (h.wheel_calls.append(n) or n == -30)
        h.cancel_after = 2
        rt = h.runtime()
        result = rt.run()
        self.assertIs(result.outcome, OUT.BLOCKED)
        self.assertEqual(h.wheel_calls, [-30, 16])
        self.assertEqual(h.commits, [])

    def test_foreground_false_does_not_start_or_wheel(self):
        h, rt, result = self.run_once((20, False, False), foreground=False)
        self.assertIs(result.outcome, OUT.BLOCKED)
        self.assertEqual(result.reason, "foreground_lost")
        self.assertEqual(h.sink.calls, [])
        self.assertEqual(h.wheel_calls, [])


# ==================== preflight / 收尾释放 ====================


class TestPreflightAndRelease(unittest.TestCase):
    def test_preflight_error_blocks_before_any_key(self):
        h = Harness([Frame()])
        cb = h.callbacks()
        cb.preflight_error = lambda: "capture_template_unavailable:/tmp/keep.png"
        rt = afr.FightRuntime(cb, interval=0.0005)
        result = rt.run()
        self.assertIs(result.outcome, OUT.BLOCKED)
        self.assertIn("capture_template_unavailable", result.reason)
        self.assertEqual(h.sink.calls, [])       # 一个键都没按
        self.assertEqual(h.wheel_calls, [])

    def test_capture_release_failure_raises_not_captured(self):
        h = Harness([Frame(score=0.99)])
        h.cancel_after = 10
        h.sink.fail_ups = {F5: 99}
        rt = h.runtime()
        with self.assertRaises(afr.FightReleaseError):
            rt.run()
        f5_ups = [c for c in h.sink.calls if c[0] == F5 and not c[1]]
        self.assertEqual(len(f5_ups), 3)         # 最多 3 次，不无限重试
        # 释放失败不能返回 CAPTURED 让外层继续入护/recast（已由上面的
        # FightReleaseError 断言覆盖：捕获结果没有机会被返回）。

    def test_release_failure_retries_three_times_then_raises(self):
        h = Harness([Frame()])
        h.cancel_after = 2
        h.sink.fail_ups = {F5: 99}
        rt = h.runtime()
        with self.assertRaises(afr.FightReleaseError):
            rt.run()
        f5_ups = [c for c in h.sink.calls if c[0] == F5 and not c[1]]
        self.assertEqual(len(f5_ups), 3)

    def test_release_recovers_within_three_attempts(self):
        h = Harness([Frame(score=0.99)])
        h.cancel_after = 10
        h.sink.fail_ups = {F5: 2}
        rt = h.runtime()
        result = rt.run()
        self.assertIs(result.outcome, OUT.CAPTURED)
        f5_ups = [c for c in h.sink.calls if c[0] == F5 and not c[1]]
        self.assertEqual(len(f5_ups), 3)
        self.assertEqual(h.sink.ups.count(F6), 1)


# ==================== 捕获强确认 ====================


class TestCapture(unittest.TestCase):
    def test_hud_unknown_still_confirms_capture(self):
        frame = Frame(hud=T.UNKNOWN, score=0.99, foreground=True)
        h = Harness([frame])
        h.cancel_after = 10
        rt = h.runtime()
        result = rt.run()
        self.assertIs(result.outcome, OUT.CAPTURED)
        self.assertEqual(result.reason, "captured_strong")
        self.assertIn(F5, h.sink.ups)
        self.assertIn(F6, h.sink.ups)

    def test_weak_geometry_true_but_strong_false_does_not_confirm(self):
        frame = Frame(captured=T.TRUE, score=0.40)
        h = Harness([frame])
        h.cancel_after = 6
        rt = h.runtime()
        result = rt.run()
        self.assertIs(result.outcome, OUT.CANCELLED)
        self.assertAlmostEqual(result.max_capture_score, 0.40)

    def test_template_failure_is_unknown_not_geometry(self):
        frame = Frame(captured=T.TRUE, score=None)
        h = Harness([frame])
        h.cancel_after = 6
        rt = h.runtime()
        result = rt.run()
        self.assertIs(result.outcome, OUT.CANCELLED)
        self.assertIsNone(result.max_capture_score)

    def test_capture_needs_two_independent_frames(self):
        # 第一帧被 preflight 消费；随后 0.99 -> 0.10 -> 0.99，连续计数被重置。
        frames = [
            Frame(score=0.0),
            Frame(score=0.99),
            Frame(score=0.10),
            Frame(score=0.99),
        ]
        h = Harness(frames)
        h.cancel_after = 4
        rt = h.runtime()
        result = rt.run()
        self.assertIs(result.outcome, OUT.CANCELLED)


# ==================== NET ====================


class TestNet(unittest.TestCase):
    def test_mid_takeover_unknown_net_never_blind_toggles(self):
        """旧 FightRuntime 是中途接管：网位置未知，不得盲发 F7。"""
        frame = Frame(fish=T.TRUE, ring=0.985, bar=2, score=0.0)
        h = Harness([frame])
        h.cancel_after = 10
        rt = h.runtime(
            land_frames=1,
            near_grace=0.0,
            net_window=0.0,
            lost_hold=999.0,
            auto_net_enabled=True,
        )
        result = rt.run()

        self.assertIs(result.outcome, OUT.CANCELLED)
        self.assertEqual(h.net_calls, 0)
        self.assertFalse([e for e in result.events if e.to_state is S.NET])


# ==================== 冻结 / 异常 ====================


class TestSafety(unittest.TestCase):
    def test_exception_still_releases_keys(self):
        h = Harness([Frame()])
        h.cancel_after = 20
        h.fail_observe_at = 2
        rt = h.runtime()
        with self.assertRaises(RuntimeError):
            rt.run()
        self.assertIn(F5, h.sink.ups)
        self.assertIn(F6, h.sink.ups)

    def test_friction_step_gets_bar_then_none_when_frozen(self):
        frames = [
            Frame(),                                  # preflight
            Frame(bar=3, hud=T.TRUE),
            Frame(bar=None, hud=T.UNKNOWN),
        ]
        h = Harness(frames)
        h.cancel_after = 4
        rt = h.runtime()
        result = rt.run()
        self.assertIs(result.outcome, OUT.CANCELLED)
        self.assertIn(3, h.friction_steps)
        self.assertIn(None, h.friction_steps)

    def test_friction_begin_called_once_per_session(self):
        h = Harness([Frame(), Frame(bar=3, hud=T.TRUE)])
        h.cancel_after = 2
        rt = h.runtime()
        self.assertIs(rt.run().outcome, OUT.CANCELLED)
        self.assertEqual(h.friction_begins, 1)


# ==================== 适配器改动：AST 静态检查（不 import 游戏） ====================


def _func(tree, name):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing function {name}")


def _calls(node, name):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == name]


class TestAdapterAst(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = (BOT / "rf4s_adapter.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(src)

    def test_friction_absolute_fails_closed(self):
        fn = _func(self.tree, "friction_absolute")
        self.assertTrue(_calls(fn, "_no_fish_gate"))
        self.assertFalse(_calls(fn, "fish_icon_present"))
        returns_false = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
            and n.value.value is False
        ]
        returns_true = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
            and n.value.value is True
        ]
        self.assertGreaterEqual(len(returns_false), 2)
        self.assertEqual(len(returns_true), 1)

    def test_cast_refuses_on_false(self):
        fn = _func(self.tree, "patch_cast")
        casts = [n for n in ast.walk(fn)
                 if isinstance(n, ast.FunctionDef) and n.name == "cast"]
        self.assertEqual(len(casts), 1)
        guarded = False
        for node in ast.walk(casts[0]):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
                    and isinstance(test.operand, ast.Call)
                    and isinstance(test.operand.func, ast.Name)
                    and test.operand.func.id == "friction_absolute"):
                raises = [n for n in ast.walk(node)
                          if isinstance(n, ast.Raise)
                          and isinstance(n.exc, ast.Call)
                          and getattr(n.exc.func, "id", "") == "RuntimeError"]
                guarded = bool(raises)
        self.assertTrue(guarded)

    def test_cast_still_recalibrates_to_17_every_rod(self):
        fn = _func(self.tree, "patch_cast")
        calls = _calls(fn, "friction_absolute")
        self.assertTrue(calls)
        self.assertTrue(any(
            c.args and isinstance(c.args[0], ast.Name)
            and c.args[0].id == "FRICTION_INITIAL"
            for c in calls
        ))

    def test_callback_factory_preflights_keep_template(self):
        fn = _func(self.tree, "_fight_callbacks")
        self.assertTrue(_calls(fn, "_fight_keep_template"))
        keywords = set()
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", "") == "FightCallbacks"):
                keywords.update(kw.arg for kw in node.keywords)
        self.assertIn("preflight_error", keywords)

    def test_patch_fight_runtime_injects_hooked_error(self):
        fn = _func(self.tree, "patch_fight_runtime")
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "install_player_patches"]
        self.assertTrue(calls)
        self.assertIn("hooked_error", {kw.arg for kw in calls[0].keywords})

    def test_runtime_patch_is_installed_after_old_patches(self):
        main = _func(self.tree, "main")
        names = [n.func.id for n in ast.walk(main)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        self.assertIn("patch_fight_runtime", names)
        self.assertLess(names.index("patch_bottom_mode_held"),
                        len(names) - 1)
        idx_fight = names.index("patch_fight_runtime")
        idx_old = names.index("patch_bottom_mode_held")
        self.assertGreater(idx_fight, idx_old)


if __name__ == "__main__":
    unittest.main()
