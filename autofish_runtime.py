"""国钓斧头助手 —— 运行宿主：主线程 tick 循环 + IPC 发布/命令回执。

这一层只做三件事，不碰游戏、不抓屏、不按键：

1. 在主线程反复调用 :meth:`LifecycleCoordinator.step`（``run(stop_event)``）；
2. 每个 tick 把快照 ``publish`` 给 :class:`autofish_ipc.RuntimeServer`；
3. 把 ``drain_commands()`` 取到的命令交给协调器，并把回执
   ``acknowledge`` 回去。

适配层（adapter bridge）只需要实现 :class:`RuntimeCallbacks`，构造本对象并
在独立线程/主线程调用 ``run(stop_event)``，不再调用上游 Player 循环。

``server`` 是**注入**的：本模块不会自己绑定端口。测试传 fake，生产传真实的
``RuntimeServer``，因此导入/构造本模块没有任何网络副作用。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Mapping, Optional

from autofish_control import ControlSession
from autofish_lifecycle import (
    AutoFishState,
    LifecycleConfig,
    LifecycleCoordinator,
    RuntimeCallbacks,
)

__all__ = ["AutoFishRuntime"]


class AutoFishRuntime:
    """协调器 + 可选 IPC 宿主；``run()`` 是唯一阻塞入口。"""

    def __init__(
        self,
        callbacks: RuntimeCallbacks,
        control: Optional[ControlSession] = None,
        *,
        config: Optional[LifecycleConfig] = None,
        initial_auto: bool = False,
        server: Any = None,
        epoch: Optional[str] = None,
        session_timer: Any = None,
    ) -> None:
        if not isinstance(callbacks, RuntimeCallbacks):
            raise TypeError("AutoFishRuntime requires RuntimeCallbacks")
        self._control = control if control is not None else ControlSession()
        self._config = config or LifecycleConfig()
        # 会话时限计时器（可选）：由宿主注入，None 时本功能完全不启用。
        self._session_timer = session_timer
        self._winddown_pending = False
        self._coordinator = LifecycleCoordinator(
            callbacks,
            self._control,
            config=self._config,
            initial_auto=initial_auto,
            epoch=epoch,
        )
        self._server = server
        self._latest: dict[str, Any] = self._coordinator.snapshot()
        self._running = False
        self._closed = False

    # ------------------------------------------------------------------
    # 只读视图
    # ------------------------------------------------------------------
    @property
    def coordinator(self) -> LifecycleCoordinator:
        return self._coordinator

    @property
    def control(self) -> ControlSession:
        return self._control

    @property
    def server(self) -> Any:
        return self._server

    @property
    def state(self) -> AutoFishState:
        return self._coordinator.state

    @property
    def running(self) -> bool:
        return self._running

    def snapshot(self) -> dict[str, Any]:
        return self._latest

    # ------------------------------------------------------------------
    # 单 tick（也供适配层自己做循环时复用）
    # ------------------------------------------------------------------
    def tick(self) -> dict[str, Any]:
        commands = None
        if self._server is not None:
            try:
                commands = self._server.drain_commands()
            except Exception:  # noqa: BLE001 - IPC 故障不能改变钓鱼安全边界
                commands = None
        snapshot = self._coordinator.step(commands)
        self._tick_session(snapshot)
        self._flush_acks()
        self._publish(snapshot)
        return snapshot

    def _tick_session(self, snapshot: dict[str, Any]) -> None:
        """会话时限：AUTO 运行时累计，否则暂停；到期发一次 stop_after_fish。

        只在注入了 session_timer 时生效；所有判断不碰 lifecycle 状态机。
        """
        timer = self._session_timer
        if timer is None:
            return
        now = float(self._clock())
        auto = bool(getattr(self._coordinator, "auto_requested", False))
        state = getattr(self._coordinator.state, "name", self._coordinator.state)
        active = auto and str(state).upper() not in {"STOPPED", "STOPPING", "FAULT"}
        if active:
            timer.resume(now)
        else:
            timer.pause(now)
        expired = timer.expired(now)
        if expired and not self._winddown_pending:
            # 到时只锁存"钓完最后一条"，绝不强停丢鱼。
            self._control.request_stop_after_fish()
            self._winddown_pending = True
            self._write_winddown_request()
        snapshot["session_status"] = {
            "limit_seconds": timer.limit,
            "elapsed_seconds": round(timer.elapsed_seconds(now), 1),
            "remaining_seconds": round(timer.remaining(now), 1),
            "state": timer.state,
            "expired": expired,
            "winddown_pending": self._winddown_pending,
        }

    def _write_winddown_request(self) -> None:
        """到时限时写触发文件，供面板在 bot 退出后拉起收尾（不依赖 IPC）。"""
        try:
            import json  # noqa: PLC0415
            import os  # noqa: PLC0415
            import app_paths  # noqa: PLC0415
            p = app_paths.data_path("winddown_request.json")
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"requested_wall": time.strftime("%H:%M:%S"),
                           "reason": "session_limit"}, f, ensure_ascii=False)
            os.replace(tmp, p)
        except Exception:
            pass

    @property
    def _clock(self):
        return getattr(self._coordinator, "_clock", time.time)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(
        self,
        stop_event: Optional[threading.Event] = None,
        max_ticks: Optional[int] = None,
    ) -> dict[str, Any]:
        """阻塞运行，直到 STOPPED / ``stop_event`` / ``max_ticks``。

        FAULT 不退出：继续发布快照，等待用户 ``stop``（IPC 侧仍能看到状态）。
        退出时执行有界释放（最多 ``shutdown_retries`` 次），然后发布最终快照。
        """
        if self._running:
            return self._latest
        if self._closed:
            raise RuntimeError("runtime already closed")
        self._running = True
        ticks = 0
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    self._control.request_stop("stop_event")
                self._latest = self.tick()
                ticks += 1
                if self._coordinator.state is AutoFishState.STOPPED:
                    break
                if max_ticks is not None and ticks >= int(max_ticks):
                    break
                self._control.wait(self._config.tick_interval)
        finally:
            self.shutdown()
            self._latest = self._coordinator.snapshot()
            # 最终快照也带上会话状态，面板才能在 bot 退出后看到收尾标志。
            self._tick_session(self._latest)
            self._publish(self._latest)
            self._running = False
        return self._latest

    def stop(self, reason: str = "runtime_stop") -> None:
        """线程/宿主安全：只置停止位，真正的释放由主循环的下一 tick 完成。"""
        self._control.request_stop(reason)

    def shutdown(self, attempts: Optional[int] = None) -> bool:
        """有界停止：最多尝试 ``attempts``（默认配置）次释放，不会无限重试。"""
        return self._coordinator.stop_now("shutdown", attempts=attempts)

    def close(self) -> None:
        """停止并（如果注入了 server）关闭它；可重复调用。"""
        if self._closed:
            return
        self.shutdown()
        self._closed = True
        server = self._server
        close = getattr(server, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # IPC 辅助
    # ------------------------------------------------------------------
    def _flush_acks(self) -> None:
        server = self._server
        if server is None:
            self._coordinator.take_acks()
            return
        for ack in self._coordinator.take_acks():
            cid = ack.get("id")
            if not cid:
                continue
            try:
                server.acknowledge(cid, ack["status"], ack.get("message", ""))
            except Exception:  # noqa: BLE001 - UI 回执失败不影响控制
                pass

    def _publish(self, snapshot: Mapping[str, Any]) -> None:
        server = self._server
        if server is None:
            return
        try:
            server.publish(dict(snapshot))
        except Exception:  # noqa: BLE001
            pass
