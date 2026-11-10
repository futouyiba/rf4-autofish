"""坐标驱动导航：读小地图坐标 + 罗盘朝向，鼠标转向，键盘前进。

## 这套方案的依据（2026-11-06 实机标定）

| 项 | 实测值 |
|---|---|
| 坐标显示 | 右下角小地图圆盘内，`72:58` |
| 坐标 ROI | `(1725, 950, 160, 56)`，raw 直接识别，置信度 0.97+ |
| 坐标系 | +x = 东，+y = 北；`bearing = atan2(dx, dy)` |
| 罗盘 | 圆盘边缘 N（蓝块）/ S（红块），随视角旋转 |
| 玩家朝向 | `-θ_N`（θ_N = N 标记相对圆心、以正上方为 0 的顺时针角） |
| **A/D 键** | **是横移，不是转身**（按住 D 一秒朝向完全不变） |
| 转身方式 | **鼠标水平相对移动**，约 9.6 px/度 |
| 移动速度 | 约 0.33 坐标单位/秒（1 单位约 4~5 米） |

A/D 不转身这一条是决定性的：任何"按 A/D 转向"的实现都会变成横向平移，
永远调不准方向。所以本模块的转向**只用鼠标**，朝向**只从罗盘读**。

## 为什么不用"走一步看位移"估朝向

坐标是整数，1 单位要走近 3 秒，用位移估朝向既慢又不准。罗盘每帧都能读到
（实测波动 < 0.1°），转向后立刻能验证是否到位。

## 失败处理

读不到坐标、读不到罗盘、走路坐标不变，全部**明确抛错**，绝不猜方向继续走：

| stage | 触发 |
|---|---|
| `timeout` | 超过 `NavConfig.timeout` |
| `blind` | 连续 `blind_limit` 次读不到坐标 |
| `heading_blind` | 连续 `heading_blind_limit` 次读不到罗盘 |
| `stuck` | 连续 `stuck_limit` 次前进后坐标没变 |
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, Sequence, Tuple

Coord = Tuple[float, float]

# 坐标文本区域（1920×1080 客户区实测）：右下角小地图圆盘内偏下的一行。
# 全屏 OCR 会把首字符切掉（检测框只框到 `67`），所以必须用固定 ROI。
# 上界 950 是为了避开圆盘中心的白色箭头；ROI 再放大反而会把 `59` 读成 `50`。
COORD_ROI = (1725, 950, 160, 56)

# 罗盘：N（蓝）/ S（红）方块在圆盘边缘，随视角旋转
DIAL_SEARCH = (1650, 850, 1920, 1080)
DIAL_CENTER = (1792, 952)
DIAL_R_MIN = 55
DIAL_R_MAX = 130
DIAL_MIN_AREA = 60
DIAL_NS_TOLERANCE = 35.0

_COORD_RE = re.compile(r"^(\d{1,4})\s*[.:：]\s*(\d{1,4})$")
COORD_MIN = 0
COORD_MAX = 2000


class NavError(RuntimeError):
    """导航失败；``stage`` 说明卡在哪一类问题上。"""

    def __init__(self, stage: str, detail: str = "") -> None:
        self.stage = stage
        self.detail = detail
        super().__init__("%s:%s" % (stage, detail) if detail else stage)


class NavIo(Protocol):
    def read_coord(self) -> Optional[Coord]:
        """读当前坐标；读不到返回 None（绝不返回 (0,0) 冒充）。"""

    def read_heading(self) -> Optional[float]:
        """读角色朝向（0=北、顺时针）；读不到返回 None。"""

    def move_mouse(self, dx: int) -> None:
        """鼠标水平相对移动 dx 像素（正=向右=顺时针转）。"""

    def key_down(self, name: str) -> None:
        ...

    def key_up(self, name: str) -> None:
        ...


# ----------------------------------------------------------------------
# 纯几何
# ----------------------------------------------------------------------
def parse_coord(text: str) -> Optional[Coord]:
    """把 `123:456` / `123：456` / `123.456` / `1 2 3 : 4 5 6` 解析成 (123, 456)。"""
    if not text:
        return None
    m = _COORD_RE.match(str(text).replace(" ", "").strip())
    if not m:
        return None
    x, y = int(m.group(1)), int(m.group(2))
    if not (COORD_MIN <= x <= COORD_MAX and COORD_MIN <= y <= COORD_MAX):
        return None
    return float(x), float(y)


def pick_coord(items: Sequence[Any]) -> Optional[Coord]:
    """从 OCR 结果里挑一个坐标（置信度最高、且不是 (0,0)）。"""
    best: Optional[Coord] = None
    best_score = -1.0
    for item in items:
        if isinstance(item, dict):
            text = item.get("text", "")
            score = float(item.get("score", 0.0) or 0.0)
        else:
            text = str(item)
            score = 1.0
        coord = parse_coord(text)
        if coord is None or coord == (0.0, 0.0):
            continue
        if score > best_score:
            best, best_score = coord, score
    return best


def distance(a: Coord, b: Coord) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def bearing(src: Coord, dst: Coord) -> float:
    """从 src 指向 dst 的方位角，0°=+y（北），顺时针为正，范围 [0,360)。"""
    dx = dst[0] - src[0]
    dy = dst[1] - src[1]
    return math.degrees(math.atan2(dx, dy)) % 360.0


def normalize_deg(angle: float) -> float:
    """把角度折到 (-180, 180]；正值=顺时针（右转）。"""
    a = math.fmod(angle + 180.0, 360.0)
    if a <= 0:
        a += 360.0
    return a - 180.0


# ----------------------------------------------------------------------
# 读图：坐标文本 / 罗盘朝向
# ----------------------------------------------------------------------
def _bgr(frame: Any) -> Any:
    import numpy as np

    arr = np.asarray(frame)
    if arr.ndim == 3 and arr.shape[2] >= 3:
        arr = arr[:, :, :3]
        if type(frame).__module__.startswith("PIL"):
            arr = arr[:, :, ::-1]
        return np.ascontiguousarray(arr)
    return arr


def read_coord_from_frame(ocr: Any, frame: Any, roi: Sequence[int] = COORD_ROI,
                          min_score: float = 0.2) -> Tuple[Optional[Coord], str, float]:
    """从一帧画面读坐标，返回 (coord, 原始文本, 置信度)。

    `frame` 可以是 RGB 的 ``PIL.Image`` 或 BGR 的 ``ndarray``。
    """
    try:
        arr = _bgr(frame)
    except Exception:  # noqa: BLE001 - 不是图像
        return None, "", 0.0
    try:
        text, score = ocr.read_line(arr, roi=roi, min_score=min_score)
    except Exception:  # noqa: BLE001 - OCR 不可用时按"没读到"处理
        return None, "", 0.0
    return parse_coord(text), text, float(score)


def find_dial_marks(frame: Any) -> dict:
    """在右下角区域找 N（蓝）/ S（红）方位标记，返回 {name: (x, y, area)}。"""
    import cv2
    import math as _m

    bgr = _bgr(frame)
    x0, y0, x1, y1 = DIAL_SEARCH
    roi = bgr[y0:y1, x0:x1]
    if roi.size == 0:
        return {}
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    masks = {
        "N": cv2.inRange(hsv, (95, 150, 80), (125, 255, 255)),
        "S": cv2.inRange(hsv, (0, 150, 90), (10, 255, 255))
             | cv2.inRange(hsv, (170, 150, 90), (180, 255, 255)),
    }
    out = {}
    for name, mask in masks.items():
        n, _labels, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
        best = None
        for i in range(1, n):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < DIAL_MIN_AREA:
                continue
            cx, cy = cents[i][0] + x0, cents[i][1] + y0
            r = _m.hypot(cx - DIAL_CENTER[0], cy - DIAL_CENTER[1])
            if not (DIAL_R_MIN <= r <= DIAL_R_MAX):
                continue
            if best is None or area > best[2]:
                best = (float(cx), float(cy), area)
        if best is not None:
            out[name] = best
    return out


def dial_center(marks: dict) -> Tuple[float, float]:
    """N/S 都在时取两点中点（比写死坐标更抗 UI 缩放）；否则退回标定值。"""
    if "N" in marks and "S" in marks:
        (nx, ny, _a), (sx, sy, _b) = marks["N"], marks["S"]
        return ((nx + sx) / 2.0, (ny + sy) / 2.0)
    return (float(DIAL_CENTER[0]), float(DIAL_CENTER[1]))


def polar_angle(center: Tuple[float, float], point: Tuple[float, float]) -> float:
    """相对圆心的极角：0°=正上方，顺时针为正，范围 [0,360)。"""
    dx = point[0] - center[0]
    up = center[1] - point[1]
    return math.degrees(math.atan2(dx, up)) % 360.0


def heading_from_marks(marks: dict) -> Optional[float]:
    """由方位标记反推角色朝向（0=北、顺时针）。认不准返回 None。

    N 与 S 应相差 180°；`normalize_deg` 在 ±180 边界会返回 ±180 两种符号，
    所以校验用 ``abs(180 - abs(diff))``，不能直接 ``abs(diff - 180)``
    （后者在 diff=-180 时得到 360，会把正常帧判成认不准）。
    """
    if "N" not in marks:
        return None
    center = dial_center(marks)
    theta_n = polar_angle(center, (marks["N"][0], marks["N"][1]))
    if "S" in marks:
        theta_s = polar_angle(center, (marks["S"][0], marks["S"][1]))
        moved = abs(normalize_deg(theta_n - theta_s))
        if abs(180.0 - moved) > DIAL_NS_TOLERANCE:
            return None
    return (-theta_n) % 360.0


def read_heading(frame: Any) -> Tuple[Optional[float], dict]:
    """从一帧画面读角色朝向；返回 (heading, marks)。"""
    marks = find_dial_marks(frame)
    return heading_from_marks(marks), marks


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class NavConfig:
    tolerance: float = 1.5        # 到达判定（坐标单位；1 单位约 4~5 米）
    step_sec: float = 3.0         # 每次前进时长（实测 3 秒约走 1 单位，刚好跨过取整）
    deadzone: float = 6.0         # 朝向死区（度）
    px_per_deg: float = 9.6       # 鼠标像素/度（实测标定，可被自适应修正）
    max_mouse_px: int = 400       # 单次鼠标位移上限，防止甩飞
    turn_settle: float = 0.35     # 转完等画面稳定再读
    timeout: float = 240.0
    # 连续多少步"离目标没有更近"算卡住。判据是**距目标的进展**，不是单步位移：
    # 坐标是整数，一步常常走不满 1 个单位、取整后照样是 0，用单步位移判会误报。
    stuck_limit: int = 5
    stuck_epsilon: float = 0.3    # 距离目标至少改善这么多才算"有进展"
    blind_limit: int = 8          # 连续读不到坐标
    heading_blind_limit: int = 6  # 连续读不到罗盘
    # 到达后对齐朝向（目标点 = 坐标 + 朝向）
    face_tolerance: float = 5.0   # 朝向容差（度）
    face_max_rounds: int = 10     # 最多纠正几轮（180° + 单次限幅时约需 5~9 轮）
    face_timeout: float = 30.0    # 对齐朝向的总超时


class _Clock:
    def __init__(self, sleep: Optional[Callable[[float], None]],
                 now: Optional[Callable[[], float]]) -> None:
        self.sleep = sleep or time.sleep
        self.now = now or time.monotonic


def _checkpoint(control: Any) -> None:
    if control is None:
        return
    fn = getattr(control, "checkpoint", None)
    if callable(fn):
        fn()


def _noop(_msg: str) -> None:
    return


def _fmt(p: Optional[Coord]) -> str:
    if p is None:
        return "?"
    return "(%.0f, %.0f)" % (p[0], p[1])


def face_heading(io: NavIo, target_heading: float, cfg: Optional[NavConfig] = None,
                 control: Any = None, log: Optional[Callable[[str], None]] = None,
                 *, sleep: Optional[Callable[[float], None]] = None,
                 now: Optional[Callable[[], float]] = None) -> dict:
    """原地转到指定朝向（鼠标闭环），容差 ``face_tolerance``。

    坐标只解决"站在哪"，朝向决定"面朝哪"：站到钓位却面朝岸上，抛竿就废了。
    所以一个目标点是 :class:`autofish_places.Place` 的 ``(x, y, heading)`` 三元组，
    走到位之后必须再对齐朝向。
    """
    cfg = cfg or NavConfig()
    emit = log or _noop
    clock = _Clock(sleep, now)
    started = clock.now()
    deadline = started + cfg.face_timeout
    px_per_deg = cfg.px_per_deg
    blind = 0
    rounds = 0
    heading: Optional[float] = None

    emit("nav: 对齐朝向 %s°（容差 %.0f°）" % (_fmt_deg(target_heading), cfg.face_tolerance))

    while True:
        _checkpoint(control)
        if clock.now() > deadline:
            raise NavError("face_timeout",
                           "停在 %s°，目标 %s°"
                           % (_fmt_deg(heading), _fmt_deg(target_heading)))
        if rounds >= cfg.face_max_rounds:
            raise NavError("face_unreachable",
                           "%d 轮仍未对齐（当前 %s°）"
                           % (rounds, _fmt_deg(heading)))

        heading = io.read_heading()
        if heading is None:
            blind += 1
            if blind >= cfg.heading_blind_limit:
                raise NavError("heading_blind", "对齐朝向时连续 %d 次读不到罗盘" % blind)
            clock.sleep(0.3)
            continue
        blind = 0

        delta = normalize_deg(float(target_heading) - heading)
        if abs(delta) <= cfg.face_tolerance:
            emit("nav: 朝向已对齐 %s°（差 %.1f°，%d 轮）"
                 % (_fmt_deg(heading), delta, rounds))
            return {"ok": True, "heading": heading, "delta": round(delta, 1),
                    "rounds": rounds, "elapsed": round(clock.now() - started, 2)}

        dx = int(round(delta * px_per_deg))
        if dx == 0:
            dx = 1 if delta > 0 else -1
        dx = max(-cfg.max_mouse_px, min(cfg.max_mouse_px, dx))
        _checkpoint(control)
        io.move_mouse(dx)
        rounds += 1
        clock.sleep(cfg.turn_settle)

        after = io.read_heading()
        if after is not None:
            moved = normalize_deg(after - heading)
            if abs(moved) > 1.0 and (moved > 0) == (delta > 0):
                ratio = abs(dx) / max(abs(moved), 1e-6)
                if 3.0 <= ratio <= 30.0:
                    px_per_deg = 0.6 * px_per_deg + 0.4 * ratio


def _fmt_deg(value: Optional[float]) -> str:
    return "?" if value is None else "%.1f" % (value % 360.0)


def navigate_to_place(io: NavIo, places: Any, place: str,
                      map_name: Optional[str] = None,
                      cfg: Optional[NavConfig] = None,
                      control: Any = None,
                      log: Optional[Callable[[str], None]] = None,
                      *, sleep: Optional[Callable[[float], None]] = None,
                      now: Optional[Callable[[], float]] = None,
                      face: bool = True) -> dict:
    """按**名字**导航到目标点：先走到坐标，再对齐朝向。

    ``places`` 只需要提供 ``require(place, map_name) -> Place``（见
    :mod:`autofish_places`）。目标点的 ``heading`` 为 ``None`` 时跳过朝向对齐。

    **查不到点绝不回退**：在错误的地图上用另一张图的坐标，角色会直奔完全
    错误的方向。宁可报错。
    """
    emit = log or _noop
    try:
        entry = places.require(place, map_name)
    except Exception as exc:  # noqa: BLE001 - 缺地图/缺点位都算"没有这个点"
        raise NavError("unknown_place", "%s: %s" % (place, exc)) from exc
    used_map = map_name or getattr(places, "active_map", "?")
    extra = ""
    if entry.heading is not None:
        extra = "，朝 %s°" % _fmt_deg(entry.heading)
    emit("nav: 目标 %s/%s = (%d, %d)%s"
         % (used_map, entry.name, entry.x, entry.y, extra))

    result = navigate(io, entry.coord, cfg=cfg, control=control, log=log,
                      sleep=sleep, now=now)
    result["place"] = entry.name
    result["map"] = used_map
    result["heading_target"] = entry.heading

    if face and entry.heading is not None:
        faced = face_heading(io, entry.heading, cfg=cfg, control=control, log=log,
                             sleep=sleep, now=now)
        result["heading"] = faced["heading"]
        result["faced"] = True
    else:
        result["faced"] = False
    return result


def navigate(io: NavIo, target: Coord, cfg: Optional[NavConfig] = None,
             control: Any = None, log: Optional[Callable[[str], None]] = None,
             *, sleep: Optional[Callable[[float], None]] = None,
             now: Optional[Callable[[], float]] = None) -> dict:
    """走到 ``target``；成功返回摘要，失败抛 :class:`NavError`。

    每一步只做一个短动作（转一次 / 走一步），停止请求最多延迟一个 ``step_sec``。
    """
    cfg = cfg or NavConfig()
    emit = log or _noop
    clock = _Clock(sleep, now)
    target = (float(target[0]), float(target[1]))
    started = clock.now()
    deadline = started + cfg.timeout

    px_per_deg = cfg.px_per_deg
    blind = 0
    heading_blind = 0
    stuck = 0
    best_dist: Optional[float] = None
    walks = 0
    turns = 0
    last: Optional[Coord] = None
    pos: Optional[Coord] = None

    emit("nav: 目标 %s（到达 %.1f 单位，死区 %.0f°）"
         % (_fmt(target), cfg.tolerance, cfg.deadzone))

    while True:
        _checkpoint(control)
        if clock.now() > deadline:
            raise NavError("timeout", "停在 %s，距目标 %.1f"
                           % (_fmt(last), distance(last, target) if last else -1.0))

        pos = io.read_coord()
        if pos is None:
            blind += 1
            if blind >= cfg.blind_limit:
                raise NavError("blind", "连续 %d 次读不到坐标" % blind)
            clock.sleep(0.3)
            continue
        blind = 0
        last = pos

        dist = distance(pos, target)
        if dist <= cfg.tolerance:
            emit("nav: 到达 %s（误差 %.1f，走 %d 步、转 %d 次）"
                 % (_fmt(pos), dist, walks, turns))
            return {"ok": True, "pos": pos, "distance": round(dist, 2),
                    "walks": walks, "turns": turns,
                    "elapsed": round(clock.now() - started, 2),
                    "px_per_deg": round(px_per_deg, 2)}

        heading = io.read_heading()
        if heading is None:
            heading_blind += 1
            if heading_blind >= cfg.heading_blind_limit:
                raise NavError("heading_blind",
                               "连续 %d 次读不到罗盘" % heading_blind)
            clock.sleep(0.3)
            continue
        heading_blind = 0

        wanted = bearing(pos, target)
        delta = normalize_deg(wanted - heading)
        if abs(delta) > cfg.deadzone:
            dx = int(round(delta * px_per_deg))
            if dx == 0:
                dx = 1 if delta > 0 else -1
            dx = max(-cfg.max_mouse_px, min(cfg.max_mouse_px, dx))
            _checkpoint(control)
            io.move_mouse(dx)
            turns += 1
            clock.sleep(cfg.turn_settle)
            # 用实际转动量反修正 px/deg（转完必须能读到罗盘，否则保持原值）
            now_heading = io.read_heading()
            if now_heading is not None:
                moved = normalize_deg(now_heading - heading)
                if abs(moved) > 1.0 and (moved > 0) == (delta > 0):
                    ratio = abs(dx) / max(abs(moved), 1e-6)
                    if 3.0 <= ratio <= 30.0:
                        px_per_deg = 0.6 * px_per_deg + 0.4 * ratio
            continue

        # ---- 前进 ----
        _checkpoint(control)
        io.key_down("w")
        try:
            clock.sleep(cfg.step_sec)
        finally:
            io.key_up("w")
        walks += 1
        clock.sleep(0.2)

        after = io.read_coord()
        if after is None:
            continue
        new_dist = distance(after, target)
        if new_dist <= cfg.tolerance:
            emit("nav: 到达 %s（走 %d 步）" % (_fmt(after), walks))
            return {"ok": True, "pos": after, "distance": 0.0,
                    "walks": walks, "turns": turns,
                    "elapsed": round(clock.now() - started, 2),
                    "px_per_deg": round(px_per_deg, 2)}
        if best_dist is None:
            best_dist = distance(pos, target)
        if new_dist < best_dist - cfg.stuck_epsilon:
            best_dist = new_dist
            stuck = 0
        else:
            stuck += 1
            emit("nav: 离目标没更近（%d/%d，当前 %.1f）"
                 % (stuck, cfg.stuck_limit, new_dist))
            if stuck >= cfg.stuck_limit:
                raise NavError("stuck", "连续 %d 步没有更靠近目标，停在 %s（距 %.1f）"
                               % (stuck, _fmt(after), new_dist))
