"""分地图的目的地表 + 钓鱼策略表。

## 为什么按地图分开

游戏坐标是**地图内局部坐标**（不同地图各自从原点算），两张图里都可能出现
`(73, 57)`。如果把目标点平铺在一张表里、不分地图，那么在 A 图取到 B 图的钓位
坐标，角色会直奔一个完全错误的方向——轻则绕路，重则走进水里。

```
active_map: "蚊子湖"
maps:
  蚊子湖: { 钓位A:(73,57) fishing, 钓位B:(88,44) fishing, 鱼市:(120,30) sell }
  老奥:   { 钓位:(400,120) fishing }
```

## 三层结构

```
地图  ──►  目的地（多个）  ──►  策略（可复用）
```

* **地图**：坐标的命名空间，切换地图后目的地集合整套换掉。
* **目的地**：一张图上的若干点，每个点有 `kind` 标明用途
  （`fishing` 钓位 / `sell` 鱼市 / `shore` 岸边 / `dock` 码头）。
  一张图可以有多个钓位，靠 `active_destination` 选当前用哪个。
* **策略**：钓鱼参数组（钓法、抛竿力度、摩擦基准、等口超时、是否自动抄网、
  鱼饵备注）。策略是**跨地图共享**的：同一套"底钓标准"可以给多张图的钓位用，
  改一处全部生效。

## 安全边界

* `friction_base` 被限制在 **1..29**：29 是客户端的硬顶，配置不能突破；
* 目的地引用了不存在的策略 → 记 warning，但仍然可用（策略可以后补），
  运行时取到 `None` 就该用默认值，而不是照着一个空策略乱设；
* 当前地图没有这个点 → `None` / `unknown_place`，**绝不回退到别的地图**；
* 文件缺失 = 空表；坏条目跳过不致命；写盘用原子替换。

## 当前地图怎么确定

目前靠**显式维护** `active_map`（`record_place.py use <地图>`）。换图钓鱼是低频
动作，先不引入猜测：猜错地图比没有导航更危险。以后从加载画面/地图界面 OCR
地图名再调 `set_active()` 即可，接口已留好。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BOT_DIR = Path(__file__).resolve().parent
DEFAULT_PATH = BOT_DIR / "autofish_places.json"

SCHEMA_VERSION = 3
# 游戏坐标是两三位整数；超出范围基本是识别错误（例如把时间当坐标）
COORD_MIN = 0
COORD_MAX = 2000
MAX_NAME_LEN = 32
DEFAULT_MAP = "default"

# 目的地用途；不强制枚举，只是约定，方便按用途筛
KIND_FISHING = "fishing"
KIND_SELL = "sell"
KIND_SHORE = "shore"
KIND_DOCK = "dock"
KINDS = (KIND_FISHING, KIND_SELL, KIND_SHORE, KIND_DOCK)

# 摩擦档位硬顶：29 是客户端上限，任何配置都不得突破
FRICTION_MAX = 29
FRICTION_MIN = 1

DEFAULT_STRATEGY = "默认"


class PlacesError(RuntimeError):
    """坐标表/策略表操作失败（名字非法、坐标非法、条目不存在等）。"""


def _valid_coord(x: Any, y: Any) -> bool:
    for v in (x, y):
        if isinstance(v, bool) or not isinstance(v, int):
            return False
        if not (COORD_MIN <= v <= COORD_MAX):
            return False
    return True


def _clean_name(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise PlacesError("%s must be str" % what)
    cleaned = value.strip()
    if not cleaned:
        raise PlacesError("%s must not be empty" % what)
    if len(cleaned) > MAX_NAME_LEN:
        raise PlacesError("%s too long (max %d)" % (what, MAX_NAME_LEN))
    if any(ord(ch) < 32 for ch in cleaned):
        raise PlacesError("%s must not contain control characters" % what)
    return cleaned


def _clamp_friction(value: Any) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        raise PlacesError("friction_base must be a number")
    if not (FRICTION_MIN <= number <= FRICTION_MAX):
        raise PlacesError("friction_base must be within %d..%d (got %s)"
                          % (FRICTION_MIN, FRICTION_MAX, number))
    return number


def _parse_heading(value: Any) -> Optional[float]:
    """朝向：0..360 的度（0=北、顺时针）；None/空 表示不要求。"""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= number < 360.0):
        number = number % 360.0
    return round(number, 1)


@dataclass(frozen=True)
class Place:
    """一个目的地：坐标 + **朝向** + 用途 + 绑定的策略名。

    朝向不是可选项而是目标点的一半：坐标只管"站在哪"，朝向决定"面朝哪"。
    到了钓位却面朝岸上，抛竿就废了；卖鱼站到门口却背对门，按 E 也进不去。
    `heading=None` 表示这个点不要求朝向（例如纯路过点）。
    """

    name: str
    x: int
    y: int
    note: str = ""
    marked_at: str = ""
    kind: str = KIND_FISHING
    strategy: str = ""
    heading: Optional[float] = None
    # 记点时留的画面截图（相对 bot 目录）。自动编号的点靠它认地方再改名。
    shot: str = ""

    @property
    def coord(self) -> Tuple[float, float]:
        return (float(self.x), float(self.y))


@dataclass(frozen=True)
class Strategy:
    """钓鱼策略：一组可复用的参数。

    这些字段都是**运行层真的会读**的项；没把握的参数先不放进配置，
    免得出现"改了没效果"的假开关。
    """

    name: str
    method: str = "bottom"           # bottom / float / lure
    cast_power: float = 0.85         # 0..1，抛竿力度
    friction_base: int = 17          # 每竿抛前校准到的基准档位（1..29）
    wait_bite_timeout: float = 50.0  # 等口超时（秒）
    auto_net: bool = False           # 是否启用自动抄网
    bait: str = ""                   # 鱼饵说明，仅记录，暂不驱动
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "method": self.method,
            "cast_power": self.cast_power,
            "friction_base": self.friction_base,
            "wait_bite_timeout": self.wait_bite_timeout,
            "auto_net": self.auto_net,
            "bait": self.bait,
            "note": self.note,
        }


@dataclass
class MapPlaces:
    name: str
    display: str = ""
    places: Dict[str, Place] = field(default_factory=dict)

    def get(self, place: str) -> Optional[Place]:
        return self.places.get(place)

    def destinations(self, kind: Optional[str] = None) -> List[Place]:
        items = [p for p in self.places.values() if kind is None or p.kind == kind]
        return sorted(items, key=lambda p: p.name)


class Places:
    """目的地表 + 策略表的读写与查询。`path` 不存在时视为空表。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self.active_map: str = DEFAULT_MAP
        self.active_destination: str = ""
        self.active_strategy: str = ""
        self.maps: Dict[str, MapPlaces] = {}
        self.strategies: Dict[str, Strategy] = {}
        self.last_error: Optional[str] = None
        self.warnings: List[str] = []
        self.load()

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def load(self) -> None:
        self.last_error = None
        self.warnings = []
        self.active_map = DEFAULT_MAP
        self.active_destination = ""
        self.active_strategy = ""
        self.maps = {}
        self.strategies = {}
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 坏文件不该拖垮钓鱼
            self.last_error = "places_unreadable:%s:%s" % (type(exc).__name__, exc)
            return
        if not isinstance(raw, dict):
            self.last_error = "places_bad_root"
            return

        active = raw.get("active_map")
        if isinstance(active, str) and active.strip():
            self.active_map = active.strip()
        for key in ("active_destination", "active_strategy"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                setattr(self, key, value.strip())

        for name, body in (raw.get("strategies") or {}).items():
            parsed = self._parse_strategy(name, body)
            if parsed is not None:
                self.strategies[parsed.name] = parsed

        maps = raw.get("maps")
        if not isinstance(maps, dict):
            self.last_error = "places_bad_maps"
            return
        for map_name, body in maps.items():
            if not isinstance(map_name, str) or not map_name.strip():
                continue
            if not isinstance(body, dict):
                continue
            entry = MapPlaces(
                name=map_name.strip(),
                display=str(body.get("display") or ""),
            )
            places = body.get("places")
            if isinstance(places, dict):
                for place_name, item in places.items():
                    parsed = self._parse_place(place_name, item)
                    if parsed is not None:
                        entry.places[parsed.name] = parsed
            self.maps[entry.name] = entry

        if self.active_map not in self.maps and self.maps:
            # active_map 指向不存在的地图：不要静默换成别的地图（那会走错坐标）
            self.last_error = "active_map_missing:%s" % self.active_map
        if self.active_destination:
            mp = self.maps.get(self.active_map)
            if mp is None or self.active_destination not in mp.places:
                self.warnings.append(
                    "active_destination_missing:%s" % self.active_destination)
        if self.active_strategy and self.active_strategy not in self.strategies:
            self.warnings.append(
                "active_strategy_missing:%s" % self.active_strategy)
        self._check_strategy_refs()

    def _check_strategy_refs(self) -> None:
        for mp in self.maps.values():
            for place in mp.places.values():
                if place.strategy and place.strategy not in self.strategies:
                    self.warnings.append(
                        "strategy_ref_missing:%s@%s->%s"
                        % (place.name, mp.name, place.strategy))

    @staticmethod
    def _parse_place(name: Any, item: Any) -> Optional[Place]:
        if not isinstance(name, str) or not isinstance(item, dict):
            return None
        try:
            clean = _clean_name(name, "place name")
        except PlacesError:
            return None
        x, y = item.get("x"), item.get("y")
        if not _valid_coord(x, y):
            return None
        kind = str(item.get("kind") or KIND_FISHING)
        return Place(
            name=clean,
            x=int(x),
            y=int(y),
            note=str(item.get("note") or ""),
            marked_at=str(item.get("marked_at") or ""),
            kind=kind,
            strategy=str(item.get("strategy") or ""),
            heading=_parse_heading(item.get("heading")),
            shot=str(item.get("shot") or ""),
        )

    @staticmethod
    def _parse_strategy(name: Any, item: Any) -> Optional[Strategy]:
        if not isinstance(name, str) or not isinstance(item, dict):
            return None
        try:
            clean = _clean_name(name, "strategy name")
            friction = _clamp_friction(item.get("friction_base", 17))
        except PlacesError:
            return None
        try:
            power = float(item.get("cast_power", 0.85))
        except (TypeError, ValueError):
            power = 0.85
        try:
            timeout = float(item.get("wait_bite_timeout", 50.0))
        except (TypeError, ValueError):
            timeout = 50.0
        return Strategy(
            name=clean,
            method=str(item.get("method") or "bottom"),
            cast_power=max(0.0, min(1.0, power)),
            friction_base=friction,
            wait_bite_timeout=max(1.0, timeout),
            auto_net=bool(item.get("auto_net", False)),
            bait=str(item.get("bait") or ""),
            note=str(item.get("note") or ""),
        )

    def save(self) -> None:
        payload: Dict[str, Any] = {
            "version": SCHEMA_VERSION,
            "active_map": self.active_map,
            "active_destination": self.active_destination,
            "active_strategy": self.active_strategy,
            "strategies": {n: s.as_dict() for n, s in self.strategies.items()},
            "maps": {},
        }
        for name, mp in self.maps.items():
            payload["maps"][name] = {
                "display": mp.display,
                "places": {
                    p.name: {
                        "x": p.x,
                        "y": p.y,
                        "heading": p.heading,
                        "kind": p.kind,
                        "strategy": p.strategy,
                        "note": p.note,
                        "marked_at": p.marked_at,
                        "shot": p.shot,
                    }
                    for p in mp.places.values()
                },
            }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".places_", suffix=".tmp",
                                   dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # 查询：目的地
    # ------------------------------------------------------------------
    def _map(self, map_name: Optional[str]) -> Optional[MapPlaces]:
        return self.maps.get(map_name or self.active_map)

    def get(self, place: str, map_name: Optional[str] = None) -> Optional[Place]:
        mp = self._map(map_name)
        if mp is None:
            return None
        return mp.get(place)

    def require(self, place: str, map_name: Optional[str] = None) -> Place:
        """取不到就抛错；导航前必须走这个，绝不能拿默认值硬走。"""
        used_map = map_name or self.active_map
        mp = self.maps.get(used_map)
        if mp is None:
            raise PlacesError("unknown_map:%s" % used_map)
        found = mp.get(place)
        if found is None:
            raise PlacesError("unknown_place:%s@%s" % (place, used_map))
        return found

    def coord(self, place: str, map_name: Optional[str] = None) -> Optional[Tuple[float, float]]:
        found = self.get(place, map_name)
        return None if found is None else found.coord

    def map_names(self) -> List[str]:
        return sorted(self.maps)

    def place_names(self, map_name: Optional[str] = None) -> List[str]:
        mp = self._map(map_name)
        return [] if mp is None else sorted(mp.places)

    def destinations(self, map_name: Optional[str] = None,
                     kind: Optional[str] = None) -> List[Place]:
        """当前（或指定）地图上的一批目的地，可按用途筛。"""
        mp = self._map(map_name)
        return [] if mp is None else mp.destinations(kind)

    def active_place(self, kind: Optional[str] = None) -> Optional[Place]:
        """当前选中的目的地；没选或不存在时返回 None。

        没显式选过就退回"第一个指定用途的目的地"，但**只在本图内**找。
        """
        mp = self._map(None)
        if mp is None:
            return None
        if self.active_destination:
            found = mp.get(self.active_destination)
            if found is not None and (kind is None or found.kind == kind):
                return found
            return None
        candidates = mp.destinations(kind)
        if len(candidates) == 1:
            return candidates[0]
        return None

    def set_active_destination(self, name: str, map_name: Optional[str] = None) -> None:
        used_map = map_name or self.active_map
        mp = self.maps.get(used_map)
        if mp is None:
            raise PlacesError("unknown_map:%s" % used_map)
        if name not in mp.places:
            raise PlacesError("unknown_place:%s@%s" % (name, used_map))
        self.active_destination = name

    # ------------------------------------------------------------------
    # 查询：策略
    # ------------------------------------------------------------------
    def strategy(self, name: Optional[str] = None) -> Optional[Strategy]:
        """取策略；不传名字时用 active_strategy，再退回 DEFAULT_STRATEGY。"""
        for key in (name, self.active_strategy, DEFAULT_STRATEGY):
            if key and key in self.strategies:
                return self.strategies[key]
        return None

    def strategy_for(self, place: Optional[Place] = None) -> Optional[Strategy]:
        """目的地 → 它绑定的策略；没绑就用当前策略。"""
        if place is not None and place.strategy:
            found = self.strategies.get(place.strategy)
            if found is not None:
                return found
        return self.strategy()

    def strategy_names(self) -> List[str]:
        return sorted(self.strategies)

    def put_strategy(self, name: str, **fields: Any) -> Strategy:
        name = _clean_name(name, "strategy name")
        base = self.strategies.get(name) or Strategy(name=name)
        allowed = set(Strategy.__dataclass_fields__) - {"name"}
        unknown = set(fields) - allowed
        if unknown:
            raise PlacesError("unknown strategy fields: %s" % ", ".join(sorted(unknown)))
        if "friction_base" in fields:
            fields["friction_base"] = _clamp_friction(fields["friction_base"])
        if "cast_power" in fields:
            fields["cast_power"] = max(0.0, min(1.0, float(fields["cast_power"])))
        if "wait_bite_timeout" in fields:
            fields["wait_bite_timeout"] = max(1.0, float(fields["wait_bite_timeout"]))
        entry = replace(base, **fields)
        self.strategies[name] = entry
        return entry

    def set_active_strategy(self, name: str) -> None:
        if name not in self.strategies:
            raise PlacesError("unknown_strategy:%s" % name)
        self.active_strategy = name

    def remove_strategy(self, name: str) -> bool:
        return self.strategies.pop(name, None) is not None

    # ------------------------------------------------------------------
    # 写入：目的地
    # ------------------------------------------------------------------
    def put(self, place: str, coord: Tuple[float, float],
            map_name: Optional[str] = None, note: str = "",
            kind: str = KIND_FISHING, strategy: str = "",
            heading: Optional[float] = None, shot: str = "") -> Place:
        used_map = _clean_name(map_name or self.active_map, "map name")
        name = _clean_name(place, "place name")
        x, y = int(round(float(coord[0]))), int(round(float(coord[1])))
        if not _valid_coord(x, y):
            raise PlacesError("coord out of range: (%s, %s)" % (x, y))
        mp = self.maps.get(used_map)
        if mp is None:
            mp = MapPlaces(name=used_map)
            self.maps[used_map] = mp
        entry = Place(
            name=name, x=x, y=y, note=str(note or ""),
            marked_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            kind=str(kind or KIND_FISHING), strategy=str(strategy or ""),
            heading=_parse_heading(heading), shot=str(shot or ""),
        )
        mp.places[name] = entry
        return entry

    def remove(self, place: str, map_name: Optional[str] = None) -> bool:
        mp = self._map(map_name)
        if mp is None:
            return False
        return mp.places.pop(place, None) is not None

    def set_active(self, map_name: str) -> None:
        name = _clean_name(map_name, "map name")
        if name not in self.maps:
            self.maps[name] = MapPlaces(name=name)
        if name != self.active_map:
            # 换地图后旧的目的地不属于新图，必须清掉，否则会拿错坐标
            self.active_destination = ""
        self.active_map = name

    # ------------------------------------------------------------------
    # 展示
    # ------------------------------------------------------------------
    def describe(self, map_name: Optional[str] = None) -> str:
        used_map = map_name or self.active_map
        mp = self.maps.get(used_map)
        if mp is None:
            return "%s: （无此地图）" % used_map
        if not mp.places:
            return "%s: （空）" % used_map
        lines = ["%s%s:" % (used_map, "（当前）" if used_map == self.active_map else "")]
        for place in mp.destinations():
            flags = [place.kind]
            flags.append("朝%5.1f°" % place.heading if place.heading is not None
                         else "朝  ?  ")
            if place.strategy:
                flags.append("策略=" + place.strategy)
            if used_map == self.active_map and place.name == self.active_destination:
                flags.append("当前")
            note = ("  # " + place.note) if place.note else ""
            shot = ("  [图 %s]" % place.shot.rsplit("/", 1)[-1]) if place.shot else ""
            lines.append("  %-10s (%4d, %4d)  %s%s%s"
                         % (place.name, place.x, place.y, " ".join(flags), note, shot))
        return "\n".join(lines)

    def describe_strategies(self) -> str:
        if not self.strategies:
            return "策略表是空的。"
        lines = []
        for name in self.strategy_names():
            s = self.strategies[name]
            mark = "（当前）" if name == self.active_strategy else ""
            lines.append(
                "  %-12s%s 钓法=%s 力度=%.2f 摩擦=%d 等口=%.0fs 抄网=%s%s"
                % (name, mark, s.method, s.cast_power, s.friction_base,
                   s.wait_bite_timeout, "开" if s.auto_net else "关",
                   ("  # " + s.note) if s.note else ""))
        return "\n".join(lines)
