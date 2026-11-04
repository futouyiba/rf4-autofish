# vendor/ —— 上游 RF4S 源码（统一管理副本）

## 这是什么

`dereklee0310/RussianFishing4Script`（GPL-3.0）在 `612f501`（v0.11.0）的快照，
外加本仓库运行所需的本地配置。**去掉 `.git` 与 `.venv`，纳入本仓库统一管理。**

原因：适配层（`rf4s_adapter.py`）已经深度依赖上游内部实现（`player.py:490` 的
error_handler 分支、`tackle.py` 的阶段常量等），上游逻辑直接影响整体行为；
靠"运行时打补丁 + 心里记住上游长什么样"已经不可维护。

上游说明见 [RF4S-README.md](RF4S-README.md)。

## 与上游的差异（只有 3 个文件）

| 文件 | 差异 | 原因 |
|---|---|---|
| `config.yaml` | 阈值调低（wheel 匹配 0.98→0.93 等）、BOTTOM profile 定制 | 适配 1920×1080 中文客户端 |
| `run_bot.py` | 新增 | 本地启动入口 |
| `rf4s_patch.py` | 新增 | 历史实验补丁（已被 adapter 取代，保留参考） |

上游其余源码**零改动**——所有适配都在外层 `rf4s_adapter.py` 里以运行时补丁完成。

## 许可证

上游为 **GPL-3.0**（见 `LICENSE`）。本目录代码遵循 GPL-3.0；
外层适配层的 MIT 许可证不覆盖本目录。两套许可并存，分发时需一并携带本目录的 LICENSE。

## 更新上游的流程

```bash
# 在原克隆目录拉新版后，对比差异再同步过来
git -C <原克隆路径> fetch
git -C <原克隆路径> diff HEAD origin/main --stat
# 确认后重新执行快照（保持本文件所列差异）
```

⚠️ 同步后必须重跑适配层的补丁挂载（启动日志里那串 `[patch]` 全绿）才算兼容。
