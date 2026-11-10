# 坐标导航（autofish_nav.py）

给卖鱼、回钓点这类"要走一段路"的场景用。只吃**坐标**和**罗盘**两种输入。

## 1. 实机标定结果（2026-11-06，本客户端）

| 项 | 实测值 |
|---|---|
| 坐标位置 | 右下角小地图圆盘内偏下，`72:58` |
| 坐标 ROI | `(1725, 950, 160, 56)`，raw 直读，置信度 0.97+，约 18ms |
| 坐标系 | `+x = 东`、`+y = 北`；`bearing = atan2(dx, dy)` |
| 罗盘 | 圆盘边缘 N（蓝方块）/ S（红方块），随视角旋转 |
| 玩家朝向 | `-θ_N`（θ_N = N 标记相对圆心、以正上方为 0 的顺时针角） |
| **A / D 键** | **是横移，不是转身**——按住 D 一秒，朝向 278.60° → 278.58° |
| 转身方式 | **鼠标水平相对移动**，实测约 **14.3 px/度** |
| 移动速度 | 约 0.15~0.33 坐标单位/秒（1 单位约 4~5 米） |

**A/D 不转身这一条是决定性的。** 中文版 `navigator.py` 正是用 A/D 当转向键；
在本客户端上那样写只会让角色横向平移，方向永远调不准，而它还只打一行
"方向识别异常"的日志。所以本模块：转向只走鼠标，朝向只从罗盘读。

## 2. 为什么不靠"走一步看位移"估朝向

坐标是整数，1 单位要走 3 秒以上，用位移估朝向既慢又粗。罗盘每帧都能读到
（实测波动 < 0.1°），转完立刻能验证是否到位。

## 3. 主循环

```
读坐标 → 到 tolerance 内就结束
  ↓
读罗盘朝向 → 算目标方位差 Δ
  ↓
|Δ| > deadzone ? 鼠标转 Δ×px_per_deg（单次限幅 400px）→ 复读罗盘校正 px_per_deg
  ↓
否则按 W 走 step_sec 秒 → 复查坐标
```

`px_per_deg` 会自适应：每次转完用"实际转过多少度"反推真实比例，
EMA 系数 0.4（实测从 9.6 自纠到 14.29）。

## 4. 失败分类（全部明确抛错，不猜方向）

| stage | 触发 |
|---|---|
| `timeout` | 超过 `NavConfig.timeout`（默认 240 秒） |
| `blind` | 连续 8 次读不到坐标 |
| `heading_blind` | 连续 6 次读不到罗盘 |
| `stuck` | 连续 4 步前进后坐标没变（每步 2 秒，即 8 秒没挪窝） |

`read_coord()` / `read_heading()` 返回 `None` 只表示"这一帧没读到"，
**绝不返回 `(0,0)` 或 0° 冒充**。

## 5. 实机验收记录

| 场景 | 结果 |
|---|---|
| 小退（Esc→退出→登录） | **11.7 秒**完成，一次通过；重登后仍在原钓点（差 3.2 单位） |
| 后退走开 3.6 单位 → 导航回原点 | **8.3 秒**，转 1 次、走 3 步，最终误差 1.4 单位 |
| 整数坐标导致的"单步位移为 0" | 未误判卡住（`min_move=0.25`，连续 4 次才判） |

## 6. 目标点与策略：三层配置（`autofish_places.py` + `record_place.py`）

### 结构

```
地图  ──►  目的地（多个，可切换）  ──►  策略（跨地图复用）
```

**一个目的地 = 坐标 + 朝向**。坐标只管"站在哪"，朝向决定"面朝哪"：
站到钓位却面朝岸上，抛竿就废了；卖鱼走到门口却背对门，按 E 也进不去。
所以 `mark` 一次同时记 `(x, y, heading)` 三样。

```json
{
  "version": 3,
  "active_map": "蚊子湖",
  "active_destination": "钓位A",
  "active_strategy": "底钓标准",
  "strategies": {
    "底钓标准": {"method": "bottom", "cast_power": 0.85, "friction_base": 17,
               "wait_bite_timeout": 50, "auto_net": false, "bait": "玉米", "note": ""}
  },
  "maps": {
    "蚊子湖": {"places": {
      "钓位A": {"x": 73, "y": 57, "heading": 278.6, "kind": "fishing",
               "strategy": "底钓标准", "note": "第三块石头，面朝水面"},
      "钓位B": {"x": 88, "y": 44, "heading": 300.0, "kind": "fishing", "strategy": "底钓标准"},
      "鱼市":  {"x": 120, "y": 30, "heading": 90.0, "kind": "sell"}
    }},
    "老奥": {"places": {
      "钓位": {"x": 400, "y": 120, "heading": 15.0, "kind": "fishing", "strategy": "路亚快抽"}
    }}
  }
}
```

`heading` 语义与罗盘一致：**0° = 北，顺时针增大**，取值 `[0, 360)`；
`null` 表示这个点不要求朝向（纯路过点），导航到达后不做对齐。

三层各自解决一个问题：

| 层 | 解决什么 | 换一个时改哪里 |
|---|---|---|
| 地图 | 坐标是**地图内局部坐标**，跨图同号值很常见 | `use <地图>`，目的地集合整套换掉 |
| 目的地 | 一张图多个钓位/鱼市，按 `kind` 区分用途 | `mark` 加一个点，`dest use` 选当前 |
| 策略 | 钓法/力度/摩擦基准/等口超时/抄网开关，跨图复用 | `strategy set`，所有引用它的点一起生效 |

### 安全边界

* `friction_base` 限制在 **1..29**：29 是客户端硬顶，配置不能突破；
* **当前地图没有这个点 → `None` / `unknown_place`，绝不回退到别的地图**；
* 换地图会**清空 `active_destination`**，避免拿图 A 的钓位在图 B 里走；
* 多个钓位却还没选 → `active_place()` 返回 `None`，不猜"大概哪个"；
* 策略引用缺失只记 warning（策略可以后补），运行时取到 `None` 用默认值；
* 文件缺失 = 空表；坏条目跳过；写盘原子替换；v2 旧文件仍能读；
* `heading=0` 是**合法值**（正北），不能跟"没有朝向"混为一谈。

### 到达后对齐朝向

`navigate_to_place()` 是两段：

1. `navigate()` 走到 `(x, y)`，容差 `tolerance`（默认 1.5 单位）；
2. `face_heading()` 原地转到 `heading`，容差 `face_tolerance`（默认 5°），
   最多 10 轮、总超时 30 秒，全程鼠标闭环（A/D 是横移，不能用）。

朝向读不到、转不到位都**抛错**（`heading_blind` / `face_unreachable` / `face_timeout`），
不会带着错朝向继续抛竿。`face=False` 可跳过对齐（纯路过场景）。

### 命令

```bash
python record_place.py maps                  # 地图列表（* = 当前）
python record_place.py use 蚊子湖             # 切地图
python record_place.py list                  # 当前地图的目的地
python record_place.py mark 钓位A --kind fishing --strategy 底钓标准 --use
python record_place.py mark 鱼市 --kind sell
python record_place.py dest use 钓位B         # 选当前钓位
python record_place.py delete 钓位A
python record_place.py rename 点3 钓位A       # 给录制的点改名

python record_place.py strategy list
python record_place.py strategy set 底钓标准 method=bottom friction_base=17 cast_power=0.85
python record_place.py strategy use 底钓标准
python record_place.py strategy delete 底钓标准
```

### 两种记点方式

**A. 热键录制（推荐，一次记多个）**

```bash
python record_place.py record --map 蚊子湖
# 站到位置、面朝要抛竿的方向
# Ctrl+Alt+Shift+F11  记一个点（自动命名 点1、点2…）
# Ctrl+Alt+Shift+F12  结束
```

* 每按一次**立即落盘**，中途退出不丢已记的点；
* 每个点**自动存一张缩略截图**（`bot/_places/shots/`），`list` 里显示成
  `[图 20261109-104159_蚊子湖_点1.png]`，事后改名时对着图认地方，不用回忆坐标数字；
* 记点**不抢焦点**（PrintWindow 抓图），不会打断已经摆好的视角；
  后台抓不到画面时会自动拉一次前台重试，只激活窗口、不动鼠标；
* 每次记点**连读两帧校验**：坐标差 > 0.5 单位或朝向差 > 3° 就拒绝，
  提示"画面在动，稳住再按"——防止记到一个路过的位姿；
* 名字是自动编号，事后 `rename` 改，不用边站边打字。

**B. 命令行 mark（一次一个，名字直接给）**

```bash
python record_place.py mark 钓位A --kind fishing --strategy 底钓标准 --use
```

适合已经想好名字、或者要顺带绑定策略的场合。读不到坐标/朝向就报错退出，
不写猜的值。

两种方式都要求：**站定、面朝目标方向、别转视角**——朝向只能在那一刻读准。

### 代码里

```python
places = autofish_places.Places()             # bot/autofish_places.json
place  = places.active_place("fishing")       # 当前钓位（没选且不唯一 → None）
strat  = places.strategy_for(place)           # 该点绑定的策略，退到当前策略
nav.navigate_to_place(io, places, place.name)
```

`places` 是鸭子类型（只要 `require(place, map_name) -> Place`），导航内核不 import
配置模块，两者互不依赖。

### 当前地图怎么确定

现在靠**显式维护** `active_map`。换图钓鱼是低频动作，先不引入猜测：猜错地图比
没有导航更危险。以后从加载画面/地图界面 OCR 地图名再调 `set_active()` 即可。

## 7. 还没做的

- **卖鱼流程**：走到鱼市 → E 进店 → 全选 → 出售 → Esc，商店内按钮坐标未标定。
- **鱼市目标坐标**：需要站到店门口记一次 `X:Y`。
- 导航目前是独立模块，**没有接进状态机**。
- 长距离导航（几十单位）没测过；按 0.3 单位/秒估算，100 米要走 1 分钟以上，
  实际路线可能被地形挡住，需要先看地图规划路点。
