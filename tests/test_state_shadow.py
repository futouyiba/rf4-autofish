"""autofish_state 的纯逻辑测试（不截图、不启动游戏、不发送输入）。

运行：python -m unittest tests.test_state_shadow -v
或在 bot 目录：python -m unittest discover -s tests -p "test_state_shadow.py" -v
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import autofish_observation as aobs  # noqa: E402
import autofish_state as astate  # noqa: E402

T = aobs.TriState
S = astate.GameState


def mk(
    *,
    fish=T.FALSE,
    bar_level=None,
    bar_tier=None,
    ring=None,
    captured=T.FALSE,
    ok_modal=T.FALSE,
    hud=T.TRUE,
    foreground=True,
    ts=0.0,
) -> aobs.Observation:
    """造一条观测。默认值刻意是"有 HUD 但没有任何状态证据" -> 候选 UNKNOWN。"""
    return aobs.Observation(
        monotonic_ts=ts,
        wall_ts=ts,
        foreground=foreground,
        fish_icon=fish,
        bar_level=bar_level,
        bar_tier=bar_tier,
        bar_hue=None,
        bar_sat=None,
        ring_ratio=ring,
        captured=captured,
        ok_modal=ok_modal,
        hud_visible=hud,
        evidence={},
    )


def feed(tracker, n, **kw):
    est = None
    for _ in range(n):
        est = tracker.update(mk(**kw))
    return est


class TestClassifyCandidate(unittest.TestCase):
    def test_modal_wins_over_everything(self):
        cand, reason = astate.classify_candidate(
            mk(ok_modal=T.TRUE, captured=T.TRUE, fish=T.TRUE, ring=0.99))
        self.assertIs(cand, S.MODAL)
        self.assertEqual(reason, "ok_modal_true")

    def test_settle_wins_over_fish(self):
        cand, _ = astate.classify_candidate(
            mk(captured=T.TRUE, fish=T.TRUE, ring=0.99))
        self.assertIs(cand, S.SETTLE)

    def test_ring_none_is_fight_never_land(self):
        cand, _ = astate.classify_candidate(mk(fish=T.TRUE, ring=None))
        self.assertIs(cand, S.FIGHT)

    def test_cold_level_one_is_not_fight(self):
        """level=1 / tier=None 的冷色带不构成搏鱼证据。"""
        cand, _ = astate.classify_candidate(
            mk(fish=T.FALSE, bar_level=1, bar_tier=None, ring=0.40))
        self.assertIs(cand, S.WAIT_BITE)       # 圆环可读 -> 只是待咬钩
        cand, _ = astate.classify_candidate(
            mk(fish=T.FALSE, bar_level=1, bar_tier=None, ring=None))
        self.assertIs(cand, S.UNKNOWN)         # 冷色带不许升级成 FIGHT

    def test_ready_is_not_inferred(self):
        """HUD 可见、无鱼、无档、无环 —— 不猜 READY。"""
        cand, reason = astate.classify_candidate(mk(hud=T.TRUE))
        self.assertIs(cand, S.UNKNOWN)
        self.assertIn("action_context", reason)

    def test_hud_false_is_not_menu(self):
        cand, reason = astate.classify_candidate(mk(hud=T.FALSE))
        self.assertIs(cand, S.UNKNOWN)
        self.assertIn("template", reason)


class TestDebounce(unittest.TestCase):
    def test_fish_two_frames_to_fight(self):
        t = astate.ShadowStateTracker()
        est = t.update(mk(fish=T.TRUE))
        self.assertIs(est.state, S.UNKNOWN)    # 单帧不切
        self.assertFalse(est.changed)
        est = t.update(mk(fish=T.TRUE))
        self.assertIs(est.state, S.FIGHT)
        self.assertTrue(est.changed)
        self.assertTrue(0.0 < est.confidence <= 1.0)
        self.assertEqual(est.stable_frames, 2)

    def test_ring_full_three_frames_to_land(self):
        t = astate.ShadowStateTracker()
        for n in (1, 2):
            est = t.update(mk(fish=T.TRUE, ring=0.91))
            self.assertIs(est.state, S.UNKNOWN, f"第 {n} 帧不得判 LAND")
        est = t.update(mk(fish=T.TRUE, ring=0.91))
        self.assertIs(est.state, S.LAND)
        self.assertTrue(est.changed)

    def test_ring_none_with_fish_is_fight(self):
        t = astate.ShadowStateTracker()
        est = feed(t, 3, fish=T.TRUE, ring=None)
        self.assertIs(est.state, S.FIGHT)
        self.assertIsNot(est.state, S.LAND)

    def test_bar_tier2_without_fish_is_fight(self):
        t = astate.ShadowStateTracker()
        est = feed(t, 2, fish=T.FALSE, bar_level=3, bar_tier=2, ring=None)
        self.assertIs(est.state, S.FIGHT)
        self.assertEqual(est.evidence["candidate"], "fight")

    def test_idle_bar_with_hud_is_wait_bite(self):
        t = astate.ShadowStateTracker()
        for n in (1, 2):
            est = t.update(mk(fish=T.FALSE, bar_level=0, bar_tier=None,
                              ring=0.42))
            self.assertIs(est.state, S.UNKNOWN, f"第 {n} 帧不得判 WAIT_BITE")
        est = t.update(mk(fish=T.FALSE, bar_level=0, bar_tier=None, ring=0.42))
        self.assertIs(est.state, S.WAIT_BITE)
        self.assertLessEqual(est.confidence, 0.55)

    def test_modal_single_frame_holds_then_two_frames_interrupt(self):
        t = astate.ShadowStateTracker()
        feed(t, 2, fish=T.TRUE)                 # 先稳定在 FIGHT
        est = t.update(mk(ok_modal=T.TRUE))
        self.assertIs(est.state, S.FIGHT)
        self.assertFalse(est.changed)
        self.assertEqual(est.evidence["pending"]["state"], "modal")
        est = t.update(mk(ok_modal=T.TRUE))
        self.assertIs(est.state, S.MODAL)
        self.assertTrue(est.changed)

    def test_captured_single_frame_holds_then_two_frames_interrupt(self):
        t = astate.ShadowStateTracker()
        feed(t, 2, fish=T.TRUE)
        est = t.update(mk(captured=T.TRUE, hud=T.UNKNOWN))
        self.assertIs(est.state, S.FIGHT)
        est = t.update(mk(captured=T.TRUE, hud=T.UNKNOWN))
        self.assertIs(est.state, S.SETTLE)
        self.assertTrue(est.changed)
        # 几何判据证据少 -> 置信度必须打折
        self.assertLess(est.confidence, 1.0)

    def test_one_unknown_frame_does_not_break_fight(self):
        t = astate.ShadowStateTracker()
        feed(t, 2, fish=T.TRUE)                 # FIGHT
        est = t.update(mk())                    # 一帧 HUD 遮挡/侧光
        self.assertIs(est.state, S.FIGHT)
        self.assertFalse(est.changed)
        self.assertEqual(est.evidence["pending"]["state"], "unknown")

    def test_five_unknown_frames_needed_to_replace_fight(self):
        t = astate.ShadowStateTracker()
        feed(t, 2, fish=T.TRUE)
        t.update(mk())                          # 单帧 UNKNOWN
        t.update(mk(fish=T.TRUE))               # 计数复位
        for n in range(1, 5):
            est = t.update(mk())
            self.assertIs(est.state, S.FIGHT, f"第 {n} 帧 UNKNOWN 不得切换")
        est = t.update(mk())
        self.assertIs(est.state, S.UNKNOWN)
        self.assertTrue(est.changed)
        self.assertEqual(est.stable_frames, 5)

    def test_fight_holds_two_land_candidates_then_lands(self):
        t = astate.ShadowStateTracker()
        feed(t, 2, fish=T.TRUE)                 # 稳定 FIGHT
        for n in (1, 2):
            est = t.update(mk(fish=T.TRUE, ring=0.93))
            self.assertIs(est.state, S.FIGHT, f"LAND 候选第 {n} 帧不得切换")
            self.assertIn("pending land", est.reason)
            self.assertEqual(est.evidence["pending"]["need"], 3)
        est = t.update(mk(fish=T.TRUE, ring=0.93))
        self.assertIs(est.state, S.LAND)
        self.assertTrue(est.changed)

    def test_stable_frames_not_incremented_while_other_candidate_pending(self):
        """stable_frames = 当前稳定状态被候选连续支持的帧数；pending 帧不算支持。"""
        t = astate.ShadowStateTracker()
        est = feed(t, 2, fish=T.TRUE)           # 2 帧 FIGHT -> 稳定
        self.assertIs(est.state, S.FIGHT)
        self.assertEqual(est.stable_frames, 2)

        for n in (1, 2):                        # 两帧 LAND 候选（need=3）不足以切换
            est = t.update(mk(fish=T.TRUE, ring=0.93))
            self.assertIs(est.state, S.FIGHT, f"第 {n} 帧不得切换")
            self.assertEqual(est.stable_frames, 2,
                             f"第 {n} 帧 pending 时 stable_frames 不得增加")

        est = t.update(mk(fish=T.TRUE, ring=0.93))   # 第三帧确认切 LAND
        self.assertIs(est.state, S.LAND)
        self.assertTrue(est.changed)
        self.assertEqual(est.stable_frames, 3)

    def test_hud_false_never_ready_or_menu(self):
        t = astate.ShadowStateTracker()
        for _ in range(10):
            est = t.update(mk(hud=T.FALSE))
            self.assertIs(est.state, S.UNKNOWN)
            self.assertNotEqual(est.state.value, "ready")
            self.assertNotEqual(est.state.value, "menu")
            self.assertIn("needs", est.reason)

    def test_tracker_is_deterministic_and_keeps_history(self):
        t = astate.ShadowStateTracker()
        feed(t, 3, fish=T.TRUE)
        self.assertEqual(len(t.observations), 3)
        self.assertIs(t.state, S.FIGHT)
        # 重复喂同样的帧不会让置信度越界
        est = feed(t, 50, fish=T.TRUE)
        self.assertIs(est.state, S.FIGHT)
        self.assertLessEqual(est.confidence, 1.0)


class TestNoInputSurface(unittest.TestCase):
    """源码级检查：状态层只吃 Observation，不许出现输入/抓帧入口。"""

    FORBIDDEN = (
        "pydirectinput", "ctypes", "win32", "SendInput", "mouse_event",
        "keyDown", "keyUp", "ImageGrab", "screenshot", "mss", "capture_game",
    )

    def test_source_has_no_input_or_capture(self):
        src = pathlib.Path(astate.__file__).read_text(encoding="utf-8")
        for token in self.FORBIDDEN:
            self.assertNotIn(token, src, f"状态层源码出现禁用符号: {token}")

    def test_source_does_not_import_adapter(self):
        src = pathlib.Path(astate.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import rf4s_adapter", src)
        self.assertNotIn("from rf4s_adapter", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
