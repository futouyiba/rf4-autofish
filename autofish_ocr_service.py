# -*- coding: utf-8 -*-
"""OCR 服务进程：常驻，按行读写 JSON（由 autofish_ocr_client 拉起）。

为什么要独立进程：本机 cv2 4.8 无法加载 PP-OCR 的 rec 模型（不支持 MatMul 算子），
能跑的 cv2 5.0 装在 `venv-ocr` 里，而主进程（游戏 venv）必须保持 numpy 1.26 + cv2 4.8
（底座依赖，随便升级会把主脚本搞坏）。所以 OCR 只能跑在独立环境，用管道通信。

请求（一行一个 JSON）::

    {"id": 1, "png_b64": "...", "mode": "line"}     # 已知单行，最快
    {"id": 2, "png_b64": "...", "mode": "block"}    # 区域检测+识别
    {"id": 3, "png_b64": "...", "mode": "block", "min_score": 0.6}

响应::

    {"id": 1, "ok": true, "items": [{"text": "17", "score": 0.99, "box": [0,0,30,20]}]}
    {"id": 1, "ok": false, "error": "..."}
"""

from __future__ import annotations

import base64
import json
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from autofish_ocr import OcrEngine  # noqa: E402


def _decode(png_b64: str):
    raw = base64.b64decode(png_b64)
    arr = np.frombuffer(raw, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def main() -> int:
    try:
        engine = OcrEngine()
    except Exception as exc:                      # 模型缺失等：让客户端立刻知道
        sys.stdout.write(json.dumps({"id": 0, "ok": False,
                                     "error": "engine_init: %s" % exc}) + "\n")
        sys.stdout.flush()
        return 1

    sys.stdout.write(json.dumps({"id": 0, "ok": True, "ready": True}) + "\n")
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as exc:
            sys.stdout.write(json.dumps({"id": 0, "ok": False,
                                         "error": "bad_json: %s" % exc}) + "\n")
            sys.stdout.flush()
            continue

        req_id = req.get("id", 0)
        try:
            img = _decode(req.get("png_b64", ""))
            if img is None:
                raise ValueError("png decode failed")
            mode = req.get("mode", "block")
            roi = req.get("roi")
            if mode == "line":
                text, score = engine.read_line(img, roi=roi,
                                               min_score=req.get("min_score", 0.3))
                items = [{"text": text, "score": score, "box": [0, 0, 0, 0]}] if text else []
            else:
                got = engine.read(img, roi=roi, min_score=req.get("min_score", 0.5))
                items = [{"text": it.text, "score": it.score, "box": list(it.box)}
                         for it in got]
            out = {"id": req_id, "ok": True, "items": items}
        except Exception as exc:                  # 单次请求失败不影响后续
            out = {"id": req_id, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
