"""坐标表命令行里不依赖游戏的部分：改名、自动命名。"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

BOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT))

import autofish_places as ap  # noqa: E402
import record_place  # noqa: E402


class RenameTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._tmp.name) / "p.json"
        store = ap.Places(self.path)
        store.put("点1", (73, 57), map_name="蚊子湖", heading=278.6,
                  kind=ap.KIND_FISHING, strategy="底钓标准", note="石头边")
        store.put("点2", (88, 44), map_name="蚊子湖")
        store.set_active("蚊子湖")
        store.set_active_destination("点1", "蚊子湖")
        store.save()

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args):
        return record_place.main(["--file", str(self.path), *args])

    def test_rename_keeps_all_fields(self):
        code = self.run_cli("rename", "点1", "钓位A")
        self.assertEqual(code, 0)
        store = ap.Places(self.path)
        place = store.require("钓位A", "蚊子湖")
        self.assertEqual((place.x, place.y), (73, 57))
        self.assertAlmostEqual(place.heading, 278.6)
        self.assertEqual(place.strategy, "底钓标准")
        self.assertEqual(place.note, "石头边")
        self.assertIsNone(store.get("点1", "蚊子湖"))

    def test_rename_updates_active_destination(self):
        self.run_cli("rename", "点1", "钓位A")
        store = ap.Places(self.path)
        self.assertEqual(store.active_destination, "钓位A")

    def test_rename_to_existing_fails(self):
        code = self.run_cli("rename", "点1", "点2")
        self.assertEqual(code, 1)
        store = ap.Places(self.path)
        self.assertIsNotNone(store.get("点1", "蚊子湖"))

    def test_rename_missing_fails(self):
        self.assertEqual(self.run_cli("rename", "不存在", "新名"), 1)

    def test_rename_unknown_map_fails(self):
        self.assertEqual(self.run_cli("rename", "点1", "x", "--map", "没这图"), 1)

    def test_rename_with_map_option(self):
        code = self.run_cli("rename", "点1", "钓位A", "--map", "蚊子湖")
        self.assertEqual(code, 0)


class NextNameTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self._tmp.name) / "p.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_increments(self):
        store = ap.Places(self.path)
        store.put("点1", (1, 2), map_name="m")
        store.put("点2", (3, 4), map_name="m")
        self.assertEqual(record_place._next_name(store, "m", "点"), "点3")

    def test_skips_taken_numbers(self):
        store = ap.Places(self.path)
        store.put("点1", (1, 2), map_name="m")
        store.put("点3", (3, 4), map_name="m")
        self.assertEqual(record_place._next_name(store, "m", "点"), "点2")

    def test_custom_prefix(self):
        store = ap.Places(self.path)
        self.assertEqual(record_place._next_name(store, "m", "钓位"), "钓位1")


if __name__ == "__main__":
    unittest.main()
