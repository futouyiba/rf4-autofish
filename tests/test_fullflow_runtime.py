"""运行宿主 + UI 快照契约的离线测试（无 socket、无截图、无输入）。

运行：
    python -m unittest tests.test_fullflow_runtime -v
"""

from __future__ import annotations

import pathlib
import sys
import threading
import unittest

BOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT))

import autofish_lifecycle as alf  # noqa: E402
import autofish_observation as aobs  # noqa: E402
import autofish_runtime as art  # noqa: E402
import autofish_ui as ui  # noqa: E402

T = aobs.TriState
S = alf.AutoFishState


class Clock:
    def __init__(self, now: float = 500.0, step: float = 0.02):
        self.now = now
        self.step = step

    def __call__(self) -> float:
        self.now = round(self.now + self.step, 9)
        return self.now


class FakeServer:
    """只记录；不监听端口。"""

    def __init__(self, commands=()):
        self.published: list[dict] = []
        self.acks: list[tuple[str, str, str]] = []
        self.closed = 0
        self._commands = [dict(item) for item in commands]

    def publish(self, snapshot):
        self.published.append(dict(snapshot))

    def drain_commands(self):
        commands = self._commands
        self._commands = []
        return commands

    def acknowledge(self, cid, status, message=""):
        self.acks.append((cid, status, message))

    def close(self):
        self.closed += 1


class FakeSink:
    def __init__(self, fail_ups: int = 0):
        self.calls: list[tuple[str, bool, str]] = []
        self.fail_ups = fail_ups
        self.up_attempts = 0

    def __call__(self, key, is_down, reason):
        self.calls.append((key, is_down, reason))
        if not is_down:
            self.up_attempts += 1
            if self.fail_ups:
                self.fail_ups -= 1
                raise RuntimeError("keyUp failed")


class Callbacks:
    def __init__(self, *, sink=None, readiness=True, fail_calibrate=False, friction=None):
        self.clock = Clock()
        self.sink = sink or FakeSink()
        self.readiness = readiness
        self.fail_calibrate = fail_calibrate
        self.calls: list[str] = []
        self.logs: list[str] = []
        self.friction = (
            dict(friction) if friction is not None else {"cur": 17, "calibrated": True}
        )
        self.keep_result = True

        self.cb = alf.RuntimeCallbacks(
            capture_observation=self._capture,
            input_sink=self.sink,
            readiness=lambda obs: self.readiness,
            calibrate=self._calibrate,
            cast=self._cast,
            keep=self._keep,
            dismiss=lambda control: True,
            net_action=lambda: None,
            friction_step=lambda level: None,
            friction_snapshot=lambda: dict(self.friction),
            keepnet_full=lambda: False,
            log=self.logs.append,
            clock=self.clock,
        )

    def _capture(self):
        return aobs.Observation(
            monotonic_ts=self.clock(),
            wall_ts=0.0,
            foreground=True,
            fish_icon=T.FALSE,
            bar_level=None,
            bar_tier=None,
            bar_hue=None,
            bar_sat=None,
            ring_ratio=None,
            captured=T.UNKNOWN,
            ok_modal=T.UNKNOWN,
            hud_visible=T.UNKNOWN,
            evidence={},
        )

    def _calibrate(self, control, fight=False):
        self.calls.append("calibrate")
        if self.fail_calibrate:
            raise RuntimeError("calibration exploded")

    def _cast(self, control):
        self.calls.append("cast")

    def _keep(self, control):
        self.calls.append("keep")
        return self.keep_result


CONFIG = alf.LifecycleConfig(tick_interval=0.001)


class RuntimeLoopTests(unittest.TestCase):
    def test_publishes_every_tick_and_final_snapshot(self) -> None:
        server = FakeServer()
        callbacks = Callbacks()
        runtime = art.AutoFishRuntime(
            callbacks.cb, config=CONFIG, initial_auto=False, server=server
        )
        snap = runtime.run(max_ticks=5)
        self.assertEqual(len(server.published), 6)  # 5 tick + 退出时 1
        self.assertIs(runtime.state, S.STOPPED)
        self.assertEqual(server.published[-1]["state"], "STOPPED")
        self.assertEqual(snap["state"], "STOPPED")
        self.assertTrue(any("BOOT -> PROBING" in line for line in callbacks.logs))
        self.assertTrue(any("STOPPING -> STOPPED" in line for line in callbacks.logs))
        for item in server.published:
            for field in alf.SNAPSHOT_FIELDS:
                self.assertIn(field, item)

    def test_drain_commands_and_forward_executed_ack(self) -> None:
        server = FakeServer([{"id": "r1", "name": "resume"}])
        runtime = art.AutoFishRuntime(
            Callbacks().cb, config=CONFIG, initial_auto=False, server=server
        )
        runtime.run(max_ticks=3)
        statuses = [status for cid, status, _ in server.acks if cid == "r1"]
        self.assertIn("accepted", statuses)
        self.assertIn("executed", statuses)

    def test_stop_event_terminates_run(self) -> None:
        server = FakeServer()
        runtime = art.AutoFishRuntime(
            Callbacks().cb, config=CONFIG, initial_auto=False, server=server
        )
        stop = threading.Event()
        stop.set()
        snap = runtime.run(stop, max_ticks=10)
        self.assertIs(runtime.state, S.STOPPED)
        self.assertTrue(snap["stopped"])
        self.assertGreaterEqual(len(server.published), 1)

    def test_fault_keeps_publishing_until_stop(self) -> None:
        callbacks = Callbacks(fail_calibrate=True)
        server = FakeServer()
        runtime = art.AutoFishRuntime(
            callbacks.cb, config=CONFIG, initial_auto=True, server=server
        )
        runtime.run(max_ticks=10)
        states = [item["state"] for item in server.published]
        self.assertIn("FAULT", states)
        fault_at = states.index("FAULT")
        self.assertGreater(len(states) - fault_at, 1)  # 故障后继续发布
        self.assertEqual(runtime.snapshot()["state"], "STOPPED")

    def test_shutdown_is_bounded_to_three_release_attempts(self) -> None:
        sink = FakeSink(fail_ups=99)
        callbacks = Callbacks(sink=sink)
        runtime = art.AutoFishRuntime(callbacks.cb, config=CONFIG, initial_auto=False)
        runtime.coordinator.machine.start()  # 正常按住 F5+F6
        self.assertTrue(runtime.coordinator.arbiter.emitted)
        ok = runtime.shutdown()
        self.assertFalse(ok)
        self.assertIs(runtime.state, S.STOPPED)
        self.assertEqual(sink.up_attempts, 3)

    def test_close_is_idempotent(self) -> None:
        server = FakeServer()
        runtime = art.AutoFishRuntime(
            Callbacks().cb, config=CONFIG, initial_auto=False, server=server
        )
        runtime.close()
        runtime.close()
        self.assertEqual(server.closed, 1)
        self.assertIs(runtime.state, S.STOPPED)


class SnapshotContractTests(unittest.TestCase):
    def test_ui_reads_state_mode_reason_friction_and_net_fields(self) -> None:
        runtime = art.AutoFishRuntime(
            Callbacks().cb, config=CONFIG, initial_auto=True
        )
        runtime.tick()
        snap = runtime.tick()
        status = {"ok": True, "stopped": False, "session": snap["session"]}

        view = ui.build_view(snap, status)
        self.assertIn(view.state, {"PROBING", "READY"})
        self.assertIn(view.mode, {"AUTO", "BLOCKED"})
        self.assertNotEqual(view.reason, ui.MISSING)
        self.assertEqual(view.cur, "17 / 29")
        self.assertEqual(view.desired, "[]")
        self.assertEqual(view.emitted, "[]")
        self.assertEqual(view.net_pending_bool, False)
        self.assertEqual(view.kept, "%d" % snap["kept"])

        payload = ui.command_payload("confirm_net_retracted", snap)
        self.assertEqual(payload["epoch"], snap["epoch"])
        self.assertEqual(payload["state_version"], snap["state_version"])

    def test_net_pending_true_in_net(self) -> None:
        # 直接构造 NET 快照的内部状态代价高；这里验证字段语义与 UI 的映射：
        # NET 状态 + net_pending=True 必须显示为「是」。
        snap = {
            "state": "NET",
            "control_mode": "AUTO",
            "reason": "net",
            "net_pending": True,
            "state_elapsed": 1.0,
            "kept": 0,
            "desired_keys": ["f6"],
            "emitted_keys": ["f6"],
        }
        view = ui.build_view(snap, {"ok": True, "stopped": False})
        self.assertEqual(view.net_pending_bool, True)
        self.assertEqual(view.net_pending, "是")

    def test_external_friction_false_is_not_shown_as_calibrated(self) -> None:
        callbacks = Callbacks(friction={"cur": 17, "calibrated": False})
        runtime = art.AutoFishRuntime(
            callbacks.cb, config=CONFIG, initial_auto=True
        )
        runtime.tick()
        snap = runtime.tick()
        self.assertFalse(snap["friction"]["calibrated"])
        self.assertEqual(snap["confidence"], 0.0)

        view = ui.build_view(
            snap, {"ok": True, "stopped": False, "session": snap["session"]}
        )
        self.assertEqual(view.confidence, "0.00")


if __name__ == "__main__":
    unittest.main()
