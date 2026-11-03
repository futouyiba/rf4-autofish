# -*- coding: utf-8 -*-
"""RF4 窗口聚焦 + 带校验的点击。

问题：`SetForegroundWindow` 会被前台锁静默拒绝，于是点击落到别的窗口
（踩过两次：点到资源管理器、点到 Clash、点到 Everything）。

做法：TOPMOST → NOTOPMOST → SetForegroundWindow → 用 WindowFromPoint
验证目标点确实属于游戏进程，验证不过就重试，仍不过就报错而不是盲点。

用法:
    import rf4_focus as rf
    h = rf.find_game()          # 找 rf4_x64 主窗口
    rf.raise_game(h)            # 置前
    rf.click(h, 1764, 684)      # 客户区坐标点击（内部转绝对坐标 + 校验）
"""
import ctypes
import ctypes.wintypes as wt
import time

u = ctypes.windll.user32
PROC = 'rf4_x64'


def find_game(pid=None):
    """返回 rf4_x64 最大的可见窗口句柄；pid 可指定。"""
    if pid is None:
        import subprocess
        out = subprocess.run(['tasklist', '/FI', f'IMAGENAME eq {PROC}.exe', '/FO', 'CSV'],
                             capture_output=True, text=True).stdout
        for line in out.splitlines()[1:]:
            parts = line.split('","')
            if len(parts) > 1:
                pid = int(parts[1].strip('"')); break
    best = [None, 0]
    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, l):
        p = wt.DWORD(); u.GetWindowThreadProcessId(h, ctypes.byref(p))
        if pid and p.value == pid and u.IsWindowVisible(h):
            r = wt.RECT(); u.GetWindowRect(h, ctypes.byref(r))
            area = (r.right - r.left) * (r.bottom - r.top)
            if area > best[1]:
                best[0], best[1] = h, area
        return True
    u.EnumWindows(cb, 0)
    return best[0]


def rect(h):
    r = wt.RECT(); u.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def raise_game(h, tries=4):
    """强制把游戏窗口提到前台（绕前台锁），返回是否成功。"""
    for _ in range(tries):
        u.ShowWindow(h, 9)                                   # SW_RESTORE
        u.SetWindowPos(h, -1, 0, 0, 0, 0, 0x0001 | 0x0002)    # TOPMOST
        u.SetWindowPos(h, -2, 0, 0, 0, 0, 0x0001 | 0x0002)    # NOTOPMOST
        u.BringWindowToTop(h)
        u.SetForegroundWindow(h)
        time.sleep(0.5)
        if u.GetForegroundWindow() == h:
            return True
        # 有时需要先“点一下”才拿到前台：点到窗口内安全区域
        ox, oy, w, hh = rect(h)
        u.SetCursorPos(ox + w // 2, oy + hh // 2)
        u.mouse_event(0x0002, 0, 0, 0, 0); time.sleep(0.05); u.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.4)
        if u.GetForegroundWindow() == h:
            return True
    return False


def _pid_of(h):
    p = wt.DWORD(); u.GetWindowThreadProcessId(h, ctypes.byref(p)); return p.value


def click(h, x, y, w=1920, hh=1080, verify=True, pause=0.25):
    """客户区坐标 -> 绝对坐标点击；verify=True 时校验该点确实属于游戏窗口。"""
    ox, oy, rw, rh = rect(h)
    ax, ay = ox + int(x * rw / w), oy + int(y * rh / hh)
    want = _pid_of(h)
    got = _pid_of(u.WindowFromPoint(wt.POINT(ax, ay)))
    if verify and got != want:
        raise RuntimeError(f'点击点 ({ax},{ay}) 属于别的进程 pid={got}，不是游戏 pid={want}；'
                           f'先 raise_game() 或把挡住的窗口移开')
    u.SetCursorPos(ax, ay)
    time.sleep(pause)
    u.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.08)
    u.mouse_event(0x0004, 0, 0, 0, 0)
    return ax, ay


def shot(path, h):
    from PIL import ImageGrab
    ox, oy, w, hh = rect(h)
    vx, vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
    ImageGrab.grab(all_screens=True).crop((ox - vx, oy - vy, ox - vx + w, oy - vy + hh)).save(path)
