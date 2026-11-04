"""shadow 接入测试（不截图、不启动 bot、不发输入）。

覆盖三层：
  1. `autofish_shadow.ShadowObserver` 的日志节流与异常降级（纯内存）。
  2. `autofish_shadow` 源码级约束：无输入/无帧源/无适配层依赖。
  3. `rf4s_adapter._shadow_observe` 的结构约束：全局限频、每个放行样本
     只抓一帧、返回值不进控制流。

运行：
    cd bot && python -m unittest discover -s tests -p "test_shadow_integration.py" -v
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import autofish_observation as aobs  # noqa: E402
import autofish_shadow as ashadow  # noqa: E402

BOT_DIR = pathlib.Path(__file__).resolve().parents[1]
ADAPTER = BOT_DIR / "rf4s_adapter.py"
SHADOW = BOT_DIR / "autofish_shadow.py"

T = aobs.TriState


def mk(
    *,
    fish=T.FALSE,
    bar_level=None,
    bar_tier=None,
    ring=None,
    captured=T.FALSE,
    ok_modal=T.FALSE,
    hud=T.TRUE,
    foreground=True,
    ts=0.0,
) -> aobs.Observation:
    return aobs.Observation(
        monotonic_ts=ts,
        wall_ts=ts,
        foreground=foreground,
        fish_icon=fish,
        bar_level=bar_level,
        bar_tier=bar_tier,
        bar_hue=None,
        bar_sat=None,
        ring_ratio=ring,
        captured=captured,
        ok_modal=ok_modal,
        hud_visible=hud,
        evidence={},
    )


class FakeClock:
    """可控单调时钟：不推进就不会触发 heartbeat。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class Feed:
    """按顺序吐出 Observation；用光了就一直吐最后一条。"""

    def __init__(self, *items: aobs.Observation) -> None:
        self.items = list(items)
        self.n = 0

    def __call__(self, image=None, **kwargs):
        item = self.items[min(self.n, len(self.items) - 1)]
        self.n += 1
        return item


def _observer_with_feed(feed, patchers, **kwargs):
    """构造 observer，把模块内的 observe_frame 换成 feed（patcher 由调用方收）。"""
    clock = kwargs.pop("clock", None) or FakeClock()
    lines: list[str] = []
    obs = ashadow.ShadowObserver(enabled=True, clock=clock,
                                 sink=lines.append, **kwargs)
    p = mock.patch.object(ashadow, "observe_frame", side_effect=feed)
    p.start()
    patchers.append(p)
    return obs, lines, clock


# ==================================================================== 日志层


class TestShadowObserverLogging(unittest.TestCase):
    def setUp(self):
        self._patchers = []

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def _observer(self, feed, **kwargs):
        return _observer_with_feed(feed, self._patchers, **kwargs)

    def test_pending_then_changed_logs_exactly_once_each(self):
        obs, lines, _ = self._observer(Feed(mk(fish=T.TRUE)))
        obs.update(object(), foreground=True, source="pull")
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("[STATE] pending", lines[0])
        self.assertIn("stable=unknown", lines[0])
        self.assertIn("cand=fight", lines[0])

        obs.update(object(), foreground=True, source="pull")
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("[STATE] changed", lines[1])
        self.assertIn("stable=fight", lines[1])

    def test_steady_frames_are_throttled(self):
        obs, lines, clock = self._observer(Feed(mk(fish=T.TRUE)))
        obs.update(object(), foreground=True, source="pull")
        obs.update(object(), foreground=True, source="pull")   # -> fight
        before = len(lines)
        for _ in range(20):
            obs.update(object(), foreground=True, source="pull")
            clock.advance(0.2)                                  # 4 秒 < 10 秒
        self.assertEqual(len(lines), before,
                         f"稳定状态下不得每帧打印: {lines[before:]}")

    def test_heartbeat_fires_after_interval(self):
        obs, lines, clock = self._observer(Feed(mk(fish=T.TRUE)),
                                           heartbeat=10.0)
        obs.update(object(), foreground=True, source="pull")
        obs.update(object(), foreground=True, source="pull")
        before = len(lines)

        clock.advance(9.9)
        obs.update(object(), foreground=True, source="pull")
        self.assertEqual(len(lines), before, "未到心跳间隔不应打印")

        clock.advance(0.1)
        obs.update(object(), foreground=True, source="pull")
        self.assertEqual(len(lines), before + 1, lines)
        self.assertIn("[STATE] hold", lines[-1])
        self.assertIn("source=pull", lines[-1])

    def test_heartbeat_is_configurable(self):
        obs, lines, clock = self._observer(Feed(mk(fish=T.TRUE)),
                                           heartbeat=2.0)
        obs.update(object(), foreground=True, source="lift")
        obs.update(object(), foreground=True, source="lift")
        before = len(lines)
        clock.advance(2.0)
        obs.update(object(), foreground=True, source="lift")
        self.assertEqual(len(lines), before + 1, lines)

    def test_pending_frame_progress_logs_but_not_state_change(self):
        obs, lines, _ = self._observer(Feed(
            mk(fish=T.TRUE), mk(fish=T.TRUE),
            mk(fish=T.TRUE, ring=0.95)))
        obs.update(object(), foreground=True, source="pull")
        obs.update(object(), foreground=True, source="pull")   # 稳定 fight
        before = len(lines)

        obs.update(object(), foreground=True, source="pull")   # land 1/3
        obs.update(object(), foreground=True, source="pull")   # land 2/3
        self.assertEqual(len(lines), before + 2, lines)
        self.assertIn("pending", lines[-2])
        self.assertIn("cand=land", lines[-2])
        self.assertIn("stable=fight", lines[-1])

    def test_line_contains_all_diagnostic_fields(self):
        obs, lines, _ = self._observer(Feed(
            mk(fish=T.TRUE, bar_level=3, bar_tier=2, ring=0.873)))
        for _ in range(2):
            obs.update(object(), foreground=False, source="wait_bite")
        line = lines[-1]
        for token in ("stable=fight", "cand=fight", "conf=", "fish=true",
                      "bar=3/2", "ring=0.87", "source=wait_bite",
                      "reason="):
            self.assertIn(token, line, line)

    def test_disabled_returns_none_and_never_reads_frame(self):
        lines = []
        obs = ashadow.ShadowObserver(enabled=False, sink=lines.append)
        with mock.patch.object(ashadow, "observe_frame",
                               side_effect=AssertionError("不该被调用")):
            self.assertIsNone(obs.update(object(), foreground=True,
                                         source="pull"))
        self.assertEqual(lines, [])

    def test_env_switch_disables(self):
        with mock.patch.dict(os.environ, {"RF4_SHADOW_STATE": "0"}, clear=False):
            lines = []
            obs = ashadow.ShadowObserver(sink=lines.append)
            self.assertFalse(obs.enabled)
            with mock.patch.object(ashadow, "observe_frame",
                                   side_effect=AssertionError("不该被调用")):
                self.assertIsNone(obs.update(object(), foreground=True,
                                             source="lift"))
            self.assertEqual(lines, [])

    def test_env_switch_default_on(self):
        env = {"RF4_SHADOW_STATE": "1"}
        self.assertTrue(ashadow.shadow_enabled(env))
        self.assertTrue(ashadow.shadow_enabled({}))
        self.assertFalse(ashadow.shadow_enabled({"RF4_SHADOW_STATE": "off"}))

    def test_heartbeat_env_parsing(self):
        self.assertEqual(ashadow.heartbeat_from_env({}),
                         ashadow.DEFAULT_HEARTBEAT)
        self.assertEqual(ashadow.heartbeat_from_env(
            {"RF4_SHADOW_HEARTBEAT_S": "3.5"}), 3.5)
        self.assertEqual(ashadow.heartbeat_from_env(
            {"RF4_SHADOW_HEARTBEAT_S": "bogus"}), ashadow.DEFAULT_HEARTBEAT)
        self.assertEqual(ashadow.heartbeat_from_env(
            {"RF4_SHADOW_HEARTBEAT_S": "0"}), ashadow.DEFAULT_HEARTBEAT)

    def test_sample_interval_env_parsing(self):
        self.assertEqual(ashadow.DEFAULT_SAMPLE, 0.5)
        self.assertEqual(ashadow.sample_interval_from_env({}),
                         ashadow.DEFAULT_SAMPLE)
        self.assertEqual(ashadow.sample_interval_from_env(
            {"RF4_SHADOW_SAMPLE_S": "0.25"}), 0.25)
        for bad in ("bogus", "0", "-1", "nan", "inf"):
            self.assertEqual(ashadow.sample_interval_from_env(
                {"RF4_SHADOW_SAMPLE_S": bad}), ashadow.DEFAULT_SAMPLE, bad)


class TestShadowObserverErrors(unittest.TestCase):
    def test_exception_swallowed_and_warned_three_times(self):
        lines = []
        obs = ashadow.ShadowObserver(enabled=True, clock=FakeClock(),
                                     sink=lines.append)
        with mock.patch.object(ashadow, "observe_frame",
                               side_effect=ValueError("boom")):
            for _ in range(6):
                self.assertIsNone(obs.update(object(), foreground=True,
                                             source="pull"))

        warns = [x for x in lines if "[STATE][warn]" in x]
        self.assertEqual(len(warns), 3, warns)
        self.assertEqual(obs.errors, 6)
        self.assertEqual(obs.suppressed.get("ValueError"), 3)

    def test_each_exception_type_has_its_own_budget(self):
        lines = []
        obs = ashadow.ShadowObserver(enabled=True, clock=FakeClock(),
                                     sink=lines.append)
        with mock.patch.object(ashadow, "observe_frame",
                               side_effect=ValueError("v")):
            for _ in range(6):
                obs.update(object(), foreground=True, source="pull")
        with mock.patch.object(ashadow, "observe_frame",
                               side_effect=TypeError("t")):
            obs.update(object(), foreground=True, source="pull")

        kinds = [x.split(":")[0] for x in lines if "[STATE][warn]" in x]
        self.assertEqual(len(kinds), 4, lines)
        self.assertTrue(any("TypeError" in k for k in kinds), kinds)

    def test_broken_sink_never_escapes(self):
        def boom(_line):
            raise RuntimeError("sink down")

        obs = ashadow.ShadowObserver(enabled=True, clock=FakeClock(), sink=boom)
        with mock.patch.object(ashadow, "observe_frame",
                               return_value=mk(fish=T.TRUE)):
            self.assertIsNotNone(obs.update(object(), foreground=True,
                                            source="pull"))

    def test_tracker_keeps_running_after_one_bad_frame(self):
        obs = ashadow.ShadowObserver(enabled=True, clock=FakeClock(),
                                     sink=lambda _l: None)
        good = mk(fish=T.TRUE)
        with mock.patch.object(ashadow, "observe_frame", return_value=good):
            obs.update(object(), foreground=True, source="pull")
        with mock.patch.object(ashadow, "observe_frame",
                               side_effect=ValueError("bad")):
            self.assertIsNone(obs.update(object(), foreground=True,
                                         source="pull"))
        with mock.patch.object(ashadow, "observe_frame", return_value=good):
            est = obs.update(object(), foreground=True, source="pull")
        self.assertIsNotNone(est)
        self.assertEqual(est.evidence["candidate_frames"], 2,
                         "坏帧不应打断候选帧计数")


# ============================================================ 模块源码约束


class TestShadowModuleSurface(unittest.TestCase):
    FORBIDDEN = (
        "pydirectinput", "ctypes", "win32", "SendInput", "mouse_event",
        "keyDown", "keyUp", "ImageGrab", "screenshot", "capture_game",
        "game_hwnd", "threading", ".csv", "open(",
    )

    def test_source_has_no_input_capture_or_thread(self):
        src = SHADOW.read_text(encoding="utf-8")
        for token in self.FORBIDDEN:
            self.assertNotIn(token, src, f"shadow 模块出现禁用符号: {token}")

    def test_source_does_not_import_adapter(self):
        src = SHADOW.read_text(encoding="utf-8")
        self.assertNotIn("import rf4s_adapter", src)
        self.assertNotIn("from rf4s_adapter", src)

    def test_shadow_module_only_imports_observation_and_state(self):
        tree = ast.parse(SHADOW.read_text(encoding="utf-8"))
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        allowed = {"__future__", "math", "os", "time", "autofish_observation",
                   "autofish_state"}
        self.assertTrue(mods <= allowed, f"出现了额外依赖: {sorted(mods - allowed)}")


# ========================================================== 适配层函数结构


def _adapter_source() -> str:
    return ADAPTER.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    src = _adapter_source()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"rf4s_adapter 里找不到函数 {name}")


class _FakeUser32:
    def __init__(self, fg: int) -> None:
        self.fg = fg

    def GetForegroundWindow(self) -> int:
        return self.fg


class _FakeWindll:
    def __init__(self, fg: int) -> None:
        self.user32 = _FakeUser32(fg)


class _FakeCtypes:
    def __init__(self, fg: int) -> None:
        self.windll = _FakeWindll(fg)


class _FakeObserver:
    def __init__(self, enabled=True, result="ESTIMATE", error=None) -> None:
        self.enabled = enabled
        self.calls = []
        self._result = result
        self._error = error

    def update(self, image, *, foreground, source=""):
        self.calls.append((image, foreground, source))
        if self._error is not None:
            raise self._error
        return self._result


class _Capture:
    def __init__(self, error=None) -> None:
        self.n = 0
        self.error = error

    def __call__(self):
        self.n += 1
        if self.error is not None:
            raise self.error
        return object(), (0, 0)


class FakeMonotonic:
    """可控单调时钟：不推进就不放行新的采样。"""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def _load_shadow_observe(*, fg=42, hwnd=42, observer=None, capture=None,
                         clock=None, interval=0.5):
    """把 `_shadow_observe` 单独 exec 出来，注入假的帧源/窗口/观察者/时钟。"""
    body = _function_source("_shadow_observe")
    observer = observer if observer is not None else _FakeObserver()
    capture = capture if capture is not None else _Capture()
    clock = clock if clock is not None else FakeMonotonic()
    printed: list[str] = []
    state = {"tried": False, "obj": observer, "err": 0,
             "next_t": 0.0, "sample_s": interval}
    ns = {
        "_shadow": state,
        "_shadow_observer": lambda: observer,
        "_shadow_interval": lambda: state["sample_s"],
        "capture_game": capture,
        "ctypes": _FakeCtypes(fg),
        "game_hwnd": lambda: hwnd,
        "time": mock.Mock(monotonic=clock),
        "print": lambda *a, **k: printed.append(" ".join(str(x) for x in a)),
    }
    exec(compile(body, "<shadow_observe>", "exec"), ns)
    return ns["_shadow_observe"], observer, capture, printed, state, clock


class TestAdapterShadowObserve(unittest.TestCase):
    def test_one_capture_per_granted_sample(self):
        fn, observer, capture, _, _, clock = _load_shadow_observe()
        for _ in range(3):                       # 3 次调用落在同一个 0.5s 窗口
            fn("pull")
        self.assertEqual(capture.n, 1, "未到采样间隔，不许重复抓图")
        self.assertEqual(len(observer.calls), 1)

        # 不到 0.5s 仍不抓
        fn("lift")
        self.assertEqual(capture.n, 1)

        # 跨过 0.5s -> 第二个样本
        clock.advance(0.5)
        fn("lift")
        self.assertEqual(capture.n, 2, "到点后应放行第二个样本")
        self.assertEqual([c[2] for c in observer.calls], ["pull", "lift"])

    def test_throttle_is_global_not_per_source(self):
        fn, _, capture, _, _, _ = _load_shadow_observe()
        fn("pull")
        fn("lift")
        fn("wait_bite")
        self.assertEqual(capture.n, 1, "不同 source 共用同一个闸门")

    def test_force_bypasses_throttle_and_pushes_deadline(self):
        fn, _, capture, _, _, clock = _load_shadow_observe(interval=0.5)
        fn("pull")
        self.assertEqual(capture.n, 1)

        fn("cast_before", force=True)             # 未到点也放行
        self.assertEqual(capture.n, 2)

        fn("pull")                                # force 也会推进闸门
        self.assertEqual(capture.n, 2, "force 采样同样重置限频窗口")

        clock.advance(0.5)
        fn("pull")
        self.assertEqual(capture.n, 3)

    def test_foreground_is_window_comparison(self):
        fn, observer, _, _, _, _ = _load_shadow_observe(fg=42, hwnd=42)
        fn("wait_bite")
        self.assertTrue(observer.calls[0][1])

        fn2, observer2, _, _, _, _ = _load_shadow_observe(fg=7, hwnd=42)
        fn2("wait_bite")
        self.assertFalse(observer2.calls[0][1])

    def test_foreground_query_failure_degrades_to_false(self):
        body = _function_source("_shadow_observe")
        observer = _FakeObserver()
        capture = _Capture()

        class _Broken:
            @property
            def windll(self):
                raise OSError("no win32")

        ns = {
            "_shadow": {"tried": False, "obj": observer, "err": 0,
                        "next_t": 0.0, "sample_s": 0.5},
            "_shadow_observer": lambda: observer,
            "_shadow_interval": lambda: 0.5,
            "capture_game": capture,
            "ctypes": _Broken(),
            "game_hwnd": lambda: 42,
            "time": mock.Mock(monotonic=FakeMonotonic()),
            "print": lambda *a, **k: None,
        }
        exec(compile(body, "<shadow_observe>", "exec"), ns)
        result = ns["_shadow_observe"]("pull")
        self.assertEqual(result, "ESTIMATE", "取前台失败只降级，不影响观测")
        self.assertFalse(observer.calls[0][1],
                         "取前台失败必须降级为 False，而不是抛异常")

    def test_disabled_observer_skips_capture(self):
        fn, observer, capture, _, state, _ = _load_shadow_observe(
            observer=_FakeObserver(enabled=False))
        for _ in range(5):
            self.assertIsNone(fn("pull"))
        self.assertEqual(capture.n, 0, "关闭 shadow 后不许再抓帧")
        self.assertEqual(observer.calls, [])
        self.assertEqual(state["next_t"], 0.0, "关闭时不该占用限频窗口")

    def test_capture_failure_is_swallowed_and_still_throttled(self):
        fn, observer, capture, printed, state, clock = _load_shadow_observe(
            capture=_Capture(error=RuntimeError("printwindow failed")))
        for _ in range(5):                       # 同一窗口内连续失败
            self.assertIsNone(fn("lift"))
        self.assertEqual(capture.n, 1, "失败也要限频，不能刷爆 PrintWindow")
        self.assertEqual(state["err"], 1)
        self.assertEqual(len(printed), 1)
        self.assertEqual(len(observer.calls), 0)

        # 每次跨过一个窗口再试：总次数上去，警告仍然只打 3 条
        for _ in range(4):
            clock.advance(0.5)
            fn("lift")
        self.assertEqual(capture.n, 5)
        self.assertEqual(state["err"], 5)
        self.assertEqual(len(printed), 3, "适配层同样只警告前 3 次")

    def test_observer_exception_is_swallowed(self):
        fn, _, capture, _, _, _ = _load_shadow_observe(
            observer=_FakeObserver(error=ValueError("bad frame")))
        self.assertIsNone(fn("settle_before", force=True))
        self.assertEqual(capture.n, 1)

    def test_interval_parses_env_once(self):
        body = _function_source("_shadow_interval")
        state = {"sample_s": None}
        ns = {"_shadow": state}
        exec(compile(body, "<shadow_interval>", "exec"), ns)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RF4_SHADOW_SAMPLE_S", None)
            self.assertEqual(ns["_shadow_interval"](), 0.5, "缺省 0.5 秒")
        self.assertEqual(state["sample_s"], 0.5)

        frozen = {"sample_s": None}
        ns2 = {"_shadow": frozen}
        exec(compile(body, "<shadow_interval>", "exec"), ns2)
        with mock.patch.dict(os.environ, {"RF4_SHADOW_SAMPLE_S": "0.25"}):
            self.assertEqual(ns2["_shadow_interval"](), 0.25)
        self.assertEqual(frozen["sample_s"], 0.25, "解析一次后应缓存")

    def test_body_has_no_control_or_input_symbols(self):
        body = _function_source("_shadow_observe")
        for token in ("keyDown", "keyUp", "wheel", "friction", "self.",
                      "pydirectinput", "keyUp", "press(", "sleep("):
            self.assertNotIn(token, body, f"_shadow_observe 出现禁用符号: {token}")

    def test_body_has_no_stage_or_timer_mutation(self):
        body = _function_source("_shadow_observe")
        for token in ("self.stage", "self.timer", "_fric", "idle_since",
                      "time.sleep"):
            self.assertNotIn(token, body)


class TestAdapterWiringPoints(unittest.TestCase):
    """接入点必须是独立语句，返回值绝不进入控制流。"""

    CALL = re.compile(
        r"^_shadow_observe\((['\"])(?P<src>[a-z_]+)\1"
        r"(?P<force>,\s*force=True)?\)\s*(?:#.*)?$")

    def _call_lines(self):
        found = []
        for line in _adapter_source().splitlines():
            stripped = line.strip()
            if stripped.startswith("def _shadow_observe("):
                continue
            if "_shadow_observe(" not in stripped:
                continue
            found.append(stripped)
        return found

    def test_all_call_sites_are_standalone_statements(self):
        calls = self._call_lines()
        self.assertTrue(calls)
        for stripped in calls:
            self.assertRegex(stripped, self.CALL,
                             f"接入点必须是独立表达式语句: {stripped!r}")

    def test_return_value_never_used_for_control(self):
        src = _adapter_source()
        self.assertNotRegex(src, r"=\s*_shadow_observe\(")
        self.assertNotRegex(
            src, r"(?:if|while|return|assert|and|or|not|elif)\s+_shadow_observe\(")

    def test_expected_sources_are_wired(self):
        sources = []
        for stripped in self._call_lines():
            m = self.CALL.match(stripped)
            if m:
                sources.append(m.group("src"))
        for expected in ("wait_bite", "pull", "lift",
                         "cast_before", "cast_after",
                         "settle_before", "settle_after"):
            self.assertIn(expected, sources, f"缺少接入点 {expected}: {sources}")

    def test_only_cast_and_settle_force(self):
        forced = []
        for stripped in self._call_lines():
            m = self.CALL.match(stripped)
            if m and m.group("force"):
                forced.append(m.group("src"))
        self.assertEqual(sorted(forced),
                         ["cast_after", "cast_before",
                          "settle_after", "settle_before"],
                         "只有抛竿/入护的前后对照片可以 force")

    def test_call_sites_are_not_import_time_side_effects(self):
        """接入点必须在函数体内（缩进 >= 8），不能在模块顶层裸跑。"""
        for line in _adapter_source().splitlines():
            stripped = line.strip()
            if stripped.startswith("def _shadow_observe("):
                continue
            if not stripped.startswith("_shadow_observe("):
                continue
            indent = len(line) - len(line.lstrip())
            self.assertGreaterEqual(indent, 8, f"接入点缩进异常: {line!r}")

    def test_shadow_observe_catches_everything(self):
        body = _function_source("_shadow_observe")
        self.assertIn("except BaseException", body,
                      "必须兜住 BaseException（含 SystemExit），否则会中断 bot")

    def test_shadow_init_failure_does_not_block_startup(self):
        body = _function_source("_shadow_observer")
        self.assertIn("except BaseException", body)
        self.assertIn("_shadow[\"obj\"] = None", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
