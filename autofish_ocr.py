# -*- coding: utf-8 -*-
"""本地中文 OCR（cv2.dnn + PP-OCR ONNX），**不依赖 onnxruntime**。

## 为什么这么写

本机启用了 WDAC 代码完整性策略（enforcement=2），未签名的 `onnxruntime`
DLL 会被系统以 `WinError 5 拒绝访问` 拒绝加载——装哪个版本都一样。
`cv2` 本身可以正常加载，而 `cv2.dnn` 自带 ONNX 导入器，所以直接用它跑
PP-OCR 的 det/rec 模型，绕开整个 onnxruntime 依赖。

模型取自 `rapidocr` 包自带的 PP-OCRv6 small（det 约 9.5MB / rec 约 21MB），
已经复制到 `bot/models/ocr/`，**离线可用、不联网下载**。

## 用法

```python
from autofish_ocr import OcrEngine

engine = OcrEngine()                       # 首次构造会加载模型（约 0.2s）
items = engine.read(image)                 # 整图
items = engine.read(image, roi=(x, y, w, h))
for it in items:
    print(it.text, it.score, it.box)
```

返回值按"先上后下、先左后右"排序，便于直接取第一行当标题。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL_DIR = os.path.join(HERE, "models", "ocr")

# 检测：短边缩放到这个尺寸（越大越准，越慢）
DET_LIMIT_SIDE = 736
DET_THRESH = 0.3
DET_BOX_THRESH = 0.5
DET_UNCLIP = 1.6
# 识别：固定高 48，宽度上限 320（PP-OCR 的标准输入）
REC_H = 48
REC_MAX_W = 320


@dataclass(frozen=True)
class TextItem:
    """一行识别结果。box 为 (x, y, w, h)，相对传入图片/ROI 的坐标。"""

    text: str
    score: float
    box: Tuple[int, int, int, int]

    @property
    def x(self) -> int:
        return self.box[0]

    @property
    def y(self) -> int:
        return self.box[1]


def _load_charset(rec_path: str) -> List[str]:
    """从 rec 模型的 ONNX 元数据里取字符表（index 0 = CTC blank）。"""
    blob = open(rec_path, "rb").read()
    key = b"character"
    i = blob.find(key)
    if i < 0:
        raise RuntimeError("rec 模型里没有 character 元数据，无法构建字符表")
    j = i + len(key)
    if blob[j] != 0x12:                    # protobuf tag: field 2, wire type 2
        raise RuntimeError("character 元数据结构不符合预期")
    j += 1
    n = 0
    shift = 0
    while True:                            # varint 长度
        c = blob[j]
        j += 1
        n |= (c & 0x7F) << shift
        shift += 7
        if not c & 0x80:
            break
    chars = blob[j:j + n].decode("utf-8").splitlines()
    return ["blank"] + chars


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    xa, ya, wa, ha = cv2.boundingRect(a.astype(np.int32))
    xb, yb, wb, hb = cv2.boundingRect(b.astype(np.int32))
    x1, y1 = max(xa, xb), max(ya, yb)
    x2, y2 = min(xa + wa, xb + wb), min(ya + ha, yb + hb)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    return inter / float(wa * ha + wb * hb - inter)


class OcrEngine:
    """PP-OCR（cv2.dnn）中文识别器。线程不安全，单实例复用。"""

    def __init__(
        self,
        model_dir: Optional[str] = None,
        det_name: str = "PP-OCRv6_det_small.onnx",
        rec_name: str = "PP-OCRv6_rec_small.onnx",
        limit_side: int = DET_LIMIT_SIDE,
    ) -> None:
        model_dir = model_dir or DEFAULT_MODEL_DIR
        det_path = os.path.join(model_dir, det_name)
        rec_path = os.path.join(model_dir, rec_name)
        for p in (det_path, rec_path):
            if not os.path.exists(p):
                raise FileNotFoundError(
                    f"缺少 OCR 模型: {p}\n"
                    f"       模型应放在 {model_dir}（随仓库分发，勿运行时下载）")
        self._det = cv2.dnn.readNetFromONNX(det_path)
        self._rec = cv2.dnn.readNetFromONNX(rec_path)
        self._chars = _load_charset(rec_path)
        self._limit_side = int(limit_side)

    # -- 对外 ---------------------------------------------------------
    def read(
        self,
        image: np.ndarray,
        roi: Optional[Sequence[int]] = None,
        min_score: float = 0.5,
    ) -> List[TextItem]:
        """识别整图或 ROI=(x, y, w, h)。返回按阅读顺序排序的文本行。"""
        img = image
        ox = oy = 0
        if roi is not None:
            x, y, w, h = (int(v) for v in roi)
            img = self._crop_roi(image, roi)
            if img is None:
                return []
            ox, oy = x, y
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        elif img.shape[2] == 4:
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

        quad_boxes = self._detect(img)
        items: List[TextItem] = []
        for box in quad_boxes:
            crop = self._crop_quad(img, box)
            text, score = self._recognize(crop)
            if not text.strip() or score < min_score:
                continue
            bx, by, bw, bh = cv2.boundingRect(box.astype(np.int32))
            items.append(TextItem(text.strip(), round(float(score), 3),
                                  (bx + ox, by + oy, bw, bh)))
        items.sort(key=lambda it: (it.y // 12, it.x))
        return items

    def read_line(
        self,
        image: np.ndarray,
        roi: Optional[Sequence[int]] = None,
        min_score: float = 0.3,
    ) -> Tuple[str, float]:
        """**已知是一行字**时用这个：跳过检测，直接识别，最快（几十毫秒）。

        适合固定位置的读数：摩擦档位数字、坐标、价格、状态提示语。
        如果一行里有多段（如 "33,528.34 硬币"），整段会拼在一起返回。
        """
        crop = self._crop_roi(image, roi)
        if crop is None:
            return "", 0.0
        text, score = self._recognize(crop)
        text = text.strip()
        if score < min_score:
            return "", score
        return text, round(float(score), 3)

    def read_text(self, image: np.ndarray, roi=None, min_score: float = 0.5) -> str:
        return " ".join(it.text for it in self.read(image, roi, min_score))

    @staticmethod
    def _crop_roi(image: np.ndarray, roi: Optional[Sequence[int]]) -> Optional[np.ndarray]:
        """按 roi=(x,y,w,h) 裁剪并做边界收敛；越界/空返回 None。"""
        if roi is None:
            return image
        x, y, w, h = (int(v) for v in roi)
        h_img, w_img = image.shape[:2]
        x, y = max(0, x), max(0, y)
        w, h = min(w, w_img - x), min(h, h_img - y)
        if w <= 0 or h <= 0:
            return None
        crop = image[y:y + h, x:x + w]
        if crop.ndim == 2:
            crop = cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR)
        elif crop.shape[2] == 4:
            crop = cv2.cvtColor(crop, cv2.COLOR_BGRA2BGR)
        return crop

    # -- 检测 ---------------------------------------------------------
    def _detect(self, img: np.ndarray) -> List[np.ndarray]:
        h, w = img.shape[:2]
        # 短边放大到 limit_side，但**最多放大 2.5 倍**：
        # 小 ROI（几十像素高）放大十几倍既慢又不会更准（实测 470ms -> 见 read_line）
        ratio = self._limit_side / max(1, min(h, w))
        ratio = min(ratio, 2.5)
        if min(h, w) * ratio < 32:          # 太小的区域直接给个下限
            ratio = 32.0 / max(1, min(h, w))
        nh = max(32, int(round(h * ratio / 32) * 32))
        nw = max(32, int(round(w * ratio / 32) * 32))
        resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        blob = cv2.dnn.blobFromImage(resized, 1 / 127.5, (nw, nh),
                                     (127.5, 127.5, 127.5), swapRB=True)
        self._det.setInput(blob)
        pred = self._det.forward()[0, 0]

        mask = (pred > DET_THRESH).astype(np.uint8)
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        found: List[Tuple[np.ndarray, float]] = []
        for c in contours:
            if len(c) < 4:
                continue
            rect = cv2.minAreaRect(c)
            (_, _), (rw, rh), ang = rect
            if min(rw, rh) < 3:
                continue
            area = cv2.contourArea(c)
            perim = cv2.arcLength(c, True)
            if area < 10 or perim <= 0:
                continue
            dist = area * DET_UNCLIP / perim
            (cx, cy) = rect[0]
            box = cv2.boxPoints(((cx, cy), (rw + 2 * dist, rh + 2 * dist), ang))
            box = box / ratio
            box[:, 0] = np.clip(box[:, 0], 0, w - 1)
            box[:, 1] = np.clip(box[:, 1], 0, h - 1)
            found.append((box.astype(np.float32), float(area)))

        found.sort(key=lambda t: -t[1])
        keep: List[np.ndarray] = []
        for box, _ in found:
            if all(_iou(box, kb) < 0.5 for kb in keep):
                keep.append(box)
        return keep

    # -- 裁剪 / 识别 ---------------------------------------------------
    @staticmethod
    def _crop_quad(img: np.ndarray, box: np.ndarray) -> np.ndarray:
        pts = box.reshape(4, 2)
        s = pts.sum(axis=1)
        d = np.diff(pts, axis=1).reshape(-1)
        tl = pts[np.argmin(s)]
        br = pts[np.argmax(s)]
        tr = pts[np.argmin(d)]
        bl = pts[np.argmax(d)]
        src = np.array([tl, tr, br, bl], dtype=np.float32)
        w = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
        h = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
        w, h = max(w, 1), max(h, 1)
        dst = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32)
        M = cv2.getPerspectiveTransform(src, dst)
        crop = cv2.warpPerspective(img, M, (w, h), borderValue=(0, 0, 0))
        if h > w * 1.5 and h > 20:          # 竖排文字转正
            crop = np.rot90(crop, 3)
        return crop

    def _recognize(self, crop: np.ndarray) -> Tuple[str, float]:
        h, w = crop.shape[:2]
        nw = min(REC_MAX_W, max(1, int(REC_H * w / h)))
        resized = cv2.resize(crop, (nw, REC_H))
        blob = cv2.dnn.blobFromImage(resized, 1 / 127.5, (nw, REC_H),
                                     (127.5, 127.5, 127.5), swapRB=True)
        self._rec.setInput(blob)
        pred = self._rec.forward()[0]        # T x C
        idx = pred.argmax(axis=1)
        prob = pred.max(axis=1)
        out: List[str] = []
        scores: List[float] = []
        prev = -1
        for k, i in enumerate(idx):
            if i != 0 and i != prev and i < len(self._chars):
                out.append(self._chars[i])
                scores.append(float(prob[k]))
            prev = i
        return "".join(out), (float(np.mean(scores)) if scores else 0.0)
