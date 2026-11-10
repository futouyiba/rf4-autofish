# -*- coding: utf-8 -*-
"""打包层（app_paths / 冻结态启动链）的回归测试。

这些用例专门盯"打包后静默失效"的三处：
* 路径解析（可写目录不能落在只读安装目录）；
* 面板 → bot_ctl 的命令行（冻结态是同一个 exe + ``--ctl``）；
* bot_ctl → bridge 的命令行（冻结态是同一个 exe + ``--bot``）。
"""

from __future__ import annotations

import importlib
import os
import queue
import sys
import unittest
from pathlib import Path
from unittest import mock

BOT_DIR = Path(__file__).resolve().parents[1]
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import app_paths  # noqa: E402


class AppPathsTests(unittest.TestCase):
    def test_dev_mode_uses_source_dir(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            module = importlib.reload(app_paths)
            self.assertFalse(module.is_frozen())
            self.assertEqual(module.APP_DIR, BOT_DIR)
            self.assertEqual(module.BUNDLE_DIR, BOT_DIR)
            # 私仓用 refs/，发布仓用 vendor/，两种布局都要能解析到
            ref = module.ref_dir()
            self.assertEqual(ref, BOT_DIR / "refs" / "RussianFishing4Script"
                             if (BOT_DIR / "refs" / "RussianFishing4Script" / "rf4s").is_dir()
                             else BOT_DIR / "vendor")

    def test_frozen_mode_uses_exe_dir_and_localappdata(self):
        exe = BOT_DIR / "dist" / "RF4AutoFish" / "RF4AutoFish.exe"
        bundle = BOT_DIR / "dist" / "RF4AutoFish" / "_internal"
        local = BOT_DIR / "dist" / "_fake_local"
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local)}, clear=False):
            os.environ.pop("RF4_DATA_DIR", None)
            with mock.patch.object(sys, "frozen", True, create=True), \
                 mock.patch.object(sys, "executable", str(exe)), \
                 mock.patch.object(sys, "_MEIPASS", str(bundle), create=True):
                module = importlib.reload(app_paths)
                self.assertTrue(module.is_frozen())
                self.assertEqual(module.APP_DIR, exe.parent)
                self.assertEqual(module.BUNDLE_DIR, bundle)
                self.assertEqual(module.DATA_DIR, local / "RF4AutoFish")
                # 上游 rf4s 用 parents[2] 找 static，所以 ref_dir 必须是解包根
                self.assertEqual(module.ref_dir(), bundle)

    def test_data_dir_env_override_wins(self):
        target = BOT_DIR / "dist" / "_env_data"
        with mock.patch.dict(os.environ, {"RF4_DATA_DIR": str(target)}, clear=False):
            module = importlib.reload(app_paths)
            self.assertEqual(module.DATA_DIR, target)

    def test_restore_for_other_tests(self):
        os.environ.pop("RF4_DATA_DIR", None)
        importlib.reload(app_paths)
        self.assertEqual(app_paths.DATA_DIR, BOT_DIR)


class PanelLaunchTests(unittest.TestCase):
    """面板点"启动后台"时到底跑了什么命令。"""

    def _panel(self):
        import autofish_ui

        panel = object.__new__(autofish_ui.AutoFishPanel)
        panel._output = queue.Queue()
        return panel

    def _capture(self, frozen: bool, action: str = "start"):
        panel = self._panel()
        calls = []

        class _FakePopen:
            def __init__(self, cmd, **kwargs):
                calls.append((cmd, kwargs.get("cwd")))

        with mock.patch("autofish_ui.app_paths.is_frozen", return_value=frozen), \
             mock.patch("autofish_ui.subprocess.Popen", _FakePopen):
            panel._launch_ctl(action, "ok")
            # _launch_ctl 在后台线程里 Popen，等它跑完
            for thread in __import__("threading").enumerate():
                if thread.name.startswith("autofish-ui-ctl-"):
                    thread.join(2.0)
        return calls

    def test_frozen_launches_same_exe_with_ctl(self):
        calls = self._capture(frozen=True)
        self.assertEqual(len(calls), 1)
        cmd, cwd = calls[0]
        self.assertEqual(cmd, [sys.executable, "--ctl", "start"])
        self.assertEqual(Path(cwd), app_paths.DATA_DIR)

    def test_dev_launches_bot_ctl_script(self):
        calls = self._capture(frozen=False)
        cmd, cwd = calls[0]
        self.assertEqual(cmd, [sys.executable, str(BOT_DIR / "bot_ctl.py"), "start"])
        self.assertEqual(Path(cwd), BOT_DIR)

    def test_frozen_action_whitelist(self):
        import autofish_ui

        with mock.patch("autofish_ui.app_paths.is_frozen", return_value=True):
            self.assertTrue(autofish_ui._bot_ctl_has_action("start"))
            self.assertTrue(autofish_ui._bot_ctl_has_action("emergency-stop"))
            self.assertFalse(autofish_ui._bot_ctl_has_action("does-not-exist"))


class BotCtlCommandTests(unittest.TestCase):
    """bot_ctl 拉起 bridge 的命令行。"""

    def test_frozen_uses_exe_bot_mode(self):
        import bot_ctl

        with mock.patch.object(bot_ctl.app_paths, "is_frozen", return_value=True):
            cmd, cwd, env = bot_ctl._build_command(False)
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1], "--bot")
        self.assertIn("--pidfile", cmd)
        self.assertIn("--endpoint", cmd)
        self.assertEqual(Path(cwd), Path(bot_ctl.DATA))
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")

    def test_frozen_rejects_legacy(self):
        import bot_ctl

        with mock.patch.object(bot_ctl.app_paths, "is_frozen", return_value=True):
            with self.assertRaises(RuntimeError):
                bot_ctl._build_command(True)

    def test_dev_uses_venv_python(self):
        import bot_ctl

        with mock.patch.object(bot_ctl.app_paths, "is_frozen", return_value=False):
            cmd, cwd, env = bot_ctl._build_command(False)
        self.assertTrue(cmd[0].endswith("python.exe"))
        self.assertTrue(cmd[1].endswith("autofish_bridge.py"))
        self.assertEqual(env["PYTHONPATH"], bot_ctl.BOT)


if __name__ == "__main__":
    unittest.main()
