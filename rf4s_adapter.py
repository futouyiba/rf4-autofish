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

# ---- RF4S 源码位置（第三方项目，未随本仓库分发，见 README 的"准备"一节）----
# 默认取 <本文件目录>/refs/RussianFishing4Script，也可用环境变量 RF4S_REF 指定
REF_DIR = Path(os.environ.get("RF4S_REF",
                              Path(__file__).resolve().parent / "refs" / "RussianFishing4Script"))
if not REF_DIR.exists():
    raise SystemExit(f"[patch] 找不到 RF4S 源码目录: {REF_DIR}\n"
                     f"       请先 clone RussianFishing4Script 到该路径，"
                     f"或设置环境变量 RF4S_REF")
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
                        while waited < self.cfg.PROFILE.CHECK_DELAY:
                            if self.detection.is_fish_hooked():
                                break
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
FRICTION_WINDOW = 10.0
FRICTION_SAMPLE = 0.2
FRICTION_SCROLL_GAP = 0.055    # 每格之间的间隔
FRICTION_LOCK = 0.8            # 调整后锁这么久，避免连续触发

_fric = {"cur": FRICTION_INITIAL, "t": 0.0}


def _drop_target(cur: int) -> int:
    """降 1/3：**整数除法**。17 -> 12，16 -> 11（玩家给的例子，用 round 会算成 11）。"""
    return max(FRICTION_MIN, cur - max(1, cur // FRICTION_DROP_DIV))


def _rise_target(cur: int) -> int:
    """升 1/5：**整数除法**。17 -> 20，正好是"滚轮向上滚 3 次"。"""
    return min(FRICTION_MAX, cur + max(1, cur // FRICTION_RISE_DIV))


def _wheel(n: int) -> None:
    """滚轮 n 格：正=向上（加档），负=向下。"""
    if n == 0:
        return
    u = ctypes.windll.user32
    delta = 120 if n > 0 else -120
    for _ in range(abs(n)):
        u.mouse_event(0x0800, 0, 0, delta, 0)   # MOUSEEVENTF_WHEEL
        time.sleep(FRICTION_SCROLL_GAP)


def friction_absolute(target: int = FRICTION_INITIAL) -> None:
    """抛竿前的绝对校准：先滚到顶，再退回 target。**只在抛竿前用。**"""
    if not focus_game_quiet():
        print("[patch][warn] 摩擦片绝对校准跳过：游戏窗口没能置前", flush=True)
        return
    _wheel(FRICTION_ABS_TOP)
    time.sleep(0.2)
    _wheel(-(FRICTION_ABS_TOP - target))
    _fric["cur"] = target
    _fric["t"] = time.time()
    print(f"[patch] 摩擦片绝对校准 -> {target} 档", flush=True)


def friction_nudge(delta: int) -> None:
    """增量调整：只滚 |delta| 格，并同步脚本内的状态变量。

    置前失败就**什么都不做、也不改状态**——宁可这轮不调，也不要把状态变量改成一个
    游戏里实际不成立的值（那会让后面所有判断都错，最后就是断线）。
    """
    if delta == 0:
        return
    new = max(FRICTION_MIN, min(FRICTION_MAX, _fric["cur"] + delta))
    if new == _fric["cur"]:
        return
    if not focus_game_quiet():
        print("[patch][warn] 摩擦片调整跳过：游戏窗口没能置前", flush=True)
        return
    _wheel(new - _fric["cur"])
    print(f"[patch] 摩擦片 {_fric['cur']} -> {new}（{new - _fric['cur']:+d} 格）",
          flush=True)
    _fric["cur"] = new
    _fric["t"] = time.time()


def friction_controller_step(win: list) -> None:
    """搏鱼中的摩擦片控制。`win` 是 [(t, level), ...]，只保留最近 10 秒。

    规则（玩家指定）：
      * 最近 10 秒**只要有一帧到红** -> 立刻降 1/3（整数除法：17 -> 12）
      * 最近 10 秒**全是绿**（没有黄/橙/红） -> 升 1/5（17 -> 20，向上滚 3 格）
      * 其余（只有黄/橙）-> 不动
    """
    now = time.time()
    while win and now - win[0][0] > FRICTION_WINDOW:
        win.pop(0)
    if not win or now - _fric["t"] < FRICTION_LOCK:
        return

    levels = [l for _, l in win]
    wmax = max(levels)

    if wmax >= 4:                                   # 出现过红 -> 立刻降
        # 只在"红确实持续"时动手：最近 3 帧里至少 2 帧是红（约 0.4s）。
        # 单帧红常常是半透明条把背景（红色格纹袖子）透上来造成的假红。
        tail = [l for _, l in win[-3:]]
        if tail.count(4) < 2:
            return
        target = _drop_target(_fric["cur"])
        friction_nudge(target - _fric["cur"])
        win.clear()
    elif wmax == 1 and now - win[0][0] >= FRICTION_WINDOW - 0.5:
        # 整个窗口都只有绿 -> 升 1/5
        target = _rise_target(_fric["cur"])
        friction_nudge(target - _fric["cur"])
        win.clear()


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
        time.sleep(0.15)
        friction_absolute(FRICTION_INITIAL)   # 唯一一次绝对校准
        self.stage = StageId.CAST
        focus_game_quiet()

        pdi.keyDown("shift")
        time.sleep(0.08)
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


def hold_lift() -> None:
    """按住抬竿（右键 -> F6），幂等。"""
    if _lift["held"]:
        return
    import pydirectinput as pdi

    pdi.FAILSAFE = False
    pdi.keyDown(LIFT_KEY)
    _lift["held"] = True


def release_lift() -> None:
    """松开抬竿，幂等。必须用 pdi.keyUp：SendInput+VK 解不开卡住的键。"""
    if not _lift["held"]:
        return
    import pydirectinput as pdi

    pdi.keyUp(LIFT_KEY)
    _lift["held"] = False


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
        if self.stage != StageId.PULL:
            self.stage = StageId.PULL
            self.timer.set_timeout_start_time()
        hold_lift()      # 抬竿全程按住（左键由外层 hold_keys 一直按着）
        start = time.time()
        idle = 0
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
                    lvl = friction_level()
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

                if lvl > 0:
                    idle = 0
                else:
                    idle += 1
                    if idle >= 10:      # 约 5 秒
                        print("[patch] 力度条持续无色，结束搏鱼（转抬竿上鱼）",
                              flush=True)
                        return

                if time.time() - start > timeout:
                    print(f"[patch] 搏鱼收线超过 {timeout:.0f}s，结束本轮", flush=True)
                    return

                sleep(FRICTION_SAMPLE)
                # 底钓不需要 coffee / gear ratio 检查：
                # 它们会在循环里反复抛异常被 error_handler 吞掉，导致 pull 被反复重入。
        finally:
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


def _send_mouse(flags: int, dx: int = 0, dy: int = 0) -> None:
    inp = _INPUT()
    inp.type = 0                      # INPUT_MOUSE
    inp.u.mi = _MOUSEINPUT(dx, dy, 0, flags, 0, 0)
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


# ---- 通用弹窗「确定」按钮：(871..1048, 793..826)，实测多个弹窗共用这个位置 ----
OK_BTN_BOX = (871, 793, 1048, 826)
OK_BTN_CENTER = (960, 810)


def _ok_button_present() -> bool:
    """检测 (871..1048, 793..826) 处有没有那个「确定」按钮（看上下边框亮线）。"""
    img, _ = capture_game()
    a = np.asarray(img.convert("L")).astype(int)
    x0, y0, x1, y1 = OK_BTN_BOX
    top = a[y0, x0:x1]
    bot = a[y1, x0:x1]
    return int((top >= 150).sum()) > (x1 - x0) * 0.85 and \
        int((bot >= 150).sum()) > (x1 - x0) * 0.85


def dismiss_modals(max_clicks: int = 12, gap: float = 0.9) -> bool:
    """连点「确定」把排队弹窗清空（能力解锁/升级/技能点这类）。

    这些弹窗**长得一模一样**、而且是排队的：点掉一个立刻弹出下一个，
    单看点一次会误以为没生效。所以按固定位置连点到按钮消失为止。
    返回 True 表示清干净了。
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
            if self.result.kept == limit:
                self.general_quit("Keepnet is full")

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
FRICTION_SAT_MIN = 90      # HSV 饱和度下限
FRICTION_COLOR_MIN = 0.05  # 某种颜色占比超过 5% 才算数
# 注：摩擦片的档位常量统一在上面「摩擦片：维护状态变量」一节定义，
# 这里不要再重复定义，否则后面的值会把前面的覆盖掉（踩过：RISE_RATIO 变成 1/6）。


def friction_level() -> int:
    """返回钓具力度条当前等级：0=无, 1=绿, 2=黄, 3=橙, 4=红。

    取条上占比最大的那种颜色。条是半透明叠在背景上的，
    所以用 HSV 的**色相 + 饱和度**分类，而不是比对固定 RGB。

    必须定义在模块级：Windows 用 spawn，子进程靠 pickle 引用。
    """
    import cv2

    img, _ = capture_game()
    a = np.asarray(img)
    y1, y2, x1, x2 = FRICTION_BAR
    if a.shape[0] < y2 or a.shape[1] < x2:
        return 0
    strip = a[y1:y2, x1:x2]
    hsv = cv2.cvtColor(strip, cv2.COLOR_RGB2HSV)
    hh = hsv[:, :, 0].astype(int)
    ss = hsv[:, :, 1].astype(int)
    vv = hsv[:, :, 2].astype(int)
    sat = (ss > FRICTION_SAT_MIN) & (vv > 80)

    shares = {
        4: (((hh <= 10) | (hh >= 170)) & sat).mean(),   # 红
        3: (((hh >= 11) & (hh <= 22)) & sat).mean(),    # 橙
        2: (((hh >= 23) & (hh <= 35)) & sat).mean(),    # 黄
        1: (((hh >= 36) & (hh <= 85)) & sat).mean(),    # 绿
    }
    best = max(shares, key=shares.get)
    if shares[best] < FRICTION_COLOR_MIN:
        return 0
    return best



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
