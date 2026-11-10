"""分地图坐标表的离线测试：不碰游戏、不碰网络。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

BOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT))

import autofish_places as ap  # noqa: E402


class PlacesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._tmp.name) / "places.json"

    def tearDown(self):
        self._tmp.cleanup()

    def store(self) -> ap.Places:
        return ap.Places(self.path)

    # ---- 空表 / 坏文件 ----
    def test_missing_file_is_empty(self):
        s = self.store()
        self.assertEqual(s.map_names(), [])
        self.assertIsNone(s.get("钓位"))
        self.assertIsNone(s.last_error)

    def test_corrupt_file_does_not_crash(self):
        self.path.write_text("{not json", encoding="utf-8")
        s = self.store()
        self.assertEqual(s.map_names(), [])
        self.assertIn("places_unreadable", s.last_error or "")

    def test_bad_root_does_not_crash(self):
        self.path.write_text("[1,2,3]", encoding="utf-8")
        s = self.store()
        self.assertEqual(s.map_names(), [])
        self.assertEqual(s.last_error, "places_bad_root")

    def test_bad_places_are_skipped_not_fatal(self):
        self.path.write_text(json.dumps({
            "version": 2,
            "active_map": "a",
            "maps": {"a": {"places": {
                "好点": {"x": 10, "y": 20},
                "坏点": {"x": 9999, "y": 3},
                "缺字段": {"x": 1},
            }}},
        }, ensure_ascii=False), encoding="utf-8")
        s = self.store()
        self.assertEqual(s.place_names("a"), ["好点"])

    # ---- 基本读写 ----
    def test_put_get_require(self):
        s = self.store()
        s.put("钓位", (73, 57), map_name="蚊子湖", note="第三块石头")
        self.assertEqual(s.coord("钓位", "蚊子湖"), (73.0, 57.0))
        place = s.require("钓位", "蚊子湖")
        self.assertEqual((place.x, place.y), (73, 57))
        self.assertEqual(place.note, "第三块石头")
        self.assertTrue(place.marked_at)

    def test_roundtrip_through_disk(self):
        s = self.store()
        s.set_active("蚊子湖")
        s.put("钓位", (73, 57), note="abc")
        s.put("鱼市", (10, 20))
        s.save()

        again = self.store()
        self.assertEqual(again.active_map, "蚊子湖")
        self.assertEqual(again.coord("钓位"), (73.0, 57.0))
        self.assertEqual(again.require("钓位").note, "abc")
        self.assertEqual(again.coord("鱼市"), (10.0, 20.0))

    def test_coord_rounds_float_input(self):
        s = self.store()
        entry = s.put("点", (72.6, 57.4), map_name="m")
        self.assertEqual((entry.x, entry.y), (73, 57))

    # ---- 分地图隔离（核心） ----
    def test_same_name_on_two_maps_does_not_collide(self):
        s = self.store()
        s.put("钓位", (73, 57), map_name="蚊子湖")
        s.put("钓位", (400, 120), map_name="老奥")
        self.assertEqual(s.coord("钓位", "蚊子湖"), (73.0, 57.0))
        self.assertEqual(s.coord("钓位", "老奥"), (400.0, 120.0))
        self.assertEqual(s.map_names(), sorted(["蚊子湖", "老奥"]))

    def test_active_map_switches_lookup(self):
        s = self.store()
        s.put("钓位", (73, 57), map_name="蚊子湖")
        s.put("钓位", (400, 120), map_name="老奥")
        s.set_active("老奥")
        self.assertEqual(s.coord("钓位"), (400.0, 120.0))
        s.set_active("蚊子湖")
        self.assertEqual(s.coord("钓位"), (73.0, 57.0))

    def test_place_missing_on_current_map_is_none(self):
        # 关键安全行为：A 图有的点，在 B 图必须是 None，不能回退到别的地图
        s = self.store()
        s.put("鱼市", (10, 20), map_name="蚊子湖")
        s.set_active("老奥")
        self.assertIsNone(s.coord("鱼市"))
        with self.assertRaises(ap.PlacesError) as ctx:
            s.require("鱼市")
        self.assertIn("unknown_place", str(ctx.exception))

    def test_unknown_map_raises(self):
        s = self.store()
        with self.assertRaises(ap.PlacesError) as ctx:
            s.require("钓位", "不存在的地图")
        self.assertIn("unknown_map", str(ctx.exception))

    def test_active_map_missing_is_reported(self):
        self.path.write_text(json.dumps({
            "version": 2,
            "active_map": "已经删掉的图",
            "maps": {"别的图": {"places": {"钓位": {"x": 1, "y": 2}}}},
        }, ensure_ascii=False), encoding="utf-8")
        s = self.store()
        self.assertIn("active_map_missing", s.last_error or "")
        # 不能偷偷切到别的地图，否则会拿错坐标
        self.assertIsNone(s.coord("钓位"))

    # ---- 校验 ----
    def test_rejects_bad_coord(self):
        s = self.store()
        for bad in ((-1, 5), (5000, 5), (0, 2001)):
            with self.assertRaises(ap.PlacesError):
                s.put("点", bad, map_name="m")

    def test_rejects_bad_names(self):
        s = self.store()
        for bad in ("", "   ", "x" * 40, "a\nb"):
            with self.assertRaises(ap.PlacesError):
                s.put(bad, (1, 2), map_name="m")

    def test_remove(self):
        s = self.store()
        s.put("钓位", (1, 2), map_name="m")
        self.assertTrue(s.remove("钓位", "m"))
        self.assertFalse(s.remove("钓位", "m"))
        self.assertIsNone(s.coord("钓位", "m"))

    # ---- 展示 ----
    def test_describe_marks_active_map(self):
        s = self.store()
        s.put("钓位", (73, 57), map_name="蚊子湖", note="石头边")
        s.set_active("蚊子湖")
        text = s.describe()
        self.assertIn("蚊子湖（当前）", text)
        self.assertIn("钓位", text)
        self.assertIn("73", text)
        self.assertIn("57", text)
        self.assertIn("# 石头边", text)
        self.assertIn("fishing", text)


class _TempStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._tmp.name) / "places.json"

    def tearDown(self):
        self._tmp.cleanup()

    def store(self) -> ap.Places:
        return ap.Places(self.path)


class StrategyTests(_TempStore):
    def test_defaults_when_none_defined(self):
        s = self.store()
        self.assertIsNone(s.strategy())

    def test_put_and_roundtrip(self):
        s = self.store()
        s.put_strategy("底钓标准", method="bottom", friction_base=17,
                       cast_power=0.9, wait_bite_timeout=45, auto_net=True,
                       bait="玉米")
        s.set_active_strategy("底钓标准")
        s.save()

        again = self.store()
        st = again.strategy()
        self.assertEqual(st.name, "底钓标准")
        self.assertEqual(st.method, "bottom")
        self.assertEqual(st.friction_base, 17)
        self.assertAlmostEqual(st.cast_power, 0.9)
        self.assertAlmostEqual(st.wait_bite_timeout, 45.0)
        self.assertTrue(st.auto_net)
        self.assertEqual(st.bait, "玉米")

    def test_friction_base_is_capped_at_29(self):
        # 29 是客户端硬顶，配置不能突破
        s = self.store()
        with self.assertRaises(ap.PlacesError):
            s.put_strategy("越界", friction_base=30)
        with self.assertRaises(ap.PlacesError):
            s.put_strategy("越界", friction_base=0)

    def test_cast_power_is_clamped(self):
        s = self.store()
        self.assertAlmostEqual(s.put_strategy("强", cast_power=3.0).cast_power, 1.0)
        self.assertAlmostEqual(s.put_strategy("弱", cast_power=-1.0).cast_power, 0.0)

    def test_unknown_field_rejected(self):
        s = self.store()
        with self.assertRaises(ap.PlacesError) as ctx:
            s.put_strategy("坏", 摩擦=17)
        self.assertIn("unknown strategy fields", str(ctx.exception))

    def test_strategy_for_place(self):
        s = self.store()
        s.put_strategy("浮钓", method="float")
        s.put_strategy("底钓", method="bottom")
        s.put("钓位A", (1, 2), map_name="m", strategy="浮钓")
        s.set_active_strategy("底钓")
        place = s.require("钓位A", "m")
        self.assertEqual(s.strategy_for(place).method, "float")
        # 没绑策略的目的地退回当前策略
        s.put("钓位B", (3, 4), map_name="m")
        self.assertEqual(s.strategy_for(s.require("钓位B", "m")).method, "bottom")

    def test_missing_strategy_ref_warns_but_loads(self):
        self.path.write_text(json.dumps({
            "version": 3,
            "active_map": "m",
            "maps": {"m": {"places": {"钓位": {"x": 1, "y": 2, "strategy": "没定义"}}}},
        }, ensure_ascii=False), encoding="utf-8")
        s = self.store()
        self.assertIsNotNone(s.require("钓位", "m"))   # 点还在
        self.assertTrue(any("strategy_ref_missing" in w for w in s.warnings))

    def test_active_strategy_missing_warns(self):
        self.path.write_text(json.dumps({
            "version": 3, "active_map": "m", "active_strategy": "不存在",
            "maps": {"m": {"places": {}}},
        }, ensure_ascii=False), encoding="utf-8")
        s = self.store()
        self.assertTrue(any("active_strategy_missing" in w for w in s.warnings))
        self.assertIsNone(s.strategy())

    def test_v2_file_still_loads(self):
        # 旧格式（没有 strategies/kind）必须还能读
        self.path.write_text(json.dumps({
            "version": 2,
            "active_map": "蚊子湖",
            "maps": {"蚊子湖": {"places": {"钓位": {"x": 73, "y": 57}}}},
        }, ensure_ascii=False), encoding="utf-8")
        s = self.store()
        place = s.require("钓位", "蚊子湖")
        self.assertEqual((place.x, place.y), (73, 57))
        self.assertEqual(place.kind, ap.KIND_FISHING)


class DestinationTests(_TempStore):
    def test_multiple_destinations_per_map(self):
        s = self.store()
        s.put("钓位A", (10, 20), map_name="蚊子湖", kind=ap.KIND_FISHING)
        s.put("钓位B", (30, 40), map_name="蚊子湖", kind=ap.KIND_FISHING)
        s.put("鱼市", (50, 60), map_name="蚊子湖", kind=ap.KIND_SELL)
        self.assertEqual(len(s.destinations("蚊子湖")), 3)
        self.assertEqual(len(s.destinations("蚊子湖", kind=ap.KIND_FISHING)), 2)
        self.assertEqual(s.destinations("蚊子湖", kind=ap.KIND_SELL)[0].name, "鱼市")

    def test_active_place_requires_explicit_choice_when_ambiguous(self):
        s = self.store()
        s.put("钓位A", (10, 20), map_name="蚊子湖")
        s.put("钓位B", (30, 40), map_name="蚊子湖")
        s.set_active("蚊子湖")
        # 两个钓位又没选，不能猜
        self.assertIsNone(s.active_place(ap.KIND_FISHING))
        s.set_active_destination("钓位B", "蚊子湖")
        self.assertEqual(s.active_place(ap.KIND_FISHING).name, "钓位B")

    def test_single_fishing_place_is_implicit(self):
        s = self.store()
        s.put("钓位", (10, 20), map_name="蚊子湖")
        s.put("鱼市", (50, 60), map_name="蚊子湖", kind=ap.KIND_SELL)
        s.set_active("蚊子湖")
        self.assertEqual(s.active_place(ap.KIND_FISHING).name, "钓位")
        self.assertEqual(s.active_place(ap.KIND_SELL).name, "鱼市")

    def test_switching_map_clears_active_destination(self):
        s = self.store()
        s.put("钓位A", (10, 20), map_name="蚊子湖")
        s.set_active_destination("钓位A", "蚊子湖")
        s.set_active("老奥")
        self.assertEqual(s.active_destination, "")
        # 换图后旧目的地不能残留，否则会拿图A的点在图B里走
        self.assertIsNone(s.active_place())

    def test_set_active_destination_rejects_unknown(self):
        s = self.store()
        s.put("钓位A", (10, 20), map_name="蚊子湖")
        with self.assertRaises(ap.PlacesError):
            s.set_active_destination("不存在", "蚊子湖")
        with self.assertRaises(ap.PlacesError):
            s.set_active_destination("钓位A", "不存在的地图")

    def test_active_place_missing_after_delete(self):
        s = self.store()
        s.put("钓位A", (10, 20), map_name="蚊子湖")
        s.set_active_destination("钓位A", "蚊子湖")
        s.remove("钓位A", "蚊子湖")
        self.assertIsNone(s.active_place())


class HeadingTests(_TempStore):
    """目标点 = 坐标 + 朝向；朝向缺失时不能当成 0°（北）。"""

    def test_put_and_reload_keeps_heading(self):
        s = self.store()
        s.put("钓位A", (73, 57), map_name="蚊子湖", heading=278.6)
        s.save()
        again = self.store()
        place = again.require("钓位A", "蚊子湖")
        self.assertAlmostEqual(place.heading, 278.6)

    def test_heading_none_is_allowed(self):
        s = self.store()
        place = s.put("路过点", (1, 2), map_name="m")
        self.assertIsNone(place.heading)

    def test_heading_is_normalized(self):
        s = self.store()
        self.assertAlmostEqual(s.put("a", (1, 2), map_name="m", heading=370).heading, 10.0)
        self.assertAlmostEqual(s.put("b", (1, 2), map_name="m", heading=-90).heading, 270.0)

    def test_heading_zero_is_valid_not_missing(self):
        s = self.store()
        place = s.put("正北", (1, 2), map_name="m", heading=0)
        self.assertEqual(place.heading, 0.0)
        self.assertIsNotNone(place.heading)

    def test_describe_shows_heading(self):
        s = self.store()
        s.put("钓位A", (73, 57), map_name="蚊子湖", heading=278.6)
        text = s.describe("蚊子湖")
        self.assertIn("278.6", text)


if __name__ == "__main__":
    unittest.main()
