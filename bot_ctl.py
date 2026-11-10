# -*- coding: utf-8 -*-
"""钓鱼脚本开关（桌面 bat / 面板调用）。

用法::

    python bot_ctl.py              # 切换：在跑就停，停了就开
    python bot_ctl.py start        # 默认启动 autofish_bridge.py（正式全流程）
    python bot_ctl.py start --legacy
    python bot_ctl.py stop
    python bot_ctl.py status
    python bot_ctl.py emergency-stop
    python bot_ctl.py ui           # 前台启动当前 venv 的 UI
    python bot_ctl.py release      # 仅显式松开按键

约束：

* ``start`` 只启动一次，**不会**因为返回码 0 自动再来一轮；
* ``stop`` 先走 IPC stop，等最多 3 秒；不抢前台、不强杀。超时只提示用户
  使用 ``emergency-stop``；
* **没有实例时 stop 绝不调用 release_keys**（避免抢前台/误发 keyUp）；
* ``emergency-stop`` 只对通过身份校验（pid + 创建时间 + python 镜像 +
  role）的进程执行 ``taskkill /PID <pid> /T /F``，然后松键；仅凭旧
  ``run.pid`` 的整数 PID 一律拒绝强杀；
* 运行身份写在 ``autofish.process.json``；bridge 启动后可调用
  ``write_process_identity(os.getpid(), "bridge")`` 写入/刷新；
* 未知命令不会落到 toggle，必须显式 ``toggle`` 或无参数才切换。
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time

import app_paths

# 只读资源目录（开发态 = bot/，打包态 = exe 目录）：用来找脚本与 refs。
BOT = str(app_paths.APP_DIR)
# 可写数据目录（开发态 = bot/，打包态 = %LOCALAPPDATA%\RF4AutoFish）：
# pid / 身份 / endpoint / 日志都放这里，绝不写只读安装目录。
DATA = str(app_paths.DATA_DIR)
REFS = str(app_paths.ref_dir())
#: legacy 适配器必须站在 RF4S 源码目录里跑（它 import rf4s 的模块）。
LEGACY_CWD = REFS
#: 允许用 RF4_PYTHON 指定解释器（换机器 / 自建 venv 时不用改代码）。
PY = os.environ.get("RF4_PYTHON") or os.path.join(
    REFS, ".venv", "Scripts", "python.exe"
)

BRIDGE = os.path.join(BOT, "autofish_bridge.py")
LEGACY = os.path.join(BOT, "rf4s_adapter.py")
UI = os.path.join(BOT, "autofish_ui.py")

PIDFILE = os.path.join(DATA, "run.pid")
IDENTITY_FILE = os.path.join(DATA, "autofish.process.json")
ENDPOINT = os.path.join(DATA, "autofish.endpoint.json")
LOG_BRIDGE = os.path.join(DATA, "_bridge.log")
LOG_LEGACY = os.path.join(DATA, "_run_desktop.log")

STOP_TIMEOUT = 3.0
CREATE_NO_WINDOW = 0x08000000
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
IDENTITY_VERSION = 1
IDENTITY_ROLES = ("bridge", "legacy")
PYTHON_IMAGE_NAMES = ("python.exe", "pythonw.exe")

LEGACY_ENV_VARS = ("RF4_BOT_LEGACY", "RF4S_LEGACY")


# ----------------------------------------------------------------------
# PID / 运行身份
#
# 裸整数 PID 会因 PID 复用导致误杀。新版身份文件 autofish.process.json
# 同时记录 pid、进程创建时间、可执行文件路径和角色；emergency-stop 只在
# 创建时间与当前进程一致、镜像是 python(.exe)/pythonw(.exe) 且角色合法时
# 才允许 taskkill。旧 run.pid 只作为 status 的“未验证”展示，绝不用于强杀。
# ----------------------------------------------------------------------
class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_ulong),
        ("dwHighDateTime", ctypes.c_ulong),
    ]


_K32_TYPED = False


def _kernel32():
    """Return kernel32 with 64-bit-safe prototypes (Windows only)."""
    global _K32_TYPED
    kernel = ctypes.windll.kernel32
    if not _K32_TYPED:
        kernel.OpenProcess.argtypes = [
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel.GetExitCodeProcess.restype = ctypes.c_int
        kernel.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
        ]
        kernel.GetProcessTimes.restype = ctypes.c_int
        kernel.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel.QueryFullProcessImageNameW.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        _K32_TYPED = True
    return kernel


def _as_pid(pid):
    if isinstance(pid, bool):
        return None
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _alive(pid) -> bool:
    """进程还活着吗（纯 ctypes，不依赖 tasklist/wmic）。"""
    pid = _as_pid(pid)
    if pid is None or os.name != "nt":
        return False
    k = _kernel32()
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not k.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def _process_creation_time(pid):
    """进程创建时间（Windows FILETIME 整数）；不可用时返回 None。"""
    pid = _as_pid(pid)
    if pid is None or os.name != "nt":
        return None
    k = _kernel32()
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        created = _FILETIME()
        exited = _FILETIME()
        kernel_time = _FILETIME()
        user_time = _FILETIME()
        if not k.GetProcessTimes(
            h,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        value = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        return value or None
    finally:
        k.CloseHandle(h)


def _process_image_path(pid):
    """进程可执行文件完整路径；不可用时返回 None。"""
    pid = _as_pid(pid)
    if pid is None or os.name != "nt":
        return None
    k = _kernel32()
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        size = ctypes.c_ulong(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not k.QueryFullProcessImageNameW(
            h, 0, ctypes.byref(buffer), ctypes.byref(size)
        ):
            return None
        return buffer.value or None
    finally:
        k.CloseHandle(h)


def _image_name(path) -> str:
    """``C:\\x\\python.exe`` 与 ``/x/python.exe`` 都能取到文件名。"""
    return str(path or "").replace("\\", "/").rsplit("/", 1)[-1].strip().lower()


def _atomic_write_json(path: str, payload: dict) -> None:
    path = os.path.abspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass


def write_process_identity(pid, role="bridge", *, path=None, session=None) -> dict:
    """Write the verifiable runtime identity for *pid*.

    The bridge is expected to call this once after startup (it knows its own
    pid)::

        import bot_ctl
        bot_ctl.write_process_identity(os.getpid(), "bridge")

    ``role`` must be ``"bridge"`` or ``"legacy"``.  The process creation time
    and image path are probed here; if the probe fails the record is still
    written but will be treated as unverified and can never authorize a kill.
    """
    pid_value = _as_pid(os.getpid() if pid is None else pid)
    if pid_value is None:
        raise ValueError("invalid pid: %r" % (pid,))
    role_value = str(role).strip().lower()
    if role_value not in IDENTITY_ROLES:
        raise ValueError("invalid role: %r" % (role,))

    identity = {
        "version": IDENTITY_VERSION,
        "pid": pid_value,
        "creation_time": _process_creation_time(pid_value),
        "image": _process_image_path(pid_value) or "",
        "role": role_value,
        "written_at": time.time(),
    }
    if session:
        identity["session"] = str(session)[:128]
    _atomic_write_json(path or IDENTITY_FILE, identity)
    return identity


def read_identity(path=None):
    try:
        with open(path or IDENTITY_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def verify_identity(identity, expected_pid=None):
    """Return ``(ok, reason)`` for an identity record.

    A record is verifiable only when the pid still refers to the same process
    (creation time match), the live image is a Python interpreter and the role
    is one of ``bridge``/``legacy``.  Anything weaker is refused.
    """
    if not isinstance(identity, dict):
        return False, "identity missing"
    pid = _as_pid(identity.get("pid"))
    if pid is None:
        return False, "identity pid invalid"
    if expected_pid is not None and pid != _as_pid(expected_pid):
        return False, "identity pid mismatch"

    role = str(identity.get("role") or "").strip().lower()
    if role not in IDENTITY_ROLES:
        return False, "identity role invalid"

    stored_created = identity.get("creation_time")
    if isinstance(stored_created, bool) or not isinstance(stored_created, int):
        return False, "identity creation time missing"
    if stored_created <= 0:
        return False, "identity creation time missing"

    live_created = _process_creation_time(pid)
    if live_created is None:
        return False, "process not running"
    if int(live_created) != stored_created:
        return False, "identity creation time mismatch"

    live_image = _process_image_path(pid)
    image = live_image or identity.get("image") or ""
    if _image_name(image) not in PYTHON_IMAGE_NAMES:
        return False, "image not python"
    stored_image = identity.get("image")
    if (
        stored_image
        and live_image
        and os.path.normcase(str(stored_image)) != os.path.normcase(str(live_image))
    ):
        return False, "image path mismatch"
    return True, "verified"


def _remove_identity(pid=None, path=None) -> None:
    """Remove the identity file, optionally only when it belongs to *pid*."""
    target = path or IDENTITY_FILE
    try:
        if pid is not None:
            data = read_identity(target)
            if not data or _as_pid(data.get("pid")) != _as_pid(pid):
                return
        os.remove(target)
    except Exception:
        pass


def read_pid():
    try:
        with open(PIDFILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def running_pid():
    """已通过身份验证的 pid；裸 run.pid 或旧文件一律不返回。

    ``run.pid`` 只由 :func:`status` 作为“未验证”信息展示，绝不参与强杀。
    """
    identity = read_identity()
    ok, _reason = verify_identity(identity)
    return _as_pid(identity.get("pid")) if ok else None


def legacy_pid():
    """旧 run.pid 中仍存活的 pid（仅供 status 展示，未验证）。"""
    pid = read_pid()
    if pid and _alive(pid):
        return pid
    return None


def _write_pid(path: str, pid: int) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(int(pid)))
    except Exception:
        pass


def _remove_pid(path: str = PIDFILE, pid=None) -> None:
    """删除 PID 文件；给出 pid 时只删除匹配的那个。"""
    try:
        if pid is not None:
            with open(path, encoding="utf-8") as f:
                if int(f.read().strip()) != int(pid):
                    return
        os.remove(path)
    except Exception:
        pass


# ----------------------------------------------------------------------
# 显式释放按键（仅 emergency-stop / release 使用）
# ----------------------------------------------------------------------
_RELEASE_SCRIPT = r'''
import ctypes
import time
import pydirectinput as pdi

pdi.FAILSAFE = False
pdi.PAUSE = 0
if __FOCUS__:
    try:
        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, "Russian Fishing 4")
        if hwnd and u.GetForegroundWindow() != hwnd:
            u.ShowWindow(hwnd, 9)
            u.SetForegroundWindow(hwnd)
            time.sleep(0.2)
    except Exception:
        pass
# f7 是正式抄网键；pagedown/space 仅清理旧版本可能残留的按键。
for key in ("f5", "f6", "shift", "shiftleft", "shiftright",
            "enter", "f7", "pagedown", "space"):
    try:
        pdi.keyUp(key)
    except Exception:
        pass
'''


def _release_keys_subprocess(focus: bool) -> None:
    """当前解释器没有 pydirectinput 时，用 RF4S venv 执行一次显式松键。

    打包态没有独立 venv：直接在同一进程里松键（`release_keys` 已经会先试
    本进程导入，这里再兜一层子进程反而会重复）。
    """
    if app_paths.is_frozen():
        return
    if not os.path.isfile(PY):
        return
    code = _RELEASE_SCRIPT.replace("__FOCUS__", "True" if focus else "False")
    try:
        subprocess.run(
            [PY, "-c", code],
            cwd=BOT,
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        pass


def release_keys(focus: bool = True) -> None:
    """明确动作：把可能按住的键松开。stop 不会调用这里。"""
    try:
        import pydirectinput as pdi

        pdi.FAILSAFE = False
        pdi.PAUSE = 0
    except Exception:
        _release_keys_subprocess(focus)
        return
    if focus:
        try:
            u = ctypes.windll.user32
            hwnd = u.FindWindowW(None, "Russian Fishing 4")
            if hwnd and u.GetForegroundWindow() != hwnd:
                u.ShowWindow(hwnd, 9)
                u.SetForegroundWindow(hwnd)
                time.sleep(0.2)
        except Exception:
            pass
    # f7 是正式抄网键；pagedown/space 仅清理旧版本可能残留的按键。
    for key in (
        "f5",
        "f6",
        "shift",
        "shiftleft",
        "shiftright",
        "enter",
        "f7",
        "pagedown",
        "space",
    ):
        try:
            pdi.keyUp(key)
        except Exception:
            pass


# ----------------------------------------------------------------------
# IPC
# ----------------------------------------------------------------------
def _ipc_client():
    from autofish_ipc import RuntimeClient  # 惰性导入：导入本模块无网络/无进程

    return RuntimeClient(ENDPOINT)


def _ipc_stop(reason: str = "bot_ctl stop") -> dict:
    try:
        return _ipc_client().command("stop", reason=reason)
    except Exception as exc:  # noqa: BLE001 - IPC 不可用只影响提示
        return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}


def _ipc_status() -> dict:
    try:
        return _ipc_client().status()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}


def _endpoint_pid():
    """当前 IPC 会话的 pid（仅用于 stop 等待退出，不作为强杀依据）。"""
    try:
        with open(ENDPOINT, encoding="utf-8") as f:
            data = json.load(f)
        pid = data.get("pid") if isinstance(data, dict) else None
    except Exception:
        return None
    return _as_pid(pid)


def _wait_pid_exit(pid: int, timeout: float = STOP_TIMEOUT) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return not _alive(pid)


# ----------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------
def _legacy_requested(argv) -> bool:
    if "--legacy" in list(argv):
        return True
    for name in LEGACY_ENV_VARS:
        value = os.environ.get(name)
        if value is not None and str(value).strip().lower() not in (
            "",
            "0",
            "false",
            "no",
        ):
            return True
    return False


def _build_command(legacy: bool):
    """返回 (cmd, cwd, env)。路径全部是绝对路径。

    打包态没有 ``python.exe`` 可用：后台就是**同一个 exe** 加 ``--bot``，
    cwd 落在可写数据目录（日志与 rf4s 的落盘都在这里）。
    """
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    if app_paths.is_frozen():
        if legacy:
            # 打包版不提供 legacy（它直接驱动 RF4S 的 Player 循环，
            # 与 bridge 的补丁层语义冲突），明确报错而不是假装启动。
            raise RuntimeError("打包版不支持 legacy 模式，请使用 bridge")
        cmd = [
            sys.executable,
            "--bot",
            "--pidfile",
            PIDFILE,
            "--endpoint",
            ENDPOINT,
        ]
        return cmd, DATA, env
    if legacy:
        env["PYTHONPATH"] = "."
        cmd = [PY, LEGACY, "bot", "-N", "BOTTOM"]
        return cmd, REFS, env
    env["PYTHONPATH"] = BOT
    cmd = [
        PY,
        BRIDGE,
        "--pidfile",
        PIDFILE,
        "--endpoint",
        ENDPOINT,
    ]
    return cmd, BOT, env


def _launch(cmd, cwd, env, log_path: str, role: str = "bridge") -> int:
    """前台等待子进程；stdout 直接写日志文件（不 PIPE，不阻塞）。"""
    log_path = os.path.abspath(log_path)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    try:
        with open(log_path, "a", encoding="utf-8", errors="replace") as log:
            log.write(
                "\n=== %s %s\n"
                % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(cmd))
            )
            log.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            _write_pid(PIDFILE, proc.pid)
            # Popen knows the child pid, so the identity can be written here
            # immediately; the bridge may call the same helper again later.
            try:
                write_process_identity(proc.pid, role)
            except Exception:  # noqa: BLE001 - identity failure must not block start
                pass
            print("已启动 pid=%d，日志：%s" % (proc.pid, log_path), flush=True)
            try:
                return int(proc.wait())
            except KeyboardInterrupt:
                _ipc_stop("bot_ctl keyboard interrupt")
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2.0)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                return 130
            finally:
                _remove_identity(proc.pid)
    except Exception as exc:  # noqa: BLE001
        print("启动失败：%s: %s" % (type(exc).__name__, exc), flush=True)
        return 1


def start(legacy: bool = False) -> int:
    """启动一次；返回码 0 也不自动重启。"""
    pid = running_pid()
    if pid:
        print("已经在跑了 (pid=%d，身份已验证)，先停再开" % pid)
        return 1
    stale = legacy_pid()
    if stale:
        print(
            "注意：旧 run.pid=%d 仍存活但无法验证身份，已忽略；"
            "stop/emergency 不会仅凭该 PID 强杀。" % stale
        )
    _remove_pid(PIDFILE)
    cmd, cwd, env = _build_command(bool(legacy))
    kind = "legacy" if legacy else "bridge"
    log_path = LOG_LEGACY if legacy else LOG_BRIDGE
    print("=" * 56)
    print("  自动钓鱼启动中（%s）..." % kind)
    print("  停止：python bot_ctl.py stop，或 emergency-stop")
    print("=" * 56)
    try:
        rc = _launch(cmd, cwd, env, log_path, role=kind)
    except KeyboardInterrupt:
        rc = 130
    finally:
        # legacy 适配器不会自己 release；bridge 在 runtime.close() 里释放 owned。
        if legacy:
            release_keys(focus=True)
        _remove_pid(PIDFILE)
    print("[已退出] %s rc=%s" % (kind, rc), flush=True)
    return rc


# ----------------------------------------------------------------------
# 停止 / 状态 / 紧急
# ----------------------------------------------------------------------
def stop() -> int:
    pid = running_pid()
    session_pid = None
    ipc_ok = False
    if pid is None:
        # No verified identity.  If the authenticated IPC session is alive we
        # may still use its own endpoint pid for *waiting* on exit (never kill).
        ipc = _ipc_status()
        ipc_ok = bool(ipc.get("ok"))
        if ipc_ok:
            session_pid = _endpoint_pid()

    if pid is None and not ipc_ok:
        print("本来就没在跑")
        _remove_pid(PIDFILE)
        # 无实例：绝不 release_keys（不抢前台、不发 keyUp）。
        return 0

    target = pid or session_pid
    ack = _ipc_stop("bot_ctl stop")
    if not ack.get("ok"):
        print(
            "IPC stop 未确认（%s），继续等待进程退出"
            % ack.get("message", "unknown")
        )

    if target is None:
        if ack.get("ok"):
            _remove_pid(PIDFILE)
            print("IPC stop 已确认（无可等待的 pid）")
            return 0
        print("无法确认进程 pid，且 IPC stop 未成功；如确需立即停止，请显式运行：")
        print("    python bot_ctl.py emergency-stop")
        return 1

    if _wait_pid_exit(target, STOP_TIMEOUT):
        _remove_pid(PIDFILE, target)
        _remove_identity(target)
        print("已停止 (pid=%d)" % target)
        return 0

    print("stop 请求已发送，但进程 %d 未在 %.0f 秒内退出。" % (target, STOP_TIMEOUT))
    print("不会强抢前台、也不会自动强杀。如确需立即停止，请显式运行：")
    print("    python bot_ctl.py emergency-stop")
    return 1


def status() -> int:
    pid = running_pid()
    identity = read_identity()
    stale = legacy_pid()
    running = bool(pid or stale)
    if pid:
        role = (identity or {}).get("role", "?")
        print("pid=%d (alive, identity=verified, role=%s)" % (pid, role))
    elif identity:
        _ok, reason = verify_identity(identity)
        print(
            "pid=%s (identity=unverified: %s；不能用于强杀)"
            % (identity.get("pid"), reason)
        )
    elif stale:
        print("pid=%d (alive, legacy run.pid, unverified；不能用于强杀)" % stale)
    else:
        print("pid=None (stopped)")

    ipc = _ipc_status()
    if ipc.get("ok"):
        snapshot = ipc.get("snapshot") or {}
        print(
            "ipc=ok session=%s state=%s stopped=%s"
            % (
                ipc.get("session", "?"),
                snapshot.get("state", "?"),
                bool(ipc.get("stopped")),
            )
        )
        acks = ipc.get("acks")
        if isinstance(acks, list):
            print("acks=%d" % len(acks))
            for ack in acks[-5:]:
                if not isinstance(ack, dict):
                    continue
                message = str(ack.get("message") or "")
                print(
                    "  %s=%s%s"
                    % (
                        ack.get("id", "?"),
                        ack.get("status", "?"),
                        (" (%s)" % message) if message else "",
                    )
                )
    else:
        print("ipc=offline (%s)" % ipc.get("message", "unknown"))
    return 0 if running else 1


def emergency_stop() -> int:
    """明确动作：仅对**可验证身份**的进程 taskkill /T /F，然后松键。

    裸整数 PID（包括旧 run.pid）绝不允许强杀：PID 复用会杀掉无关进程。
    """
    identity = read_identity()
    verified, reason = verify_identity(identity)
    actions = []
    rc = 0

    if not verified:
        rc = 1
        actions.append("refused")
        actions.append("reason=%s" % (reason or "no verified identity"))
    else:
        pid = _as_pid(identity.get("pid"))
        if pid is None or not _alive(pid):
            actions.append("not_running")
            if pid is not None:
                _remove_identity(pid)
        else:
            actions.append(
                "taskkill /PID %d /T /F (role=%s)"
                % (pid, identity.get("role"))
            )
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception as exc:  # noqa: BLE001
                actions.append("taskkill_failed:%s" % type(exc).__name__)
                rc = 1
            if _wait_pid_exit(pid, 1.0):
                _remove_identity(pid)
                _remove_pid(PIDFILE, pid)

    actions.append("release_keys")
    release_keys(focus=True)
    print("emergency-stop: " + "; ".join(actions))
    if not verified:
        print("拒绝只凭整数 PID 强杀：未找到可验证的 autofish.process.json 身份。")
        print("旧 run.pid 只能用于 status 展示；请确认 bridge 已调用：")
        print("    bot_ctl.write_process_identity(os.getpid(), 'bridge')")
        return 1
    return rc


# ----------------------------------------------------------------------
# UI / 帮助
# ----------------------------------------------------------------------
def ui(args) -> int:
    """用**当前解释器**前台启动 UI，不 detach（打包态就是同一个 exe）。"""
    if app_paths.is_frozen():
        cmd = [sys.executable, "--ui"] + list(args)
    else:
        cmd = [sys.executable, UI] + list(args)
    print("启动 UI（前台）：%s" % " ".join(cmd), flush=True)
    try:
        return int(subprocess.call(cmd, cwd=BOT))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print("UI 启动失败：%s: %s" % (type(exc).__name__, exc), flush=True)
        return 1


def _usage() -> None:
    print(
        "用法: python bot_ctl.py [start [--legacy] | stop | status | "
        "emergency-stop | release | ui | toggle]"
    )


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    legacy = _legacy_requested(raw)
    args = [item for item in raw if item != "--legacy"]
    if not args:
        return stop() if running_pid() else start(legacy)

    cmd, rest = args[0], args[1:]
    if cmd == "start":
        return start(legacy)
    if cmd == "stop":
        return stop()
    if cmd == "status":
        return status()
    if cmd in ("emergency-stop", "emergency_stop"):
        return emergency_stop()
    if cmd == "release":
        release_keys(focus=True)
        print("keys released")
        return 0
    if cmd == "ui":
        return ui(rest)
    if cmd == "toggle":
        if rest:
            print("toggle 不接受参数：%s" % " ".join(rest))
            _usage()
            return 2
        return stop() if running_pid() else start(legacy)
    if cmd in ("-h", "--help", "help"):
        _usage()
        return 0

    # 未知命令：绝不落到 toggle/start。
    print("未知命令：%s（未执行任何动作）" % cmd)
    _usage()
    return 2


if __name__ == "__main__":
    sys.exit(main())
