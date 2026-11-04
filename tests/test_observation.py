"""autofish_observation 的合成图测试（不截图、不碰游戏窗口）。

运行：python -m unittest discover -s tests -p "test_observation.py" -v
也可以直接：python -m unittest tests.test_observation -v

所有画面都是 NumPy/Pillow 现画的 1920x1080 黑底图，判据阈值来自
rf4s_adapter.py 的实测常量。
"""

from __future__ import annotations

import dataclasses
import math
import pathlib
import sys
import unittest

import numpy as np
from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import autofish_observation as obs  # noqa: E402

W, H = 1920, 1080


# ---------------------------------------------------------------- 造图工具


def canvas(color=(0, 0, 0)) -> Image.Image:
    a = np.zeros((H, W, 3), np.uint8)
    a[:, :] = color
    return Image.fromarray(a, "RGB")


def filled_rect(color, x0, x1, y0, y1, base=None) -> Image.Image:
    a = np.array(base if base is not None else canvas(), dtype=np.uint8)
    a[y0:y1, x0:x1] = color
    return Image.fromarray(a, "RGB")


def draw_bar_band(color, width=200, base=None) -> Image.Image:
    """画一条"轨道 + 彩色带"的力度条：轨道全灰，带子从 x=600 起。"""
    a = np.array(base if base is not None else canvas(), dtype=np.uint8)
    y1, y2, x1, x2 = obs.FRICTION_BAR
    a[y1:y2, x1:x2] = (120, 120, 120)              # 灰色轨道（S=0，不算带子）
    a[y1:y2, 600:600 + width] = color              # 高饱和彩色带
    return Image.fromarray(a, "RGB")


def draw_ring(degrees, base=None) -> Image.Image:
    """在圆环位置按角度画一圈亮线（半径 12..16 保证采样点命中）。"""
    a = np.array(base if base is not None else canvas((60, 60, 60)),
                 dtype=np.uint8)
    cx, cy = obs.LINE_RING_CENTER
    for deg in degrees:
        rad = math.radians(deg)
        for r in range(11, 17):
            x = int(round(cx + r * math.cos(rad)))
            y = int(round(cy + r * math.sin(rad)))
            a[y, x] = (255, 255, 255)
    return Image.fromarray(a, "RGB")


# ---------------------------------------------------------------- 圆环


class TestRing(unittest.TestCase):
    def test_unreadable_returns_none_not_minus_one(self):
        ratio, ev = obs.detect_ring(canvas())          # 全黑：HUD 没画
        self.assertIsNone(ratio)
        self.assertEqual(ev["reason"], "hud_dark")

    def test_icon_missing_returns_none(self):
        ratio, ev = obs.detect_ring(canvas((60, 60, 60)))  # 均匀灰：图标没画
        self.assertIsNone(ratio)
        self.assertEqual(ev["reason"], "icon_not_drawn")

    def test_half_ring(self):
        ratio, ev = obs.detect_ring(draw_ring(range(0, 180)))
        self.assertIsNotNone(ratio)
        self.assertAlmostEqual(ratio, 0.5, delta=0.08)

    def test_full_ring(self):
        ratio, _ = obs.detect_ring(draw_ring(range(0, 360)))
        self.assertIsNotNone(ratio)
        self.assertGreaterEqual(ratio, 0.95)

    def test_small_image_is_unknown(self):
        ratio, ev = obs.detect_ring(Image.new("RGB", (200, 100)))
        self.assertIsNone(ratio)
        self.assertEqual(ev["reason"], "non_reference_frame")


# ---------------------------------------------------------------- 力度条


class TestBar(unittest.TestCase):
    # 玩家指认的三个实测色（rf4s_adapter.py 2026-11-04 12:49 标定）
    HUE33 = (200, 214, 63)      # 1 档（力最小）
    HUE25 = (229, 188, 0)       # 2 档（目标）
    HUE6 = (214, 70, 34)        # 3 档（红）

    def test_hue_ladder(self):
        for color, hue, level, tier in (
            (self.HUE33, 33, 2, 1),
            (self.HUE25, 25, 3, 2),
            (self.HUE6, 6, 4, 3),
        ):
            with self.subTest(hue=hue):
                reading = obs.detect_bar(draw_bar_band(color))
                self.assertEqual(reading.hue, hue)
                self.assertEqual(reading.level, level)
                self.assertEqual(reading.tier, tier)
                self.assertIsNotNone(reading.sat)

    def test_no_band_is_level_zero_and_tier_none(self):
        a = np.array(canvas(), dtype=np.uint8)
        y1, y2, x1, x2 = obs.FRICTION_BAR
        a[y1:y2, x1:x2] = (120, 120, 120)          # 只有灰色轨道
        reading = obs.detect_bar(Image.fromarray(a, "RGB"))
        self.assertEqual(reading.level, 0)
        self.assertIsNone(reading.tier)            # 不许伪装成 1 档
        self.assertIsNone(reading.hue)
        self.assertIsNone(reading.sat)
        self.assertTrue(reading.evidence["track_drawn"])

    def test_hud_not_drawn_is_none(self):
        reading = obs.detect_bar(canvas())         # 全黑：轨道也没有
        self.assertIsNone(reading.level)
        self.assertIsNone(reading.tier)
        self.assertFalse(reading.evidence["track_drawn"])

    def test_band_visible_without_track_diff_still_reads(self):
        """带子本身就够证明可读（不需要亮度差）。"""
        a = np.zeros((H, W, 3), np.uint8)
        y1, y2, x1, x2 = obs.FRICTION_BAR
        a[y1:y2, 600:900] = self.HUE25
        reading = obs.detect_bar(Image.fromarray(a, "RGB"))
        self.assertEqual(reading.level, 3)

    def test_thresholds_match_adapter_constants(self):
        self.assertEqual(obs.BAR_SAT_MIN, 130)
        self.assertEqual(obs.BAR_VAL_MIN, 110)
        self.assertEqual(obs.FISH_ICON_WHITE_MIN, 60)
        self.assertEqual(obs.LINE_RING_LIT, 180)
        self.assertEqual(obs.LINE_RING_CENTER, (1237, 1023))

    def test_reading_unpacks_like_a_tuple(self):
        level, tier, hue, sat, ev = obs.detect_bar(
            draw_bar_band(self.HUE25))
        self.assertEqual((level, tier, hue), (3, 2, 25))
        self.assertIsInstance(sat, int)
        self.assertIsInstance(ev, dict)


# ---------------------------------------------------------------- 鱼图标


class TestFishIcon(unittest.TestCase):
    def test_absent_on_blank(self):
        state, ev = obs.detect_fish_icon(canvas())
        self.assertIs(state, obs.TriState.FALSE)
        self.assertEqual(ev["white_pixels"], 0)

    def test_present_when_white_ring_drawn(self):
        a = np.array(canvas(), dtype=np.uint8)
        cx, cy = obs.FISH_ICON_CENTER
        for deg in range(0, 360, 2):
            rad = math.radians(deg)
            for r in range(9, 15):
                x = int(round(cx + r * math.cos(rad)))
                y = int(round(cy + r * math.sin(rad)))
                a[y, x] = (255, 255, 255)
        state, ev = obs.detect_fish_icon(Image.fromarray(a, "RGB"))
        self.assertIs(state, obs.TriState.TRUE)
        self.assertGreaterEqual(ev["white_pixels"], obs.FISH_ICON_WHITE_MIN)


# ---------------------------------------------------------------- 弹窗 / 结算


class TestModalsAndSettlement(unittest.TestCase):
    def test_ok_button_present(self):
        x0, y0, x1, y1 = obs.OK_BTN_BOX
        a = np.array(canvas(), dtype=np.uint8)
        a[y0, x0:x1] = (235, 235, 235)
        a[y1, x0:x1] = (235, 235, 235)
        state, ev = obs.detect_ok_modal(Image.fromarray(a, "RGB"))
        self.assertIs(state, obs.TriState.TRUE)
        self.assertGreater(ev["top_frac"], obs.OK_BTN_FRAC)

    def test_ok_button_absent(self):
        state, _ = obs.detect_ok_modal(canvas())
        self.assertIs(state, obs.TriState.FALSE)

    def test_ok_button_single_border_not_enough(self):
        """单帧只有一条边框 = 本帧不算（防抖在上层，但单帧也不能瞎认）。"""
        x0, y0, x1, _ = obs.OK_BTN_BOX
        a = np.array(canvas(), dtype=np.uint8)
        a[y0, x0:x1] = (235, 235, 235)
        state, _ = obs.detect_ok_modal(Image.fromarray(a, "RGB"))
        self.assertIs(state, obs.TriState.FALSE)

    @staticmethod
    def _settle_frame(width=(711, 1208)):
        xa, xb = width
        a = np.array(canvas(), dtype=np.uint8)
        a[941:943, xa:xb] = (235, 235, 235)
        a[1007:1009, xa:xb] = (235, 235, 235)
        mid = (xa + xb) // 2
        for x in (xa, mid, xb - 1):
            a[941:1009, x:x + 2] = (235, 235, 235)
        return Image.fromarray(a, "RGB")

    def test_settlement_detected(self):
        state, ev = obs.detect_captured(self._settle_frame())
        self.assertIs(state, obs.TriState.TRUE)
        self.assertEqual(ev["button_width"], 497)
        self.assertGreaterEqual(ev["dividers"], 3)
        self.assertIn("keep_btn_center", ev)

    def test_settlement_absent_on_blank(self):
        state, ev = obs.detect_captured(canvas())
        self.assertIs(state, obs.TriState.FALSE)
        self.assertEqual(ev["reason"], "no_border_pair")

    def test_wide_pair_rejected(self):
        """弹窗/待抛竿那种横贯整个扫描区的边框对，必须拒掉。"""
        state, ev = obs.detect_captured(self._settle_frame(width=(603, 1319)))
        self.assertIs(state, obs.TriState.FALSE)
        self.assertEqual(ev["reason"], "geometry_reject")


# ---------------------------------------------------------------- HUD


class TestHud(unittest.TestCase):
    def test_dark_frame_without_cues_is_false(self):
        state, ev = obs.detect_hud(canvas((25, 25, 25)))
        self.assertIs(state, obs.TriState.FALSE)
        self.assertEqual(ev["reason"], "all_cues_absent_and_dark")

    def test_ring_drawn_is_true(self):
        state, ev = obs.detect_hud(draw_ring(range(0, 360)))
        self.assertIs(state, obs.TriState.TRUE)
        self.assertIn("ring", ev["positives"])

    def test_bar_track_only_is_true(self):
        a = np.array(canvas(), dtype=np.uint8)
        y1, y2, x1, x2 = obs.FRICTION_BAR
        a[y1:y2, x1:x2] = (120, 120, 120)
        state, ev = obs.detect_hud(Image.fromarray(a, "RGB"))
        self.assertIs(state, obs.TriState.TRUE)
        self.assertIn("bar_track", ev["positives"])

    def test_bright_frame_without_cues_is_unknown_not_guess(self):
        """满是侧光但三处 UI 都没有 —— 分不清"没 HUD"还是"被挡住"，不许猜。"""
        state, ev = obs.detect_hud(canvas((150, 150, 150)))
        self.assertIs(state, obs.TriState.UNKNOWN)
        self.assertEqual(ev["reason"], "all_cues_absent_but_bright")


# ---------------------------------------------------------------- Observation


class TestObservation(unittest.TestCase):
    def test_frozen(self):
        o = obs.observe_frame(canvas(), foreground=True)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            o.ring_ratio = 0.5              # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            o.foreground = False            # type: ignore[misc]

    def test_same_frame_semantics(self):
        """一次观测 = 一张图：圆环、力度条、鱼图标都来自传进来的这张。"""
        img = draw_ring(range(0, 360))
        img = draw_bar_band(TestBar.HUE25, base=img)
        o = obs.observe_frame(
            img, foreground=True, monotonic_ts=123.0, wall_ts=456.0)
        self.assertEqual(o.monotonic_ts, 123.0)
        self.assertEqual(o.wall_ts, 456.0)
        self.assertTrue(o.foreground)
        self.assertEqual(o.bar_level, 3)
        self.assertEqual(o.bar_tier, 2)
        self.assertGreaterEqual(o.ring_ratio, 0.95)
        self.assertIs(o.fish_icon, obs.TriState.FALSE)
        self.assertIs(o.hud_visible, obs.TriState.TRUE)
        # 鱼图标没画 -> 不是咬钩，但也不能因此把其它判据一并作废
        self.assertIs(o.captured, obs.TriState.FALSE)
        self.assertIs(o.ok_modal, obs.TriState.FALSE)
        for key in ("frame", "fish_icon", "bar", "ring",
                    "ok_modal", "captured", "hud"):
            self.assertIn(key, o.evidence)

    def test_unreadable_frame_all_unknown(self):
        o = obs.observe_frame(None, foreground=False)
        self.assertIs(o.fish_icon, obs.TriState.UNKNOWN)
        self.assertIsNone(o.bar_level)
        self.assertIsNone(o.bar_tier)
        self.assertIsNone(o.ring_ratio)
        self.assertIs(o.ok_modal, obs.TriState.UNKNOWN)
        self.assertIs(o.captured, obs.TriState.UNKNOWN)
        self.assertIs(o.hud_visible, obs.TriState.UNKNOWN)

    def test_small_image_never_lies(self):
        o = obs.observe_frame(Image.new("RGB", (320, 200)), foreground=True)
        self.assertIs(o.fish_icon, obs.TriState.UNKNOWN)
        self.assertIsNone(o.bar_level)
        self.assertIsNone(o.ring_ratio)
        self.assertIs(o.hud_visible, obs.TriState.UNKNOWN)

    def test_non_reference_resolution_is_unknown(self):
        """1600x900 的客户区不算"读不懂"，算"坐标全错"，必须整体 UNKNOWN。

        这是纯安全边界：错误分辨率下点 (549,1024) 已经不是鱼图标，
        读出"有鱼/无鱼"都只是巧合。
        """
        img = draw_bar_band(TestBar.HUE25).resize((1600, 900))
        o = obs.observe_frame(img, foreground=True)
        self.assertIs(o.fish_icon, obs.TriState.UNKNOWN)
        self.assertIsNone(o.bar_level)
        self.assertIsNone(o.bar_tier)
        self.assertIsNone(o.ring_ratio)
        self.assertIs(o.captured, obs.TriState.UNKNOWN)
        self.assertIs(o.ok_modal, obs.TriState.UNKNOWN)
        self.assertIs(o.hud_visible, obs.TriState.UNKNOWN)
        self.assertEqual(o.evidence["bar"]["reason"], "non_reference_frame")

    def test_as_dict_is_flat_and_json_shaped(self):
        d = obs.observe_frame(canvas(), foreground=True,
                              monotonic_ts=1.0, wall_ts=2.0).as_dict()
        self.assertEqual(d["fish_icon"], "false")
        self.assertEqual(d["hud_visible"], "false")
        self.assertIsInstance(d["evidence"], dict)
        import json
        json.dumps(d)          # 必须可直接落盘

    def test_evidence_is_deeply_frozen(self):
        """evidence 是深冻结快照：顶层和嵌套层都不可写。"""
        o = obs.observe_frame(draw_bar_band(TestBar.HUE25), foreground=True)
        with self.assertRaises(TypeError):
            o.evidence["injected"] = 1                 # type: ignore[index]
        with self.assertRaises(TypeError):
            o.evidence["bar"]["injected"] = 1          # type: ignore[index]
        # 嵌套 list 已变成 tuple，没有 append
        self.assertIsInstance(o.evidence["bar"]["region"], tuple)
        self.assertIsInstance(o.evidence["frame"]["size"], tuple)
        with self.assertRaises(AttributeError):
            o.evidence["frame"]["size"].append(1)      # type: ignore[union-attr]

    def test_evidence_snapshot_survives_source_mutation(self):
        """构造后修改原始 dict/list，不得影响已建好的 observation。"""
        inner = {"thresholds": [1, 2, 3], "reason": "keep"}
        source = {"bar": inner, "foreground": True}
        o = obs.Observation(
            monotonic_ts=1.0,
            wall_ts=2.0,
            foreground=True,
            fish_icon=obs.TriState.FALSE,
            bar_level=0,
            bar_tier=None,
            bar_hue=None,
            bar_sat=None,
            ring_ratio=0.1,
            captured=obs.TriState.FALSE,
            ok_modal=obs.TriState.FALSE,
            hud_visible=obs.TriState.TRUE,
            evidence=source,
        )
        source["foreground"] = False
        source["bar"]["reason"] = "mutated"
        source["bar"]["thresholds"].append(4)
        inner["new_key"] = True

        self.assertTrue(o.evidence["foreground"])
        self.assertEqual(o.evidence["bar"]["reason"], "keep")
        self.assertEqual(o.evidence["bar"]["thresholds"], (1, 2, 3))
        self.assertNotIn("new_key", o.evidence["bar"])

    def test_as_dict_thaws_recursively(self):
        """as_dict() 把 evidence 还原成普通 dict/list，且是独立副本。"""
        o = obs.observe_frame(draw_ring(range(0, 360)), foreground=True)
        d = o.as_dict()
        ev = d["evidence"]
        self.assertIsInstance(ev, dict)
        self.assertIsInstance(ev["frame"], dict)
        self.assertIsInstance(ev["frame"]["size"], list)
        self.assertIsInstance(ev["ring"]["radii"], list)
        self.assertEqual(sorted(ev), sorted(o.evidence))
        # thaw 出来的副本可写，且不会反写回 frozen 快照
        ev["frame"]["size"].append(0)
        self.assertEqual(len(o.evidence["frame"]["size"]), 2)
        # 嵌套值全部是 JSON 原生类型
        import json
        json.dumps(d)


class TestNoInputSurface(unittest.TestCase):
    """源码级检查：观测层不许出现任何输入/截屏入口。"""

    FORBIDDEN = (
        "keyDown", "keyUp", "mouse_event", "SendInput",
        "pydirectinput", "capture_game",
    )

    def test_source_has_no_input_or_capture(self):
        path = pathlib.Path(obs.__file__)
        src = path.read_text(encoding="utf-8")
        for token in self.FORBIDDEN:
            self.assertNotIn(token, src, f"观测层源码出现禁用符号: {token}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
