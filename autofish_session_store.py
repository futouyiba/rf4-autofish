# -*- coding: utf-8 -*-
"""会话配置持久化：时限与计时状态存到 data/session.json。

纯 IO 封装；默认路径经 app_paths.data_path（冻结态指向 LOCALAPPDATA）。
文件损坏/不可读时回退默认值并保留原因，绝不因配置文件坏掉而阻塞启动。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import autofish_session_timer as st

__all__ = ["SESSION_FILE", "session_path", "load_session", "save_session"]

SESSION_FILE = "session.json"


def session_path() -> Path:
    try:
        import app_paths  # noqa: PLC0415
        return app_paths.data_path(SESSION_FILE)
    except Exception:
        return Path(__file__).resolve().parent / "data" / SESSION_FILE


def load_session(path: Optional[Path] = None) -> Tuple[st.SessionTimer, str]:
    """读配置，返回 (timer, 说明)。读不到返回默认 timer + 原因。"""
    p = path or session_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return st.SessionTimer(), "配置格式错误，已用默认 8 小时"
        return st.SessionTimer.from_dict(data), ""
    except FileNotFoundError:
        return st.SessionTimer(), ""
    except (OSError, ValueError) as exc:
        return st.SessionTimer(), "配置读取失败：%s" % type(exc).__name__


def save_session(timer: st.SessionTimer, path: Optional[Path] = None) -> bool:
    """原子写：临时文件 + os.replace，避免写一半被读到。"""
    p = path or session_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload: Dict[str, Any] = timer.to_dict()
        fd, tmp = tempfile.mkstemp(prefix=".session-", suffix=".json",
                                   dir=str(p.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except (OSError, TypeError, ValueError):
        return False
