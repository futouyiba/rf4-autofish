"""RF4S 适配启动器（不改 RF4S 源码，运行时打补丁）。

解决本客户端（A:/RF4_CN_daoban2/Game/rf4_x64.exe）的四个不兼容：
  1. 独占渲染：ImageGrab / mss 截图全黑 -> 改用 PrintWindow 合成虚拟桌面
  2. 鼠标注入无效：mouseDown/Up -> keyDown/Up('f5')（F5 已绑定为"基本操作"）
  3. 抛竿需全力抛：mouseDown 期间配合 pag.hold('shift') 自动等价于 Shift+F5
  4. 线杯未开则抛不出：cast 前补按 Enter
窗口方面：**保留标题栏**，把窗口高度补偿成 1080+标题栏，使客户区精确 1920x1080；
不能直接去掉 WS_CAPTION，那会让窗口变成无边框全屏，DWM 进 direct-flip，
拖慢其他窗口重绘（实测会导致 WorkBuddy 滚轮失效）。

用法：
    python rf4s_patch.py bot -N BOTTOM
"""
import ctypes
import ctypes.wintypes as wt
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

# ---- RF4S 源码位置（第三方克隆，被 .gitignore 排除，不入库）----
REF_DIR = Path(r"A:/Projs/REA/.rea-work/bot/refs/RussianFishing4Script")
if not REF_DIR.exists():
    raise SystemExit(f"[patch] 找不到 RF4S 源码目录: {REF_DIR}\n"
                     f"       请先 git clone 到该路径（见 bot/RF4S-NOTES.md）")
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
    """用 PrintWindow 抓游戏客户区，返回 (Image, 屏幕左上角坐标)。"""
    h = game_hwnd()
    pt = wt.POINT(0, 0)
    ctypes.windll.user32.ClientToScreen(h, ctypes.byref(pt))
    cr = wt.RECT()
    ctypes.windll.user32.GetClientRect(h, ctypes.byref(cr))
    w, hh = cr.right, cr.bottom
    wdc = win32gui.GetWindowDC(h)
    mfc = win32ui.CreateDCFromHandle(wdc)
    mem = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, w, hh)
    mem.SelectObject(bmp)
    ctypes.windll.user32.PrintWindow(h, mem.GetSafeHdc(), 2)
    info = bmp.GetInfo()
    img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                           bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    mem.DeleteDC()
    mfc.DeleteDC()
    win32gui.ReleaseDC(h, wdc)
    win32gui.DeleteObject(bmp.GetHandle())
    return img.copy(), (pt.x, pt.y)


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
        pdi.keyDown("f6" if button == "right" else "f5")

    def mouseUp(x=None, y=None, button="left", **kwargs):
        pdi.keyUp("f6" if button == "right" else "f5")

    pag.mouseDown = mouseDown
    pag.mouseUp = mouseUp
    print("[patch] 鼠标按键已映射: 左键->F5, 右键->F6")


FRICTION_WINDOW = 10.0   # 连续观察窗口（秒）
FRICTION_SAMPLE = 0.2    # 采样间隔


def monitor_friction_brake_windowed(friction_brake):
    """连续 FRICTION_WINDOW 秒内拉力峰值都过小，才上调摩擦片。

    上游逻辑是「当前不是高档就每 INCREASE_DELAY 升一档」，鱼短暂松力的瞬间
    就会误解为"力度小"而误升。这里改成滑动窗口：窗口内只要出现过一次高峰
    （摩擦片指示变红 或 线杯发热）就重置窗口；整段都平缓才升一档。

    必须定义在模块级：Windows 用 spawn，子进程靠 pickle 引用
    `rf4s_adapter.monitor_friction_brake_windowed` 来取到这个函数。
    """
    from time import sleep, time

    from rf4s.component.friction_brake import FRICTION_BRAKE_MONITOR_DELAY
    from rf4s.component.logger import logger
    from rf4s.utils import add_jitter

    fb = friction_brake
    fb_cfg = fb.fb_cfg
    logger.info("Monitoring friction brake (window=%.0fs)", FRICTION_WINDOW)

    fish_hooked = False
    window_start = None
    try:
        while True:
            if not fb.detection.is_fish_hooked_pixel():
                sleep(add_jitter(FRICTION_BRAKE_MONITOR_DELAY))
                window_start = None          # 没鱼不计窗口
                continue

            if not fish_hooked:
                sleep(add_jitter(fb_cfg.START_DELAY, fb.cfg.BOT.JITTER_SCALE))
                fish_hooked = True
                window_start = time()
                continue

            with fb.lock:
                if (fb.detection.is_friction_brake_high()
                        or fb.detection.is_reel_burning()):
                    # 出现峰值：降档并重置窗口
                    fb.change(increase=False)
                    sleep(add_jitter(fb_cfg.DECREASE_DELAY, fb.cfg.BOT.JITTER_SCALE))
                    window_start = time()
                else:
                    if window_start is None:
                        window_start = time()
                    elif time() - window_start >= FRICTION_WINDOW:
                        # 整窗都平缓 -> 升档，重新计窗
                        fb.change(increase=True)
                        window_start = time()
            sleep(FRICTION_SAMPLE)
    except KeyboardInterrupt:
        pass


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
        limit = self.cfg.PROFILE.CHECK_MISS_LIMIT
        logger.info("Bottom mode started (single rod, held, no put-down)")

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
                        sleep(add_jitter(self.cfg.PROFILE.CHECK_DELAY))

    Player.start_bottom_mode = start_bottom_mode_held
    print("[patch] 底钓改为单竿手持模式（不放竿、不轮询）")


def patch_friction_brake():
    """把上游的摩擦片监控换成窗口版。"""
    import rf4s.component.friction_brake as fbmod

    fbmod.monitor_friction_brake = monitor_friction_brake_windowed
    print(f"[patch] 摩擦片改为窗口判定：连续 {FRICTION_WINDOW:.0f}s 无高峰才升档")


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


def patch_cast(pdi):
    """抛竿前先按 Enter 开线杯，否则抛不出去。"""
    from rf4s.component.tackle import Tackle
    orig = Tackle.cast

    def cast(self, lock):
        pdi.press("enter")
        time.sleep(0.25)
        return orig(self, lock)

    Tackle.cast = cast
    print("[patch] 抛竿前已注入 Enter（开线杯）")


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


def main():
    h = game_hwnd()
    prepare_window(h)
    selfcheck()

    import pyautogui as pag
    import pydirectinput as pdi

    patch_screenshot(pag)
    patch_input(pag, pdi)
    patch_cast(pdi)
    patch_single_rod(pag, pdi)
    patch_friction_brake()
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
