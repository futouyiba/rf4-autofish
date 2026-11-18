# -*- coding: utf-8 -*-
"""会话时限接入 runtime 的测试（最小壳，聚焦 _tick_session）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import autofish_runtime as art  # noqa: E402
import autofish_session_timer as st  # noqa: E402


def _runtime_with(timer, *, auto=True, state="WAIT_BITE", stopped_flags=None):
    """构造最小 runtime 壳，只给 _tick_session 需要的属性。"""
    rt = object.__new__(art.AutoFishRuntime)
    rt._session_timer = timer
    rt._winddown_pending = False
    calls = []
    rt._control = SimpleNamespace(
        request_stop_after_fish=lambda: calls.append("stop_after_fish"),
    )
    rt._coordinator = SimpleNamespace(
        auto_requested=auto,
        state=SimpleNamespace(name=state),
        _clock=lambda: 0.0,
    )
    return rt, calls


class SessionTickTests(unittest.TestCase):
    def test_no_timer_is_noop(self) -> None:
        rt, calls = _runtime_with(None)
        snap = {}
        rt._tick_session(snap)
        self.assertEqual(calls, [])
        self.assertNotIn("session_status", snap)

    def test_auto_accumulates_and_snapshot_has_fields(self) -> None:
        timer = st.SessionTimer(limit_seconds=3600)
        rt, calls = _runtime_with(timer)
        snap = {}
        # 用 runtime 注入的 clock（coordinator._clock=0）→ elapsed 0；
        # 改 clock 推进验证累计
        rt._coordinator._clock = lambda: 100.0
        rt._tick_session(snap)
        self.assertEqual(snap["session_status"]["state"], st.RUNNING)
        self.assertEqual(snap["session_status"]["remaining_seconds"], 3600.0)
        self.assertEqual(calls, [])

    def test_paused_when_not_auto(self) -> None:
        timer = st.SessionTimer(limit_seconds=3600)
        rt, calls = _runtime_with(timer, auto=False)
        rt._coordinator._clock = lambda: 100.0
        snap = {}
        rt._tick_session(snap)
        self.assertEqual(snap["session_status"]["state"], st.PAUSED)

    def test_expire_latches_stop_after_fish_once(self) -> None:
        timer = st.SessionTimer(limit_seconds=60)  # 最小合法时限 60s
        rt, calls = _runtime_with(timer)
        clock = {"t": 100.0}
        rt._coordinator._clock = lambda: clock["t"]
        for i in range(5):
            clock["t"] = 100.0 + i * 30   # 0,30,60,... 第3次到60s
            rt._tick_session({})
        self.assertEqual(calls, ["stop_after_fish"])  # 只发一次
        self.assertTrue(rt._winddown_pending)

    def test_fault_state_pauses(self) -> None:
        timer = st.SessionTimer(limit_seconds=3600)
        rt, calls = _runtime_with(timer, state="FAULT")
        rt._coordinator._clock = lambda: 100.0
        snap = {}
        rt._tick_session(snap)
        self.assertEqual(snap["session_status"]["state"], st.PAUSED)

    def test_session_snapshot_has_all_keys(self) -> None:
        timer = st.SessionTimer()
        rt, _ = _runtime_with(timer)
        snap = {}
        rt._tick_session(snap)
        for key in ("limit_seconds", "elapsed_seconds", "remaining_seconds",
                    "state", "expired", "winddown_pending"):
            self.assertIn(key, snap["session_status"])


if __name__ == "__main__":
    unittest.main()
