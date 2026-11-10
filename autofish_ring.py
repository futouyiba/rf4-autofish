"""线杯圆环（wheel.png 图标外圈）的点亮比例测量 —— 纯像素、无 IO、无输入。

图标位置：底部状态图标排的第 2 个（与 ``static/*/wheel.png`` 同形），
外圈圆环就是"线杯上还剩多少线"的指示器：满环 = 线基本都在线杯上（鱼近），
环掉下来 = 线被鱼拉出去（鱼远）。

## 阈值为什么不能写死（2026-11-05 实机证据）

同一帧画面里，相邻三个状态图标的环峰值亮度分别是 **109 / 178 / 104**。
旧实现把"点亮"写死成 ``>= 180``，于是夜间/HUD 变暗时线杯环峰值只有 **178**，
整圈被判成"图标没画出来" → 返回不可读。后果是所有依赖它的判断全部失效：
临岸判定拿不到输入、收网回退无从触发、状态卡在搏鱼里出不来。

所以改成两步判定：

1. 先看环上**最亮**的角度够不够亮（低于 ``peak_min`` 才认为图标真没绘制）；
2. 再用该帧峰值**按比例**（``rel_lit``）判每个角度是否点亮。

这样"满环"在任何光照下都读成接近 1.0，"图标没画"仍然稳定返回不可读，
既不会把夜间误判成 0%，也不会把背景噪声误判成环。
"""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np

# 图标中心与环半径（1920×1080 标定）
RING_CENTER = (1237, 1023)
RING_RADII = (13, 15)

# 环上最亮角度都低于该值 ⇒ 图标根本没画出来（HUD 隐藏 / 被结算框盖住）
RING_PEAK_MIN = 60.0
# 相对峰值的点亮判据：>= peak * 0.68 视为点亮
RING_REL_LIT = 0.68
# 采样步长（2° = 180 个样本，量化误差约 0.56%，比原来的 3° 更细）
RING_STEP_DEG = 2
# 整个图标区域的灰度均值低于此值 ⇒ 全黑，直接判不可读
RING_DARK_MEAN = 20.0
# 区域均值 >= 峰值 * 该比例 ⇒ 是一块平坦色块（没有"环 + 暗内圈"结构），不是图标
RING_MIN_STRUCTURE = 0.85


def _gray(frame) -> np.ndarray:
    """转成 2D 灰度 int 数组；输入可以是 RGB/RGBA/灰度 ndarray。"""
    a = np.asarray(frame)
    if a.ndim == 3:
        a = a[:, :, :3].mean(axis=2)
    return a.astype(float)


def measure_ring(
    frame,
    center: Iterable[int] = RING_CENTER,
    radii: Iterable[int] = RING_RADII,
    *,
    peak_min: float = RING_PEAK_MIN,
    structure: float = RING_MIN_STRUCTURE,
    rel_lit: float = RING_REL_LIT,
    step_deg: int = RING_STEP_DEG,
    dark_mean: float = RING_DARK_MEAN,
):
    """返回 ``(ratio, evidence)``；``ratio`` 为 None 表示**不可读**（不是 0）。

    evidence 里的 ``reason`` 说明为什么不可读，便于日志与排错：
      * ``non_reference_frame`` —— 分辨率不是标定尺寸
      * ``hud_dark``            —— 图标区域全黑（HUD 没画）
      * ``icon_not_drawn``      —— 环上一点都不亮（图标没绘制/被遮挡）
    可读时 evidence 带 ``peak`` / ``lit_min``（实际使用的绝对阈值）/ ``ratio``。
    """
    cx, cy = int(center[0]), int(center[1])
    r0, r1 = int(radii[0]), int(radii[1])
    ev = {
        "center": [cx, cy],
        "radii": [r0, r1],
        "step_deg": int(step_deg),
        "rel_lit": float(rel_lit),
        "peak_min": float(peak_min),
        "structure": float(structure),
    }

    a = _gray(frame)
    if a.ndim != 2 or a.shape[0] <= cy + r1 or a.shape[1] <= cx + r1:
        ev["reason"] = "non_reference_frame"
        return None, ev

    win = a[cy - r1:cy + r1 + 1, cx - r1:cx + r1 + 1]
    region_mean = float(win.mean())
    ev["region_mean"] = round(region_mean, 1)
    if region_mean < dark_mean:
        ev["reason"] = "hud_dark"
        return None, ev

    best_per_angle = []
    for deg in range(0, 360, int(step_deg)):
        rad = math.radians(deg)
        best = 0.0
        for r in range(r0, r1 + 1):        # 环有 2~3px 厚，取该角度最亮的一圈
            x = int(round(r * math.cos(rad)))
            y = int(round(r * math.sin(rad)))
            v = win[r1 + y, r1 + x]
            if v > best:
                best = v
        best_per_angle.append(best)

    if not best_per_angle:
        ev["reason"] = "non_reference_frame"
        return None, ev

    peak = max(best_per_angle)
    ev["peak"] = round(peak, 1)
    if peak < peak_min:
        ev["reason"] = "icon_not_drawn"
        return None, ev

    # 平坦色块（均匀灰/纯白盘）没有"环亮、内圈暗"的结构，不能靠比例判成满环。
    if region_mean >= peak * structure:
        ev["reason"] = "icon_not_drawn"
        return None, ev

    lit_min = peak * rel_lit
    ev["lit_min"] = round(lit_min, 1)
    total = len(best_per_angle)
    lit = sum(1 for v in best_per_angle if v >= lit_min)
    ratio = lit / total
    ev["lit"] = lit
    ev["total"] = total
    ev["ratio"] = round(ratio, 3)
    return ratio, ev
