# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包描述：把控制面板 + 钓鱼主循环 + RF4S 快照打成一个绿色包。

用法（在 bot/ 目录下）::

    refs/RussianFishing4Script/.venv/Scripts/python.exe -m PyInstaller \
        --noconfirm --clean rf4autofish.spec

产物::

    dist/RF4AutoFish/RF4AutoFish.exe          ← 双击即控制面板
    dist/RF4AutoFish/_internal/               ← 依赖与资源，必须一起分发

设计要点
--------
* **默认 onedir**：面板要拉起后台子进程（``--bot``）。onedir 只解包一次，
  冷启动快；onefile 每次启动都要把 ~215MB 解到 %TEMP%，慢 5~15 秒，但只有
  一个文件、拷起来干净。想打单文件就设 ``RF4_ONEFILE=1``（见 build_exe.sh
  ``--onefile``），spec 会切成 EXE+datas 的单文件模式。
* ``rf4s`` 用 ``collect_submodules`` 全收：它内部大量按字符串动态导入
  controller/component，漏一个就是运行期 ImportError。
* ``static/`` 与 ``config.yaml`` 放在 ``_internal`` 根：上游 RF4S 用
  ``Path(__file__).parents[2]`` 定位这两个，位置错了要到实机才炸。
* 不放 OCR venv（362MB，且只是可选增强），需要时把它放到 exe 旁边即可。
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

BOT = Path(SPECPATH).resolve()
# 开发仓库里 RF4S 快照在 refs/（不入库）；公开发布仓库里同一份源码叫 vendor/。
REFS = BOT / "refs" / "RussianFishing4Script"
if not REFS.is_dir():
    REFS = BOT / "vendor"
if not (REFS / "rf4s").is_dir():
    raise SystemExit("找不到 RF4S 快照：请在 refs/RussianFishing4Script 或 vendor/ 下准备")
print("[spec] RF4S 快照: %s" % REFS)

hiddenimports = [
    # 我们自己按字符串/惰性导入的模块
    "app_paths",
    "app_main",
    "autofish_bridge",
    "autofish_lifecycle",
    "autofish_ui",
    "autofish_hotkeys",
    "autofish_ipc",
    "autofish_nav",
    "autofish_ocr",
    "autofish_ocr_client",
    "autofish_places",
    "autofish_handoff",
    "bot_ctl",
    "rf4_sniff",
    # 上游 RF4S 的动态导入
    "yacs",
    "yacs.config",
    "omegaconf",
    "pyscreeze",
    "pyautogui",
    "mouseinfo",
    "pygetwindow",
    "pymsgbox",
    "pytweening",
    "discord_webhook",
    "rich",
    "rich.logging",
    "win32timezone",
    # autofish_hotkeys 的兜底后端 / 截图备用后端，按字符串导入
    "keyboard",
    "mss",
    # 协议抓包（--sniff）：scapy 由 PyInstaller hook 收集，这里显式列关键层
    "scapy",
    "scapy.all",
    "scapy.layers.inet",
    "scapy.sendrecv",
    # matplotlib 只被 rf4s 的 timer 用来画统计图，默认 Agg 后端
    "matplotlib.backends.backend_agg",
]
hiddenimports += collect_submodules("rf4s")

datas = [
    # 上游模板（en/ru）+ 中文 ready 模板合并进同一个 static/
    (str(REFS / "static"), "static"),
    (str(BOT / "static"), "static"),
    # 上游配置：rf4s 在冻结态从 OUTER_ROOT（即 _internal）读它
    (str(REFS / "config.yaml"), "."),
]

excludes = [
    "paddleocr",
    "paddle",
    "torch",
    "tensorflow",
    "onnxruntime",
    "pytest",
    "IPython",
    "notebook",
    # tkinter 的测试与演示模块，能省几 MB
    "tkinter.test",
    "matplotlib.tests",
]

a = Analysis(
    [str(BOT / "app_main.py")],
    pathex=[str(BOT), str(REFS)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RF4AutoFish",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 双击不弹黑框；日志一律落文件
    disable_windowed_traceback=False,
    icon=str(BOT / "assets" / "rf4autofish.ico"),
)

if os.environ.get("RF4_ONEFILE") == "1":
    # 单文件：依赖/资源全部塞进 EXE，运行时解到 sys._MEIPASS。
    # app_paths 已经按 _MEIPASS 解析只读资源，datas 的相对位置不用改。
    exe_onefile = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="RF4AutoFish",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        icon=str(BOT / "assets" / "rf4autofish.ico"),
    )

else:
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="RF4AutoFish",
    )
