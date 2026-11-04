# RF4 自动钓鱼 · RF4S 运行时补丁层

给「俄罗斯钓鱼 4」的第三方客户端做底层钓鱼自动化。**不修改 RF4S 源码**，
在运行时把它的截图、输入、判据、流程逐个 patch 掉，改成能在这个客户端上跑起来的一整套循环：

```
抛竿 → 等咬钩 → 搏鱼（左右键按住 + 实时调摩擦片）→ 入护 → 重抛
```

卡住的时候不硬扛：把现场截图丢出来，等外部 Agent 给指令再继续。

> ⚠️ 仅供个人学习与单机使用。本仓库**不包含**游戏本体与任何账号 / 服务器信息；上游 RF4S 源码以 `vendor/` 快照形式内置（GPL-3.0）。

---

## 目录

- [它分几层](#它分几层)
- [adapter 到底干什么](#adapter-到底干什么)
- [快速开始](#快速开始)
- [运行](#运行)
- [桌面开关](#桌面开关)
- [常用配置](#常用配置)
- [出问题了怎么查](#出问题了怎么查)
- [已知限制](#已知限制)

---

## 它分几层

一共 5 层，从下往上：

```
┌──────────────────────────────────────────────────────────┐
│ ⑤ 开关层     bot_ctl.py        启停 / 查状态 / 释放按键      │
├──────────────────────────────────────────────────────────┤
│ ④ 接管层     agent_hook.py     卡住 -> 上报现场 -> 等指令    │
│              agentctl.py       人/Agent 下发指令            │
├──────────────────────────────────────────────────────────┤
│ ③ 适配层     rf4s_adapter.py   ★ 本仓库的主体               │
│              rf4_focus.py      窗口置前 / SendInput 点击     │
├──────────────────────────────────────────────────────────┤
│ ② 流程层     RF4S 上游（第三方，需自行 clone）               │
│              提供 cast / pull / lift / handle_fish 等骨架    │
├──────────────────────────────────────────────────────────┤
│ ① 游戏窗口   截图（PrintWindow）+ 输入（SendInput）          │
└──────────────────────────────────────────────────────────┘
```

**为什么要分这么多层**：

- ① 是平台能力。这个客户端独占渲染 + 拒绝合成鼠标事件，所以必须换掉 Windows 层面的截图和注入方式。
- ② 是别人写好的流程骨架，能用就用，不自造一遍。
- ③ 是补丁层：上游的判据和流程假设了英文原版，在这个客户端上大半失效，全部在这里替换。
- ④ 因为游戏里会冒出脚本没见过的东西（解锁弹窗、卡底、线快被拉空），写死处理会很脆，
  所以改成"上报现场 + 等指令"。
- ⑤ 只是让人能一键开停，不参与游戏逻辑。

---

## adapter 到底干什么

`rf4s_adapter.py` 做三件事：**修截图 / 换输入 / 改判据**。
启动时按顺序打 13 个补丁，每个都对应一个具体的实测问题：

### 一、平台层（让程序能"看见"和"操作"游戏）

| 补丁 | 干了什么 | 为什么必须改 |
|---|---|---|
| `patch_screenshot` | 用 `PrintWindow` 把游戏画到内存，再伪装成 `pyautogui.screenshot` | 游戏独占渲染，`ImageGrab`/`mss` 截出来全黑 |
| `patch_input` | 鼠标按下/抬起映射到键盘 `F5`/`F6` | 合成的鼠标事件被游戏丢弃；`F5`/`F6` 已在游戏里绑定为"基本操作"/"必选其一" |

### 二、判据层（回答"现在是什么状态"）

| 补丁 | 判据 | 实测依据 |
|---|---|---|
| `patch_fish_hooked_bar` | **咬钩 = 底部圆形鱼图标出现** | 外环白色像素：有鱼 110~133，无鱼 0~22 |
| `patch_fish_captured` | **结算框** = `keep.png` ≥0.95 且位置落在 (300,40)-(1500,620) | 鱼护 HUD 图标也会匹配到 0.82，会误判 |
| `patch_tackle_ready_timeout` | 复位完成 = 无鱼图标 + 无结算框，连续 2 次 | 中文界面 `ready.png` 匹配不到，原本每轮白等 18 秒 |
| `patch_retrieval_finished_debounce` | 收线完成要连续 6 次匹配 | `wheel.png`（线杯图标，外圈就是线长圆环）**本身能匹配**，但鱼一松力线杯停转、图标回到静止形态就会误命中，所以要防抖 |
| `patch_retrieval_timeout` | 45 秒强制兜底 | 同上，靠判据可能永远等不到 |

### 三、流程层（把动作顺序改成这个客户端要的样子）

| 补丁 | 改成了什么 |
|---|---|
| `maybe_dismiss` | **弹窗「确定」统一处理**：卡牌/技能/升级/能力解锁这类排队弹窗自动连点到消失，插在抛竿前、等口时、入护后，清不掉才上报 Agent。详见 [docs/modals.md](docs/modals.md) |
| `patch_cast` | 抛竿前摩擦片绝对校准 → **Shift + 左键按住 1 秒** → 等 2.5 秒 → **短按左键关线杯**。不做"抛竿成功"的画面 diff 校验 |
| `patch_pull_continuous` | 搏鱼全程**左右键都按住**，每 0.2 秒采一次力度条 + 线长圆环，同时跑摩擦片控制 |
| `patch_lift_fish` | 收完线后继续按住左右键直到结算框；鱼留在水里就**停手等人工** |
| `patch_handle_fish_click` | **入护改成点按钮**（SendInput），不按空格；顺带跳过上游 10 秒的结算动画等待 |
| `patch_bottom_mode_held` | 单竿手持：不放下竿、不轮询多竿；咬钩用 0.4 秒轮询而不是死等 16 秒 |
| `patch_single_rod` | 修上游单竿模式 `_get_available_rods` 把对象当索引的 bug |

### 摩擦片：为什么单独讲

> 完整的规则口径（多少秒算"一直绿"、遇红几帧确认、每条鱼恢复 17 等）
> 见 **[docs/friction-control.md](docs/friction-control.md)**，那是唯一权威版本。

### 状态机（设计草案，未实现）

脚本各阶段（未抛竿 / 等口 / 搏鱼 / 临岸 / 抄网 / 结算 / 弹窗 / 未知）
应整理成显式状态机，并在**启动时从屏幕推断当前状态**再决定初始化动作——
详见 **[docs/state-machine.md](docs/state-machine.md)**。核心约束：
摩擦片校准只能朝**安全端（向下）**饱和，向上饱和（滚满 30）仅限线上无鱼时。

摩擦档位在屏幕上**没有读数**，只能靠"滚了几格"自己记：

```python
抛竿前：向上滚满 30 档，再退回 17        # 绝对校准，唯一允许"顶满"的时刻
搏鱼中：维护 _fric["cur"]，算 delta，只滚 |delta| 格   # 增量
```

| 条件 | 动作 |
|---|---|
| 最近 10 秒有一帧到红 | 立刻 `cur - cur//3`（17 → 12） |
| 最近 10 秒全是绿 | `cur + cur//5`（17 → 20） |
| 只有黄/橙 | 不动 |

> 红必须**立刻**降。早先版本要等观察窗口走完才降，等于红了还能再扛 10 秒 —— 直接断线。

---

## 快速开始

### 1. 游戏设置（必须）

- 窗口客户区 **1920×1080**（程序启动时会自己拉齐并校验）
- 键位映射，游戏内 `设置 → 控制`：

  | 功能 | 槽位 1 | 槽位 2 |
  |---|---|---|
  | 基本操作 | 鼠标左键 | **F5** |
  | 必选其一的操作 | 鼠标右键 | **F6** |
  | 使用 / 收起抄网 | Space | **PD (PageDown)** |
  | 打开 / 关闭线杯 | Enter | Num Enter |

  空格**不再被脚本使用**（入护是点按钮），抄网走 PageDown。

### 2. 依赖

```bash
pip install pywin32 pillow numpy opencv-python pyautogui pydirectinput
```

### 3. 第三方 RF4S 源码

**推荐**：本仓库已内置快照（`vendor/`，v0.11.0 + 定制 config），直接软链或设环境变量即可：

```bat
rem Windows cmd（junction 不需要管理员）：
mklink /J refs\RussianFishing4Script vendor
rem 或者：
set RF4S_REF=%CD%\vendor
```

也可以自行克隆上游（行为等价，只是要自己维护 config 定制）：

```bash
git clone https://github.com/dereklee0310/RussianFishing4Script refs/RussianFishing4Script
```

用环境变量指定任意路径：

```bash
set RF4S_REF=D:\path\to\RussianFishing4Script
```

> `vendor/` 的内容、与上游的差异、许可证说明见 [vendor/VENDOR.md](vendor/VENDOR.md)。

---

## 运行

```bash
python rf4s_adapter.py bot -N BOTTOM
```

启动后正常的话会看到这样一串：

```
[patch] 客户区 1920x1080 (目标 1920x1080) OK
[patch] 鼠标按键已映射: 左键->F5, 右键(抬竿)->F6
[patch] 咬钩检测 = 「底部圆形鱼图标是否出现」(中心 (549, 1024)，环白色像素 >= 60)
[patch] 虚拟桌面 (6000, 1440)，游戏区域已合成
...
INFO  Bottom mode started (single rod, held, no put-down)
INFO  Fish hooked, retrieving
INFO  Handling fish (click keep)
[patch] 入护按钮定位 (829, 974)
INFO  Keep fish by clicking the button
```

---

## 桌面开关

不想每次敲命令，用 `bot_ctl.py` 包一层：

```bash
python bot_ctl.py start     # 前台运行（窗口就是它的控制台）
python bot_ctl.py stop      # 停止 + 释放所有按键 + 清 PID
python bot_ctl.py status    # running / stopped
```

Windows 上做一个桌面 `钓鱼开关.bat` 指向 `bot_ctl.py` 即可（示例）：

```bat
@echo off
chcp 936 >nul
set "BOT=D:\path\to\rf4-autofish"
set "PY=%BOT%\refs\RussianFishing4Script\.venv\Scripts\python.exe"
"%PY%" "%BOT%\bot_ctl.py" start
pause
```

> ⚠️ 别把 bot 做成后台子进程（`DETACHED_PROCESS` 或 `start` 新开窗口）：
> 实测控制台一关，子进程会被一起收掉。**前台跑**最稳，关窗口就是停止。
> 停止请用 `bot_ctl.py stop`，它会显式松开 F5/F6/Shift/Enter/PageDown/Space。

---

## 常用配置

都在 `rf4s_adapter.py` 顶部，改完重启生效：

| 常量 | 默认 | 含义 |
|---|---|---|
| `FISH_ICON_CENTER` | (549, 1024) | 圆形鱼图标中心 |
| `FISH_ICON_WHITE_MIN` | 60 | 咬钩阈值（有鱼 110+，无鱼 0~22） |
| `LINE_RING_CENTER` / `LINE_RING_RADII` | (1237,1023) / (13,15) | 线长进度环 |
| `FRICTION_INITIAL` / `FRICTION_MAX` | 17 / 29 | 摩擦片起始档 / 上限 |
| `FRICTION_WINDOW` | 10.0 | 力度条观察窗口（秒） |
| `CAST_HOLD` / `CAST_LAND_DELAY` | 1.0 / 2.5 | 抛竿蓄力时长 / 等饵落水 |
| `LINE_RING_ACTIONS` | True | 线长异常是否真的上报 Agent |

---

## 出问题了怎么查

| 现象 | 先看什么 |
|---|---|
| 起来就退出 | 是不是没 clone RF4S，或者 `RF4S_REF` 指错了 |
| 一直空抛 / 不咬钩 | 键位有没有绑成 F5/F6；窗口客户区是不是 1920×1080 |
| 卡在某个界面不动 | 看 `_stuck/latest.json` 和同名截图，Agent 通道会存现场 |
| 卡在菜单里 | `python agentctl.py keys esc` 下发一条按键指令 |
| 断线频繁 | 摩擦片降档不及时，看日志里 `摩擦片 x -> y` 的时机 |
| 鱼上不来 | 鱼可能贴在岸边没结算，脚本会等人工；看日志有没有「需要人工上鱼」 |

Agent 指令（脚本卡住时用）：

```bash
python agentctl.py status                # 看最新一次上报
python agentctl.py click 960 810         # 点客户区坐标
python agentctl.py keys esc              # 按一次键
python agentctl.py resume                # 什么都不做，继续
python agentctl.py quit                  # 让它停手
```

---

## 已知限制

- 只适配 **1920×1080** 客户区，所有坐标都是按这个尺寸量的
- 只写了**底钓单竿**，海钓 / 路亚没有
- 线长圆环 `<10%` 的区间还没有足够实测样本，阈值是保守估计
- 结算框按钮位置靠"扫亮边边框"推断，UI 大改就失效
- 摩擦片依赖滚轮，**滚轮是否真的送达游戏尚未验证**（档位没有屏幕读数可对账）
- 中文客户端专用：`ready.png` 匹配不到（走了等价判据）；`wheel.png` 本身能匹配（它就是线杯图标），只是需要防抖

---

## License

外层适配层为 MIT；`vendor/` 内的上游 RF4S 快照遵循 **GPL-3.0**（见 `vendor/LICENSE`）。两套许可并存，分发时需一并携带。
