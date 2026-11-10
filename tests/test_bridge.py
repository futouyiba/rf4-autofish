"""离线桥接测试：不抓屏、不按键、不联网、不导入真实适配器。"""

from __future__ import annotations

import ast
import contextlib
import io
import os
import pathlib
import subprocess
import sys
import types
import unittest
from unittest import mock

BOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT))

import autofish_bridge as ab  # noqa: E402
import autofish_observation as aobs  # noqa: E402
import autofish_lifecycle as alf  # noqa: E402
from autofish_control import AutoStop, ControlSession  # noqa: E402
from autofish_input import F5, F6, InputArbiter  # noqa: E402

from PIL import Image  # noqa: E402

T = aobs.TriState
READY_TEMPLATE = BOT / "static" / "ready_cn.png"
FULLFLOW = BOT / "_fullflow_fault.png"
READY_CAST = BOT / "_ready_cast.png"
READY_EN = BOT / "_ready.png"
WATERSIDE = BOT / "_waterside.png"


class FakeImage:
    def __init__(self, size=(1920, 1080)):
        self.size = size


def plain_frame(size=(1920, 1080), color=(35, 40, 50)):
    return Image.new("RGB", size, color)


def ready_frame():
    """合成一张只在底部固定位置带中文 readiness 提示的 1080p 帧。"""
    base = plain_frame()
    with Image.open(READY_TEMPLATE) as tpl:
        base.paste(tpl.convert("RGB"), ab.READY_CN_ANCHOR)
    return base


class FakeFrictionState:
    def __init__(self):
        self.invalidated = 0
        self.set_calls = []

    def invalidate(self):
        self.invalidated += 1

    def set(self, cur, calibrated=True):
        self.set_calls.append((int(cur), bool(calibrated)))


class FakeAdapter:
    REF_DIR = BOT
    FRICTION_MIN = 1
    FRICTION_MAX = 29
    FRICTION_INITIAL = 17
    FRICTION_ABS_TOP = 30
    CAPTURE_KEEP_BTN = (829, 974)
    OK_BTN_CENTER = (960, 810)
    _MOUSEEVENTF_LEFTUP = 0x0004

    def __init__(self):
        self.image = FakeImage()
        self.images = []
        self.capture_calls = 0
        self.strong_calls = []
        self.strong_score = 0.0
        self.frame_valid = True
        self.click_calls = []
        self.send_mouse_calls = []
        self.find_btn = None
        self.keep_args = None
        self.keep_exc = None
        self._fric = {"cur": 17, "calibrated": True, "t": 0.0}
        self._lift = {"held": False}
        self.friction_state = FakeFrictionState()
        self.friction_reset_calls: list[bool] = []

    def capture_game(self):
        self.capture_calls += 1
        if self.images:
            index = min(len(self.images) - 1, self.capture_calls - 1)
            return self.images[index], (0, 0)
        return self.image, (0, 0)

    def _fight_strong_capture(self, image, path):
        self.strong_calls.append((image, path))
        return self.strong_score

    def _fight_frame_valid(self, image):
        return bool(self.frame_valid)

    def _click_client(self, cx, cy):
        self.click_calls.append((int(cx), int(cy)))

    def _send_mouse(self, flags, dx=0, dy=0, data=0):
        self.send_mouse_calls.append(flags)

    def find_capture_keep_btn(self):
        return self.find_btn

    def click_keep_until_settled(self, detect, locate, click, wait, dismiss=None):
        self.keep_args = {
            "detect": detect,
            "locate": locate,
            "click": click,
            "wait": wait,
            "dismiss": dismiss,
        }
        if self.keep_exc is not None:
            raise self.keep_exc
        return 1

    def friction_controller_step(self, win):
        self.fric_win = list(win)

    def friction_reset_observation(self, keep_last_action=True):
        self.friction_reset_calls.append(bool(keep_last_action))

    def _wheel_raw(self, n):
        return True


class FakePdi:
    def __init__(self):
        self.calls = []

    def keyDown(self, key):
        self.calls.append((key, True))

    def keyUp(self, key):
        self.calls.append((key, False))


class FakeFishCoordinator:
    """只暴露 bridge 需要的 fish_id；用于验证跨鱼观察窗重置。"""

    def __init__(self, fish_id=None):
        self.fish_id = fish_id


class FakeControl:
    def __init__(self):
        self.stopped = False
        self.stop_reason = ""
        self.wait_calls = []

    def request_stop(self, reason="stop"):
        self.stopped = True
        self.stop_reason = reason

    def checkpoint(self):
        if self.stopped:
            raise AutoStop(self.stop_reason or "stop")

    def wait(self, seconds, quantum=0.05):
        self.wait_calls.append(float(seconds))
        return bool(self.stopped)


class OneShotStopControl(FakeControl):
    """第一次 wait 就置停止位，用于验证滚轮中途失败的前缀记账。"""

    def __init__(self):
        super().__init__()
        self._tripped = False

    def wait(self, seconds, quantum=0.05):
        self.wait_calls.append(float(seconds))
        if not self._tripped:
            self._tripped = True
            self.stopped = True
            self.stop_reason = "stop_during_wait"
            return True
        return True


class StateControl(FakeControl):
    """模拟 ActionControl 的实时只读状态契约。"""

    def __init__(self, state):
        super().__init__()
        self.state = state


class TickingControl(FakeControl):
    """wait 时推进注入时钟，用于验证释放后的 ready 宽限窗口。"""

    def __init__(self, clock, step=0.05):
        super().__init__()
        self.clock = clock
        self.step = float(step)

    def wait(self, seconds, quantum=0.05):
        self.wait_calls.append(float(seconds))
        self.clock.advance(self.step)
        return bool(self.stopped)


class FakeClock:
    def __init__(self, step=0.01, start=1000.0):
        self.value = float(start)
        self.step = float(step)

    def __call__(self):
        self.value += self.step
        return self.value


class ScriptClock:
    """按调用顺序返回值，用于验证“先消费帧证据、后判 deadline”。"""

    def __init__(self, values):
        self.values = [float(v) for v in values]
        self.index = 0

    def __call__(self):
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value


def make_obs(*, fish=T.FALSE, ring=None, ok_modal=T.FALSE, ts=1.0):
    return aobs.Observation(
        monotonic_ts=float(ts),
        wall_ts=0.0,
        foreground=True,
        fish_icon=fish,
        bar_level=None,
        bar_tier=None,
        bar_hue=None,
        bar_sat=None,
        ring_ratio=ring,
        captured=T.FALSE,
        ok_modal=ok_modal,
        hud_visible=T.TRUE,
        evidence={},
    )


class BridgeImportTests(unittest.TestCase):
    def test_import_does_not_import_adapter(self) -> None:
        code = (
            "import sys; import autofish_bridge; "
            "print('1' if 'rf4s_adapter' in sys.modules else '0')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(BOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "0")

    def test_preflight_window_mismatch_reports_fault_without_resize(self) -> None:
        adapter = FakeAdapter()
        adapter.game_hwnd = lambda: 7
        with mock.patch.object(ab, "_read_client_size", return_value=(1280, 720)):
            hwnd, keep, ready, keepnet, error = ab._preflight(adapter)
        self.assertEqual(hwnd, 7)
        self.assertIsNone(keep)
        self.assertIsNone(ready)
        self.assertIsNone(keepnet)
        self.assertTrue(error.startswith("window_size_1280x720"))

    def test_top_level_ast_has_no_adapter_import(self) -> None:
        source = (BOT / "autofish_bridge.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
        self.assertNotIn("rf4s_adapter", names)
        self.assertNotIn("pydirectinput", names)

    def test_import_does_not_import_input_backend(self) -> None:
        code = (
            "import sys; import autofish_bridge; "
            "print('1' if 'pydirectinput' in sys.modules else '0')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(BOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "0")

    def test_main_ast_passes_pdi_keyword_to_bridge(self) -> None:
        source = (BOT / "autofish_bridge.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        main_def = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        calls = [
            node
            for node in ast.walk(main_def)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Bridge"
        ]
        self.assertEqual(len(calls), 1)
        keywords = {kw.arg: kw.value for kw in calls[0].keywords}
        self.assertIn("pdi", keywords)
        # Must be the lazily imported backend variable, never a literal None.
        self.assertIsInstance(keywords["pdi"], ast.Name)
        self.assertEqual(keywords["pdi"].id, "input_backend")


class FakeServer:
    def __init__(self, control, endpoint=None):
        self.control = control
        self.endpoint_path = endpoint
        self.started = False
        self.closed = 0

    def start(self):
        self.started = True

    def close(self):
        self.closed += 1


class FakeRuntime:
    last = None

    def __init__(
        self, callbacks, control=None, *, config=None, initial_auto=False, server=None
    ):
        self.callbacks = callbacks
        self.control = control
        self.config = config
        self.initial_auto = initial_auto
        self.server = server
        self.coordinator = mock.Mock()
        self.ran = False
        self.closed = 0
        FakeRuntime.last = self

    def run(self):
        self.ran = True

    def shutdown(self):
        pass

    def close(self):
        self.closed += 1


class MainWiringTests(unittest.TestCase):
    """Simulated production main(); import itself stays side-effect free."""

    def setUp(self) -> None:
        self.pdi = types.ModuleType("pydirectinput")
        self.pdi.PAUSE = 0.123
        self.pdi.keyDown = lambda _key: None
        self.pdi.keyUp = lambda _key: None
        self.control = FakeControl()
        self.bot_ctl = types.ModuleType("bot_ctl")
        self.bot_ctl.write_process_identity = mock.Mock()
        self.ipc = types.ModuleType("autofish_ipc")
        self.ipc.RuntimeServer = FakeServer
        self.runtime_mod = types.ModuleType("autofish_runtime")
        self.runtime_mod.AutoFishRuntime = FakeRuntime
        FakeRuntime.last = None

    def _main(self, modules, preflight_error=None):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(sys.modules, modules))
            stack.enter_context(mock.patch.object(ab, "_write_pid"))
            stack.enter_context(mock.patch.object(ab, "_remove_pid"))
            stack.enter_context(
                mock.patch.object(ab, "_load_adapter", return_value=FakeAdapter())
            )
            stack.enter_context(
                mock.patch.object(
                    ab,
                    "_preflight",
                    return_value=(7, None, None, None, preflight_error),
                )
            )
            stack.enter_context(
                mock.patch.object(ab, "ControlSession", return_value=self.control)
            )
            bridge_cls = stack.enter_context(mock.patch.object(ab, "Bridge"))
            bridge = mock.Mock()
            bridge.control = self.control
            bridge_cls.return_value = bridge
            out = io.StringIO()
            stack.enter_context(contextlib.redirect_stdout(out))
            rc = ab.main([])
        return rc, bridge_cls, bridge, out.getvalue()

    def test_production_main_passes_lazy_input_backend(self) -> None:
        rc, bridge_cls, bridge, output = self._main(
            {
                "bot_ctl": self.bot_ctl,
                "pydirectinput": self.pdi,
                "autofish_ipc": self.ipc,
                "autofish_runtime": self.runtime_mod,
            }
        )
        self.assertEqual(rc, 0, output)
        bridge_cls.assert_called_once()
        self.assertIsNotNone(bridge_cls.call_args.args[0])
        kwargs = bridge_cls.call_args.kwargs
        self.assertIs(kwargs["pdi"], self.pdi)
        self.assertEqual(self.pdi.PAUSE, 0.0)
        self.assertIs(kwargs["control"], self.control)
        self.assertIsNone(kwargs["preflight_error"])
        self.assertIs(kwargs["auto_net_enabled"], False)
        bridge.install.assert_called_once()
        self.assertIsNotNone(FakeRuntime.last)
        self.assertFalse(FakeRuntime.last.initial_auto)
        # 生产必须显式关闭自动抄网（底层默认也是 False，这里是双保险）。
        self.assertIs(FakeRuntime.last.config.fight.auto_net_enabled, False)
        self.assertTrue(FakeRuntime.last.ran)

    def test_rf4_auto_net_env_is_single_source_for_bridge_and_fight_cfg(self) -> None:
        with mock.patch.dict(os.environ, {"RF4_AUTO_NET": "1"}):
            rc, bridge_cls, bridge, output = self._main(
                {
                    "bot_ctl": self.bot_ctl,
                    "pydirectinput": self.pdi,
                    "autofish_ipc": self.ipc,
                    "autofish_runtime": self.runtime_mod,
                }
            )
        self.assertEqual(rc, 0, output)
        kwargs = bridge_cls.call_args.kwargs
        self.assertIs(kwargs["auto_net_enabled"], True)
        self.assertIs(kwargs["fight_auto_net_enabled"], True)
        self.assertIs(FakeRuntime.last.config.fight.auto_net_enabled, True)

    def test_preflight_failure_does_not_import_input_backend(self) -> None:
        with mock.patch.dict(sys.modules):
            sys.modules.pop("pydirectinput", None)
            rc, bridge_cls, bridge, output = self._main(
                {
                    "bot_ctl": self.bot_ctl,
                    "autofish_ipc": self.ipc,
                    "autofish_runtime": self.runtime_mod,
                },
                preflight_error="window_size_bad",
            )
            self.assertNotIn("pydirectinput", sys.modules)
        self.assertEqual(rc, 0, output)
        kwargs = bridge_cls.call_args.kwargs
        self.assertIsNone(kwargs["pdi"])
        self.assertIsNone(kwargs["keep_path"])
        self.assertEqual(kwargs["preflight_error"], "window_size_bad")
        bridge.install.assert_not_called()


class BridgeBase(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = FakeAdapter()
        self.pdi = FakePdi()
        self.control = FakeControl()
        self.clock = FakeClock()
        self.sent = []
        self.foreground = True
        self.key_down = False
        self.logs: list[str] = []
        self.bridge = ab.Bridge(
            self.adapter,
            control=self.control,
            pdi=self.pdi,
            keep_path=BOT / "keep.png",
            ready_path=BOT / "ready.png",
            ready_cn_path=READY_TEMPLATE,
            keepnet_path=BOT / "keepnet_is_full.png",
            foreground=lambda: self.foreground,
            clock=self.clock,
            wheel_sender=self.sent.append,
            key_state=lambda key: self.key_down if key == "shift" else False,
            log=self.logs.append,
        )
        self.arbiter = InputArbiter(self.bridge.sink)
        self.bridge.set_arbiter(self.arbiter)

    def tearDown(self) -> None:
        self.bridge.restore()


class CaptureTests(BridgeBase):
    def test_capture_uses_one_frame_for_observe_and_strong(self) -> None:
        self.adapter.strong_score = 0.99
        obs = self.bridge.capture_observation()
        self.assertEqual(self.adapter.capture_calls, 1)
        self.assertEqual(len(self.adapter.strong_calls), 1)
        image, path = self.adapter.strong_calls[0]
        self.assertIs(image, self.adapter.image)
        self.assertEqual(path, str(BOT / "keep.png"))
        self.assertIs(obs.captured, T.TRUE)

    def test_capture_template_none_is_explicit_fault(self) -> None:
        self.adapter.strong_score = None
        with self.assertRaises(ab.BridgeFault):
            self.bridge.capture_observation()

    def test_preflight_fault_raises_once_then_reports_unknown(self) -> None:
        bridge = ab.Bridge(None, preflight_error="window_size_bad", log=lambda _t: None)
        with self.assertRaises(ab.BridgeFault):
            bridge.capture_observation()
        obs = bridge.capture_observation()
        self.assertFalse(obs.foreground)
        self.assertIs(obs.captured, T.UNKNOWN)
        self.assertFalse(bridge._foreground())

    def test_readiness_never_guesses(self) -> None:
        obs = make_obs(ts=12.5)
        self.bridge._last_image = plain_frame()
        self.bridge._last_ts = 12.5
        self.assertIs(self.bridge.readiness(obs), False)
        stale = make_obs(ts=99.0)
        self.assertIsNone(self.bridge.readiness(stale))

    def test_readiness_hits_chinese_template_at_fixed_roi(self) -> None:
        obs = make_obs(ts=12.5)
        self.bridge._last_image = ready_frame()
        self.bridge._last_ts = 12.5
        self.assertIs(self.bridge.readiness(obs), True)
        self.assertTrue(any("[READY] hit score=" in line for line in self.logs))

    def test_readiness_vetoes_captured_modal_and_fish(self) -> None:
        for obs in (
            make_obs(ts=12.5, fish=T.TRUE),
            make_obs(ts=12.5, ok_modal=T.TRUE),
        ):
            self.bridge._last_image = ready_frame()
            self.bridge._last_ts = 12.5
            self.assertIs(self.bridge.readiness(obs), False)
        self.bridge._last_image = ready_frame()
        self.bridge._last_ts = 12.5
        strong = make_obs(ts=12.5)
        strong = mock.Mock(
            monotonic_ts=12.5,
            foreground=True,
            captured=T.TRUE,
            ok_modal=T.FALSE,
            fish_icon=T.FALSE,
        )
        self.assertIs(self.bridge.readiness(strong), False)

    def test_readiness_rejects_low_variance_template(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "flat.png"
            Image.new("L", (172, 27), 128).save(path)
            bridge = ab.Bridge(
                self.adapter,
                ready_cn_path=path,
                foreground=lambda: True,
                clock=FakeClock(),
                log=self.logs.append,
            )
            obs = make_obs(ts=1.0)
            bridge._last_image = ready_frame()
            bridge._last_ts = 1.0
            self.assertIsNone(bridge.readiness(obs))


class RealScreenshotReadinessTests(unittest.TestCase):
    """真实截图回归：只有带中文提示的帧才是 ready。"""

    @classmethod
    def setUpClass(cls) -> None:
        missing = [
            str(path)
            for path in (FULLFLOW, READY_CAST, READY_EN, WATERSIDE)
            if not path.is_file()
        ]
        if missing:
            raise unittest.SkipTest("offline screenshots missing: %s" % missing)

    def _readiness(self, path):
        adapter = FakeAdapter()
        bridge = ab.Bridge(
            adapter,
            ready_cn_path=READY_TEMPLATE,
            foreground=lambda: True,
            clock=FakeClock(),
            log=lambda _t: None,
        )
        with Image.open(path) as im:
            image = im.convert("RGB")
        obs = aobs.observe_frame(image, foreground=True)
        bridge._last_image = image
        bridge._last_ts = obs.monotonic_ts
        return bridge.readiness(obs)

    def test_chinese_prompt_screenshots_are_ready(self) -> None:
        self.assertIs(self._readiness(FULLFLOW), True)
        self.assertIs(self._readiness(READY_CAST), True)

    def test_full_ring_without_prompt_is_not_ready(self) -> None:
        self.assertIs(self._readiness(READY_EN), False)
        self.assertIs(self._readiness(WATERSIDE), False)

    def test_fish_icon_vetoes_even_a_ready_screenshot(self) -> None:
        adapter = FakeAdapter()
        bridge = ab.Bridge(
            adapter,
            ready_cn_path=READY_TEMPLATE,
            foreground=lambda: True,
            clock=FakeClock(),
            log=lambda _t: None,
        )
        with Image.open(READY_CAST) as im:
            image = im.convert("RGB")
        obs = aobs.observe_frame(image, foreground=True)
        obs = mock.Mock(
            monotonic_ts=obs.monotonic_ts,
            foreground=True,
            captured=T.FALSE,
            ok_modal=T.FALSE,
            fish_icon=T.TRUE,
        )
        bridge._last_image = image
        bridge._last_ts = obs.monotonic_ts
        self.assertIs(bridge.readiness(obs), False)

    def test_ready_prompt_clears_full_ring_for_probing(self) -> None:
        adapter = FakeAdapter()
        with Image.open(FULLFLOW) as im:
            adapter.image = im.convert("RGB")
        bridge = ab.Bridge(
            adapter,
            keep_path=BOT / "keep.png",
            ready_cn_path=READY_TEMPLATE,
            foreground=lambda: True,
            clock=FakeClock(),
            log=lambda _t: None,
        )
        obs = bridge.capture_observation()
        self.assertIsNone(obs.ring_ratio)
        self.assertIs(bridge.readiness(obs), True)

    def test_probing_uses_readiness_before_ring(self) -> None:
        adapter = FakeAdapter()
        with Image.open(FULLFLOW) as im:
            adapter.image = im.convert("RGB")
        bridge = ab.Bridge(
            adapter,
            keep_path=BOT / "keep.png",
            ready_cn_path=READY_TEMPLATE,
            foreground=lambda: True,
            clock=FakeClock(),
            log=lambda _t: None,
        )
        obs = bridge.capture_observation()
        callbacks = alf.RuntimeCallbacks(
            capture_observation=lambda: obs,
            input_sink=lambda *_args: None,
            readiness=bridge.readiness,
            log=lambda _text: None,
            clock=lambda: 0.0,
        )
        coord = alf.LifecycleCoordinator(
            callbacks,
            initial_auto=True,
            config=alf.LifecycleConfig(tick_interval=0.001),
        )
        coord._frame_ok = True
        coord._foreground = True
        coord._last_frame_ts = obs.monotonic_ts
        coord._tick_probing(obs, 1.0)
        self.assertIs(coord.state, alf.AutoFishState.READY)


class ManualClock:
    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class _CoordinatorState:
    def __init__(self, name):
        self.name = str(name)


class FakeCoordinator:
    def __init__(self, *, auto=True, state_name="PROBING", control=None):
        self.auto_requested = bool(auto)
        self.state = _CoordinatorState(state_name)
        self.control = control
        self.control_mode = "AUTO" if auto else "OBSERVE"

    def mode(self):
        return _CoordinatorState(self.control_mode)


class ForegroundRecoveryTests(unittest.TestCase):
    """模式受控的前台恢复：只拉回 AUTO，且永远用新帧。"""

    def setUp(self) -> None:
        self.adapter = FakeAdapter()
        self.pdi = FakePdi()
        self.control = FakeControl()
        self.clock = ManualClock()
        self.foreground = False
        self.focus_calls: list[float] = []
        self.logs: list[str] = []
        self.new_frame = plain_frame(color=(10, 20, 30))
        self.old_frame = plain_frame(color=(90, 90, 90))
        self.adapter.image = self.new_frame
        self.bridge = ab.Bridge(
            self.adapter,
            control=self.control,
            pdi=self.pdi,
            keep_path=BOT / "keep.png",
            ready_path=BOT / "ready.png",
            ready_cn_path=READY_TEMPLATE,
            foreground=lambda: self.foreground,
            clock=self.clock,
            wheel_sender=lambda _delta: None,
            log=self.logs.append,
            focus=self._focus,
        )
        self.arbiter = InputArbiter(self.bridge.sink)
        self.bridge.set_arbiter(self.arbiter)
        self.coord = FakeCoordinator(control=self.control)
        self.bridge.attach(self.coord)

    def _focus(self):
        self.focus_calls.append(self.clock.now)
        self.foreground = True
        return True

    def _focus_fails(self):
        self.focus_calls.append(self.clock.now)
        return False

    def test_auto_background_focuses_once_and_recaptures_new_frame(self) -> None:
        self.bridge._last_image = self.old_frame
        self.bridge._last_ts = 123.0

        obs = self.bridge.capture_observation()

        self.assertEqual(len(self.focus_calls), 1)
        self.assertEqual(self.adapter.capture_calls, 1)
        self.assertTrue(obs.foreground)
        self.assertIs(self.bridge._last_image, self.new_frame)
        self.assertIsNot(self.bridge._last_image, self.old_frame)
        # 强捕获/就绪都必须基于聚焦后的新帧。
        self.assertIs(self.adapter.strong_calls[0][0], self.new_frame)

    def test_observe_stop_and_fault_never_focus(self) -> None:
        self.coord.auto_requested = False
        obs = self.bridge.capture_observation()
        self.assertFalse(obs.foreground)
        self.assertEqual(self.focus_calls, [])

        self.foreground = False
        self.coord.auto_requested = True
        self.coord.state = _CoordinatorState("FAULT")
        self.bridge.capture_observation()
        self.assertEqual(self.focus_calls, [])

        self.coord.state = _CoordinatorState("PROBING")
        self.control.request_stop("operator")
        obs = self.bridge.capture_observation()
        self.assertFalse(obs.foreground)
        self.assertEqual(self.focus_calls, [])

    def test_pending_observe_command_blocks_focus(self) -> None:
        session = ControlSession("pending-observe")
        self.coord.control = session
        self.bridge.control = session
        session.enqueue_command({"id": "o", "name": "observe"})

        obs = self.bridge.capture_observation()

        self.assertFalse(obs.foreground)
        self.assertEqual(self.focus_calls, [])

    def test_recovery_attempts_are_throttled_and_bounded(self) -> None:
        self.bridge._focus_fn = self._focus_fails

        self.bridge.capture_observation()
        self.assertEqual(len(self.focus_calls), 1)

        self.clock.advance(0.5)
        self.bridge.capture_observation()
        self.assertEqual(len(self.focus_calls), 1)  # 1s 节流

        self.clock.advance(0.6)
        self.bridge.capture_observation()
        self.assertEqual(len(self.focus_calls), 2)

        # 连续失败达到上限后不再抢窗口，即使已过很久。
        self.clock.advance(30.0)
        self.bridge.capture_observation()
        self.assertEqual(len(self.focus_calls), 2)

    def test_recovery_failure_keeps_stale_frame_inert(self) -> None:
        self.bridge._focus_fn = self._focus_fails
        self.bridge._last_image = self.old_frame
        self.bridge._last_ts = 5.0

        cast_calls: list[str] = []

        def _cast(_control):
            cast_calls.append("cast")
            return None

        coord = alf.LifecycleCoordinator(
            alf.RuntimeCallbacks(
                capture_observation=self.bridge.capture_observation,
                input_sink=self.bridge.sink,
                readiness=self.bridge.readiness,
                cast=_cast,
                log=lambda _text: None,
                clock=self.clock,
            ),
            control=ControlSession("recovery-test"),
            initial_auto=True,
            config=alf.LifecycleConfig(tick_interval=0.001),
        )
        self.bridge.attach(coord)

        coord.step()
        snap = coord.step()

        self.assertIs(coord.state, alf.AutoFishState.PROBING)
        self.assertEqual(snap["control_mode"], "BLOCKED")
        self.assertEqual(cast_calls, [])
        self.assertEqual(self.pdi.calls, [])
        self.assertIs(self.bridge._last_image, self.new_frame)
        self.assertIsNot(self.bridge._last_image, self.old_frame)

    def test_action_and_sink_reuse_ensure_foreground(self) -> None:
        self.assertIsNone(self.bridge.calibrate(FakeControl()))
        self.assertEqual(len(self.focus_calls), 1)
        self.assertEqual(self.adapter._fric["cur"], 17)

        self.foreground = False
        self.clock.advance(1.1)
        self.bridge.sink(F5, True, "test")
        self.assertEqual(self.pdi.calls[-1], (F5, True))
        self.assertEqual(len(self.focus_calls), 2)


class InputTests(BridgeBase):
    def test_down_requires_foreground_but_up_never_skipped(self) -> None:
        self.foreground = False
        with self.assertRaises(RuntimeError):
            self.bridge.sink(F5, True, "test")
        self.assertEqual(self.pdi.calls, [])
        self.bridge.sink(F5, False, "test")
        self.assertEqual(self.pdi.calls, [(F5, False)])

    def test_stop_blocks_new_down(self) -> None:
        self.control.request_stop("operator")
        with self.assertRaises(RuntimeError):
            self.bridge.sink(F5, True, "test")
        self.assertEqual(self.pdi.calls, [])

    def test_f6_syncs_adapter_lift_ledger(self) -> None:
        self.bridge.sink(F6, True, "fight")
        self.assertTrue(self.adapter._lift["held"])
        self.bridge.sink(F6, False, "fight")
        self.assertFalse(self.adapter._lift["held"])

    def _run_cast(self, frames, obs_factory, *, key_state=True, control=None):
        self.adapter.images = list(frames)
        self.bridge._key_state = lambda _key: key_state
        calls = {"n": 0}

        def _obs(*_args, **kwargs):
            calls["n"] += 1
            ts = kwargs.get("monotonic_ts", 1.0)
            return obs_factory(calls["n"], ts)

        with mock.patch.object(ab.aobs, "observe_frame", side_effect=_obs):
            return self.bridge.cast(control or self.control)

    def test_cast_orders_shift_before_f5_and_releases(self) -> None:
        self.clock.step = 0.25
        result = self._run_cast(
            [ready_frame(), plain_frame()],
            lambda call, ts: (
                make_obs(ts=ts, ring=1.0)
                if call == 1
                else make_obs(ts=ts, ring=0.42)
            ),
        )
        self.assertIsNone(result)
        self.assertEqual(
            self.pdi.calls,
            [
                (ab.SHIFT, True),
                (F5, True),
                (F5, False),
                (ab.SHIFT, False),
                (F5, True),
                (F5, False),
            ],
        )
        self.assertIn(ab.CAST_SHIFT_SETTLE, self.control.wait_calls)
        self.assertEqual(
            [call for call in self.pdi.calls if call[0] == F5],
            [(F5, True), (F5, False), (F5, True), (F5, False)],
        )

    def test_cast_without_ready_sends_nothing_and_returns_false(self) -> None:
        self.adapter.image = plain_frame()
        self.assertIs(self.bridge.cast(self.control), False)
        self.assertEqual(self.pdi.calls, [])
        self.assertTrue(
            any("original_cause=not_ready" in line for line in self.logs)
        )

    def test_cast_ready_persist_returns_false_without_second_f5(self) -> None:
        self.clock.step = 0.25
        result = self._run_cast(
            [ready_frame(), ready_frame()],
            lambda _call, ts: make_obs(ts=ts, ring=1.0),
        )
        self.assertIs(result, False)
        self.assertEqual(
            [call for call in self.pdi.calls if call[0] == F5],
            [(F5, True), (F5, False)],
        )
        self.assertTrue(
            any("original_cause=ready_persist" in line for line in self.logs)
        )

    def test_cast_full_ring_after_release_is_not_positive_evidence(self) -> None:
        self.clock.step = 0.25
        result = self._run_cast(
            [ready_frame(), plain_frame()],
            lambda _call, ts: make_obs(ts=ts, ring=1.0),
        )
        self.assertIs(result, False)
        self.assertEqual(
            [call for call in self.pdi.calls if call[0] == F5],
            [(F5, True), (F5, False)],
        )
        self.assertTrue(
            any("original_cause=no_cast_evidence" in line for line in self.logs)
        )

    def test_cast_aborts_when_get_async_key_state_says_shift_up(self) -> None:
        self.clock.step = 0.25
        result = self._run_cast(
            [ready_frame(), plain_frame()],
            lambda _call, ts: make_obs(ts=ts, ring=1.0),
            key_state=False,
        )
        self.assertIs(result, False)
        self.assertEqual(
            self.pdi.calls, [(ab.SHIFT, True), (ab.SHIFT, False)]
        )
        self.assertFalse(any(key == F5 for key, _down in self.pdi.calls))

    def _attach_auto_coordinator(self, control, *, auto=True):
        coord = FakeCoordinator(auto=auto, state_name="CASTING", control=control)
        coord.arbiter = self.arbiter
        self.bridge.attach(coord)
        return coord

    def test_cast_uses_explicit_casting_state_authorization(self) -> None:
        self.clock.step = 0.25
        self.key_down = True
        control = StateControl("CASTING")
        self._attach_auto_coordinator(control)
        self.adapter.images = [plain_frame(), plain_frame()]
        calls = {"n": 0}

        def _obs(*_args, **kwargs):
            calls["n"] += 1
            ts = kwargs.get("monotonic_ts", 1.0)
            ring = 1.0 if calls["n"] == 1 else 0.4
            return make_obs(ts=ts, ring=ring)

        with mock.patch.object(ab.aobs, "observe_frame", side_effect=_obs):
            result = self.bridge.cast(control)
        self.assertIsNone(result)
        self.assertEqual(
            [call for call in self.pdi.calls if call[0] == F5],
            [(F5, True), (F5, False), (F5, True), (F5, False)],
        )

    def test_cast_authorized_ready_false_is_not_double_blocked(self) -> None:
        """校准面板遮住中文 ready 提示时，CASTING 授权仍可继续。"""
        self.clock.step = 0.25
        self.key_down = True
        control = StateControl("CASTING")
        self._attach_auto_coordinator(control)
        self.adapter.images = [plain_frame(), plain_frame()]
        calls = {"n": 0}

        def _obs(*_args, **kwargs):
            calls["n"] += 1
            ts = kwargs.get("monotonic_ts", 1.0)
            ring = 1.0 if calls["n"] == 1 else 0.4
            return make_obs(ts=ts, ring=ring)

        with mock.patch.object(ab.aobs, "observe_frame", side_effect=_obs):
            result = self.bridge.cast(control)
        self.assertIsNone(result)
        self.assertTrue(
            any("authorized=True" in line for line in self.logs)
        )
        self.assertFalse(
            any("original_cause=not_ready" in line for line in self.logs)
        )

    def test_cast_authorization_requires_auto_and_casting_state(self) -> None:
        for state, auto in (("READY", True), ("CASTING", False)):
            with self.subTest(state=state, auto=auto):
                self.pdi.calls.clear()
                control = StateControl(state)
                self._attach_auto_coordinator(control, auto=auto)
                self.adapter.images = [plain_frame()]
                self.assertIs(self.bridge.cast(control), False)
                self.assertEqual(self.pdi.calls, [])

    def test_cast_authorized_unknown_pre_down_never_blind_jumps(self) -> None:
        for field in ("fish", "modal"):
            with self.subTest(field=field):
                self.pdi.calls.clear()
                control = StateControl("CASTING")
                self._attach_auto_coordinator(control)
                self.adapter.images = [plain_frame()]
                kwargs = {
                    "fish": T.UNKNOWN,
                    "ok_modal": T.FALSE,
                }
                if field == "modal":
                    kwargs["ok_modal"] = T.UNKNOWN
                with mock.patch.object(
                    ab.aobs,
                    "observe_frame",
                    side_effect=lambda *_a, **k: make_obs(
                        ts=k.get("monotonic_ts", 1.0), **kwargs
                    ),
                ):
                    self.assertIs(self.bridge.cast(control), False)
                self.assertEqual(self.pdi.calls, [])
                self.assertTrue(
                    any("original_cause=unsafe_pre_down" in line
                        for line in self.logs)
                )

    def test_cast_short_ready_animation_after_release_is_not_failure(self) -> None:
        """刚释放 0.25s 内的 ready 命中只是动画帧，不能判失败。"""
        self.key_down = True
        clock = ManualClock()
        self.bridge._clock = clock
        control = TickingControl(clock, step=0.05)
        self._attach_auto_coordinator(control)
        # 抛前 ready；释放后两帧仍 ready（<0.25s），随后落水消失。
        self.adapter.images = [
            ready_frame(),
            ready_frame(),
            ready_frame(),
            plain_frame(),
            plain_frame(),
        ]
        calls = {"n": 0}

        def _obs(*_args, **kwargs):
            calls["n"] += 1
            ts = kwargs.get("monotonic_ts", 1.0)
            ring = 1.0 if calls["n"] == 1 else 0.42
            return make_obs(ts=ts, ring=ring)

        with mock.patch.object(ab.aobs, "observe_frame", side_effect=_obs):
            result = self.bridge.cast(control)
        self.assertIsNone(result)
        self.assertFalse(
            any("original_cause=ready_persist" in line for line in self.logs)
        )

    def test_cast_stop_before_start_sends_no_down(self) -> None:
        self.control.request_stop("stop")
        with self.assertRaises(AutoStop):
            self.bridge.cast(self.control)
        self.assertEqual(self.pdi.calls, [])
        self.assertEqual(self.arbiter.emitted, frozenset())

    def test_net_action_refused_by_default_without_f7(self) -> None:
        """Bridge 默认 auto_net_enabled=False：第二道 gate，不发 F7。"""
        self.arbiter.set_desired({F6}, "init")
        self.arbiter.reconcile()
        self.pdi.calls.clear()
        with self.assertRaises(ab.BridgeFault):
            self.bridge.net_action()
        self.assertEqual(self.pdi.calls, [])
        self.assertEqual(self.arbiter.desired, frozenset({F6}))
        self.assertEqual(self.arbiter.emitted, frozenset({F6}))

    def test_net_action_taps_f7_keeps_f6_no_f5(self) -> None:
        # 显式打开才保留 NET 行为；生产 main() 不会打开。
        self.bridge.auto_net_enabled = True
        self.arbiter.set_desired({F6}, "init")
        self.arbiter.reconcile()
        self.pdi.calls.clear()
        self.bridge.net_action()
        self.assertEqual(
            self.pdi.calls,
            [(ab.F7, True), (ab.F7, False)],
        )
        self.assertFalse(any(key == F5 for key, _down in self.pdi.calls))
        self.assertFalse(any(key == ab.PAGEDOWN for key, _down in self.pdi.calls))
        self.assertEqual(self.arbiter.desired, frozenset({F6}))
        self.assertEqual(self.arbiter.emitted, frozenset({F6}))


class FrictionTests(BridgeBase):
    def test_calibration_stop_before_any_wheel(self) -> None:
        control = FakeControl()
        control.request_stop("stop")
        with self.assertRaises(AutoStop):
            self.bridge.calibrate(control)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.adapter._fric["calibrated"])

    def test_calibration_requires_foreground(self) -> None:
        self.foreground = False
        self.assertFalse(self.bridge.calibrate(FakeControl()))
        self.assertEqual(self.sent, [])
        self.assertFalse(self.adapter._fric["calibrated"])

    def test_calibration_down_then_up_to_initial(self) -> None:
        self.assertIsNone(self.bridge.calibrate(FakeControl()))
        self.assertEqual(self.sent[:30], [-ab.WHEEL_DELTA] * 30)
        self.assertEqual(self.sent[30:], [ab.WHEEL_DELTA] * 16)
        self.assertEqual(len(self.sent), 46)
        self.assertEqual(self.adapter._fric["cur"], 17)
        self.assertTrue(self.adapter._fric["calibrated"])
        self.assertTrue(self.bridge._friction_calibrated)

    def test_raw_wheel_denies_increase_when_uncalibrated(self) -> None:
        self.adapter._fric["calibrated"] = False
        self.bridge._friction_calibrated = False
        self.assertFalse(self.bridge._bridge_wheel_raw(1))
        self.assertEqual(self.sent, [])
        self.assertGreaterEqual(self.adapter.friction_state.invalidated, 1)

    def test_raw_wheel_allows_decrease_when_uncalibrated(self) -> None:
        self.adapter._fric["calibrated"] = False
        self.bridge._friction_calibrated = False
        self.assertTrue(self.bridge._bridge_wheel_raw(-2))
        self.assertEqual(self.sent, [-ab.WHEEL_DELTA, -ab.WHEEL_DELTA])

    def test_raw_wheel_clamps_at_upper_bound(self) -> None:
        self.adapter._fric.update({"cur": 29, "calibrated": True})
        self.bridge._friction_calibrated = True
        self.assertTrue(self.bridge._bridge_wheel_raw(5))
        self.assertEqual(self.sent, [])

    def test_raw_wheel_accounts_only_successful_prefix_on_stop(self) -> None:
        self.adapter._fric.update({"cur": 17, "calibrated": True})
        self.bridge._friction_calibrated = True
        self.bridge.control = OneShotStopControl()
        self.assertFalse(self.bridge._bridge_wheel_raw(-3))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.adapter._fric["cur"], 16)
        self.assertFalse(self.adapter._fric["calibrated"])

    def test_install_discards_restored_state_and_replaces_raw(self) -> None:
        original = self.adapter._wheel_raw
        self.adapter._fric.update({"cur": 17, "calibrated": True, "_restored": True})
        self.bridge.install()
        self.assertFalse(self.adapter._fric["calibrated"])
        self.assertNotIn("_restored", self.adapter._fric)
        self.assertGreaterEqual(self.adapter.friction_state.invalidated, 1)
        self.assertIs(self.adapter._wheel_raw.__self__, self.bridge)
        self.bridge.restore()
        self.assertEqual(self.adapter._wheel_raw, original)

    def test_calibrate_success_clears_observation_window(self) -> None:
        self.bridge._fric_win.append((0.0, 2))
        self.assertIsNone(self.bridge.calibrate(FakeControl()))
        self.assertEqual(self.bridge._fric_win, [])
        # 校准前 invalidate + 成功后各一次；都要求连 last_action 一起清。
        self.assertGreaterEqual(self.adapter.friction_reset_calls.count(False), 2)

    def test_new_fish_resets_observation_without_touching_cur_or_wheel(self) -> None:
        coord = FakeFishCoordinator("e-f1")
        self.bridge.attach(coord)
        self.bridge.friction_step(2)
        self.assertEqual(self.adapter.friction_reset_calls.count(False), 1)
        coord.fish_id = "e-f2"
        self.bridge.friction_step(2)
        self.assertEqual(self.adapter.friction_reset_calls.count(False), 2)
        self.assertEqual(len(self.bridge._fric_win), 1)  # 旧样本已清掉
        self.assertEqual(self.adapter._fric["cur"], 17)  # 不动 cur
        self.assertEqual(self.sent, [])                  # 不发滚轮

    def test_friction_step_pause_keeps_last_action(self) -> None:
        self.bridge.attach(FakeFishCoordinator("e-f1"))
        self.bridge.friction_step(2)
        self.bridge.friction_step(None)
        self.assertEqual(self.bridge._fric_win, [])
        self.assertTrue(self.adapter.friction_reset_calls[-1])


class RetrieveTests(BridgeBase):
    def _bridge(self, frames, control=None, clock=None, logs=None):
        self.adapter.images = list(frames)
        bridge = ab.Bridge(
            self.adapter,
            control=control or self.control,
            pdi=self.pdi,
            keep_path=BOT / "keep.png",
            ready_cn_path=READY_TEMPLATE,
            foreground=lambda: True,
            clock=clock or FakeClock(),
            log=(logs.append if logs is not None else (lambda _t: None)),
        )
        arbiter = InputArbiter(bridge.sink)
        bridge.set_arbiter(arbiter)
        return bridge, arbiter

    def _run(
        self,
        frames,
        ready,
        observations,
        *,
        control=None,
        clock=None,
        logs=None,
    ):
        bridge, arbiter = self._bridge(frames, control, clock, logs)
        ready_iter = iter(ready)
        bridge._ready_verdict = lambda _image, _obs: next(ready_iter, False)
        obs_iter = iter(observations)
        last_obs = observations[-1]

        def _obs(*_args, **_kwargs):
            try:
                return next(obs_iter)
            except StopIteration:
                return last_obs

        with mock.patch.object(ab.aobs, "observe_frame", side_effect=_obs):
            result = bridge.retrieve(control or self.control)
        return bridge, arbiter, result

    def test_retrieve_ready_frame_returns_true_without_f5(self) -> None:
        logs: list[str] = []
        bridge, arbiter = self._bridge([ready_frame()], logs=logs)
        with mock.patch.object(
            ab.aobs, "observe_frame", return_value=make_obs(ring=1.0)
        ):
            result = bridge.retrieve(self.control)
        self.assertTrue(result)
        self.assertEqual(self.pdi.calls, [])
        self.assertEqual(arbiter.emitted, frozenset())
        self.assertTrue(
            any("ready template hit before F5" in line for line in logs)
        )

    def test_retrieve_ready_two_frames_finishes_even_ring_967(self) -> None:
        _bridge, arbiter, result = self._run(
            [plain_frame()],
            [False, True, True],
            [make_obs(ring=0.967) for _ in range(3)],
        )
        self.assertTrue(result)
        self.assertEqual(self.pdi.calls, [(F5, True), (F5, False)])
        self.assertEqual(arbiter.desired, frozenset())

    def test_retrieve_near_full_ring_967_alone_never_completes(self) -> None:
        with self.assertRaises(ab.RetrieveBlocked):
            self._run(
                [plain_frame()],
                [False],
                [make_obs(fish=T.FALSE, ring=0.967, ok_modal=T.FALSE)],
                clock=FakeClock(step=100.0),
            )
        self.assertEqual(self.pdi.calls, [(F5, True), (F5, False)])

    def test_retrieve_near_full_ring_does_not_press_f5(self) -> None:
        """ring 接近满环 = 线还在杯上，一次 F5 都不能按。

        线未抛出去时 F5 是**抛竿蓄力**键，盲按会把力度攒到 130% 卡死
        （2026-11-09 实机事故）。旧行为是"ring 0.98 fallback 仍按 F5，
        再靠 HUD+无鱼判完成"，现在改为提前返回。
        """
        _bridge, _arbiter, result = self._run(
            [plain_frame()],
            [False],
            [make_obs(fish=T.FALSE, ring=0.99) for _ in range(4)],
        )
        self.assertTrue(result)
        self.assertEqual(self.pdi.calls, [])

    def test_retrieve_with_fish_still_presses_f5(self) -> None:
        # 有鱼在线上就不能判"在杯上"，该收还得收
        _bridge, _arbiter, result = self._run(
            [plain_frame()],
            [False],
            [make_obs(fish=T.TRUE, ring=0.99) for _ in range(2)]
            + [make_obs(fish=T.FALSE, ring=0.2) for _ in range(2)],
        )
        self.assertTrue(result)
        self.assertIn((F5, True), self.pdi.calls)

    def test_retrieve_stop_before_start_no_down(self) -> None:
        control = FakeControl()
        control.request_stop("stop")
        bridge, _arbiter = self._bridge([plain_frame()], control=control)
        with self.assertRaises(AutoStop):
            bridge.retrieve(control)
        self.assertEqual(self.pdi.calls, [])

    def test_retrieve_stop_during_wait_releases(self) -> None:
        control = OneShotStopControl()
        with self.assertRaises(AutoStop):
            self._run(
                [plain_frame()],
                [False],
                [make_obs(fish=T.FALSE, ring=0.3)],
                control=control,
            )
        self.assertEqual(self.pdi.calls, [(F5, True), (F5, False)])

    def test_retrieve_logs_status_each_second(self) -> None:
        logs: list[str] = []
        _bridge, _arbiter, result = self._run(
            [plain_frame()],
            [False, False, False, True, True],
            [
                make_obs(ring=0.3),
                make_obs(ring=0.4),
                make_obs(ring=0.5),
                make_obs(ring=0.6),
                make_obs(ring=0.6),
            ],
            clock=FakeClock(step=0.6),
            logs=logs,
        )
        self.assertTrue(result)
        loop_logs = [line for line in logs if "[RETRIEVE] loop" in line]
        self.assertGreaterEqual(len(loop_logs), 2)

    def test_retrieve_last_frame_evidence_wins_over_deadline(self) -> None:
        # 第 1 帧后 clock 仍在 deadline 内；第 3 帧的取帧/日志时间已经越界，
        # 但该帧的 .98 后备证据必须先被消费，不能先抛 deadline。
        clock = ScriptClock(
            [
                1000.0,  # deadline = 1045
                1000.1,
                1000.2,
                1000.3,
                1000.4,
                1000.5,
                1000.6,
                1000.7,
                1000.8,  # 第 2 帧后 deadline 检查仍通过
                1046.0,  # 第 3 帧 probe 时间已越界
                1046.1,
                1046.2,
            ]
        )
        _bridge, _arbiter, result = self._run(
            [plain_frame()],
            [False],
            [make_obs(ring=0.99) for _ in range(4)],
            clock=clock,
        )
        self.assertTrue(result)


class KeepTests(BridgeBase):
    def test_keep_stop_before_start_sends_no_click(self) -> None:
        control = FakeControl()
        control.request_stop("stop")
        with self.assertRaises(AutoStop):
            self.bridge.keep(control)
        self.assertIsNone(self.adapter.keep_args)
        self.assertEqual(self.adapter.click_calls, [])

    def test_keep_restores_autostop_swallowed_by_keep_helper(self) -> None:
        control = FakeControl()
        self.bridge.control = control

        def fake_keep(detect, locate, click, wait, dismiss=None):
            control.request_stop("stop-in-keep")
            raise RuntimeError("swallowed by click_keep_until_settled")

        self.adapter.click_keep_until_settled = fake_keep
        with self.assertRaises(AutoStop):
            self.bridge.keep(control)
        self.assertEqual(self.adapter.click_calls, [])

    def test_keep_rejects_foreground_loss_without_clicking(self) -> None:
        self.foreground = False
        self.assertFalse(self.bridge.keep(self.control))
        self.assertIsNone(self.adapter.keep_args)
        self.assertEqual(self.adapter.click_calls, [])

    def test_keep_rejects_invalid_final_frame(self) -> None:
        self.adapter.frame_valid = False
        self.assertFalse(self.bridge.keep(self.control))
        self.assertIsNotNone(self.adapter.keep_args)
        self.assertEqual(self.adapter.click_calls, [])

    def test_keep_rejects_modal_after_click(self) -> None:
        self.adapter.strong_score = 0.0
        with mock.patch.object(
            ab.aobs,
            "observe_frame",
            return_value=make_obs(ok_modal=T.TRUE),
        ):
            self.assertFalse(self.bridge.keep(self.control))

    def test_keep_success_requires_strong_capture_false(self) -> None:
        self.adapter.strong_score = 0.0
        with mock.patch.object(
            ab.aobs,
            "observe_frame",
            return_value=make_obs(ok_modal=T.FALSE),
        ):
            self.assertTrue(self.bridge.keep(self.control))
        self.assertIsNotNone(self.adapter.keep_args)

    def test_keepnet_full_uses_expected_template_and_does_not_click(self) -> None:
        paths = []

        def score(image, path):
            paths.append(path)
            return 0.95

        self.bridge._match_template_score = score
        self.assertTrue(self.bridge.keepnet_full())
        self.assertEqual(paths, [BOT / "keepnet_is_full.png"])
        self.assertEqual(self.adapter.click_calls, [])


class DismissTests(BridgeBase):
    def test_click_stop_before_start_does_nothing(self) -> None:
        control = FakeControl()
        control.request_stop("stop")
        with self.assertRaises(AutoStop):
            self.bridge._click(control, 10, 20)
        self.assertEqual(self.adapter.click_calls, [])

    def test_dismiss_stop_before_start_no_click(self) -> None:
        control = FakeControl()
        control.request_stop("stop")
        with self.assertRaises(AutoStop):
            self.bridge.dismiss(control)
        self.assertEqual(self.adapter.click_calls, [])

    def test_dismiss_caps_at_twelve_proven_clicks(self) -> None:
        with mock.patch.object(
            ab.aobs, "detect_ok_modal", return_value=(T.TRUE, {})
        ):
            self.assertFalse(self.bridge.dismiss(self.control))
        self.assertEqual(len(self.adapter.click_calls), 12)
        self.assertEqual(self.adapter.click_calls[0], (960, 810))

    def test_dismiss_without_modal_is_false_and_no_click(self) -> None:
        with mock.patch.object(
            ab.aobs, "detect_ok_modal", return_value=(T.FALSE, {})
        ):
            self.assertFalse(self.bridge.dismiss(self.control))
        self.assertEqual(self.adapter.click_calls, [])



class ProtocolVetoRetrieveTests(unittest.TestCase):
    """retrieve 的 .98 环兜底必须被协议否决（短抛时线还在水里）。"""

    def test_line_in_water_keeps_reeling(self):
        import autofish_bridge as ab
        b = object.__new__(ab.Bridge)
        b._proto_snap = {"phase": "line_in_water",
                         "beat": {"step_age": 0.4, "load_age": None}}
        b._proto_snap_at = __import__("time").time()
        self.assertTrue(b._protocol_line_in_water())

    def test_line_home_allows_complete(self):
        import autofish_bridge as ab
        b = object.__new__(ab.Bridge)
        b._proto_snap = {"phase": "kept",
                         "beat": {"step_age": 12.0, "load_age": None}}
        b._proto_snap_at = __import__("time").time()
        self.assertFalse(b._protocol_line_in_water())

    def test_no_snapshot_does_not_veto(self):
        import autofish_bridge as ab
        b = object.__new__(ab.Bridge)
        b._proto_snap = None
        b._proto_snap_at = __import__("time").time()
        self.assertFalse(b._protocol_line_in_water())


if __name__ == "__main__":
    unittest.main()


class NetToggleTests(BridgeBase):
    def test_env_is_single_source_and_defaults_off(self) -> None:
        self.assertFalse(ab.resolve_auto_net_enabled({}))
        self.assertFalse(ab.resolve_auto_net_enabled({"RF4_AUTO_NET": "0"}))
        self.assertTrue(ab.resolve_auto_net_enabled({"RF4_AUTO_NET": "1"}))

    def test_config_mismatch_fails_preflight_before_any_net_key(self) -> None:
        bridge = ab.Bridge(
            self.adapter,
            control=self.control,
            pdi=self.pdi,
            auto_net_enabled=True,
            fight_auto_net_enabled=False,
            keep_path=BOT / "keep.png",
            log=self.logs.append,
        )
        self.assertIsNotNone(bridge.preflight_error)
        self.assertIn("net_config_mismatch", bridge.preflight_error)
        with self.assertRaises(ab.BridgeFault):
            bridge.capture_observation()
        with self.assertRaises(ab.BridgeFault):
            bridge.net_action()
        self.assertEqual(self.pdi.calls, [])

    def test_deploy_and_retract_are_distinct_logs_one_f7_each(self) -> None:
        self.bridge.auto_net_enabled = True
        self.arbiter.set_desired({F6}, "init")
        self.arbiter.reconcile()
        self.pdi.calls.clear()

        self.bridge.net_action()
        self.assertIn("deploy F7 tap", "\n".join(self.logs))
        self.assertEqual(
            self.pdi.calls, [(ab.F7, True), (ab.F7, False)]
        )
        self.assertFalse(any(k == F5 for k, _d in self.pdi.calls))
        self.assertFalse(any(k == ab.PAGEDOWN for k, _d in self.pdi.calls))
        self.assertEqual(self.arbiter.desired, frozenset({F6}))

        self.pdi.calls.clear()
        self.bridge.net_retract_action()
        self.assertIn("retract F7 tap", "\n".join(self.logs))
        self.assertEqual(
            self.pdi.calls, [(ab.F7, True), (ab.F7, False)]
        )
        self.assertFalse(any(k == F5 for k, _d in self.pdi.calls))
        self.assertFalse(any(k == ab.PAGEDOWN for k, _d in self.pdi.calls))
        self.assertEqual(self.arbiter.desired, frozenset({F6}))

    def test_stop_cancels_toggle_before_any_key(self) -> None:
        self.bridge.auto_net_enabled = True
        self.control.request_stop("operator")
        with self.assertRaises(AutoStop):
            self.bridge.net_action()
        self.assertEqual(self.pdi.calls, [])

    def test_foreground_required_before_f7(self) -> None:
        self.bridge.auto_net_enabled = True
        self.foreground = False
        with mock.patch.object(self.bridge, "_focus_game", return_value=False):
            with self.assertRaises(ab.BridgeFault):
                self.bridge.net_retract_action()
        self.assertEqual(self.pdi.calls, [])

    def test_net_modifier_held_blocks_bare_f7_without_releasing_user_keys(self) -> None:
        self.bridge.auto_net_enabled = True
        self.arbiter.set_desired({F6}, "init")
        self.arbiter.reconcile()
        self.pdi.calls.clear()
        for held in ("ctrl", "alt", "shift"):
            with self.subTest(held=held):
                self.bridge._key_state = lambda key, held=held: key == held
                with self.assertRaises(ab.BridgeFault) as ctx:
                    self.bridge.net_action()
                self.assertIn("net_modifier_held:" + held, str(ctx.exception))
                # 只读阻塞：不发 F7，也不替用户松开任何键。
                self.assertEqual(self.pdi.calls, [])
                self.assertEqual(self.arbiter.desired, frozenset({F6}))
                self.assertEqual(self.arbiter.emitted, frozenset({F6}))

    def test_allowed_keys_include_f7_but_net_never_sends_pagedown(self) -> None:
        self.assertIn(ab.F7, ab.ALLOWED_KEYS)
        self.assertIn(ab.NET_KEY, ab.ALLOWED_KEYS)
        self.assertNotIn(ab.PAGEDOWN, ab.ALLOWED_KEYS)
        self.assertEqual(ab.NET_KEY, "f7")

    def test_stop_during_hold_still_releases_f7(self) -> None:
        self.bridge.auto_net_enabled = True
        self.bridge.control = OneShotStopControl()
        self.arbiter.set_desired({F6}, "init")
        self.arbiter.reconcile()
        self.pdi.calls.clear()
        with self.assertRaises(AutoStop):
            self.bridge.net_action()
        self.assertEqual(
            self.pdi.calls, [(ab.F7, True), (ab.F7, False)]
        )
        self.assertEqual(self.arbiter.desired, frozenset({F6}))

    def test_callbacks_expose_both_net_actions(self) -> None:
        cbs = self.bridge.callbacks()
        self.assertTrue(callable(cbs.net_action))
        self.assertTrue(callable(cbs.net_retract_action))
