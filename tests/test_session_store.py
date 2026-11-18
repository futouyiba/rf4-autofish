# -*- coding: utf-8 -*-
"""会话配置持久化测试（临时目录，不碰真实数据）。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import autofish_session_store as store  # noqa: E402
import autofish_session_timer as st  # noqa: E402


class SessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "session.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_file_returns_default(self) -> None:
        timer, note = store.load_session(self.path)
        self.assertEqual(timer.limit, 8 * 3600)
        self.assertEqual(timer.state, st.PAUSED)
        self.assertEqual(note, "")

    def test_roundtrip(self) -> None:
        timer = st.SessionTimer(limit_seconds=7200)
        timer.start(1000.0)
        self.assertTrue(store.save_session(timer, self.path))
        loaded, note = store.load_session(self.path)
        self.assertEqual(note, "")
        self.assertEqual(loaded.limit, 7200)
        self.assertEqual(loaded.state, st.RUNNING)
        self.assertEqual(loaded.elapsed_seconds(1600.0), 600.0)

    def test_corrupt_file_falls_back(self) -> None:
        self.path.write_text("not json", encoding="utf-8")
        timer, note = store.load_session(self.path)
        self.assertEqual(timer.limit, 8 * 3600)
        self.assertIn("读取失败", note)

    def test_wrong_shape_falls_back(self) -> None:
        self.path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        timer, note = store.load_session(self.path)
        self.assertEqual(timer.limit, 8 * 3600)
        self.assertIn("格式错误", note)

    def test_save_creates_parent(self) -> None:
        nested = Path(self._tmp.name) / "a" / "b" / "session.json"
        self.assertTrue(store.save_session(st.SessionTimer(), nested))
        self.assertTrue(nested.exists())


if __name__ == "__main__":
    unittest.main()
