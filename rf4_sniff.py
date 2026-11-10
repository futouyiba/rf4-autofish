"""常驻抓包器 + 协议状态机：实时解码 RF4 私服钓鱼事件，维护半途接手所需的状态快照。

## 为什么需要它

视觉判据有**歧义**：圆环读数同时对应好几种状态。实测同一个 0.983
既可能是"刚短抛出去"，也可能是"鱼被拉到近岸"；0.975 既可能是"入护收回"，
也可能是"大鱼贴着岸边"。用这种连续量去区分 5 个状态，怎么调阈值都是错的
（2026-11-09 为此连修四个 bug）。

协议不一样——游戏自己会明确说：

| 类型 | RPC | 方向 | 含义 |
|---|---|---|---|
| `0e01` | fishing/cast_prepare | C2S | 按下蓄力 |
| `0e02` | fishing/cast | C2S | **真正抛出**（含落点 x/z） |
| `0e0e` | fishing/bite | S2C | **咬钩**（带鱼种名） |
| `0e0b` | fishing/hooked | C2S | 中鱼成立 |
| `0e04` | fishing/end | C2S | 战斗结束（空竿/断线也发，**不能当上鱼**） |
| `0e05` | fishing/keep_fish | C2S | **只有真上鱼才发** |
| `0e0c` | fishing/left | C2S | 鱼跑了 |
| `0e07` | fishing/step | C2S | **竿在水里时 1 Hz**（持续态） |
| `0e08` | fishing/load | C2S | **搏斗中每 2 秒**，含受力 + 放线长度（持续态） |

## 半途接手：边沿 vs 持续

边沿事件（0e01/0e02/0e0b/0e04/0e05/0e0c）错过就没了；**持续态信号可以事后重建**：

| 观测到 | 判定 |
|---|---|
| `0e07` 在流（≤2s 内有）+ `0e0b` 晚于最后 `0e02` | **fighting**（`0e08` 在流则置信更高） |
| `0e07` 在流 + 无 `0e0b` | **line_in_water**（等口） |
| 有 `0e01` 无 `0e02` | **casting**（蓄力中，键可能还按着） |
| `0e07` 停 + 最后边沿是 `0e04` | **ended**（上鱼与否看 `0e05`） |
| `0e07` 停 + 最后边沿是 `0e05` | **kept**（已上鱼） |
| `0e07` 停 + 最后边沿是 `0e0c` | **fish_left**（鱼跑了） |
| 什么都没有 | **unknown** → bot 不许猜，停机交人工 |

关键不是"有没有收到过"，而是 **多久没收到了**（`beat` 年龄）。

## 关键限制：必须从握手开始抓

RC4 是流密码，解第 N 个字节必须先从第 0 个字节走过。所以**中途接入的连接
解不出来**——本进程必须在游戏建立连接之前就在跑。

判据：C2S 流以 ``\\x01\\x00 | keylen:4 | token`` 开头，S2C 以 ``DAB12FBC`` 开头。
看不到握手的连接会被标成 `no_handshake` 并跳过，不会瞎解。

## 产物

* stdout：每个事件一行（带方向、类型、序号）
* ``bot/_sniff/events.jsonl``：append-only 事件流（含 0e08 的 float 字段），供事后对齐/回放
* ``bot/_sniff/state.json``：**状态快照**，每事件更新、原子写。
  bot 启动第一件事读它，按 phase 决定接手动作。与 bot 是否在跑无关，抓包器独立维护。

## 0e08 的 float 解析（搏鱼/物理分析用）

按 FISHING-RPC-MAP.md §10.2（DTO21，27 个 float；偏移待实捕校准）：

| 索引 | 含义 |
|---|---|
| 3 | 本场搏斗已经过秒数 |
| 9 | 力/速度类变化量 |
| 19 | 当前放线长度 |
| 20 | 与 0e02 的 line_out_length 同值 |
| 25 | 会话时钟（秒） |

## 用法

    python rf4_sniff.py              # 常驻，Ctrl+C 退出
    python rf4_sniff.py --quiet      # 控制台只打关键事件；jsonl 全量落盘不受影响

启动顺序铁律：**先起本进程，再进游戏**（拿握手 token）。
autofish-start 流程已把它挂在"启动登录器之前"。
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from decode_rf4 import RC4  # noqa: E402

PORT = 19213
OUT_DIR = os.path.join(HERE, "_sniff")

# 表来自 bot/AUTOFISH-HANDOFF.md §2（全部实测过）。
EVENT_NAMES = {
    0x0E01: "cast_prepare",
    0x0E02: "cast",
    0x0E03: "fall",
    0x0E04: "end",
    0x0E05: "keep_fish",
    0x0E07: "step",
    0x0E08: "load",
    0x0E09: "pull",
    0x0E0B: "hooked",
    0x0E0C: "left",
    0x0E0E: "bite",
}
# 判状态真正用得上的几个；step/load 高频但**必须落盘**（持续态信号）。
KEY_EVENTS = {0x0E02, 0x0E0E, 0x0E0B, 0x0E05, 0x0E04, 0x0E0C}

MAX_FRAME = 10 * 1024 * 1024

# 持续态判据（秒）：超过这个年龄视为"该信号已停"。
# 0e07 实测 1.0005 s/条，留 2 倍余量；0e08 实测 2.000 s/条。
STEP_STALE = 2.0
LOAD_STALE = 3.0

# 0e08 DTO21 float 布局（FISHING-RPC-MAP.md §10.2，待实捕校准）：
# `... type:2 | "524" | 03 00 | 0c <GUID> | 01 15 00 00 00 | 27×float`
LOAD_FLOATS = 27
LOAD_DTO_MAGIC = b"\x01\x15\x00\x00\x00"   # tag 0x01 + DTO id 21 (u32 LE)


def _u32(buf, off=0):
    return struct.unpack_from("<I", buf, off)[0]


def parse_load_floats(plain: bytes):
    """从一条 0e08 明文里解出关键 float。

    GUID 长度不固定，所以全文找 DTO21 魔数，取其后 27 个 float。
    返回 dict 或 None（没找到魔数 / 长度不足）。
    """
    idx = plain.find(LOAD_DTO_MAGIC)
    if idx < 0:
        return None
    start = idx + len(LOAD_DTO_MAGIC)
    need = LOAD_FLOATS * 4
    if len(plain) < start + need:
        return None
    vals = struct.unpack_from("<%df" % LOAD_FLOATS, plain, start)
    return {
        "bout_elapsed": vals[3],
        "load_var": vals[9],
        "line_out": vals[19],
        "line_out_at_cast": vals[20],
        "session_clock": vals[25],
    }


class Direction:
    """一个方向的字节流：先认握手，再切帧、解密。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.buf = bytearray()
        self.rc4 = None
        self.ready = False
        self.dead = False
        self.reason = ""
        self.token = None

    # -- 握手 ---------------------------------------------------------
    def try_handshake(self, shared: dict) -> None:
        if self.ready or self.dead:
            return
        if self.name == "C2S":
            if len(self.buf) < 6:
                return
            if self.buf[0:2] != b"\x01\x00":
                self._die("c2s_bad_magic")
                return
            keylen = _u32(self.buf, 2)
            if keylen <= 0 or keylen > 4096:
                self._die("c2s_bad_keylen")
                return
            if len(self.buf) < 6 + keylen + 4:
                return
            self.token = bytes(self.buf[6:6 + keylen])
            rest = self.buf[6 + keylen:]
            xml_len = _u32(rest, 0)
            if len(rest) < 4 + xml_len:
                return
            self.buf = bytearray(rest[4 + xml_len:])
            self.rc4 = RC4(self.token)
            self.ready = True
            shared["token"] = self.token
        else:
            if len(self.buf) < 44:
                return
            if self.buf[0:8] != b"DAB12FBC":
                self._die("s2c_bad_magic")
                return
            if not shared.get("token"):
                return          # 等 C2S 先给出 token
            self.buf = bytearray(self.buf[36:])
            self.rc4 = RC4(shared["token"])
            self.ready = True

    def _die(self, reason: str) -> None:
        self.dead = True
        self.reason = reason
        self.buf.clear()

    # -- 切帧 ---------------------------------------------------------
    def drain(self, emit) -> None:
        if not self.ready:
            return
        while len(self.buf) >= 4:
            length = _u32(self.buf, 0)
            if length < 9 or length > MAX_FRAME:
                self._die("bad_frame_len:%d" % length)
                return
            if len(self.buf) < 4 + length:
                return
            payload = bytes(self.buf[4:4 + length])
            del self.buf[:4 + length]
            flag = payload[0]
            seq = _u32(payload, 1)
            if flag != 0:
                continue
            plain = self.rc4.process(payload[9:])
            if len(plain) < 11:
                continue
            rpc = (plain[9] << 8) | plain[10]
            emit(self.name, rpc, seq, plain)


class ProtocolState:
    """协议侧的钓鱼状态机：每次事件后更新，phase 按需重算。

    与 bot 解耦：不 import 任何 bot 模块，bot 来读 state.json。
    半途接手时 bot 只需要 snapshot 里的 phase + beat 年龄。
    """

    def __init__(self) -> None:
        self.last_edge = {}       # 边沿事件名 -> 最后时刻
        self.last_step = None     # 0e07 最后到达时刻（持续态）
        self.last_load = None     # 0e08 最后到达时刻（持续态）
        self.bout_elapsed = None  # 最近 0e08 的搏斗秒数
        self.line_out = None      # 最近 0e08 的放线长度
        self.last_event_ts = None  # 任意钓鱼事件最后到达时刻（用于陈旧判定）
        self.events_total = 0

    def on_event(self, name: str, t: float, plain: bytes) -> None:
        self.events_total += 1
        # 任意钓鱼事件（含 step/load 持续态）都算"连接还活着"的证据。
        # 2026-11-09 修：原来只在边沿事件里更新，搏斗 60s 后（0e07 停了、
        # 只有 0e08 每 2s 一条）会被 handoff 误判 sniff_stale_events 降级。
        self.last_event_ts = t
        if name == "step":
            self.last_step = t
            return
        if name == "load":
            self.last_load = t
            floats = parse_load_floats(plain)
            if floats:
                self.bout_elapsed = round(floats["bout_elapsed"], 3)
                self.line_out = round(floats["line_out"], 3)
            return
        if name == "pull":
            return                      # 高频，不参与状态判定
        self.last_edge[name] = t
        if name == "cast":
            # 新一竿：清上一竿的边沿残留（bite/hooked/end/left/keep_fish）
            for stale in ("bite", "hooked", "end", "left", "keep_fish",
                          "cast_prepare"):
                self.last_edge.pop(stale, None)
            self.last_step = None
            self.last_load = None
            self.bout_elapsed = None
            self.line_out = None
        elif name == "hooked":
            self.last_edge.pop("bite", None)

    def phase(self, now: float) -> str:
        """根据"持续态年龄 + 最后边沿"判定当前阶段。"""
        step_alive = self.last_step is not None and (now - self.last_step) <= STEP_STALE
        load_alive = self.last_load is not None and (now - self.last_load) <= LOAD_STALE
        hooked_t = self.last_edge.get("hooked")
        cast_t = self.last_edge.get("cast")
        # 终止边沿（end / keep_fish / left）晚于 hooked 时，即使 0e08 还在
        # LOAD_STALE 窗口内也不能算搏斗——收鱼后 0e05 会比 bot 点击晚到，
        # 残留的"搏斗"会让状态机立刻重新开战（2026-11-09 实机踩过）。
        terminal = [t for n, t in self.last_edge.items()
                    if n in ("end", "keep_fish", "left")]
        terminated = bool(terminal) and (hooked_t is None or max(terminal) > hooked_t)

        if (step_alive or load_alive) and not terminated:
            if hooked_t is not None and (cast_t is None or hooked_t > cast_t):
                return "fighting"
            return "line_in_water"

        # 持续态都停了：看最后边沿
        if not self.last_edge:
            return "unknown"
        latest_t, latest = max((t, n) for n, t in self.last_edge.items())
        if latest == "cast_prepare":
            # 有 0e01 但没有紧随的 0e02 → 还在蓄力（或卡住）
            if cast_t is None or latest_t > cast_t:
                return "casting"
            return "line_in_water"
        if latest == "keep_fish":
            return "kept"
        if latest == "left":
            return "fish_left"
        if latest == "end":
            return "ended"
        if latest == "hooked":
            # 0e0b 到了但持续态刚好超时（网络抖动）：短暂宽限
            return "fighting" if (now - latest_t) <= LOAD_STALE * 2 else "ended"
        if latest in ("cast", "fall", "bite"):
            return "line_in_water"
        return "unknown"

    def snapshot(self, now: float, conn_desc: str = "", ready: bool = False) -> dict:
        return {
            "ts": round(now, 3),
            "conn": conn_desc,
            "ready": ready,
            "phase": self.phase(now),
            "last_edge": {n: round(t, 3) for n, t in sorted(self.last_edge.items())},
            "beat": {
                "step_age": None if self.last_step is None
                else round(now - self.last_step, 2),
                "load_age": None if self.last_load is None
                else round(now - self.last_load, 2),
            },
            "bout_elapsed": self.bout_elapsed,
            "line_out": self.line_out,
            "event_age": None if self.last_event_ts is None
            else round(now - self.last_event_ts, 1),
            "events_total": self.events_total,
        }


class Connection:
    def __init__(self, key: tuple) -> None:
        self.key = key
        self.shared: dict = {}
        self.c2s = Direction("C2S")
        self.s2c = Direction("S2C")
        self.created = time.time()
        self.events = 0
        self.state = ProtocolState()

    def feed(self, direction: str, data: bytes, emit, log) -> None:
        side = self.c2s if direction == "C2S" else self.s2c
        if side.dead:
            return
        first = len(side.buf) == 0 and not side.ready
        side.buf += data
        if first:
            # 诊断：没握手就抓到的连接长什么样。若首包不是 \x01\x00 / DAB12FBC，
            # 说明连接在我们启动之前就建立了——整条连接不可解，只能等重建。
            log("[sniff] %s 首包 %s len=%d"
                % (direction, data[:24].hex(), len(data)))
        side.try_handshake(self.shared)
        if side.dead:
            log("[sniff] %s %s 放弃：%s" % (self.key, direction, side.reason))
            return
        if side.ready:
            side.drain(emit)

    @property
    def ready(self) -> bool:
        # 所有状态判定都基于 C2S（客户端自己发的 0e0x）。S2C 只用于 bite
        # 预判（可选），实测 s2c_bad_magic 一直解不开，但不影响主链路。
        # 所以 ready = C2S 解密就绪，而不是“双向都就绪”。
        return self.c2s.ready


def _atomic_write(path: str, payload: dict) -> None:
    """原子写快照。bot 会高频读这个文件；Windows 上 os.replace 偶尔会撞上
    读取方的共享锁（PermissionError），重试几次即可，**绝不能让这里抛出去**——
    一抛就打断 scapy 的抓包线程（2026-11-09 实测过：bot 开始轮询后抓包器静默死掉）。
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    for attempt in range(4):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            if attempt == 3:
                return          # 放弃这次快照，不影响抓包主循环
            time.sleep(0.02)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="RF4 协议实时抓包器 + 状态机")
    parser.add_argument("--quiet", action="store_true",
                        help="控制台只打关键事件；jsonl 全量落盘不受影响")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--out", default=os.path.join(OUT_DIR, "events.jsonl"))
    parser.add_argument("--state", default=os.path.join(OUT_DIR, "state.json"))
    parser.add_argument("--pcap", default="", help="同时落一份 pcap 便于事后复核")
    args = parser.parse_args(argv)

    try:
        from scapy.all import IP, TCP, AsyncSniffer, wrpcap  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print("需要 scapy + Npcap：%s" % exc, file=sys.stderr)
        return 2

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    out = open(args.out, "a", encoding="utf-8")
    state_path = args.state
    state = ProtocolState()

    conns: dict = {}
    stats = {"packets": 0, "events": 0}

    def log(text: str) -> None:
        print(text, flush=True)

    def _conn_desc() -> str:
        latest = max(conns.values(), key=lambda c: c.created, default=None)
        if latest is None:
            return ""
        return "%s:%d→%s:%d" % (latest.key[0][0], latest.key[0][1],
                                latest.key[1][0], latest.key[1][1])

    def _ready_flag() -> bool:
        latest = max(conns.values(), key=lambda c: c.created, default=None)
        return bool(latest and latest.ready)

    def emit(direction: str, rpc: int, seq: int, plain: bytes) -> None:
        name = EVENT_NAMES.get(rpc)
        if name is None:
            return
        stats["events"] += 1
        now = time.time()
        # ---- 全量落盘（不受 quiet 影响）----
        record = {
            "t": round(now, 3),
            "dir": direction,
            "name": name,
            "rpc": "0x%04x" % rpc,
            "seq": seq,
            "len": len(plain),
        }
        if rpc == 0x0E08:
            floats = parse_load_floats(plain)
            if floats:
                record["f"] = {k: round(v, 3) for k, v in floats.items()}
        record["head"] = plain[:24].hex()
        out.write(json.dumps(record, ensure_ascii=False) + "\n")
        out.flush()
        # ---- 状态机更新 + 快照 ----
        state.on_event(name, now, plain)
        try:
            _atomic_write(state_path, state.snapshot(
                now, _conn_desc(), _ready_flag()))
        except Exception:  # noqa: BLE001 - 快照失败不能影响抓包
            pass
        # ---- 控制台 ----
        if args.quiet and rpc not in KEY_EVENTS and rpc != 0x0E08:
            return
        mark = "★" if rpc in KEY_EVENTS else " "
        extra = ""
        if rpc == 0x0E08 and "f" in record:
            extra = " bout=%.1fs line=%.2fm" % (
                record["f"]["bout_elapsed"], record["f"]["line_out"])
        print("%s %-7s %-13s seq=%d len=%d%s"
              % (mark, direction, name, seq, len(plain), extra), flush=True)

    def handle(pkt) -> None:
        try:
            _handle(pkt)
        except Exception as exc:  # noqa: BLE001 - 单包异常绝不能停掉抓包
            stats["errors"] = stats.get("errors", 0) + 1
            if stats["errors"] <= 5:
                log("[sniff] 包处理异常（已忽略）：%s: %s"
                    % (type(exc).__name__, exc))

    def _handle(pkt) -> None:
        if IP not in pkt or TCP not in pkt:
            return
        tcp = pkt[TCP]
        payload = bytes(tcp.payload)
        if not payload:
            return
        dst_is_game = tcp.dport == args.port
        src_is_game = tcp.sport == args.port
        if not (dst_is_game or src_is_game):
            return
        stats["packets"] += 1
        a = (pkt[IP].src, tcp.sport)
        b = (pkt[IP].dst, tcp.dport)
        key = (a, b) if dst_is_game else (b, a)
        conn = conns.get(key)
        if conn is None:
            conn = Connection(key)
            conns[key] = conn
            log("[sniff] 新连接 %s:%d → %s:%d"
                % (key[0][0], key[0][1], key[1][0], key[1][1]))
        conn.feed("C2S" if dst_is_game else "S2C", payload, emit, log)

    sniffer = AsyncSniffer(filter="tcp port %d" % args.port, prn=handle,
                           store=False)
    log("[sniff] 开始抓 tcp port %d，事件 → %s，状态 → %s"
        % (args.port, args.out, state_path))
    log("[sniff] 铁律：本进程必须先于游戏连接启动，否则整条连接不可解")
    sniffer.start()
    try:
        while True:
            time.sleep(5.0)
            if conns:
                ready = sum(1 for c in conns.values() if c.ready)
                alive = sum(1 for c in conns.values() if not c.c2s.dead)
                now = time.time()
                # 周期性刷快照（phase 可能因持续态超时而变化，与事件无关）
                try:
                    _atomic_write(state_path, state.snapshot(
                        now, _conn_desc(), _ready_flag()))
                except Exception:  # noqa: BLE001
                    pass
                snap = state.snapshot(now, _conn_desc(), _ready_flag())
                step_age = snap["beat"]["step_age"]
                load_age = snap["beat"]["load_age"]
                log("[sniff] 连接 %d（解密 %d / 存活 %d）事件 %d | phase=%s "
                    "beat(step=%s load=%s)"
                    % (len(conns), ready, alive, stats["events"], snap["phase"],
                       "-" if step_age is None else "%.1fs" % step_age,
                       "-" if load_age is None else "%.1fs" % load_age))
    except KeyboardInterrupt:
        log("\n[sniff] 收工：收包 %d，事件 %d" % (stats["packets"], stats["events"]))
    finally:
        try:
            sniffer.stop()
        except Exception:  # noqa: BLE001
            pass
        out.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
