"""入护闭环：点按钮 -> 等动画 -> 强模板复核（最多 3 次）。

纯离线：只从 `rf4s_adapter.py` 里 AST 抽出函数，注入假检测/假点击，
不截图、不聚焦、不发真实输入。

覆盖 2026-11-05 实测事故：
  * 单次点击后结算没消失（按钮位置偶尔偏移），旧实现照样 total/kept +1；
  * 首点失败、第二/第三次才成功；
  * 结算框被「确定」弹窗盖住，清弹窗后复核恢复；
  * 检测抛异常不得当成功；
  * 多次点击只记一次 kept/total；3 次都失败要抛错且不记账。

运行：
    python -m unittest discover -s tests -p "test_capture_keep_retry.py" -v
"""

from __future__ import annotations

import ast
import builtins
import pathlib
import sys
import unittest
from types import SimpleNamespace

BOT = pathlib.Path(__file__).resolve().parents[1]
ADAPTER = BOT / "rf4s_adapter.py"

KEEP_BTN = (830, 975)


# ============================== AST 工具 ==============================

def _tree() -> ast.Module:
    return ast.parse(ADAPTER.read_text(encoding="utf-8"))


def _find_func(name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"adapter 里找不到函数 {name}")


def _exec_funcs(names, extra=None) -> dict:
    nodes = [_find_func(n) for n in names]
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    code = compile(module, str(ADAPTER), "exec")
    ns = {"__name__": "rf4s_adapter_fake", "__builtins__": builtins}
    ns.update(extra or {})
    exec(code, ns)
    return ns


# ============================== 假对象 ==============================

class _ScriptedDetect:
    """按脚本依次返回 True(结算还在)/False(已消失)/异常实例；用完后沿用最后一项。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __call__(self):
        i = min(self.calls, len(self.script) - 1)
        self.calls += 1
        item = self.script[i]
        if isinstance(item, BaseException):
            raise item
        return item


class _Player:
    def __init__(self, detect, locate=None, modal_ok=False):
        self.clicks = []
        self.located = 0
        self.dismissed = 0
        self.quit_reason = None
        self.detection = SimpleNamespace(
            is_fish_captured=detect,
            is_keepnet_full=lambda: False,
        )
        self.result = SimpleNamespace(total=0, kept=0)
        self.general_quit = self._quit
        self._locate = locate
        self._modal_ok = modal_ok
        self.locate_calls = 0

    def _quit(self, reason):
        self.quit_reason = reason

    # ---- 注入给被测函数的外部动作 ----
    def locate(self):
        self.locate_calls += 1
        if self._locate is not None:
            return self._locate(self.locate_calls)
        return (780, 932)

    def click(self, btn):
        self.clicks.append(btn)

    def wait(self):
        pass

    def dismiss(self):
        self.dismissed += 1


class _Logger:
    def __init__(self):
        self.msgs = []

    def info(self, msg):
        self.msgs.append(msg)


def _ns(player, logger=None):
    class CaptureKeepNotSettled(RuntimeError):
        pass

    ns = _exec_funcs(
        ["_settle_state", "click_keep_until_settled", "_handle_fish"],
        {
            "CAPTURE_KEEP_BTN": KEEP_BTN,
            "CAPTURE_KEEP_ATTEMPTS": 3,
            "CAPTURE_SETTLE_WAIT": 0.8,
            "CaptureKeepNotSettled": CaptureKeepNotSettled,
        },
    )
    ns.update({
        "focus_game_quiet": lambda: True,
        "find_capture_keep_btn": player.locate,
        "_click_client": lambda x, y: player.click((x, y)),
        "maybe_dismiss": lambda tag="": player.dismiss(),
        "sleep": lambda s: None,
        "add_jitter": lambda s: s,
        "logger": logger or _Logger(),
        "print": lambda *a, **k: None,
    })
    return ns


def _run_handler(player):
    ns = _ns(player)
    ns["_handle_fish"](player)
    return ns


# ============================== 用例 ==============================

class TestClickKeepRetry(unittest.TestCase):
    def test_first_click_fails_second_succeeds(self):
        # 第 1 次点击后结算还在（含清弹窗后复核），第 2 次点击后消失
        player = _Player(_ScriptedDetect([True, True, False]))
        ns = _ns(player)
        attempt = ns["click_keep_until_settled"](
            player.detection.is_fish_captured, player.locate,
            player.click, player.wait, player.dismiss)
        self.assertEqual(attempt, 2)
        self.assertEqual(len(player.clicks), 2)
        self.assertEqual(player.dismissed, 1)
        # 每次都要重新定位，不能复用上一次的坐标
        self.assertEqual(player.locate_calls, 2)

    def test_ok_modal_dismiss_then_reverify_recovers(self):
        # 点击后结算被「确定」弹窗盖住 -> 复核仍在 -> 清弹窗 -> 复核消失
        player = _Player(_ScriptedDetect([True, False]))
        ns = _ns(player)
        attempt = ns["click_keep_until_settled"](
            player.detection.is_fish_captured, player.locate,
            player.click, player.wait, player.dismiss)
        self.assertEqual(attempt, 1)
        self.assertEqual(len(player.clicks), 1)
        self.assertEqual(player.dismissed, 1)

    def test_detect_exception_is_never_success(self):
        player = _Player(_ScriptedDetect([RuntimeError("boom")]))
        ns = _ns(player)
        with self.assertRaises(ns["CaptureKeepNotSettled"]):
            ns["click_keep_until_settled"](
                player.detection.is_fish_captured, player.locate,
                player.click, player.wait, player.dismiss)
        self.assertEqual(len(player.clicks), 3)
        gone, why = ns["_settle_state"](
            player.detection.is_fish_captured)
        self.assertFalse(gone)
        self.assertEqual(why, "detect_error:RuntimeError")

    def test_locate_failure_falls_back_to_fixed_btn(self):
        player = _Player(_ScriptedDetect([False]), locate=lambda n: None)
        ns = _ns(player)
        attempt = ns["click_keep_until_settled"](
            player.detection.is_fish_captured, player.locate,
            player.click, player.wait, player.dismiss)
        self.assertEqual(attempt, 1)
        self.assertEqual(player.clicks, [KEEP_BTN])


class TestHandleFishAccounting(unittest.TestCase):
    def test_three_failures_raise_and_never_count(self):
        player = _Player(_ScriptedDetect([True]))
        with self.assertRaises(RuntimeError) as ctx:
            _run_handler(player)
        self.assertEqual(type(ctx.exception).__name__, "CaptureKeepNotSettled")
        self.assertEqual(player.result.total, 0)
        self.assertEqual(player.result.kept, 0)
        self.assertEqual(len(player.clicks), 3)
        self.assertIsNone(player.quit_reason)

    def test_detect_exception_raises_and_never_counts(self):
        player = _Player(_ScriptedDetect([ValueError("bad frame")]))
        with self.assertRaises(RuntimeError) as ctx:
            _run_handler(player)
        self.assertEqual(type(ctx.exception).__name__, "CaptureKeepNotSettled")
        self.assertEqual(player.result.total, 0)
        self.assertEqual(player.result.kept, 0)

    def test_success_on_second_attempt_counts_exactly_once(self):
        player = _Player(_ScriptedDetect([True, True, False]))
        _run_handler(player)
        self.assertEqual(len(player.clicks), 2)
        self.assertEqual(player.result.total, 1)
        self.assertEqual(player.result.kept, 1)

    def test_success_on_first_attempt_counts_once(self):
        player = _Player(_ScriptedDetect([False]))
        _run_handler(player)
        self.assertEqual(len(player.clicks), 1)
        self.assertEqual(player.result.total, 1)
        self.assertEqual(player.result.kept, 1)


class TestSourceInvariants(unittest.TestCase):
    def test_error_class_is_module_level_runtimeerror(self):
        """prod 里 raise 的类型必须真的存在，且是可读的 RuntimeError 子类。"""
        classes = [n for n in _tree().body
                   if isinstance(n, ast.ClassDef)
                   and n.name == "CaptureKeepNotSettled"]
        self.assertEqual(len(classes), 1)
        bases = {ast.unparse(b) for b in classes[0].bases}
        self.assertIn("RuntimeError", bases)

    def test_no_space_key_in_keep_path(self):
        """入护只能点按钮，绝不能退回按空格（空格=抄网）。"""
        for name in ("_handle_fish", "click_keep_until_settled"):
            src = ast.unparse(_find_func(name)).lower()
            for bad in ('"space"', "'space'", "press(", "keydown", "pagedown"):
                self.assertNotIn(bad, src, f"{name} 里出现了 {bad}")

    def test_handle_fish_counts_after_settlement_confirmed(self):
        """total/kept 的累加必须排在 click_keep_until_settled 之后。"""
        fn = _find_func("_handle_fish")
        call_line = min(n.lineno for n in ast.walk(fn)
                        if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Name)
                        and n.func.id == "click_keep_until_settled")
        adds = [n.lineno for n in ast.walk(fn)
                if isinstance(n, ast.AugAssign)
                and isinstance(n.target, ast.Attribute)
                and n.target.attr in ("total", "kept")]
        self.assertTrue(adds)
        self.assertGreater(min(adds), call_line)


if __name__ == "__main__":
    unittest.main()
