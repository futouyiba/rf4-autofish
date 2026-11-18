# -*- coding: utf-8 -*-
"""面板侧收尾客户端测试（全注入，无真实进程/文件）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autofish_winddown_client import WinddownClient  # noqa: E402


class FakeFs:
    def __init__(self, files=None):
        self.files = dict(files or {})
        self.removed = []

    def read(self, path):
        return self.files.get(str(path))

    def remove(self, path):
        self.removed.append(str(path))
        self.files.pop(str(path), None)


class WinddownClientTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeFs()
        self.spawned = []
        self.client = WinddownClient(
            spawn=lambda: self.spawned.append(1) or True,
            read=self.fs.read, remove=self.fs.remove,
            request_path=Path("request.json"),
            result_path=Path("result.json"),
        )

    def test_no_trigger_does_nothing(self):
        self.assertIsNone(self.client.poll())
        self.assertEqual(self.spawned, [])

    def test_trigger_consumed_once_and_spawns(self):
        self.fs.files["request.json"] = {"reason": "session_limit"}
        self.assertIsNone(self.client.poll())
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("request.json", self.fs.removed)
        # 再 poll 不重复拉起
        self.client.poll()
        self.assertEqual(len(self.spawned), 1)

    def test_result_shown_once(self):
        self.fs.files["request.json"] = {"reason": "session_limit"}
        self.client.poll()  # 拉起
        self.fs.files["result.json"] = {
            "state": "DONE",
            "steps": {"SELL": {"ok": True}, "LOGOUT": {"ok": True}},
            "completed_wall": "14:32:05",
        }
        first = self.client.poll()
        self.assertIsNotNone(first)
        self.assertIsNone(self.client.poll())  # 不重复弹

    def test_summary_success(self):
        text = self.client.summary_text({
            "state": "DONE",
            "steps": {"SELL": {"ok": True}, "LOGOUT": {"ok": True}},
            "completed_wall": "14:32:05",
        })
        self.assertIn("卖鱼  ✓", text)
        self.assertIn("小退  ✓", text)
        self.assertIn("14:32:05", text)
        self.assertNotIn("请人工处理", text)

    def test_summary_failure(self):
        text = self.client.summary_text({
            "state": "FAILED", "failed_step": "SELL",
            "failed_reason": "未识别商店界面",
            "steps": {"SELL": {"ok": False}},
            "completed_wall": "14:32:05",
        })
        self.assertIn("卖鱼  ✗", text)
        self.assertIn("请人工处理", text)
        self.assertIn("未识别商店界面", text)


if __name__ == "__main__":
    unittest.main()
