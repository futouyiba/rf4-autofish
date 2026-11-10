# -*- coding: utf-8 -*-
"""打包后的唯一入口：同一个 exe，按参数切换角色。

角色
----
* （无参数 / ``--ui``）  右侧吸附控制面板（``autofish_ui``）
* ``--bot``             钓鱼主循环（``autofish_bridge``），由面板经 bot_ctl 拉起
* ``--ctl <action>``    ``bot_ctl`` 的命令行动作（start/stop/status/...）
* ``--selftest``        打包自检：只导入、不碰游戏
* ``--version``         版本号

为什么不用多个 exe
------------------
面板要拉起后台、后台要能被 bot_ctl 停掉，三者共享 pid/endpoint/日志。
同一个二进制 + 参数分发最省事：``sys.executable`` 永远是它自己，
``multiprocessing`` 的 fork 子进程也会回到这里（靠 ``freeze_support``）。
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional


def _force_utf8_stdio() -> None:
    """打包后 stdout 可能是 None（--noconsole）；日志写入前先兜住。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass


def _run_panel(argv: List[str]) -> int:
    import autofish_ui  # noqa: PLC0415 - 惰性导入，selftest/bot 模式不需要 tk

    return autofish_ui.main(argv)


def _run_bot(argv: List[str]) -> int:
    import autofish_bridge  # noqa: PLC0415

    return autofish_bridge.main(argv)


def _run_ctl(argv: List[str]) -> int:
    import bot_ctl  # noqa: PLC0415

    return bot_ctl.main(argv)


def _selftest() -> int:
    """打包自检：证明关键依赖都在，且路径解析正确。不启动任何游戏动作。"""
    import platform  # noqa: PLC0415

    import app_paths  # noqa: PLC0415

    report = app_paths.describe()
    # 开发态 rf4s 包不在 sys.path 上（只有 rf4s_adapter 导入时才插入），
    # selftest 要先把它加进去，否则会误报打包缺 rf4s。
    sys.path.insert(0, report["ref_dir"])

    lines: List[str] = []

    def say(text: str = "") -> None:
        lines.append(text)
        print(text)

    def dump() -> None:
        """windowed 打包没有控制台，报告必须落盘才看得见。"""
        try:
            target = app_paths.data_path("selftest.txt")
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    say("python      : %s" % platform.python_version())
    say("frozen      : %s" % report["frozen"])
    say("executable  : %s" % report["executable"])
    say("bundle_dir  : %s" % report["bundle_dir"])
    say("data_dir    : %s" % report["data_dir"])
    say("ref_dir     : %s" % report["ref_dir"])

    failures: List[str] = []
    checks = [
        ("numpy", "numpy"),
        ("cv2", "cv2"),
        ("PIL", "PIL.Image"),
        ("win32gui", "win32gui"),
        ("pydirectinput", "pydirectinput"),
        ("keyboard", "keyboard"),
        ("tkinter", "tkinter"),
        ("rf4s", "rf4s"),
        ("local: autofish_bridge", "autofish_bridge"),
        ("local: autofish_lifecycle", "autofish_lifecycle"),
        ("local: autofish_ui", "autofish_ui"),
        ("local: bot_ctl", "bot_ctl"),
        ("local: rf4_sniff", "rf4_sniff"),
        ("local: autofish_nav", "autofish_nav"),
        ("local: autofish_ocr_client", "autofish_ocr_client"),
    ]
    for label, module in checks:
        try:
            __import__(module)
        except Exception as exc:  # noqa: BLE001
            failures.append("%s -> %s: %s" % (label, type(exc).__name__, exc))
        else:
            say("import ok   : %s" % label)

    # 资源：上游 RF4S 用 parents[2] 定位 static，位置错了会在运行期才炸。
    for rel in ("config.yaml", os.path.join("static", "en")):
        target = os.path.join(report["ref_dir"], rel)
        if os.path.exists(target):
            say("resource ok : %s" % rel)
        else:
            failures.append("missing resource: %s" % target)
    # 中文 readiness 模板是我们自己的资源：开发态在 bot/static/，
    # 打包态在 _internal/static/，用 resource_path 统一探。
    ready_tpl = app_paths.resource_path("static", "ready_cn.png")
    if ready_tpl.is_file():
        say("resource ok : static/ready_cn.png")
    else:
        failures.append("missing resource: %s" % ready_tpl)

    if failures:
        say("\nSELFTEST FAILED")
        for item in failures:
            say("  - %s" % item)
        dump()
        return 1
    say("\nSELFTEST OK")
    dump()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # PyInstaller + multiprocessing：子进程会带着 --multiprocessing-fork 回来。
    try:
        import multiprocessing  # noqa: PLC0415

        multiprocessing.freeze_support()
    except Exception:  # noqa: BLE001 - 没装/不可用时忽略
        pass

    _force_utf8_stdio()

    if not args:
        return _run_panel([])
    head = args[0]
    rest = args[1:]
    if head in ("--version", "-V"):
        import app_paths  # noqa: PLC0415

        print(app_paths.APP_NAME, app_paths.APP_VERSION)
        return 0
    if head == "--selftest":
        return _selftest()
    if head == "--bot":
        return _run_bot(rest)
    if head == "--ctl":
        return _run_ctl(rest)
    if head in ("--ui", "-u"):
        return _run_panel(rest)
    if head in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    # 未识别的参数交给面板（保持 ``exe --demo`` 这类开发用法可用）。
    return _run_panel(args)


if __name__ == "__main__":
    raise SystemExit(main())
