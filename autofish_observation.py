"""RF4 自动钓鱼 —— 同帧观测层（状态机阶段 0/1，纯像素、零输入）。

设计约束（硬性）：

1. **本模块只读像素**：不截图、不聚焦窗口、不派生任何键盘/鼠标事件。
   抓帧由调用方完成，并且必须把**同一张** PIL.Image 传进来 —— 一次观测里的
   鱼图标、力度条、线长圆环必须来自同一帧，否则会出现"图标是 A 帧、圆环是
   B 帧"的错配（设计文档 §1 第 3 类根因）。

2. **读不出来必须是 None / TriState.UNKNOWN**，不许用 0、1、-1 冒充：
   * `ring_ratio=None` 不是"临岸"，也不是"空环"；
   * `bar_level=0` 只表示"力度条轨道画出来了、这一帧没有彩色带"，
     此时 `bar_tier=None`（不是 1 档）；
   * `bar_level=None` 表示连轨道都没画出来（HUD 隐藏/被盖住/画面越界）。

3. 判据全部沿用 `rf4s_adapter.py` 的实测常量（2026-11-03 ~ 11-04 标定），
   复制的是**纯像素算法**，参数值一字不改；改值的理由必须写在注释里。

坐标系：所有常量都是 **1920x1080 客户区** 下的实测坐标。**画面尺寸不是
1920x1080 时，每个 detector 都直接返回 UNKNOWN/None**（安全侧：不动作、
只上报），**不做按比例缩放** —— 缩放会把"实测阈值"变成猜测。分辨率不是
1080p 时本模块等于睁眼瞎，这是已知限制，不是 bug。

上层语义：单帧 detector 只回答"这一帧看到了什么"。"连续两帧"「连续三帧」
之类的防抖属于状态机（设计文档 §3 / §7），本模块不做记忆。
"""

from __future__ import annotations

import enum
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, NamedTuple

import numpy as np

__all__ = [
    "TriState",
    "Observation",
    "BarReading",
    "observe_frame",
    "detect_fish_icon",
    "detect_bar",
    "detect_ring",
    "detect_ok_modal",
    "detect_captured",
    "detect_hud",
]


# ==================== 模型 ====================


class TriState(enum.Enum):
    """三值判据：读到了 / 确实没有 / 说不清。"""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


def _freeze(value: Any) -> Any:
    """递归把容器冻结成只读结构（键保留）。

    dict/Mapping -> MappingProxyType；list/tuple -> tuple；
    set/frozenset -> frozenset；标量原样返回。这样构造 Observation 之后，
    外部再改传入的 dict/list 不会影响已经建好的快照，嵌套层也一样。
    """
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    """`_freeze` 的逆操作：递归还原成 JSON 友好的普通 dict/list。"""
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [_thaw(v) for v in value]
    return value


@dataclass(frozen=True)
class Observation:
    """一帧画面的全部可读事实（不可变，便于跨线程/落盘传阅）。

    字段语义见模块 docstring；`evidence` 里放每个判据的原始读数与阈值，
    只用于日志解释，不参与判定（判定在 detector 内完成）。

    `evidence` 在构造时被**递归深冻结**：顶层是 `MappingProxyType`，
    list 变成 tuple。因此 `obs.evidence[...] = ...` 会抛 `TypeError`，
    嵌套的 dict/list 同样不可变；构造之后再去改外面传进来的 dict/list
    也不会反向影响这个快照。要拿可写/可 JSON 序列化的副本请用 `as_dict()`。
    """

    monotonic_ts: float
    wall_ts: float
    foreground: bool
    fish_icon: TriState
    bar_level: int | None
    bar_tier: int | None
    bar_hue: int | None
    bar_sat: int | None
    ring_ratio: float | None
    captured: TriState
    ok_modal: TriState
    hud_visible: TriState
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # frozen dataclass 只能走 object.__setattr__。重复调用 _freeze 是幂等的。
        object.__setattr__(self, "evidence", _freeze(self.evidence))

    def as_dict(self) -> dict:
        """拍平成 JSON 友好结构（枚举转字符串，evidence 递归 thaw），给 shadow 日志用。"""
        return {
            "monotonic_ts": self.monotonic_ts,
            "wall_ts": self.wall_ts,
            "foreground": self.foreground,
            "fish_icon": self.fish_icon.value,
            "bar_level": self.bar_level,
            "bar_tier": self.bar_tier,
            "bar_hue": self.bar_hue,
            "bar_sat": self.bar_sat,
            "ring_ratio": (None if self.ring_ratio is None
                           else round(self.ring_ratio, 3)),
            "captured": self.captured.value,
            "ok_modal": self.ok_modal.value,
            "hud_visible": self.hud_visible.value,
            "evidence": _thaw(self.evidence),
        }


# ==================== 实测常量（与 rf4s_adapter.py 对齐） ====================

# 全部常量都是这个客户区尺寸下的坐标。尺寸不符 -> 所有判据 UNKNOWN/None。
REFERENCE_SIZE = (1920, 1080)

# ---- 底部圆形鱼图标：力度条上一行、最左侧、「剩余耐力」左边 ----
# 实测（10fps 连拍 1447 帧）：有鱼 110~133 个白像素，无鱼 0~22 个。
FISH_ICON_CENTER = (549, 1024)
FISH_ICON_HALF = (20, 16)          # 裁剪半径：x, y
FISH_ICON_RING = (9, 14)           # 外环半径区间
FISH_ICON_WHITE_MIN = 60           # 外环白像素阈值
FISH_ICON_V_MIN = 238              # "白"的亮度下限
FISH_ICON_S_MAX = 28               # "白"的饱和度上限

# ---- 底部钓具承受力度横条 ----
# 轨道 y≈1045..1058、x≈550..1400，平时灰色；有负载时出现一段浮动渐变带，
# 带子不透明（S≈155~170、V≈200），背景透色只有 S≈60~85。
FRICTION_BAR = (1045, 1058, 550, 1400)   # y1, y2, x1, x2
BAR_SAT_MIN = 130
BAR_VAL_MIN = 110
BAR_MIN_WIDTH = 12                 # 带子至少这么宽才认
BAR_LEAD_FRACTION = 0.25           # 前缘 = 最右 25%
BAR_LEAD_MIN_PX = 8
# 轨道是否画出来：轨道带比它上方 12 行（同一 x 区间）更亮。
# 实测（真实截图）：HUD 可见 +11.6~50.2；菜单 0.0；结算画面 -3.2；弹窗 -17。
BAR_TRACK_REF_ROWS = (1030, 1042)
BAR_TRACK_DIFF_MIN = 8.0
# 档位阈值（玩家指认截图标定）：hue<=10 或 >=170 红；<=29 橙黄；<=85 黄绿。
BAR_HUE_RED_MAX = 10
BAR_HUE_RED_WRAP = 170
BAR_HUE_AMBER_MAX = 29
BAR_HUE_YELLOWGREEN_MAX = 85

# ---- 右下角线长圆环 ----
# 半径 r13~15 是进度环，r10~12 是图标内圈（会带偏比例）。
LINE_RING_CENTER = (1237, 1023)
LINE_RING_RADII = (13, 15)
LINE_RING_LIT = 180                # "亮"的灰度阈值
LINE_RING_DARK_MEAN = 20.0         # 区域内全黑 = HUD 没画出来
LINE_RING_STEP_DEG = 3

# ---- 「确定」按钮 ----
# 实测边框 x871..1048、y793..826；判据 = 上下边框亮像素占比 > 85%。
OK_BTN_BOX = (871, 793, 1048, 826)   # x0, y0, x1, y1
OK_BTN_BRIGHT = 150
OK_BTN_FRAC = 0.85
# 2026-11-04 真实日志：搏鱼 HUD/夜雾高光也会在按钮框的两个横向上画出
# 连续亮行（`_hud_now.png` top=1.00 bot=0.96），只看上下两行必然假阳性。
# 追加三条结构判据（与真实 `_modal.png` / `_after_ok*.png` 离线标定）：
#   * 左右竖边框：按钮是闭合矩形，竖边也必须亮；
#   * 按钮外部（框上下各 12 行）必须比边框暗 —— 弹窗底色 V≈60，
#     夜雾/水面高光的外侧 V≈160~172；
#   * 边框与外侧的亮度差 >= 60，防止"整片亮区里恰好有两条亮线"。
# 阈值取自本地已有截图，未抓新图；真实回归见 tests/test_observation.py。
OK_BTN_SURROUND_ROWS = 12
OK_BTN_SURROUND_MAX = 110.0          # 框外均值上限（弹窗≈60，雾面≈170）
OK_BTN_CONTRAST_MIN = 60.0           # 边框均值 - 框外均值（弹窗≈190，雾面≈0）
OK_BTN_EDGE_FRAC = 0.60              # 左右竖边框至少一侧的亮像素占比

# ---- 渔获结算：底部「入护 [Space]」「释放 [Backspace]」按钮 ----
# 适配器用 keep.png 模板匹配（0.95）+ 位置框；本模块不依赖模板文件，
# 改用按钮几何（成对平行边框 + 竖分隔线 + 尺寸/位置约束），见 detect_captured。
CAPTURE_BTN_SCAN = (900, 1079, 600, 1320)   # y0, y1, x0, x1
CAPTURE_BTN_BRIGHT = 150
CAPTURE_BTN_ROW_MIN_PX = 150        # 一行至少这么多个亮像素才算"边框行"
CAPTURE_BTN_GAP = (40, 90)          # 上下边框的高度差（按钮会上下浮动）
CAPTURE_BTN_EDGE_TOL = 8            # 上下边框左右端点允许的偏差
CAPTURE_BTN_WIDTH = (380, 560)      # 两个按钮合起来的宽度（实测 497）
CAPTURE_BTN_CENTER = (860, 1060)    # 水平中心（实测 959.5）
CAPTURE_EDGE_MARGIN = 10            # 边框不得贴扫描区边缘（贴边=背景误判）
CAPTURE_DIVIDER_FRAC = 0.7          # 竖线需纵贯按钮高度的比例
CAPTURE_DIVIDER_MIN = 3             # 左/中/右三条竖线

# ---- HUD 总判 ----
HUD_DARK_MEAN = 40.0               # 三条线索全负 + 整帧这么暗 -> 判 HUD 不可见
FRAME_LUMA_STEP = 4                # 整帧平均亮度的降采样步长


class BarReading(NamedTuple):
    """力度条的单帧读数。level=None 表示不可读，不是 0。"""

    level: int | None
    tier: int | None
    hue: int | None
    sat: int | None
    evidence: dict


# ==================== 基础像素工具 ====================


def _as_array(image: Any) -> np.ndarray | None:
    """PIL.Image / ndarray -> (H, W, 3) uint8 RGB；不可读时 None。"""
    if isinstance(image, np.ndarray):
        arr = image
    else:
        try:
            src = (image if getattr(image, "mode", None) == "RGB"
                   else image.convert("RGB"))
            arr = np.asarray(src)
        except Exception:
            return None
    if arr.ndim != 3 or arr.shape[2] < 3 or arr.size == 0:
        return None
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(arr[..., :3])


def _hsv(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """RGB(uint8) -> OpenCV 口径的 (H, S, V)。

    H 为 0..179（度/2，四舍五入）、S/V 为 0..255。之所以自己算而不是调
    cv2：bot 的默认 Python 环境里没有 cv2，而观测层必须是纯 numpy 可测的。
    口径按实测色反推校验过：RGB(200,214,63)->H=33、RGB(229,188,0)->H=25。
    """
    a = rgb.astype(np.int32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    delta = mx - mn

    sat = np.rint(delta * 255.0 / np.maximum(mx, 1)).astype(np.int32)

    d = np.maximum(delta, 1)
    hue = np.where(
        mx == r, (((g - b) / d) % 6.0),
        np.where(mx == g, (b - r) / d + 2.0, (r - g) / d + 4.0),
    ) * 60.0
    hue = np.where(delta == 0, 0.0, hue)
    hue_code = np.rint(hue / 2.0).astype(np.int32) % 180
    return hue_code, sat, mx


def _luma(rgb: np.ndarray) -> np.ndarray:
    a = rgb.astype(np.float32)
    return 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]


def _gray(rgb: np.ndarray) -> np.ndarray:
    return np.rint(_luma(rgb)).astype(np.int32)


def _size_ok(arr: np.ndarray) -> bool:
    """画面必须是 1920x1080 客户区，否则坐标常量全部失配。"""
    return (arr.shape[1], arr.shape[0]) == REFERENCE_SIZE


def _frame_luma(arr: np.ndarray) -> float:
    """整帧平均亮度（降采样，只用于"全暗"这个粗判）。

    全分辨率算一次要 ~35ms，观测循环里白花；亮度只做数量级判断，4x4
    降采样足够，误差 <1。
    """
    sub = arr[::FRAME_LUMA_STEP, ::FRAME_LUMA_STEP]
    return round(float(_luma(sub).mean()), 1)


def _longest_run(col: np.ndarray) -> tuple[int, int]:
    """带缝容错的"连续列"最长区间，返回闭区间 (i, j)。

    与适配器同算法：列占比 > 0.5 才算"在带子里"，允许 ≤2px 的断裂
    （抗锯齿/刻度线）。
    """
    best = (0, -1)
    i = 0
    n = len(col)
    while i < n:
        if col[i] > 0.5:
            j = i
            gap = 0
            k = i
            while k < n:
                if col[k] > 0.5:
                    j = k
                    gap = 0
                else:
                    gap += 1
                    if gap > 2:
                        break
                k += 1
            if j - i > best[1] - best[0]:
                best = (i, j)
            i = k
        else:
            i += 1
    return best


# ==================== 单帧 detector（纯像素） ====================


def _fish_icon(arr: np.ndarray) -> tuple[TriState, dict]:
    cx, cy = FISH_ICON_CENTER
    hw, hh = FISH_ICON_HALF
    ev = {
        "center": [cx, cy],
        "half": [hw, hh],
        "ring": list(FISH_ICON_RING),
        "white_min": FISH_ICON_WHITE_MIN,
    }
    if not _size_ok(arr):
        ev["reason"] = "non_reference_frame"
        return TriState.UNKNOWN, ev

    crop = arr[cy - hh:cy + hh, cx - hw:cx + hw]
    _, s, v = _hsv(crop)
    yy, xx = np.mgrid[0:hh * 2, 0:hw * 2]
    rr = np.sqrt((yy - hh) ** 2 + (xx - hw) ** 2)
    ring = (rr >= FISH_ICON_RING[0]) & (rr <= FISH_ICON_RING[1])
    white = (v >= FISH_ICON_V_MIN) & (s <= FISH_ICON_S_MAX)
    count = int(white[ring].sum())

    ev["white_pixels"] = count
    ev["ring_pixels"] = int(ring.sum())
    state = (TriState.TRUE if count >= FISH_ICON_WHITE_MIN
             else TriState.FALSE)
    return state, ev


def _level_from_hue(hue: int) -> int:
    """色相 -> 适配器的四色等级（0 只由"没有带子"产生，不在这里返回）。"""
    if hue <= BAR_HUE_RED_MAX or hue >= BAR_HUE_RED_WRAP:
        return 4
    if hue <= BAR_HUE_AMBER_MAX:
        return 3
    if hue <= BAR_HUE_YELLOWGREEN_MAX:
        return 2
    return 1


def _tier_from_level(level: int | None) -> int | None:
    """四色等级 -> 玩家三档（1=力小 / 2=目标 / 3=红）。

    刻意与适配器 `_tier()` 不同：适配器把 0（无带子/松线）当成 1 档，
    观测层必须返回 None —— "没读到"不能伪装成"读到 1 档"。
    等级 1 是冷色（hue 86..169），实测从未出现过，档位不猜。
    """
    if level is None or level <= 0:
        return None
    if level >= 4:
        return 3
    if level == 3:
        return 2
    if level == 2:
        return 1
    return None


def _bar(arr: np.ndarray) -> BarReading:
    y1, y2, x1, x2 = FRICTION_BAR
    ev = {
        "region": [y1, y2, x1, x2],
        "sat_min": BAR_SAT_MIN,
        "val_min": BAR_VAL_MIN,
        "min_width": BAR_MIN_WIDTH,
        "track_diff_min": BAR_TRACK_DIFF_MIN,
    }
    if not _size_ok(arr):
        ev["reason"] = "non_reference_frame"
        return BarReading(None, None, None, None, ev)

    strip = arr[y1:y2, x1:x2]
    hh, ss, vv = _hsv(strip)
    band = (ss >= BAR_SAT_MIN) & (vv >= BAR_VAL_MIN)
    col = band.mean(axis=0)
    i, j = _longest_run(col)
    width = j - i
    ev["band_width"] = int(width)

    # 轨道是否画出来：与正上方 12 行的亮度差（HUD 隐藏/被盖住时这个差为 0 或负）
    r0, r1 = BAR_TRACK_REF_ROWS
    strip_l = float(_luma(strip).mean())
    ref_l = float(_luma(arr[r0:r1, x1:x2]).mean())
    track_diff = round(strip_l - ref_l, 1)
    ev["track_luma"] = round(strip_l, 1)
    ev["ref_luma"] = round(ref_l, 1)
    ev["track_diff"] = track_diff
    track_drawn = track_diff >= BAR_TRACK_DIFF_MIN
    ev["track_drawn"] = bool(track_drawn)
    ev["inspectable"] = True

    if width < BAR_MIN_WIDTH:
        if not track_drawn:
            ev["reason"] = "track_not_drawn"
            return BarReading(None, None, None, None, ev)
        ev["reason"] = "no_band"
        ev["level"] = 0
        return BarReading(0, None, None, None, ev)

    n_lead = max(BAR_LEAD_MIN_PX, int(width * BAR_LEAD_FRACTION))
    la, lb = j - n_lead + 1, j + 1
    lead = band[:, la:lb]
    hue = int(np.median(hh[:, la:lb][lead]))
    sat = int(np.median(ss[:, la:lb][lead]))
    level = _level_from_hue(hue)
    tier = _tier_from_level(level)

    ev["span"] = [int(x1 + i), int(x1 + j)]
    ev["hue"] = hue
    ev["sat"] = sat
    ev["level"] = level
    ev["tier"] = tier
    if level == 1:
        ev["unclassified_hue"] = True
    return BarReading(level, tier, hue, sat, ev)


def _ring(arr: np.ndarray) -> tuple[float | None, dict]:
    cx, cy = LINE_RING_CENTER
    r0, r1 = LINE_RING_RADII
    ev = {
        "center": [cx, cy],
        "radii": [r0, r1],
        "lit_min": LINE_RING_LIT,
        "step_deg": LINE_RING_STEP_DEG,
    }
    if not _size_ok(arr):
        ev["reason"] = "non_reference_frame"
        return None, ev

    gray = _gray(arr[cy - r1:cy + r1 + 1, cx - r1:cx + r1 + 1])
    region_mean = float(gray.mean())
    ev["region_mean"] = round(region_mean, 1)
    if region_mean < LINE_RING_DARK_MEAN:
        ev["reason"] = "hud_dark"
        return None, ev

    lit = 0
    total = 0
    peak = 0
    for deg in range(0, 360, LINE_RING_STEP_DEG):
        rad = math.radians(deg)
        best = 0
        for r in range(r0, r1 + 1):      # 环有 2~3px 厚，取该角度最亮的一圈
            x = int(round(r * math.cos(rad)))
            y = int(round(r * math.sin(rad)))
            best = max(best, int(gray[r1 + y, r1 + x]))
        peak = max(peak, best)
        total += 1
        if best >= LINE_RING_LIT:
            lit += 1

    ev["peak"] = peak
    ev["lit"] = lit
    ev["total"] = total
    if peak < LINE_RING_LIT:
        # 整圈最亮的角度都不够亮 = 这个图标根本没画出来（HUD 隐藏/被结算框盖住），
        # 和"圆环真的空到 0%"是两回事。
        ev["reason"] = "icon_not_drawn"
        return None, ev

    ratio = lit / total if total else None
    ev["ratio"] = None if ratio is None else round(ratio, 3)
    return ratio, ev


def _ok_modal(arr: np.ndarray) -> tuple[TriState, dict]:
    """「确定」按钮：上下 + 左右边框 + 框内外亮度对比。

    只看上下两条亮行会在搏鱼 HUD / 夜雾高光上假阳性（2026-11-04 真实日志）。
    真实按钮是一个闭合矩形：上下边框亮、左右竖边也亮，且按钮外面是深色
    弹窗底板（V≈60）；夜雾/水面高光的外侧同样是亮的（V≈160+），由
    `OK_BTN_SURROUND_MAX` 与 `OK_BTN_CONTRAST_MIN` 一并拒掉。

    ⚠️ 仍然只是"这一帧看起来有确定按钮"；连续帧防抖在状态机里做。
    """
    x0, y0, x1, y1 = OK_BTN_BOX
    ev = {
        "box": [x0, y0, x1, y1],
        "bright_min": OK_BTN_BRIGHT,
        "frac_min": OK_BTN_FRAC,
        "edge_frac_min": OK_BTN_EDGE_FRAC,
        "surround_rows": OK_BTN_SURROUND_ROWS,
        "surround_max": OK_BTN_SURROUND_MAX,
        "contrast_min": OK_BTN_CONTRAST_MIN,
    }
    if not _size_ok(arr):
        ev["reason"] = "non_reference_frame"
        return TriState.UNKNOWN, ev

    gray = _gray(arr)
    # 含 x1 这一列：实测右竖边框正好落在 x1-1..x1，旧切片漏掉了最右一列。
    box = gray[y0:y1 + 1, x0:x1 + 1]
    bright = box >= OK_BTN_BRIGHT

    top_frac = float(bright[0].mean())
    bot_frac = float(bright[-1].mean())
    left_frac = float((bright[:, 0] | bright[:, 1]).mean())
    right_frac = float((bright[:, -2] | bright[:, -1]).mean())

    r = OK_BTN_SURROUND_ROWS
    above = gray[y0 - r:y0, x0:x1 + 1]
    below = gray[y1 + 1:y1 + 1 + r, x0:x1 + 1]
    above_mean = round(float(above.mean()), 1)
    below_mean = round(float(below.mean()), 1)
    surround_mean = max(above_mean, below_mean)
    border_mean = round(float(box[0].mean() + box[-1].mean()) / 2.0, 1)
    contrast = round(border_mean - surround_mean, 1)

    checks = {
        "top": top_frac > OK_BTN_FRAC,
        "bottom": bot_frac > OK_BTN_FRAC,
        "vertical": (left_frac >= OK_BTN_EDGE_FRAC
                     or right_frac >= OK_BTN_EDGE_FRAC),
        "surround_dark": surround_mean <= OK_BTN_SURROUND_MAX,
        "contrast": contrast >= OK_BTN_CONTRAST_MIN,
    }
    ev.update({
        "top_frac": round(top_frac, 3),
        "bottom_frac": round(bot_frac, 3),
        "left_frac": round(left_frac, 3),
        "right_frac": round(right_frac, 3),
        "above_mean": above_mean,
        "below_mean": below_mean,
        "surround_mean": surround_mean,
        "border_mean": border_mean,
        "contrast": contrast,
        "checks": checks,
    })

    hit = all(checks.values())
    state = TriState.TRUE if hit else TriState.FALSE
    ev["state"] = state.value
    if not hit:
        # 用失败项名字直接说明假阳性来源，便于日志复盘。
        ev["reason"] = "structure_reject:" + ",".join(
            k for k, ok in checks.items() if not ok)
    return state, ev


def _captured(arr: np.ndarray) -> tuple[TriState, dict]:
    """结算框：底部一对「入护/释放」按钮。

    适配器的判据是 keep.png 模板匹配 0.95 + 位置框（CAPTURE_POS_BOX）；本模块
    不读模板文件，改用几何判据：成对平行亮边框（上下差 40~90px、端点差 <8px）
    + 按钮高度内有左/中/右三条竖线 + 总宽 380~560、中心 860~1060、不贴扫描区边缘。
    实测 14 张真实截图：只有结算画面命中（宽 497、中心 959.5），
    弹窗/待抛竿/菜单/搏鱼全部拒掉（它们的候选宽度都是 680~719）。

    ⚠️ 只回答"结算按钮在不在"，不保证与 keep.png 模板同源；模板那边能识别
    "结算框出现但按钮被挡住"的画面，这里识别不了（返回 FALSE 而不是 TRUE）。
    """
    y0, y1, x0, x1 = CAPTURE_BTN_SCAN
    ev = {
        "scan": [y0, y1, x0, x1],
        "gap": list(CAPTURE_BTN_GAP),
        "width_range": list(CAPTURE_BTN_WIDTH),
        "center_x_range": list(CAPTURE_BTN_CENTER),
    }
    if not _size_ok(arr):
        ev["reason"] = "non_reference_frame"
        return TriState.UNKNOWN, ev

    gray = _gray(arr[y0:y1, x0:x1])
    rows = []
    for dy in range(gray.shape[0]):
        xs = np.where(gray[dy] >= CAPTURE_BTN_BRIGHT)[0]
        if xs.size > CAPTURE_BTN_ROW_MIN_PX:
            rows.append((y0 + dy, x0 + int(xs.min()), x0 + int(xs.max())))
    ev["border_rows"] = len(rows)

    cand = None
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            dy = rows[j][0] - rows[i][0]
            if not (CAPTURE_BTN_GAP[0] <= dy <= CAPTURE_BTN_GAP[1]):
                continue
            if abs(rows[i][1] - rows[j][1]) >= CAPTURE_BTN_EDGE_TOL:
                continue
            if abs(rows[i][2] - rows[j][2]) >= CAPTURE_BTN_EDGE_TOL:
                continue
            cand = (rows[i], rows[j])
            break
        if cand:
            break

    if cand is None:
        ev["reason"] = "no_border_pair"
        return TriState.FALSE, ev

    (yt, xa, xb), (yb, _, _) = cand
    width = xb - xa
    center = (xa + xb) / 2.0
    band = gray[yt + 2 - y0:yb - 2 - y0, xa - x0:xb - x0]
    if band.size == 0:
        ev["reason"] = "empty_band"
        return TriState.FALSE, ev

    cols = (band >= CAPTURE_BTN_BRIGHT).sum(axis=0)
    vs = np.where(cols > (yb - yt - 6) * CAPTURE_DIVIDER_FRAC)[0]
    dividers = int(vs.size)
    mid = xa + int(vs[vs.size // 2]) if dividers >= 3 else (xa + xb) // 2

    checks = {
        "width": CAPTURE_BTN_WIDTH[0] <= width <= CAPTURE_BTN_WIDTH[1],
        "center": CAPTURE_BTN_CENTER[0] <= center <= CAPTURE_BTN_CENTER[1],
        "inside": (xa > x0 + CAPTURE_EDGE_MARGIN
                   and xb < x1 - CAPTURE_EDGE_MARGIN),
        "dividers": dividers >= CAPTURE_DIVIDER_MIN,
    }
    ev["button_box"] = [xa, yt, xb, yb]
    ev["button_width"] = int(width)
    ev["button_center_x"] = round(center, 1)
    ev["dividers"] = dividers
    ev["checks"] = checks
    ev["keep_btn_center"] = [(xa + mid) // 2, (yt + yb) // 2]

    hit = all(checks.values())
    state = TriState.TRUE if hit else TriState.FALSE
    ev["state"] = state.value
    if not hit:
        ev["reason"] = "geometry_reject"
    return state, ev


def _hud(
    arr: np.ndarray,
    fish: TriState,
    bar_level: int | None,
    ring: float | None,
    bar_ev: dict,
    frame_luma: float | None = None,
) -> tuple[TriState, dict]:
    """HUD 是否画出来了：只看底部三处 UI 的绘制痕迹，不靠整帧亮度下结论。

    正向线索（任一成立即"有 HUD"）：
      * 鱼图标白环出现（图标本身就是 HUD 的一部分）
      * 力度条有色带
      * 力度条轨道比上方背景亮（轨道被绘制）
      * 线长圆环可读（图标画出来了）

    全负时才往下走：只有"轨道确实没画 + 鱼图标确实没有 + 圆环没画"**并且**
    整帧很暗（菜单/登录那种黑底）才判 FALSE；全负但画面很亮（结算画面、
    被面板盖住）判 UNKNOWN —— 分不清是"没 HUD"还是"HUD 被挡住了"。
    """
    positives = []
    if fish is TriState.TRUE:
        positives.append("fish_icon")
    if bar_level is not None and bar_level > 0:
        positives.append("bar_band")
    if bar_ev.get("track_drawn"):
        positives.append("bar_track")
    if ring is not None:
        positives.append("ring")

    frame_luma = _frame_luma(arr) if frame_luma is None else frame_luma
    ev = {
        "positives": positives,
        "frame_luma": frame_luma,
        "dark_luma_max": HUD_DARK_MEAN,
    }
    if positives:
        ev["reason"] = "hud_drawn"
        return TriState.TRUE, ev

    if not bar_ev.get("inspectable") or fish is TriState.UNKNOWN:
        ev["reason"] = "not_inspectable"
        return TriState.UNKNOWN, ev

    if frame_luma < HUD_DARK_MEAN:
        ev["reason"] = "all_cues_absent_and_dark"
        return TriState.FALSE, ev

    ev["reason"] = "all_cues_absent_but_bright"
    return TriState.UNKNOWN, ev


# ==================== 对外接口 ====================


def detect_fish_icon(image) -> tuple[TriState, dict]:
    """底部圆形鱼图标 = 有鱼咬钩。"""
    arr = _as_array(image)
    if arr is None:
        return TriState.UNKNOWN, {"reason": "unreadable_frame"}
    return _fish_icon(arr)


def detect_bar(image) -> BarReading:
    """底部力度条：(level, tier, hue, sat, evidence)。

    level=None 不可读 / 0 有轨道但无色带 / 1..4 四色等级；
    tier=1/2/3 玩家三档，level<=0 或冷色带时为 None。
    """
    arr = _as_array(image)
    if arr is None:
        return BarReading(None, None, None, None,
                          {"reason": "unreadable_frame"})
    return _bar(arr)


def detect_ring(image) -> tuple[float | None, dict]:
    """线长圆环点亮比例 0..1；不可读返回 None（**不是** -1/0/1）。"""
    arr = _as_array(image)
    if arr is None:
        return None, {"reason": "unreadable_frame"}
    return _ring(arr)


def detect_ok_modal(image) -> tuple[TriState, dict]:
    """本帧是否有「确定」按钮。连续两帧的防抖在状态机里做。"""
    arr = _as_array(image)
    if arr is None:
        return TriState.UNKNOWN, {"reason": "unreadable_frame"}
    return _ok_modal(arr)


def detect_captured(image) -> tuple[TriState, dict]:
    """本帧是否是渔获结算画面（底部「入护/释放」按钮成对出现）。"""
    arr = _as_array(image)
    if arr is None:
        return TriState.UNKNOWN, {"reason": "unreadable_frame"}
    return _captured(arr)


def detect_hud(image) -> tuple[TriState, dict]:
    """HUD 是否可见。证据不足返回 UNKNOWN。"""
    arr = _as_array(image)
    if arr is None:
        return TriState.UNKNOWN, {"reason": "unreadable_frame"}
    fish, _ = _fish_icon(arr)
    bar = _bar(arr)
    ring, _ = _ring(arr)
    return _hud(arr, fish, bar.level, ring, bar.evidence)


def observe_frame(
    image,
    *,
    foreground: bool,
    monotonic_ts: float | None = None,
    wall_ts: float | None = None,
) -> Observation:
    """从**同一张**帧里提取全部判据。

    `foreground` 由调用方（窗口层）提供，本模块不查窗口状态。
    时间戳默认在函数入口取一次，保证同一次观测只有一个时刻。
    """
    mono = time.monotonic() if monotonic_ts is None else monotonic_ts
    wall = time.time() if wall_ts is None else wall_ts

    arr = _as_array(image)
    base = {"foreground": foreground}
    if arr is None:
        base["frame"] = {"reason": "unreadable_frame"}
        return Observation(
            monotonic_ts=mono,
            wall_ts=wall,
            foreground=foreground,
            fish_icon=TriState.UNKNOWN,
            bar_level=None,
            bar_tier=None,
            bar_hue=None,
            bar_sat=None,
            ring_ratio=None,
            captured=TriState.UNKNOWN,
            ok_modal=TriState.UNKNOWN,
            hud_visible=TriState.UNKNOWN,
            evidence=base,
        )

    fish, fish_ev = _fish_icon(arr)
    bar = _bar(arr)
    ring, ring_ev = _ring(arr)
    ok, ok_ev = _ok_modal(arr)
    cap, cap_ev = _captured(arr)
    frame_luma = _frame_luma(arr)
    hud, hud_ev = _hud(arr, fish, bar.level, ring, bar.evidence, frame_luma)

    evidence = {
        "frame": {
            "size": [int(arr.shape[1]), int(arr.shape[0])],
            "luma_mean": frame_luma,
        },
        "foreground": foreground,
        "fish_icon": fish_ev,
        "bar": bar.evidence,
        "ring": ring_ev,
        "ok_modal": ok_ev,
        "captured": cap_ev,
        "hud": hud_ev,
    }
    return Observation(
        monotonic_ts=mono,
        wall_ts=wall,
        foreground=foreground,
        fish_icon=fish,
        bar_level=bar.level,
        bar_tier=bar.tier,
        bar_hue=bar.hue,
        bar_sat=bar.sat,
        ring_ratio=ring,
        captured=cap,
        ok_modal=ok,
        hud_visible=hud,
        evidence=evidence,
    )
