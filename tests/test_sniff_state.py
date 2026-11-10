"""rf4_sniff 状态机测试：半途接手的 phase 判定必须离线可测。

背景（2026-11-09）：视觉判据歧义导致连修四个 bug，引入协议状态机后，
bot 半途启动的第一件事就是读 _sniff/state.json 按 phase 接手。
判定逻辑错了 = 接手动作错 = 跑鱼或空按，所以这里把判定表全量铺开。
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
if BOT not in sys.path:
    sys.path.insert(0, BOT)

from rf4_sniff import (  # noqa: E402
    ProtocolState, parse_load_floats, LOAD_DTO_MAGIC, LOAD_FLOATS,
    STEP_STALE, LOAD_STALE,
)


class PhaseTests(unittest.TestCase):
    def setUp(self):
        self.s = ProtocolState()
        self.t0 = 1000.0

    def feed(self, seq, t=None):
        """seq 形如 "cast hooked end"，依次喂事件。"""
        t = self.t0 if t is None else t
        for i, name in enumerate(seq.split()):
            self.s.on_event(name, t + i * 0.1, b"")

    def test_no_events_is_unknown(self):
        self.assertEqual(self.s.phase(self.t0), "unknown")

    def test_step_alive_no_hooked_is_line_in_water(self):
        self.s.on_event("cast", self.t0, b"")
        self.s.on_event("step", self.t0 + 1.0, b"")
        self.assertEqual(self.s.phase(self.t0 + 1.5), "line_in_water")

    def test_step_alive_with_hooked_is_fighting(self):
        self.s.on_event("cast", self.t0, b"")
        self.s.on_event("hooked", self.t0 + 0.5, b"")
        self.s.on_event("step", self.t0 + 1.0, b"")
        self.assertEqual(self.s.phase(self.t0 + 1.5), "fighting")

    def test_load_alive_alone_is_enough_for_fighting(self):
        """0e08 在流（即使 0e07 停了）也是搏斗中——收线时 0e07 会停。"""
        self.s.on_event("cast", self.t0, b"")
        self.s.on_event("hooked", self.t0 + 0.5, b"")
        self.s.on_event("load", self.t0 + 2.0, b"")
        self.assertEqual(self.s.phase(self.t0 + 2.5), "fighting")

    def test_cast_prepare_without_cast_is_casting(self):
        self.s.on_event("cast_prepare", self.t0, b"")
        self.assertEqual(self.s.phase(self.t0 + 0.2), "casting")

    def test_stale_step_with_end_is_ended(self):
        self.feed("cast hooked end")
        # 最后 step 在 end 之前；超时后应判 ended
        self.assertEqual(self.s.phase(self.t0 + 10.0), "ended")

    def test_stale_step_with_keep_fish_is_kept(self):
        self.feed("cast hooked end keep_fish")
        self.assertEqual(self.s.phase(self.t0 + 10.0), "kept")

    def test_stale_step_with_left_is_fish_left(self):
        self.feed("cast hooked left")
        self.assertEqual(self.s.phase(self.t0 + 10.0), "fish_left")

    def test_hooked_recent_without_beats_gets_grace(self):
        """0e0b 刚到、持续态还没来得及出现：短暂宽限按 fighting。"""
        self.s.on_event("cast", self.t0, b"")
        self.s.on_event("hooked", self.t0 + 1.0, b"")
        self.assertEqual(self.s.phase(self.t0 + 1.5), "fighting")

    def test_hooked_long_no_beats_is_ended(self):
        """0e0b 后很久什么都没有：当 ended（异常，但比 unknown 有信息）。"""
        self.s.on_event("cast", self.t0, b"")
        self.s.on_event("hooked", self.t0 + 1.0, b"")
        self.assertEqual(self.s.phase(self.t0 + 30.0), "ended")

    def test_new_cast_clears_previous_bout_edges(self):
        """新一竿必须清掉上一竿的 end/keep_fish，否则 phase 会卡在 kept。"""
        self.feed("cast hooked end keep_fish")
        self.s.on_event("cast", self.t0 + 5.0, b"")
        self.s.on_event("step", self.t0 + 6.0, b"")
        self.assertEqual(self.s.phase(self.t0 + 6.5), "line_in_water")

    def test_keep_fish_beats_fresh_load(self):
        """收鱼后 0e05 比 bot 点击晚到：残留 0e08 不能让 phase 回到 fighting。"""
        self.s.on_event("cast", self.t0, b"")
        self.s.on_event("hooked", self.t0 + 1.0, b"")
        self.s.on_event("load", self.t0 + 2.0, b"")
        self.s.on_event("end", self.t0 + 2.5, b"")
        self.s.on_event("keep_fish", self.t0 + 3.0, b"")
        self.assertEqual(self.s.phase(self.t0 + 3.2), "kept")

    def test_end_beats_fresh_step(self):
        self.s.on_event("cast", self.t0, b"")
        self.s.on_event("hooked", self.t0 + 1.0, b"")
        self.s.on_event("step", self.t0 + 2.0, b"")
        self.s.on_event("end", self.t0 + 2.2, b"")
        self.assertEqual(self.s.phase(self.t0 + 2.4), "ended")

    def test_bite_alone_is_line_in_water(self):
        self.s.on_event("cast", self.t0, b"")
        self.s.on_event("bite", self.t0 + 3.0, b"")
        self.s.on_event("step", self.t0 + 3.2, b"")
        self.assertEqual(self.s.phase(self.t0 + 3.5), "line_in_water")

    def test_fall_alone_is_line_in_water(self):
        self.s.on_event("cast", self.t0, b"")
        self.s.on_event("fall", self.t0 + 1.5, b"")
        self.s.on_event("step", self.t0 + 2.0, b"")
        self.assertEqual(self.s.phase(self.t0 + 2.4), "line_in_water")

    def test_step_age_boundary(self):
        self.s.on_event("cast", self.t0, b"")
        self.s.on_event("step", self.t0, b"")
        # 恰好在 STEP_STALE 边界内
        self.assertEqual(self.s.phase(self.t0 + STEP_STALE - 0.01), "line_in_water")
        # 超过但最后边沿是 cast：维持 line_in_water（安全降级——
        # 空竿收回也会发 0e04，step 停了却没有 0e04 属罕见；
        # 按"线在水里"接手，retrieve 前有 _line_on_spool 防呆兜底）
        self.assertEqual(self.s.phase(self.t0 + STEP_STALE + 0.5), "line_in_water")


class LoadFloatTests(unittest.TestCase):
    def _make(self, floats):
        body = LOAD_DTO_MAGIC + b"".join(
            struct_pack_f(v) for v in floats)
        return body

    def test_parse_ok(self):
        vals = [float(i) for i in range(27)]
        vals[3] = 12.5      # bout_elapsed
        vals[19] = 3.7      # line_out
        vals[25] = 11008.0  # session_clock
        plain = self._make(vals)
        got = parse_load_floats(plain)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got["bout_elapsed"], 12.5)
        self.assertAlmostEqual(got["line_out"], 3.7)
        self.assertAlmostEqual(got["session_clock"], 11008.0)

    def test_short_payload_returns_none(self):
        vals = [1.0] * 5
        self.assertIsNone(parse_load_floats(self._make(vals)))

    def test_no_magic_returns_none(self):
        self.assertIsNone(parse_load_floats(b"\x00" * 200))

    def test_real_frame_layout(self):
        """按文档 §10.2 的真实布局（type:2 + RPC 字符串 + DTO 头 + floats）。"""
        vals = [float(i) for i in range(27)]
        vals[3] = 14.49
        plain = (b"\x01\xff\xff\xff\xff" + b"\x00" * 4 +
                 b"\x0e\x08" + b"524" + b"\x03\x00" +
                 b"\x0c" + b"\xAA" * 8 +
                 LOAD_DTO_MAGIC + b"".join(struct_pack_f(v) for v in vals))
        got = parse_load_floats(plain)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got["bout_elapsed"], 14.49, places=4)


def struct_pack_f(v):
    import struct
    return struct.pack("<f", v)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_fields(self):
        s = ProtocolState()
        s.on_event("cast", 1000.0, b"")
        s.on_event("step", 1001.0, b"")
        snap = s.snapshot(1001.5, "a→b", True)
        self.assertEqual(snap["phase"], "line_in_water")
        self.assertEqual(snap["conn"], "a→b")
        self.assertTrue(snap["ready"])
        self.assertAlmostEqual(snap["beat"]["step_age"], 0.5)
        self.assertIn("cast", snap["last_edge"])
        self.assertEqual(snap["events_total"], 2)


if __name__ == "__main__":
    unittest.main()
