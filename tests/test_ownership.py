# -*- coding: utf-8 -*-
"""控制权归属纯逻辑测试（无 Tk / 无 Win32）。"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import autofish_ownership as own  # noqa: E402


class OwnershipStateTests(unittest.TestCase):
    def test_foreground_no_physical_is_tool(self) -> None:
        self.assertEqual(own.ownership_state(True), own.TOOL)

    def test_foreground_lost_is_user(self) -> None:
        self.assertEqual(own.ownership_state(False), own.USER)
        self.assertEqual(own.ownership_state("false"), own.USER)

    def test_recent_physical_input_while_foreground_is_user(self) -> None:
        now = 1_000_000.0
        self.assertEqual(
            own.ownership_state(True, last_physical_at=now - 1.0, now=now),
            own.USER,
        )

    def test_stale_physical_input_is_tool(self) -> None:
        now = 1_000_000.0
        self.assertEqual(
            own.ownership_state(True, last_physical_at=now - 10.0, now=now),
            own.TOOL,
        )

    def test_returning_wins_over_foreground(self) -> None:
        self.assertEqual(
            own.ownership_state(True, returning=True), own.RETURNING
        )

    def test_stopped_reports_tool(self) -> None:
        self.assertEqual(
            own.ownership_state(False, stopped=True), own.TOOL
        )

    def test_unknown_foreground_without_physical_is_tool(self) -> None:
        self.assertEqual(own.ownership_state(None), own.TOOL)


class OwnershipTextTests(unittest.TestCase):
    def test_all_states_have_text(self) -> None:
        self.assertEqual(own.ownership_text(own.TOOL), "工具控制中")
        self.assertEqual(own.ownership_text(own.USER), "你控制中（工具不动作）")
        self.assertEqual(own.ownership_text(own.RETURNING), "等你确认是否继续")
        self.assertEqual(own.ownership_text("?"), "归属未知")


class HumanDurationTests(unittest.TestCase):
    def test_boundaries(self) -> None:
        cases = [
            (0, "刚停止"),
            (1.9, "刚停止"),
            (12, "12 秒"),
            (59, "59 秒"),
            (60, "1 分"),
            (192, "3 分 12 秒"),
            (3600, "1 小时"),
            (3900, "1 小时 5 分"),
        ]
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(own.human_duration(seconds), expected)

    def test_invalid_is_dash(self) -> None:
        self.assertEqual(own.human_duration(None), "—")
        self.assertEqual(own.human_duration("x"), "—")
        self.assertEqual(own.human_duration(-5), "刚停止")


class NoticeTextTests(unittest.TestCase):
    def test_contains_clock_and_duration(self) -> None:
        now = 1_000_000.0
        since = now - 192
        text = own.notice_text(since, now=now)
        self.assertIn("控制权", text)
        self.assertIn("停止于", text)
        self.assertIn("3 分 12 秒", text)
        self.assertRegex(text, r"停止于 \d{2}:\d{2}:\d{2}")

    def test_invalid_since_does_not_crash(self) -> None:
        text = own.notice_text(None, now=1_000_000.0)
        self.assertIn("已停止：—", text)


class NoticeMessageTests(unittest.TestCase):
    """面板 message box 正文：大字结论 + 绝对时刻 + 易读时长 + 后续指引。"""

    @classmethod
    def setUpClass(cls) -> None:
        import autofish_ui  # noqa: F401 - 延迟导入，避免无 Tk 环境下顶层失败

    def test_message_has_clock_and_human_duration(self) -> None:
        import autofish_ui
        now = 1_000_000.0
        text = autofish_ui.AutoFishPanel._notice_message(now - 192, now)
        self.assertIn("工具已停止输入", text)
        self.assertIn("停止于", text)
        self.assertIn("3 分 12 秒", text)
        self.assertIn("回到游戏", text)

    def test_edge_triggers_messagebox_and_resets(self) -> None:
        from unittest import mock
        import autofish_ui

        panel = object.__new__(autofish_ui.AutoFishPanel)
        panel._notice_nonmodal = False
        panel._notice_beep = False
        panel.demo = False
        panel._clock = lambda: 1000.0
        panel._notice_state = False
        panel._notice_since = None
        panel._prev_ownership = own.ownership_text(own.TOOL)
        fake_mb = mock.Mock()
        panel.tk = mock.Mock(messagebox=fake_mb)
        panel.root = mock.Mock()

        view = mock.Mock(connected=True, stopped=False)
        view.ownership_text = own.ownership_text(own.USER)
        panel._update_ownership_notice(view)

        # after(0, cb) 已登记；执行回调 = 弹出 message box
        callback = panel.root.after.call_args[0][1]
        callback()
        self.assertTrue(fake_mb.showwarning.called)
        self.assertEqual(
            fake_mb.showwarning.call_args[1].get("title"), "控制权已交还给你")
        self.assertFalse(panel._notice_state)  # 关闭后复位，下次边沿可再弹

    def test_demo_does_not_popup(self) -> None:
        from unittest import mock
        import autofish_ui

        panel = object.__new__(autofish_ui.AutoFishPanel)
        panel._notice_nonmodal = False
        panel._notice_beep = False
        panel.demo = True
        panel._clock = lambda: 1000.0
        panel._notice_state = False
        panel._notice_since = None
        panel._prev_ownership = own.ownership_text(own.TOOL)
        panel.tk = mock.Mock()
        panel.root = mock.Mock()
        view = mock.Mock(connected=True, stopped=False)
        view.ownership_text = own.ownership_text(own.USER)
        panel._update_ownership_notice(view)
        self.assertFalse(panel.root.after.called)

    def test_alternating_handoffs_pop_each_time(self) -> None:
        """多次 TOOL→USER 交接每次都弹；复位失败会导致第二次不弹。"""
        from unittest import mock
        import autofish_ui

        panel = object.__new__(autofish_ui.AutoFishPanel)
        panel._notice_nonmodal = False
        panel._notice_beep = False
        panel.demo = False
        panel._clock = lambda: 1000.0
        panel._notice_state = False
        panel._notice_since = None
        panel._prev_ownership = own.ownership_text(own.TOOL)
        fake_mb = mock.Mock()
        panel.tk = mock.Mock(messagebox=fake_mb)
        panel.root = mock.Mock()

        tool = mock.Mock(connected=True, stopped=False)
        tool.ownership_text = own.ownership_text(own.TOOL)
        user = mock.Mock(connected=True, stopped=False)
        user.ownership_text = own.ownership_text(own.USER)

        for _ in range(3):
            panel._update_ownership_notice(user)   # TOOL -> USER
            cb = panel.root.after.call_args[0][1]
            cb()                                    # 弹窗 -> 关闭复位
            panel._update_ownership_notice(tool)   # USER -> TOOL

        self.assertEqual(fake_mb.showwarning.call_count, 3)

    def test_user_stays_user_does_not_repopup(self) -> None:
        """持续 USER 不重复弹，避免骚扰。"""
        from unittest import mock
        import autofish_ui

        panel = object.__new__(autofish_ui.AutoFishPanel)
        panel._notice_nonmodal = False
        panel._notice_beep = False
        panel.demo = False
        panel._clock = lambda: 1000.0
        panel._notice_state = False
        panel._notice_since = None
        panel._prev_ownership = own.ownership_text(own.TOOL)
        fake_mb = mock.Mock()
        panel.tk = mock.Mock(messagebox=fake_mb)
        panel.root = mock.Mock()
        user = mock.Mock(connected=True, stopped=False)
        user.ownership_text = own.ownership_text(own.USER)

        panel._update_ownership_notice(user)
        cb = panel.root.after.call_args[0][1]
        cb()
        # 后续多帧仍是 USER，不应再登记
        panel.root.after.reset_mock()
        for _ in range(5):
            panel._update_ownership_notice(user)
        self.assertFalse(panel.root.after.called)
        self.assertEqual(fake_mb.showwarning.call_count, 1)


if __name__ == "__main__":
    unittest.main()
