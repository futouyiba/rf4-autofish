"""RF4 自动钓鱼 —— 唯一输入仲裁器（状态机阶段 2，纯离线）。

本模块只做一件事：把"希望哪些键按住"翻译成**差量**按键事件，并记住
**自己真正发成功过什么**。它不 import 任何真实输入库，不读系统全局键状态，
不截图、不聚焦窗口、不关心游戏窗口。真实 sink 由调用方注入：

    def sink(key: str, is_down: bool, reason: str) -> Any:
        ...  # 真的按下去；失败必须抛异常或明确返回 False

所有权（ownership）只来自本对象成功发出的 keyDown 记录，**绝不**来自
`GetAsyncKeyState` 之类的系统状态轮询 —— 系统状态只能回答"这个键此刻是否
按下"，不能回答"是谁按下的"。把系统状态当成所有权，会在用户还压着键时
发出 keyUp，正是设计文档 §3.2 描述的不可靠场景。

几条硬约束（与设计文档 §5 对齐）：

1. `reconcile()` 只发送差集；先释放在按下前，同一组内按 key 排序，顺序确定。
2. 只有 sink **成功返回**之后才更新 `emitted`。成功指正常返回 `None`/`True`
   （或任何非 `False` 的值）；sink **明确返回 `False`** 与抛异常一样视为
   失败，立刻抛 `InputDispatchError`，不更新 `emitted`。已完成的事件保留在
   error 里，**不假装整个事务成功**。
3. `release_owned()` 只释放本对象成功发过 keyDown 的键；从未成功按下的键
   不会被发 keyUp。
4. 相同 desired 重复 reconcile 产生 0 个事件。
5. 失败不吞、不重试循环：一次调用遇到第一个失败就停，保留剩余差集，
   下一次 reconcile / release 可以继续重试。
6. 所有发送都走同一个私有 `_dispatch`，避免某条路径漏掉 "False = 失败"。

`emitted` 只是**本进程成功发出的按键账本**，不是"游戏确实收到/确实按住"
的确认；本模块无法证明后者。
"""

from __future__ import annotations

import typing

__all__ = [
    "F5",
    "F6",
    "InputArbiter",
    "InputDispatchError",
]

# 本阶段唯一接管的两个键。用字符串而不是枚举，方便 sink 直接映射到
# pydirectinput / SendInput 的键名；本模块不认识任何真实库。
F5 = "f5"
F6 = "f6"


class InputDispatchError(RuntimeError):
    """一次 sink 调用失败（抛异常，或明确返回 `False`）。

    `completed` 是**本次调用内**已经成功发送的事件（按发送顺序），调用方
    可以据此判断"事务做到哪一步"。失败的那一条不在 completed 里，也没有
    写进 arbiter 的 emitted —— 状态机和 arbiter 的事实保持一致。
    """

    def __init__(
        self,
        key: str,
        is_down: bool,
        reason: str,
        completed: typing.Iterable[tuple[str, bool]] = (),
    ) -> None:
        self.key = key
        self.attempted_key = key
        self.is_down = is_down
        self.reason = reason
        self.completed = tuple(completed)
        action = "down" if is_down else "up"
        done = ", ".join(
            f"{k}:{'down' if d else 'up'}" for k, d in self.completed
        ) or "none"
        super().__init__(
            f"input sink failed for {key} {action} ({reason}); "
            f"completed before failure: {done}"
        )


class InputArbiter:
    """声明式按键不变量 -> 差量事件的唯一 owner（纯内存）。

    典型用法：

        arb = InputArbiter(sink)
        arb.set_desired({F5, F6}, "fight")
        arb.reconcile()

    任何业务代码都只能改 desired，不能直接调 sink。`desired` 是"希望"，
    `emitted` 是"本对象确实按下去且没释放的键"，两者之差就是待发送动作。
    """

    def __init__(
        self,
        sink: typing.Callable[[str, bool, str], typing.Any],
        *,
        key_order: typing.Sequence[str] | None = None,
    ) -> None:
        """`sink` 必须注入；没有默认真实输入库。

        sink 返回 `False` 表示**明确失败**（与抛异常等价），`None`/`True`
        表示成功。`key_order` 可选，用来固定同一组内的事件顺序（例如
        (F5, F6)）。未列出的键排在后面并按字符串排序，保证顺序确定、可复现。
        """
        if not callable(sink):
            raise TypeError("InputArbiter requires a callable sink(key, is_down, reason)")
        self._sink = sink
        self._key_order = tuple(dict.fromkeys(key_order or ()))
        self._desired: set[str] = set()
        self._emitted: set[str] = set()
        self._reason = ""

    # ---------- 只读视图 ----------

    @property
    def desired(self) -> frozenset[str]:
        return frozenset(self._desired)

    @property
    def desired_keys(self) -> frozenset[str]:
        return frozenset(self._desired)

    @property
    def emitted(self) -> frozenset[str]:
        """本 arbiter 成功发过 keyDown 且尚未成功 keyUp 的键。"""
        return frozenset(self._emitted)

    @property
    def emitted_keys(self) -> frozenset[str]:
        return frozenset(self._emitted)

    @property
    def owned(self) -> frozenset[str]:
        return frozenset(self._emitted)

    # ---------- 声明 ----------

    def set_desired(
        self,
        keys: typing.Iterable[str] | str,
        reason: str = "",
    ) -> None:
        """设置期望按住的键集合，**不发送任何事件**。

        传单个字符串会被当成一个键（`"f5"` 而不是拆成 `"f"`/`"5"`）。
        重复设置相同集合是幂等的；reason 会被记住，供后续无参 reconcile 使用。
        """
        if isinstance(keys, str):
            new_keys = {keys}
        else:
            new_keys = set(keys)
        self._desired = new_keys
        self._reason = reason

    # ---------- 发送 ----------

    def _ordered(self, keys: typing.Iterable[str]) -> list[str]:
        """确定顺序：key_order 优先，其余按字符串排序。"""
        rank = {k: i for i, k in enumerate(self._key_order)}
        return sorted(keys, key=lambda k: (rank.get(k, len(rank)), str(k)))

    def _dispatch(
        self,
        key: str,
        is_down: bool,
        reason: str,
        done: list[tuple[str, bool]],
    ) -> None:
        """唯一发送入口：把 sink 的异常与显式 `False` 统一成领域失败。

        `done` 是本次调用已经成功的事件；失败的那一条不会被追加，也不会
        写进 `emitted`。`None`/`True`（以及其他非 `False` 返回值）视为成功，
        兼容旧 sink。
        """
        try:
            result = self._sink(key, is_down, reason)
        except Exception as exc:  # noqa: BLE001 - 统一包装成领域错误
            raise InputDispatchError(key, is_down, reason, done) from exc
        if result is False:
            raise InputDispatchError(key, is_down, reason, done)

    def reconcile(self, reason: str | None = None) -> tuple[tuple[str, bool], ...]:
        """把 desired 与 emitted 的差集发出去，返回本次成功的事件。

        顺序固定为：**先释放**（emitted 有、desired 没有），**后按下**
        （desired 有、emitted 没有）。遇到第一个 sink 异常或显式 `False`
        就停并抛 `InputDispatchError`；成功的前缀已经写进 emitted，剩余差集
        留给下一次 reconcile 重试。
        """
        if reason is None:
            reason = self._reason

        to_release = self._ordered(self._emitted - self._desired)
        to_press = self._ordered(self._desired - self._emitted)
        done: list[tuple[str, bool]] = []

        for key in to_release:
            self._dispatch(key, False, reason, done)
            self._emitted.discard(key)
            done.append((key, False))

        for key in to_press:
            self._dispatch(key, True, reason, done)
            self._emitted.add(key)
            done.append((key, True))

        return tuple(done)

    def release_owned(self, reason: str = "release") -> tuple[tuple[str, bool], ...]:
        """释放本 arbiter 已成功按下的全部键，并清空 desired。

        只对 `emitted` 里的键发 keyUp；从未成功 down 的键不会被碰。
        desired 一并清空，避免紧接着的 reconcile 把刚释放的键又按回去。
        失败（异常或 sink 明确返回 `False`）时已成功的释放保留、失败的键
        仍在 emitted 里，可再次调用重试。
        """
        self._desired.clear()
        self._reason = reason

        done: list[tuple[str, bool]] = []
        for key in self._ordered(self._emitted):
            self._dispatch(key, False, reason, done)
            self._emitted.discard(key)
            done.append((key, False))
        return tuple(done)
