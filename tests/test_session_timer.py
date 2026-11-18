# -*- coding: utf-8 -*-
"""会话时限计时纯逻辑测试（无 Tk / 无 IPC）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import autofish_session_timer as st  # noqa: E402


class ClampLimitTests(unittest.TestCase):
    def test_default_and_bounds(self) -> None:
        self.assertEqual(st.clamp_limit(None), 8 * 3600)
        self.assertEqual(st.clamp_limit("bad"), 8 * 3600)
        self.assertEqual(st.clamp_limit(10), st.MIN_LIMIT_SECONDS)
        self.assertEqual(st.clamp_limit(999999), st.MAX_LIMIT_SECONDS)
        self.assertEqual(st.clamp_limit(3600), 3600)


class SessionTimerTests(unittest.TestCase):
    def test_default_limit_is_eight_hours(self) -> None:
        t = st.SessionTimer()
        self.assertEqual(t.limit, 8 * 3600)
        self.assertEqual(t.state, st.PAUSED)

    def test_pause_does_not_count(self) -> None:
        t = st.SessionTimer(limit_seconds=3600)
        t.start(1000.0)
        t.pause(1300.0)        # 跑 300s
        self.assertEqual(t.elapsed_seconds(9999.0), 300.0)

    def test_resume_accumulates_across_sessions(self) -> None:
        t = st.SessionTimer(limit_seconds=3600)
        t.start(1000.0)
        t.pause(1300.0)        # +300
        t.resume(2000.0)
        t.pause(2400.0)        # +400
        self.assertEqual(t.elapsed_seconds(2400.0), 700.0)

    def test_expire_only_when_enough_runtime(self) -> None:
        t = st.SessionTimer(limit_seconds=600)
        t.start(0.0)
        self.assertFalse(t.expired(599.0))
        self.assertTrue(t.expired(600.0))
        self.assertEqual(t.state, st.EXPIRED)

    def test_expired_is_sticky_and_resume_cannot_revive(self) -> None:
        t = st.SessionTimer(limit_seconds=600)
        t.start(0.0)
        self.assertTrue(t.expired(600.0))
        t.resume(700.0)
        self.assertEqual(t.state, st.EXPIRED)
        self.assertTrue(t.expired(7000.0))

    def test_remaining_floors_at_zero(self) -> None:
        t = st.SessionTimer(limit_seconds=600)
        t.start(0.0)
        self.assertEqual(t.remaining(0.0), 600.0)
        self.assertEqual(t.remaining(300.0), 300.0)
        self.assertEqual(t.remaining(900.0), 0.0)

    def test_set_limit_mid_session(self) -> None:
        t = st.SessionTimer(limit_seconds=3600)
        t.start(0.0)
        t.set_limit(120)       # 改成 2 分钟，已跑时间保留
        self.assertFalse(t.expired(100.0))
        self.assertTrue(t.expired(120.0))

    def test_reset_clears_everything(self) -> None:
        t = st.SessionTimer(limit_seconds=3600)
        t.start(0.0)
        t.pause(100.0)
        t.reset()
        self.assertEqual(t.state, st.PAUSED)
        self.assertEqual(t.elapsed_seconds(999.0), 0.0)


class PersistenceTests(unittest.TestCase):
    def test_roundtrip_running(self) -> None:
        t = st.SessionTimer(limit_seconds=7200)
        t.start(1000.0)
        data = t.to_dict()
        t2 = st.SessionTimer.from_dict(data)
        self.assertEqual(t2.limit, 7200)
        self.assertEqual(t2.state, st.RUNNING)
        self.assertEqual(t2.started_at, 1000.0)
        self.assertEqual(t2.elapsed_seconds(1600.0), 600.0)

    def test_roundtrip_paused(self) -> None:
        t = st.SessionTimer(limit_seconds=3600)
        t.start(1000.0)
        t.pause(1300.0)
        t2 = st.SessionTimer.from_dict(t.to_dict())
        self.assertEqual(t2.state, st.PAUSED)
        self.assertIsNone(t2.started_at)
        self.assertEqual(t2.elapsed, 300.0)

    def test_from_garbage_is_safe(self) -> None:
        t = st.SessionTimer.from_dict({"state": "weird", "started_at": "x"})
        self.assertEqual(t.state, st.PAUSED)
        self.assertIsNone(t.started_at)


if __name__ == "__main__":
    unittest.main()
