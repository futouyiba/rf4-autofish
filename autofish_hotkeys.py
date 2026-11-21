# -*- coding: utf-8 -*-
"""Global hotkeys for the autofish side panel.

The panel registers four system-wide hotkeys through ``RegisterHotKey`` with a
``NULL`` window and ``MOD_NOREPEAT``:

    Ctrl+Alt+Shift+F7  resume
    Ctrl+Alt+Shift+F8  observe
    Ctrl+Alt+Shift+F9  stop_after_fish
    Ctrl+Alt+Shift+F10 stop

Only a dedicated listener thread touches Win32.  It creates its own message
queue first, registers the bindings, and then forwards ``("hotkey", event)``
tuples to the caller's queue.  It never touches Tk, sockets or input; the Tk
thread decides whether an event is still current and routes it through the
same button gate and command path as a mouse click.

Importing this module has no side effects: no DLL is loaded, no thread starts
and no key state is read.  On non-Windows platforms registering is disabled
and :func:`registration_supported` returns ``False``.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# -- Win32 constants --------------------------------------------------------

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000

ERROR_HOTKEY_ALREADY_REGISTERED = 1409

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_F5 = 0x74
VK_F6 = 0x75
VK_F7 = 0x76
VK_F8 = 0x77
VK_F9 = 0x78
VK_F10 = 0x79
VK_F11 = 0x7A

# Modifiers shared by every binding.  MOD_NOREPEAT stops the OS auto-repeat
# while a key is held; the panel additionally waits for the resume combo to be
# released before sending "resume".
_HOTKEY_MODIFIERS = MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT


@dataclass(frozen=True)
class HotkeyBinding:
    name: str
    key: str
    combo: str
    hotkey_id: int
    vk: int
    modifiers: int = _HOTKEY_MODIFIERS


HOTKEY_BINDINGS: Tuple[HotkeyBinding, ...] = (
    # F5 = 回到游戏：前台丢失后显式请求聚焦，走按钮门，不直接动作。
    # 单键 F5 仍是游戏抬竿键；这里只注册三修饰键组合，不影响游戏。
    HotkeyBinding("refocus", "F5", "Ctrl+Alt+Shift+F5", 0xA705, VK_F5),
    # 启动后台放在最前：面板独占 F6，组合键不会影响游戏里的单独 F6(抬竿)。
    HotkeyBinding(
        "start_background", "F6", "Ctrl+Alt+Shift+F6", 0xA700, VK_F6
    ),
    HotkeyBinding("resume", "F7", "Ctrl+Alt+Shift+F7", 0xA701, VK_F7),
    HotkeyBinding("observe", "F8", "Ctrl+Alt+Shift+F8", 0xA702, VK_F8),
    HotkeyBinding(
        "stop_after_fish", "F9", "Ctrl+Alt+Shift+F9", 0xA703, VK_F9
    ),
    HotkeyBinding("stop", "F10", "Ctrl+Alt+Shift+F10", 0xA704, VK_F10),
    # 录制开关（开始/结束）：录制时人要走位，不方便回头点面板。
    # 用 F11 而非 F12：F12 已被录制进程自身用作停止检键。
    HotkeyBinding("nav_record", "F11", "Ctrl+Alt+Shift+F11", 0xA706, VK_F11),
)

HOTKEY_KEYS: Dict[str, str] = {binding.name: binding.key for binding in HOTKEY_BINDINGS}
HOTKEY_COMBOS: Dict[str, str] = {
    binding.name: binding.combo for binding in HOTKEY_BINDINGS
}

# The resume release wait watches the whole combo: modifiers and F7.
RESUME_RELEASE_KEYS: Tuple[int, ...] = (VK_CONTROL, VK_MENU, VK_SHIFT, VK_F7)


class MSG(ctypes.Structure):
    """Win32 ``MSG`` with explicit field types (x64 layout)."""

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint32),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32),
        ("pt_x", ctypes.c_int32),
        ("pt_y", ctypes.c_int32),
    ]


@dataclass(frozen=True)
class HotkeyEvent:
    """A hotkey press forwarded to the Tk thread.

    ``lifecycle`` invalidates events that outlive their listener (panel closed
    or restarted) and ``monotonic`` lets the panel reject stale queue entries.
    """

    name: str
    lifecycle: int
    monotonic: float
    hotkey_id: int = 0


_LIFECYCLE_LOCK = threading.Lock()
_LIFECYCLE_NEXT = 0


def new_lifecycle_id() -> int:
    global _LIFECYCLE_NEXT
    with _LIFECYCLE_LOCK:
        _LIFECYCLE_NEXT += 1
        return _LIFECYCLE_NEXT


def registration_supported(demo: bool = False) -> bool:
    """Whether global registration may be attempted in this process."""
    return not demo and os.name == "nt"


HOTKEY_PREFIX = "Ctrl+Alt+Shift"

# 面板顶部统一声明一次前缀；按钮里只标 F 键，避免每个按钮重复 17 个字符。
# 「勿单按」是防误操作提示：游戏里单按 F7 是抄网键。
# 两行都控制在标签 wraplength=300 内，不会折成三行。
HOTKEY_LEGEND = "热键前缀 %s（勿单按 F 键）" % HOTKEY_PREFIX
HOTKEY_KEYS_LINE = "F6启动/F7恢复/F8观察/F9收尾/F10停止/F11录制"


def combo_label(name: str) -> str:
    """组合键全称：``Ctrl+Alt+Shift+F6``。"""
    key = HOTKEY_KEYS.get(str(name))
    return "%s+%s" % (HOTKEY_PREFIX, key) if key else ""


def with_hotkey_label(name: str, text: str) -> str:
    """按钮只标裸 F 键，如 ``恢复自动 (F7)``。

    面板宽 320px、按钮两列，每列文字只有约 144px；把完整的
    ``Ctrl+Alt+Shift+F7`` 拼进每个按钮会被裁断。前缀由顶部
    :func:`format_report` 统一声明一次，那里同时写明「勿单按」，
    避免用户在游戏里单按 F7（那是抄网键）造成误操作。
    """
    key = HOTKEY_KEYS.get(str(name))
    return "%s (%s)" % (text, key) if key else str(text)


def format_report(report: Any) -> str:
    """Sidebar summary of every registration attempt.

    前缀只写一次：按钮标签里是裸 F 键，靠这行说明它们都要配上
    ``Ctrl+Alt+Shift``，且不能单按。
    """
    if not isinstance(report, Mapping):
        return "热键：—"
    if not report.get("enabled"):
        reason = str(report.get("reason") or "未启用").strip() or "未启用"
        return "热键：未启用（%s）" % reason
    items = report.get("items")
    if not isinstance(items, (list, tuple)) or not items:
        return "热键：未注册"
    ok_count = sum(
        1 for item in items
        if isinstance(item, Mapping) and item.get("ok"))
    if ok_count == len(items):
        return HOTKEY_LEGEND + "\n" + HOTKEY_KEYS_LINE
    parts: List[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        # listener 上报 name（resume/observe/...）；历史/测试数据只有 key
        name = str(item.get("name") or item.get("key") or "?")
        key = HOTKEY_KEYS.get(name) or str(item.get("key") or name)
        if item.get("ok"):
            parts.append("%s✓" % key)
        elif item.get("error") == ERROR_HOTKEY_ALREADY_REGISTERED:
            parts.append("%s冲突" % key)
        else:
            parts.append("%s✗" % key)
    return HOTKEY_LEGEND + " " + " ".join(parts) if parts else "热键：未注册"


def resume_combo_down(api: "Win32HotkeyApi") -> bool:
    """Read-only ``GetAsyncKeyState`` probe for the resume combo."""
    for vk in RESUME_RELEASE_KEYS:
        if int(api.get_async_key_state(vk)) & 0x8000:
            return True
    return False


class Win32HotkeyApi:
    """Thin ``user32`` wrapper with explicit prototypes.

    Instantiating the class is the only place that loads a DLL, which keeps
    module import side-effect free and non-Windows imports safe.
    """

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("RegisterHotKey is only available on Windows")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32 = user32
        self._kernel32 = kernel32

        user32.RegisterHotKey.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        user32.RegisterHotKey.restype = ctypes.c_int

        user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.UnregisterHotKey.restype = ctypes.c_int

        user32.GetMessageW.argtypes = [
            ctypes.POINTER(MSG),
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        user32.GetMessageW.restype = ctypes.c_int

        user32.PeekMessageW.argtypes = [
            ctypes.POINTER(MSG),
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        user32.PeekMessageW.restype = ctypes.c_int

        user32.PostThreadMessageW.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        user32.PostThreadMessageW.restype = ctypes.c_int

        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = ctypes.c_uint32

        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short

    def register_hotkey(
        self, hwnd: Any, hotkey_id: int, modifiers: int, vk: int
    ) -> bool:
        return bool(
            self._user32.RegisterHotKey(
                ctypes.c_void_p(hwnd or 0),
                int(hotkey_id),
                int(modifiers),
                int(vk),
            )
        )

    def unregister_hotkey(self, hwnd: Any, hotkey_id: int) -> bool:
        return bool(
            self._user32.UnregisterHotKey(
                ctypes.c_void_p(hwnd or 0), int(hotkey_id)
            )
        )

    def get_message(self, msg: MSG, hwnd: Any, low: int, high: int) -> int:
        return int(
            self._user32.GetMessageW(
                ctypes.byref(msg), ctypes.c_void_p(hwnd or 0), int(low), int(high)
            )
        )

    def peek_message(
        self, msg: MSG, hwnd: Any, low: int, high: int, flags: int
    ) -> int:
        return int(
            self._user32.PeekMessageW(
                ctypes.byref(msg),
                ctypes.c_void_p(hwnd or 0),
                int(low),
                int(high),
                int(flags),
            )
        )

    def post_thread_message(
        self, thread_id: int, message: int, wparam: int = 0, lparam: int = 0
    ) -> bool:
        return bool(
            self._user32.PostThreadMessageW(
                int(thread_id), int(message), int(wparam), int(lparam)
            )
        )

    def get_current_thread_id(self) -> int:
        return int(self._kernel32.GetCurrentThreadId())

    def get_async_key_state(self, vk: int) -> int:
        return int(self._user32.GetAsyncKeyState(int(vk)))

    def get_last_error(self) -> int:
        return int(ctypes.get_last_error())


class HotkeyListener(threading.Thread):
    """Register global hotkeys and forward presses to *output*.

    Lifecycle of a run:

    1. ``PeekMessageW`` creates this thread's message queue and publishes the
       thread id, so :meth:`stop` can always wake ``GetMessageW`` afterwards.
    2. Bindings are registered one by one; a failure is reported per item and
       never aborts the remaining hotkeys.
    3. ``GetMessageW`` dispatches ``WM_HOTKEY`` as queue events.
    4. ``finally`` unregisters every successfully registered id on this same
       thread.

    ``stop`` before step 1 still works: the stop flag is checked immediately
    after the queue exists (the "stop-before-ready" race).
    """

    def __init__(
        self,
        output: Any,
        *,
        api: Optional[Any] = None,
        bindings: Sequence[HotkeyBinding] = HOTKEY_BINDINGS,
        clock: Optional[Any] = None,
    ) -> None:
        super().__init__(name="autofish-ui-hotkeys", daemon=True)
        self._output = output
        if api is None:
            api = Win32HotkeyApi()
        self._api = api
        self._bindings = tuple(bindings)
        self._clock = clock or time.monotonic
        self.lifecycle = new_lifecycle_id()
        self.ready = threading.Event()
        self._stop_event = threading.Event()
        self._thread_id = 0
        self._registered: Dict[int, HotkeyBinding] = {}
        self._by_id = {binding.hotkey_id: binding for binding in self._bindings}

    # -- public ---------------------------------------------------------
    def stop(self, timeout: float = 1.0) -> None:
        """Ask the listener to exit and wake it with ``PostThreadMessageW``.

        Safe to call before the message queue exists: the flag is observed as
        soon as the queue is created.  Never call from the listener thread.
        """
        self._stop_event.set()
        if threading.current_thread() is self:
            return
        try:
            limit = max(0.0, float(timeout))
        except (TypeError, ValueError):
            limit = 1.0
        deadline = time.monotonic() + limit
        while self.is_alive():
            if self._thread_id:
                try:
                    self._api.post_thread_message(self._thread_id, WM_QUIT)
                except Exception:
                    pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.join(min(0.05, remaining))

    # -- thread body ----------------------------------------------------
    def run(self) -> None:
        try:
            if self._stop_event.is_set():
                return
            try:
                # Step 1: force the thread message queue into existence before
                # ready is published or any registration happens.
                self._api.peek_message(MSG(), 0, 0, 0, PM_NOREMOVE)
                self._thread_id = int(self._api.get_current_thread_id())
            except Exception as exc:  # noqa: BLE001 - reported in the panel
                self._emit_report(
                    {"enabled": False, "reason": "消息队列失败：%s" % exc, "items": []}
                )
                return
            finally:
                self.ready.set()
            if self._stop_event.is_set():
                return
            items = self._register_all()
            self._emit_report({"enabled": True, "items": items})
            if self._stop_event.is_set() or not self._registered:
                return
            self._pump()
        except Exception as exc:  # noqa: BLE001 - listener must never crash Tk
            self._emit_report(
                {"enabled": False, "reason": "监听异常：%s" % exc, "items": []}
            )
        finally:
            self._unregister_all()
            self.ready.set()

    def _register_all(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for binding in self._bindings:
            if self._stop_event.is_set():
                break
            ok = False
            error = 0
            message = ""
            try:
                ok = bool(
                    self._api.register_hotkey(
                        None, binding.hotkey_id, binding.modifiers, binding.vk
                    )
                )
                if not ok:
                    error = int(self._api.get_last_error())
            except Exception as exc:  # noqa: BLE001 - one bad key is isolated
                message = "%s: %s" % (type(exc).__name__, exc)
            if ok:
                self._registered[binding.hotkey_id] = binding
            items.append(
                {
                    "name": binding.name,
                    "key": binding.key,
                    "combo": binding.combo,
                    "ok": ok,
                    "error": error,
                    "message": message,
                }
            )
        return items

    def _pump(self) -> None:
        msg = MSG()
        while not self._stop_event.is_set():
            try:
                result = int(self._api.get_message(msg, 0, 0, 0))
            except Exception:
                return
            if result == 0 or result == -1:
                return
            message = int(msg.message)
            if message == WM_QUIT:
                return
            if message != WM_HOTKEY:
                continue
            binding = self._by_id.get(int(msg.wParam))
            if binding is None or self._stop_event.is_set():
                continue
            self._output.put(
                (
                    "hotkey",
                    HotkeyEvent(
                        name=binding.name,
                        lifecycle=self.lifecycle,
                        monotonic=float(self._clock()),
                        hotkey_id=binding.hotkey_id,
                    ),
                )
            )

    def _emit_report(self, report: Mapping[str, Any]) -> None:
        try:
            self._output.put(("hotkeys", dict(report)))
        except Exception:
            pass

    def _unregister_all(self) -> None:
        for hotkey_id in list(self._registered):
            try:
                self._api.unregister_hotkey(None, hotkey_id)
            except Exception:
                pass
        self._registered.clear()
