# -*- coding: utf-8 -*-
"""钓鱼脚本开关（给桌面 bat 调用的后端）。

用法：
    python bot_ctl.py            # 切换：在跑就停，停了就开
    python bot_ctl.py start
    python bot_ctl.py stop
    python bot_ctl.py status
    python bot_ctl.py release    # 只松开按键

为什么不用 wmic 查进程：这台机器的安全策略把 wmic 拉黑了。
改成**自己记 PID**（启动时写 `run.pid`），停止时 `taskkill /T /F` 连子进程一起收
（venv 的 python.exe 会再起一个真实解释器子进程，不 /T 会漏掉那个）。
"""
import ctypes
import os
import subprocess
import sys
import time

BOT = os.path.dirname(os.path.abspath(__file__))
REFS = os.path.join(BOT, "refs", "RussianFishing4Script")
PY = os.path.join(REFS, ".venv", "Scripts", "python.exe")
SCRIPT = os.path.join(BOT, "rf4s_adapter.py")
PIDFILE = os.path.join(BOT, "run.pid")
LOG = os.path.join(BOT, "_run_desktop.log")

STILL_ACTIVE = 259


def _alive(pid: int) -> bool:
    """进程还活着吗（纯 ctypes，不依赖 tasklist/wmic）。"""
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not k.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def read_pid():
    try:
        with open(PIDFILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def running_pid():
    pid = read_pid()
    if pid and _alive(pid):
        return pid
    return None


def release_keys() -> None:
    """把可能按住的键松开——强杀脚本时这一下很关键。"""
    sys.path.insert(0, BOT)
    try:
        import pydirectinput as pdi

        pdi.FAILSAFE = False
    except Exception:
        return
    try:
        import rf4s_adapter as A

        h = A.game_hwnd()
        u = ctypes.windll.user32
        if u.GetForegroundWindow() != h:
            u.ShowWindow(h, 9)
            u.SetForegroundWindow(h)
        time.sleep(0.2)
    except Exception:
        pass
    for k in ("f5", "f6", "shift", "shiftleft", "shiftright",
              "enter", "pagedown", "space"):
        try:
            pdi.keyUp(k)
        except Exception:
            pass


def start() -> int:
    """**前台**运行 bot：调用方（桌面 bat）的窗口就是它的控制台。

    为什么不做后台：
      * `Popen(DETACHED_PROCESS)` —— bat 窗口一关子进程就死（挂在 bat 的控制台上）
      * `start ""` 新开窗口 —— 实测同样会被父进程的 job 一起收掉
    前台跑的额外好处：日志直接打在窗口里，出问题不会"闪一下就没了"。
    关窗口 = 停止（或者再双击一次开关，stop 会把它 kill 掉）。
    """
    if running_pid():
        print("已经在跑了，先停再开")
        return 1
    if os.path.exists(PIDFILE):
        os.remove(PIDFILE)

    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    env["PYTHONUNBUFFERED"] = "1"

    print("=" * 56)
    print("  自动钓鱼启动中 ...")
    print("  停止方式：关闭本窗口，或再双击一次桌面开关")
    print("=" * 56)
    print()

    rc = 1
    try:
        rc = subprocess.call([PY, SCRIPT, "bot", "-N", "BOTTOM"],
                             cwd=REFS, env=env)
    except KeyboardInterrupt:
        rc = 0
    finally:
        release_keys()
        try:
            if os.path.exists(PIDFILE):
                os.remove(PIDFILE)
        except Exception:
            pass
        print()
        print(f"[已退出] 返回码 {rc}，按键已松开")
    return rc


def stop() -> int:
    pid = running_pid()
    if not pid:
        print("本来就没在跑")
        if os.path.exists(PIDFILE):
            os.remove(PIDFILE)
        release_keys()
        return 0
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, creationflags=0x08000000)
    time.sleep(0.8)
    try:
        os.remove(PIDFILE)
    except Exception:
        pass
    release_keys()
    print(f"已停止 (pid={pid})，按键已松开")
    return 0


def status() -> int:
    pid = running_pid()
    print(f"running (pid={pid})" if pid else "stopped")
    return 0 if pid else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "toggle"
    if cmd == "start":
        sys.exit(start())
    elif cmd == "stop":
        sys.exit(stop())
    elif cmd == "status":
        sys.exit(status())
    elif cmd == "release":
        release_keys()
        print("keys released")
    else:                                   # toggle
        sys.exit(stop() if running_pid() else start())
