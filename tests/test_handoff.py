"""autofish_handoff 测试：快照读取 + phase→动作翻译 + 新鲜度边界。"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.dirname(HERE)
if BOT not in sys.path:
    sys.path.insert(0, BOT)

import autofish_handoff as hf  # noqa: E402


def snap(phase, ts, ready=True, **extra):
    base = {"phase": phase, "ts": ts, "ready": ready, "conn": "a→b",
            "beat": {"step_age": 0.5, "load_age": None}}
    base.update(extra)
    return base


class ReadSnapshotTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "state.json")

    def _write(self, obj):
        import json
        with open(self.p, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def test_missing_file_returns_none(self):
        self.assertIsNone(hf.read_snapshot(path=self.p, now=1000.0))

    def test_fresh_snapshot_ok(self):
        self._write(snap("fighting", 995.0))
        got = hf.read_snapshot(path=self.p, now=1000.0)
        self.assertIsNotNone(got)
        self.assertEqual(got["phase"], "fighting")

    def test_stale_snapshot_returns_none(self):
        self._write(snap("fighting", 980.0))   # 20s 前 > 10s
        self.assertIsNone(hf.read_snapshot(path=self.p, now=1000.0))

    def test_broken_json_returns_none(self):
        with open(self.p, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertIsNone(hf.read_snapshot(path=self.p, now=1000.0))

    def test_missing_phase_returns_none(self):
        self._write({"ts": 999.0, "ready": True})
        self.assertIsNone(hf.read_snapshot(path=self.p, now=1000.0))


class ActionTests(unittest.TestCase):
    def test_none_snapshot_falls_to_probing(self):
        act = hf.handoff_action(None)
        self.assertEqual(act["enter"], "PROBING")
        self.assertEqual(act["reason"], "no_protocol_snapshot")

    def test_fighting_holds_keys(self):
        act = hf.handoff_action(snap("fighting", 1000.0))
        self.assertEqual(act["enter"], "FIGHT")
        self.assertEqual(act["keys"], "hold")
        self.assertNotIn("release", act["reason"])

    def test_line_in_water_releases(self):
        act = hf.handoff_action(snap("line_in_water", 1000.0))
        self.assertEqual(act["enter"], "WAIT_BITE")
        self.assertEqual(act["keys"], "release")

    def test_casting_taps_f5(self):
        act = hf.handoff_action(snap("casting", 1000.0))
        self.assertEqual(act["keys"], "tap_f5")
        self.assertEqual(act["enter"], "PROBING")

    def test_kept_carries_chain_grant(self):
        act = hf.handoff_action(snap("kept", 1000.0))
        self.assertTrue(act["keep_chain"])
        self.assertEqual(act["enter"], "PROBING")

    def test_ended_and_left_release(self):
        for ph in ("ended", "fish_left"):
            act = hf.handoff_action(snap(ph, 1000.0))
            self.assertEqual(act["keys"], "release")
            self.assertFalse(act["keep_chain"])

    def test_unknown_releases_and_reports(self):
        act = hf.handoff_action(snap("unknown", 1000.0))
        self.assertEqual(act["reason"], "sniff_unknown")
        self.assertEqual(act["keys"], "release")

    def test_unknown_phase_downgrades(self):
        act = hf.handoff_action(snap("unknown", 1000.0, ready=False))
        self.assertEqual(act["enter"], "PROBING")
        self.assertEqual(act["reason"], "sniff_unknown")

    def test_c2s_only_ready_still_trusts_phase(self):
        # S2C 解不开（ready=False）但 phase 来自真实 C2S 事件：必须信 phase，
        # 否则 fighting 会被降级成视觉兜底（S2C 一直 s2c_bad_magic）。
        act = hf.handoff_action(snap("fighting", 1000.0, ready=False))
        self.assertEqual(act["enter"], "FIGHT")
        self.assertEqual(act["keys"], "hold")

    def test_stale_events_downgrade(self):
        # 三个年龄都过期 = 连接其实没了
        act = hf.handoff_action(snap("fighting", 1000.0, event_age=120.0,
                                     beat={"step_age": 118.0, "load_age": 90.0}))
        self.assertEqual(act["enter"], "PROBING")
        self.assertIn("stale", act["reason"])

    def test_fresh_beat_overrides_stale_event_age(self):
        # 搏斗中 0e07 停了但 0e08 每 2s 在流：event_age 可能很大，
        # 取三者最小值 → 仍然活着，必须继续 fighting。
        act = hf.handoff_action(snap("fighting", 1000.0, event_age=300.0,
                                     beat={"step_age": 120.0, "load_age": 1.0}))
        self.assertEqual(act["enter"], "FIGHT")

    def test_fresh_events_ok(self):
        act = hf.handoff_action(snap("fighting", 1000.0, event_age=1.2))
        self.assertEqual(act["enter"], "FIGHT")


class ResolveTests(unittest.TestCase):
    def test_resolve_wires_both(self):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "state.json")
        import json
        with open(p, "w", encoding="utf-8") as f:
            json.dump(snap("fighting", time_now() - 1.0), f)
        act = hf.resolve(now=time_now(), path=p)
        self.assertEqual(act["enter"], "FIGHT")


def time_now():
    import time
    return time.time()


if __name__ == "__main__":
    unittest.main()
