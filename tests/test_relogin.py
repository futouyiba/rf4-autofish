"""小退重登的离线测试：无窗口、无真实输入、无 OCR。

覆盖两层：
1. ``autofish_relogin`` 的四态判据、坐标缩放与整条流程（含重试与超时）；
2. 状态机在"线杯剩线 < 15%"时的触发、松键、失败转 FAULT、成功后重新校准。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

import numpy as np

BOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT))

import autofish_lifecycle as alf  # noqa: E402
import autofish_observation as aobs  # noqa: E402
import autofish_relogin as arl  # noqa: E402
from autofish_control import AutoStop  # noqa: E402

T = aobs.TriState
S = alf.AutoFishState


# ----------------------------------------------------------------------
# 合成画面：满足 screen_kind 的四态判据
# ----------------------------------------------------------------------
def frame_world() -> np.ndarray:
    return np.full((1080, 1920, 3), 120, dtype=np.uint8)


def frame_dark() -> np.ndarray:
    return np.full((1080, 1920, 3), 30, dtype=np.uint8)


def frame_login() -> np.ndarray:
    """均值≈69（区间内）+ 登录按钮带亮点约 5%。"""
    img = np.full((1080, 1920, 3), 69, dtype=np.uint8)
    y0, y1, x0, x1 = arl.LOGIN_BAND
    # 直接写回 img 的切片（band 是视图；不要 reshape，切片可能被复制）
    img[y0:y0 + 3, x0:x1] = 210
    return img


def frame_error() -> np.ndarray:
    # 均值 92 会被判成 world；用 50~60 之间的"既不够暗也不像登录页"的灰
    return np.full((1080, 1920, 3), 52, dtype=np.uint8)


class FakeIo:
    """按点击推进画面的最小 IO。"""

    def __init__(self) -> None:
        self.kind = arl.KIND_WORLD
        self.keys: list[str] = []
        self.downs: list[str] = []
        self.ups: list[str] = []
        self.clicks: list[tuple[int, int]] = []
        self.size = (1920, 1080)
        # 点「是」之后，还要"加载"几次才出现登录界面
        self.login_delay = 2
        self._delay = 0
        self.exit_takes_effect = True
        self.login_takes_effect = True

    def _frame(self) -> np.ndarray:
        return {
            arl.KIND_WORLD: frame_world,
            arl.KIND_DARK: frame_dark,
            arl.KIND_LOGIN: frame_login,
            arl.KIND_ERROR: frame_error,
        }[self.kind]()

    def capture(self):
        # 每次抓图消耗一格"加载延迟"；耗尽后切到登录界面
        if self._delay > 0:
            self._delay -= 1
            if self._delay == 0:
                self.kind = arl.KIND_LOGIN
            return frame_dark()
        return self._frame()

    def client_size(self):
        return self.size

    def click_client(self, cx: int, cy: int) -> None:
        self.clicks.append((cx, cy))
        # 换算回 1920x1080 基准再判是哪个按钮（窗口可能被缩放；
        # 缩放有 1~2px 的量化误差，所以按容差比较而不是精确相等）
        bx = cx * 1920 / self.size[0]
        by = cy * 1080 / self.size[1]

        def near(px, py):
            return abs(bx - px) <= 2 and abs(by - py) <= 2

        if near(*arl.EXIT_BTN):
            self.kind = arl.KIND_DARK
        elif near(*arl.CONFIRM_BTN):
            if self.exit_takes_effect:
                self._delay = self.login_delay
            else:
                self.kind = arl.KIND_WORLD
                self._delay = 0
        elif near(*arl.LOGIN_BTN):
            if self.login_takes_effect:
                self.kind = arl.KIND_WORLD
            else:
                self.kind = arl.KIND_LOGIN

    def key(self, name: str) -> None:
        self.keys.append(name)

    def key_down(self, name: str) -> None:
        self.downs.append(name)

    def key_up(self, name: str) -> None:
        self.ups.append(name)


class FakeTime:
    """虚拟时钟：sleep 推进时间，不真的等待。"""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += float(seconds)
        self.slept += float(seconds)


class StopControl:
    def __init__(self, stop_after: int = 10 ** 9) -> None:
        self.calls = 0
        self.stop_after = stop_after

    def checkpoint(self) -> None:
        self.calls += 1
        if self.calls >= self.stop_after:
            raise AutoStop("stopped")

    def wait(self, seconds: float, quantum: float = 0.05) -> bool:
        return False


# ----------------------------------------------------------------------
# 模块级：判据与坐标
# ----------------------------------------------------------------------
class ScreenKindTests(unittest.TestCase):
    def test_world(self):
        kind, mean, _ = arl.screen_kind(frame_world())
        self.assertEqual(kind, arl.KIND_WORLD)
        self.assertAlmostEqual(mean, 120.0)

    def test_dark(self):
        kind, mean, _ = arl.screen_kind(frame_dark())
        self.assertEqual(kind, arl.KIND_DARK)
        self.assertAlmostEqual(mean, 30.0)

    def test_login(self):
        kind, mean, bright = arl.screen_kind(frame_login())
        self.assertEqual(kind, arl.KIND_LOGIN)
        self.assertTrue(60 <= mean <= 80, mean)
        self.assertTrue(2.0 <= bright <= 12.0, bright)

    def test_error_band(self):
        kind, _, _ = arl.screen_kind(frame_error())
        self.assertEqual(kind, arl.KIND_ERROR)

    def test_blank_is_not_login(self):
        # 截图失败的全黑画面必须落到 dark/加载，绝不能被当成登录界面
        kind, _, _ = arl.screen_kind(np.zeros((1080, 1920, 3), dtype=np.uint8))
        self.assertNotEqual(kind, arl.KIND_LOGIN)


class ScalePointTests(unittest.TestCase):
    def test_identity_at_1080p(self):
        self.assertEqual(arl.scale_point((1418, 743), (1920, 1080)), (1418, 743))

    def test_half_scale(self):
        self.assertEqual(arl.scale_point((1418, 743), (960, 540)), (709, 372))

    def test_custom_base(self):
        self.assertEqual(arl.scale_point((100, 50), (200, 100), (100, 50)), (200, 100))


# ----------------------------------------------------------------------
# 模块级：整条流程
# ----------------------------------------------------------------------
class ReloginFlowTests(unittest.TestCase):
    def _run(self, io=None, cfg=None, control=None, stop_after=None):
        io = io or FakeIo()
        clock = FakeTime()
        control = control or StopControl(stop_after or 10 ** 9)
        result = arl.relogin(io, control=control, cfg=cfg or arl.ReloginConfig(),
                             sleep=clock.sleep, now=clock.now)
        return result, io, clock, control

    def test_happy_path(self):
        result, io, clock, _ = self._run()
        self.assertTrue(result["ok"])
        self.assertEqual(io.keys, ["esc"])
        self.assertEqual(io.clicks, [arl.EXIT_BTN, arl.CONFIRM_BTN, arl.LOGIN_BTN])
        # 退出按钮要先按住 Shift
        self.assertEqual(io.downs, ["shift"])
        self.assertEqual(io.ups, ["shift"])
        self.assertEqual(result["login_clicks"], 1)

    def test_exit_click_retried_once(self):
        io = FakeIo()
        io.exit_takes_effect = False
        # 第一次点完不生效，重试第二次才生效
        original = io.click_client
        calls = {"n": 0}

        def flaky(cx, cy):
            original(cx, cy)
            if (cx, cy) == arl.CONFIRM_BTN:
                calls["n"] += 1
                if calls["n"] == 1:
                    io.exit_takes_effect = True

        io.click_client = flaky  # type: ignore[assignment]
        result, io2, _clock, _ = self._run(io=io)
        self.assertTrue(result["ok"])
        self.assertEqual(io2.keys.count("esc"), 2)

    def test_login_timeout_raises(self):
        io = FakeIo()
        io.exit_takes_effect = False
        with self.assertRaises(arl.ReloginError) as ctx:
            self._run(io=io, cfg=arl.ReloginConfig(exit_retries=0))
        self.assertEqual(ctx.exception.stage, "login_timeout")

    def test_login_clicks_retried_then_gives_up(self):
        io = FakeIo()
        io.login_takes_effect = False
        with self.assertRaises(arl.ReloginError) as ctx:
            self._run(io=io, cfg=arl.ReloginConfig(
                login_retries=1, world_timeout=5.0))
        self.assertEqual(ctx.exception.stage, "world_timeout")
        self.assertEqual(io.clicks.count(arl.LOGIN_BTN), 2)

    def test_total_timeout_bounds_run(self):
        io = FakeIo()
        io.exit_takes_effect = False
        clock = FakeTime()
        with self.assertRaises(arl.ReloginError):
            arl.relogin(io, cfg=arl.ReloginConfig(
                login_timeout=30.0, total_timeout=40.0, exit_retries=3),
                sleep=clock.sleep, now=clock.now)
        self.assertLessEqual(clock.slept, 45.0)

    def test_stop_request_propagates(self):
        control = StopControl(stop_after=2)
        with self.assertRaises(AutoStop):
            self._run(control=control)

    def test_coordinates_scale_to_smaller_window(self):
        io = FakeIo()
        io.size = (1280, 720)
        result, io2, _c, _ = self._run(io=io)
        self.assertTrue(result["ok"])
        self.assertEqual(io2.clicks[0], arl.scale_point(arl.EXIT_BTN, (1280, 720)))
        self.assertEqual(io2.clicks[-1], arl.scale_point(arl.LOGIN_BTN, (1280, 720)))


# ----------------------------------------------------------------------
# 状态机集成
# ----------------------------------------------------------------------
class Clock:
    def __init__(self, now: float = 1000.0, step: float = 0.02):
        self.now = now
        self.step = step

    def __call__(self) -> float:
        self.now = round(self.now + self.step, 9)
        return self.now


class FakeSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool, str]] = []

    def __call__(self, key, is_down, reason):
        if key not in ("f5", "f6"):
            raise AssertionError("unexpected key %r" % (key,))
        self.calls.append((key, is_down, reason))
        return None

    def held(self) -> list[str]:
        held: list[str] = []
        for key, down, _ in self.calls:
            if down and key not in held:
                held.append(key)
            if not down and key in held:
                held.remove(key)
        return held


class LiteHarness:
    """只走到 FIGHT 的最小协调器装配。"""

    def __init__(self, config=None, relogin_result=True, relogin_error=None):
        self.clock = Clock()
        self.sink = FakeSink()
        self.frame: dict = {}
        self.calls: list[tuple] = []
        self.logs: list[str] = []
        self.friction = {"cur": 17, "calibrated": False}
        self.relogin_result = relogin_result
        self.relogin_error = relogin_error
        self.relogin_calls = 0
        self.callbacks = alf.RuntimeCallbacks(
            capture_observation=self._capture,
            input_sink=self.sink,
            readiness=lambda obs: True,
            calibrate=self._calibrate,
            cast=self._cast,
            relogin=self._relogin,
            friction_step=lambda level: None,
            friction_snapshot=lambda: dict(self.friction),
            keepnet_full=lambda: False,
            log=self.logs.append,
            clock=self.clock,
        )
        self.coord = alf.LifecycleCoordinator(
            self.callbacks, config=config or alf.LifecycleConfig(),
            initial_auto=True, epoch="test-relogin")

    def _capture(self):
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
        values.update(self.frame)
        return aobs.Observation(
            monotonic_ts=self.clock(), wall_ts=0.0,
            bar_hue=None, bar_sat=None, evidence={}, **values)

    def _calibrate(self, control, fight=False):
        self.calls.append(("calibrate", bool(fight)))
        self.friction["calibrated"] = True
        return None

    def _cast(self, control):
        self.calls.append(("cast",))
        return None

    def _relogin(self, control):
        self.relogin_calls += 1
        self.calls.append(("relogin", bool(control is not None)))
        if self.relogin_error is not None:
            raise self.relogin_error
        return self.relogin_result

    def step(self, **fields):
        if fields:
            self.frame = dict(fields)
        return self.coord.step()

    def to_fight(self):
        self.step()
        self.step(fish_icon=T.FALSE)
        self.step()
        self.step()
        self.step()
        self.step(fish_icon=T.TRUE, ring_ratio=0.6)
        self.step(fish_icon=T.TRUE, ring_ratio=0.6)
        assert self.coord.state is S.FIGHT, self.coord.state


class LifecycleReloginTests(unittest.TestCase):
    def test_low_ring_triggers_relogin(self):
        h = LiteHarness()
        h.to_fight()
        h.step(fish_icon=T.TRUE, ring_ratio=0.14)   # 第 1 帧
        self.assertIs(h.coord.state, S.FIGHT)
        h.step(fish_icon=T.TRUE, ring_ratio=0.14)   # 第 2 帧 -> 触发
        self.assertIs(h.coord.state, S.RELOGIN)
        h.step(fish_icon=T.TRUE, ring_ratio=0.14)   # 下一 tick 执行入口动作
        self.assertEqual(h.relogin_calls, 1)

    def test_high_ring_does_not_trigger(self):
        h = LiteHarness()
        h.to_fight()
        for _ in range(5):
            h.step(fish_icon=T.TRUE, ring_ratio=0.20)
        self.assertIs(h.coord.state, S.FIGHT)
        self.assertEqual(h.relogin_calls, 0)

    def test_unknown_ring_does_not_trigger(self):
        h = LiteHarness()
        h.to_fight()
        for _ in range(5):
            h.step(fish_icon=T.TRUE, ring_ratio=None)
        self.assertIs(h.coord.state, S.FIGHT)
        self.assertEqual(h.relogin_calls, 0)

    def test_single_low_frame_does_not_trigger(self):
        h = LiteHarness()
        h.to_fight()
        h.step(fish_icon=T.TRUE, ring_ratio=0.10)
        h.step(fish_icon=T.TRUE, ring_ratio=0.60)   # 回升 -> 计数清零
        h.step(fish_icon=T.TRUE, ring_ratio=0.10)
        self.assertIs(h.coord.state, S.FIGHT)

    def test_success_goes_to_calibrating(self):
        h = LiteHarness(relogin_result=True)
        h.to_fight()
        h.step(fish_icon=T.TRUE, ring_ratio=0.10)
        h.step(fish_icon=T.TRUE, ring_ratio=0.10)
        h.step(fish_icon=T.TRUE, ring_ratio=0.10)
        self.assertIs(h.coord.state, S.CALIBRATING)
        # 重登后必须重新校准，而不是直接抛竿
        h.step(fish_icon=T.FALSE, ring_ratio=None)
        self.assertEqual(h.calls[-1], ("calibrate", False))

    def test_failure_goes_to_fault(self):
        h = LiteHarness(relogin_result=False)
        h.to_fight()
        h.step(fish_icon=T.TRUE, ring_ratio=0.10)
        h.step(fish_icon=T.TRUE, ring_ratio=0.10)
        h.step(fish_icon=T.TRUE, ring_ratio=0.10)
        self.assertIs(h.coord.state, S.FAULT)
        self.assertIn("relogin_failed", (h.coord.last_error or ""))

    def test_exception_goes_to_fault(self):
        h = LiteHarness(relogin_error=RuntimeError("boom"))
        h.to_fight()
        h.step(fish_icon=T.TRUE, ring_ratio=0.10)
        h.step(fish_icon=T.TRUE, ring_ratio=0.10)
        h.step(fish_icon=T.TRUE, ring_ratio=0.10)
        self.assertIs(h.coord.state, S.FAULT)
        self.assertIn("relogin_error", (h.coord.last_error or ""))

    def test_missing_callback_faults(self):
        h = LiteHarness()
        h.callbacks.relogin = None
        h.to_fight()
        h.step(fish_icon=T.TRUE, ring_ratio=0.10)
        h.step(fish_icon=T.TRUE, ring_ratio=0.10)
        h.step(fish_icon=T.TRUE, ring_ratio=0.10)
        self.assertIs(h.coord.state, S.FAULT)
        self.assertIn("relogin_missing", (h.coord.last_error or ""))

    def test_keys_released_before_relogin(self):
        h = LiteHarness()
        h.to_fight()
        h.step(fish_icon=T.TRUE, ring_ratio=0.10)
        h.step(fish_icon=T.TRUE, ring_ratio=0.10)
        # 进入 RELOGIN 的迁移必须先停子机、松键
        self.assertEqual(h.sink.held(), [])

    def test_config_threshold_is_15_percent(self):
        self.assertAlmostEqual(alf.LifecycleConfig().relogin_line_low, 0.15)

    def test_config_rejects_bad_threshold(self):
        with self.assertRaises(ValueError):
            alf.LifecycleConfig(relogin_line_low=0.0)
        with self.assertRaises(ValueError):
            alf.LifecycleConfig(relogin_line_low=1.0)
        with self.assertRaises(ValueError):
            alf.LifecycleConfig(relogin_line_frames=0)


if __name__ == "__main__":
    unittest.main()
