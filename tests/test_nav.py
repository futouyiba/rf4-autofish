"""坐标导航的离线测试：几何、罗盘判据、闭环走位。

模拟角色：坐标随按 W 变化（沿当前朝向），鼠标位移改变朝向。
关键约束来自实机：**A/D 是横移不是转身**，所以转向只能走 ``move_mouse``。
"""

from __future__ import annotations

import math
import pathlib
import sys
import unittest

BOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT))

import autofish_nav as nav  # noqa: E402
from autofish_control import AutoStop  # noqa: E402


# ----------------------------------------------------------------------
# 纯几何与解析
# ----------------------------------------------------------------------
class ParseCoordTests(unittest.TestCase):
    def test_half_and_full_width(self):
        self.assertEqual(nav.parse_coord("123:456"), (123.0, 456.0))
        self.assertEqual(nav.parse_coord("123：456"), (123.0, 456.0))
        self.assertEqual(nav.parse_coord("123.456"), (123.0, 456.0))
        self.assertEqual(nav.parse_coord(" 1 2 3 : 4 5 6 "), (123.0, 456.0))

    def test_rejects_junk(self):
        for bad in ("", "abc", "123", "123:", "1:2:3", "1234:5678", "-5:7"):
            self.assertIsNone(nav.parse_coord(bad), bad)

    def test_pick_coord_skips_origin_and_prefers_confident(self):
        self.assertIsNone(nav.pick_coord([{"text": "0:0", "score": 0.99}]))
        items = [{"text": "12:34", "score": 0.4},
                 {"text": "567:890", "score": 0.95},
                 {"text": "菜单", "score": 0.99}]
        self.assertEqual(nav.pick_coord(items), (567.0, 890.0))
        self.assertEqual(nav.pick_coord(["hello", "45:67"]), (45.0, 67.0))


class GeometryTests(unittest.TestCase):
    def test_bearing_axes(self):
        self.assertAlmostEqual(nav.bearing((0, 0), (0, 10)), 0.0)
        self.assertAlmostEqual(nav.bearing((0, 0), (10, 0)), 90.0)
        self.assertAlmostEqual(nav.bearing((0, 0), (0, -10)), 180.0)
        self.assertAlmostEqual(nav.bearing((0, 0), (-10, 0)), 270.0)

    def test_normalize(self):
        self.assertAlmostEqual(nav.normalize_deg(370), 10.0)
        self.assertAlmostEqual(nav.normalize_deg(-370), -10.0)
        self.assertEqual(abs(nav.normalize_deg(180)), 180.0)

    def test_polar_angle(self):
        c = (100.0, 100.0)
        self.assertAlmostEqual(nav.polar_angle(c, (100, 0)), 0.0)     # 正上
        self.assertAlmostEqual(nav.polar_angle(c, (200, 100)), 90.0)  # 右
        self.assertAlmostEqual(nav.polar_angle(c, (100, 200)), 180.0)  # 下
        self.assertAlmostEqual(nav.polar_angle(c, (0, 100)), 270.0)   # 左


# ----------------------------------------------------------------------
# 罗盘朝向
# ----------------------------------------------------------------------
class HeadingTests(unittest.TestCase):
    def _marks(self, theta_n, radius=100.0, center=(1792.0, 952.0)):
        """构造一对 N/S 标记：N 在相对圆心 theta_n 处，S 在正对面。"""
        cx, cy = center

        def at(theta, r=radius):
            rad = math.radians(theta)
            return (cx + math.sin(rad) * r, cy - math.cos(rad) * r, 300)

        return {"N": at(theta_n), "S": at((theta_n + 180) % 360)}

    def test_real_calibration_sample(self):
        # 实机样本：N(1894,937) / S(1691,968) → 朝向 278.6°
        marks = {"N": (1894.1, 937.1, 345), "S": (1691.0, 967.9, 375)}
        heading = nav.heading_from_marks(marks)
        self.assertIsNotNone(heading)
        self.assertAlmostEqual(heading, 278.6, delta=0.5)

    def test_heading_is_negative_theta(self):
        for theta in (0, 45, 90, 180, 270):
            h = nav.heading_from_marks(self._marks(theta))
            self.assertAlmostEqual(h, (-theta) % 360, delta=0.5, msg=str(theta))

    def test_180_boundary_is_not_rejected(self):
        # N/S 正好相差 180° 时，normalize_deg 会返回 ±180 两种符号，
        # 校验不能因此判成"认不准"（这是实机上偶发 None 的根因）。
        for theta in (0.0, 90.0, 180.0, 270.0):
            self.assertIsNotNone(nav.heading_from_marks(self._marks(theta)))

    def test_slight_float_drift_still_ok(self):
        marks = self._marks(90.0)
        marks["S"] = (marks["S"][0] + 1e-6, marks["S"][1], 300)
        self.assertIsNotNone(nav.heading_from_marks(marks))

    def test_missing_north_returns_none(self):
        self.assertIsNone(nav.heading_from_marks({"S": (1.0, 2.0, 300)}))

    def test_inconsistent_ns_rejected(self):
        marks = self._marks(0)
        # S 跑到 N 的同侧：差 0°，应判定认错
        marks["S"] = marks["N"]
        self.assertIsNone(nav.heading_from_marks(marks))


# ----------------------------------------------------------------------
# 模拟角色
# ----------------------------------------------------------------------
class FakeWorld:
    def __init__(self, pos=(0.0, 0.0), heading=0.0, speed=0.5, px_per_deg=9.6,
                 blocked=False, integer_coords=False, heading_readable=True):
        self.pos = [float(pos[0]), float(pos[1])]
        self.heading = heading % 360.0
        self.speed = speed
        self.px_per_deg = px_per_deg
        self.blocked = blocked
        self.integer_coords = integer_coords
        self.heading_readable = heading_readable
        self.held: set[str] = set()
        self.t = 0.0
        self.mouse_dx = 0

    def advance(self, seconds: float) -> None:
        if "w" in self.held and not self.blocked:
            rad = math.radians(self.heading)
            self.pos[0] += math.sin(rad) * self.speed * seconds
            self.pos[1] += math.cos(rad) * self.speed * seconds
        self.t += seconds

    def move_mouse(self, dx: int) -> None:
        self.mouse_dx += dx
        self.heading = (self.heading + dx / self.px_per_deg) % 360.0

    def coord(self):
        if self.integer_coords:
            return (float(round(self.pos[0])), float(round(self.pos[1])))
        return (self.pos[0], self.pos[1])


class FakeIo:
    def __init__(self, world: FakeWorld, blind_after=None, heading_blind=False):
        self.world = world
        self.reads = 0
        self.blind_after = blind_after
        self.heading_blind = heading_blind
        self.keys: list[tuple[str, str]] = []

    def read_coord(self):
        if self.blind_after is not None and self.reads >= self.blind_after:
            self.reads += 1
            return None
        self.reads += 1
        return self.world.coord()

    def read_heading(self):
        return None if self.heading_blind else self.world.heading

    def move_mouse(self, dx):
        self.world.move_mouse(dx)

    def key_down(self, name):
        self.keys.append(("down", name))
        self.world.held.add(name)

    def key_up(self, name):
        self.keys.append(("up", name))
        self.world.held.discard(name)


class StopControl:
    def __init__(self, stop_after=10 ** 9):
        self.calls = 0
        self.stop_after = stop_after

    def checkpoint(self):
        self.calls += 1
        if self.calls >= self.stop_after:
            raise AutoStop("stopped")


def run(world, target, cfg=None, control=None, blind_after=None,
        heading_blind=False):
    io = FakeIo(world, blind_after=blind_after, heading_blind=heading_blind)
    logs = []
    result = nav.navigate(io, target, cfg=cfg, control=control,
                          log=logs.append, sleep=world.advance,
                          now=lambda: world.t)
    return result, io, logs


# ----------------------------------------------------------------------
# 导航闭环
# ----------------------------------------------------------------------
class NavigateTests(unittest.TestCase):
    def test_already_there(self):
        w = FakeWorld(pos=(10, 10), heading=0)
        result, io, _ = run(w, (10.6, 10))
        self.assertTrue(result["ok"])
        self.assertEqual(io.keys, [])
        self.assertEqual(result["walks"], 0)

    def test_walks_straight_north(self):
        w = FakeWorld(pos=(0, 0), heading=0)
        result, io, _ = run(w, (0, 20))
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["walks"], 1)
        self.assertAlmostEqual(w.pos[1], 20, delta=2)
        self.assertEqual(result["turns"], 0)

    def test_turns_with_mouse_then_walks(self):
        w = FakeWorld(pos=(0, 0), heading=0)   # 朝北
        result, _io, _ = run(w, (20, 0))       # 目标在东
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["turns"], 1)
        self.assertAlmostEqual(w.pos[0], 20, delta=2)
        self.assertAlmostEqual(w.pos[1], 0, delta=2)

    def test_never_presses_side_keys(self):
        # A/D 在本客户端是横移不是转身，导航绝不能按它们
        w = FakeWorld(pos=(0, 0), heading=0)
        _r, io, _ = run(w, (15, 15))
        self.assertTrue(io.keys)
        for _direction, name in io.keys:
            self.assertEqual(name, "w", "导航只应操作 W，实际按了 %r" % name)

    def test_self_calibrates_px_per_deg(self):
        # 必须真的转一次才有机会标定；目标放在正东（要转 90°）
        w = FakeWorld(pos=(0, 0), heading=0, px_per_deg=15.0)
        result, _io, _ = run(w, (30, 0), cfg=nav.NavConfig(px_per_deg=6.0))
        self.assertTrue(result["ok"])
        self.assertGreater(result["px_per_deg"], 6.0)

    def test_blind_coord_gives_up(self):
        w = FakeWorld()
        with self.assertRaises(nav.NavError) as ctx:
            run(w, (50, 50), cfg=nav.NavConfig(blind_limit=3), blind_after=0)
        self.assertEqual(ctx.exception.stage, "blind")

    def test_heading_blind_gives_up(self):
        w = FakeWorld(pos=(0, 0), heading=0)
        with self.assertRaises(nav.NavError) as ctx:
            run(w, (50, 50), cfg=nav.NavConfig(heading_blind_limit=3),
                heading_blind=True)
        self.assertEqual(ctx.exception.stage, "heading_blind")

    def test_stuck_detected(self):
        w = FakeWorld(pos=(0, 0), heading=0, blocked=True)
        with self.assertRaises(nav.NavError) as ctx:
            run(w, (30, 0), cfg=nav.NavConfig(stuck_limit=2, step_sec=0.2))
        self.assertEqual(ctx.exception.stage, "stuck")

    def test_timeout(self):
        # 走得动但太慢：不能误判成 stuck，应该报 timeout
        w = FakeWorld(pos=(0, 0), heading=0, speed=0.2)
        with self.assertRaises(nav.NavError) as ctx:
            run(w, (500, 500), cfg=nav.NavConfig(timeout=5.0, step_sec=2.0))
        self.assertEqual(ctx.exception.stage, "timeout")

    def test_stop_request_propagates(self):
        w = FakeWorld(pos=(0, 0), heading=0)
        with self.assertRaises(AutoStop):
            run(w, (200, 200), control=StopControl(stop_after=2))

    def test_integer_coord_resolution_still_converges(self):
        # 真实坐标是整数：单步位移可能取整后不变，不能因此误判卡住
        w = FakeWorld(pos=(0, 0), heading=0, speed=0.5, integer_coords=True)
        result, _io, _ = run(w, (0, 6), cfg=nav.NavConfig(step_sec=1.0,
                                                          stuck_limit=6))
        self.assertTrue(result["ok"])
        self.assertLessEqual(abs(w.pos[1] - 6), 3)

    def test_mouse_step_capped(self):
        # 需要转 180° 时，单次鼠标位移必须被上限截住，不能一把甩飞
        w = FakeWorld(pos=(0, 0), heading=0)
        cfg = nav.NavConfig(max_mouse_px=120, px_per_deg=9.6)
        result, _io, _ = run(w, (0, -20), cfg=cfg)
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["turns"], 2)


class FaceHeadingTests(unittest.TestCase):
    """目标点 = 坐标 + 朝向：走到位之后还要面朝对的方向。"""

    def test_turns_to_target_heading(self):
        w = FakeWorld(pos=(0, 0), heading=0, px_per_deg=10.0)
        io = FakeIo(w)
        result = nav.face_heading(io, 90, sleep=w.advance, now=lambda: w.t)
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(w.heading, 90, delta=6)

    def test_turns_the_long_way_through_mouse_only(self):
        # 180° 也要只靠鼠标，且单次位移被 max_mouse_px 截断
        w = FakeWorld(pos=(0, 0), heading=0, px_per_deg=10.0)
        io = FakeIo(w)
        result = nav.face_heading(io, 180, cfg=nav.NavConfig(max_mouse_px=200),
                                  sleep=w.advance, now=lambda: w.t)
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(w.heading, 180, delta=6)
        self.assertGreaterEqual(result["rounds"], 2)

    def test_heading_blind_raises(self):
        w = FakeWorld(pos=(0, 0), heading=0)
        io = FakeIo(w, heading_blind=True)
        with self.assertRaises(nav.NavError) as ctx:
            nav.face_heading(io, 90, cfg=nav.NavConfig(heading_blind_limit=2),
                             sleep=w.advance, now=lambda: w.t)
        self.assertEqual(ctx.exception.stage, "heading_blind")

    def test_face_timeout(self):
        w = FakeWorld(pos=(0, 0), heading=0)
        io = FakeIo(w)
        io.world.heading = 0
        with self.assertRaises(nav.NavError) as ctx:
            nav.face_heading(io, 90, cfg=nav.NavConfig(face_timeout=0.0),
                             sleep=w.advance, now=lambda: w.t)
        self.assertEqual(ctx.exception.stage, "face_timeout")


class NavigateToPlaceTests(unittest.TestCase):
    """按名字导航：坐标从分地图坐标表里取，取不到必须失败而不是乱走。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        import autofish_places as ap
        self.places = ap.Places(pathlib.Path(self._tmp.name) / "p.json")
        self.places.put("钓位", (0, 10), map_name="图A")
        self.places.put("钓位", (10, 0), map_name="图B")
        self.places.set_active("图A")

    def tearDown(self):
        self._tmp.cleanup()

    def test_uses_active_map_place(self):
        w = FakeWorld(pos=(0, 0), heading=0)
        io = FakeIo(w)
        result = nav.navigate_to_place(io, self.places, "钓位",
                                       sleep=w.advance, now=lambda: w.t)
        self.assertTrue(result["ok"])
        self.assertEqual(result["map"], "图A")
        self.assertAlmostEqual(w.pos[1], 10, delta=2)

    def test_explicit_map_overrides_active(self):
        w = FakeWorld(pos=(0, 0), heading=0)
        io = FakeIo(w)
        result = nav.navigate_to_place(io, self.places, "钓位", map_name="图B",
                                       sleep=w.advance, now=lambda: w.t)
        self.assertEqual(result["map"], "图B")
        self.assertAlmostEqual(w.pos[0], 10, delta=2)

    def test_unknown_place_raises_without_moving(self):
        w = FakeWorld(pos=(0, 0), heading=0)
        io = FakeIo(w)
        with self.assertRaises(nav.NavError) as ctx:
            nav.navigate_to_place(io, self.places, "鱼市",
                                  sleep=w.advance, now=lambda: w.t)
        self.assertEqual(ctx.exception.stage, "unknown_place")
        self.assertEqual(io.keys, [])
        self.assertEqual(w.pos, [0.0, 0.0])

    def test_faces_target_heading_after_arrival(self):
        # 走到点只是第一步；抛竿前还得面朝水面
        self.places.put("带朝向的钓位", (0, 10), map_name="图A", heading=90.0)
        w = FakeWorld(pos=(0, 0), heading=0, px_per_deg=10.0)
        io = FakeIo(w)
        result = nav.navigate_to_place(io, self.places, "带朝向的钓位",
                                       sleep=w.advance, now=lambda: w.t)
        self.assertTrue(result["faced"])
        self.assertAlmostEqual(result["heading_target"], 90.0)
        self.assertAlmostEqual(w.heading, 90, delta=6)

    def test_place_without_heading_skips_facing(self):
        w = FakeWorld(pos=(0, 0), heading=0, px_per_deg=10.0)
        io = FakeIo(w)
        result = nav.navigate_to_place(io, self.places, "钓位",
                                       sleep=w.advance, now=lambda: w.t)
        self.assertFalse(result["faced"])
        self.assertIsNone(result["heading_target"])

    def test_face_can_be_disabled(self):
        self.places.put("带朝向的钓位", (0, 10), map_name="图A", heading=90.0)
        w = FakeWorld(pos=(0, 0), heading=0, px_per_deg=10.0)
        io = FakeIo(w)
        result = nav.navigate_to_place(io, self.places, "带朝向的钓位", face=False,
                                       sleep=w.advance, now=lambda: w.t)
        self.assertFalse(result["faced"])
        self.assertAlmostEqual(w.heading, 0, delta=6)   # 保持行进方向

    def test_place_on_other_map_does_not_fall_back(self):
        # 图B 没有「鱼市」，当前图是图A；绝不能拿图A的坐标去图B走
        self.places.put("鱼市", (5, 5), map_name="图A")
        self.places.set_active("图B")
        w = FakeWorld(pos=(0, 0), heading=0)
        io = FakeIo(w)
        with self.assertRaises(nav.NavError):
            nav.navigate_to_place(io, self.places, "鱼市",
                                  sleep=w.advance, now=lambda: w.t)
        self.assertEqual(io.keys, [])


if __name__ == "__main__":
    unittest.main()
