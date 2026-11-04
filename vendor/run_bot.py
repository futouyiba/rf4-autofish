"""RF4S 启动器（适配本机 rf4_x64.exe 客户端）。

本客户端（Unity, rf4_x64.exe）只接受 pydirectinput 的鼠标事件，
完全忽略 pyautogui 的 mouse_event 注入（键盘两者都有效）。因此这里把
pyautogui 的鼠标动作整体重定向到 pydirectinput，并补齐它缺失的 drag/scroll。

同时打的两个补丁：
  * locateOnScreen 抓图偶发 OSError 时重试，避免整脚本崩掉
  * SetForegroundWindow 被拒时不再抛异常，改为校验前台句柄
用法：.venv/Scripts/python.exe run_bot.py bot -N BOTTOM
"""
import time

import ctypes

import pyautogui as pag
import pydirectinput as pdi

pdi.FAILSAFE = False
pdi.PAUSE = 0.0

WHEEL_DELTA = 120
MOUSEEVENTF_WHEEL = 0x0800

_orig_locate = pag.locateOnScreen
_orig_locate_all = pag.locateAllOnScreen


def _with_retry(fn):
    def wrapper(*args, **kwargs):
        last = None
        for _ in range(4):
            try:
                return fn(*args, **kwargs)
            except OSError as exc:
                last = exc
                time.sleep(0.15)
        raise last
    return wrapper


pag.locateOnScreen = _with_retry(_orig_locate)
pag.locateAllOnScreen = _with_retry(_orig_locate_all)


def _pos(x=None, y=None):
    """兼容 pag.moveTo((x, y)) / pag.moveTo(x, y) / Point 三种写法。"""
    if x is None:
        return None
    if not isinstance(x, (int, float)):
        try:
            x, y = x[0], x[1]
        except Exception:
            return None
    return int(x), int(y)


def moveTo(x=None, y=None, duration=0.0, **kwargs):
    p = _pos(x, y)
    if p:
        pdi.moveTo(p[0], p[1], relative=False)


def moveRel(xOffset=None, yOffset=None, duration=0.0, **kwargs):
    pdi.moveRel(int(xOffset or 0), int(yOffset or 0))


def mouseDown(x=None, y=None, button="left", **kwargs):
    p = _pos(x, y)
    if p:
        pdi.moveTo(p[0], p[1], relative=False)
    pdi.mouseDown(button=button)


def mouseUp(x=None, y=None, button="left", **kwargs):
    p = _pos(x, y)
    if p:
        pdi.moveTo(p[0], p[1], relative=False)
    pdi.mouseUp(button=button)


def click(x=None, y=None, clicks=1, interval=0.0, button="left", **kwargs):
    p = _pos(x, y)
    if p:
        pdi.moveTo(p[0], p[1], relative=False)
    pdi.click(clicks=clicks, interval=interval, button=button)


def doubleClick(x=None, y=None, interval=0.0, button="left", **kwargs):
    p = _pos(x, y)
    if p:
        pdi.moveTo(p[0], p[1], relative=False)
    pdi.click(clicks=2, interval=interval, button=button)


def drag(xOffset=0, yOffset=0, duration=0.0, button="left", **kwargs):
    pdi.mouseDown(button=button)
    try:
        time.sleep(max(duration, 0.1))
        steps = 10
        for _ in range(steps):
            pdi.moveRel(int(xOffset) // steps, int(yOffset) // steps)
            time.sleep(0.02)
    finally:
        pdi.mouseUp(button=button)


def scroll(clicks, x=None, y=None, **kwargs):
    p = _pos(x, y)
    if p:
        pdi.moveTo(p[0], p[1], relative=False)
    ctypes.windll.user32.mouse_event(
        MOUSEEVENTF_WHEEL, 0, 0, int(clicks) * WHEEL_DELTA, 0
    )


for _name, _fn in (
    ("moveTo", moveTo), ("moveRel", moveRel), ("mouseDown", mouseDown),
    ("mouseUp", mouseUp), ("click", click), ("doubleClick", doubleClick),
    ("drag", drag), ("scroll", scroll),
):
    setattr(pag, _name, _fn)


# --------------------------- 前台激活补丁 ---------------------------------
import win32gui  # noqa: E402

from rf4s.controller.window import Window  # noqa: E402

_orig_activate = Window.activate_game_window


def _safe_activate(self):
    hwnd = win32gui.FindWindow(None, self.game_title)
    if hwnd and win32gui.GetForegroundWindow() == hwnd:
        return
    try:
        _orig_activate(self)
    except Exception:
        pass
    if hwnd and win32gui.GetForegroundWindow() != hwnd:
        print("[warn] 无法把游戏窗口切到前台，请手动点一下游戏窗口", flush=True)


Window.activate_game_window = _safe_activate


# ------------------------------ 入口 --------------------------------------
import main as M  # noqa: E402

if __name__ == "__main__":
    try:
        M.main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        M.logger.critical(exc, exc_info=True)
        M.utils.safe_exit()
