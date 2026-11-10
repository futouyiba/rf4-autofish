"""离线 bot_ctl 测试：不启动真实 bot、不 taskkill、不联网。

所有状态文件都指向临时目录；进程身份用 fake 的 creation time / image 模拟，
不会查询或操作真实进程。
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

BOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT))

import bot_ctl  # noqa: E402

FAKE_IMAGE = r"C:\Python311\python.exe"


class BotCtlTestCase(unittest.TestCase):
    """Redirect every state file to a fresh temporary directory."""

    def setUp(self) -> None:
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp_dir = pathlib.Path(temp.name)
        self.identity_path = self.temp_dir / "autofish.process.json"
        self.pid_path = self.temp_dir / "run.pid"
        self.endpoint_path = self.temp_dir / "autofish.endpoint.json"
        for name, value in (
            ("IDENTITY_FILE", str(self.identity_path)),
            ("PIDFILE", str(self.pid_path)),
            ("ENDPOINT", str(self.endpoint_path)),
        ):
            patcher = mock.patch.object(bot_ctl, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_identity(
        self,
        pid: int = 4242,
        role: str = "bridge",
        created: int = 111,
        image: str = FAKE_IMAGE,
    ) -> dict:
        with mock.patch.object(
            bot_ctl, "_process_creation_time", return_value=created
        ), mock.patch.object(
            bot_ctl, "_process_image_path", return_value=image
        ):
            return bot_ctl.write_process_identity(pid, role)

    @staticmethod
    def verify_env(created: int = 111, image: str = FAKE_IMAGE):
        return (
            mock.patch.object(
                bot_ctl, "_process_creation_time", return_value=created
            ),
            mock.patch.object(bot_ctl, "_process_image_path", return_value=image),
        )


class CommandSelectionTests(BotCtlTestCase):
    def test_bridge_is_default(self) -> None:
        cmd, cwd, _env = bot_ctl._build_command(False)
        self.assertEqual(pathlib.Path(cmd[1]).name, "autofish_bridge.py")
        self.assertEqual(pathlib.Path(cwd).resolve(), BOT.resolve())
        self.assertIn("--pidfile", cmd)
        self.assertIn("--endpoint", cmd)

    def test_legacy_is_explicit(self) -> None:
        cmd, cwd, _env = bot_ctl._build_command(True)
        self.assertEqual(pathlib.Path(cmd[1]).name, "rf4s_adapter.py")
        self.assertEqual(
            pathlib.Path(cwd).resolve(),
            pathlib.Path(bot_ctl.LEGACY_CWD).resolve(),
        )

    def test_legacy_flag_and_env(self) -> None:
        self.assertTrue(bot_ctl._legacy_requested(["--legacy"]))
        with mock.patch.dict(
            bot_ctl.os.environ, {"RF4_BOT_LEGACY": "1"}, clear=False
        ):
            self.assertTrue(bot_ctl._legacy_requested([]))
        with mock.patch.dict(
            bot_ctl.os.environ, {"RF4S_LEGACY": "0"}, clear=False
        ):
            self.assertFalse(bot_ctl._legacy_requested([]))


class StartTests(BotCtlTestCase):
    def test_start_launches_exactly_once_even_on_zero(self) -> None:
        calls = []

        def fake_launch(cmd, cwd, env, log_path, role="bridge"):
            calls.append((cmd, cwd, env, log_path, role))
            return 0

        with mock.patch.object(bot_ctl, "running_pid", return_value=None), \
                mock.patch.object(bot_ctl, "legacy_pid", return_value=None), \
                mock.patch.object(bot_ctl, "_remove_pid"), \
                mock.patch.object(bot_ctl, "_launch", side_effect=fake_launch):
            rc = bot_ctl.start(False)

        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)  # rc=0 不自动重启
        self.assertIn("autofish_bridge.py", calls[0][0][1])
        self.assertEqual(calls[0][4], "bridge")

    def test_start_refuses_when_identity_verified(self) -> None:
        with mock.patch.object(bot_ctl, "running_pid", return_value=123), \
                mock.patch.object(bot_ctl, "_launch") as launch:
            self.assertEqual(bot_ctl.start(False), 1)
        launch.assert_not_called()

    def test_start_warns_about_unverified_legacy_pid_but_does_not_kill(self) -> None:
        with mock.patch.object(bot_ctl, "running_pid", return_value=None), \
                mock.patch.object(bot_ctl, "legacy_pid", return_value=5555), \
                mock.patch.object(bot_ctl, "_remove_pid"), \
                mock.patch.object(bot_ctl, "_launch", return_value=0) as launch, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = bot_ctl.start(False)

        self.assertEqual(rc, 0)
        launch.assert_called_once()
        self.assertIn("5555", out.getvalue())
        self.assertIn("无法验证身份", out.getvalue())


class StopTests(BotCtlTestCase):
    def test_stop_without_instance_never_releases_keys(self) -> None:
        with mock.patch.object(bot_ctl, "running_pid", return_value=None), \
                mock.patch.object(
                    bot_ctl, "_ipc_status", return_value={"ok": False}
                ), \
                mock.patch.object(bot_ctl, "_ipc_stop") as ipc, \
                mock.patch.object(bot_ctl, "release_keys") as release, \
                mock.patch.object(bot_ctl, "_remove_pid"):
            rc = bot_ctl.stop()
        self.assertEqual(rc, 0)
        ipc.assert_not_called()
        release.assert_not_called()

    def test_stop_uses_ipc_and_waits_without_foreground_or_kill(self) -> None:
        with mock.patch.object(bot_ctl, "running_pid", return_value=4321), \
                mock.patch.object(
                    bot_ctl, "_ipc_stop", return_value={"ok": True}
                ) as ipc, \
                mock.patch.object(bot_ctl, "_wait_pid_exit", return_value=True), \
                mock.patch.object(bot_ctl, "release_keys") as release, \
                mock.patch.object(bot_ctl, "_remove_pid"), \
                mock.patch.object(bot_ctl, "_remove_identity"), \
                mock.patch.object(bot_ctl.subprocess, "run") as run:
            rc = bot_ctl.stop()
        self.assertEqual(rc, 0)
        ipc.assert_called_once()
        release.assert_not_called()
        run.assert_not_called()

    def test_stop_timeout_only_hints_emergency(self) -> None:
        with mock.patch.object(bot_ctl, "running_pid", return_value=4321), \
                mock.patch.object(bot_ctl, "_ipc_stop", return_value={"ok": True}), \
                mock.patch.object(bot_ctl, "_wait_pid_exit", return_value=False), \
                mock.patch.object(bot_ctl, "release_keys") as release, \
                mock.patch.object(bot_ctl.subprocess, "run") as run, \
                mock.patch.object(bot_ctl, "_remove_pid"):
            rc = bot_ctl.stop()
        self.assertEqual(rc, 1)
        release.assert_not_called()
        run.assert_not_called()

    def test_stop_can_use_session_pid_when_identity_is_missing(self) -> None:
        self.endpoint_path.write_text(
            json.dumps({"pid": 7777, "session": "s", "token": "t"}),
            encoding="utf-8",
        )
        with mock.patch.object(bot_ctl, "running_pid", return_value=None), \
                mock.patch.object(
                    bot_ctl, "_ipc_status", return_value={"ok": True}
                ), \
                mock.patch.object(
                    bot_ctl, "_ipc_stop", return_value={"ok": True}
                ), \
                mock.patch.object(bot_ctl, "_wait_pid_exit", return_value=True) as wait, \
                mock.patch.object(bot_ctl, "_remove_pid"), \
                mock.patch.object(bot_ctl, "_remove_identity"), \
                mock.patch.object(bot_ctl.subprocess, "run") as run:
            rc = bot_ctl.stop()

        self.assertEqual(rc, 0)
        wait.assert_called_once_with(7777, bot_ctl.STOP_TIMEOUT)
        run.assert_not_called()


class IdentityTests(BotCtlTestCase):
    def test_write_process_identity_persists_all_fields(self) -> None:
        identity = self.write_identity()
        data = json.loads(self.identity_path.read_text(encoding="utf-8"))

        self.assertEqual(data["version"], bot_ctl.IDENTITY_VERSION)
        self.assertEqual(data["pid"], 4242)
        self.assertEqual(data["creation_time"], 111)
        self.assertEqual(data["image"], FAKE_IMAGE)
        self.assertEqual(data["role"], "bridge")
        self.assertEqual(identity["pid"], 4242)

    def test_write_process_identity_rejects_bad_role(self) -> None:
        with self.assertRaises(ValueError):
            self.write_identity(role="root")
        self.assertFalse(self.identity_path.exists())

    def test_verified_identity_requires_matching_creation_time_and_image(self) -> None:
        self.write_identity()
        created_patch, image_patch = self.verify_env()
        with created_patch, image_patch:
            ok, reason = bot_ctl.verify_identity(bot_ctl.read_identity())
            self.assertTrue(ok, reason)
            self.assertEqual(bot_ctl.running_pid(), 4242)

        created_patch, image_patch = self.verify_env(created=222)
        with created_patch, image_patch:
            ok, reason = bot_ctl.verify_identity(bot_ctl.read_identity())
            self.assertFalse(ok)
            self.assertIn("creation time mismatch", reason)
            self.assertIsNone(bot_ctl.running_pid())

    def test_non_python_image_is_refused(self) -> None:
        self.write_identity(image=r"C:\Windows\notepad.exe")
        created_patch, image_patch = self.verify_env(image=r"C:\Windows\notepad.exe")
        with created_patch, image_patch:
            ok, reason = bot_ctl.verify_identity(bot_ctl.read_identity())
        self.assertFalse(ok)
        self.assertIn("image not python", reason)
        self.assertIsNone(bot_ctl.running_pid())

    def test_invalid_role_is_refused(self) -> None:
        self.identity_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": 4242,
                    "creation_time": 111,
                    "image": FAKE_IMAGE,
                    "role": "game",
                }
            ),
            encoding="utf-8",
        )
        created_patch, image_patch = self.verify_env()
        with created_patch, image_patch:
            ok, reason = bot_ctl.verify_identity(bot_ctl.read_identity())
        self.assertFalse(ok)
        self.assertIn("role", reason)

    def test_running_pid_ignores_legacy_run_pid(self) -> None:
        self.pid_path.write_text("5555", encoding="utf-8")
        with mock.patch.object(bot_ctl, "_alive", return_value=True):
            self.assertIsNone(bot_ctl.running_pid())
            self.assertEqual(bot_ctl.legacy_pid(), 5555)

    def test_missing_identity_is_not_verifiable(self) -> None:
        ok, reason = bot_ctl.verify_identity(None)
        self.assertFalse(ok)
        self.assertIn("missing", reason)


class StatusTests(BotCtlTestCase):
    def test_status_marks_legacy_pid_unverified(self) -> None:
        self.pid_path.write_text("5555", encoding="utf-8")
        with mock.patch.object(bot_ctl, "running_pid", return_value=None), \
                mock.patch.object(bot_ctl, "_alive", return_value=True), \
                mock.patch.object(
                    bot_ctl, "_ipc_status", return_value={"ok": False}
                ), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = bot_ctl.status()

        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("5555", text)
        self.assertIn("legacy run.pid", text)
        self.assertIn("unverified", text)

    def test_status_marks_stale_identity_unverified(self) -> None:
        self.write_identity()
        created_patch, image_patch = self.verify_env(created=999)
        with created_patch, image_patch, \
                mock.patch.object(
                    bot_ctl, "_ipc_status", return_value={"ok": False}
                ), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = bot_ctl.status()

        text = out.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("unverified", text)
        self.assertIn("creation time mismatch", text)

    def test_status_prints_recent_acks_for_ui(self) -> None:
        with mock.patch.object(bot_ctl, "running_pid", return_value=321), \
                mock.patch.object(
                    bot_ctl,
                    "_ipc_status",
                    return_value={
                        "ok": True,
                        "session": "s",
                        "snapshot": {"state": "PROBING"},
                        "stopped": False,
                        "acks": [
                            {"id": "c1", "status": "accepted", "message": ""},
                            {"id": "c2", "status": "executed", "message": "done"},
                        ],
                    },
                ), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = bot_ctl.status()

        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("acks=2", text)
        self.assertIn("c2=executed (done)", text)

    def test_status_stopped_without_ipc(self) -> None:
        with mock.patch.object(bot_ctl, "running_pid", return_value=None), \
                mock.patch.object(
                    bot_ctl,
                    "_ipc_status",
                    return_value={"ok": False, "message": "offline"},
                ), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = bot_ctl.status()
        self.assertEqual(rc, 1)


class EmergencyStopTests(BotCtlTestCase):
    def test_emergency_stop_taskkills_only_verified_identity(self) -> None:
        self.write_identity()
        created_patch, image_patch = self.verify_env()
        with created_patch, image_patch, \
                mock.patch.object(bot_ctl, "_alive", return_value=True), \
                mock.patch.object(bot_ctl.subprocess, "run") as run, \
                mock.patch.object(bot_ctl, "release_keys") as release, \
                mock.patch.object(bot_ctl, "_wait_pid_exit", return_value=True):
            rc = bot_ctl.emergency_stop()

        self.assertEqual(rc, 0)
        release.assert_called_once_with(focus=True)
        args = run.call_args[0][0]
        self.assertEqual(args[:3], ["taskkill", "/PID", "4242"])
        self.assertIn("/T", args)
        self.assertIn("/F", args)
        self.assertFalse(self.identity_path.exists())

    def test_emergency_stop_refuses_without_identity(self) -> None:
        with mock.patch.object(bot_ctl.subprocess, "run") as run, \
                mock.patch.object(bot_ctl, "release_keys") as release, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = bot_ctl.emergency_stop()

        self.assertEqual(rc, 1)
        run.assert_not_called()
        release.assert_called_once_with(focus=True)
        self.assertIn("拒绝", out.getvalue())
        self.assertIn("write_process_identity", out.getvalue())

    def test_emergency_stop_refuses_creation_time_mismatch(self) -> None:
        self.write_identity()
        created_patch, image_patch = self.verify_env(created=999)
        with created_patch, image_patch, \
                mock.patch.object(bot_ctl.subprocess, "run") as run, \
                mock.patch.object(bot_ctl, "release_keys"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = bot_ctl.emergency_stop()

        self.assertEqual(rc, 1)
        run.assert_not_called()
        self.assertIn("creation time mismatch", out.getvalue())
        self.assertTrue(self.identity_path.exists())

    def test_emergency_stop_refuses_non_python_image(self) -> None:
        self.write_identity(image=r"C:\Windows\notepad.exe")
        created_patch, image_patch = self.verify_env(
            image=r"C:\Windows\notepad.exe"
        )
        with created_patch, image_patch, \
                mock.patch.object(bot_ctl.subprocess, "run") as run, \
                mock.patch.object(bot_ctl, "release_keys"), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = bot_ctl.emergency_stop()

        self.assertEqual(rc, 1)
        run.assert_not_called()

    def test_emergency_stop_never_trusts_legacy_run_pid(self) -> None:
        self.pid_path.write_text("5555", encoding="utf-8")
        with mock.patch.object(bot_ctl, "_alive", return_value=True), \
                mock.patch.object(bot_ctl.subprocess, "run") as run, \
                mock.patch.object(bot_ctl, "release_keys"), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = bot_ctl.emergency_stop()

        self.assertEqual(rc, 1)
        run.assert_not_called()


class CommandDispatchTests(BotCtlTestCase):
    def test_unknown_command_never_toggles(self) -> None:
        with mock.patch.object(bot_ctl, "start") as start, \
                mock.patch.object(bot_ctl, "stop") as stop:
            rc = bot_ctl.main(["bogus-command"])
        self.assertEqual(rc, 2)
        start.assert_not_called()
        stop.assert_not_called()

    def test_toggle_only_no_args_or_explicit(self) -> None:
        with mock.patch.object(bot_ctl, "running_pid", return_value=None), \
                mock.patch.object(bot_ctl, "start", return_value=0) as start, \
                mock.patch.object(bot_ctl, "stop") as stop:
            self.assertEqual(bot_ctl.main([]), 0)
            self.assertEqual(bot_ctl.main(["toggle"]), 0)
        self.assertEqual(start.call_count, 2)
        stop.assert_not_called()

    def test_toggle_rejects_extra_args(self) -> None:
        with mock.patch.object(bot_ctl, "start") as start, \
                mock.patch.object(bot_ctl, "stop") as stop:
            rc = bot_ctl.main(["toggle", "extra"])
        self.assertEqual(rc, 2)
        start.assert_not_called()
        stop.assert_not_called()

    def test_ui_uses_current_python_foreground(self) -> None:
        with mock.patch.object(
            bot_ctl.subprocess, "call", return_value=0
        ) as call:
            rc = bot_ctl.main(["ui", "--demo"])
        self.assertEqual(rc, 0)
        cmd = call.call_args[0][0]
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(pathlib.Path(cmd[1]).name, "autofish_ui.py")
        self.assertIn("--demo", cmd)
        self.assertEqual(
            pathlib.Path(call.call_args[1]["cwd"]).resolve(), BOT.resolve()
        )


if __name__ == "__main__":
    unittest.main()
