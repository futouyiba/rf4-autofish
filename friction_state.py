# -*- coding: utf-8 -*-
"""摩擦档位的**唯一事实来源**（single source of truth）。

## 为什么需要它

档位屏幕上没有读数，以前记在**进程内存**（`_fric["cur"]`）里：
进程一重启、或两个进程并存（主脚本 + 临时接管工具），内存各自为政，
就出现"游戏实际 29、脚本以为 17，一加就顶到 30 断线"的事故（2026-11-04 02:47）。

## 设计

* 档位落盘在 `bot/friction.state`（JSON），原子写（tmp + os.replace）
* **任何进程**（主脚本 / 接管工具 / 手动调试）都通过这里读写
* 启动校准（滚满 30 退回 17）时写 `calibrated=true`；
  其他进程先看这个标记，没校准过就不做增量调整（避免起点未知）
* 读不到/损坏 -> 返回 None，调用方必须先重新绝对校准
"""
import json
import os
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "friction.state")

_FIELDS = ("cur", "updated", "pid", "calibrated")


def _read() -> dict | None:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            st = json.load(f)
        if all(k in st for k in _FIELDS):
            return st
    except Exception:
        pass
    return None


def get() -> int | None:
    """当前档位；未校准/读不到返回 None。"""
    st = _read()
    if st and st.get("calibrated"):
        try:
            return int(st["cur"])
        except Exception:
            return None
    return None


def set(cur: int, calibrated: bool = True, pid: int | None = None) -> None:
    """原子更新档位。"""
    d = os.path.dirname(STATE_PATH) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"cur": int(cur), "updated": time.time(),
                       "pid": pid if pid is not None else os.getpid(),
                       "calibrated": bool(calibrated)}, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_PATH)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def invalidate() -> None:
    """档位不可信了（手动动过滚轮 / 游戏重登），下次必须重新校准。"""
    try:
        os.remove(STATE_PATH)
        return
    except FileNotFoundError:
        return
    except Exception:
        pass
    try:
        set(0, calibrated=False)
    except Exception:
        pass


def owner() -> int | None:
    st = _read()
    return int(st["pid"]) if st else None


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "get"
    if cmd == "get":
        v = get()
        print(v if v is not None else "uncalibrated")
    elif cmd == "set":
        set(int(sys.argv[2]))
        print("ok", sys.argv[2])
    elif cmd == "invalidate":
        invalidate()
        print("invalidated")
    elif cmd == "info":
        st = _read()
        print(json.dumps(st, ensure_ascii=False) if st else "no state")
