"""RF4S 适配启动器（不改 RF4S 源码，运行时打补丁）。

解决本客户端（第三方汉化/私服客户端 rf4_x64.exe）的四个不兼容：
  1. 独占渲染：ImageGrab / mss 截图全黑 -> 改用 PrintWindow 合成虚拟桌面
  2. 鼠标注入无效：mouseDown/Up -> keyDown/Up('f5'/'f6')（F5=基本操作，F6=抬竿）
  3. 抛竿需全力抛：Shift + 左键按住 1 秒
  4. 不用管线杯开关：抛竿本身会自动打开线杯，饵落水后**短按一次左键**即可关上

**全流程只有这一处碰线杯**（就是上面第 4 条的短按左键）。
不再需要抛竿前补按 Enter，也不在任何其他环节切换线杯 —— 上游 `handle_events()`
里那个 `press("enter")` 循环已在本适配器里停用。

窗口方面：**保留标题栏**，把窗口高度补偿成 1080+标题栏，使客户区精确 1920x1080；
不能直接去掉 WS_CAPTION，那会让窗口变成无边框全屏，DWM 进 direct-flip，
拖慢其他窗口重绘（实测会导致 WorkBuddy 滚轮失效）。

用法：
    python rf4s_adapter.py bot -N BOTTOM
"""
import ctypes
import ctypes.wintypes as wt

import friction_state
import math
import os
import sys
import time
from pathlib import Path

import win32con
import win32gui
import win32ui
from PIL import Image
import numpy as np

ctypes.windll.shcore.SetProcessDpiAwareness(2)

# ---- RF4S 源码位置（第三方项目快照在 vendor/，也可用环境变量 RF4S_REF 指定）----
REF_DIR = Path(os.environ.get("RF4S_REF",
                              Path(__file__).resolve().parent / "refs" / "RussianFishing4Script"))
if not REF_DIR.exists():
    alt = Path(__file__).resolve().parent / "vendor"
    if (alt / "rf4s").exists():
        REF_DIR = alt
if not REF_DIR.exists():
    raise SystemExit(f"[patch] 找不到 RF4S 源码目录: {REF_DIR}\n"
                     f"       请 clone RussianFishing4Script 到 refs/，或设置 RF4S_REF，"
                     f"或使用仓库内置的 vendor/ 目录")
sys.path.insert(0, str(REF_DIR))
os.chdir(REF_DIR)

GAME_TITLE = "Russian Fishing 4"
TARGET_W, TARGET_H = 1920, 1080


def pick_monitor(prefer_non_primary=True):
    """返回 (left, top, right, bottom)。优先选非主屏，避免和操作端窗口抢焦点。"""
    mons = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT),
                        wt.LPARAM)
    def cb(hm, hdc, lpr, lp):
        class MI(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", wt.RECT),
                        ("rcWork", wt.RECT), ("dwFlags", ctypes.c_ulong)]
        mi = MI()
        mi.cbSize = ctypes.sizeof(MI)
        ctypes.windll.user32.GetMonitorInfoW(hm, ctypes.byref(mi))
        r = mi.rcMonitor
        mons.append(((r.left, r.top, r.right, r.bottom), bool(mi.dwFlags & 1)))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, cb, 0)
    if prefer_non_primary:
        for r, pri in mons:
            if not pri:
                return r
    for r, pri in mons:
        if pri:
            return r
    return (0, 0, TARGET_W, TARGET_H)
_orig_state = {}


def game_hwnd() -> int:
    h = win32gui.FindWindow(None, GAME_TITLE)
    if not h:
        raise SystemExit(f"[patch] 找不到窗口 '{GAME_TITLE}'，请先启动并登录游戏")
    return h


def prepare_window(h: int) -> tuple:
    """保留标题栏，迭代补偿使客户区精确 1920x1080，并挪到副屏。"""
    _orig_state["rect"] = win32gui.GetWindowRect(h)
    _orig_state["style"] = win32gui.GetWindowLong(h, win32con.GWL_STYLE)
    u = ctypes.windll.user32

    # 确保有标题栏（无边框全屏会让 DWM 进 direct-flip，拖慢其他窗口）
    st = u.GetWindowLongW(h, win32con.GWL_STYLE)
    if not (st & win32con.WS_CAPTION):
        u.SetWindowLongW(h, win32con.GWL_STYLE, st | win32con.WS_CAPTION)

    ml, mt, mr, mb = pick_monitor()
    x = ml + (mr - ml - TARGET_W) // 2
    y = mt + (mb - mt - TARGET_H) // 2
    print(f"[patch] 目标显示器 ({ml},{mt})-({mr},{mb})，窗口放 ({x},{y})")

    u.SetWindowPos(h, 0, x, y, TARGET_W, TARGET_H,
                   win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)
    time.sleep(0.5)

    # 迭代补偿：标题栏/边框吃客户区，且游戏切显示器后会自己改尺寸
    for _ in range(4):
        cr = wt.RECT()
        u.GetClientRect(h, ctypes.byref(cr))
        if cr.right == TARGET_W and cr.bottom == TARGET_H:
            break
        wr = win32gui.GetWindowRect(h)
        nw = (wr[2] - wr[0]) + (TARGET_W - cr.right)
        nh = (wr[3] - wr[1]) + (TARGET_H - cr.bottom)
        u.SetWindowPos(h, 0, x, y, nw, nh,
                       win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
        time.sleep(0.4)

    cr = wt.RECT()
    u.GetClientRect(h, ctypes.byref(cr))
    flag = "OK" if (cr.right, cr.bottom) == (TARGET_W, TARGET_H) else "MISMATCH"
    print(f"[patch] 客户区 {cr.right}x{cr.bottom} (目标 {TARGET_W}x{TARGET_H}) {flag}")
    return cr.right, cr.bottom


def restore_window(h: int) -> None:
    """收尾还原窗口，避免留下奇怪状态。"""
    try:
        r = _orig_state.get("rect")
        if r:
            win32gui.SetWindowPos(h, 0, r[0], r[1], r[2] - r[0], r[3] - r[1],
                                  win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
    except Exception as e:
        print("[patch] 还原窗口失败:", e)


# ========================= 截图层：PrintWindow 合成 =========================
_cache = {"t": 0.0, "img": None}
CACHE_TTL = 0.10


def _vorigin():
    u = ctypes.windll.user32
    return u.GetSystemMetrics(76), u.GetSystemMetrics(77)


def capture_game():
    """用 PrintWindow 抓游戏画面，返回 (客户区图, 客户区屏幕原点)。

    **必须按窗口尺寸建位图**：`PrintWindow` 是按**整个窗口**（含标题栏/边框）
    绘制到目标 DC 的。之前用客户区尺寸 1920x1080 建位图，结果窗口实际是
    1938x1127，**右侧 18px 和底部 47px 被裁掉** —— 而游戏底部那条状态栏
    （负重、体力条、快捷栏、小地图、搏鱼时的钓具承受力度条）正好在那 47px 里。

    症状就是：所有基于 1920x1080 的定点采样（fish_icon / spool / friction_brake）
    全部采到水面背景色，模板匹配也错位，看起来像是"坐标对不上"。
    实际上是截图少了一截。

    正确做法：按窗口尺寸抓，再按「客户区相对窗口的偏移」裁出客户区。
    """
    h = game_hwnd()
    wr = win32gui.GetWindowRect(h)
    W, H = wr[2] - wr[0], wr[3] - wr[1]

    pt = wt.POINT(0, 0)
    ctypes.windll.user32.ClientToScreen(h, ctypes.byref(pt))
    cr = wt.RECT()
    ctypes.windll.user32.GetClientRect(h, ctypes.byref(cr))
    cw, ch = cr.right, cr.bottom
    cx, cy = pt.x - wr[0], pt.y - wr[1]        # 客户区在窗口内的偏移

    wdc = win32gui.GetWindowDC(h)
    mfc = win32ui.CreateDCFromHandle(wdc)
    mem = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, W, H)      # ← 窗口尺寸，不是客户区尺寸
    mem.SelectObject(bmp)
    ctypes.windll.user32.PrintWindow(h, mem.GetSafeHdc(), 2)
    info = bmp.GetInfo()
    img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                           bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1).copy()
    mem.DeleteDC()
    mfc.DeleteDC()
    win32gui.ReleaseDC(h, wdc)
    win32gui.DeleteObject(bmp.GetHandle())

    if 0 <= cx < W and 0 <= cy < H and cx + cw <= W and cy + ch <= H:
        img = img.crop((cx, cy, cx + cw, cy + ch))
    return img, (pt.x, pt.y)


def screen(force=False):
    """虚拟桌面截图：真实截图打底，游戏区域用 PrintWindow 覆盖（带缓存）。"""
    now = time.time()
    if not force and _cache["img"] is not None and now - _cache["t"] < CACHE_TTL:
        return _cache["img"]
    from PIL import ImageGrab
    vx, vy = _vorigin()
    full = ImageGrab.grab(all_screens=True).convert("RGB")
    game, (gx, gy) = capture_game()
    full.paste(game, (gx - vx, gy - vy))
    _cache.update(t=now, img=full)
    return full


def patch_screenshot(pag):
    import cv2
    from pyscreeze import Box

    def _screenshot(imageFilename=None, region=None):
        img = screen()
        if region is not None:
            vx, vy = _vorigin()
            l, t, w, hh = region
            img = img.crop((l - vx, t - vy, l - vx + w, t - vy + hh))
        if imageFilename:
            img.save(imageFilename)
        return img

    def _pixel(x, y):
        vx, vy = _vorigin()
        img = screen()
        ix, iy = int(x) - vx, int(y) - vy
        if not (0 <= ix < img.width and 0 <= iy < img.height):
            print(f"[patch][warn] pixel 越界 x={x} y={y} -> ({ix},{iy}) img={img.size}",
                  flush=True)
            return (0, 0, 0)
        return img.getpixel((ix, iy))

    def _pixelMatchesColor(x, y, expectedRGBColor, tolerance=0):
        p = _pixel(x, y)
        if len(p) == 3 and len(expectedRGBColor) == 3:
            return all(abs(a - b) <= tolerance for a, b in zip(p, expectedRGBColor))
        return p == expectedRGBColor

    def _hits(tpl_path, confidence):
        tpl = cv2.imread(str(tpl_path))
        if tpl is None:
            return []
        hay = cv2.cvtColor(np.asarray(screen()), cv2.COLOR_RGB2BGR)
        th, tw = tpl.shape[:2]
        if th > hay.shape[0] or tw > hay.shape[1]:
            return []
        res = cv2.matchTemplate(hay, tpl, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(res >= confidence)
        return [(int(x), int(y), tw, th) for x, y in zip(xs, ys)]

    def _locate(image, confidence=0.9, **kwargs):
        h = _hits(image, confidence)
        if not h:
            return None
        vx, vy = _vorigin()
        x, y, w, hh = h[0]
        return Box(x + vx, y + vy, w, hh)

    def _locate_all(image, confidence=0.9, **kwargs):
        vx, vy = _vorigin()
        for x, y, w, hh in _hits(image, confidence):
            yield Box(x + vx, y + vy, w, hh)

    pag.screenshot = _screenshot
    pag.pixel = _pixel
    pag.pixelMatchesColor = _pixelMatchesColor
    pag.locateOnScreen = _locate
    pag.locateAllOnScreen = _locate_all


# ========================== 输入层：鼠标 -> 键盘 ===========================
def patch_input(pag, pdi):
    """左键 -> F5（基本操作），右键 -> F6（必选其一的操作）。"""
    pdi.FAILSAFE = False
    pdi.PAUSE = 0

    def mouseDown(x=None, y=None, button="left", **kwargs):
        if button == "right":
            hold_lift()          # 走统一的按住状态，避免与手动按住互相踩
        else:
            pdi.keyDown("f5")

    def mouseUp(x=None, y=None, button="left", **kwargs):
        if button == "right":
            release_lift()
        else:
            pdi.keyUp("f5")

    pag.mouseDown = mouseDown
    pag.mouseUp = mouseUp
    print("[patch] 鼠标按键已映射: 左键->F5, 右键(抬竿)->F6")


FRICTION_WINDOW = 10.0   # 连续观察窗口（秒）
FRICTION_SAMPLE = 0.2    # 采样间隔


def patch_bottom_mode_held():
    """单竿专用：一直手持竿等待咬钩，不放竿、不轮询。

    上游 `start_bottom_mode` 每轮都 `press 1` 拿起 -> 检查 -> `press 0` 放下，
    这是为多竿轮询设计的。单竿时这套来回纯属浪费，而且 `press 0` 在某些地形
    还会失败（"没办法把鱼竿支架安置在此位置"）。

    替换后的循环：
      - 开头 `press 1` 拿竿，只这一次
      - 之后一直手持：咬钩就收线+提鱼+重抛（`retrieve_and_recast` 内部
        `cast_tackle(lock=True)` 抛完仍留在手上），没咬钩就等待
      - 连续 CHECK_MISS_LIMIT 次没咬钩才重抛一次
    """
    from time import sleep

    from pydirectinput import press

    from rf4s.component.logger import logger
    from rf4s.controller.player import Player
    from rf4s.utils import add_jitter

    def start_bottom_mode_held(self):
        rod_key = str(self.cfg.KEY.BOTTOM_RODS[0])
        press(rod_key)
        sleep(1.5)

        miss = 0
        # 上游默认 16 轮（约 4 分钟）才重抛一次，单竿手持下太久：
        # 一旦竿没抛出去，会长时间空等，看起来就像"卡住不动"。
        limit = min(self.cfg.PROFILE.CHECK_MISS_LIMIT, 3)
        logger.info("Bottom mode started (single rod, held, no put-down)")
        hud_lost_since = None      # HUD 连续读不到 = 卡在菜单/弹窗/解锁界面

        while True:
            self.enable_trolling()
            self.refill_stats()
            with self.loop_restart_handler():
                if self.detection.is_fish_hooked():
                    miss = 0
                    logger.info("Fish hooked, retrieving")
                    self.retrieve_and_recast()
                else:
                    miss += 1
                    if miss >= limit:
                        miss = 0
                        logger.info("No bite for %d rounds, recasting", limit)
                        self.recast_tackle()
                    else:
                        # 咬钩判据现在是"圆形鱼图标出现"，一次判断只要几毫秒，
                        # 所以不必死等 CHECK_DELAY=16s —— 那会让鱼在钩上白挂十几秒。
                        # 改成每 0.4 秒看一眼，咬钩后几乎立刻进搏鱼。
                        waited = 0.0
                        next_modal_check = 0.0
                        while waited < self.cfg.PROFILE.CHECK_DELAY:
                            if self.detection.is_fish_hooked():
                                break
                            # 等口期间也查弹窗：弹窗盖住 HUD 时鱼图标恒 False，
                            # 不处理就会一直等到连续 3 轮没口然后重抛，白浪费。
                            if waited >= next_modal_check:
                                try:
                                    if maybe_dismiss("等咬钩"):
                                        waited = 0.0
                                        next_modal_check = 2.0
                                        continue
                                except Exception:
                                    pass
                                next_modal_check = waited + 2.0
                            # 看门狗：HUD 连续 15 秒读不出来（线长圆环返回 -1），
                            # 说明画面被菜单/能力解锁弹窗之类盖住了 —— 常规循环
                            # 不可能自己脱困，交给 Agent 看截图决定点哪里。
                            try:
                                if line_ring_ratio() < 0:
                                    if hud_lost_since is None:
                                        hud_lost_since = time.time()
                                    elif time.time() - hud_lost_since > 15:
                                        # 先自己试着清"排队弹窗"（能力解锁/升级/技能点，
                                        # 长得一模一样、点掉一个立刻弹下一个）
                                        cleared = False
                                        try:
                                            cleared = dismiss_modals()
                                        except Exception:
                                            pass
                                        if cleared:
                                            print("[patch] 排队弹窗已自动点完", flush=True)
                                            hud_lost_since = None
                                            try:
                                                if self.detection.is_fish_captured():
                                                    self.handle_fish()
                                            except Exception:
                                                pass
                                            continue
                                        import agent_hook
                                        print("[patch] HUD 连续 15s 不可读，上报 Agent",
                                              flush=True)
                                        agent_hook.escalate(
                                            "hud_hidden",
                                            "HUD 读不到，可能卡在菜单/能力解锁弹窗",
                                            {}, timeout=300.0)
                                        hud_lost_since = None
                                else:
                                    hud_lost_since = None
                            except Exception:
                                pass
                            sleep(0.4)
                            waited += 0.4

    Player.start_bottom_mode = start_bottom_mode_held
    print("[patch] 底钓改为单竿手持模式（不放竿、不轮询）")
def patch_single_rod(pag, pdi):
    """修复 RF4S 0.11.0 单竿模式的 bug。

    `Player._get_available_rods()` 在 num_tackle==1 时返回 [self.tackle]（对象），
    而其他分支返回的是 int 索引；上层 `self.tackles[self.tackle_idx]` 拿对象当索引，
    直接 TypeError。单竿必崩，这里改成返回索引。
    """
    from rf4s.controller.player import Player
    orig = Player._get_available_rods

    def _get_available_rods(self):
        if self.num_tackle == 1 and self.tackle.available:
            return [self.tackle_idx]
        return orig(self)

    Player._get_available_rods = _get_available_rods
    print("[patch] 已修复单竿模式 _get_available_rods 返回对象当索引的 bug")


CAST_HOLD = 1.0            # 抛竿：Shift + 左键按住 1 秒（全力抛）
CAST_LAND_DELAY = 2.5      # 抛出后等饵落水（玩家指定 2~3 秒）
CAST_TAP_HOLD = 0.15       # 短按一次左键 -> 关线杯


def focus_game_quiet() -> bool:
    """把游戏窗口置前，**绝不点击**。

    滚轮（WM_MOUSEWHEEL）和键盘都只发给前台窗口，调摩擦之前必须先确保是游戏。
    搏鱼中途不能"先点一下再置前"——那一下会被当成一次左键（刺鱼/暴收）。
    """
    h = game_hwnd()
    u = ctypes.windll.user32
    if u.GetForegroundWindow() == h:
        return True
    for _ in range(3):
        u.ShowWindow(h, 9)                                     # SW_RESTORE
        u.SetWindowPos(h, -1, 0, 0, 0, 0, 0x0001 | 0x0002)     # TOPMOST
        u.SetWindowPos(h, -2, 0, 0, 0, 0, 0x0001 | 0x0002)     # NOTOPMOST
        u.BringWindowToTop(h)
        u.SetForegroundWindow(h)
        time.sleep(0.15)
        if u.GetForegroundWindow() == h:
            return True
    print("[patch][warn] 置前游戏窗口失败，滚轮可能打不到游戏", flush=True)
    return False


# ==================== 摩擦片：维护状态变量 + 增量滚轮 ====================
# 关键认知（`bot/AUTOFISH-IMPL.md` 已记过）：**摩擦档位在屏幕上没有直接读数**，
# 只能靠"滚了几格"自己维护。所以：
#   * 抛竿前：绝对校准（向上滚满 30 档，再向下滚回 17）—— 不依赖当前实际是多少
#   * 搏鱼中：只做增量（维护 _fric['cur']，算出 delta，滚 |delta| 格）
# 绝对校准那种"先顶满再退回"只有抛竿前能用：搏鱼时滚 30 格相当于把卸力拉到极限，
# 那一下直接断线。
FRICTION_INITIAL = 17
FRICTION_MAX = 29
FRICTION_MIN = 1
FRICTION_ABS_TOP = 30          # 绝对校准时的"顶"（上游 MAX_FRICTION_BRAKE=30）
FRICTION_DROP_DIV = 3          # 红 -> 降 1/3：17 - 17//3 = 12，16 - 16//3 = 11
FRICTION_RISE_DIV = 5          # 10 秒全绿 -> 升 1/5：17 + 17//5 = 20（向上滚 3 格）
FRICTION_WINDOW = 10.0     # 观察窗口：过去 10 秒
FRICTION_SAMPLE = 0.2       # 采样间隔
FRICTION_RISE_GAP = 5.0     # 升档后再升的最短间隔（玩家 02:41：升完过5s再看）
FRICTION_RECOVER_GAP = 10.0  # 降档后再升的最短间隔（红事件后谨慎恢复，防震荡）
FRICTION_DROP_GAP = 6.0     # 两次降档至少隔 6 秒：一次红事件只降一次
FRICTION_RISE_MIN_READ = 3  # 升档前窗口内至少 3 次真实读到"最低档"
FRICTION_SCROLL_GAP = 0.055    # 每格之间的间隔
FRICTION_LOCK = 0.8            # 调整后锁这么久，避免连续触发

_fric = {"cur": FRICTION_INITIAL, "t": 0.0, "calibrated": False}


def _drop_target(cur: int) -> int:
    """降 1/3：**整数除法**。17 -> 12，16 -> 11（玩家给的例子，用 round 会算成 11）。"""
    return max(FRICTION_MIN, cur - max(1, cur // FRICTION_DROP_DIV))


def _rise_target(cur: int) -> int:
    """升 1/5：**整数除法**。17 -> 20，正好是"滚轮向上滚 3 次"。"""
    return min(FRICTION_MAX, cur + max(1, cur // FRICTION_RISE_DIV))


def _wheel(n: int) -> None:
    """滚轮 n 格（正=加档，负=减档），**并立刻把这个变化记进 _fric["cur"]**。

    这里必须自己是唯一入口：只要有一次"滚了但没记账"，后面所有的判断都建立在错的
    档位上，最后就是断线。所以谁调档都只能通过这个函数（或它上面包一层的
    friction_nudge / friction_absolute），不要直接发滚轮事件。

    玩家确认（2026-11-04）：这套滚轮方式一直是有效的（抛竿前"滚满 30 再退回 17"
    那套绝对校准就是用它做的），所以保持原样。
    """
    _wheel_raw(n)
    _fric["cur"] = max(FRICTION_MIN, min(FRICTION_MAX, _fric["cur"] + n))
    _fric["t"] = time.time()
    friction_state.set(_fric["cur"], calibrated=_fric.get("calibrated", False))
    print(f"[patch] 摩擦片 -> {_fric['cur']} 档（{n:+d}）", flush=True)


def _wheel_raw(n: int) -> None:
    """只发滚轮事件，不记账（绝对校准内部用：它的档位由调用方设定）。"""
    if n == 0:
        return
    if not focus_game_quiet():
        print("[patch][warn] 滚轮跳过：游戏窗口没能置前", flush=True)
        return
    delta = 120 if n > 0 else -120
    for _ in range(abs(n)):
        ctypes.windll.user32.mouse_event(0x0800, 0, 0, delta, 0)   # MOUSEEVENTF_WHEEL
        time.sleep(FRICTION_SCROLL_GAP)


def friction_absolute(target: int = FRICTION_INITIAL) -> None:
    """绝对校准：先滚到顶（30），再退回 target。

    **每次抛竿前都做**（玩家 2026-11-04 04:17 明确要求，理由也是对的）：

      * 抛竿时线上没有鱼，**顶着满档也是安全的**；等鱼上钩之后再校准才危险
        （鱼已经在拉，一滚到 30 就可能直接断线）
      * 绝对校准不依赖"档位记忆"，错一次不会一直错下去 —— 相对调整则会累积误差
      * 每条鱼的起点都确定是 17，搏鱼中的调整可以放心做，下一条鱼自动归位

    代价是每竿多约 2.4 秒（43 格滚轮 × 55ms），可以接受。
    """
    if not focus_game_quiet():
        print("[patch][warn] 摩擦片绝对校准跳过：游戏窗口没能置前", flush=True)
        return
    _wheel_raw(FRICTION_ABS_TOP)
    time.sleep(0.2)
    _wheel_raw(-(FRICTION_ABS_TOP - target))
    _fric["cur"] = target
    _fric["t"] = time.time()
    _fric["calibrated"] = True
    friction_state.set(target, calibrated=True)
    print(f"[patch] 摩擦片绝对校准 -> {target} 档（抛竿前，线上无鱼，安全）", flush=True)


def _reset_friction_for_fish() -> None:
    """新的一条鱼开始：把摩擦片**相对**调回 FRICTION_INITIAL。

    不做"滚满 30 再退回"的绝对校准（那个只在脚本启动时做一次）：
    每条鱼都滚满再退会多花 ~4 秒，而且顶着满档抛竿有断线风险。
    相对调整只需要滚 |cur - 17| 格，档位变量是准的（_wheel 统一记账）。
    """
    cur = _fric["cur"]
    _fric["_last_drop"] = 0.0
    if cur == FRICTION_INITIAL:
        return
    friction_nudge(FRICTION_INITIAL - cur)
    print(f"[patch] 新的一条鱼：摩擦片恢复 -> {FRICTION_INITIAL} 档", flush=True)


def friction_nudge(delta: int) -> None:
    """增量调整：只滚需要的那几格，档位由 _wheel 统一记账。"""
    if delta == 0:
        return
    target = max(FRICTION_MIN, min(FRICTION_MAX, _fric["cur"] + delta))
    _wheel(target - _fric["cur"])


def _tier(lvl: int) -> int:
    """四色分类 -> 玩家的三档口径（2026-11-04 02:41）：

      1 档 = 力度最低的颜色（黄绿，实测 hue=33, RGB(200,214,63)）→ 一直 1 档就升
      2 档 = 金黄偏橙/琥珀（实测 hue=25, RGB(229,188,0)）→ 目标，稳住不动
      3 档 = 红（实测 hue=6）→ 立刻降
    阈值 2026-11-04 12:49 由玩家指认截图标定：橙/黄绿分界=29（25 与 33 的中点）。
    分类器 0（空载/松线瞬间）按 1 档对待——松线不该阻止升档判断。
    """
    if lvl >= 4:
        return 3
    if lvl == 3:
        return 2
    return 1


def friction_controller_step(win: list) -> None:
    """搏鱼中的摩擦片控制（2026-11-04 03:02 定稿：降=事件驱动，升=状态驱动）。

    ## 为什么两个方向用不同的算法

    **降档（红）是"事件"**：红的语义是"现在正在超载"，是紧急信号。
    看历史窗口反而有害 —— 第 9 秒红过一次降了档，第 14 秒回看"过去 10s"
    仍含那次红，会二次降档把泄力打到地板（玩家指出的陷阱）。
    所以降档只看**当下尾部**，配冷却期。

    **升档（绿）是"状态"**："一直绿"是持续轻松的判断，必须看一段历史。
    但窗口起点 = **上次调整时刻 T0**（玩家指定）：T0 之前的样本反映的是
    旧档位下的力度，混进来会把判断带偏。

    ## 规则

    降档（尾部确认 + 冷却）：
      * 最近 3 帧（~0.6s）里 ≥2 帧是 3 档（红）→ 立刻降 1/3（17→12→8…）
      * 距上次降档 <6s 不降（同一红事件去重；6s 后仍红则说明鱼真的强，再降）

    升档（只看 T0 之后的数据）：
      * 窗口 = [T0, now]，全部为 1 档（出现橙=命中目标不升；出现红走降档）
      * 真实读到 1 档（有色带）≥3 次；松线空读不算数但也不阻断
      * 距 T0：上次动作是升档 → ≥5s；上次动作是降档 → ≥10s（红后谨慎恢复）
      * 动作：升 1/5（17→20），上限 29

    每次调整后清空样本窗口，从 T0 重新累计。
    """
    now = time.time()
    while win and now - win[0][0] > FRICTION_WINDOW:
        win.pop(0)
    if not win:
        return
    if now - _fric["t"] < FRICTION_LOCK:      # 滚轮刚动过，先等生效
        return

    t0 = _fric["t"]                            # 上次调整时刻
    tiers = [(t, _tier(l)) for t, l in win]

    # ---- 降档：红事件（只看当下尾部 + 冷却去重）----
    tail = [tier for _, tier in tiers[-3:]]
    if (tail.count(3) >= 2
            and now - _fric.get("_last_drop", 0.0) >= FRICTION_DROP_GAP):
        target = _drop_target(_fric["cur"])
        if target < _fric["cur"]:
            print(f"[patch] ★红事件 降档 {_fric['cur']} -> {target}"
                  f"（尾部3帧 {tail}）", flush=True)
            _fric["_last_drop"] = now
            _fric["_last_action"] = "drop"
            friction_nudge(target - _fric["cur"])   # nudge 更新 _fric["t"]
            win.clear()
        return

    # ---- 升档：只统计 T0 之后的**真实读数** ----
    # ⚠️ 必须剔除 lvl==0 的样本：那是"力度条无色"＝这一瞬间鱼没在拉（鱼在歇、
    # 或者线已经收到岸边），它既不能证明"负载低"，也不该参与"一直绿"的判断。
    # 之前 idle 只有 2 秒时攒不下几个 0，问题被掩盖了；idle 提到 15 秒后一次
    # 能攒 70+ 个 0，而 _tier(0)==1，会凑出"10 秒全 1 档"导致鱼刚歇完就误升档。
    since = [(t, _tier(lvl), lvl) for t, lvl in win
             if t >= t0 - 0.05 and lvl > 0]
    if not since:
        return                                 # 这 10 秒全是空档，没有信息量
    s_tiers = [tier for _, tier, _ in since]
    if max(s_tiers) != 1:                      # 有橙/红 → 不是"一直低"
        return
    n_real_low = len(since)                    # since 里已经全是真实低档读数
    gap = (FRICTION_RECOVER_GAP if _fric.get("_last_action") == "drop"
           else FRICTION_RISE_GAP)
    if (n_real_low >= FRICTION_RISE_MIN_READ
            and now - t0 >= gap):
        target = _rise_target(_fric["cur"])
        if target > _fric["cur"]:
            print(f"[patch] ▲调整后全1档 升档 {_fric['cur']} -> {target}"
                  f"（T0后真实低档读数 {n_real_low} 次，距T0 {now - t0:.1f}s）",
                  flush=True)
            _fric["_last_action"] = "rise"
            friction_nudge(target - _fric["cur"])
            win.clear()
    # max == 2（橙 = 目标）：不动


def _game_gray():
    """抓一帧游戏画面并缩成小灰度图（仅用于日志）。"""
    img, _ = capture_game()
    return np.asarray(img.convert("L").resize((240, 135))).astype(np.int16)


def patch_cast(pdi):
    """抛竿：Shift + 左键按住 1 秒 -> 等 2.5 秒饵落水 -> 短按左键关线杯。

    流程（玩家指定，2026-11-03）：
      1. 抛竿前做一次摩擦片**绝对校准**到 17 档
      2. 按住 Shift，再按住左键 1.0 秒（全力抛）
      3. 松手后等 2~3 秒（饵落水）
      4. 短按一次左键 -> 关线杯（不关的话搏鱼时鱼一拉就持续出线）
      5. 之后就是等圆形鱼图标出现

    不做"抛竿是否成功"的画面 diff 校验：实测成功/失败的 diff 区间重叠
    （成功 5.4~6.1，失败 2.5~5.0），校验只会把正常抛竿判失败再补按 Enter，
    把线杯状态越搞越乱。抛失败交给外层 CHECK_MISS_LIMIT 重抛。
    """
    from rf4s.component.tackle import StageId, Tackle

    def cast(self, lock):
        release_lift()                     # 抛竿前把抬竿松开
        # 重置"收线 45 秒"的计时：每一竿都从零开始算，否则会继承上一轮的旧截止时间
        try:
            from rf4s.controller.detection import Detection
            Detection.is_retrieval_finished.reset_deadline()
        except Exception:
            pass
        time.sleep(0.15)
        maybe_dismiss("抛竿前")            # 卡牌/技能/升级弹窗会吃掉 Shift+F5
        friction_absolute(FRICTION_INITIAL)   # 每竿前都校准（线上无鱼，安全）
        self.stage = StageId.CAST
        focus_game_quiet()

        # Shift 必须真的按下去（2026-11-04 事故：焦点被抢时 Shift 落到别的
        # 窗口，游戏只收到 F5，变成无蓄力短抛）。按下后用 GetAsyncKeyState
        # 验证，没按上就重新置前再试一次，还不行就放弃这次抛竿等重抛。
        u = ctypes.windll.user32
        pdi.keyDown("shift")
        time.sleep(0.08)
        if not (u.GetAsyncKeyState(0x10) & 0x8000):    # VK_SHIFT
            print("[patch][warn] Shift 没按上（焦点被抢），重试", flush=True)
            pdi.keyUp("shift")
            focus_game_quiet()
            time.sleep(0.2)
            pdi.keyDown("shift")
            time.sleep(0.08)
            if not (u.GetAsyncKeyState(0x10) & 0x8000):
                pdi.keyUp("shift")
                print("[patch][warn] Shift 仍未按上，放弃本次抛竿（交给重抛）",
                      flush=True)
                self.stage = StageId.RESET
                return
        pdi.keyDown("f5")
        time.sleep(CAST_HOLD)              # 按住左键 1.0s 蓄力
        pdi.keyUp("f5")
        time.sleep(0.05)
        pdi.keyUp("shift")

        time.sleep(CAST_LAND_DELAY)        # 等饵落水
        pdi.keyDown("f5")                  # 短按一次，关线杯
        time.sleep(CAST_TAP_HOLD)
        pdi.keyUp("f5")
        print(f"[patch] 抛竿完成（Shift+左键 {CAST_HOLD}s，等 {CAST_LAND_DELAY}s，"
              f"短按关线杯）", flush=True)

    Tackle.cast = cast
    print("[patch] 抛竿：Shift+左键 1s -> 等 2.5s -> 短按关线杯（不做 diff 校验）")


# =============================== 入口 ======================================
def selfcheck():
    """确认 PrintWindow 真的能抓到画面（不是全黑）。"""
    img, (gx, gy) = capture_game()
    gray = np.asarray(img.convert("L")).astype(float)
    mean = float(gray.mean())
    print(f"[patch] PrintWindow 抓到 {img.size}，灰度均值 {mean:.1f}，位置 ({gx},{gy})")
    if mean < 1.0:
        raise SystemExit("[patch] 抓到全黑画面，PrintWindow 失败，终止")
    return mean


RETRIEVAL_TIMEOUT = 45.0   # 秒；超过仍判定为"没收完"就强制结束，防止无限卡死


def patch_retrieval_timeout(seconds=RETRIEVAL_TIMEOUT):
    """给 is_retrieval_finished 加超时兜底。

    实测 wheel.png 在 1920x1080 下最高只有 0.9675，低于配置的 0.98，
    于是 `is_retrieval_finished()` 恒为 False，收线永不停 —— 表现为卡在
    `Resetting tackle` 一动不动，实际上 F5 一直被按着在收线。

    阈值已在 config.yaml 调低到 0.93；这里再加一层保险：真的判断不出来时，
    收线超过 N 秒就强制视为完成，避免整条流程死在这里。
    """
    from rf4s.controller.detection import Detection

    orig = Detection.is_retrieval_finished
    _start: dict[int, float] = {}

    def reset_deadline() -> None:
        """每次抛竿开始时清掉计时（否则会继承上一轮的旧截止时间）。

        实测症状：刚咬钩 3 秒就报"收线超过 45s 仍未判定完成" —— 因为计时器
        从上一轮第一次调用起算，那条鱼收完后没人清，一直挂在那里。
        """
        _start.clear()

    is_retrieval_finished.reset_deadline = reset_deadline

    def is_retrieval_finished(self):
        key = id(self)
        t0 = _start.get(key)
        if t0 is None:
            t0 = time.time()
            _start[key] = t0
        try:
            if orig(self):
                _start.pop(key, None)
                return True
        except Exception:
            pass
        if time.time() - t0 > seconds:
            _start.pop(key, None)
            print(f"[patch] 收线超过 {seconds:.0f}s 仍未判定完成，强制结束", flush=True)
            return True
        return False

    Detection.is_retrieval_finished = is_retrieval_finished
    print(f"[patch] 收线判定加 {seconds:.0f}s 超时兜底")


TACKLE_READY_TIMEOUT = 6.0   # 秒；等价判据失效时的兜底，别设太大（每轮都要等它）


def patch_tackle_ready_timeout(seconds=TACKLE_READY_TIMEOUT):
    """给 is_tackle_ready 加**等价判据 + 超时兜底**，解开 Tackle.reset 的等待。

    `Tackle.reset()` 是这样的：

        while True:
            if self.detection.is_tackle_ready():   # 唯一正常出口
                return
            ... 其余全是 raise 异常 ...

    而 `is_tackle_ready()` 匹配的是 ready.png（"Tackle ready for casting"），
    中文界面下最高只有 0.3885，**永远返回 False**。于是这个 while 永不退出，
    只能靠超时兜底 —— 但那样**每一轮都要白等 18 秒**（实测就是这样）。

    中文模板补齐之前，用 HUD 状态做等价判断：
        * 圆形鱼图标不在（没有鱼挂在钩上）
        * 力度条无色（没有张力）
        * 没有渔获结算框
    三个都成立且**连续两次确认**（约 0.4s，避开收线/结算的瞬间），就认为已复位。
    18 秒超时保留做最后保险。
    """
    from rf4s.controller.detection import Detection

    orig = Detection.is_tackle_ready
    state: dict[int, float] = {}
    streak: dict[int, int] = {}

    def is_tackle_ready(self):
        key = id(self)
        try:
            if orig(self):
                state.pop(key, None)
                streak.pop(key, None)
                return True
        except Exception:
            pass

        # 等价判据：只看「没鱼 + 没结算框」。
        # 不要再加"力度条无色"——那条是半透明的，收竿后背景（红格纹袖子/沙地）
        # 透上来会让 friction_level() 恒 >0，判据就永远不成立（实测每轮白等 18 秒）。
        try:
            idle = not fish_icon_present()
            if idle:
                try:
                    if self.is_fish_captured():
                        idle = False
                except Exception:
                    pass
            if idle:
                streak[key] = streak.get(key, 0) + 1
                if streak[key] >= 2:
                    state.pop(key, None)
                    streak.pop(key, None)
                    return True
            else:
                streak[key] = 0
        except Exception:
            pass

        t0 = state.get(key)
        if t0 is None:
            state[key] = time.time()
            return False
        if time.time() - t0 > seconds:
            state.pop(key, None)
            streak.pop(key, None)
            print(f"[patch] is_tackle_ready 连续 {seconds:.0f}s 未命中，超时兜底 -> True",
                  flush=True)
            return True
        return False

    Detection.is_tackle_ready = is_tackle_ready
    print(f"[patch] is_tackle_ready：HUD 等价判据（无鱼图标+无张力+无结算框，连续2次）"
          f"，{seconds:.0f}s 超时兜底")


PULL_TIMEOUT = 120.0
PULL_IDLE_SECS = 15.0   # 力度条连续无色多久才算搏鱼结束（原来实际只有 2 秒）
RETRIEVAL_FINISH_STREAK = 6   # 连续匹配次数才认定"收线完成"

# ====================== 抬竿（右键 -> F6，全程按住）=======================
# 实测（2026-11-03）：
#   * 线收到头后只按 F5 再也不会动 —— 鱼已贴岸，力度条全灰（friction_level()==0），
#     上游 `lift_fish()` 用 `is_fish_hooked()` 当门槛，判定"没鱼"直接 return，
#     于是**最后一步上鱼永远不执行**，接着复位重抛。
#   * 实际按住**抬竿**（右键，已绑 F6）只要 3 秒，鱼就出水并弹出结算画面：
#     抬竿 1.5s 后力度条 灰 -> 黄 -> 橙，3s 后结算；keep.png 匹配 0.997，
#     Space = 入护（实测鱼护 3/100，底部回到"钓具准备好,可以抛掷一下"）。
#
# 所以：搏鱼全程按住抬竿（用户要求），收线结束后再单独走一次"抬竿上鱼"。
LIFT_KEY = "f6"
LIFT_TIMEOUT = 30.0     # 抬竿最长按住时间（超时放弃，交给主循环复位）
LIFT_PROBE = 4.0        # 抬这么久仍无拉力/结算 -> 认为本来就没鱼
LIFT_POLL = 0.25
LIFT_LOST_LIMIT = 8     # 拉力消失连续次数（约 2s）-> 认为鱼跑了

_lift = {"held": False}


def _who() -> str:
    """调用方是谁（函数@行号），用来定位"谁把键松了"。"""
    import inspect

    try:
        f = inspect.stack()[2]
        return f"{f.function}@{f.lineno}"
    except Exception:
        return "?"


def hold_lift() -> None:
    """按住抬竿（右键 -> F6），幂等。"""
    if _lift["held"]:
        return
    import pydirectinput as pdi

    pdi.FAILSAFE = False
    pdi.keyDown(LIFT_KEY)
    _lift["held"] = True
    print(f"[KEY] F6 按下 (by {_who()})", flush=True)


def release_lift() -> None:
    """松开抬竿，幂等。必须用 pdi.keyUp：SendInput+VK 解不开卡住的键。"""
    if not _lift["held"]:
        return
    import pydirectinput as pdi

    pdi.keyUp(LIFT_KEY)
    _lift["held"] = False
    print(f"[KEY] F6 松开 (by {_who()})", flush=True)


# ================= 线长圆环（spool 图标外圈 = 线长进度）=================
# 玩家描述：鱼指标那一排有个"像线轮一样"的圆形图标，**外圈是一圈进度环**。
#   * 圆环 ≥90%  -> 收线已经到位
#   * 一旦掉到 ≤10% -> 线快被鱼拉空，必须立刻小退
#   * 90% 以上卡住 30 秒仍不结算 -> 交给 Agent 判断（卡底/太重/够不到…）
#
# 坐标来自 1920x1080 的 HUD 表：spool_icon (1237, 1024)。
# 环很细（半径 8~10px，见 `TENSION-SCAN`/`AUTOFISH-IMPL` 的极坐标扫描结论），
# 所以沿每个角度取半径区间里的最大亮度，再统计"亮"的角度占比。
LINE_RING_CENTER = (1237, 1023)
# 半径实测（逐半径扫描，图标 30x30）：r13~15 才是那圈进度环，
# 收满时 r13/r14 的点亮比例 ≈97~99%；r10~12 是图标内圈（线层图案），会把比例带偏。
LINE_RING_RADII = (13, 15)
LINE_RING_LIT = 180            # 判定"亮"的灰度阈值
LINE_RING_NEAR = 0.90
LINE_RING_LOW = 0.10
LINE_RING_STUCK_SECS = 30.0
# 阈值还没标定完（实机看到 0.00 / 0.28 / 0.47 这些值），所以先只记录、不上报。
# 攒够几场搏鱼的数据、确认分布之后把这里改成 True 就会真的触发 Agent 接管。
LINE_RING_ACTIONS = True


def line_ring_ratio() -> float:
    """线长圆环的点亮比例 0..1；画面不可读时返回 -1（未知，不参与判断）。"""
    img, _ = capture_game()
    a = np.asarray(img.convert("L")).astype(int)
    cx, cy = LINE_RING_CENTER
    r0, r1 = LINE_RING_RADII
    if a.shape[0] <= cy + r1 or a.shape[1] <= cx + r1:
        return -1.0

    region = a[cy - r1:cy + r1 + 1, cx - r1:cx + r1 + 1]
    if float(region.mean()) < 20:          # 全黑 = HUD 没画出来
        return -1.0

    lit = 0
    total = 0
    peak = 0
    for deg in range(0, 360, 3):
        rad = math.radians(deg)
        best = 0
        for r in range(r0, r1 + 1):        # 环有 2~3px 厚，取该角度最亮的一圈
            x = int(round(cx + r * math.cos(rad)))
            y = int(round(cy + r * math.sin(rad)))
            best = max(best, int(a[y, x]))
        peak = max(peak, best)
        total += 1
        if best >= LINE_RING_LIT:
            lit += 1

    # 整圈最亮的角度都不到阈值 = 这个图标根本没画出来（HUD 隐藏 / 结算界面盖住），
    # 和"圆环真的空到 0%"是两回事，必须区分，否则会疯狂误触发"立刻小退"。
    if peak < LINE_RING_LIT:
        return -1.0
    return lit / total if total else -1.0


def patch_pull_continuous(timeout=PULL_TIMEOUT):
    """搏鱼阶段持续收线 + 全程按住抬竿，不再因线杯短暂停转而中断。

    `Tackle.pull()` 原本是：

        while True:
            if self.detection.is_retrieval_finished():   # wheel.png 匹配
                return

    `wheel.png` 是底部那个线杯图标。**鱼一停顿，线杯跟着停转，图标回到静止形态
    正好与模板吻合**，于是被判成"收线完成"，松开 F5 —— 这就是用户看到的
    「转一会轮，又停一会」。实际上这时鱼还在，根本不该停。

    改为只认 `is_fish_captured()`（`keep.png` 是鱼护图标，不含文字，语言无关，
    实测可靠）作为结束条件，另加超时兜底。

    另外按用户要求：**搏鱼全程除按住左键（收线）外，一直按住右键（抬竿）**。
    """
    from time import sleep

    from rf4s import exceptions
    from rf4s.component.logger import logger
    from rf4s.component.tackle import LOOP_DELAY, StageId, Tackle
    from rf4s.utils import add_jitter

    def pull(self) -> None:
        logger.info("Pulling fish (continuous, reel + lift, friction control)")
        _t_pull = time.time()
        print(f"[FLOW] pull 进入（档位 {_fric['cur']}）", flush=True)
        if self.stage != StageId.PULL:
            self.stage = StageId.PULL
            self.timer.set_timeout_start_time()
        # 兜底：正常情况下"抛竿前绝对校准"已经把档位设回 17 了，
        # 这里只处理"没抛竿就有鱼"的边角情况。cur 已是 17 时这是个空操作，
        # 不会再出现"鱼上钩后才去大幅调档"的危险动作。
        _reset_friction_for_fish()
        hold_lift()      # 抬竿全程按住（左键由外层 hold_keys 一直按着）
        start = time.time()
        idle_since = None      # 力度条开始持续无色的时刻（None=当前有张力）
        win = []         # [(t, 力度等级)]，只留最近 10 秒，给摩擦片控制器用
        ring_high_since = None
        low_streak = 0
        last_ring_log = 0.0
        try:
            while True:
                if self.detection.is_fish_captured():
                    raise exceptions.FishCapturedError

                if self.cfg.BOT.SPOOLING_DETECTION and self.detection.is_line_at_end():
                    raise exceptions.LineAtEndError
                if self.cfg.BOT.SNAG_DETECTION and self.detection.is_line_snagged():
                    raise exceptions.LineSnaggedError
                if self.timer.is_rare_event_checkable():
                    self.check_rare_events()

                # 力度条等级：既用来判断"鱼还在不在"，也喂给摩擦片控制器
                try:
                    lvl, bar_info = friction_bar_state()
                    _log_bar_sample(lvl, bar_info)
                except Exception:
                    lvl = 0
                win.append((time.time(), lvl))
                friction_controller_step(win)

                # ---- 线长圆环：≥90% 卡住 30s / ≤10% 都快被拉空，都交给 Agent ----
                try:
                    ring = line_ring_ratio()
                except Exception:
                    ring = -1.0
                if LINE_RING_ACTIONS and ring >= LINE_RING_NEAR:
                    if ring_high_since is None:
                        ring_high_since = time.time()
                    elif time.time() - ring_high_since >= LINE_RING_STUCK_SECS:
                        import agent_hook
                        print(f"[patch] 线长圆环 {ring:.0%} 已卡 {LINE_RING_STUCK_SECS:.0f}s，"
                              f"上报 Agent", flush=True)
                        act = agent_hook.escalate(
                            "line_ring_high_stuck",
                            f"线已收到 {ring:.0%}，{LINE_RING_STUCK_SECS:.0f}s 仍未结算",
                            {"ring": round(ring, 3), "levels": lvl}, timeout=60.0)
                        ring_high_since = None
                        if act == "quit":
                            return
                else:
                    ring_high_since = None

                if LINE_RING_ACTIONS and 0 <= ring <= LINE_RING_LOW:
                    low_streak += 1
                    if low_streak >= 2:
                        import agent_hook
                        print(f"[patch] 线长圆环只剩 {ring:.0%}，线快被拉空，上报 Agent",
                              flush=True)
                        agent_hook.escalate(
                            "line_ring_low",
                            f"线长圆环只剩 {ring:.0%}，需要立刻小退",
                            {"ring": round(ring, 3)}, timeout=60.0)
                        return
                else:
                    low_streak = 0

                # 标定用观测日志：可读就每秒记一次，用来确认搏鱼时圆环的取值范围
                if ring >= 0 and time.time() - last_ring_log > 1.0:
                    print(f"[patch][ring] {ring:.2f} (t={time.time()-start:.0f}s)",
                          flush=True)
                    last_ring_log = time.time()

                # 力度条无色 = 这一瞬间鱼没在拉（可能是停顿，也可能已经到岸边）。
                # ⚠️ 这里原来写的是 `idle >= 10` 个样本，注释还写着"约 5 秒"，
                # 但实际采样间隔 0.2s，**10 个样本只有 2 秒** —— 鱼只要歇两秒就被
                # 判成"搏鱼结束"，于是松开左右键、转进抬竿阶段再重新按住，
                # 玩家看到的就是"钓着钓着键松一下又按回来"（2026-11-04 04:18 反馈）。
                # 改成按真实秒数计时，阈值给到 15 秒：正常鱼停顿不会这么久。
                if lvl > 0:
                    idle_since = None
                else:
                    if idle_since is None:
                        idle_since = time.time()
                    elif time.time() - idle_since >= PULL_IDLE_SECS:
                        print(f"[FLOW] pull 退出：力度条持续无色 "
                              f"{PULL_IDLE_SECS:.0f}s（历时 {time.time()-_t_pull:.1f}s）",
                              flush=True)
                        return

                if time.time() - start > timeout:
                    print(f"[FLOW] pull 退出：超时 {timeout:.0f}s", flush=True)
                    return

                sleep(FRICTION_SAMPLE)
                # 底钓不需要 coffee / gear ratio 检查：
                # 它们会在循环里反复抛异常被 error_handler 吞掉，导致 pull 被反复重入。
        finally:
            print(f"[FLOW] pull 结束（历时 {time.time()-_t_pull:.1f}s，"
                  f"档位 {_fric['cur']}）", flush=True)
            release_lift()

    Tackle.pull = pull
    print("[patch] 搏鱼：全程按住左右键 + 10s 窗口摩擦片控制"
          "（红->降1/3，全绿->升1/5，上限 29）+ 线长圆环卡住上报 Agent")


def patch_lift_fish(timeout=LIFT_TIMEOUT, manual_wait=600.0):
    """线收完之后的上鱼处理。

    玩家给的流程（2026-11-03）：搏鱼时左右键一路按住不放，直到线完全收回。
    收完之后只有两种结局：

      1. **鱼直接飞回来撞到人身上** -> 弹出结算画面，`handle_fish()` 按 Space 入护，完成；
      2. **鱼还留在水里** -> 先不做自动处理，停下等玩家手动把鱼拉上来。

    上游 `Player.lift_fish()` 首行是 `if not is_fish_hooked(): return`，而鱼被拉到
    岸边时力度条全灰，于是抛竿收尾这一段被整个跳过 —— 现在不看力度条，改看
    「圆形鱼图标」（有鱼=图标出现）和结算画面 `keep.png`（实测 0.997）。
    """
    from time import sleep

    from rf4s.component.logger import logger
    from rf4s.controller.player import Player

    def _both_down():
        import pydirectinput as pdi
        pdi.FAILSAFE = False
        pdi.keyDown("f5")
        hold_lift()

    def _both_up():
        import pydirectinput as pdi
        pdi.keyUp("f5")
        release_lift()

    def lift_fish(self) -> None:
        logger.info("Lifting fish (hold left + right)")
        _t_lift = time.time()
        print("[FLOW] lift_fish 进入（左右键将按住）", flush=True)
        # 进来时先看一眼:有鱼就不许用"4 秒无信号"提前放弃 ——
        # 鱼被拉到岸边时本来就没有张力(力度条全灰),只有图标还亮着。
        try:
            had_fish = fish_icon_present()
        except Exception:
            had_fish = True
        _both_down()                       # 左右键一起按住，一路到收完
        t0 = time.time()
        confirmed = had_fish
        lost = 0
        captured = False
        try:
            while time.time() - t0 < timeout:
                if self.detection.is_fish_captured():
                    captured = True
                    break
                icon = False
                try:
                    icon = fish_icon_present()      # 图标还在 = 鱼还挂在钩上
                except Exception:
                    icon = True                     # 读不到就当有鱼，别误判脱钩
                try:
                    lvl = friction_level()
                except Exception:
                    lvl = 1
                if icon or lvl > 0:                     # 任一信号在 = 鱼还在
                    confirmed = True
                    lost = 0
                elif confirmed:
                    # 图标熄灭且无张力，连续 2 秒才认为脱钩（单帧误判不算）
                    lost += 1
                    if lost >= LIFT_LOST_LIMIT:
                        print("[patch] 图标熄灭且无拉力，鱼已脱钩", flush=True)
                        break
                elif time.time() - t0 > LIFT_PROBE:
                    # 只有"进来时就没鱼"才走这条快速放弃
                    print("[patch] 图标不亮、也无拉力，判定无鱼", flush=True)
                    break
                sleep(LIFT_POLL)
        finally:
            _both_up()
            print(f"[FLOW] lift_fish 松开左右键（历时 {time.time()-_t_lift:.1f}s，"
                  f"captured={captured}）", flush=True)

        if not captured:
            sleep(0.2)
            captured = bool(self.detection.is_fish_captured())

        if captured:
            logger.info("Fish captured")
            self.handle_fish()
            return

        # 情况 2：鱼还在水里 —— 停手交给玩家，同时继续盯着结算画面
        print("", flush=True)
        print("[patch] ================ 需要人工上鱼 ================", flush=True)
        print("[patch] 线已收完但没弹结算框，鱼可能还在水边。", flush=True)
        print("[patch] 脚本已松手并暂停自动流程，请手动把鱼拉上来。", flush=True)
        print("[patch] 你把鱼收进鱼护后，脚本会自动继续下一竿。", flush=True)
        print("[patch] ================================================", flush=True)
        wait_until = time.time() + manual_wait
        while time.time() < wait_until:
            sleep(1.0)
            try:
                if self.detection.is_fish_captured():
                    logger.info("Fish captured manually")
                    self.handle_fish()
                    return
            except Exception:
                pass
        print("[patch] 等待人工处理超时，继续下一轮", flush=True)

    Player.lift_fish = lift_fish
    print("[patch] 上鱼：左右键按住直到结算；鱼留在水里则停手等人工")


def patch_retrieval_finished_debounce(streak=RETRIEVAL_FINISH_STREAK):
    """给 is_retrieval_finished 加防抖：必须连续 N 次匹配才认定收线完成。

    wheel.png 在线杯静止时就会匹配，鱼短暂松力的一瞬间就会被误判成"收完线"。
    要求连续 N 次（默认 6 × LOOP_DELAY≈0.5s ≈ 3 秒）都匹配才算数，
    这样鱼的短暂停顿不会打断收线，真正的收完仍能识别。
    """
    from rf4s.controller.detection import Detection

    orig = Detection.is_retrieval_finished
    counts: dict[int, int] = {}

    def is_retrieval_finished(self):
        try:
            hit = orig(self)
        except Exception:
            hit = False
        key = id(self)
        if not hit:
            counts[key] = 0
            return False
        counts[key] = counts.get(key, 0) + 1
        if counts[key] >= streak:
            counts[key] = 0
            return True
        return False

    Detection.is_retrieval_finished = is_retrieval_finished
    print(f"[patch] 收线完成判定加防抖（需连续 {streak} 次匹配）")
# ---------------- 咬钩判据：底部「圆形鱼图标」是否出现（玩家指定） ----------------
# 位置：力度条上一行、最左侧、「剩余耐力 XX%」文字左边，圆形白环 + 中间实心白鱼。
#
# 实测（2026-11-03 15:52–15:56，10fps 连拍 1447 帧）——**它是二值信号**：
#
#   | 状态 | 外环白色像素数(V>=238 & S<=28, r=9..14) |
#   |---|---|
#   | **有鱼咬钩** | 110 ~ 133（基本恒为 115） |
#   | **没有鱼**（刚抛竿等待 / 鱼已收完） | 0 ~ 22 |
#
# 与 bot 日志逐一吻合：
#   15:52:47 图标消失 ← 15:52:31 上鱼、15:52:50 抛竿（无鱼）
#   15:53:09 图标出现 ← 有鱼咬钩
#   15:53:37 图标消失 ← 15:53:40 收鱼、15:53:59 抛竿
#   15:54:19 图标出现 ← 有鱼（bot 16 秒后才在 15:54:39 报 Fish hooked）
#   15:55:33 图标消失 ← 15:55:37 收鱼
#
# 教训：之前所有"图标永远是亮的"结论，都是因为采样窗口正好覆盖了"已经挂着鱼"的时段
# （这个服咬钩快、bot 又要等 CHECK_DELAY=16s 才查），单帧比对自然看不出差别。
FISH_ICON_CENTER = (549, 1024)   # 图标中心
FISH_ICON_HALF = (20, 16)        # 裁剪半径：x,y
FISH_ICON_RING = (9, 14)         # 测量"外环"的半径区间
FISH_ICON_WHITE_MIN = 60         # 外环白色像素阈值（有=110+，无=0~22）


def fish_icon_present() -> bool:
    """底部圆形鱼图标是否出现 = 是否有鱼咬钩。"""
    import cv2

    cx, cy = FISH_ICON_CENTER
    hw, hh = FISH_ICON_HALF
    img, _ = capture_game()
    a = np.asarray(img.crop((cx - hw, cy - hh, cx + hw, cy + hh)))
    hsv = cv2.cvtColor(a, cv2.COLOR_RGB2HSV)
    v = hsv[:, :, 2].astype(int)
    s = hsv[:, :, 1].astype(int)
    yy, xx = np.mgrid[0:hh * 2, 0:hw * 2]
    rr = np.sqrt((yy - hh) ** 2 + (xx - hw) ** 2)
    ring = (rr >= FISH_ICON_RING[0]) & (rr <= FISH_ICON_RING[1])
    white = (v >= 238) & (s <= 28)
    return int(white[ring].sum()) >= FISH_ICON_WHITE_MIN


def is_fish_hooked_by_bar() -> bool:
    """旧判据（保留做兜底）：底部**钓具力度条出现了颜色**。

    ⚠️ 不可靠，实测有反例：2026-11-03 15:46 那轮，力度条从 15:46:01 到 15:46:11
    一直是黄（level 2），鱼是 15:46:12 才咬钩——没有鱼时条也可能是彩色的
    （半透明条把背景的沙地/红色格纹袖子透上来了）。只在图标判据取不到画面时兜底用。
    """
    try:
        return friction_level() > 0
    except Exception:
        return False


def _is_fish_hooked_bar_method(self) -> bool:
    """模块级方法：用来替换 Detection.is_fish_hooked_pixel。

    必须定义在模块级而不是写成 lambda —— 摩擦片（FrictionBrake）会把 Detection
    实例 pickle 给 spawn 子进程，**实例属性里塞 lambda 会直接报
    `Can't get local object ...<lambda>`**，一加 `-FB` 就崩。

    另外 `__name__` 必须改成 `is_fish_hooked_pixel`：pickle 序列化 bound method 用的是
    `func.__name__`，不是它被赋给类里的哪个名字。名字对不上的话父进程能 pickle，
    子进程反序列化时报 `'Detection' object has no attribute '_is_fish_hooked_bar_method'`。
    """
    try:
        return fish_icon_present()
    except Exception:
        return is_fish_hooked_by_bar()


_is_fish_hooked_bar_method.__name__ = "is_fish_hooked_pixel"


def patch_fish_hooked_bar():
    """把 is_fish_hooked 的底层判据换成「圆形鱼图标是否出现」。

    注意 1：`Detection.__init__` 里做过 `self.is_fish_hooked = self.is_fish_hooked_pixel`，
    这是**实例属性**，所以直接改类的 `is_fish_hooked` 不生效。

    注意 2：不能往实例属性里塞 lambda/`MethodType` —— 摩擦片（FrictionBrake）会把
    Detection 实例 pickle 给 spawn 子进程，`lambda` 会报 `Can't get local object`，
    `MethodType` 则能在父进程 pickle 成功、**子进程反序列化时找不到该属性**报
    `'Detection' object has no attribute '_is_fish_hooked_bar_method'`。

    所以改的是**类方法 `is_fish_hooked_pixel`**：`__init__` 里那句赋值自然就绑到了
    新实现上，而且 bound method 按「类 + 方法名」序列化，子进程能正常还原。
    子进程里的摩擦片监控只用 `friction_level()`，不依赖这个判据，换成原版也无影响。
    """
    from rf4s.controller.detection import Detection

    Detection.is_fish_hooked_pixel = _is_fish_hooked_bar_method
    print(f"[patch] 咬钩检测 = 「底部圆形鱼图标是否出现」"
          f"(中心 {FISH_ICON_CENTER}，环白色像素 >= {FISH_ICON_WHITE_MIN})")


# ==================== 渔获结算框：加置信度 + 位置双重校验 ====================
# 为什么必须收紧：`handle_fish()` 一旦认为"结算框出现了"就会**按空格**。
# 如果这一下误判，空格就落到世界里 —— 那等于把**抄网掏出来**，之后整个收线流程
# 都完不成（玩家实测踩到过，2026-11-03 16:34）。
#
# 实测 keep.png 的两个典型匹配点（1920x1080 客户区）：
#   * 真结算框：**0.997** @ (772, 137)   —— 屏幕左上的鱼图鉴
#   * 右下角鱼护 HUD 图标：0.74 ~ 0.76 @ (1776, 938)  —— 误判来源
# 所以除了把置信度提到 0.95，还要把匹配位置限制在结算框所在的区域。
CAPTURE_CONFIDENCE = 0.95
CAPTURE_POS_BOX = (300, 40, 1500, 620)   # x1, y1, x2, y2（客户区坐标）


def patch_fish_captured():
    """`is_fish_captured()` 收紧：结算框必须出现在它该出现的位置。"""
    from rf4s.controller.detection import Detection

    def is_fish_captured(self):
        try:
            box = self._get_image_box("keep", CAPTURE_CONFIDENCE)
        except Exception:
            return False
        if not box:
            return False
        vx, vy = _vorigin()
        cx, cy = box.left - vx, box.top - vy        # 转成客户区坐标
        x1, y1, x2, y2 = CAPTURE_POS_BOX
        if not (x1 <= cx <= x2 and y1 <= cy <= y2):
            print(f"[patch] keep.png 命中但位置不对 ({cx},{cy})，判为误匹配，忽略",
                  flush=True)
            return False
        return True

    Detection.is_fish_captured = is_fish_captured
    print(f"[patch] 结算框判据：keep.png >= {CAPTURE_CONFIDENCE} 且位置落在 "
          f"{CAPTURE_POS_BOX}（防止误按空格掏出抄网）")


# ==================== 入护：点按钮，不按空格（玩家指定） ====================
# 为什么必须分开：空格在游戏里同时是"入护"和"抄网"，一旦判据抖一下，
# 空的空格就会把抄网掏出来，之后整个收线流程都完不成。
# 玩家要求（2026-11-03）：入护一律**点屏幕上的按钮**；抄网另绑一个专用键。
#
# 按钮实测（1920x1080 客户区，来自真实结算框截图 `_cap_now.png`）：
#   「入护 [Space]」   边框 x 715..950, y 940..1008 -> 中心 (832, 974)
#   「释放 [Backspace]」边框 x 972..1208, y 940..1008 -> 中心 (1090, 974)
CAPTURE_KEEP_BTN = (830, 975)      # 实测边框 711..948 x 941..1008
CAPTURE_RELEASE_BTN = (1090, 975)  # 实测边框 971..1208 x 941..1008


# ---- SendInput 点击（mouse_event 在本客户端无效，只有 SendInput 能进 UI 层）----
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_VIRTUALDESK = 0x4000
_ULONG_PTR = ctypes.c_ulonglong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", _ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


def _send_mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
    inp = _INPUT()
    inp.type = 0                      # INPUT_MOUSE
    inp.u.mi = _MOUSEINPUT(dx, dy, ctypes.c_ulong(data & 0xFFFFFFFF), flags, 0, 0)
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def _click_client(cx: int, cy: int, hover: float = 0.35, tap: float = 0.12) -> None:
    """在游戏**客户区坐标**处做一次真实点击。

    ⚠️ 必须用 **SendInput**，不能再用 `mouse_event`：本客户端把 `mouse_event`
    合成的事件直接丢掉（实测点了十几次「确定」毫无反应），而 SendInput 的
    绝对坐标事件能进 UI 层。键盘同理（pydirectinput 走 scancode）。
    坐标要按**虚拟桌面**归一化到 0..65535（多屏、负坐标都靠 VIRTUALDESK）。
    """
    u = ctypes.windll.user32
    h = game_hwnd()
    p = wt.POINT(0, 0)
    u.ClientToScreen(h, ctypes.byref(p))
    sx, sy = p.x + cx, p.y + cy
    vx, vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
    vw, vh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
    nx = int(round((sx - vx) * 65535 / (vw - 1)))
    ny = int(round((sy - vy) * 65535 / (vh - 1)))
    _send_mouse(_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK,
                nx, ny)
    time.sleep(hover)                 # 留出 hover 时间，UI 才认这次按下
    _send_mouse(_MOUSEEVENTF_LEFTDOWN)
    time.sleep(tap)
    _send_mouse(_MOUSEEVENTF_LEFTUP)
    time.sleep(0.15)


def find_capture_keep_btn():
    """在结算框里动态找「入护」按钮中心。

    按钮位置**不固定**：结算框底部的奖励行数不一样（有没有"新级别/免费技能点/奖励"），
    按钮会整体上下浮动。实测两种情况：边框 y 941..1008 和 y 969..1049。
    所以扫"底部一对平行的长亮线"当按钮上下边框，中间那条竖线就是两个按钮的分隔。
    找不到就返回 None，调用方退回到固定坐标。
    """
    img, _ = capture_game()
    a = np.asarray(img.convert("L")).astype(int)
    y0, y1, x0, x1 = 900, 1079, 600, 1320
    rows = []
    for y in range(y0, y1):
        xs = np.where(a[y, x0:x1] >= 150)[0]
        if len(xs) > 150:
            rows.append((y, x0 + int(xs.min()), x0 + int(xs.max())))

    cand = None
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            dy = rows[j][0] - rows[i][0]
            if 40 <= dy <= 90 and abs(rows[i][1] - rows[j][1]) < 8 \
                    and abs(rows[i][2] - rows[j][2]) < 8:
                cand = (rows[i], rows[j])
                break
        if cand:
            break
    if not cand:
        return None

    (yt, xa, xb), (yb, _, _) = cand
    band = a[yt + 2:yb - 2, xa:xb]
    if band.size == 0:
        return None
    cols = (band >= 150).sum(axis=0)
    vs = np.where(cols > (yb - yt - 6) * 0.7)[0]
    mid_x = xa + int(vs[len(vs) // 2]) if len(vs) >= 3 else (xa + xb) // 2
    return ((xa + mid_x) // 2, (yt + yb) // 2)


# ---- 鱼护满了怎么办 ----
#   "stop"（默认）：只停手，**不关游戏**。醒来还能直接接着钓，人来了自己卖鱼/换护。
#   "quit"        ：上游行为，走控制面板把游戏关掉。
KEEPNET_FULL_ACTION = "stop"


def on_keepnet_full(player) -> None:
    """鱼护满时的收尾。默认只停手，不动游戏。"""
    if KEEPNET_FULL_ACTION == "quit":
        player.general_quit("Keepnet is full")
        return
    release_lift()
    print("", flush=True)
    print("=" * 56, flush=True)
    print("  鱼护已满，脚本停手（游戏保持不动，不会退出）", flush=True)
    print("  去卖鱼 / 换鱼护之后，再双击一次桌面开关就能继续。", flush=True)
    print("=" * 56, flush=True)
    # 用 KeyboardInterrupt 打断主循环：main() 里会接住它，
    # 走 finally 还原窗口、bot_ctl 再释放按键。比 general_quit 温和得多。
    raise KeyboardInterrupt


# ================= 弹窗「确定」统一处理 =================
#
# 这类弹窗在这套客户端里非常多，长得几乎一模一样、而且**排队出现**：
#   * 获得卡牌（鱼卡）-> 确定
#   * 获得新技能 / 技能点 -> 确定
#   * 能力解锁（"使用直排(IN-LINE)钓组"、"使用雪人钓组"…）-> 确定
#   * 升级（"新级别 27"）-> 确定
#   * 需要手动点击才能继续的提示框 -> 确定
#
# 实测（2026-11-03 ~ 04）：
#   * 「确定」按钮固定在这个位置：边框 x871..1048、y793..826，中心 (960, 810)
#   * 点掉一个立刻弹下一个（连锁 4 层都见过），所以必须**连点到按钮消失**
#   * 用 mouse_event 点无效（客户端丢弃），必须 SendInput（_click_client）
#   * 弹窗盖住 HUD 时，线长圆环读数为 -1、鱼图标恒为 False —— 这正好是判据
#
# 所以策略从"等 Agent 来救"改成"自己先清，清不掉才上报"：
#   maybe_dismiss() 在被调用的位置（抛竿前/等口时/入护后/抬竿前）先看一眼，
#   有就点干净；只有点不掉（按钮消失但 HUD 仍不可读 = 不是这类弹窗）才上报 Agent。
OK_BTN_BOX = (871, 793, 1048, 826)
OK_BTN_CENTER = (960, 810)


def _ok_button_present() -> bool:
    """检测「确定」按钮在不在。

    判据：按钮上下两条边框线各自的亮像素占比 > 85%。
    连续两帧都成立才认（防一格闪烁误判成按钮）。
    """
    try:
        for _ in range(2):
            img, _ = capture_game()
            a = np.asarray(img.convert("L")).astype(int)
            x0, y0, x1, y1 = OK_BTN_BOX
            top = a[y0, x0:x1]
            bot = a[y1, x0:x1]
            if not (int((top >= 150).sum()) > (x1 - x0) * 0.85
                    and int((bot >= 150).sum()) > (x1 - x0) * 0.85):
                return False
            time.sleep(0.05)
        return True
    except Exception:
        return False


def dismiss_modals(max_clicks: int = 12, gap: float = 0.9) -> bool:
    """连点「确定」把排队弹窗清空。返回 True 表示清干净了（或本来就没有）。

    弹窗是排队的：点掉"新级别 27"立刻弹"+1 免费技能点"，
    所以要点到按钮消失为止，不能只点一次。
    """
    for i in range(max_clicks):
        if _ok_button_present():
            focus_game_quiet()
            _click_client(*OK_BTN_CENTER)
            print(f"[patch] 点掉弹窗 #{i + 1}", flush=True)
            time.sleep(gap)
        else:
            return True
    return not _ok_button_present()


def maybe_dismiss(tag: str = "", quiet: bool = True) -> bool:
    """**统一入口**：有弹窗就清掉，没有就几乎零开销返回。

    在流程里这些位置调用：
      * 抛竿前（弹窗会吃掉 Shift/F5）
      * 等咬钩的轮询里（弹窗盖住 HUD 时鱼图标恒 False，会一直等到重抛）
      * 入护之后（卡牌/技能点弹窗就在这之后弹）
      * 抬竿/收线前
    返回 True = 这次确实清掉了东西（调用方据此决定要不要重判状态）。
    """
    if not _ok_button_present():
        return False
    if not quiet:
        print(f"[patch] 检测到弹窗（{tag}），开始清理", flush=True)
    ok = dismiss_modals()
    print(f"[patch] 弹窗清理完成（{tag}）：{'干净' if ok else '没清完，交给上层处理'}",
          flush=True)
    return ok


# ---- 小退：Esc 回主菜单 -> 按住 Shift 点「退出」-> 回登录界面（玩家给的步骤）----
RELOG_QUIT_BTN = (1430, 745)   # 主菜单右下角「退出」磁贴，实测边框 x1370..1490 / y683..807


def quick_relog() -> None:
    """小退到登录界面。

    玩家给的流程（2026-11-03）：Esc 打开主菜单 -> **按住 Shift** 点「退出」-> 登录界面。
    之后要重新登录（用户名密码那一套不在这里做，避免误操作）。
    """
    import pydirectinput as pdi

    pdi.FAILSAFE = False
    focus_game_quiet()
    pdi.press("esc")
    time.sleep(1.6)
    pdi.keyDown("shift")
    _click_client(*RELOG_QUIT_BTN)
    time.sleep(0.3)
    pdi.keyUp("shift")
    time.sleep(1.2)
    print("[patch] 已小退到登录界面，请重新登录", flush=True)


def patch_key_watch():
    """只加日志：记录左键(mouse_pressed)变化、以及 error_handler 吞掉了什么异常。

    目的：查"搏鱼中左右键莫名松开"到底是谁干的。可能来源：
      * 我们自己的 idle 判据误判搏鱼结束（已改成 15 秒）
      * 上游 error_handler 捕获 LiftTimeoutError -> hold_keys(mouse=False, reset=True)
        -> 强制松键再重入 pull_fish（player.py:490）
      * 上游 lift/pull 内部用 hold_mouse_button(N) 的脉冲式按松（tackle.py:190）
    加完日志跑一轮，看 [KEY]/[EXC] 的时序就能定案，不靠猜。

    ⚠️ 上游这两个都是 @contextmanager，包装时也必须用 @contextmanager，
    否则调用方 `with` 会报 "generator object does not support the context manager protocol"。
    """
    from contextlib import contextmanager

    from rf4s.controller.player import Player

    orig_hold_keys = Player.hold_keys
    orig_err = Player.error_handler

    @contextmanager
    def hold_keys(self, mouse, shift, reset=False):
        before = getattr(self, "mouse_pressed", None)
        with orig_hold_keys(self, mouse, shift, reset):
            after = getattr(self, "mouse_pressed", None)
            if before != after:
                print(f"[KEY] 左键 {'按下' if after else '松开'} "
                      f"(hold_keys mouse={mouse} shift={shift} reset={reset} "
                      f"by {_who()})", flush=True)
            yield
        final = getattr(self, "mouse_pressed", None)
        if final != after:
            print(f"[KEY] 左键 {'按下' if final else '松开'} "
                  f"(hold_keys 退出 reset={reset} by {_who()})", flush=True)

    @contextmanager
    def error_handler(self):
        import traceback

        try:
            with orig_err(self):
                yield
        except Exception as e:
            print(f"[EXC] error_handler 捕获 {type(e).__name__}: {e}", flush=True)
            try:
                print("[EXC] 调用栈: " + " <- ".join(
                    f"{fr.name}:{fr.lineno}" for fr in
                    traceback.extract_stack()[-6:-1]), flush=True)
            except Exception:
                pass
            raise

    Player.hold_keys = hold_keys
    Player.error_handler = error_handler
    print("[patch] 键状态日志已启用（[KEY] 按下/松开、[EXC] 被吞掉的异常）")


def patch_handle_fish_click():
    """`_handle_fish()` 改成**点「入护」按钮**，不再按空格。

    顺带把 `handle_events()` 也停掉：它循环 `while is_event_triggered(): press("enter")`，
    而本客户端 `event_ok.png` 会误判 —— 每收一条鱼都要按十几下 **Enter**
    （Enter 是「开/关线杯」），实测每次结算卡 10~11 秒，还会把线杯状态搞乱。
    我们不需要礼物/卡片那些事件处理，直接跳过。
    """
    from time import sleep

    from rf4s.component.logger import logger
    from rf4s.controller.player import Player
    from rf4s.utils import add_jitter

    def handle_events(self) -> None:
        try:
            if self.detection.is_event_triggered():
                print("[patch] 检测到'事件'弹窗，但本适配器已跳过事件处理（不按 Enter）",
                      flush=True)
        except Exception:
            pass

    def handle_fish(self) -> None:
        """接管上游：跳过 `CATCH_SCREEN_STYLE=animated` 那两段硬睡（10s + 0.5s）。"""
        if not self.detection.is_fish_captured():
            return
        logger.info("Handling fish (click keep)")
        sleep(0.5)                                  # 等结算框淡入
        with self.hold_keys(mouse=False, shift=False):
            self._handle_fish()
            if self.cfg.PROFILE.MODE in ["bottom", "pirk", "elevator"]:
                self.timer.update_cast_time()
            self.timer.add_cast_time()
            limit = self.cfg.BOT.KEEPNET.CAPACITY - self.cfg.ARGS.FISHES_IN_KEEPNET
            if self.result.kept >= limit:
                on_keepnet_full(self)

    def _handle_fish(self) -> None:
        self.result.total += 1
        try:
            focus_game_quiet()
            btn = find_capture_keep_btn() or CAPTURE_KEEP_BTN
            print(f"[patch] 入护按钮定位 {btn}", flush=True)
            _click_client(*btn)
            logger.info("Keep fish by clicking the button")
        except Exception as e:
            print(f"[patch][warn] 点击入护按钮失败: {e}", flush=True)
        self.result.kept += 1

        sleep(add_jitter(0.6))
        # 入护后最常见的就是「获得卡牌」/「获得技能点」弹窗：它会盖住 HUD，
        # 让下一轮等口判据失效。这里立刻清掉，别等看门狗 15 秒后才发现。
        try:
            maybe_dismiss("入护后")
        except Exception:
            pass
        try:
            if self.detection.is_keepnet_full():
                self.result.kept -= 1
                self.general_quit("Keepnet is full")
        except Exception:
            pass

    Player.handle_events = handle_events
    Player.handle_fish = handle_fish
    Player._handle_fish = _handle_fish
    print(f"[patch] 入护改为点击按钮 {CAPTURE_KEEP_BTN}（不按空格），"
          f"并跳过 10s 结算动画等待")


# 抄网：不再共用空格。客户端里「使用/放下炒网」本来就有**两个**槽位：
#   Space / PD(PageDown)  ——  见 设置-控制-键盘设置
# 所以不需要新绑键，直接用 **PageDown**，把 Space 完全让给"入护"（现在入护已经改成点按钮）。
NET_KEY = "pagedown"


def net_toggle() -> None:
    """拿出/收起抄网。用专用键，避免和"入护"抢空格。"""
    import pydirectinput as pdi

    pdi.FAILSAFE = False
    focus_game_quiet()
    pdi.press(NET_KEY)
    print(f"[patch] 切换抄网 ({NET_KEY})", flush=True)


# 底部钓具承受力度横条，实测 y=1045..1057（半透明条），x 从 561 延伸到 1383 附近
# 界面文字写作「剩余耐力 XX%」，但物理含义是「钓具承受的力度」，不是鱼的耐力。
FRICTION_BAR = (1045, 1058, 550, 1400)   # y1, y2, x1, x2

# 等级：0=无/说不清, 1=绿, 2=黄, 3=橙, 4=红
FRICTION_SAT_MIN = 90      # 旧判据的饱和度下限（保留给文档/兼容）
FRICTION_COLOR_MIN = 0.05  # 旧判据的颜色占比阈值（保留）

# 新判据（按"彩色带前缘"识别）：实测带子是不透明画的，S 155~170、V≈200；
# 而背景（红格纹袖子）透上来的颜色只有 S 60~85，用这个阈值就能把假红挡掉。
BAR_SAT_MIN = 130          # 带子像素的饱和度下限
BAR_VAL_MIN = 110          # 带子像素的亮度下限
BAR_MIN_WIDTH = 12         # 带子至少这么宽才认（再窄就是噪声）
# 注：摩擦片的档位常量统一在上面「摩擦片：维护状态变量」一节定义，
# 这里不要再重复定义，否则后面的值会把前面的覆盖掉（踩过：RISE_RATIO 变成 1/6）。


_bar_info = {"level": 0, "span": None, "hue": None, "sat": None}
_fric_log = {"fh": None, "t0": None}


def _log_bar_sample(level, info):
    """把每次力度条读数写进 bot/_fric_log.csv，供事后标定阈值用。"""
    try:
        if _fric_log["fh"] is None:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "_fric_log.csv")
            _fric_log["fh"] = open(path, "w", encoding="utf-8")
            _fric_log["fh"].write("t,level,span0,span1,hue,sat,friction\n")
            _fric_log["t0"] = time.time()
        span = info.get("span") or ("", "")
        t = time.time() - _fric_log["t0"]
        _fric_log["fh"].write(
            f"{t:.2f},{level},{span[0]},{span[1]},{info.get('hue','')},"
            f"{info.get('sat','')},{_fric['cur']}\n")
        _fric_log["fh"].flush()
    except Exception:
        pass



def friction_bar_state():
    """解析底部力度条，返回 (level, info)。

    实测这个条的结构（2026-11-04 在真实搏鱼画面上一列一列量出来的）：

      * 轨道 x≈540..1390，y≈1047..1058，平时是灰的（S≈40）
      * **有负载时出现一段彩色带**，而且不是"从左往右填"，是**一段浮动的渐变带**：
        左端绿、右端黄/橙/红 —— **右端（前缘）的颜色才代表当前负载**
      * 带子是**不透明**画的（S≈155~170, V≈200），背景透上来的颜色要暗得多
        （实测红格纹袖子透上来只有 S≈60~85）—— 这就是之前"假红"的来源

    所以判据改成：先用高饱和阈值找出带子 -> 取最右 25% 的中位色相当等级。
    这样背景透色被阈值挡掉，也不会被"整条里红色占比"带的到处乱跑。
    """
    import cv2

    img, _ = capture_game()
    a = np.asarray(img)
    y1, y2, x1, x2 = FRICTION_BAR
    if a.shape[0] < y2 or a.shape[1] < x2:
        return 0, dict(_bar_info)

    strip = a[y1:y2, x1:x2]
    hsv = cv2.cvtColor(strip, cv2.COLOR_RGB2HSV)
    hh = hsv[:, :, 0].astype(int)
    ss = hsv[:, :, 1].astype(int)
    vv = hsv[:, :, 2].astype(int)

    # 带子像素：高饱和 + 够亮（不透明绘制），背景透色达不到
    band = (ss >= BAR_SAT_MIN) & (vv >= BAR_VAL_MIN)
    col = band.mean(axis=0)

    # 最长的连续列段（允许 2px 的缝，抗锯齿/刻度线会造成小断裂）
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

    w = best[1] - best[0]
    if w < BAR_MIN_WIDTH:
        _bar_info.update(level=0, span=None, hue=None, sat=None)
        return 0, dict(_bar_info)

    # 前缘 = 最右 25%（至少 8 列）
    n_lead = max(8, int(w * 0.25))
    lead = band[:, best[1] - n_lead + 1:best[1] + 1]
    hue = int(np.median(hh[:, best[1] - n_lead + 1:best[1] + 1][lead]))
    sat = int(np.median(ss[:, best[1] - n_lead + 1:best[1] + 1][lead]))

    # 分档阈值（2026-11-04 12:49 用玩家指认的截图标定）：
    #   1 档（力最小）实测 hue=33 RGB(200,214,63) 黄绿——玩家指认
    #   2 档（目标）  实测 hue=25 RGB(229,188,0) 金黄偏橙——玩家指认
    #   3 档（红）    实测 hue=6  红
    # 橙/黄绿分界取 25 与 33 的正中间 = 29（两侧各留 4° 余量）。
    # ⚠️ 之前分界是 22，把 2 档的 25° 误判成 1 档——历史日志里 25° 出现 100 次
    # 全被当成"力太小"继续升档，本该命中目标稳住的。
    if hue <= 10 or hue >= 170:
        level = 4                                  # 红 = 3 档
    elif hue <= 29:
        level = 3                                  # 橙/琥珀 = 2 档（目标）
    elif hue <= 85:
        level = 2                                  # 黄绿 = 1 档（力小）
    else:
        level = 0

    _bar_info.update(level=level, span=(x1 + best[0], x1 + best[1]),
                     hue=hue, sat=sat)
    return level, dict(_bar_info)


def friction_level() -> int:
    """兼容旧调用：只要等级。"""
    return friction_bar_state()[0]


def suppress_ime_overlay() -> int:
    """隐藏 IME（讯飞等）注入宿主进程后创建的大面积通知窗口。

    实测：讯飞输入法会在 rf4_x64.exe 里建一个 `iFLY_notify_window`，
    被撑成 1920x1080、WS_EX_TOPMOST 且**不穿透**，位置冻在窗口早期的坐标上。
    它没有任何可见内容，但 `WindowFromPoint` 命中的就是它——主屏一大块区域
    的点击/滚轮全被吃掉，而且截图上完全看不出异常。

    这类窗口用 `ImmAssociateContext(hwnd, NULL)` 关不掉（游戏窗口的 IME
    上下文本来就是 0），只能枚举出来直接 SW_HIDE。实测隐藏后不会重建。

    返回隐藏的窗口数。
    """
    hidden = 0

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lp):
        nonlocal hidden
        cls = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, cls, 256)
        name = cls.value
        if "iFLY" not in name and "InputMethod" not in name:
            return True
        r = wt.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, hh = r.right - r.left, r.bottom - r.top
        if w >= 200 and hh >= 200:      # 只处理会遮挡的，0x0 的不用管
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE
            hidden += 1
        return True

    ctypes.windll.user32.EnumWindows(_cb, 0)
    if hidden:
        print(f"[patch] 已隐藏 {hidden} 个 IME 幽灵窗口（原本会吃掉鼠标事件）")
    return hidden


def main():
    # 把自己的 PID 写下来，桌面开关靠它判断/停止（不依赖 wmic 查命令行）
    try:
        pid_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.pid")
        with open(pid_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    h = game_hwnd()
    prepare_window(h)
    suppress_ime_overlay()
    selfcheck()

    import pyautogui as pag
    import pydirectinput as pdi

    patch_screenshot(pag)
    patch_input(pag, pdi)
    patch_cast(pdi)
    patch_retrieval_timeout()
    patch_tackle_ready_timeout()
    patch_retrieval_finished_debounce()
    patch_pull_continuous()
    patch_lift_fish()
    patch_fish_hooked_bar()
    patch_fish_captured()
    patch_handle_fish_click()
    patch_key_watch()
    patch_single_rod(pag, pdi)
    patch_bottom_mode_held()

    # 验证合成后的虚拟桌面里游戏区域确实有内容
    vx, vy = _vorigin()
    img = screen(force=True)
    print(f"[patch] 虚拟桌面 {img.size}，游戏区域已合成")

    import main as M
    try:
        M.main()
    except KeyboardInterrupt:
        print("\n[patch] 已中断")
    finally:
        restore_window(h)
        print("[patch] 窗口已还原")


if __name__ == "__main__":
    main()
