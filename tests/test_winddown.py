# -*- coding: utf-8 -*-
"""收尾编排状态机测试（纯逻辑）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import autofish_winddown as wd  # noqa: E402


class WinddownNextTests(unittest.TestCase):
    def test_sequence(self) -> None:
        self.assertEqual(wd.winddown_next(wd.GOTO), wd.SELL)
        self.assertEqual(wd.winddown_next(wd.SELL), wd.LOGOUT)
        self.assertIsNone(wd.winddown_next(wd.LOGOUT))
        self.assertIsNone(wd.winddown_next(wd.DONE))
        self.assertIsNone(wd.winddown_next("?"))


class WinddownStateTests(unittest.TestCase):
    def test_happy_path_reaches_done(self) -> None:
        w = wd.WinddownState()
        w.start(1000.0)
        self.assertEqual(w.state, wd.GOTO)
        w.record(wd.GOTO, True, "鱼市到达", 1100.0)
        w.record(wd.SELL, True, "已出售", 1200.0)
        w.record(wd.LOGOUT, True, "停在登录界面", 1300.0)
        self.assertEqual(w.state, wd.DONE)
        self.assertTrue(w.done)
        self.assertIsNone(w.failed_step)

    def test_failure_stops_immediately(self) -> None:
        w = wd.WinddownState()
        w.start(1000.0)
        w.record(wd.GOTO, True, "ok", 1100.0)
        w.record(wd.SELL, False, "未识别商店界面", 1200.0)
        self.assertEqual(w.state, wd.FAILED)
        self.assertEqual(w.failed_step, wd.SELL)
        self.assertIn("商店", w.failed_reason)
        self.assertTrue(w.done)

    def test_all_step_results_recorded(self) -> None:
        w = wd.WinddownState()
        w.start(1000.0)
        for step in wd.STEPS:
            w.record(step, True, "ok", 1000.0)
        self.assertEqual(len(w.step_results), 3)

    def test_roundtrip(self) -> None:
        w = wd.WinddownState()
        w.start(1000.0)
        w.record(wd.GOTO, False, "路线不存在", 1001.0)
        w2 = wd.WinddownState.from_dict(w.to_dict())
        self.assertEqual(w2.state, wd.FAILED)
        self.assertEqual(w2.failed_step, wd.GOTO)
        self.assertEqual(w2.failed_reason, "路线不存在")

    def test_start_clears_previous(self) -> None:
        w = wd.WinddownState()
        w.start(1000.0)
        w.record(wd.GOTO, False, "fail", 1001.0)
        w.start(2000.0)
        self.assertEqual(w.state, wd.GOTO)
        self.assertIsNone(w.failed_step)
        self.assertEqual(w.step_results, {})


if __name__ == "__main__":
    unittest.main()
