"""摩擦片观察窗判据的离线测试（不导入适配器、不抓屏、不按键）。"""

from __future__ import annotations

import pathlib
import sys
import unittest

BOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT))

import autofish_friction_window as fw  # noqa: E402


LOW = 2      # 1 档（黄绿）
TARGET = 3   # 2 档（橙/琥珀）
RED = 4      # 3 档（红）


def feed(samples, state, t, level, cfg=None):
    samples.append((t, level))
    return fw.evaluate(samples, state, t, cfg)


def feed_range(samples, state, start, end, step=0.5, level=LOW, cfg=None):
    """按正常采样节奏喂样本，返回最后一次决策。"""
    t = start
    decision = None
    while t <= end + 1e-9:
        decision = feed(samples, state, t, level, cfg)
        t = round(t + step, 6)
    return decision


class FirstObservationTests(unittest.TestCase):
    def test_old_calibration_plus_three_frames_does_not_rise(self):
        """校准 600 秒前 + 中鱼后仅 3 帧低：绝不能 2 秒升档。"""
        samples: list[tuple[float, int]] = []
        state = fw.FrictionObservationState()
        # 校准时刻不参与：状态里根本没有旧时间，只有首次真实读数。
        for t in (0.0, 0.5, 1.0):
            decision = feed(samples, state, t, LOW)
        self.assertIsNone(decision)
        self.assertEqual(state.observe_since, 0.0)
        self.assertEqual(state.last_action, None)

    def test_first_rise_needs_ten_seconds(self):
        samples: list[tuple[float, int]] = []
        state = fw.FrictionObservationState()
        self.assertIsNone(feed_range(samples, state, 0.0, 9.9))
        self.assertEqual(
            feed(samples, state, 10.1, LOW).action, fw.ACTION_RISE
        )

    def test_zero_run_never_starts_observation(self):
        samples: list[tuple[float, int]] = []
        state = fw.FrictionObservationState()
        self.assertIsNone(feed_range(samples, state, 0.0, 20.0, level=0))
        self.assertIsNone(state.observe_since)

    def test_current_zero_does_not_rise(self):
        samples: list[tuple[float, int]] = []
        state = fw.FrictionObservationState()
        feed_range(samples, state, 0.0, 10.0)
        # 观察已满 10 秒，但当前帧 level==0（没读到/鱼在歇）：不升。
        self.assertIsNone(feed(samples, state, 10.5, 0))
        self.assertEqual(state.observe_since, 0.0)


class SlidingWindowTrapTests(unittest.TestCase):
    def test_first_sample_evicted_still_rises(self):
        """win 裁到 10 秒后首帧被淘汰，持久 epoch 仍能触发。"""
        samples: list[tuple[float, int]] = []
        state = fw.FrictionObservationState()
        decision = feed_range(samples, state, 0.0, 10.5)
        self.assertGreater(samples[0][0], 0.0)  # 首帧确实已被淘汰
        self.assertEqual(decision.action, fw.ACTION_RISE)

    def test_observe_since_does_not_follow_window_head(self):
        samples: list[tuple[float, int]] = []
        state = fw.FrictionObservationState()
        feed_range(samples, state, 0.0, 10.0)
        self.assertEqual(state.observe_since, 0.0)
        self.assertEqual(
            feed(samples, state, 10.5, LOW).action, fw.ACTION_RISE
        )
        self.assertGreater(samples[0][0], 0.0)
        self.assertEqual(state.observe_since, 0.0)

class GapTests(unittest.TestCase):
    def test_long_gap_restarts_observation(self):
        samples: list[tuple[float, int]] = []
        state = fw.FrictionObservationState()
        feed_range(samples, state, 0.0, 1.0)
        # 4 秒没有有效读数：旧观察作废，从 5.0 重新累计。
        self.assertIsNone(feed(samples, state, 5.0, LOW))
        self.assertEqual(state.observe_since, 5.0)
        self.assertEqual(samples, [(5.0, LOW)])
        self.assertIsNone(feed_range(samples, state, 5.5, 14.9))
        self.assertEqual(
            feed(samples, state, 15.1, LOW).action, fw.ACTION_RISE
        )


class RiseGapTests(unittest.TestCase):
    def _first_rise(self):
        samples: list[tuple[float, int]] = []
        state = fw.FrictionObservationState()
        decision = feed_range(samples, state, 0.0, 10.1)
        self.assertEqual(decision.action, fw.ACTION_RISE)
        fw.commit(state, decision, 10.1)
        samples.clear()
        return samples, state

    def test_rise_waits_five_seconds_after_rise(self):
        samples, state = self._first_rise()
        self.assertIsNone(feed_range(samples, state, 10.6, 14.9))
        self.assertEqual(
            feed(samples, state, 15.1, LOW).action, fw.ACTION_RISE
        )

    def test_cross_fish_reset_waits_full_ten_again(self):
        samples, state = self._first_rise()
        state.reset(keep_last_action=False)
        self.assertIsNone(state.last_action)
        self.assertIsNone(state.observe_since)
        self.assertIsNone(feed_range(samples, state, 20.0, 29.9))
        self.assertEqual(
            feed(samples, state, 30.1, LOW).action, fw.ACTION_RISE
        )

    def test_pause_keeps_last_action_for_resume_wait(self):
        samples, state = self._first_rise()
        state.reset(keep_last_action=True)
        self.assertEqual(state.last_action, fw.ACTION_RISE)
        self.assertIsNone(state.observe_since)
        samples.clear()
        self.assertIsNone(feed_range(samples, state, 20.0, 24.9))
        self.assertEqual(
            feed(samples, state, 25.1, LOW).action, fw.ACTION_RISE
        )


class DropTests(unittest.TestCase):
    def test_red_tail_drops_immediately(self):
        samples: list[tuple[float, int]] = []
        state = fw.FrictionObservationState()
        for t in (0.0, 0.5, 1.0):
            decision = feed(samples, state, t, RED)
        self.assertEqual(decision.action, fw.ACTION_DROP)
        self.assertLess(decision.observed, 1.1)  # 不等 10 秒

    def test_drop_respects_six_second_cooldown(self):
        samples: list[tuple[float, int]] = []
        state = fw.FrictionObservationState()
        for t in (0.0, 0.5, 1.0):
            decision = feed(samples, state, t, RED)
        fw.commit(state, decision, 1.0)
        samples.clear()
        # 4 秒后仍然红：冷却未到，不重复降。
        self.assertIsNone(feed_range(samples, state, 4.0, 5.0, level=RED))
        self.assertIsNone(feed_range(samples, state, 5.5, 6.9, level=RED))
        # 6 秒冷却后用连续红尾确认新事件。
        self.assertEqual(
            feed(samples, state, 7.0, RED).action, fw.ACTION_DROP
        )

    def test_rise_after_drop_waits_ten_seconds(self):
        samples: list[tuple[float, int]] = []
        state = fw.FrictionObservationState()
        for t in (0.0, 0.5, 1.0):
            decision = feed(samples, state, t, RED)
        fw.commit(state, decision, 1.0)
        samples.clear()
        self.assertIsNone(feed_range(samples, state, 1.5, 10.9))
        self.assertEqual(
            feed(samples, state, 11.1, LOW).action, fw.ACTION_RISE
        )


class TargetTierTests(unittest.TestCase):
    """橙色（目标档）不再"一帧作废整窗"，改为按比例容忍（默认 30%）。"""

    CFG = fw.FrictionWindowConfig(lock=0.0, first_observe=4.0)

    def _state(self):
        # 观察纪元已经跑够时间，直接考察"橙帧比例"这一条
        return fw.FrictionObservationState(observe_since=0.0, last_valid_ts=4.5)

    def test_minority_orange_still_allows_rise(self):
        # 11 帧里 1 帧橙（9%）→ 允许升档（大鱼常态压在 2 档）
        samples = [(i * 0.5, LOW) for i in range(11)]
        samples[5] = (2.5, TARGET)
        decision = fw.evaluate(samples, self._state(), 5.0, self.CFG)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, fw.ACTION_RISE)

    def test_majority_orange_still_blocks(self):
        # 5 帧里 3 帧橙（60%）→ 阻断，避免把泄力一路打紧
        samples = [(0.0, TARGET), (0.5, LOW), (1.0, TARGET),
                   (1.5, LOW), (2.0, TARGET)]
        decision = fw.evaluate(samples, self._state(), 2.0, self.CFG)
        self.assertIsNone(decision)


class CommitTests(unittest.TestCase):
    def test_failed_adjustment_does_not_advance_epoch(self):
        samples: list[tuple[float, int]] = []
        state = fw.FrictionObservationState()
        decision = feed_range(samples, state, 0.0, 10.1)
        self.assertEqual(decision.action, fw.ACTION_RISE)
        # 模拟滚轮失败：不调用 commit、不清窗口。
        self.assertEqual(state.observe_since, 0.0)
        self.assertIsNone(state.last_action)
        later = feed(samples, state, 10.6, LOW)
        self.assertEqual(later.action, fw.ACTION_RISE)
        self.assertEqual(state.observe_since, 0.0)

    def test_commit_starts_new_epoch(self):
        samples: list[tuple[float, int]] = []
        state = fw.FrictionObservationState()
        decision = feed_range(samples, state, 0.0, 10.1)
        fw.commit(state, decision, 10.1)
        self.assertEqual(state.last_action, fw.ACTION_RISE)
        self.assertEqual(state.last_adjust, 10.1)
        self.assertEqual(state.observe_since, 10.1)
        self.assertIsNone(state.last_valid_ts)


if __name__ == "__main__":
    unittest.main()
