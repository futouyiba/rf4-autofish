"""Pure model/layout tests for autofish_ui.

These tests never create a Tk root, open a socket, start the bot, inspect the
game screen or send input.  The Win32 helpers are only exercised indirectly by
layout arithmetic with explicit rectangles.
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import autofish_ui as ui  # noqa: E402


class ImportSideEffectTests(unittest.TestCase):
    def test_import_does_not_create_tk_or_threads(self) -> None:
        code = (
            "import sys, threading\n"
            "sys.path.insert(0, %r)\n"
            "import autofish_ui\n"
            "assert 'tkinter' not in sys.modules, 'tkinter imported eagerly'\n"
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


class PlacePanelTests(unittest.TestCase):
    def test_right_side_is_preferred(self) -> None:
        x, y = ui.place_panel((100, 100, 900, 700), [(0, 0, 1920, 1080)])
        self.assertEqual((x, y), (908, 108))

    def test_left_side_when_right_does_not_fit(self) -> None:
        x, y = ui.place_panel((1500, 100, 1900, 700), [(0, 0, 1920, 1080)])
        self.assertEqual((x, y), (1172, 108))

    def test_clamped_when_neither_side_fits(self) -> None:
        x, y = ui.place_panel((0, 0, 1920, 1080), [(0, 0, 1920, 1080)])
        self.assertEqual((x, y), (1592, 8))

    def test_no_game_floats_inside_work_area(self) -> None:
        x, y = ui.place_panel(None, [(0, 0, 1920, 1080)])
        self.assertEqual((x, y), (1592, 8))

    def test_negative_screen_coordinates(self) -> None:
        x, y = ui.place_panel(
            (-1920, 0, 0, 1080), [(-1920, 0, 0, 1080)]
        )
        self.assertEqual(x, -328)
        self.assertEqual(y, 8)

    def test_second_monitor_is_used_for_right_side(self) -> None:
        x, y = ui.place_panel(
            (0, 0, 1920, 1080),
            [(0, 0, 1920, 1080), (1920, 0, 3840, 1080)],
        )
        self.assertEqual((x, y), (1928, 8))

    def test_panel_larger_than_area_stays_anchored_not_crashing(self) -> None:
        x, y = ui.place_panel(
            (0, 0, 100, 100), [(0, 0, 200, 200)], 320, 640, 8
        )
        self.assertEqual((x, y), (8, 8))

    def test_dict_and_xywh_rects_are_accepted(self) -> None:
        x, y = ui.place_panel(
            {"left": 100, "top": 100, "right": 900, "bottom": 700},
            [{"x": 0, "y": 0, "width": 1920, "height": 1080}],
        )
        self.assertEqual((x, y), (908, 108))

    def test_empty_work_areas_do_not_raise(self) -> None:
        x, y = ui.place_panel((10, 20, 110, 120), [])
        self.assertIsInstance(x, int)
        self.assertIsInstance(y, int)

    def test_single_work_area_tuple_is_accepted(self) -> None:
        self.assertEqual(
            ui.place_panel((100, 100, 900, 700), (0, 0, 1920, 1080)),
            (908, 108),
        )

    def test_geometry_signs_support_negative_origins(self) -> None:
        self.assertEqual(ui._geometry_string(320, 640, -328, 8), "320x640-328+8")
        self.assertEqual(ui._geometry_string(320, 640, 100, 8), "320x640+100+8")


class ViewModelTests(unittest.TestCase):
    def test_missing_fields_are_not_fabricated(self) -> None:
        view = ui.build_view({})
        self.assertEqual(view.state, ui.MISSING)
        self.assertEqual(view.mode, ui.MISSING)
        self.assertEqual(view.reason, ui.MISSING)
        self.assertEqual(view.state_elapsed, ui.MISSING)
        self.assertEqual(view.confidence, ui.MISSING)
        self.assertEqual(view.bar, ui.MISSING)
        self.assertEqual(view.tier, ui.MISSING)
        self.assertEqual(view.desired, ui.MISSING)
        self.assertEqual(view.emitted, ui.MISSING)
        self.assertEqual(view.kept, ui.MISSING)
        self.assertEqual(view.last_error, ui.MISSING)
        self.assertEqual(view.heartbeat, ui.MISSING)

    def test_cur_has_explicit_upper_bound_29(self) -> None:
        self.assertEqual(ui.build_view({"cur": 7}).cur, "7 / 29")
        self.assertEqual(ui.build_view({"cur": 29}).cur, "29 / 29")
        self.assertIn("越界", ui.build_view({"cur": 30}).cur)
        self.assertIn("29", ui.build_view({}).cur)

    def test_ring_is_unknown_when_missing_or_none(self) -> None:
        self.assertEqual(ui.build_view({}).ring, "unknown")
        self.assertEqual(ui.build_view({"ring": None}).ring, "unknown")
        self.assertEqual(ui.build_view({"ring": 0.5}).ring, "0.50")

    def test_confidence_formatting(self) -> None:
        self.assertEqual(ui.build_view({"confidence": 0.876}).confidence, "0.88")
        self.assertEqual(ui.build_view({"confidence": float("nan")}).confidence, ui.MISSING)
        self.assertEqual(ui.build_view({"confidence": 87}).confidence, "87")

    def test_elapsed_formatting_and_no_cross_field_guessing(self) -> None:
        view = ui.build_view({"state_elapsed": 12.34})
        self.assertEqual(view.state_elapsed, "12.3s")
        self.assertEqual(view.fish_elapsed, ui.MISSING)
        only_fish = ui.build_view({"fish_elapsed": 99})
        self.assertEqual(only_fish.state_elapsed, ui.MISSING)
        self.assertEqual(only_fish.fish_elapsed, "99.0s")

    def test_desired_emitted_are_rendered_as_send_ledger(self) -> None:
        view = ui.build_view({"desired": {"F6", "F5"}, "emitted": []})
        self.assertEqual(view.desired, "[F5, F6]")
        self.assertEqual(view.emitted, "[]")
        text = "\n".join(ui.render_lines(view))
        self.assertIn("发送账", text)
        self.assertIn("不是游戏确认", text)

    def test_kept_with_total_when_both_are_present(self) -> None:
        self.assertEqual(ui.build_view({"kept": 3, "total": 20}).kept, "3 / 20")
        self.assertEqual(ui.build_view({"kept": 3}).kept, "3")

    def test_stopped_status_overrides_state_text(self) -> None:
        view = ui.build_view(
            {"state": "FIGHT", "stop_reason": "user"},
            {"ok": True, "stopped": True},
        )
        self.assertTrue(view.stopped)
        self.assertEqual(view.state, "已停止")
        self.assertEqual(view.reason, "user")

    def test_offline_status_is_text_not_only_color(self) -> None:
        view = ui.build_view(
            {"state": "FIGHT"}, {"ok": False, "message": "endpoint unavailable"}
        )
        self.assertFalse(view.connected)
        self.assertIn("离线", view.state)
        self.assertEqual(view.status_message, "endpoint unavailable")

    def test_nested_bar_and_state_dicts_do_not_shadow_scalars(self) -> None:
        view = ui.build_view(
            {
                "state": {"value": "FIGHT", "reason": "nested"},
                "bar": {"level": 4, "tier": 2},
            }
        )
        self.assertEqual(view.state, "FIGHT")
        self.assertEqual(view.reason, "nested")
        self.assertEqual(view.bar, "4")
        self.assertEqual(view.tier, "2")

    def test_net_pending_tristate(self) -> None:
        self.assertIs(ui.build_view({"net_pending": True}).net_pending_bool, True)
        self.assertEqual(ui.build_view({"net_pending": True}).net_pending, "是")
        self.assertEqual(ui.build_view({"net_pending": False}).net_pending, "否")
        self.assertEqual(
            ui.build_view({"state": "NET"}).net_pending, "未知"
        )
        self.assertEqual(ui.build_view({"state": "FIGHT"}).net_pending, ui.MISSING)


class ButtonConditionTests(unittest.TestCase):
    def test_confirm_net_uses_state_not_control_mode(self) -> None:
        self.assertTrue(ui.can_confirm_net_retracted(False, "NET", True))
        self.assertTrue(ui.can_confirm_net_retracted(False, "net", True))
        self.assertFalse(ui.can_confirm_net_retracted(False, "FIGHT", True))
        self.assertFalse(ui.can_confirm_net_retracted(False, "AUTO", True))
        self.assertFalse(ui.can_confirm_net_retracted(False, "NET", False))
        self.assertFalse(ui.can_confirm_net_retracted(False, "NET", None))

    def test_confirm_net_ignores_control_mode_auto(self) -> None:
        # mode=AUTO is the normal production value; it must not gate the button.
        view = ui.build_view(
            {"state": "NET", "mode": "AUTO", "net_pending": True},
            {"ok": True},
        )
        self.assertTrue(ui.button_states(view).confirm_net_retracted)

    def test_stopped_disables_confirmations(self) -> None:
        self.assertFalse(ui.can_confirm_ready(True, state="PROBING", mode="AUTO"))
        self.assertFalse(ui.can_confirm_net_retracted(True, "NET", True))

    def test_confirm_ready_requires_probing_or_ready(self) -> None:
        self.assertTrue(ui.can_confirm_ready(False, state="PROBING", mode="AUTO"))
        self.assertTrue(ui.can_confirm_ready(False, state="READY", mode="AUTO"))
        self.assertFalse(ui.can_confirm_ready(False, state="WAIT_BITE", mode="AUTO"))
        self.assertFalse(ui.can_confirm_ready(False, state="NET", mode="AUTO"))
        self.assertFalse(ui.can_confirm_ready(False))
        self.assertFalse(ui.can_confirm_ready(False, state=""))

    def test_confirm_ready_respects_non_auto_modes(self) -> None:
        for mode in ("OBSERVE", "MANUAL"):
            self.assertFalse(
                ui.can_confirm_ready(False, state="PROBING", mode=mode), mode
            )
        # BLOCKED without the narrow readiness_unknown evidence stays disabled.
        self.assertFalse(ui.can_confirm_ready(False, state="PROBING", mode="BLOCKED"))
        # Old snapshots without a control mode keep the button usable.
        self.assertTrue(ui.can_confirm_ready(False, state="PROBING"))

    def test_blocked_readiness_unknown_is_manually_confirmable(self) -> None:
        # Production: requested AUTO, readiness unknown -> control_mode BLOCKED.
        self.assertTrue(
            ui.can_confirm_ready(
                False,
                state="PROBING",
                mode="BLOCKED",
                reason="readiness_unknown",
                foreground=True,
            )
        )
        view = ui.build_view(
            {
                "state": "PROBING",
                "control_mode": "BLOCKED",
                "mode": "BLOCKED",
                "reason": "readiness_unknown",
                "foreground": True,
            },
            {"ok": True},
        )
        self.assertIs(view.foreground, True)
        self.assertTrue(ui.button_states(view).confirm_ready)

    def test_blocked_confirm_rejects_faults_and_foreground_loss(self) -> None:
        base = {
            "state": "PROBING",
            "mode": "BLOCKED",
            "reason": "readiness_unknown",
        }
        self.assertFalse(ui.can_confirm_ready(False, foreground=False, **base))
        self.assertFalse(ui.can_confirm_ready(False, foreground=None, **base))
        for reason in ("preflight_fault", "foreground_lost", "confirm_required"):
            self.assertFalse(
                ui.can_confirm_ready(
                    False, foreground=True, state="PROBING", mode="BLOCKED",
                    reason=reason,
                ),
                reason,
            )
        # BLOCKED confirmation is only for PROBING, never READY.
        self.assertFalse(
            ui.can_confirm_ready(
                False,
                state="READY",
                mode="BLOCKED",
                reason="readiness_unknown",
                foreground=True,
            )
        )

    def test_stopped_disables_command_buttons_but_not_start_or_emergency(self) -> None:
        view = ui.build_view(
            {"state": "NET", "mode": "AUTO", "net_pending": True},
            {"ok": True, "stopped": True},
        )
        states = ui.button_states(view)
        self.assertTrue(states.start_background)
        self.assertTrue(states.emergency_stop)
        self.assertFalse(states.resume)
        self.assertFalse(states.observe)
        self.assertFalse(states.stop)
        self.assertFalse(states.stop_after_fish)
        self.assertFalse(states.confirm_ready)
        self.assertFalse(states.confirm_net_retracted)

    def test_running_net_pending_enables_net_confirmation(self) -> None:
        view = ui.build_view(
            {"state": "NET", "mode": "AUTO", "net_pending": True}, {"ok": True}
        )
        states = ui.button_states(view)
        self.assertTrue(states.resume)
        self.assertTrue(states.observe)
        self.assertTrue(states.stop)
        self.assertFalse(states.confirm_ready)
        self.assertTrue(states.confirm_net_retracted)

    def test_stop_after_fish_disabled_when_already_requested(self) -> None:
        states = ui.button_states(
            ui.build_view({}, {"ok": True, "stop_after_fish": True})
        )
        self.assertFalse(states.stop_after_fish)
        self.assertTrue(states.stop)

    def test_offline_keeps_stop_clickable_to_report_offline(self) -> None:
        states = ui.button_states(ui.build_view({}, {"ok": False}))
        self.assertFalse(states.resume)
        self.assertFalse(states.observe)
        self.assertTrue(states.stop)
        self.assertFalse(states.confirm_ready)
        self.assertTrue(states.start_background)
        self.assertTrue(states.emergency_stop)


class ProductionSnapshotTests(unittest.TestCase):
    """Snapshot shape produced by LifecycleCoordinator.snapshot()."""

    def test_reason_column_maps_net_codes_to_chinese(self) -> None:
        # 旧 runtime 仍可能发 "net"；原因栏必须给出可读中文，而不是 code。
        view = ui.build_view(
            {"state": "NET", "control_mode": "AUTO", "reason": "net"},
            {"ok": True},
        )
        self.assertEqual(view.reason, "抄网（已松收线键、保持提竿）")

        view = ui.build_view(
            {
                "state": "LAND",
                "control_mode": "AUTO",
                "reason": "near_net_disabled",
            },
            {"ok": True},
        )
        self.assertEqual(
            view.reason, "临岸候选，自动抄网待验证；继续收线提竿"
        )

        # 未知 code 原样保留，不编造中文。
        view = ui.build_view({"reason": "readiness_unknown"}, {"ok": True})
        self.assertEqual(view.reason, "readiness_unknown")

    @staticmethod
    def snapshot(**overrides: object) -> dict:
        base = {
            "state": "NET",
            "control_mode": "AUTO",
            "mode": "AUTO",
            "reason": "net_pending",
            "state_elapsed": 1.5,
            "net_pending": True,
            "fish_id": "fish-1",
            "state_version": 7,
            "epoch": "e1",
            "cur": 12,
            "kept": 2,
            "heartbeat": 0.3,
        }
        base.update(overrides)
        return base

    def test_net_state_with_auto_mode_enables_net_confirm(self) -> None:
        view = ui.build_view(self.snapshot(), {"ok": True})
        self.assertEqual(view.state, "NET")
        self.assertEqual(view.state_raw, "NET")
        self.assertEqual(view.mode, "AUTO")
        self.assertEqual(view.net_pending, "是")
        states = ui.button_states(view)
        self.assertTrue(states.confirm_net_retracted)
        self.assertFalse(states.confirm_ready)
        self.assertTrue(states.resume)
        self.assertTrue(states.observe)
        self.assertTrue(states.stop)

    def test_net_state_without_pending_keeps_net_confirm_grey(self) -> None:
        view = ui.build_view(self.snapshot(net_pending=False), {"ok": True})
        self.assertEqual(view.net_pending, "否")
        self.assertFalse(ui.button_states(view).confirm_net_retracted)

    def test_net_state_with_unknown_pending_keeps_net_confirm_grey(self) -> None:
        snapshot = self.snapshot()
        snapshot.pop("net_pending")
        view = ui.build_view(snapshot, {"ok": True})
        self.assertEqual(view.net_pending, "未知")
        self.assertFalse(ui.button_states(view).confirm_net_retracted)

    def test_offline_or_stopped_production_snapshot_disables_confirm(self) -> None:
        offline = ui.button_states(ui.build_view(self.snapshot(), {"ok": False}))
        self.assertFalse(offline.confirm_net_retracted)
        self.assertFalse(offline.confirm_ready)
        stopped = ui.button_states(
            ui.build_view(self.snapshot(), {"ok": True, "stopped": True})
        )
        self.assertFalse(stopped.confirm_net_retracted)

    def test_probing_state_pending_false_enables_ready_confirm(self) -> None:
        view = ui.build_view(
            self.snapshot(
                state="PROBING", net_pending=False, fish_id=None, reason="probe"
            ),
            {"ok": True},
        )
        states = ui.button_states(view)
        self.assertTrue(states.confirm_ready)
        self.assertFalse(states.confirm_net_retracted)


class CommandPayloadTests(unittest.TestCase):
    SNAP = {"fish_id": "f1", "state_version": 12, "epoch": "e1"}

    def test_net_confirmation_attaches_all_context(self) -> None:
        payload = ui.command_payload("confirm_net_retracted", self.SNAP)
        self.assertEqual(
            payload,
            {"fish_id": "f1", "state_version": 12, "epoch": "e1"},
        )

    def test_resume_does_not_attach_fish_id(self) -> None:
        payload = ui.command_payload("resume", self.SNAP)
        self.assertEqual(payload, {"state_version": 12, "epoch": "e1"})

    def test_stop_has_no_context_payload(self) -> None:
        self.assertEqual(ui.command_payload("stop", self.SNAP), {})
        self.assertEqual(ui.command_payload("stop_after_fish", self.SNAP), {})

    def test_missing_context_is_not_fabricated(self) -> None:
        self.assertEqual(ui.command_payload("confirm_net_retracted", {}), {})
        self.assertEqual(ui.command_payload("resume", None), {})

    def test_nested_fish_and_session_epoch(self) -> None:
        payload = ui.command_payload(
            "confirm_net_retracted", {"fish": {"id": "nested"}, "session": {"epoch": 5}}
        )
        self.assertEqual(payload, {"fish_id": "nested", "epoch": 5})

    def test_empty_strings_and_bools_are_skipped(self) -> None:
        payload = ui.command_payload(
            "confirm_net_retracted",
            {"fish_id": "", "state_version": True, "epoch": "  "},
        )
        self.assertEqual(payload, {})


class AckTests(unittest.TestCase):
    def test_accepted_is_not_reported_as_executed(self) -> None:
        text = ui.ack_text({"status": "accepted"})
        self.assertIn("已接收待执行", text)
        self.assertNotIn("已执行", text)

    def test_executed_is_reported(self) -> None:
        self.assertTrue(ui.ack_text({"status": "executed"}).startswith("已执行"))

    def test_stop_transport_failure_can_say_offline(self) -> None:
        text = ui.ack_text(
            {"status": "rejected", "message": "endpoint unavailable"}, "stop"
        )
        self.assertIn("可能离线", text)

    def test_normal_rejection_is_not_called_offline(self) -> None:
        text = ui.ack_text(
            {"status": "rejected", "message": "session stopped"}, "stop"
        )
        self.assertIn("未执行", text)
        self.assertNotIn("可能离线", text)


class FinalAckTests(unittest.TestCase):
    """status['acks'] lookup for accepted → executed/rejected transitions."""

    ACCEPTED = {"id": "c1", "status": "accepted", "message": ""}

    def test_status_without_acks_is_reported_as_unsupported(self) -> None:
        self.assertIsNone(ui.status_acks({"ok": True, "snapshot": {}}))
        self.assertIsNone(ui.status_acks(None))
        self.assertIsNone(ui.final_ack({"ok": True}, "c1"))

    def test_acks_accepts_list_and_mapping_forms(self) -> None:
        executed = {"id": "c1", "status": "executed", "message": "ready"}
        self.assertEqual(
            ui.final_ack({"acks": [executed]}, "c1")["message"], "ready"
        )
        self.assertEqual(
            ui.final_ack({"acks": {"c1": executed}}, "c1")["status"], "executed"
        )
        self.assertEqual(ui.status_acks({"acks": []}), {})

    def test_final_ack_only_executed_or_rejected(self) -> None:
        self.assertIsNone(
            ui.final_ack({"acks": [self.ACCEPTED]}, "c1")
        )
        self.assertIsNone(
            ui.final_ack(
                {"acks": [{"id": "c1", "status": "unknown", "message": ""}]}, "c1"
            )
        )

    def test_resolve_pending_moves_final_acks_out(self) -> None:
        pending = {"c1": "confirm_ready", "c2": "stop"}
        status = {
            "acks": [
                {"id": "c1", "status": "executed", "message": "ready"},
                {"id": "c2", "status": "accepted", "message": ""},
            ]
        }
        resolved, remaining = ui.resolve_pending_acks(status, pending)
        self.assertEqual(list(resolved), ["c1"])
        self.assertEqual(resolved["c1"]["status"], "executed")
        self.assertEqual(remaining, {"c2": "stop"})

    def test_resolve_pending_keeps_everything_without_acks_field(self) -> None:
        pending = {"c1": "confirm_ready"}
        resolved, remaining = ui.resolve_pending_acks({"ok": True}, pending)
        self.assertEqual(resolved, {})
        self.assertEqual(remaining, pending)

    def test_final_ack_message_rendering(self) -> None:
        executed = {"id": "c1", "status": "executed", "message": "net_retracted"}
        self.assertIn(
            "已执行", ui.ack_text(ui.final_ack({"acks": [executed]}, "c1"))
        )
        rejected = {"id": "c1", "status": "rejected", "message": "stale_epoch"}
        text = ui.ack_text(ui.final_ack({"acks": [rejected]}, "c1"))
        self.assertIn("未执行", text)
        self.assertIn("stale_epoch", text)

    def test_ack_command_id_reads_id(self) -> None:
        self.assertEqual(ui.ack_command_id(self.ACCEPTED), "c1")
        self.assertIsNone(ui.ack_command_id({"status": "accepted"}))
        self.assertIsNone(ui.ack_command_id(None))


class NonActivateTests(unittest.TestCase):
    """The panel only ever styles its own window."""

    def test_style_flags_match_windows_constants(self) -> None:
        self.assertEqual(ui.WS_EX_NOACTIVATE, 0x08000000)
        self.assertEqual(ui.WS_EX_TOOLWINDOW, 0x00000080)
        self.assertEqual(
            ui.NOACTIVATE_EXSTYLE, ui.WS_EX_NOACTIVATE | ui.WS_EX_TOOLWINDOW
        )
        self.assertEqual(ui.SWP_NOACTIVATE, 0x0010)
        self.assertEqual(ui.HWND_TOPMOST, -1)

    def test_non_windows_is_a_harmless_noop(self) -> None:
        with mock.patch.object(ui.os, "name", "posix"):
            self.assertFalse(ui.make_panel_non_activating(1234))
            self.assertFalse(ui.place_panel_noactivate(1234, show=True))
        self.assertFalse(ui.make_panel_non_activating(0))
        self.assertFalse(ui.place_panel_noactivate(0, show=True))

    def test_exstyle_and_swp_noactivate_are_applied_to_own_hwnd(self) -> None:
        user32 = mock.Mock()
        user32.GetWindowLongPtrW.return_value = 0x00000010
        user32.SetWindowPos.return_value = 1
        with mock.patch.object(ui, "_user32", return_value=user32):
            ok = ui.make_panel_non_activating(0x1234)
        self.assertTrue(ok)
        args, _kwargs = user32.SetWindowLongPtrW.call_args
        self.assertEqual(args[0].value, 0x1234)
        self.assertEqual(args[1], ui.GWL_EXSTYLE)
        self.assertEqual(
            args[2] & ui.NOACTIVATE_EXSTYLE, ui.NOACTIVATE_EXSTYLE
        )
        self.assertEqual(
            args[2] & 0x00000010, 0x00000010, "existing style bits preserved"
        )
        swp = user32.SetWindowPos.call_args[0]
        self.assertEqual(swp[0].value, 0x1234)
        self.assertEqual(swp[1].value, 0xFFFFFFFFFFFFFFFF)
        self.assertTrue(swp[6] & ui.SWP_NOACTIVATE)
        self.assertTrue(swp[6] & 0x0002, "SWP_NOMOVE: position untouched")
        self.assertTrue(swp[6] & 0x0001, "SWP_NOSIZE: size untouched")

    def test_failed_win32_calls_degrade_to_false(self) -> None:
        user32 = mock.Mock()
        user32.GetWindowLongPtrW.side_effect = OSError("boom")
        with mock.patch.object(ui, "_user32", return_value=user32):
            self.assertFalse(ui.make_panel_non_activating(0x1234))
            user32.SetWindowPos.side_effect = OSError("boom")
            self.assertFalse(ui.place_panel_noactivate(0x1234, show=True))

    def test_no_user32_returns_false(self) -> None:
        with mock.patch.object(ui, "_user32", return_value=None):
            self.assertFalse(ui.make_panel_non_activating(0x1234))
            self.assertFalse(ui.place_panel_noactivate(0x1234))

    def test_tk_window_handle_parses_hex_and_int(self) -> None:
        hex_root = mock.Mock()
        hex_root.wm_frame.return_value = "0x1a2b"
        self.assertEqual(ui.tk_window_handle(hex_root), 0x1A2B)
        int_root = mock.Mock()
        int_root.wm_frame.return_value = 4321
        self.assertEqual(ui.tk_window_handle(int_root), 4321)
        fallback = mock.Mock()
        fallback.wm_frame.side_effect = RuntimeError("no frame")
        fallback.winfo_id.return_value = 99
        self.assertEqual(ui.tk_window_handle(fallback), 99)
        self.assertEqual(ui.tk_window_handle(object()), 0)


class DemoTests(unittest.TestCase):
    def test_demo_snapshot_mirrors_production_shape(self) -> None:
        # A fake mode="NET" used to hide the real state/mode split.
        snapshot = ui.demo_snapshot(18)
        self.assertTrue(snapshot["demo"])
        self.assertEqual(snapshot["state"], "NET")
        self.assertEqual(snapshot["mode"], "AUTO")
        self.assertEqual(snapshot["control_mode"], "AUTO")
        self.assertIs(snapshot["net_pending"], True)
        self.assertEqual(snapshot["cur"], 7)

    def test_demo_net_phase_enables_net_confirm(self) -> None:
        states = ui.button_states(ui.demo_snapshot(18))
        self.assertTrue(states.confirm_net_retracted)
        self.assertFalse(states.confirm_ready)

    def test_demo_source_never_opens_a_socket(self) -> None:
        source = ui.DemoSource()
        with mock.patch("socket.socket", side_effect=AssertionError("socket used")):
            status = source.status()
        self.assertTrue(status["ok"])
        self.assertTrue(status["snapshot"]["demo"])

    def test_demo_flag_is_opt_in(self) -> None:
        self.assertFalse(ui.parse_args([]).demo)
        self.assertTrue(ui.parse_args(["--demo"]).demo)


class HeadlessSmokeTests(unittest.TestCase):
    def test_headless_smoke_does_not_create_tk(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = ui.main(["--headless-smoke"])
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("NET pending", text)
        self.assertIn("29", text)
        self.assertIn("confirm_net=True", text)


class TkThreadSocketIsolationTests(unittest.TestCase):
    def test_model_paths_never_open_a_socket(self) -> None:
        """Everything the Tk thread calls must stay socket-free."""
        snapshot = ProductionSnapshotTests.snapshot()
        with mock.patch(
            "socket.socket", side_effect=AssertionError("socket on Tk thread")
        ), mock.patch(
            "socket.create_connection", side_effect=AssertionError("socket")
        ):
            view = ui.build_view(snapshot, {"ok": True, "acks": []})
            ui.render_lines(view)
            ui.button_states(view)
            ui.command_payload("confirm_net_retracted", snapshot)
            ui.ack_text({"status": "executed", "message": "ok"}, "resume")
            ui.status_acks({"acks": []})
            ui.resolve_pending_acks({"acks": []}, {"c1": "resume"})
            ui.place_panel((0, 0, 100, 100), [(0, 0, 1920, 1080)])
        self.assertEqual(view.state_raw, "NET")


if __name__ == "__main__":
    unittest.main()
