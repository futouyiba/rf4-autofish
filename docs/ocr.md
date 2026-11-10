# 本地中文 OCR —— 为什么绕开 onnxruntime，怎么用，有多快

> 本文说明这套 OCR 的**环境约束、实现边界与实测口径**。接口以
> `autofish_ocr.py` / `autofish_ocr_client.py` / `autofish_ocr_service.py`
> 为准；代码与本文不一致时，先改代码或先改本文，不许各说各话。

---

## 1. 结论先行

- **不能用 onnxruntime**：本机启用了 WDAC 代码完整性策略（`enforcement=2`），
  未签名的 `onnxruntime` DLL 会被系统直接拒绝加载，报 `WinError 5（拒绝访问）`，
  装哪个版本都一样，换 pip 源也一样。
- **游戏 venv 里的 cv2 4.8 也跑不了 rec**：能加载模型，但推理时 `MatMul`
  算子不支持，rec 阶段直接失败。
- **最终方案**：用 `cv2.dnn` 直接跑 PP-OCR 的 det/rec ONNX，
  OCR 放在 `bot/venv-ocr`（cv2 5.0）的**独立服务进程**里，主进程通过
  stdin/stdout 的 JSON 管道调用。主进程（游戏 venv）保持 numpy 1.26 + cv2 4.8 不动。
- **模型随仓库分发、离线可用**，运行时**不下载**。

---

## 2. 为什么是这套组合（三次踩坑）

| 尝试 | 结果 | 原因 |
|---|---|---|
| `pip install onnxruntime`（任意版本） | 失败 | WDAC `enforcement=2` 拒绝加载未签名 DLL，`WinError 5` |
| 游戏 venv（cv2 4.8）直接跑 PP-OCR rec | 失败 | 该版本 dnn 不支持 rec 模型里的 `MatMul` |
| `bot/venv-ocr`（cv2 5.0）+ `cv2.dnn` | 可用 | cv2 5.0 的 dnn 支持所需算子 |

因此不是"选型偏好"，是**本机唯一能跑通的路径**。任何"顺手装个
onnxruntime 更省事"的想法都会回到 `WinError 5`，不要再试。

---

## 3. 架构

```
主进程（游戏 venv：numpy 1.26.4 + cv2 4.8）
  └─ autofish_ocr_client.OcrClient
       │  裁 ROI → PNG → base64 → 一行 JSON
       ▼
     stdin/stdout（subprocess，cwd=bot/）
       ▲
  └─ autofish_ocr_service.py（venv-ocr：cv2 5.0）
       └─ autofish_ocr.OcrEngine（cv2.dnn + PP-OCR v6 small）
```

- **只传 ROI，不传整帧**：客户端先按 `roi=(x, y, w, h)` 裁剪再 PNG 编码，
  小区域通常只有几 KB。
- **服务常驻**：进程启动时加载模型并回一行 `ready`，之后逐请求处理。
- **失败不抛异常**：客户端约定"读不到就返回空"，超时（默认 2s）会杀掉服务，
  下次调用再懒启动，绝不阻塞钓鱼主循环。
- **崩溃后懒重启**：`_ensure_locked()` 在下次调用时重建进程；但**启动失败**会记住
  `_start_failed`，本次进程内不再重试。

---

## 4. 模型

目录：`bot/models/ocr/`（发布目录同构）。

| 文件 | 大小 | 用途 |
|---|---|---|
| `PP-OCRv6_det_small.onnx` | 约 9.5 MB | 文本检测 |
| `PP-OCRv6_rec_small.onnx` | 约 21 MB | 文本识别 |
| `ch_PP-OCRv4_det_infer.onnx` | 约 4.7 MB | v4 备份（det） |
| `ch_PP-OCRv4_rec_infer.onnx` | 约 10.8 MB | v4 备份（rec） |

- 来自 `rapidocr` 自带的 PP-OCRv6 small，已复制进仓库目录，**离线、不联网下载**。
- 字符表不单独存文件：`_load_charset()` 从 rec 模型 ONNX 元数据的
  `character` 字段读出，`index 0` 视为 CTC blank。
- **onnx 不入 git**：发布仓库 `.gitignore` 里加了 `/models/ocr/*.onnx`，
  避免每次提交带几十 MB 二进制；本地/部署时把模型放到该目录即可。
- 缺模型时 `OcrEngine` 抛 `FileNotFoundError`，服务进程回 `engine_init: ...`
  后退出，客户端记为不可用（不重试、不崩主流程）。

---

## 5. 接口

### 5.1 `autofish_ocr.OcrEngine`（进程内，线程不安全）

```python
engine = OcrEngine(model_dir=None, det_name="PP-OCRv6_det_small.onnx",
                   rec_name="PP-OCRv6_rec_small.onnx", limit_side=736)

items = engine.read(image, roi=(x, y, w, h), min_score=0.5)   # List[TextItem]
text, score = engine.read_line(image, roi=(x, y, w, h), min_score=0.3)
text = engine.read_text(image, roi=None, min_score=0.5)
```

- `TextItem(text: str, score: float, box: (x, y, w, h))`，`box` 是**相对传入
  图片/ROI** 的坐标；`read()` 结果按"先上后下、先左后右"排序。
- `read()`：先 det 再逐框 rec，适合未知版式。
- `read_line()`：**跳过检测**，直接把 ROI 当一行送 rec；一行里的多段文字会拼成
  一个字符串返回（如 `33,528.34 硬币`）。
- 检测端短边缩放到 `DET_LIMIT_SIDE=736`，但**最多放大 2.5 倍**；识别端固定
  高 `48`、宽上限 `320`。

### 5.2 `autofish_ocr_client.OcrClient`（主进程用，线程安全）

```python
ocr = OcrClient(python_exe=None, service=None, timeout=2.0)
ocr.available                     # 属性：venv-ocr 与服务脚本是否都在
text, score = ocr.read_line(frame, roi=(x, y, w, h), min_score=0.3)
items = ocr.read_block(frame, roi=(x, y, w, h), min_score=0.5)  # [{'text','score','box'}]
text = ocr.read_block_text(frame, roi=None, min_score=0.5)
ocr.close()
```

- 默认解释器：`bot/venv-ocr/Scripts/python.exe`；默认服务：
  `bot/autofish_ocr_service.py`（都在 `HERE` 下解析，无硬编码绝对路径）。
- `read_line()` 读不到返回 `("", 0.0)`；`read_block()` 读不到返回 `[]`。
- `read_block()` 的 `box` 是**服务端图片内坐标**，客户端不额外加 ROI 偏移。
- 进程内共享单例：`from autofish_ocr_client import default_client`。

### 5.3 服务协议（一行一个 JSON）

请求：

```json
{"id": 1, "png_b64": "iVBOR...", "mode": "line", "min_score": 0.3}
{"id": 2, "png_b64": "iVBOR...", "mode": "block", "min_score": 0.5}
```

响应：

```json
{"id": 1, "ok": true, "items": [{"text": "17", "score": 0.99, "box": [0, 0, 30, 20]}]}
{"id": 1, "ok": false, "error": "ValueError: png decode failed"}
```

启动时先回 `{"id": 0, "ok": true, "ready": true}`；初始化失败回
`{"id": 0, "ok": false, "error": "engine_init: ..."}`。
`mode="line"` 时 `box` 固定为 `[0, 0, 0, 0]`，只有 `text/score` 有意义。

---

## 6. 实测数据

测试环境：本机，客户区 1920×1080，模型为 PP-OCRv6 small，服务常驻。

| 场景 | 耗时 | 说明 |
|---|---|---|
| ready 提示识别 | score **0.996** | 文本「钓具准备好,可以抛掷一下」 |
| `read_line` 单 ROI | **7–10 ms** | 进程内 `OcrEngine`，不含 IPC/PNG |
| 端到端首个请求 | 约 **156 ms** | 含子进程启动与模型加载 |
| 小 ROI `read` | **16–25 ms** | ROI 很小，det 放大后仍很快 |
| 全屏 1920×1080 `read` | **0.6–1.2 s** | 只在离线/调试用，禁止进主循环 |
| 700×220 区域 `read_block` | 约 **337 ms** | 多行检测的典型代价 |

> 数据口径：进程内耗时是直接调 `OcrEngine`；端到端是 `OcrClient` 首次调用。
> 数值随 ROI 大小、文字行数、机器负载波动，不要当硬指标写进判据。

---

## 7. 使用策略（重要）

1. **固定位置的单行读数，一律用 `read_line`**：摩擦档位、坐标、价格、状态提示、
   剩余耐力百分比。它是毫秒级且不依赖检测。
2. **版式未知或可能多行，用小 ROI `read_block`**：只圈可能出现文字的那块，
   不要整屏。700×220 已经要 300ms 级，面积再大就不可接受。
3. **禁止整屏每帧 OCR**：全屏 0.6–1.2s 会让钓鱼主循环直接失去节奏。
4. **不要把 OCR 当唯一判据**：颜色/像素类判据（如线杯圆环）比 OCR 快且稳；
   OCR 适合"必须读文字"的场合，例如 ready 提示、结算弹窗、切鱼/小退界面。
5. **采样而不是轮询**：同一状态用低频（如 0.5–2s 一次）确认，不要每帧都调。

---

## 8. 已知的坑

- **不要往游戏 venv 里 `pip install` 任何会升级 numpy 的包**。装 OCR 相关依赖时
  曾把 numpy 升到 2.x，直接打断 cv2 4.8（`_ARRAY_API not found` 一类报错），
  主脚本起不来。已回滚到 **numpy 1.26.4**。
- 需要新依赖时，装进 `bot/venv-ocr`，不要动游戏 venv。
- `OcrEngine` **线程不安全**，单实例复用；多线程请走 `OcrClient`（内部加锁串行）。
- 服务进程的 stderr 被丢弃（`DEVNULL`）；排查启动问题要临时改成继承 stderr，
  或直接手动跑 `venv-ocr\Scripts\python.exe autofish_ocr_service.py`。
- 空帧、越界 ROI、坏 PNG 都返回空结果，不会抛异常——调用方不要靠异常判错。
- 发布目录**不带 `venv-ocr`**：OCR 依赖本机自建环境；`tests/test_ocr_client.py`
  在没有 venv-ocr 或模型时自动跳过真实识别用例，只跑兜底路径。

---

## 9. 测试

```bash
# 在 bot/ 下（有 venv-ocr 与模型时才会跑真实识别）
python -m unittest tests.test_ocr_client -v
```

覆盖：

- 服务缺失 / 服务脚本缺失 / `frame=None` / ROI 越界 / 重复 `close()`：
  一律返回空结果，不抛异常、不阻塞。
- 真实截图 `_fullflow_fault.png` 的 ready 提示（`ROI=(532, 1014, 200, 32)`）
  必须读出「钓具准备好」，且 `score > 0.8`。
- 合成数字图读得出 `17`；纯灰图读不出任何文字。

发布侧文件与 sha256 见 [`release-manifest.json`](release-manifest.json)。
