"""InputArbiter 的纯离线测试：不启动游戏、不截图、不调用真实输入库。

运行：
    python -m unittest discover -s tests -p "test_input_arbiter.py" -v
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import autofish_input as ain  # noqa: E402

F5 = ain.F5
F6 = ain.F6


class FakeSink:
    """记录 (key, is_down, reason)；可指定某次调用抛错或返回 False。"""

    def __init__(self, fail=None, false=None):
        self.calls: list[tuple[str, bool, str]] = []
        self.fail = fail          # callable(key, is_down) -> bool（抛异常）
        self.false = false        # callable(key, is_down) -> bool（明确返回 False）
        self.attempts: list[tuple[str, bool]] = []

    def __call__(self, key, is_down, reason):
        self.attempts.append((key, is_down))
        if self.fail is not None and self.fail(key, is_down):
            raise RuntimeError(f"boom:{key}:{'down' if is_down else 'up'}")
        if self.false is not None and self.false(key, is_down):
            return False
        self.calls.append((key, is_down, reason))
        return None

    @property
    def down_keys(self):
        return [k for k, d, _ in self.calls if d]

    @property
    def up_keys(self):
        return [k for k, d, _ in self.calls if not d]


class TestConstruction(unittest.TestCase):
    def test_requires_callable_sink(self):
        with self.assertRaises(TypeError):
            ain.InputArbiter(None)

    def test_starts_empty(self):
        arb = ain.InputArbiter(FakeSink())
        self.assertEqual(arb.desired, frozenset())
        self.assertEqual(arb.emitted, frozenset())
        self.assertEqual(arb.owned, frozenset())

    def test_set_desired_sends_nothing(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5, F6}, "fight")
        self.assertEqual(sink.calls, [])
        self.assertEqual(arb.desired, frozenset({F5, F6}))

    def test_bare_string_is_one_key(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink)
        arb.set_desired(F5, "one")
        arb.reconcile()
        self.assertEqual([(k, d) for k, d, _ in sink.calls], [(F5, True)])

    def test_duplicate_keys_are_deduped(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink)
        arb.set_desired([F5, F5, F5], "dup")
        arb.reconcile()
        self.assertEqual(sink.down_keys, [F5])


class TestReconcile(unittest.TestCase):
    def test_empty_to_both_keys_two_downs(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5, F6}, "fight")
        done = arb.reconcile()
        self.assertEqual(sink.down_keys, [F5, F6])
        self.assertEqual(arb.emitted, frozenset({F5, F6}))
        self.assertEqual(len(done), 2)

    def test_same_desired_is_idempotent(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5, F6}, "fight")
        arb.reconcile()
        before = len(sink.calls)
        done = arb.reconcile()
        self.assertEqual(done, ())
        self.assertEqual(len(sink.calls), before)

    def test_release_before_press(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5}, "a")
        arb.reconcile()
        sink.calls.clear()
        arb.set_desired({F6}, "b")
        arb.reconcile()
        self.assertEqual(
            [(k, d) for k, d, _ in sink.calls],
            [(F5, False), (F6, True)],
        )

    def test_only_diff_is_sent(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5, F6}, "both")
        arb.reconcile()
        sink.calls.clear()
        arb.set_desired({F5}, "drop-f6")
        arb.reconcile()
        self.assertEqual([(k, d) for k, d, _ in sink.calls], [(F6, False)])
        self.assertEqual(arb.emitted, frozenset({F5}))

    def test_order_is_deterministic(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink)
        arb.set_desired({"f9", F6, F5, "f7"}, "many")
        arb.reconcile()
        self.assertEqual(sink.down_keys, sorted([F5, F6, "f7", "f9"]))

    def test_key_order_override(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink, key_order=(F6, F5))
        arb.set_desired({F5, F6}, "fight")
        arb.reconcile()
        self.assertEqual(sink.down_keys, [F6, F5])

    def test_reason_is_passed_to_sink(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5}, "fight")
        arb.reconcile()
        self.assertEqual(sink.calls[-1][2], "fight")

    def test_reconcile_reason_override(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5}, "fight")
        arb.reconcile("explicit")
        self.assertEqual(sink.calls[-1][2], "explicit")


class TestFailures(unittest.TestCase):
    def test_down_failure_is_not_recorded_as_success(self):
        def fail(key, is_down):
            return key == F6 and is_down

        sink = FakeSink(fail=fail)
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5, F6}, "fight")
        with self.assertRaises(ain.InputDispatchError):
            arb.reconcile()
        self.assertEqual(arb.emitted, frozenset({F5}))
        self.assertEqual(sink.calls and [k for k, d, _ in sink.calls if d], [F5])

    def test_error_reports_completed_prefix(self):
        def fail(key, is_down):
            return key == F6 and is_down

        sink = FakeSink(fail=fail)
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5, F6}, "fight")
        with self.assertRaises(ain.InputDispatchError) as ctx:
            arb.reconcile()
        exc = ctx.exception
        self.assertEqual(exc.key, F6)
        self.assertTrue(exc.is_down)
        self.assertEqual(exc.completed, ((F5, True),))

    def test_error_is_chained_to_cause(self):
        sink = FakeSink(fail=lambda key, is_down: True)
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5}, "fight")
        with self.assertRaises(ain.InputDispatchError) as ctx:
            arb.reconcile()
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)

    def test_partial_failure_retry_only_remaining(self):
        state = {"fail": True}

        def fail(key, is_down):
            return state["fail"] and key == F6 and is_down

        sink = FakeSink(fail=fail)
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5, F6}, "fight")
        with self.assertRaises(ain.InputDispatchError):
            arb.reconcile()

        state["fail"] = False
        done = arb.reconcile()
        self.assertEqual(done, ((F6, True),))
        self.assertEqual(arb.emitted, frozenset({F5, F6}))
        # F5 没有被重新按下
        self.assertEqual([k for k, d in sink.attempts if d].count(F5), 1)

    def test_up_failure_keeps_ownership(self):
        state = {"fail": False}

        def fail(key, is_down):
            return state["fail"] and key == F5 and not is_down

        sink = FakeSink(fail=fail)
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5, F6}, "fight")
        arb.reconcile()

        state["fail"] = True
        arb.set_desired(set(), "release")
        with self.assertRaises(ain.InputDispatchError):
            arb.reconcile()
        self.assertEqual(arb.emitted, frozenset({F5, F6}))

    def test_up_failure_retry_completes(self):
        state = {"fail": True}

        def fail(key, is_down):
            return state["fail"] and key == F5 and not is_down

        sink = FakeSink(fail=fail)
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5, F6}, "fight")
        arb.reconcile()

        arb.set_desired(set(), "release")
        with self.assertRaises(ain.InputDispatchError):
            arb.reconcile()

        state["fail"] = False
        done = arb.reconcile()
        self.assertEqual(done, ((F5, False), (F6, False)))
        self.assertEqual(arb.emitted, frozenset())

    def test_no_infinite_retry_inside_one_call(self):
        sink = FakeSink(fail=lambda key, is_down: True)
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5, F6}, "fight")
        with self.assertRaises(ain.InputDispatchError):
            arb.reconcile()
        # 遇到第一个失败立刻停：只尝试了一次，没有在内部循环重试。
        self.assertEqual(len(sink.attempts), 1)
        self.assertEqual(arb.emitted, frozenset())


class TestExplicitFalse(unittest.TestCase):
    """sink 明确返回 False 必须与抛异常同等地视为失败。"""

    def test_false_down_is_failure_and_not_emitted(self):
        sink = FakeSink(false=lambda key, is_down: key == F6 and is_down)
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5, F6}, "fight")
        with self.assertRaises(ain.InputDispatchError) as ctx:
            arb.reconcile()
        exc = ctx.exception
        self.assertEqual(exc.key, F6)
        self.assertTrue(exc.is_down)
        self.assertEqual(exc.completed, ((F5, True),))
        self.assertEqual(arb.emitted, frozenset({F5}))
        self.assertEqual(sink.down_keys, [F5])

    def test_false_up_in_reconcile_keeps_ownership(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5, F6}, "fight")
        arb.reconcile()

        sink.false = lambda key, is_down: key == F5 and not is_down
        arb.set_desired(set(), "release")
        with self.assertRaises(ain.InputDispatchError) as ctx:
            arb.reconcile()
        self.assertEqual(ctx.exception.key, F5)
        self.assertFalse(ctx.exception.is_down)
        self.assertEqual(arb.emitted, frozenset({F5, F6}))

        sink.false = None
        done = arb.reconcile()
        self.assertEqual(done, ((F5, False), (F6, False)))
        self.assertEqual(arb.emitted, frozenset())

    def test_false_release_owned_keeps_ownership(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5, F6}, "fight")
        arb.reconcile()

        sink.false = lambda key, is_down: key == F5 and not is_down
        with self.assertRaises(ain.InputDispatchError) as ctx:
            arb.release_owned()
        self.assertEqual(ctx.exception.key, F5)
        self.assertEqual(ctx.exception.completed, ())
        self.assertEqual(arb.emitted, frozenset({F5, F6}))

        sink.false = None
        done = arb.release_owned()
        self.assertEqual(done, ((F5, False), (F6, False)))
        self.assertEqual(arb.emitted, frozenset())

    def test_false_partial_retry_only_remaining(self):
        state = {"fail": True}

        def false(key, is_down):
            return state["fail"] and key == F6 and is_down

        sink = FakeSink(false=false)
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5, F6}, "fight")
        with self.assertRaises(ain.InputDispatchError):
            arb.reconcile()
        self.assertEqual(arb.emitted, frozenset({F5}))

        state["fail"] = False
        done = arb.reconcile()
        self.assertEqual(done, ((F6, True),))
        self.assertEqual(arb.emitted, frozenset({F5, F6}))
        # F5 没有被重新按下
        self.assertEqual([k for k, d in sink.attempts if d].count(F5), 1)

    def test_none_and_true_are_success(self):
        for value in (None, True, 1, "ok"):
            with self.subTest(value=value):
                calls = []

                def sink(key, is_down, reason, value=value):
                    calls.append((key, is_down, reason))
                    return value

                arb = ain.InputArbiter(sink)
                arb.set_desired({F5, F6}, "fight")
                done = arb.reconcile()
                self.assertEqual(done, ((F5, True), (F6, True)))
                self.assertEqual(arb.emitted, frozenset({F5, F6}))
                self.assertEqual(arb.release_owned(), ((F5, False), (F6, False)))
                self.assertEqual(arb.emitted, frozenset())


class TestReleaseOwned(unittest.TestCase):
    def test_empty_release_is_noop(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink)
        self.assertEqual(arb.release_owned(), ())
        self.assertEqual(sink.calls, [])

    def test_release_only_owned_keys(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink)
        # 只成功按下 F5；F6 的 down 失败，不算 owned。
        sink.fail = lambda key, is_down: key == F6 and is_down
        arb.set_desired({F5, F6}, "fight")
        with self.assertRaises(ain.InputDispatchError):
            arb.reconcile()
        sink.fail = None
        sink.calls.clear()

        done = arb.release_owned()
        self.assertEqual(done, ((F5, False),))
        self.assertEqual(sink.up_keys, [F5])
        self.assertEqual(arb.emitted, frozenset())

    def test_release_is_idempotent(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5, F6}, "fight")
        arb.reconcile()
        arb.release_owned()
        before = len(sink.calls)
        self.assertEqual(arb.release_owned(), ())
        self.assertEqual(len(sink.calls), before)

    def test_release_clears_desired(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5, F6}, "fight")
        arb.reconcile()
        arb.release_owned()
        self.assertEqual(arb.desired, frozenset())
        before = len(sink.calls)
        arb.reconcile()
        self.assertEqual(len(sink.calls), before)

    def test_release_failure_retry(self):
        state = {"fail": True}

        def fail(key, is_down):
            return state["fail"] and key == F5 and not is_down

        sink = FakeSink(fail=fail)
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5, F6}, "fight")
        arb.reconcile()

        with self.assertRaises(ain.InputDispatchError):
            arb.release_owned()
        self.assertEqual(arb.emitted, frozenset({F5, F6}))

        state["fail"] = False
        done = arb.release_owned()
        self.assertEqual(done, ((F5, False), (F6, False)))
        self.assertEqual(arb.emitted, frozenset())

    def test_emitted_is_immutable_view(self):
        sink = FakeSink()
        arb = ain.InputArbiter(sink)
        arb.set_desired({F5}, "fight")
        arb.reconcile()
        view = arb.emitted
        self.assertIsInstance(view, frozenset)
        self.assertEqual(view, frozenset({F5}))


if __name__ == "__main__":
    unittest.main()
