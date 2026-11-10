"""HUD 看门狗离线测试：AST 提取 + 假实现。

约束（与本次任务一致）：
  * 不抓图、不碰游戏窗口、不发任何输入；
  * 不从 `rf4s_adapter` 直接 import（它会 SetProcessDpiAwareness / chdir），
    而是用 AST 把要测的函数源码抠出来 exec，注入假依赖；
  * 不断言具体日志全文，只断言行为：没弹窗不点、弹窗消失不等于 HUD 恢复、
    只有重新抓帧读环成功才算恢复。

运行：python -m pytest tests/test_hud_watchdog.py -q
"""

from __future__ import annotations

import ast
import pathlib
import sys
import unittest
from unittest import mock

import numpy as np
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import autofish_observation as obs  # noqa: E402

ADAPTER = ROOT / "rf4s_adapter.py"
SRC = ADAPTER.read_text(encoding="utf-8")
TREE = ast.parse(SRC)


def _func_node(name: str) -> ast.FunctionDef:
    """按名字找函数；`start_bottom_mode_held` 是嵌套函数，所以用 walk。"""
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"rf4s_adapter 里找不到函数 {name}")


def _func_source(name: str) -> str:
    return ast.get_source_segment(SRC, _func_node(name))


def _load(name: str, **namespace):
    ns = dict(namespace)
    exec(compile(_func_source(name), f"<{name}>", "exec"), ns)
    return ns[name]


def _parents(root: ast.AST) -> dict[ast.AST, ast.AST]:
    table: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(root):
        for child in ast.iter_child_nodes(parent):
            table[child] = parent
    return table


def _enclosing_if(node: ast.AST, parents: dict[ast.AST, ast.AST]):
    cur = parents.get(node)
    while cur is not None and not isinstance(cur, ast.If):
        cur = parents.get(cur)
    return cur


def _is_outcome_test(node: ast.AST, value: str) -> bool:
    """判断 `if outcome == "<value>"`。"""
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return False
    if not isinstance(node.ops[0], ast.Eq):
        return False
    if not isinstance(node.left, ast.Name) or node.left.id != "outcome":
        return False
    right = node.comparators[0]
    return isinstance(right, ast.Constant) and right.value == value


class TestResolveUnreadableHud(unittest.TestCase):
    """纯决策 helper：所有副作用注入，覆盖三种关键结局。"""

    def _run(self, rings, *, modal, dismiss_ok=True):
        log: list[str] = []
        seq = iter(rings)

        def read_ring():
            log.append("read")
            return next(seq)

        def modal_present():
            log.append("modal")
            return modal

        def dismiss():
            log.append("dismiss")
            return dismiss_ok

        fn = _load("resolve_unreadable_hud")
        return fn(read_ring, modal_present, dismiss), log

    def test_ring_already_readable_is_healthy(self):
        outcome, log = self._run([0.5], modal=True)
        self.assertEqual(outcome, "healthy")
        self.assertEqual(log, ["read"], "环已可读时不许检测弹窗/点击")

    def test_no_modal_never_dismisses(self):
        """核心回归：没有弹窗时绝不能调用 dismiss（旧代码在这里谎报清完）。"""
        outcome, log = self._run([-1.0], modal=False)
        self.assertEqual(outcome, "no_modal")
        self.assertEqual(log, ["read", "modal"])
        self.assertNotIn("dismiss", log)

    def test_dismiss_failure_is_not_recovery(self):
        outcome, log = self._run([-1.0], modal=True, dismiss_ok=False)
        self.assertEqual(outcome, "modal_failed")
        self.assertEqual(log, ["read", "modal", "dismiss"])
        self.assertNotIn("recovered", outcome)

    def test_modal_gone_but_ring_unreadable_is_not_recovery(self):
        """弹窗点掉了但环仍读不到：不得报 HUD 恢复，且必须重新读一次环。"""
        outcome, log = self._run([-1.0, -1.0], modal=True)
        self.assertEqual(outcome, "still_hidden")
        self.assertEqual(log, ["read", "modal", "dismiss", "read"],
                         "dismiss 之后必须 fresh 重读环，不能复用旧读数")

    def test_real_recovery_needs_second_ring_read(self):
        outcome, log = self._run([-1.0, 0.42], modal=True)
        self.assertEqual(outcome, "recovered")
        self.assertEqual(log, ["read", "modal", "dismiss", "read"])
        self.assertGreater(log.count("read"), 1, "只读一次环不算恢复")


class TestDismissModalsSemantics(unittest.TestCase):
    """`dismiss_modals` 的返回值语义：没点过 = False。"""

    def _load(self, present_results):
        state = {"i": 0, "clicks": []}

        def present():
            i = state["i"]
            state["i"] += 1
            return present_results[min(i, len(present_results) - 1)]

        fn = _load(
            "dismiss_modals",
            _ok_button_present=present,
            focus_game_quiet=lambda: None,
            _click_client=lambda x, y: state["clicks"].append((x, y)),
            time=mock.Mock(sleep=lambda *_: None),
            print=lambda *a, **k: None,
            OK_BTN_CENTER=(960, 810),
        )
        return fn, state

    def test_no_modal_returns_false_without_clicking(self):
        fn, state = self._load([False])
        self.assertFalse(fn())
        self.assertEqual(state["clicks"], [])

    def test_one_modal_clicked_returns_true(self):
        fn, state = self._load([True, False])
        self.assertTrue(fn())
        self.assertEqual(len(state["clicks"]), 1)

    def test_button_stuck_returns_false(self):
        fn, state = self._load([True])
        self.assertFalse(fn(max_clicks=2))
        self.assertEqual(len(state["clicks"]), 2)

    def test_max_clicks_then_gone_still_true(self):
        fn, state = self._load([True, True, False])
        self.assertTrue(fn(max_clicks=2))
        self.assertEqual(len(state["clicks"]), 2)


class TestOkButtonPresentUsesStructure(unittest.TestCase):
    """`_ok_button_present` 复用 detect_ok_modal 的结构判据。"""

    @staticmethod
    def _modal_frame() -> Image.Image:
        a = np.zeros((1080, 1920, 3), np.uint8)
        x0, y0, x1, y1 = obs.OK_BTN_BOX
        a[y0, x0:x1 + 1] = (235, 235, 235)
        a[y1, x0:x1 + 1] = (235, 235, 235)
        a[y0:y1 + 1, x0] = (235, 235, 235)
        a[y0:y1 + 1, x1] = (235, 235, 235)
        return Image.fromarray(a, "RGB")

    def _present(self, frame=None, error=None):
        def capture():
            if error is not None:
                raise error
            return frame, (0, 0)

        fn = _load("_ok_button_present", capture_game=capture)
        return fn()

    def test_synthetic_modal_detected(self):
        self.assertTrue(self._present(self._modal_frame()))

    def test_blank_frame_is_absent(self):
        self.assertFalse(self._present(Image.new("RGB", (1920, 1080))))

    def test_capture_error_is_absent(self):
        """抓帧失败时返回 False（宁可漏点，也不能乱点）。"""
        self.assertFalse(self._present(error=OSError("boom")))

    def test_reuses_detect_ok_modal_not_old_border_check(self):
        node = _func_node("_ok_button_present")
        attrs = [n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)]
        self.assertIn("detect_ok_modal", attrs)


class TestWatchdogWiring(unittest.TestCase):
    """AST 约束：看门狗只有在确认按钮后才走清理，且成功文案受环恢复保护。"""

    @classmethod
    def setUpClass(cls):
        cls.fn = _func_node("start_bottom_mode_held")
        cls.parents = _parents(cls.fn)

    def test_dismiss_only_reached_through_guarded_helper(self):
        calls = [n for n in ast.walk(self.fn) if isinstance(n, ast.Call)]
        direct = [c for c in calls
                  if isinstance(c.func, ast.Name) and c.func.id == "dismiss_modals"]
        self.assertEqual(direct, [], "不得直接调用 dismiss_modals（必须经 helper）")

        helper = [c for c in calls
                  if isinstance(c.func, ast.Name)
                  and c.func.id == "resolve_unreadable_hud"]
        self.assertEqual(len(helper), 1, "看门狗应调用一次恢复决策 helper")
        args = [a.id if isinstance(a, ast.Name) else None
                for a in helper[0].args]
        self.assertEqual(args, ["line_ring_ratio", "_ok_button_present",
                                "dismiss_modals"],
                         "第二个参数必须是真的按钮检测，不能无条件 dismiss")

    def test_success_message_requires_outcome_recovered(self):
        msg = [n for n in ast.walk(self.fn)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and "排队弹窗已自动点完" in n.value]
        self.assertEqual(len(msg), 1, "成功文案应只有一处")
        guard = _enclosing_if(msg[0], self.parents)
        self.assertIsNotNone(guard, "成功文案必须在 if 分支里")
        self.assertTrue(_is_outcome_test(guard.test, "recovered"),
                        "只有 outcome == 'recovered' 才允许报清完")

    def test_no_modal_branch_warns_and_does_not_continue(self):
        msg = [n for n in ast.walk(self.fn)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and "ring unreadable no modal" in n.value]
        self.assertEqual(len(msg), 1, "无弹窗必须打明确的 warn 日志")
        guard = _enclosing_if(msg[0], self.parents)
        self.assertIsNotNone(guard)
        self.assertTrue(_is_outcome_test(guard.test, "no_modal"))
        continues = [n for n in ast.walk(guard) if isinstance(n, ast.Continue)]
        self.assertEqual(continues, [],
                         "无弹窗分支不得 continue，否则会跳过升级")

    def test_escalation_path_is_present(self):
        esc = [c for c in ast.walk(self.fn)
               if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
               and c.func.attr == "escalate"]
        self.assertTrue(esc, "必须保留 agent_hook.escalate 上报路径")
        kinds = [c.args[0].value for c in esc
                 if c.args and isinstance(c.args[0], ast.Constant)]
        self.assertIn("hud_hidden", kinds)


if __name__ == "__main__":
    unittest.main()
