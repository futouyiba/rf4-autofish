# -*- coding: utf-8 -*-
"""运行路径解析：开发态（源码目录）与冻结态（PyInstaller 打包）统一入口。

为什么要单独一层
----------------
打包成 exe 之后有三类路径会变味，混在业务代码里改很容易漏：

* **可执行入口**：开发时是 ``python xxx.py``，冻结后是 ``sys.executable``
  本身（同一个 exe 用 ``--bot`` / ``--ctl`` 切换角色）。
* **只读资源**（``static/``、``config.yaml``、``rf4s`` 包）：onefile 在
  ``sys._MEIPASS``、onedir 在 exe 同级的 ``_internal/``。上游 RF4S 用
  ``Path(__file__).parents[2]`` 找 ``static``，所以这些资源必须放在
  ``_internal/`` 根上。
* **可写数据**（pid、log、endpoint、config.yaml 的写回副本）：绝不能落在
  Program Files 里。冻结态统一放到 ``%LOCALAPPDATA%\\RF4AutoFish``。

所有模块只从这里取路径，不在别处再拼 ``__file__``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "RF4AutoFish"
APP_VERSION = "1.0.0"


def is_frozen() -> bool:
    """当前是否是打包后的进程（PyInstaller onefile/onedir 都为 True）。"""
    return bool(getattr(sys, "frozen", False))


def _exe_path() -> Path:
    return Path(sys.executable).resolve()


def _bundle_dir() -> Path:
    """只读资源根：onefile 解包临时目录 / onedir 的 ``_internal``。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return _exe_path().parent
    return Path(__file__).resolve().parent


#: exe 所在目录（开发态 = bot/ 源码目录）。
APP_DIR: Path = _exe_path().parent if is_frozen() else Path(__file__).resolve().parent
#: 只读资源根目录。
BUNDLE_DIR: Path = _bundle_dir()


def _ensure(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def data_dir() -> Path:
    """可写目录（pid / 日志 / endpoint / 运行期配置）。

    优先级：``RF4_DATA_DIR`` 环境变量 > 冻结态 ``%LOCALAPPDATA%\\RF4AutoFish``
    > 开发态源码目录。这样测试可以用环境变量把脏数据写到临时目录。
    """
    override = os.environ.get("RF4_DATA_DIR")
    if override:
        return _ensure(Path(override).expanduser())
    if is_frozen():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return _ensure(Path(base) / APP_NAME)
    return _ensure(APP_DIR)


DATA_DIR: Path = data_dir()


def ref_dir() -> Path:
    """RF4S 源码/资源目录（``rf4s`` 包 + ``static`` + ``config.yaml``）。

    冻结态返回 ``BUNDLE_DIR``：PyInstaller 把 ``rf4s`` 打成模块、把
    ``static`` 与 ``config.yaml`` 放到 ``_internal`` 根，正好符合上游
    ``Path(__file__).parents[2]`` 的假设。
    """
    override = os.environ.get("RF4_REF_DIR") or os.environ.get("RF4S_REF")
    if override:
        return Path(override).expanduser()
    if is_frozen():
        return BUNDLE_DIR
    # 开发态两种目录布局都支持：私仓用 refs/（git clone），发布仓用 vendor/。
    for candidate in (APP_DIR / "refs" / "RussianFishing4Script", APP_DIR / "vendor"):
        if (candidate / "rf4s").is_dir():
            return candidate
    return APP_DIR / "refs" / "RussianFishing4Script"


def resource_path(*parts: str) -> Path:
    """只读资源：优先打包目录，其次源码目录（同名文件）。"""
    for base in (BUNDLE_DIR, APP_DIR):
        target = base.joinpath(*parts)
        if target.exists():
            return target
    return BUNDLE_DIR.joinpath(*parts)


def data_path(*parts: str) -> Path:
    return DATA_DIR.joinpath(*parts)


def describe() -> dict:
    """诊断用：打印当前解析结果（selftest 会输出这些）。"""
    return {
        "version": APP_VERSION,
        "frozen": is_frozen(),
        "executable": str(_exe_path()),
        "app_dir": str(APP_DIR),
        "bundle_dir": str(BUNDLE_DIR),
        "data_dir": str(DATA_DIR),
        "ref_dir": str(ref_dir()),
    }
