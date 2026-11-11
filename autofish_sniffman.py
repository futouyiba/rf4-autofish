# -*- coding: utf-8 -*-
"""协议层自包含管理：检测 Npcap → 引导安装 → 以管理员拉起 ``--sniff``。

为什么需要这一层
----------------
抓包链路的用户态部分（scapy + rf4_sniff）已经打进 exe；剩下的外部依赖只有
**Npcap 内核驱动**。Npcap 免费版许可证**禁止再分发**，不能塞进安装包，所以
只能检测 + 引导用户自己装。此外嗅探需要管理员权限，exe 本身不以管理员运行，
所以 sniff 进程必须用 ShellExecuteW 的 ``runas`` 单独提权拉起。

角色与生命周期::

    exe --sniff            抓包进程本体（需要管理员；由 sniffman 提权拉起）
    exe --sniff-ctl start  检测+引导+拉起（面板 / bot_ctl 调）
    exe --sniff-ctl stop   杀掉抓包进程（按身份校验，不误杀）
    exe --sniff-ctl status 检测驱动 + 进程状态（面板显示用）

状态文件与 rf4_sniff 一致：``%LOCALAPPDATA%/RF4AutoFish/_sniff/state.json``
（autofish_handoff.resolve_snapshot_path 已会找到它）。
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import app_paths

#: Npcap 安装目录（默认安装位置；也接受 WinPcap 的老目录）。
_NPCAP_DIRS = (
    r"C:\Windows\System32\Npcap",
    r"C:\Windows\SysWOW64\Npcap",
    r"C:\Windows\System32\wpcap.dll",
)
#: Npcap 官方下载页（免费版禁止再分发，只能引导用户自己下载）。
NPCAP_URL = "https://npcap.com/dist/"
#: 引导安装的提示窗标题。
BOX_TITLE = "RF4 协议层"


# ----------------------------------------------------------------------
# 检测
# ----------------------------------------------------------------------
def npcap_available() -> bool:
    """Npcap/WinPcap 是否已安装（只查文件，不加载驱动）。"""
    for item in _NPCAP_DIRS:
        if os.path.exists(item):
            return True
    return False


def sniff_running() -> dict:
    """读抓包进程身份；返回 {pid, alive} 或 {pid: None, alive: False}。"""
    path = app_paths.data_path("_sniff", "sniff.process.json")
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 无文件/损坏都视为没跑
        return {"pid": None, "alive": False}
    pid = info.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return {"pid": None, "alive": False}
    return {"pid": pid, "alive": _pid_alive(pid)}


def _pid_alive(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True
    finally:
        kernel32.CloseHandle(h)


def _write_identity(pid: int) -> None:
    path = app_paths.data_path("_sniff", "sniff.process.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": pid, "t": time.time()}, ensure_ascii=False),
        encoding="utf-8",
    )


# ----------------------------------------------------------------------
# 提权拉起
# ----------------------------------------------------------------------
def _elevated_spawn(args: list) -> bool:
    """用 runas 提权运行 ``sys.executable <args>``；成功返回 True。

    会弹 UAC；用户拒绝时 ShellExecuteW 返回 SE_ERR_ACCESSDENIED (5)。
    """
    if not app_paths.is_frozen():
        # 开发态：直接用当前解释器（测试时通常已经是管理员或不需要）。
        cmd = [sys.executable] + args
        try:
            subprocess.Popen(
                cmd,
                cwd=str(app_paths.APP_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            return True
        except OSError:
            return False
    params = " ".join(
        ('"%s"' % a) if (" " in a or '"' not in a) else a for a in args
    )
    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("fMask", ctypes.c_ulong),
            ("hwnd", ctypes.c_void_p),
            ("lpVerb", ctypes.c_wchar_p),
            ("lpFile", ctypes.c_wchar_p),
            ("lpParameters", ctypes.c_wchar_p),
            ("lpDirectory", ctypes.c_wchar_p),
            ("nShow", ctypes.c_int),
            ("hInstApp", ctypes.c_void_p),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", ctypes.c_wchar_p),
            ("hkeyClass", ctypes.c_void_p),
            ("dwHotKey", ctypes.c_ulong),
            ("hIconOrMonitor", ctypes.c_void_p),
            ("hProcess", ctypes.c_void_p),
        ]

    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = sys.executable
    sei.lpParameters = params
    sei.lpDirectory = str(app_paths.APP_DIR)
    sei.nShow = SW_HIDE
    ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
    if sei.hProcess:
        ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    return bool(ok)


def start_sniff(quiet: bool = False) -> int:
    """入口：``--sniff-ctl start``。返回进程退出码（sniff 是常驻，正常不返回）。

    幂等：已在跑就直接报状态返回 0。
    """
    running = sniff_running()
    if running["alive"]:
        print("抓包器已在运行 (pid=%d)" % running["pid"])
        return 0
    if not npcap_available():
        _guide_install()
        return 2
    args = ["--sniff", "--quiet"]
    if not _elevated_spawn(args):
        print("提权失败：用户拒绝 UAC，或无法启动抓包进程", file=sys.stderr)
        return 3
    # 提权进程写身份可能稍慢；轮询一下让调用方拿到 pid。
    for _ in range(10):
        time.sleep(0.3)
        running = sniff_running()
        if running["alive"]:
            print("抓包器已启动 (pid=%d)。" % running["pid"])
            print("提醒：必须【先起抓包、再让游戏登录】，否则该连接解不开。")
            return 0
    print("抓包进程已拉起（UAC 通过），但未在 3 秒内确认存活；")
    print("如无输出请查看 %s" % app_paths.data_path("_sniff", "sniff.log"))
    return 0


def stop_sniff() -> int:
    """入口：``--sniff-ctl stop``。按身份校验后 taskkill，不误杀。"""
    running = sniff_running()
    if not running["alive"]:
        print("抓包器没在跑")
        return 0
    pid = running["pid"]
    # 抓包进程是提权起来的；普通权限 taskkill /PID 会拒绝，所以整条命令
    # 也走 runas（对已确认的自身 pid，UAC 语义上可接受）。
    r = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 and "拒绝" in (r.stderr or "") + (r.stdout or ""):
        ok = _elevated_spawn(["--sniff-kill", str(pid)])
        print("已请求提权结束 pid=%d（%s）" % (pid, "ok" if ok else "fail"))
        return 0 if ok else 1
    app_paths.data_path("_sniff", "sniff.process.json").unlink(missing_ok=True)
    print("已停止 (pid=%d)" % pid)
    return 0


def status_sniff() -> int:
    """入口：``--sniff-ctl status``。人类可读 + 退出码（0=就绪）。"""
    drv = npcap_available()
    running = sniff_running()
    print("npcap   : %s" % ("已安装" if drv else "未安装"))
    print("sniffer : %s" % (
        "运行中 (pid=%d)" % running["pid"] if running["alive"] else "未运行"))
    if drv and running["alive"]:
        return 0
    return 1


def kill_by_pid(pid_text: str) -> int:
    """入口：``--sniff-kill <pid>``（提权后的自杀通道）。"""
    try:
        pid = int(pid_text)
    except ValueError:
        return 2
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True)
    app_paths.data_path("_sniff", "sniff.process.json").unlink(missing_ok=True)
    return 0


def run_sniff_forever() -> int:
    """入口：``--sniff``（已被提权的本体）。写身份，然后进 rf4_sniff.main。"""
    _write_identity(os.getpid())
    sys.path.insert(0, str(app_paths.APP_DIR))
    import rf4_sniff  # noqa: PLC0415 - 冻结态同样可导入（已打进包）

    # 输出重定向到数据目录，便于排错（提权进程没有控制台）。
    log = app_paths.data_path("_sniff", "sniff.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8", errors="replace") as fh:
        try:
            old = sys.stdout
            sys.stdout = fh
            rc = rf4_sniff.main(["--quiet",
                                 "--out", str(app_paths.data_path("_sniff", "events.jsonl")),
                                 "--state", str(app_paths.data_path("_sniff", "state.json"))])
        finally:
            sys.stdout = old
    app_paths.data_path("_sniff", "sniff.process.json").unlink(missing_ok=True)
    return rc


def _guide_install() -> None:
    """Npcap 缺失时弹MessageBox 引导安装（free 版禁止再分发，只能引导）。"""
    MB_OKCANCEL = 0x1
    MB_ICONINFORMATION = 0x40
    IDOK = 1
    text = (
        "协议层需要 Npcap 抓包驱动（免费，但许可证不允许随包分发）。\n\n"
        "点「确定」打开 Npcap 下载页：\n" + NPCAP_URL + "\n\n"
        "下载 npcap-x.x.x.exe 安装时请勾选「Install Npcap in WinPcap API-compatible Mode」。\n"
        "装完后再执行一次本操作即可。"
    )
    rc = ctypes.windll.user32.MessageBoxW(
        None, text, BOX_TITLE, MB_OKCANCEL | MB_ICONINFORMATION
    )
    if rc == IDOK:
        os.startfile(NPCAP_URL)  # noqa: S606 - 官方下载页
