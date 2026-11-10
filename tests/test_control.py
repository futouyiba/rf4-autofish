"""Offline tests for autofish_control (no game, no network)."""

from __future__ import annotations

import pathlib
import sys
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autofish_control import (  # noqa: E402
    COMMAND_QUEUE_MAX,
    AutoStop,
    ControlSession,
)
from autofish_snapshot import SnapshotStore  # noqa: E402


class ControlSessionTests(unittest.TestCase):
    def test_session_id_generated_and_custom(self) -> None:
        first = ControlSession()
        second = ControlSession()
        self.assertTrue(first.session_id)
        self.assertNotEqual(first.session_id, second.session_id)
        custom = ControlSession("my-session")
        self.assertEqual(custom.session_id, "my-session")

    def test_checkpoint_raises_auto_stop_with_reason(self) -> None:
        control = ControlSession("s")
        control.checkpoint()  # no stop yet
        control.request_stop("operator")
        self.assertTrue(control.stopped)
        with self.assertRaises(AutoStop) as raised:
            control.checkpoint()
        self.assertEqual(raised.exception.reason, "operator")
        self.assertIn("operator", str(raised.exception))

    def test_first_stop_reason_wins_and_is_thread_safe(self) -> None:
        control = ControlSession()
        barrier = threading.Barrier(16)
        errors = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=2.0)
                control.request_stop("reason-%d" % index)
            except Exception as exc:  # pragma: no cover - failure reporting
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2.0)

        self.assertFalse(errors)
        self.assertTrue(control.stopped)
        self.assertIn(control.stop_reason, {"reason-%d" % i for i in range(16)})
        # A later stop must not overwrite the first reason.
        control.request_stop("late")
        self.assertNotEqual(control.stop_reason, "late")

    def test_wait_is_interruptible(self) -> None:
        control = ControlSession()
        entered = threading.Event()
        result = {}

        def worker() -> None:
            entered.set()
            started = time.monotonic()
            result["stopped"] = control.wait(10.0)
            result["elapsed"] = time.monotonic() - started

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(entered.wait(timeout=1.0))
        time.sleep(0.05)
        control.request_stop("wake")
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        self.assertTrue(result["stopped"])
        self.assertLess(result["elapsed"], 1.0)

    def test_wait_timeout_returns_false(self) -> None:
        control = ControlSession()
        started = time.monotonic()
        self.assertFalse(control.wait(0.08, quantum=0.01))
        self.assertFalse(control.stopped)
        self.assertGreaterEqual(time.monotonic() - started, 0.05)

    def test_wait_zero_reports_current_stop_state(self) -> None:
        control = ControlSession()
        self.assertFalse(control.wait(0))
        control.request_stop()
        self.assertTrue(control.wait(0))

    def test_stop_after_fish_and_resume(self) -> None:
        control = ControlSession()
        self.assertFalse(control.stop_after_fish)
        control.request_stop_after_fish()
        self.assertTrue(control.stop_after_fish)
        self.assertFalse(control.stopped)
        self.assertTrue(control.resume())
        self.assertFalse(control.stop_after_fish)

        control.request_stop("hard")
        self.assertFalse(control.resume())
        self.assertFalse(control.stop_after_fish)
        self.assertTrue(control.stopped)

    def test_command_queue_is_bounded(self) -> None:
        control = ControlSession()
        for index in range(COMMAND_QUEUE_MAX):
            self.assertTrue(
                control.enqueue_command({"id": str(index), "name": "observe"})
            )
        self.assertEqual(control.command_count, COMMAND_QUEUE_MAX)
        self.assertFalse(control.enqueue_command({"id": "overflow", "name": "observe"}))
        self.assertEqual(len(control.consume_commands()), COMMAND_QUEUE_MAX)
        self.assertEqual(control.command_count, 0)

    def test_consume_commands_returns_deep_copies(self) -> None:
        control = ControlSession()
        original = {"id": "1", "payload": {"nested": [1, 2]}}
        control.enqueue_command(original)
        original["payload"]["nested"].append(99)

        commands = control.consume_commands()
        self.assertEqual(commands[0]["payload"]["nested"], [1, 2])
        commands[0]["payload"]["nested"].append(3)
        self.assertEqual(control.consume_commands(), [])

    def test_enqueue_rejects_non_dict(self) -> None:
        with self.assertRaises(TypeError):
            ControlSession().enqueue_command(["not", "a", "dict"])  # type: ignore[arg-type]


class SnapshotStoreTests(unittest.TestCase):
    def test_publish_and_read_are_deep_copies(self) -> None:
        store = SnapshotStore({"nested": {"values": [1, 2]}})
        store.snapshot()["nested"]["values"].append(9)
        self.assertEqual(store.snapshot()["nested"]["values"], [1, 2])

        source = {"nested": {"values": [3]}}
        store.publish(source)
        source["nested"]["values"].append(99)
        self.assertEqual(store.read()["nested"]["values"], [3])

        store.clear()
        self.assertEqual(store.get(), {})
        self.assertEqual(len(store), 0)

    def test_non_dict_rejected(self) -> None:
        with self.assertRaises(TypeError):
            SnapshotStore(["not", "a", "dict"])  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            SnapshotStore().publish("nope")  # type: ignore[arg-type]

    def test_concurrent_publish_and_snapshot_are_consistent(self) -> None:
        store = SnapshotStore({"left": 0, "right": 0})
        errors = []
        stop = threading.Event()

        def writer(seed: int) -> None:
            for index in range(200):
                value = seed * 1000 + index
                store.publish({"left": value, "right": value})

        def reader() -> None:
            while not stop.is_set():
                snap = store.snapshot()
                if snap.get("left") != snap.get("right"):
                    errors.append(snap)
                    return

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        readers = [threading.Thread(target=reader) for _ in range(4)]
        for thread in threads + readers:
            thread.start()
        for thread in threads:
            thread.join(timeout=2.0)
        stop.set()
        for thread in readers:
            thread.join(timeout=2.0)

        self.assertFalse(errors)
        snap = store.snapshot()
        self.assertEqual(snap["left"], snap["right"])


if __name__ == "__main__":
    unittest.main()
