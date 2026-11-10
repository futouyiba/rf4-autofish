"""启动阶段回归：纯 AST 提取 + 假对象注入，不碰实机。

覆盖四条已经踩过的坑：

1. `prepare_window`：客户区已经是 1920x1080 时不能再 `SetWindowPos`，
   也不能再改窗口样式（改样式会让 DWM 进 direct-flip）；
2. `_hits`：模板置信度必须先 `float()` 再校验，NaN / 越界 / 非数字一律拒绝；
3. `patch_fish_captured`：命中坐标要减 `ClientToScreen` 的**游戏客户区原点**，
   不能减虚拟桌面原点 `_vorigin`；
4. `friction_absolute`：真正校准成功才清 `_restored`，失败必须保留。

本文件全程只读 `rf4s_adapter.py` 源码、`exec` 抽出来的函数，不 import 适配器，
因此不会触发 `SetProcessDpiAwareness` / `chdir` / 截图 / 聚焦 / 输入。

运行：
    python -m unittest discover -s tests -p "test_live_startup_regressions.py" -v
"""

from __future__ import annotations

import ast
import builtins
import pathlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

BOT = pathlib.Path(__file__).resolve().parents[1]
ADAPTER = BOT / "rf4s_adapter.py"


# ============================== AST 工具 ==============================

def _tree() -> ast.Module:
    return ast.parse(ADAPTER.read_text(encoding="utf-8"))


def _find_func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"adapter 里找不到函数 {name}")


def _exec_funcs(nodes, extra=None) -> dict:
    """把抽出来的函数/常量编译到独立命名空间，不执行其所在模块。"""
    module = ast.Module(body=list(nodes), type_ignores=[])
    ast.fix_missing_locations(module)
    code = compile(module, str(ADAPTER), "exec")
    ns = {"__name__": "rf4s_adapter_fake", "__builtins__": builtins}
    ns.update(extra or {})      # 默认参数在 def 时求值，必须提前注入常量
    exec(code, ns)
    return ns


def _name_calls(fn, name):
    return [n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == name]


def _attr_calls(fn, attr):
    return [n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == attr]


# ============================== 假 Win32 对象 ==============================

class _Rect:
    def __init__(self, right=0, bottom=0):
        self.right = right
        self.bottom = bottom


class _Point:
    def __init__(self, x=0, y=0):
        self.x = x
        self.y = y


class _ByRef:
    def __init__(self, obj):
        self._obj = obj


class _Win32Gui:
    def __init__(self, window=(0, 0, 1938, 1127), style=0):
        self.window = window
        self.style = style
        self.calls = []

    def GetWindowRect(self, h):
        self.calls.append("GetWindowRect")
        return self.window

    def GetWindowLong(self, h, index):
        self.calls.append("GetWindowLong")
        return self.style


class _User32:
    """按 `sizes` 依次返回客户区尺寸；SetWindowPos 后前进一格。"""

    def __init__(self, win: _Win32Gui, sizes):
        self.win = win
        self.sizes = list(sizes)
        self.index = 0
        self.calls = []
        self.window_pos = []

    def GetClientRect(self, h, ref):
        self.calls.append("GetClientRect")
        w, hh = self.sizes[min(self.index, len(self.sizes) - 1)]
        ref._obj.right, ref._obj.bottom = w, hh
        return 1

    def GetWindowLongW(self, h, index):
        self.calls.append("GetWindowLongW")
        return self.win.style

    def SetWindowLongW(self, h, index, value):
        self.calls.append("SetWindowLongW")
        self.win.style = value
        return value

    def SetWindowPos(self, h, after, x, y, w, hh, flags):
        self.calls.append("SetWindowPos")
        self.window_pos.append((x, y, w, hh))
        self.index += 1
        self.win.window = (x, y, x + w, y + hh)
        return 1


_WIN32CON = SimpleNamespace(
    GWL_STYLE=-16,
    WS_CAPTION=0x00C00000,
    SWP_NOZORDER=0x0004,
    SWP_NOACTIVATE=0x0010,
    SWP_SHOWWINDOW=0x0040,
)


def _prepare_window_ns(sizes, style=0):
    win = _Win32Gui(style=style)
    user32 = _User32(win, sizes)
    fake_ctypes = SimpleNamespace(byref=_ByRef,
                                  windll=SimpleNamespace(user32=user32))
    ns = _exec_funcs([_find_func(_tree(), "prepare_window")])
    ns.update({
        "TARGET_W": 1920,
        "TARGET_H": 1080,
        "win32gui": win,
        "win32con": _WIN32CON,
        "ctypes": fake_ctypes,
        "wt": SimpleNamespace(RECT=_Rect),
        "_orig_state": {},
        "pick_monitor": lambda: (0, 0, 1920, 1080),
        "time": SimpleNamespace(sleep=lambda s: None),
        "print": lambda *a, **k: None,
    })
    return ns, win, user32


# ============================== prepare_window ==============================

class TestPrepareWindowIdempotent(unittest.TestCase):
    def test_already_1920x1080_does_not_touch_window(self):
        ns, win, user32 = _prepare_window_ns([(1920, 1080)])
        result = ns["prepare_window"](123)
        self.assertEqual(result, (1920, 1080))
        self.assertIn("GetClientRect", user32.calls)
        self.assertNotIn("SetWindowPos", user32.calls)
        self.assertNotIn("SetWindowPos", win.calls)
        # 连“读样式”都不该走到：早退必须在改样式之前
        self.assertNotIn("GetWindowLongW", user32.calls)
        self.assertNotIn("SetWindowLongW", user32.calls)
        self.assertEqual(user32.window_pos, [])

    def test_non_target_client_still_recompensates(self):
        ns, win, user32 = _prepare_window_ns(
            [(1900, 1000), (1920, 1080)], style=0)
        result = ns["prepare_window"](123)
        self.assertEqual(result, (1920, 1080))
        self.assertIn("SetWindowLongW", user32.calls)   # 无标题栏 -> 补回标题栏
        self.assertTrue(win.style & _WIN32CON.WS_CAPTION)
        self.assertGreaterEqual(user32.calls.count("SetWindowPos"), 1)

    def test_early_return_precedes_any_style_or_position_write(self):
        fn = _find_func(_tree(), "prepare_window")
        writes = [n.lineno for n in _attr_calls(fn, "SetWindowPos")]
        writes += [n.lineno for n in _attr_calls(fn, "SetWindowLongW")]
        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        self.assertTrue(returns)
        first_return = min(returns, key=lambda n: n.lineno)
        self.assertTrue(writes)
        self.assertLess(first_return.lineno, min(writes))

        guarded = False
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            names = {n.id for n in ast.walk(node.test)
                     if isinstance(n, ast.Name)}
            body_returns = [n for n in ast.walk(node)
                            if isinstance(n, ast.Return)
                            and n.lineno == first_return.lineno]
            if body_returns and {"TARGET_W", "TARGET_H"} & names:
                guarded = True
        self.assertTrue(guarded, "早退必须由 TARGET_W/TARGET_H 判断保护")


# ============================== _hits 置信度 ==============================

class _FakeCv2:
    TM_CCOEFF_NORMED = 5

    def __init__(self):
        self.imread_calls = []

    def imread(self, path):
        self.imread_calls.append(str(path))
        return None          # 模板读不到 -> 直接返回 []，不进入匹配

    def cvtColor(self, img, code):
        raise AssertionError("置信度通过前不应进入图像处理")

    def matchTemplate(self, *a, **k):
        raise AssertionError("置信度通过前不应进入图像处理")


def _hits_ns():
    fake_cv2 = _FakeCv2()
    ns = _exec_funcs([_find_func(_tree(), "_hits")])
    ns.update({
        "cv2": fake_cv2,
        "np": SimpleNamespace(asarray=lambda x: x, where=lambda *a, **k: ([], [])),
        "screen": lambda: None,
    })
    return ns, fake_cv2


class TestHitsConfidence(unittest.TestCase):
    def test_string_confidence_is_coerced_to_float(self):
        ns, fake_cv2 = _hits_ns()
        self.assertEqual(ns["_hits"]("keep.png", "0.95"), [])
        # 走到 imread 说明字符串已经被 float() 过（否则比较时会 TypeError）
        self.assertEqual(fake_cv2.imread_calls, ["keep.png"])

    def test_boundary_values_accepted(self):
        ns, _ = _hits_ns()
        self.assertEqual(ns["_hits"]("t.png", 0.0), [])
        self.assertEqual(ns["_hits"]("t.png", 1.0), [])
        self.assertEqual(ns["_hits"]("t.png", "1"), [])

    def test_nan_and_invalid_rejected(self):
        bad = [float("nan"), "nan", float("inf"), -0.01, 1.01, "abc", ""]
        for value in bad:
            ns, fake_cv2 = _hits_ns()
            with self.subTest(confidence=value):
                with self.assertRaises(ValueError):
                    ns["_hits"]("t.png", value)
                self.assertEqual(fake_cv2.imread_calls, [])

    def test_confidence_float_then_range_check_in_source(self):
        fn = _find_func(_tree(), "_hits")
        body = [n for n in fn.body
                if not (isinstance(n, ast.Expr)
                        and isinstance(n.value, ast.Constant)
                        and isinstance(n.value.value, str))]
        first = body[0]
        self.assertIsInstance(first, ast.Assign)
        self.assertIsInstance(first.value, ast.Call)
        self.assertEqual(getattr(first.value.func, "id", None), "float")
        self.assertEqual(getattr(first.value.args[0], "id", None), "confidence")

        range_checks = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Compare)
            and any(isinstance(c, ast.Constant)
                    and c.value in (0.0, 1.0) for c in ast.walk(n))
        ]
        self.assertTrue(range_checks)
        raises = [n for n in ast.walk(fn)
                  if isinstance(n, ast.Raise)
                  and isinstance(n.exc, ast.Call)
                  and getattr(n.exc.func, "id", "") == "ValueError"]
        self.assertTrue(raises)


# ============================== patch_fish_captured ==============================

class _FakeDetection:
    """替换 `rf4s.controller.detection.Detection`。"""


class _User32ClientToScreen:
    def __init__(self, origin=(0, 0), ok=1):
        self.origin = origin
        self.ok = ok
        self.calls = []

    def ClientToScreen(self, hwnd, ref):
        self.calls.append(hwnd)
        if not self.ok:
            return 0
        ref._obj.x, ref._obj.y = self.origin
        return 1


def _captured_ns(origin=(0, 0), screen_ok=1):
    user32 = _User32ClientToScreen(origin, screen_ok)
    fake_ctypes = SimpleNamespace(byref=_ByRef,
                                  windll=SimpleNamespace(user32=user32))
    ns = _exec_funcs([_find_func(_tree(), "patch_fish_captured")])
    ns.update({
        "game_hwnd": lambda: 4242,
        "wt": SimpleNamespace(POINT=_Point),
        "ctypes": fake_ctypes,
        "CAPTURE_CONFIDENCE": 0.95,
        "CAPTURE_POS_BOX": (300, 40, 1500, 620),
        "print": lambda *a, **k: None,
    })

    det_mod = types.ModuleType("rf4s.controller.detection")
    det_mod.Detection = _FakeDetection
    ctrl_mod = types.ModuleType("rf4s.controller")
    ctrl_mod.detection = det_mod
    pkg_mod = types.ModuleType("rf4s")
    pkg_mod.controller = ctrl_mod
    modules = {
        "rf4s": pkg_mod,
        "rf4s.controller": ctrl_mod,
        "rf4s.controller.detection": det_mod,
    }
    return ns, user32, modules


def _make_instance(box, recorded):
    inst = _FakeDetection()
    inst.box = box

    def _get_image_box(name, confidence):
        recorded.append((name, confidence))
        if isinstance(box, Exception):
            raise box
        return box

    inst._get_image_box = _get_image_box
    return inst


class TestPatchFishCapturedOrigin(unittest.TestCase):
    def _patch_and_call(self, origin, screen_ok, box):
        ns, user32, modules = _captured_ns(origin, screen_ok)
        recorded = []
        try:
            with mock.patch.dict(sys.modules, modules):
                ns["patch_fish_captured"]()
                inst = _make_instance(box, recorded)
                verdict = _FakeDetection.is_fish_captured(inst)
        finally:
            if "is_fish_captured" in _FakeDetection.__dict__:
                del _FakeDetection.is_fish_captured
        return verdict, recorded, user32

    def test_patch_installs_callable_on_detection(self):
        ns, _user32, modules = _captured_ns()
        try:
            with mock.patch.dict(sys.modules, modules):
                ns["patch_fish_captured"]()
                self.assertTrue(callable(_FakeDetection.is_fish_captured))
                self.assertEqual(_FakeDetection.is_fish_captured.__name__,
                                 "is_fish_captured")
        finally:
            if "is_fish_captured" in _FakeDetection.__dict__:
                del _FakeDetection.is_fish_captured

    def test_box_is_converted_with_client_origin(self):
        box = SimpleNamespace(left=500, top=300)
        verdict, recorded, user32 = self._patch_and_call((100, 200), 1, box)
        self.assertTrue(verdict)
        self.assertEqual(user32.calls, [4242])
        self.assertEqual(recorded, [("keep", 0.95)])

    def test_virtual_desktop_origin_regression_rejected(self):
        # 客户区原点 (120,60)，命中点 (400,100) -> 客户区坐标 (280,40)，
        # x 小于 CAPTURE_POS_BOX 的左边界 300。若误用虚拟桌面原点（0,0）
        # 就会算成 (400,100) 而被放行 —— 这条专门锁住该回归。
        box = SimpleNamespace(left=400, top=100)
        verdict, _recorded, _user32 = self._patch_and_call(
            (120, 60), 1, box)
        self.assertFalse(verdict)

    def test_out_of_region_hit_rejected(self):
        box = SimpleNamespace(left=1776, top=938)   # 鱼护 HUD 图标
        verdict, _recorded, _user32 = self._patch_and_call((0, 0), 1, box)
        self.assertFalse(verdict)

    def test_client_to_screen_failure_is_false(self):
        box = SimpleNamespace(left=500, top=300)
        verdict, _recorded, user32 = self._patch_and_call((0, 0), 0, box)
        self.assertFalse(verdict)
        self.assertEqual(user32.calls, [4242])

    def test_lookup_failure_is_false(self):
        verdict, recorded, _user32 = self._patch_and_call(
            (0, 0), 1, RuntimeError("boom"))
        self.assertFalse(verdict)
        self.assertEqual(recorded, [("keep", 0.95)])

    def test_source_uses_client_to_screen_not_vorigin(self):
        fn = _find_func(_tree(), "patch_fish_captured")
        self.assertTrue(_attr_calls(fn, "ClientToScreen"))
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        self.assertNotIn("_vorigin", names)
        self.assertIn("game_hwnd", names)


# ============================== friction_absolute ==============================

class _FricCapture:
    def __init__(self, wheel_ok=True, gate=(True, "no_fish"), focus=True):
        self.state = {"cur": 20, "t": 0.0, "calibrated": True,
                      "_restored": True}
        self.wheel_ok = wheel_ok
        self.gate = gate
        self.focus = focus
        self.wheel_calls = []
        self.saved = []
        self.invalidated = 0

    def wheel(self, n):
        self.wheel_calls.append(n)
        return self.wheel_ok

    def save(self, value, calibrated=False):
        self.saved.append((value, calibrated))

    def invalidate(self):
        self.invalidated += 1


def _friction_ns(cap: _FricCapture):
    constants = {"FRICTION_ABS_TOP": 30, "FRICTION_INITIAL": 17}
    ns = _exec_funcs([_find_func(_tree(), "friction_absolute")], constants)
    state_mod = SimpleNamespace(set=cap.save, invalidate=cap.invalidate)
    ns.update({
        "focus_game_quiet": lambda: cap.focus,
        "capture_game": lambda: (object(), (0, 0)),
        "_no_fish_gate": lambda img: cap.gate,
        "_wheel_raw": cap.wheel,
        "time": SimpleNamespace(sleep=lambda s: None, time=lambda: 1234.5),
        "_fric": cap.state,
        "friction_state": state_mod,
        "friction_reset_observation": lambda **kwargs: None,
        "print": lambda *a, **k: None,
    })
    return ns


class TestFrictionAbsoluteRestored(unittest.TestCase):
    def test_success_clears_restored(self):
        cap = _FricCapture()
        result = _friction_ns(cap)["friction_absolute"](17)
        self.assertTrue(result)
        self.assertEqual(cap.wheel_calls, [30, -13])
        self.assertEqual(cap.state["cur"], 17)
        self.assertTrue(cap.state["calibrated"])
        self.assertNotIn("_restored", cap.state)
        self.assertEqual(cap.saved, [(17, True)])

    def test_gate_failure_keeps_restored(self):
        cap = _FricCapture(gate=(False, "background"))
        result = _friction_ns(cap)["friction_absolute"](17)
        self.assertFalse(result)
        self.assertEqual(cap.wheel_calls, [])
        self.assertTrue(cap.state["_restored"])
        self.assertFalse(cap.state["calibrated"])
        self.assertEqual(cap.saved, [])

    def test_second_wheel_failure_keeps_restored(self):
        cap = _FricCapture()
        ns = _friction_ns(cap)
        first = [True]

        def wheel(n):
            if first:
                first.pop()
                return True
            return False

        ns["_wheel_raw"] = wheel
        result = ns["friction_absolute"](17)
        self.assertFalse(result)
        self.assertTrue(cap.state["_restored"])
        self.assertFalse(cap.state["calibrated"])
        self.assertEqual(cap.invalidated, 1)

    def test_source_pops_restored_only_after_success_marker(self):
        fn = _find_func(_tree(), "friction_absolute")
        pops = [n for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "pop"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "_fric"
                and n.args
                and isinstance(n.args[0], ast.Constant)
                and n.args[0].value == "_restored"]
        self.assertEqual(len(pops), 1)

        calibrated = [n for n in ast.walk(fn)
                      if isinstance(n, ast.Assign)
                      and isinstance(n.targets[0], ast.Subscript)
                      and isinstance(n.targets[0].value, ast.Name)
                      and n.targets[0].value.id == "_fric"
                      and isinstance(n.targets[0].slice, ast.Constant)
                      and n.targets[0].slice.value == "calibrated"
                      and isinstance(n.value, ast.Constant)
                      and n.value.value is True]
        self.assertTrue(calibrated)
        self.assertGreater(pops[0].lineno, max(n.lineno for n in calibrated))


# ============================== friction_controller_step ==============================

class _Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now


class _Nudge:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def __call__(self, delta):
        self.calls.append(int(delta))
        return self.ok


def _controller_ns(state, nudge, clock):
    import autofish_friction_window as fwin

    ns = _exec_funcs([_find_func(_tree(), "friction_controller_step")])
    ns.update({
        "fwin": fwin,
        "_fric": {"cur": 17, "t": -600.0},  # 旧校准时刻：不得参与首次升档计时
        "_fwin_state": lambda: state,
        "_drop_target": lambda cur: max(1, cur - max(1, cur // 3)),
        "_rise_target": lambda cur: min(29, cur + max(1, cur // 5)),
        "friction_nudge": nudge,
        "_FW_CONFIG": fwin.FrictionWindowConfig(),
        "time": clock,
        "print": lambda *a, **k: None,
    })
    return ns


class TestFrictionControllerStepObservation(unittest.TestCase):
    def test_three_frames_do_not_rise_but_ten_seconds_do(self):
        import autofish_friction_window as fwin

        state = fwin.FrictionObservationState()
        clock = _Clock()
        nudge = _Nudge(True)
        step = _controller_ns(state, nudge, clock)["friction_controller_step"]
        win = []
        for t in (0.0, 0.5, 1.0):
            win.append((t, 2))
            clock.now = t
            step(win)
        self.assertEqual(nudge.calls, [])
        self.assertEqual(state.observe_since, 0.0)

        t = 1.5
        while t <= 10.1:
            win.append((t, 2))
            clock.now = t
            step(win)
            t = round(t + 0.5, 6)
        self.assertEqual(nudge.calls, [3])
        self.assertEqual(state.last_action, fwin.ACTION_RISE)
        self.assertEqual(win, [])

    def test_failed_adjustment_does_not_commit_or_clear(self):
        import autofish_friction_window as fwin

        state = fwin.FrictionObservationState()
        clock = _Clock()
        nudge = _Nudge(False)
        step = _controller_ns(state, nudge, clock)["friction_controller_step"]
        win = []
        t = 0.0
        while t <= 10.1:
            win.append((t, 2))
            clock.now = t
            step(win)
            t = round(t + 0.5, 6)
        self.assertEqual(len(nudge.calls), 1)   # 尝试过
        self.assertIsNone(state.last_action)    # 失败不写 last_action
        self.assertIsNone(state.last_adjust)
        self.assertEqual(state.observe_since, 0.0)
        self.assertNotEqual(win, [])            # 失败不清窗口


# ============================== 等口看门狗 ==============================

def _ring_missing(node) -> bool:
    return (isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Call)
            and isinstance(node.left.func, ast.Name)
            and node.left.func.id == "line_ring_ratio"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Lt)
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value == 0)


def _watchdog_gate(tree):
    """返回 (缺环判断节点列表, 其外层 If 节点列表)。"""
    compares = [n for n in ast.walk(tree) if _ring_missing(n)]
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    ifs = []
    for cmp_node in compares:
        node = parents.get(cmp_node)
        while node is not None and not isinstance(node, ast.If):
            node = parents.get(node)
        ifs.append(node)
    return compares, ifs


class TestWaitRingWatchdogStatic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = _tree()
        cls.compares, cls.ifs = _watchdog_gate(cls.tree)

    def test_default_is_zero_and_compares_to_one(self):
        found = []
        for gate in self.ifs:
            if gate is None:
                continue
            for node in ast.walk(gate.test):
                if not isinstance(node, ast.Compare):
                    continue
                call = node.left
                if not (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "get"):
                    continue
                if not (isinstance(call.func.value, ast.Attribute)
                        and call.func.value.attr == "environ"):
                    continue
                if (len(call.args) >= 2
                        and isinstance(call.args[0], ast.Constant)
                        and call.args[0].value == "RF4_WAIT_RING_WATCHDOG"):
                    found.append(call)
                    self.assertEqual(call.args[1].value, "0")
                    self.assertIsInstance(call.args[1], ast.Constant)
                    self.assertIsInstance(node.comparators[0], ast.Constant)
                    self.assertEqual(node.comparators[0].value, "1")
        self.assertTrue(found, "缺少 RF4_WAIT_RING_WATCHDOG 显式开关")

    def test_missing_ring_never_escalates_on_its_own(self):
        self.assertTrue(self.compares, "找不到缺环判断")
        for gate in self.ifs:
            self.assertIsNotNone(gate, "缺环判断必须位于 if 条件中")
            self.assertIsInstance(gate.test, ast.BoolOp)
            self.assertIsInstance(gate.test.op, ast.And)
            self.assertTrue(
                any(_ring_missing(sub) for sub in ast.walk(gate.test)),
                "缺环必须是 and 的一侧，不能单独触发")

    def test_recovery_goes_through_unreadable_hud_resolver(self):
        for gate in self.ifs:
            if gate is None:
                continue
            names = {n.id for n in ast.walk(gate)
                     if isinstance(n, ast.Name)}
            attrs = {n.attr for n in ast.walk(gate)
                     if isinstance(n, ast.Attribute)}
            self.assertIn("resolve_unreadable_hud", names | attrs)
            constants = {n.value for n in ast.walk(gate)
                         if isinstance(n, ast.Constant)
                         and isinstance(n.value, str)}
            self.assertIn("recovered", constants)
            self.assertIn("healthy", constants)


if __name__ == "__main__":
    unittest.main()
