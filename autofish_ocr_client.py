# -*- coding: utf-8 -*-
"""OCR 客户端：在**主进程**里调用独立 OCR 服务（子进程 + JSON 管道）。

主进程（游戏 venv，numpy 1.26 / cv2 4.8）不能直接跑 PP-OCR rec 模型，
所以把识别交给 `venv-ocr` 里的服务进程；本模块负责：

* 懒启动 / 崩溃后自动重启一次；
* 把 ROI 裁好、编码成 PNG（小区域只有几 KB），不传整帧；
* 超时保护：默认 2 秒，超时就杀掉服务，**绝不阻塞钓鱼主循环**。

用法::

    from autofish_ocr_client import OcrClient
    ocr = OcrClient()                       # 不启动进程，只记参数
    text, score = ocr.read_line(frame, (532, 1014, 200, 32))
    items = ocr.read_block(frame, roi=(120, 60, 300, 120))

约定：`frame` 是 numpy BGR 数组；出错一律返回空结果，不抛异常（调用方只当"没读到"）。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
from typing import List, Optional, Sequence, Tuple

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))


def _ocr_python() -> str:
    """找 venv-ocr 的解释器。

    开发态 = 模块目录旁的 venv-ocr；打包态模块目录是临时解包目录，所以
    改找 exe 旁边的 venv-ocr（用户可选装，没装就自动降级为"读不到文字"，
    不影响钓鱼主链路）。也支持 RF4_OCR_PYTHON 显式指定。
    """
    override = os.environ.get("RF4_OCR_PYTHON")
    if override:
        return override
    candidates = [os.path.join(HERE, "venv-ocr", "Scripts", "python.exe")]
    try:
        import app_paths  # noqa: PLC0415

        candidates.append(
            os.path.join(str(app_paths.APP_DIR), "venv-ocr", "Scripts", "python.exe"))
    except Exception:  # noqa: BLE001
        pass
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]


OCR_PYTHON = _ocr_python()
OCR_SERVICE = os.path.join(HERE, "autofish_ocr_service.py")


class OcrClient:
    """线程安全的 OCR 客户端（内部串行化，适合状态机在主线程里用）。"""

    def __init__(
        self,
        python_exe: Optional[str] = None,
        service: Optional[str] = None,
        timeout: float = 2.0,
    ) -> None:
        self._python = python_exe or OCR_PYTHON
        self._service = service or OCR_SERVICE
        self._timeout = float(timeout)
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._seq = 0
        self._start_failed: Optional[str] = None

    # -- 生命周期 -----------------------------------------------------
    @property
    def available(self) -> bool:
        """服务是否可用（首次调用前会尝试启动）。"""
        if self._start_failed:
            return False
        return os.path.exists(self._python) and os.path.exists(self._service)

    def close(self) -> None:
        with self._lock:
            self._kill_locked()

    def _kill_locked(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.0)
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout):   # 显式关闭管道，避免 ResourceWarning
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    def _ensure_locked(self) -> bool:
        if self._start_failed:
            return False
        if self._proc is not None and self._proc.poll() is None:
            return True
        if not self.available:
            self._start_failed = "缺少 venv-ocr 或服务脚本"
            return False
        try:
            self._proc = subprocess.Popen(
                [self._python, self._service],
                cwd=HERE,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except Exception as exc:
            self._start_failed = "%s: %s" % (type(exc).__name__, exc)
            self._proc = None
            return False
        # 等 ready 行（模型加载约 0.2s，给 10 秒余量）
        try:
            ready = self._read_line_locked(timeout=10.0)
        except Exception as exc:
            self._start_failed = "启动超时: %s" % exc
            self._kill_locked()
            return False
        if not ready or not ready.get("ok"):
            self._start_failed = (ready or {}).get("error", "服务未就绪")
            self._kill_locked()
            return False
        return True

    # -- 通信 ---------------------------------------------------------
    def _read_line_locked(self, timeout: float) -> Optional[dict]:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        box: dict = {}

        def _reader() -> None:
            try:
                box["line"] = proc.stdout.readline()
            except Exception as exc:               # noqa: BLE001
                box["error"] = str(exc)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            self._kill_locked()                    # 卡住就重启，别拖累主循环
            return None
        line = box.get("line") or ""
        if not line.strip():
            return None
        try:
            return json.loads(line)
        except Exception:
            return None

    def _request(self, frame, roi, mode: str, min_score: Optional[float] = None):
        if frame is None:
            return None
        if roi is not None:
            x, y, w, h = (int(v) for v in roi)
            h_img, w_img = frame.shape[:2]
            x, y = max(0, x), max(0, y)
            w, h = min(w, w_img - x), min(h, h_img - y)
            if w <= 0 or h <= 0:
                return None
            frame = frame[y:y + h, x:x + w]
        ok, buf = cv2.imencode(".png", frame)
        if not ok:
            return None
        payload = {
            "png_b64": base64.b64encode(buf.tobytes()).decode("ascii"),
            "mode": mode,
        }
        if min_score is not None:
            payload["min_score"] = float(min_score)

        with self._lock:
            if not self._ensure_locked():
                return None
            self._seq += 1
            payload["id"] = self._seq
            proc = self._proc
            try:
                proc.stdin.write(json.dumps(payload) + "\n")
                proc.stdin.flush()
            except Exception:
                self._kill_locked()
                return None
            resp = self._read_line_locked(self._timeout)
            if resp is None or not resp.get("ok"):
                if resp is None:
                    self._kill_locked()
                return None
            return resp.get("items") or []

    # -- 对外接口 -----------------------------------------------------
    def read_line(
        self, frame, roi: Optional[Sequence[int]] = None, min_score: float = 0.3
    ) -> Tuple[str, float]:
        """识别**已知单行**区域，返回 (文本, 置信度)；读不到返回 ("", 0.0)。"""
        items = self._request(frame, roi, "line", min_score)
        if not items:
            return "", 0.0
        first = items[0]
        return first.get("text", ""), float(first.get("score", 0.0))

    def read_block(
        self, frame, roi: Optional[Sequence[int]] = None, min_score: float = 0.5
    ) -> List[dict]:
        """区域/整图：检测多行，返回 [{text, score, box}, ...]。"""
        return self._request(frame, roi, "block", min_score) or []

    def read_block_text(
        self, frame, roi: Optional[Sequence[int]] = None, min_score: float = 0.5
    ) -> str:
        return " ".join(it.get("text", "") for it in self.read_block(frame, roi, min_score))


_DEFAULT: Optional[OcrClient] = None


def default_client() -> OcrClient:
    """进程内共享单例（懒启动）。"""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = OcrClient()
    return _DEFAULT
