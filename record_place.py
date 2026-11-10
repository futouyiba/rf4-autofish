"""坐标表 / 策略表命令行。

用法：
    python record_place.py maps                     # 列出所有地图（* = 当前）
    python record_place.py use 蚊子湖                # 切换当前地图
    python record_place.py list                     # 当前地图的目的地
    python record_place.py list --all               # 全部地图
    python record_place.py mark 钓位A --kind fishing --strategy 底钓标准
    python record_place.py mark 鱼市 --kind sell
    python record_place.py delete 钓位A
    python record_place.py dest use 钓位A            # 选当前目的地

    python record_place.py strategy list
    python record_place.py strategy set 底钓标准 method=bottom friction_base=17
    python record_place.py strategy use 底钓标准
    python record_place.py strategy delete 底钓标准

`mark` 会真的去读游戏画面（需要游戏在前台、角色站着不动），读不到就明确报错，
不会写入一个猜出来的坐标。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autofish_places as ap  # noqa: E402


# ----------------------------------------------------------------------
# 读现场坐标
# ----------------------------------------------------------------------
def _read_pose_and_frame(focus: bool = True):
    """读位姿并带回那一帧画面（存截图用）。"""
    import autofish_nav as nav
    import rf4s_adapter as A
    from autofish_ocr_client import OcrClient

    if focus:
        try:
            A.focus_game_quiet()
        except Exception as exc:  # noqa: BLE001
            return None, None, "无法聚焦游戏窗口：%s" % exc, None
        time.sleep(0.4)
    try:
        img, _pos = A.capture_game()
    except Exception as exc:  # noqa: BLE001
        return None, None, "抓图失败：%s" % exc, None

    ocr = OcrClient(timeout=8.0)
    coord, text, score = nav.read_coord_from_frame(ocr, img)
    if coord is None:
        return None, None, "读不到坐标（原始文本 %r，置信度 %.2f）" % (text, score), img

    heading, _marks = nav.read_heading(img)
    detail = "识别 %r（%.2f）" % (text, score)
    if heading is None:
        detail += "；朝向读不到（罗盘标记被挡？），只记坐标"
    else:
        detail += "；朝向 %.1f°" % heading
    return ((int(round(coord[0])), int(round(coord[1]))), heading, detail, img)


def _read_live_pose(focus: bool = True):
    """读当前坐标与朝向；返回 ((x, y), heading, 说明) 或 (None, None, 原因)。

    ``focus=False`` 用于录制模式：抓图走 PrintWindow，不需要把游戏拉到前台，
    这样按热键记点时不会打断玩家已经摆好的视角。
    """
    coord, heading, detail, _frame = _read_pose_and_frame(focus)
    return coord, heading, detail


def _save_shot(frame, map_name: str, name: str) -> str:
    """把记点时的画面缩略图存下来，返回相对 bot 目录的路径（失败返回空串）。

    自动编号的点（点1、点2…）事后要改名，光看坐标数字想不起是哪儿；
    给一张缩略图，`list` 里能直接对照着命名。
    """
    if frame is None:
        return ""
    try:
        root = pathlib.Path(__file__).resolve().parent / "_places" / "shots"
        root.mkdir(parents=True, exist_ok=True)

        def safe(text: str) -> str:
            return "".join(ch if ch not in '\\/:*?"<>|' else "_" for ch in str(text))

        fname = "%s_%s_%s.png" % (time.strftime("%Y%m%d-%H%M%S"),
                                  safe(map_name), safe(name))
        thumb = frame.copy()
        thumb.thumbnail((720, 720))
        thumb.save(root / fname)
        return "_places/shots/" + fname
    except Exception:  # noqa: BLE001 - 截图失败不影响记点
        return ""


def _report_warnings(store: ap.Places) -> None:
    if store.last_error:
        print("（注意：%s）" % store.last_error)
    for warning in store.warnings:
        print("（注意：%s）" % warning)


# ----------------------------------------------------------------------
# 地图
# ----------------------------------------------------------------------
def cmd_maps(args, store: ap.Places) -> int:
    names = store.map_names()
    if not names:
        print("还没有任何地图。用 `mark` 记第一个点时会自动创建。")
        return 0
    for name in names:
        mark = "*" if name == store.active_map else " "
        mp = store.maps[name]
        print("%s %-16s %d 个目的地  %s" % (mark, name, len(mp.places), mp.display))
    return 0


def cmd_use(args, store: ap.Places) -> int:
    store.set_active(args.map)
    store.save()
    print("当前地图 = %s（目的地已按新地图重置）" % store.active_map)
    return 0


# ----------------------------------------------------------------------
# 目的地
# ----------------------------------------------------------------------
def cmd_list(args, store: ap.Places) -> int:
    if getattr(args, "all", False):
        _report_warnings(store)
        if not store.maps:
            print("坐标表还是空的。")
            return 0
        for name in store.map_names():
            print(store.describe(name))
        return 0
    _report_warnings(store)
    if not store.maps:
        print("坐标表还是空的。站到位置后跑：python record_place.py mark 钓位A")
        return 0
    print(store.describe())
    return 0


def cmd_dest_use(args, store: ap.Places) -> int:
    store.set_active_destination(args.name, args.map)
    store.save()
    place = store.require(args.name, args.map)
    print("当前目的地 = %s (%d, %d)" % (place.name, place.x, place.y))
    return 0


def cmd_mark(args, store: ap.Places) -> int:
    coord, heading, detail, frame = _read_pose_and_frame(focus=True)
    if coord is None:
        print("记点失败：%s" % detail, file=sys.stderr)
        print("站到位置、面朝你希望抛竿/交互的方向、别转视角，再跑一次。",
              file=sys.stderr)
        return 2
    used_map = args.map or store.active_map
    shot = _save_shot(frame, used_map, args.name)
    entry = store.put(args.name, coord, map_name=args.map, note=args.note or "",
                      kind=args.kind, strategy=args.strategy or "",
                      heading=heading, shot=shot)
    if args.use:
        store.active_destination = entry.name
    store.save()
    print("已记录 %s/%s = (%d, %d) 朝向=%s  kind=%s%s  %s"
          % (used_map, entry.name, entry.x, entry.y,
             ("%.1f°" % entry.heading) if entry.heading is not None else "未记录",
             entry.kind, ("  策略=" + entry.strategy) if entry.strategy else "",
             detail))
    if shot:
        print("截图：%s" % shot)
    return 0


def cmd_delete(args, store: ap.Places) -> int:
    ok = store.remove(args.name, args.map)
    if not ok:
        print("没有这个点：%s" % args.name, file=sys.stderr)
        return 1
    if store.active_destination == args.name:
        store.active_destination = ""
    store.save()
    print("已删除 %s" % args.name)
    return 0


def cmd_rename(args, store: ap.Places) -> int:
    used_map = args.map or store.active_map
    mp = store.maps.get(used_map)
    if mp is None:
        print("没有这张地图：%s" % used_map, file=sys.stderr)
        return 1
    old = mp.places.get(args.old)
    if old is None:
        print("没有这个点：%s" % args.old, file=sys.stderr)
        return 1
    if args.new in mp.places and args.new != args.old:
        print("目标名字已存在：%s" % args.new, file=sys.stderr)
        return 1
    if args.new == args.old:
        print("名字没变。")
        return 0
    entry = ap.Place(
        name=args.new, x=old.x, y=old.y, note=old.note, marked_at=old.marked_at,
        kind=old.kind, strategy=old.strategy, heading=old.heading,
    )
    mp.places.pop(args.old, None)
    mp.places[args.new] = entry
    if store.active_destination == args.old:
        store.active_destination = args.new
    store.save()
    print("已改名：%s → %s  (%d, %d) 朝向=%s"
          % (args.old, args.new, entry.x, entry.y,
             ("%.1f°" % entry.heading) if entry.heading is not None else "未记录"))
    return 0


# ----------------------------------------------------------------------
# 热键录制
# ----------------------------------------------------------------------
def _next_name(store: ap.Places, map_name: str, prefix: str) -> str:
    mp = store.maps.get(map_name)
    existing = set(mp.places) if mp else set()
    index = 1
    while ("%s%d" % (prefix, index)) in existing:
        index += 1
    return "%s%d" % (prefix, index)


def _read_stable_pose(focus: bool = True, tries: int = 2, gap: float = 0.35):
    """连读两帧，坐标和朝向都稳定才认；返回 (coord, heading, detail, frame)。

    录制时人可能还在走、视角还在转，单帧读到的可能是一个"路过的"位姿。
    两帧差超过 0.5 单位或 3° 就重试，仍不稳就拒绝记录。
    """
    import autofish_nav as nav

    last = (None, None, "读不到位姿", None)
    for _ in range(max(1, tries)):
        first = _read_pose_and_frame(focus=focus)
        if first[0] is None:
            last = first
            continue
        time.sleep(gap)
        second = _read_pose_and_frame(focus=False)
        if second[0] is None:
            last = second
            continue
        moved = nav.distance(first[0], second[0])
        turned = 0.0
        if first[1] is not None and second[1] is not None:
            turned = abs(nav.normalize_deg(second[1] - first[1]))
        if moved <= 0.5 and turned <= 3.0:
            return second
        last = (None, None,
                "画面在动（坐标差 %.1f，朝向差 %.1f°），稳住再按" % (moved, turned),
                None)
    return last


def cmd_record(args, store: ap.Places) -> int:
    """站在游戏里按热键记点：F11 记一个、F12 结束。

    每次按下立即落盘，中途退出也不丢已经记好的点。记点不抢焦点
    （PrintWindow 抓图），所以不会打断已经摆好的视角。
    """
    import queue

    import autofish_hotkeys as hk

    VK_F11, VK_F12 = 0x7A, 0x7B
    bindings = (
        hk.HotkeyBinding("mark", "F11", "Ctrl+Alt+Shift+F11", 0xA711, VK_F11),
        hk.HotkeyBinding("done", "F12", "Ctrl+Alt+Shift+F12", 0xA712, VK_F12),
    )

    # 先确认能抓到画面、能读到坐标，否则录一路空记录
    coord, heading, detail = _read_live_pose(focus=True)
    if coord is None:
        print("开始前自检失败：%s" % detail, file=sys.stderr)
        return 2
    used_map = args.map or store.active_map
    print("地图 = %s，起始位姿 (%d, %d) 朝向 %s"
          % (used_map,
             coord[0], coord[1],
             ("%.1f°" % heading) if heading is not None else "未读到"))
    print("按 Ctrl+Alt+Shift+F11 记一个点，Ctrl+Alt+Shift+F12 结束。")
    print("记点不抢焦点，站好姿势直接按就行。")

    events: "queue.Queue" = queue.Queue()
    listener = hk.HotkeyListener(events, bindings=bindings)
    listener.start()
    if not listener.ready.wait(3.0):
        print("热键监听线程没起来。", file=sys.stderr)
        return 2

    saved = []
    try:
        while True:
            # 监听线程发的是 ("hotkey", HotkeyEvent) / ("hotkeys", report) 元组
            item = events.get()
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            kind, payload = item
            if kind == "hotkeys":
                if not payload.get("enabled", True):
                    print("热键注册失败：%s" % payload.get("reason"), file=sys.stderr)
                    return 2
                print("热键已就绪：F11 记点 / F12 结束")
                continue
            if kind != "hotkey":
                continue
            event = payload
            if getattr(event, "name", None) == "done":
                break
            if getattr(event, "name", None) != "mark":
                continue

            coord, heading, detail, frame = _read_stable_pose(focus=False)
            if coord is None:
                # 后台 PrintWindow 抓不到有效画面时（实测会返回无文字的黑帧），
                # 把游戏拉回前台再读一次；只激活窗口、不动鼠标。
                coord, heading, detail, frame = _read_stable_pose(focus=True)
                if coord is not None:
                    detail = "（拉前台后读到）" + detail
            if coord is None:
                print("  ✗ 记点失败：%s" % detail, file=sys.stderr)
                continue
            name = _next_name(store, used_map, args.prefix)
            shot = _save_shot(frame, used_map, name)
            entry = store.put(name, coord, map_name=used_map,
                              kind=args.kind, heading=heading, shot=shot)
            store.save()
            saved.append(name)
            print("  #%d %s = (%d, %d) 朝向=%s  %s"
                  % (len(saved), name, entry.x, entry.y,
                     ("%.1f°" % entry.heading) if entry.heading is not None
                     else "未记录", detail))
            if shot:
                print("      截图 %s" % shot)
    except KeyboardInterrupt:
        print("\n收到中断，保存已记的点。")
    finally:
        listener.stop()

    if saved:
        print("\n本轮到 %s 地图记了 %d 个点：%s"
              % (used_map, len(saved), "、".join(saved)))
        print("改名：python record_place.py rename %s 钓位A" % saved[0])
    else:
        print("\n没有记录任何点。")
    return 0


# ----------------------------------------------------------------------
# 策略
# ----------------------------------------------------------------------
def _parse_value(text: str):
    low = text.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def cmd_strategy_list(args, store: ap.Places) -> int:
    _report_warnings(store)
    print(store.describe_strategies())
    return 0


def cmd_strategy_set(args, store: ap.Places) -> int:
    fields = {}
    for item in args.pairs:
        if "=" not in item:
            print("参数格式应为 key=value：%r" % item, file=sys.stderr)
            return 2
        key, value = item.split("=", 1)
        fields[key.strip()] = _parse_value(value)
    try:
        entry = store.put_strategy(args.name, **fields)
    except ap.PlacesError as exc:
        print("设置失败：%s" % exc, file=sys.stderr)
        return 2
    store.save()
    print("策略 %s 已保存：%s" % (entry.name, json.dumps(entry.as_dict(), ensure_ascii=False)))
    return 0


def cmd_strategy_use(args, store: ap.Places) -> int:
    try:
        store.set_active_strategy(args.name)
    except ap.PlacesError as exc:
        print("切换失败：%s" % exc, file=sys.stderr)
        return 2
    store.save()
    print("当前策略 = %s" % store.active_strategy)
    return 0


def cmd_strategy_delete(args, store: ap.Places) -> int:
    if not store.remove_strategy(args.name):
        print("没有这个策略：%s" % args.name, file=sys.stderr)
        return 1
    if store.active_strategy == args.name:
        store.active_strategy = ""
    store.save()
    print("已删除策略 %s" % args.name)
    return 0


# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="分地图目的地 + 钓鱼策略")
    p.add_argument("--file", default=None, help="配置文件路径（默认 bot/autofish_places.json）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("maps", help="列出所有地图").set_defaults(func=cmd_maps)

    use = sub.add_parser("use", help="切换当前地图")
    use.add_argument("map")
    use.set_defaults(func=cmd_use)

    ls = sub.add_parser("list", help="列出目的地")
    ls.add_argument("--all", action="store_true")
    ls.add_argument("--map", default=None)
    ls.set_defaults(func=cmd_list)

    mk = sub.add_parser("mark", help="读当前游戏坐标并记为目的地")
    mk.add_argument("name")
    mk.add_argument("--map", default=None)
    mk.add_argument("--note", default="")
    mk.add_argument("--kind", default=ap.KIND_FISHING, choices=ap.KINDS)
    mk.add_argument("--strategy", default="", help="绑定的策略名")
    mk.add_argument("--use", action="store_true", help="同时设为当前目的地")
    mk.set_defaults(func=cmd_mark)

    dl = sub.add_parser("delete", help="删除一个目的地")
    dl.add_argument("name")
    dl.add_argument("--map", default=None)
    dl.set_defaults(func=cmd_delete)

    rn = sub.add_parser("rename", help="给目的地改名")
    rn.add_argument("old")
    rn.add_argument("new")
    rn.add_argument("--map", default=None)
    rn.set_defaults(func=cmd_rename)

    rec = sub.add_parser("record", help="热键录制：F11 记一个点，F12 结束")
    rec.add_argument("--map", default=None)
    rec.add_argument("--prefix", default="点", help="自动命名前缀，默认「点」")
    rec.add_argument("--kind", default=ap.KIND_FISHING, choices=ap.KINDS)
    rec.set_defaults(func=cmd_record)

    dest = sub.add_parser("dest", help="目的地操作")
    dest_sub = dest.add_subparsers(dest="dest_cmd", required=True)
    du = dest_sub.add_parser("use", help="选当前目的地")
    du.add_argument("name")
    du.add_argument("--map", default=None)
    du.set_defaults(func=cmd_dest_use)

    st = sub.add_parser("strategy", help="策略操作")
    st_sub = st.add_subparsers(dest="st_cmd", required=True)
    st_sub.add_parser("list", help="列出策略").set_defaults(func=cmd_strategy_list)
    sts = st_sub.add_parser("set", help="新建/修改策略（key=value）")
    sts.add_argument("name")
    sts.add_argument("pairs", nargs="+")
    sts.set_defaults(func=cmd_strategy_set)
    stu = st_sub.add_parser("use", help="切换当前策略")
    stu.add_argument("name")
    stu.set_defaults(func=cmd_strategy_use)
    std = st_sub.add_parser("delete", help="删除策略")
    std.add_argument("name")
    std.set_defaults(func=cmd_strategy_delete)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    store = ap.Places(args.file)
    return args.func(args, store)


if __name__ == "__main__":
    raise SystemExit(main())
