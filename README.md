# RF4 自动钓鱼 · RF4S 运行时补丁层

给「俄罗斯钓鱼 4」的第三方客户端做底层钓鱼自动化。**不修改 RF4S 源码**，
在运行时把它的截图、输入、判据、流程逐个 patch 掉，改成能在这个客户端上跑起来的一整套循环：

```
抛竿 → 等咬钩 → 搏鱼（左右键按住 + 实时调摩擦片）→ 入护 → 重抛
```

卡住的时候不硬扛：把现场截图丢出来，等外部 Agent 给指令再继续。

当前有两个入口：**`autofish_bridge.py`** 是正式全流程（显式状态机 + 本机 IPC + 独立 UI），
**`rf4s_adapter.py`** 保留为 legacy 适配层。两者的实测/验收分级见
[实机与验收分级](#实机与验收分级)。

> ⚠️ 仅供个人学习与单机使用。本仓库**不包含**游戏本体与任何账号 / 服务器信息；上游 RF4S 源码以 `vendor/` 快照形式内置（GPL-3.0）。

---

## 下载即用（Windows 绿色版）

不想碰 Python 的话，直接去 [Releases](../../releases) 取现成包，两种形态任选：

| 产物 | 适合 | 说明 |
|---|---|---|
| `RF4AutoFish-vX.Y.Z-win64-single.exe` | **发给别人 / 图省事** | 一个 94 MB 文件，双击即用；冷启动慢 5~15 秒 |
| `RF4AutoFish-vX.Y.Z-win64.zip` | 自己长期用 | 解压后 ~215 MB，启动快、临时空间不翻倍 |

**单文件版**（推荐发给别人）：

1. 双击 exe（首次启动会解包，等 5~15 秒），面板自动贴到游戏窗口右侧。
2. 点「启动后台（离线时）」→ `PROBING`；再点「开始/恢复自动」→ 开钓。

**zip 版**：解压到不含中文/空格的目录（`_internal` 必须和 exe 同级，别只拿 exe 走），
之后步骤同上。

> 两版都自带面板（tkinter）和全部依赖，**不需要装 Python**。
> 抓包器（Npcap + 管理员）与 `venv-ocr` 都是可选增强，普通钓鱼用不到。

- 一页纸速查（发给别人时一起给）：**[docs/QUICKSTART.txt](docs/QUICKSTART.txt)**
- 完整使用指南（能做什么/不能做什么、面板字段、状态对照、卡住怎么办）：
  **[docs/exe-guide.md](docs/exe-guide.md)**

```
┌──────────────────────┬───────────┐
│                      │ RF4 自动钓鱼│
│                      │ ● FIGHT    │
│      游戏窗口         │ cur 21/29  │
│   1920×1080           │ kept 12    │
│                      │ [开始][观察]│
│                      │ [停止][紧急]│
└──────────────────────┴───────────┘
                         ↑ 面板贴在右侧，跟随游戏
```

* 面板只是遥控器，**关掉面板不会停掉钓鱼**；要停点「立即停止」，要杀进程点「紧急停止」。
* 运行数据（日志/端点/pid）都在 `%LOCALAPPDATA%\RF4AutoFish\`。
* 官方热键是 `Ctrl+Alt+Shift+F6/F7/F8/F9/F10`，详见使用指南第 4 节。

> 同一个 exe 也是命令行工具：`RF4AutoFish.exe --selftest` 自检，
> `--ctl status|start|stop` 管后台，`--bot` 直接跑主循环。

---

## 目录

- [下载即用（Windows 绿色版）](#下载即用windows-绿色版)
- [它分几层](#它分几层)
- [adapter 到底干什么](#adapter-到底干什么)
- [快速开始](#快速开始)
- [运行](#运行)
- [桌面开关](#桌面开关)
- [全局热键](#全局热键)
- [常用配置](#常用配置)
- [出问题了怎么查](#出问题了怎么查)
- [实机与验收分级](#实机与验收分级)
- [已知限制](#已知限制)

---

## 它分几层

一共 6 层，从下往上：

```
┌──────────────────────────────────────────────────────────┐
│ ⑥ 界面/开关  autofish_ui.py（独立置顶面板，可关闭）          │
│              bot_ctl.py（start/stop/status/emergency-stop）│
├──────────────────────────────────────────────────────────┤
│ ⑤ 运行层     autofish_bridge.py   正式全流程入口            │
│              autofish_runtime.py  主线程 tick + IPC 发布     │
│              autofish_lifecycle.py 显式状态机/控制模式       │
│              autofish_ipc.py      loopback TCP + token      │
├──────────────────────────────────────────────────────────┤
│ ④ 接管层     agent_hook.py     卡住 -> 上报现场 -> 等指令    │
│              agentctl.py       人/Agent 下发指令            │
├──────────────────────────────────────────────────────────┤
│ ③ 适配层     rf4s_adapter.py   ★ legacy 入口 + 平台补丁     │
│              rf4_focus.py      窗口置前 / SendInput 点击     │
├──────────────────────────────────────────────────────────┤
│ ② 流程层     RF4S 上游（第三方，vendor/ 或 RF4S_REF）        │
│              提供 cast / pull / lift / handle_fish 等骨架    │
├──────────────────────────────────────────────────────────┤
│ ① 游戏窗口   截图（PrintWindow）+ 输入（SendInput）          │
└──────────────────────────────────────────────────────────┘
```

**为什么要分这么多层**：

- ① 是平台能力。这个客户端独占渲染 + 拒绝合成鼠标事件，所以必须换掉 Windows 层面的截图和注入方式。
- ② 是别人写好的流程骨架；正式桥接只复用它少数底层能力，不再跑上游 `Player.loop`。
- ③ 是 legacy 补丁层：上游的判据和流程假设了英文原版，在这个客户端上大半失效，全部在这里替换。
- ④ 因为游戏里会冒出脚本没见过的东西（解锁弹窗、卡底、线快被拉空），写死处理会很脆，
  所以改成"上报现场 + 等指令"。
- ⑤ 是正式运行层：显式状态机、唯一按键 owner、可停止的阻塞动作、本机回环 IPC。
- ⑥ 只是界面与开关：UI 关闭不影响后台 bot；`bot_ctl.py` 负责启停与身份校验。

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

### 状态机（已实现，验收分级见文档）

脚本各阶段（未抛竿 / 等口 / 搏鱼 / 临岸 / 抄网 / 结算 / 弹窗 / 未知）
已整理成显式状态机 `autofish_lifecycle.py`，启动时从屏幕推断当前状态再决定初始化动作——
详见 **[docs/state-machine.md](docs/state-machine.md)**。核心约束：
摩擦片校准只能朝**安全端（向下）**饱和，向上饱和（滚满 30）仅限线上无鱼时。

线杯剩线掉到 **15% 以下**会停手小退重登（Esc → 退出 → 登录），回来重新校准再抛，
详见 **[docs/relogin.md](docs/relogin.md)**。这一步不需要 OCR，全程用画面亮度结构判据。

> 实现完成不等于实机验收完成：**新完整桥接尚未跑通入护验收**，见
> [实机与验收分级](#实机与验收分级)。

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

- 窗口客户区 **1920×1080**（所有坐标与 `static/ready_cn.png` 都按这个尺寸标定）
- **正式桥接不会移动或缩放游戏窗口**：只在启动时做只读尺寸检查，不是 1920×1080
  就报 `window_size_...expected_1920x1080` 并停下；需要脚本自己把窗口拉齐时，才用
  legacy `rf4s_adapter.py`（它会保留标题栏并补偿客户区）
- 键位映射，游戏内 `设置 → 控制`：

  | 功能 | 槽位 1 | 槽位 2 |
  |---|---|---|
  | 基本操作 | 鼠标左键 | **F5** |
  | 必选其一的操作 | 鼠标右键 | **F6** |
  | 使用 / 收起抄网 | Space | **F7** |
  | 打开 / 关闭线杯 | Enter | Num Enter |

  空格**不再被脚本使用**（入护是点按钮），抄网走 **F7**。旧的 `PageDown`
  不是抄网键：实机确认它会放竿/切装备，脚本已不再发送；只在 `bot_ctl release`
  里作为旧版本残留清理保留。

### 2. 依赖

```bash
pip install pywin32 pillow numpy opencv-python pyautogui pydirectinput
```

发布目录**不要求** `vendor/.venv` 或 `refs/.../.venv` 存在。`bot_ctl.py` 按下面顺序
选择解释器：

1. 环境变量 `RF4_PYTHON` 指向的 python；
2. `RF4S_REF`（或 `refs/RussianFishing4Script`）下的 `.venv`；
3. 当前运行 `bot_ctl.py` 的解释器。

`autofish_ui.py` / `bot_ctl.py ui` 始终使用**当前解释器**，因此请确保当前解释器能
`import` 上表的依赖。

### 3. 第三方 RF4S 源码

**推荐**：本仓库已内置快照（`vendor/`，v0.11.0 + 定制 config），不需要 clone：

```bat
rem Windows cmd（junction 不需要管理员）：
mklink /J refs\RussianFishing4Script vendor
rem 或者直接指定：
set RF4S_REF=%CD%\vendor
```

`rf4s_adapter.py` 的解析顺序是 `RF4S_REF` → `refs/RussianFishing4Script` →
内置 `vendor/`（存在 `vendor/rf4s` 时）。没有任何硬编码的本机路径。

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

### 正式全流程（当前推荐入口）

```bash
python bot_ctl.py start          # 前台启动 autofish_bridge.py（状态机 + IPC）
python bot_ctl.py status         # 身份校验 + IPC 快照
python bot_ctl.py stop           # 优雅停止：IPC stop，等待进程退出
```

`start` 只启动一次，**不会**因为返回码 0 自动重启。bridge 启动后默认写入
`autofish.endpoint.json` 与 `autofish.process.json`（均被 `.gitignore` 忽略）。
停止/关闭时由 runtime 释放**脚本自己持有**的按键；`emergency-stop` 也只对身份
可验证的进程强杀，绝不只凭裸 PID。

### 独立 UI

```bash
python autofish_ui.py            # 直接启动面板（当前解释器）
python bot_ctl.py ui             # 用当前解释器，通过 bot_ctl 启动面板
python autofish_ui.py --demo     # 本地假快照，不连接 bot
```

- UI 是独立进程，只通过 loopback IPC 读取快照/发送语义命令，**不发送 F5/F6/滚轮**。
- **关闭 UI 不会停止后台 bot**：后台进程继续跑；下次打开 UI 会重新连接。
- 停止请点面板的“优雅停止”，或运行 `python bot_ctl.py stop`。
- `emergency-stop` 是明确的高级动作：仅对身份可验证的 python 进程
  `taskkill /T /F`，然后强制松键；普通停止不会抢前台或强杀。

### legacy 模式

```bash
python bot_ctl.py start --legacy       # 等价于 python rf4s_adapter.py bot -N BOTTOM
python rf4s_adapter.py bot -N BOTTOM   # 也可直接前台运行
```

`--legacy` 也可用环境变量 `RF4_BOT_LEGACY=1` / `RF4S_LEGACY=1` 打开。legacy 走
旧的 13 个补丁与上游 `Player.loop`，仅作为回退路径；正式验收以 bridge 为准。

### 前台/控制模式语义

- 游戏窗口失焦：立即进入 `BLOCKED`，不产生新动作；搏鱼子机按遮挡帧冻结。
- 游戏窗口重新回到前台：**AUTO 模式自动恢复**，不需要重新点启动。
- `OBSERVE` / `MANUAL` **不会**因为回到前台自动恢复为 AUTO；必须显式切回。
- `READY` 确认许可、入护链等在失焦后作废，回来后重新取证。

---

## 桌面开关

`bot_ctl.py` 也可以包成桌面 bat：

```bat
@echo off
chcp 936 >nul
set "BOT=D:\path\to\rf4-autofish-pub"
set "PY=%BOT%\vendor\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%BOT%\bot_ctl.py" start
pause
```

> 不想维护 venv 时，直接 `set RF4_PYTHON=C:\path\to\python.exe` 即可；没有该变量
> 且 refs venv 不存在时，`bot_ctl.py` 会退回当前解释器。
> 停止用 `python bot_ctl.py stop`；`emergency-stop` 只在优雅停止超时后使用。

---

## 全局热键

侧栏运行时会注册五组系统级热键，统一使用 `Ctrl+Alt+Shift`：

| 热键 | 作用 |
|---|---|
| `Ctrl+Alt+Shift+F6` | 启动后台（仅后台未连接时；已连上会提示无需启动） |
| `Ctrl+Alt+Shift+F7` | 开始 / 恢复自动 |
| `Ctrl+Alt+Shift+F8` | 仅观察 |
| `Ctrl+Alt+Shift+F9` | 本条结束后停 |
| `Ctrl+Alt+Shift+F10` | 立即停止 |

- **只在侧栏运行期间有效**：面板启动后注册，关闭面板即注销，退出后不残留全局占用。
- **关闭 UI 不会停止后台 bot**：热键随面板一起退出，不会替后台继续执行动作。
- 侧栏底部显示注册结果：正常显示“F7成功…”；某个组合被其他程序占用时显示“F8冲突”（或对应键），其余键仍可正常注册，不会整体失败。
- 恢复 / 观察 / 停止三类热键与鼠标点击走同一个按钮门控和命令通道；当前状态不允许时提示“不可用”，不会直接下发命令。
- **确认类按钮不绑定热键**：`确认钓具就绪`、`我已手动收网` 只能手动点击；`紧急停止` 同样不走热键。
- **`F6` 有离线门控**：只有后台没连上（IPC 不通）时才真正启动；已连上则提示“后台已在运行，无需启动”，不会重复拉起第二个实例。
- `F7` 会等 `Ctrl+Alt+Shift+F7` 整组键松开后再发送“恢复自动”，避免按键状态粘连；等待超时会取消并刷新按钮。
- 演示模式（`--demo`）和非 Windows 平台不注册任何热键。

---

## 常用配置

legacy 参数在 `rf4s_adapter.py` 顶部，改完重启生效；正式 bridge 的阈值在
`autofish_bridge.py` 顶部常量与 `autofish_lifecycle.LifecycleConfig`：

| 常量 | 默认 | 含义 |
|---|---|---|
| `FISH_ICON_CENTER` | (549, 1024) | 圆形鱼图标中心 |
| `FISH_ICON_WHITE_MIN` | 60 | 咬钩阈值（有鱼 110+，无鱼 0~22） |
| `LINE_RING_CENTER` / `LINE_RING_RADII` | (1237,1023) / (13,15) | 线长进度环 |
| `FRICTION_INITIAL` / `FRICTION_MAX` | 17 / 29 | 摩擦片起始档 / 上限 |
| `FRICTION_WINDOW` | 10.0 | 力度条观察窗口（秒） |
| `FRICTION_FIRST_OBSERVE` | 10.0 | 首次升档前的连续有效观察（秒） |
| `FRICTION_MAX_SAMPLE_GAP` | 1.8 | 真实读数长空档判定；超过则重新累计观察 |
| `CAST_HOLD` / `CAST_LAND_DELAY` | 1.0 / 2.5 | 抛竿蓄力时长 / 等饵落水 |
| `LINE_RING_ACTIONS` | True | 线长异常是否真的上报 Agent |
| `RF4_AUTO_NET` | 未设置/0 | **仅人工验收**打开自动抄网；默认关闭；上一轮已发送部署/收网但鱼未入护判 LOST |

---

## 出问题了怎么查

| 现象 | 先看什么 |
|---|---|
| 起来就退出 | 看日志是否 `找不到 RF4S 源码目录`：设置 `RF4S_REF` 或确认 `vendor/`；也可能是 `RF4_PYTHON` 指错 |
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

## 实机与验收分级

| 项目 | 当前状态 |
|---|---|
| 离线测试 | **647 项：640 通过、7 项跳过**；跳过项需要本机真实截图（`_ready.png` 等），这些运行产物不随发布同步 |
| legacy `rf4s_adapter.py` | **历史实机记录：已钓到过小鱼**（仅证明旧路径能完成一次基础循环） |
| 正式 `autofish_bridge.py` | **实机部署/收网已成功发送，但鱼未入护，判 LOST**；不能记作收获，也仍未完成入护验收 |
| 独立 UI | 离线模型/响应式测试通过；UI 只读快照 + 发语义命令；**关闭 UI 后台继续跑** |
| 停止语义 | **停止即松**：优雅停止会释放脚本自己持有的 F5/F6/Shift/F7；没有实例时绝不抢前台发 keyUp（`pagedown` 仅在显式 release 时作为旧残留清理） |
| 抄网 | 正式键位 **F7**（98 入 / 90 出）；实机部署/收网已成功发送，但鱼未入护判 LOST；默认关闭，仍需继续验收 |

> 结论：可以按本文命令启动与联调，但**不能把“离线测试通过”当成生产可靠性声明**，
> 也不能把 legacy 的小鱼记录当成新桥接的入护验收。
>
> 发布文件（相对路径 + sha256）见 [`docs/release-manifest.json`](docs/release-manifest.json)。
> 文字识别（WDAC 禁用 onnxruntime、改用 cv2.dnn + 独立服务）的环境约束、接口与实测见 [`docs/ocr.md`](docs/ocr.md)。

### 自动抄网：默认关闭，仅 RF4_AUTO_NET=1 人工验收

实机已确认：真正掏网/收网键是 **F7**；旧 `PageDown` 会放竿/切装备，脚本不再发送。
上一轮实机部署与收网注入均成功发出，但鱼未进入鱼护，最终判 **LOST**——这既不能
记作“收获”，也不能算入护验收通过。

- **单源开关**：`RF4_AUTO_NET=1` 是唯一生产打开方式，默认 **False**。同一值同时
  传给 Bridge gate 与 `FightConfig.auto_net_enabled`；二者不一致时在 preflight
  直接失败，绝不先松 F5 再报 fault。
- 关闭发生在 **LAND → NET 之前**：临岸到点后仍保持 LAND 与 F5+F6，不松收线键、
  不进 NET、不发 F7；原因栏显示「临岸候选，自动抄网待验证；继续收线提竿」。
- 只把 `net_action=None` 不够：旧路径仍会先松 F5 再进 NET，所以必须在状态迁移
  前 gate；且掏网/收网回调缺任一都保持 LAND 双键。
- **F7 是裸键**：Ctrl/Alt/Shift 任一被物理按住时不发（否则会撞全局恢复热键
  `Ctrl+Alt+Shift+F7`），明确报 `net_modifier_held` 且不替用户松开任何键。
- 打开后 NET 是**连续会话**，不是永久人工挂起：
  - 正常新钓鱼会话初始假定 `STOWED`；中途接管是 `UNKNOWN`，不盲 toggle。
  - **进入阈值** `ring_min` 默认 **0.98**：0.973 不 LAND；0.985 需连续 3 帧才 LAND。
  - 掏网成功才记 `DEPLOYED`；NET 中新鲜有效帧 ring 连续 3 帧低于
    **独立的退出阈值** `net_exit_ring`（默认 **0.90**）才发一次收网 F7；
    即 **98 入 / 90 出** 的回差，0.89 连续 3 帧触发收网。
  - 收网成功后等待 1.2s 动画再回 FIGHT、恢复 F5；失败/取消保持 F6 +
    `UNKNOWN fault`，不恢复 F5、不重复盲 toggle，stop 可释放。
  - `None/NaN/失焦/过时/重复` 帧不算低环；捕获强确认优先于收网。
- `RF4_AUTO_NET=1` **仅供人工实机验收**；当前未完成实机验证，不得无人值守启用。
  状态机 `snapshot` 中的 `net_state` / `net_state_basis` 只是我方动作账
  （assumed/commanded），不是游戏画面确认。
- `pagedown` 仅作为旧版本残留键保留在 `bot_ctl release` 的显式清理列表里，
  正式 NET 动作绝不发送。

---

## 已知限制

- 只适配 **1920×1080** 客户区，所有坐标都是按这个尺寸量的
- **正式桥接不动游戏窗布局**：只读校验尺寸，不 resize/move；legacy 适配器才会拉齐窗口
- `static/ready_cn.png` 是从 **1920×1080** 中文客户端单帧裁出的底部 ROI（约 532,1017–704,1044）：
  - 只在 READY/未抛竿判定中使用，模板方差不足、路径缺失或尺寸不符时**直接判定模板不可用**
    （fail-closed，不会猜 READY，也不会崩溃）
  - 分辨率、UI 缩放、语言或 HUD 样式变化都会让模板失效，需要重新裁模板
- 只写了**底钓单竿**，海钓 / 路亚没有
- 线长圆环 `<10%` 的区间还没有足够实测样本，阈值是保守估计
- 结算框按钮位置靠"扫亮边边框"推断，UI 大改就失效
- 摩擦片依赖滚轮，**滚轮是否真的送达游戏尚未验证**（档位没有屏幕读数可对账）
- 中文客户端专用：`ready.png` 匹配不到（走了等价判据或 `ready_cn.png` 模板）；
  `wheel.png` 本身能匹配（它就是线杯图标），只是需要防抖
- 失焦时 AUTO 会暂停并自动恢复；`OBSERVE` / `MANUAL` 不会自动恢复，必须显式切回

---

## License

外层适配层为 MIT；`vendor/` 内的上游 RF4S 快照遵循 **GPL-3.0**（见 `vendor/LICENSE`）。两套许可并存，分发时需一并携带。
