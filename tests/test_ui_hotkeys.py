"""Hotkey tests for autofish_hotkeys and the side-panel integration.

No real RegisterHotKey call, game window, socket, Tk root or input is used.
The Win32 API, the clock and the Tk scheduler are replaced by small fakes.
"""

from __future__ import annotations

import pathlib
import queue
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import autofish_hotkeys as hk  # noqa: E402
import autofish_ui as ui  # noqa: E402

BUTTONS = ("resume", "observe", "stop", "stop_after_fish")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now


class FakeKeysApi:
    """Only the read-only GetAsyncKeyState surface used by the panel."""

    def __init__(self) -> None:
        self.down = set()
        self.calls = []

    def get_async_key_state(self, vk: int) -> int:
        self.calls.append(vk)
        return -32768 if vk in self.down else 0

    def release_all(self) -> None:
        self.down.clear()


class FakeHotkeyApi:
    """Scriptable user32 stand-in with a real blocking message queue."""

    def __init__(self, fail_ids=()) -> None:
        self.fail_ids = set(fail_ids)
        self.order = []
        self.register_calls = []
        self.unregister_calls = []
        self.registered = []
        self.posted = []
        self._messages: "queue.Queue" = queue.Queue()
        self._thread_id = 4242
        self.last_error = 0
        self.peek_gate = None
        self.keys = FakeKeysApi()

    def peek_message(self, msg, hwnd, low, high, flags):
        self.order.append("peek")
        if self.peek_gate is not None:
            self.peek_gate.wait(2.0)
        return 1

    def get_current_thread_id(self):
        return self._thread_id

    def register_hotkey(self, hwnd, hotkey_id, modifiers, vk):
        self.order.append(("register", hotkey_id))
        self.register_calls.append(
            (hwnd, hotkey_id, modifiers, vk)
        )
        if hotkey_id in self.fail_ids:
            self.last_error = hk.ERROR_HOTKEY_ALREADY_REGISTERED
            return False
        self.registered.append(hotkey_id)
        return True

    def unregister_hotkey(self, hwnd, hotkey_id):
        self.unregister_calls.append((hotkey_id, threading.get_ident()))
        return True

    def get_message(self, msg, hwnd, low, high):
        item = self._messages.get()
        if item is None:
            return 0
        message, wparam = item
        msg.message = message
        msg.wParam = wparam
        return 1

    def post_thread_message(self, thread_id, message, wparam=0, lparam=0):
        self.posted.append((thread_id, message))
        self._messages.put((message, wparam))
        return True

    def get_async_key_state(self, vk):
        return self.keys.get_async_key_state(vk)

    def get_last_error(self):
        return self.last_error

    def push_hotkey(self, hotkey_id):
        self._messages.put((hk.WM_HOTKEY, hotkey_id))


class FakeVar:
    def __init__(self, value: str = "") -> None:
        self.value = str(value)

    def set(self, value) -> None:
        self.value = str(value)

    def get(self) -> str:
        return self.value


class FakeLabel:
    def __init__(self) -> None:
        self.options = {}

    def configure(self, **kwargs) -> None:
        self.options.update(kwargs)


class FakeButton:
    def __init__(self) -> None:
        self.options = {}

    def configure(self, **kwargs) -> None:
        self.options.update(kwargs)


class FakeTk:
    NORMAL = "normal"
    DISABLED = "disabled"


class FakeRoot:
    """Records ``after`` callbacks so tests can drive them deterministically."""

    def __init__(self) -> None:
        self.callbacks = {}
        self.cancelled = []
        self._next = 1

    def after(self, delay, callback, *args):
        ident = "after#%d" % self._next
        self._next += 1
        self.callbacks[ident] = (delay, callback, args)
        return ident

    def after_cancel(self, ident):
        self.cancelled.append(ident)
        self.callbacks.pop(ident, None)

    def run(self, ident):
        _delay, callback, args = self.callbacks.pop(ident)
        callback(*args)


def wait_for(output, kind: str, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("no %r message in %.1fs" % (kind, timeout))
        item = output.get(timeout=remaining)
        if item[0] == kind:
            return item[1]


# ---------------------------------------------------------------------------
# Definitions / formatting
# ---------------------------------------------------------------------------


class DefinitionTests(unittest.TestCase):
    def test_all_keys_use_ctrl_alt_shift_and_norepeat(self) -> None:
        names = [binding.name for binding in hk.HOTKEY_BINDINGS]
        self.assertEqual(names, ["start_background", "resume", "observe", "stop_after_fish", "stop"])
        self.assertEqual(
            [binding.key for binding in hk.HOTKEY_BINDINGS],
            ["F6", "F7", "F8", "F9", "F10"],
        )
        for binding in hk.HOTKEY_BINDINGS:
            with self.subTest(binding=binding.name):
                self.assertEqual(
                    binding.modifiers,
                    hk.MOD_CONTROL
                    | hk.MOD_ALT
                    | hk.MOD_SHIFT
                    | hk.MOD_NOREPEAT,
                )
                self.assertIn("Ctrl+Alt+Shift", binding.combo)

    def test_hotkey_ids_are_unique(self) -> None:
        ids = [binding.hotkey_id for binding in hk.HOTKEY_BINDINGS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_button_labels_include_the_key(self) -> None:
        self.assertEqual(hk.with_hotkey_label("resume", "恢复自动"), "恢复自动 F7")
        self.assertEqual(hk.with_hotkey_label("stop", "立即停止"), "立即停止 F10")
        self.assertEqual(hk.with_hotkey_label("confirm_ready", "确认"), "确认")

    def test_report_shows_success_and_conflict(self) -> None:
        text = hk.format_report(
            {
                "enabled": True,
                "items": [
                    {"key": "F7", "ok": True},
                    {"key": "F8", "ok": False, "error": 1409},
                    {"key": "F9", "ok": False, "error": 5},
                ],
            }
        )
        self.assertIn("F7成功", text)
        self.assertIn("F8冲突", text)
        self.assertIn("F9失败(5)", text)

    def test_report_shows_disabled_reason(self) -> None:
        self.assertIn("演示模式", hk.format_report({"enabled": False, "reason": "演示模式"}))
        self.assertIn("非 Windows", hk.format_report({"enabled": False, "reason": "非 Windows"}))

    def test_registration_is_windows_only_and_off_in_demo(self) -> None:
        self.assertFalse(hk.registration_supported(demo=True))
        with mock.patch.object(hk.os, "name", "posix"):
            self.assertFalse(hk.registration_supported())

    def test_import_has_no_thread_or_tk_side_effects(self) -> None:
        code = (
            "import sys, threading\n"
            "sys.path.insert(0, %r)\n"
            "import autofish_hotkeys\n"
            "assert 'tkinter' not in sys.modules\n"
            "assert not any(t.name.startswith('autofish') "
            "for t in threading.enumerate()), threading.enumerate()\n"
            "print('ok')\n" % str(ROOT)
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)


# ---------------------------------------------------------------------------
# Listener thread
# ---------------------------------------------------------------------------


class ListenerTests(unittest.TestCase):
    def start_listener(self, api, timeout=2.0):
        output: "queue.Queue" = queue.Queue()
        listener = hk.HotkeyListener(output, api=api, clock=FakeClock())
        listener.start()
        self.addCleanup(lambda: listener.stop(0.5))
        report = wait_for(output, "hotkeys", timeout)
        self.assertTrue(listener.ready.wait(timeout))
        return listener, output, report

    def test_message_queue_is_created_before_registration(self) -> None:
        api = FakeHotkeyApi()
        listener, _output, report = self.start_listener(api)
        self.assertEqual(api.order[0], "peek")
        self.assertTrue(all(item[0] == "register" for item in api.order[1:]))
        self.assertTrue(report["enabled"])
        self.assertEqual(len(report["items"]), 5)
        listener.stop()
        self.assertFalse(listener.is_alive())

    def test_partial_registration_failure_keeps_other_keys(self) -> None:
        api = FakeHotkeyApi(fail_ids={0xA702})  # F8 conflict
        listener, output, report = self.start_listener(api)
        items = {item["name"]: item for item in report["items"]}
        self.assertFalse(items["observe"]["ok"])
        self.assertEqual(
            items["observe"]["error"], hk.ERROR_HOTKEY_ALREADY_REGISTERED
        )
        for name in ("resume", "stop_after_fish", "stop"):
            self.assertTrue(items[name]["ok"], name)

        api.push_hotkey(0xA701)
        event = wait_for(output, "hotkey")
        self.assertIsInstance(event, hk.HotkeyEvent)
        self.assertEqual(event.name, "resume")
        self.assertEqual(event.lifecycle, listener.lifecycle)
        self.assertEqual(event.hotkey_id, 0xA701)

    def test_events_carry_current_lifecycle_and_monotonic(self) -> None:
        api = FakeHotkeyApi()
        listener, output, _report = self.start_listener(api)
        api.push_hotkey(0xA704)
        event = wait_for(output, "hotkey")
        self.assertEqual(event.name, "stop")
        self.assertEqual(event.lifecycle, listener.lifecycle)
        self.assertEqual(event.monotonic, 100.0)

    def test_all_registrations_failing_exits_without_crashing(self) -> None:
        api = FakeHotkeyApi(fail_ids={binding.hotkey_id for binding in hk.HOTKEY_BINDINGS})
        listener, _output, report = self.start_listener(api)
        self.assertTrue(all(not item["ok"] for item in report["items"]))
        listener.join(2.0)
        self.assertFalse(listener.is_alive())
        self.assertEqual(api.unregister_calls, [])

    def test_exit_unregisters_in_the_listener_thread(self) -> None:
        api = FakeHotkeyApi(fail_ids={0xA702})
        listener, _output, _report = self.start_listener(api)
        listener.stop()
        self.assertFalse(listener.is_alive())
        self.assertEqual(
            sorted(item for item, _tid in api.unregister_calls),
            [0xA700, 0xA701, 0xA703, 0xA704],
        )
        for _item, thread_id in api.unregister_calls:
            self.assertEqual(thread_id, listener.ident)
        self.assertIn((api._thread_id, hk.WM_QUIT), api.posted)

    def test_stop_before_ready_is_handled(self) -> None:
        api = FakeHotkeyApi()
        api.peek_gate = threading.Event()
        output: "queue.Queue" = queue.Queue()
        listener = hk.HotkeyListener(output, api=api, clock=FakeClock())
        listener.start()
        listener.stop(timeout=0.0)  # queue/thread id not published yet
        api.peek_gate.set()
        listener.join(2.0)
        self.assertFalse(listener.is_alive())
        self.assertTrue(listener.ready.is_set())
        self.assertEqual(api.register_calls, [])
        self.assertEqual(api.unregister_calls, [])

    def test_stop_is_idempotent(self) -> None:
        api = FakeHotkeyApi()
        listener, _output, _report = self.start_listener(api)
        listener.stop()
        listener.stop()
        self.assertFalse(listener.is_alive())


# ---------------------------------------------------------------------------
# Panel integration
# ---------------------------------------------------------------------------


class PanelHarness:
    def __init__(
        self,
        view=None,
        *,
        lifecycle: int = 7,
        demo: bool = False,
    ) -> None:
        self.clock = FakeClock()
        panel = ui.AutoFishPanel.__new__(ui.AutoFishPanel)
        panel.demo = demo
        panel.root = FakeRoot()
        panel.tk = FakeTk()
        panel._closing = False
        panel._clock = self.clock
        panel._last_view = view or ui.build_view({"state": "WAIT_BITE"}, {"ok": True})
        panel._buttons = {name: FakeButton() for name in BUTTONS}
        panel.ack_var = FakeVar()
        panel.hotkey_var = FakeVar()
        panel.hotkey_label = FakeLabel()
        panel._hotkey_lifecycle = lifecycle
        panel._pending_resume = None
        panel._resume_deadline = 0.0
        panel._resume_poll_ms = 50
        panel._resume_release_timeout = 2.0
        panel._hotkey_max_age = 1.5
        panel._hotkey_api = FakeKeysApi()
        panel._hotkeys = None
        panel._output = queue.Queue()
        panel.sent = []

        def send(name: str) -> None:
            panel.sent.append(name)

        panel._send_command = send
        self.panel = panel

    def event(self, name: str, *, ts=None, lifecycle: int = 7):
        return hk.HotkeyEvent(
            name=name,
            lifecycle=lifecycle,
            monotonic=self.clock.now if ts is None else ts,
        )

    def press(self, name: str, **kwargs):
        self.panel._on_hotkey(self.event(name, **kwargs))

    def ack(self) -> str:
        return self.panel.ack_var.get()

    def button_state(self, name: str) -> str:
        return self.panel._buttons[name].options.get("state")


class PanelDispatchTests(unittest.TestCase):
    def test_hotkey_uses_button_gate_and_command_path(self) -> None:
        harness = PanelHarness()
        harness.press("observe")
        self.assertEqual(harness.panel.sent, ["observe"])

    def test_disabled_button_ignores_hotkey(self) -> None:
        stopped = ui.build_view({}, {"ok": True, "stopped": True})
        harness = PanelHarness(view=stopped)
        harness.press("resume")  # keys are up: gate runs immediately
        self.assertEqual(harness.panel.sent, [])
        self.assertIn("不可用", harness.ack())

    def test_disabled_resume_does_not_schedule_release_wait(self) -> None:
        stopped = ui.build_view({}, {"ok": True, "stopped": True})
        harness = PanelHarness(view=stopped)
        harness.panel._hotkey_api.down.update({hk.VK_CONTROL, hk.VK_F7})
        harness.press("resume")
        self.assertIsNone(harness.panel._pending_resume)
        self.assertEqual(harness.panel.sent, [])
        self.assertIn("不可用", harness.ack())

    def test_stop_after_fish_disabled_when_already_requested(self) -> None:
        view = ui.build_view({}, {"ok": True, "stop_after_fish": True})
        harness = PanelHarness(view=view)
        harness.press("stop_after_fish")
        self.assertEqual(harness.panel.sent, [])
        self.assertIn("不可用", harness.ack())

    def test_start_background_hotkey_launches_when_offline(self) -> None:
        offline = ui.build_view({}, {"ok": False, "message": "未连接"})
        harness = PanelHarness(view=offline)
        launched = []
        harness.panel._start_background = lambda: launched.append("start")
        harness.press("start_background")
        self.assertEqual(launched, ["start"])

    def test_start_background_hotkey_ignored_when_online(self) -> None:
        online = ui.build_view({"state": "WAIT_BITE"}, {"ok": True})
        harness = PanelHarness(view=online)
        launched = []
        harness.panel._start_background = lambda: launched.append("start")
        harness.press("start_background")
        self.assertEqual(launched, [])
        self.assertIn("已在运行", harness.ack())

    def test_expired_event_is_ignored(self) -> None:
        harness = PanelHarness()
        harness.press("observe", ts=harness.clock.now - 5.0)
        self.assertEqual(harness.panel.sent, [])
        self.assertIn("过期", harness.ack())

    def test_foreign_lifecycle_is_ignored(self) -> None:
        harness = PanelHarness()
        harness.press("stop", lifecycle=999)
        self.assertEqual(harness.panel.sent, [])
        self.assertIn("失效", harness.ack())

    def test_closing_panel_ignores_event(self) -> None:
        harness = PanelHarness()
        harness.panel._closing = True
        harness.press("stop")
        self.assertEqual(harness.panel.sent, [])
        self.assertEqual(harness.ack(), "")


class ResumeReleaseTests(unittest.TestCase):
    def test_resume_waits_for_ctrl_alt_shift_and_f7_release(self) -> None:
        harness = PanelHarness()
        keys = harness.panel._hotkey_api
        keys.down.update({hk.VK_CONTROL, hk.VK_MENU, hk.VK_SHIFT, hk.VK_F7})
        harness.press("resume")
        self.assertEqual(harness.panel.sent, [])
        self.assertIsNotNone(harness.panel._pending_resume)
        self.assertIn("等待", harness.ack())

        ident = harness.panel._pending_resume
        keys.release_all()
        harness.panel.root.run(ident)
        self.assertEqual(harness.panel.sent, ["resume"])
        self.assertIsNone(harness.panel._pending_resume)

    def test_resume_with_keys_already_released_is_immediate(self) -> None:
        harness = PanelHarness()
        harness.press("resume")
        self.assertEqual(harness.panel.sent, ["resume"])
        self.assertIsNone(harness.panel._pending_resume)

    def test_resume_release_timeout_cancels_and_refreshes_buttons(self) -> None:
        harness = PanelHarness()
        keys = harness.panel._hotkey_api
        keys.down.update({hk.VK_CONTROL, hk.VK_F7})
        harness.press("resume")
        ident = harness.panel._pending_resume
        self.assertIsNotNone(ident)

        # Status changed while waiting: a stopped panel must re-grey buttons.
        harness.panel._last_view = ui.build_view(
            {}, {"ok": True, "stopped": True}
        )
        harness.clock.now += 5.0
        harness.panel.root.run(ident)
        self.assertEqual(harness.panel.sent, [])
        self.assertIsNone(harness.panel._pending_resume)
        self.assertIn("超时", harness.ack())
        for name in BUTTONS:
            self.assertEqual(harness.button_state(name), FakeTk.DISABLED, name)

    def test_stop_preempts_pending_resume(self) -> None:
        harness = PanelHarness()
        harness.panel._hotkey_api.down.update({hk.VK_CONTROL, hk.VK_F7})
        harness.press("resume")
        ident = harness.panel._pending_resume
        self.assertIsNotNone(ident)

        harness.press("stop")
        self.assertEqual(harness.panel.sent, ["stop"])
        self.assertIn(ident, harness.panel.root.cancelled)
        self.assertIsNone(harness.panel._pending_resume)

    def test_observe_preempts_pending_resume(self) -> None:
        harness = PanelHarness()
        harness.panel._hotkey_api.down.update({hk.VK_CONTROL, hk.VK_F7})
        harness.press("resume")
        ident = harness.panel._pending_resume
        harness.press("observe")
        self.assertEqual(harness.panel.sent, ["observe"])
        self.assertIn(ident, harness.panel.root.cancelled)
        self.assertIsNone(harness.panel._pending_resume)

    def test_resume_does_not_block(self) -> None:
        harness = PanelHarness()
        harness.panel._hotkey_api.down.add(hk.VK_F7)
        started = time.monotonic()
        harness.press("resume")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.2)
        self.assertEqual(harness.panel.sent, [])


class PanelHotkeyStatusTests(unittest.TestCase):
    def test_partial_failure_is_visible_in_sidebar(self) -> None:
        harness = PanelHarness()
        harness.panel._apply_hotkey_report(
            {
                "enabled": True,
                "items": [
                    {"name": "resume", "key": "F7", "ok": True},
                    {
                        "name": "observe",
                        "key": "F8",
                        "ok": False,
                        "error": hk.ERROR_HOTKEY_ALREADY_REGISTERED,
                    },
                    {"name": "stop_after_fish", "key": "F9", "ok": True},
                    {"name": "stop", "key": "F10", "ok": True},
                ],
            }
        )
        text = harness.panel.hotkey_var.get()
        self.assertIn("F7成功", text)
        self.assertIn("F8冲突", text)
        self.assertEqual(harness.panel.hotkey_label.options.get("fg"), ui.AutoFishPanel.BAD)

    def test_demo_never_registers(self) -> None:
        harness = PanelHarness(demo=True)
        api = mock.Mock()
        harness.panel._hotkey_api = api
        harness.panel._start_hotkeys()
        self.assertIsNone(harness.panel._hotkeys)
        self.assertEqual(harness.panel._hotkey_lifecycle, 0)
        api.register_hotkey.assert_not_called()
        self.assertIn("演示模式", harness.panel.hotkey_var.get())

    def test_non_windows_never_registers(self) -> None:
        harness = PanelHarness()
        api = mock.Mock()
        harness.panel._hotkey_api = api
        with mock.patch.object(
            ui.hotkeys, "registration_supported", return_value=False
        ):
            harness.panel._start_hotkeys()
        self.assertIsNone(harness.panel._hotkeys)
        api.register_hotkey.assert_not_called()
        self.assertIn("非 Windows", harness.panel.hotkey_var.get())

    def test_stop_hotkeys_invalidates_and_stops_listener(self) -> None:
        harness = PanelHarness()
        harness.panel._hotkey_api.down.add(hk.VK_F7)
        harness.press("resume")
        ident = harness.panel._pending_resume
        self.assertIsNotNone(ident)

        listener = mock.Mock()
        harness.panel._hotkeys = listener
        harness.panel._stop_hotkeys()
        self.assertIsNone(harness.panel._hotkeys)
        self.assertEqual(harness.panel._hotkey_lifecycle, 0)
        self.assertIsNone(harness.panel._pending_resume)
        self.assertIn(ident, harness.panel.root.cancelled)
        listener.stop.assert_called_once()

        # A queued event from the old listener can no longer act.
        harness.press("stop")
        self.assertEqual(harness.panel.sent, [])

    def test_start_hotkeys_passes_api_and_clock(self) -> None:
        harness = PanelHarness()
        harness.panel.demo = False
        fake_api = FakeHotkeyApi()
        harness.panel._hotkey_api = fake_api
        with mock.patch.object(
            ui.hotkeys, "registration_supported", return_value=True
        ):
            harness.panel._start_hotkeys()
        listener = harness.panel._hotkeys
        self.assertIsNotNone(listener)
        self.addCleanup(lambda: listener.stop(0.5))
        self.assertEqual(harness.panel._hotkey_lifecycle, listener.lifecycle)
        report = wait_for(harness.panel._output, "hotkeys")
        self.assertTrue(report["enabled"])
        self.assertEqual(len(fake_api.register_calls), 5)


if __name__ == "__main__":
    unittest.main()
