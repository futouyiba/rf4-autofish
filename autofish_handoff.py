"""半途接手：bot 启动/重启时，从抓包器的状态快照决定从哪个状态进入。

## 为什么需要它

协议状态机（rf4_sniff.ProtocolState）独立于 bot 运行，把 phase 写进
``bot/_sniff/state.json``。bot 起来后第一件事读它：

* phase 明确 → 按 phase 对应的状态进入，**不重置按键**（fighting 中松键会跑鱼）；
* phase=unknown / 快照过期 / 抓包器没跑 → 走原有视觉路径（PROBING 起），
  并在状态里给出原因，便于诊断。

## 接手动作表（与 rf4_sniff 的 phase 一一对应）

| phase | 进入状态 | 按键处理 | 理由 |
|---|---|---|---|
| fighting | FIGHT | **不动键**（不 release） | 搏斗中松 F5/F6 会跑鱼；摩擦档位靠力度条自收敛 |
| line_in_water | WAIT_BITE | 松 F5/F6（等口不需要按键） | 0e07 在流 = 竿在水里 |
| casting | PROBING | **按住 F5 短暂后松开** | 蓄力中键可能还按着；先短按再松确保抛出，不无限蓄力 |
| ended | PROBING | 全松 | 战斗结束（空竿/断线/已上鱼分辨不了时走视觉判据） |
| kept | PROBING（带 keep 链式许可） | 全松 | 0e05 确认上鱼，等价于"入护完成" |
| fish_left | PROBING | 全松 | 鱼跑了，重新抛 |
| unknown | PROBING | 全松 + 记原因 | **不许猜**：视觉判据兜底，但明确记录 handoff_unknown |

## 快照新鲜度

state.json 每事件更新 + 每 5 秒兜底刷新。bot 读取时检查 ``ts``：

* 快照比 ``SNAPSHOT_MAX_AGE``（默认 10s）旧 → 视为过期，等同 unknown 处理。
  过期意味着抓包器死了或连接断了——这时协议信息不可信。

## 与 bot 的耦合方式

本模块**只读快照 + 给出建议**，不直接 import lifecycle/bridge：
返回一个 dict，bridge 启动时拿去决定初始状态。这样抓包器与 bot 的职责
彻底分开（状态维护在抓包器侧，bot 只消费）。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
#: 抓包器默认写在源码目录的 _sniff/state.json。
_SNIFF_REL = os.path.join("_sniff", "state.json")
DEFAULT_SNAPSHOT = os.path.join(HERE, _SNIFF_REL)

# 快照超过这个年龄视为过期（抓包器每 5s 兜底刷新一次，10s 还没动 = 有问题）
SNAPSHOT_MAX_AGE = 10.0


def resolve_snapshot_path() -> str:
    """定位 state.json：环境变量 > 可写数据目录 > exe 目录 > 模块目录。

    打包后 ``HERE`` 是临时解包目录（``_internal``），抓包器却写在源码/数据
    目录，**只认 HERE 会让协议判据整体失效**（2026-11-10 实机踩过：夜间
    咬钩、收线全部退化成纯视觉，卡在 WAIT_BITE）。
    """
    override = os.environ.get("RF4_SNIFF_STATE")
    if override:
        return override
    candidates: list = []
    try:
        import app_paths  # noqa: PLC0415

        candidates.append(str(app_paths.data_path("_sniff", "state.json")))
        candidates.append(str(app_paths.APP_DIR / "_sniff" / "state.json"))
    except Exception:  # noqa: BLE001 - 独立使用时没有 app_paths 也能跑
        pass
    candidates.append(DEFAULT_SNAPSHOT)
    # 源码目录的上一级（exe 放在 bot/ 子目录里时也能找到）
    candidates.append(os.path.join(os.path.dirname(HERE), _SNIFF_REL))
    for path in candidates:
        try:
            if os.path.exists(path):
                return path
        except OSError:
            continue
    return candidates[0] if candidates else DEFAULT_SNAPSHOT

# casting 接手：按住 F5 的时长。太短抛不出去，太长蓄力过头。
# 游戏蓄力到 100% 约需 2 秒，抛竿其实只需轻按后松开（短抛）。
CAST_RELEASE_PRESS = 0.25


class HandoffError(RuntimeError):
    """接手失败（快照损坏等）。调用方应降级到视觉路径，不应崩溃。"""


def read_snapshot(path: Optional[str] = None,
                  now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """读快照；不存在/损坏/过期返回 None（调用方走视觉兜底）。

    ``path`` 省略时每次按 :func:`resolve_snapshot_path` 重新定位，这样打包后
    也能找到抓包器写在别处的文件。
    """
    if path is None:
        path = resolve_snapshot_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(snap, dict) or "phase" not in snap or "ts" not in snap:
        return None
    ts = float(snap["ts"])
    now = time.time() if now is None else now
    if now - ts > SNAPSHOT_MAX_AGE:
        return None
    return snap


def handoff_action(snap: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把快照翻译成接手动作。

    返回字段：
    * ``phase``：协议判定（unknown 表示无可用协议信息）
    * ``enter``：建议进入的状态名（与 AutoFishState 对齐的字符串）
    * ``keys``：按键策略 "hold"（不动）/ "release"（全松）/ "tap_f5"
    * ``keep_chain``：True 表示带着"入护完成"的链式许可进 PROBING
    * ``reason``：给人看的原因
    """
    if snap is None:
        return {
            "phase": "unknown",
            "enter": "PROBING",
            "keys": "release",
            "keep_chain": False,
            "reason": "no_protocol_snapshot",
        }
    phase = str(snap.get("phase") or "unknown")
    # event_age 缺省（旧版抓包器没这个字段）时按"未知"处理，仍信任 phase。
    event_age = snap.get("event_age")
    # phase 只能从真实事件推导出来，所以 phase != unknown 本身就是
    # "C2S 解密在工作"的证据。ready 只表示"双向都解开了"，S2C 解不开
    # 不影响判定——不能用它把有效 phase 降级成视觉兜底。
    if phase == "unknown":
        return {
            "phase": phase,
            "enter": "PROBING",
            "keys": "release",
            "keep_chain": False,
            "reason": "sniff_unknown",
        }
    # 事件太旧 = 连接其实没了，phase 是历史残留，不可信。
    # 存活判据取三者最小：event_age（任意事件）/ beat.step_age / beat.load_age。
    # 搏斗中 0e07 可能停（收线），但 0e08 每 2s 一条，所以必须都看。
    ages = [event_age]
    beat = snap.get("beat") or {}
    if isinstance(beat, dict):
        ages.append(beat.get("step_age"))
        ages.append(beat.get("load_age"))
    fresh = [float(a) for a in ages if isinstance(a, (int, float))]
    if fresh and min(fresh) > 60.0:
        return {
            "phase": phase,
            "enter": "PROBING",
            "keys": "release",
            "keep_chain": False,
            "reason": "sniff_stale_events:%ds" % int(min(fresh)),
        }
    if phase == "fighting":
        return {
            "phase": "fighting",
            "enter": "FIGHT",
            "keys": "hold",
            "keep_chain": False,
            "reason": "protocol_fighting",
        }
    if phase == "line_in_water":
        return {
            "phase": "line_in_water",
            "enter": "WAIT_BITE",
            "keys": "release",
            "keep_chain": False,
            "reason": "protocol_line_in_water",
        }
    if phase == "casting":
        # 蓄力中键可能还按着。安全做法：短按 F5 再松开——
        # 若已在蓄力，这次按压追加力度后释放，确保抛出；
        # 若其实没在蓄力，轻按会开始蓄力 0.25s 后释放，形成一次短抛。
        return {
            "phase": "casting",
            "enter": "PROBING",
            "keys": "tap_f5",
            "keep_chain": False,
            "reason": "protocol_casting",
        }
    if phase == "kept":
        return {
            "phase": "kept",
            "enter": "PROBING",
            "keys": "release",
            "keep_chain": True,
            "reason": "protocol_keep_fish_confirmed",
        }
    if phase in ("ended", "fish_left"):
        return {
            "phase": phase,
            "enter": "PROBING",
            "keys": "release",
            "keep_chain": False,
            "reason": "protocol_%s" % phase,
        }
    return {
        "phase": phase,
        "enter": "PROBING",
        "keys": "release",
        "keep_chain": False,
        "reason": "handoff_unknown",
    }


def resolve(now: Optional[float] = None,
            path: str = DEFAULT_SNAPSHOT) -> Dict[str, Any]:
    """一站式：读快照 + 翻译动作。bridge 启动时调这一个就够。"""
    return handoff_action(read_snapshot(path=path, now=now))
