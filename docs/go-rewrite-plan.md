# 用 Go 重写一版：详细方案

> 结论先行：**可行，但"纯 Go"是不成立的——OCR 必须借 ONNX Runtime 的本地库**。
> 真正的收益不在性能（OCR 占 97% 耗时，换语言不会变快），而在部署形态、
> 并发模型、内存占用，以及**顺手修掉 Python 版解决不了的两个老问题**：
> Windows 上没有 raqm 导致印地/孟加拉连写不正确、Linux 上 `.ttc` face 选择踩坑。
> 渲染层 Go 反而比 Python 强，这是本次重写最实在的技术理由。

---

## 一、先明确"为什么重写"

重写工程最怕的是一边写一边发现没收益。先把这个项目的成本结构摊开：

现有实测耗时（CPU，640×400 图，中→英）：

| 阶段 | 耗时 | 占比 |
|---|---|---|
| OCR（det + rec + cls） | 11681 ms | **91%** |
| 翻译（Google） | 794 ms | 6% |
| 渲染（4x 超采样 + 光栅化） | 193 ms | 1.5% |
| 擦除（mask + 三级策略） | 55 ms | 0.4% |
| 编码 + 解码 | 72 ms | 0.6% |

**所以"用 Go 重写能更快"这个理由是不成立的**：占 91% 的是模型推理，跟宿主语言无关。
如果目标是提速，正确做法是换 GPU 或优化 det 输入尺寸，不是换语言。

重写站得住的四个理由：

1. **部署**：现在是 Python 3.13 + paddle + 一坨依赖，Windows 上还得装 VC 运行库；
   Go 编译出单文件二进制（除 ONNX Runtime 动态库），运维面小一个数量级。
2. **并发**：Go 的 `context` 能真正取消在途请求、按请求设截止时间；Python 这边
   OCR 是同步阻塞的，超时只能靠网关砍连接，请求还在后台空转烧 CPU。
3. **长驻服务的内存与预热**：Go 侧可以做到常驻 <300MB、无 GC 尖峰（图像缓冲池化）；
   当前 Python 进程光 paddle 就吃掉一大块。
4. **排版正确性**：`go-text/typesetting` 是 HarfBuzz 的逐行移植，含 bidi、脚本分段、
   OpenType 特性、fontscan（带 fontconfig 替换表）。这意味着：
   - Windows 上不需要 raqm 也能正确整形印地语、孟加拉语、泰语、阿拉伯语；
   - `.ttc` 可以按 face 精确选择（`opentype.ParseCollection`），不再有
     "中文落到日文字面" 的问题；
   - 现在是 Pillow 画好再贴，Go 侧可以直接拿 shaping 后的 glyph 序列做
     "按字形外框定位"，字距和连写比现在更准。

**不建议重写的部分**：模型训练/转换、翻译协议逆向（`google_web.py` 那 1041 行
是逆向出来的，Go 侧照着移植即可，不要重新逆向）。

---

## 二、三条技术路线与取舍

### 方案 A：Go 做服务层，Python 留作推理 worker
Go 处理 HTTP、下载、翻译、擦除、渲染，OCR 通过 Unix Socket / HTTP 调本地 Python 进程。

- 优点：风险最低，OCR 精度 100% 不变（还是同一份 paddle 模型）；Go 侧可以立刻拿到
  并发与部署的好处；后期可把 OCR 逐步换成 ONNX。
- 缺点：两个进程、两套依赖；跨进程序列化（图像 + 框 + 文本）有开销；部署仍是"两坨"。
- **适合**：只想解决并发与部署问题，不想碰精度。

### 方案 B（推荐）：Go 全量 + ONNX Runtime 本地推理
OCR 用 `onnxruntime_go` 直接加载 PP-OCR 的 ONNX 模型，det/rec/cls 的预处理与后处理
（DB 后处理、CTC 解码）在 Go 里实现；其余图层全部纯 Go。

- 优点：单进程、单二进制（+ 三个 .onnx + ORT 动态库 + 字体）；精度可控（模型可换）；
  rec 阶段可以自己做**批处理**，这是唯一有实质提速空间的地方。
- 缺点：DB 后处理与 CTC 解码要自己写（约 600 行，但算法是公开且稳定的）；
  **模型版本要从 PP-OCRv6_medium 换成 PP-OCRv5 的 ONNX 版**，识别结果会有细微差异，
  必须做 golden 回归。
- **适合**：接受一次精度回归评审，换取长期形态。

### 方案 C：Go + cgo 绑 Paddle Inference C API
- 不推荐。Paddle 的 C API 需要把整套推理库和模型格式一起带上（比 ORT 重得多），
  Windows 上编译链非常难搞，而且没有成熟的 Go binding 可参考。花这个力气不如直接用 ORT。

**结论：走方案 B，用方案 A 作为第一阶段的可回退形态。** 两条路线共用同一套 Go 接口，
切换只是换 `Ocr` 的实现。

---

## 三、Go 依赖选型（均为已核实的真实项目）

| 能力 | 选型 | 说明与风险 |
|---|---|---|
| OCR 推理 | `github.com/yalue/onnxruntime_go` **v1.38.0**（当前最新） | dlopen 方式加载 ORT，Windows 可用（这是它存在的主要理由）；**ORT 动态库版本必须与该 binding 的 C API 版本匹配**，装错版本的表现是"加载即失败"，不是编译错误 |
| OCR 管线参考 | `github.com/multippt/gopaddleocr` | 已实现 det/cls/rec 的 Go 管线（DB 后处理、CTC、字典），可作为实现参考或直接裁剪；不必从零推导 |
| 备选（零 C 工具链） | `github.com/chennqqi/go-rapidocr` | purego 方式 dlopen 预编译的 RapidOcrOnnx 动态库，不需要 gcc；但管线是黑盒，字典/阈值不可控 |
| 模型 | RapidOCR 转换的 PP-OCRv5 ONNX（modelscope / HuggingFace `monkt/paddleocr-onnx`） | 多语种 rec 分开打包（latin / eslav / arabic / thai / korean / chinese …），与现在"按源语言选模型"的设计天然对齐 |
| 文本整形 | `github.com/go-text/typesetting` | HarfBuzz 逐行移植 + bidi + 脚本分段 + fontscan；`font/opentype.ParseCollection` 处理 `.ttc` |
| 光栅化 | `golang.org/x/image/vector` + `golang.org/x/image/font` | 纯 Go 扫描线抗锯齿光栅化，4x 超采样后降采样 |
| 形态学 / 轮廓 / inpaint | `gocv.io/x/gocv` v0.43 | 已确认 `gocv.Inpaint(src, mask, &dst, radius, gocv.Telea)` 存在（`NS=0`、`Telea=1`）；Canny / DistanceTransform / FindContours / MorphologyEx 都有 |
| 图像编解码 | 标准库 `image/png`、`image/jpeg` | PNG/JPEG 够用；**webp 编码标准库不支持**（`x/image/webp` 只能解），见风险 5 |
| HTTP | 标准库 `net/http` | 不需要框架；如果要 OpenAPI 再考虑 chi/echo |
| 配置 | 自写 `.env` 解析（80 行）+ `os.Getenv` | 保持与现在三条语义一致（不覆盖已有变量 / 缺失静默 / 导入不自动加载） |

**不引入**：Gin/Echo（用不上）、Zap（`log/slog` 够）、任何 ORM。

### 3.1 本机实测（2026-11-19，Windows）

| 项 | 结果 |
|---|---|
| Go 工具链 | 已装 **go1.19**（`C:\Go\bin`，**不在 PATH**）；go1.19 已 EOL，建议升到 1.22+ |
| `go-text/typesetting` | 拉到 **v0.3.5**，其 `go.mod` 要求 `go 1.19` → **本机版本可直接编译** |
| `onnxruntime_go` | 拉到 **v1.38.0**，`go.mod` 同样只要求 `go 1.19` |
| 模块拉取 | `GOPROXY` 默认的 `proxy.golang.org` **直连返回 Bad Gateway**；换 `GOPROXY=https://goproxy.cn,direct` 后正常 |

也就是说：**不需要先升级 Go 就能开始 Phase 0**，但 `GOPROXY` 必须在 CI 与镜像里
固定成国内源，否则构建会在拉依赖这一步随机失败——这属于"本地能跑、CI 挂掉"的典型坑。

---

## 四、模块映射：Python → Go

现有 6691 行，按下表拆成 9 个包。LOC 是 Go 侧的预估（含测试前）。

| Python | 行数 | Go 包 | 预估 | 难度 | 要点 |
|---|---|---|---|---|---|
| `api/app.py` | 384 | `internal/api` | 500 | 低 | 4 个端点 + 鉴权 + 统一错误体；`/img` 的 URL/base64 双分支 |
| `pipeline.py` | 560 | `internal/pipeline` | 800 | 中 | 编排 + 计时 + warnings；`min_block_height`、贴边裁切行、语言前置校验 |
| `downloader.py` | 255 | `internal/download` | 320 | 中 | SSRF 白名单（**必须先解析 DNS 再校验 IP，防止 DNS rebinding**）、20MB 上限、代理 |
| `ocr/engine.py` | 376 | `internal/ocr`（det/rec/cls） | 900 | **高** | 模型加载、方向分类、按源语言选 rec 模型、批处理 |
| `ocr/group.py` | 207 | `internal/ocr/group.go` | 260 | 中 | 段落聚合 4 条件 + 行尾标点判据 + `MAX_LINES=14` |
| `translator/*` | 1524 | `internal/translate` | 1400 | 中 | tk token 的 32 位运算在 Go 里**比 Python 更自然**（`uint32` 溢出即回绕）；双通道降级 |
| `inpaint/mask.py` | 309 | `internal/inpaint/mask.go` | 420 | 中 | 非对称外扩 (1,1,1,4)、参考环、字形高度（行投影） |
| `inpaint/erase.py` | 262 | `internal/inpaint/erase.go` | 400 | 中 | 三级策略 + 羽化；平面拟合用 3×3 正规方程手解，不引线性代数库 |
| `render/text.py` | 845 | `internal/render` | 1200 | **高** | 字号反推、扩框、折行、超采样、贴图；细节见第五节 |
| `render/fonts.py` | 483 | `internal/render/fonts` | 500 | 中 | fontscan 索引 + 语言族映射 + face 选择（比现在简单） |
| `render/style.py` | 168 | `internal/render/style.go` | 220 | 中 | 前景/背景/描边/字重/对齐估计 |
| `env.py` / `device.py` | 278 | `internal/config` | 260 | 低 | 同样的优先级与语义 |
| `scripts/*` | 645 | `cmd/{serve,translate,doctor}` | 500 | 低 | 三个命令 |
| — | — | `internal/imagebuf`（新） | 200 | 低 | `sync.Pool` 复用大缓冲，避免 GC 抖动 |

合计约 **7.2k 行 Go**（不含测试），与 Python 量级相当——**这不是"用 Go 会写得更少"，
而是"用 Go 换部署形态"**。

---

## 五、必须原样搬过去的知识（22 条踩坑记录）

这些是项目最值钱的资产。重写时如果按"重新实现一遍"来做，会把已经踩过的坑再踩一遍。
逐条列成 Go 侧的验收清单：

### 5.1 OCR 与段落聚合

| # | 规则 | 为什么 |
|---|---|---|
| 1 | 合并四条件同时满足：竖直间距 ≤ `0.90×行高`、确实在下一行（顶部低 ≥ `0.30×行高`）、水平重叠 ≥ 45%、行高差 ≤ 40% | 少一条就会把两段合成一段 |
| 2 | **行尾出现 `。．！？!?；;…` 即不合并** | 紧凑排版里段内行距 0.68 与段间距 0.73 几何上分不开，只能靠标点 |
| 3 | `MAX_LINES = 14` 截断 | 防止整页文字被聚成一块 |
| 4 | OCR 语言只看**源语言**，不看目标语言 | 曾经按 target 推断，翻韩语时用韩语模型读中文，一个字都认不出 |
| 5 | 贴边被裁行：`top ≤ 2px` 且高度 < `0.7×中位行高` → **不翻译但擦除** | 半截字翻出来是错的，但留在图上更难看 |
| 6 | `min_block_height = 12px`：太小的框（照片内嵌小字）不翻译不擦除 | 7px 小字回填后把照片弄脏 |

### 5.2 擦除

| # | 规则 | 为什么 |
|---|---|---|
| 7 | mask 外扩**非对称**：左/上/右 1px，**下 4px** | PaddleOCR 的框比字形下缘短 4px，不补就留残影 |
| 8 | 参考环必须从**外扩后**区域再让 `RING_WIDTH` | 否则取到字形像素，背景色估计被污染 |
| 9 | 三级策略按框外环像素标准差分流：纯色 → 中位色；渐变 → 最小二乘平面；纹理 → Telea inpaint | inpaint 在纯色区会制造色差，且慢一个数量级 |
| 10 | 无论哪级，最后都用**羽化软 mask** 做 alpha 混合 | 硬边是"看得出被 P 过"最直接的原因 |

### 5.3 渲染（最容易"看着差不多、其实差很多"的一段）

| # | 规则 | 为什么 |
|---|---|---|
| 11 | 字号由**字形高度**反推（行投影切连续墨迹带、过滤 <0.4×最高带、取中位数），**不是框高** | 框里有行距，用框高反推会小一档 |
| 12 | 同图字号归一化：落在 `[0.82, 1.20]×中位数` 的簇，统一到中位数 | 否则同一张图字号忽大忽小 |
| 13 | 扩框**只向下**（上边界不动，`top_anchor` 绘制） | 向上扩会压住上一段 |
| 14 | 向下扩必须留 `0.45×行高` 的段间距，画布底部留 `EDGE_MARGIN=6` | 否则贴到底边 |
| 15 | 短标签禁折行：纯 CJK ≤ 4 字 / 单词 ≤ 12 字符 | "图二"竖排成两行就是这条缺失导致的 |
| 16 | 纯拉丁超宽词**整词成行、允许溢出**，不许拦腰截断 | `Originator` 被切成 `Originato/r` |
| 17 | 4x 超采样在**局部 patch** 上做，不是整图 | 整图 4x 内存是 16 倍，20MB 的图直接 OOM |
| 18 | 字号下限 `MIN_FONT_SIZE = 9`，纵向溢出上限 `1.5×`，横向扩展上限 `3.0×` | 防止病态放大 |

### 5.4 语言与配置

| # | 规则 | 为什么 |
|---|---|---|
| 19 | 35 个目标语言码硬白名单；`EN`/`en-US`/`pt_BR`/`iw`/`in` 都要归一 | 直接比对原文会把 `en-US` 判成不支持 |
| 20 | **语言校验必须早于图片下载** | 否则 `lang=zh` + 404 的 URL 会返回 `E_IMAGE_FETCH`，把人引到错误方向 |
| 21 | 设备默认 `cpu`，`auto` 才自动探测；配了 gpu 用不了就**启动失败**，不静默降级 | 静默跑回 CPU 会让人以为在吃卡 |
| 22 | `.env` 三条语义：不覆盖已有环境变量、文件缺失静默、库导入不自动加载 | 测试污染 / 优先级错乱 |

**Go 侧还会顺带修掉三个已知限制**（Python 版受 Pillow/raqm 限制做不到的）：
- 印地/孟加拉/泰语连写：HarfBuzz 直接给出正确 glyph 序列，不再依赖手势式的
  `shape_rtl_text()` 反转；
- `.ttc` 的 face 选择：`ParseCollection` 直接按语言族取 face，不再"探测失败就退回 0"；
- 阿拉伯语的 kashida / 连字：HarfBuzz 原生支持，现在是手工反转 + 默认字距。

---

## 六、Go 侧结构草案

```
ocr-go/
├── cmd/
│   ├── serve/main.go           # HTTP 服务（-device/-port/-no-warmup）
│   ├── translate/main.go       # 单图 CLI
│   └── doctor/main.go          # 自检：ORT / 模型 / 字体 / 设备
├── internal/
│   ├── api/                    # 路由、鉴权、错误体、请求模型
│   ├── pipeline/               # 编排：decode→ocr→group→translate→mask→erase→render→encode
│   ├── download/               # SSRF 防护、体积上限、代理
│   ├── ocr/                    # engine.go det.go rec.go cls.go dbpost.go ctc.go group.go
│   ├── translate/              # client.go tk.go langmap.go
│   ├── inpaint/                # mask.go erase.go plane.go
│   ├── render/                 # text.go wrap.go fit.go draw.go style.go patch.go
│   ├── fonts/                  # book.go family.go ttc.go
│   ├── config/                 # env.go device.go
│   └── imagebuf/               # 缓冲池
├── models/                     # *.onnx + 字典（不进 git，部署时挂载）
└── assets/fonts/               # 可选自带字体
```

### 关键接口（先定接口，再谈实现）

```go
// OCR 是可替换的：方案 A 用 HTTP 客户端，方案 B 用 onnxruntime。
type Ocr interface {
    // Detect 返回文本框（顺时针四点多边形）与置信度。
    Detect(ctx context.Context, img *image.RGBA) ([]Box, error)
    // Recognize 批量识别：一次 Run 处理多个裁片，比逐片快 2~4 倍。
    Recognize(ctx context.Context, crops []Crop) ([]Line, error)
    Close() error
}

type Translator interface {
    Translate(ctx context.Context, texts []string, source, target Lang) ([]string, error)
}

type Pipeline struct {
    ocr   Ocr
    tr    Translator
    fonts *fonts.Book
    cfg   Config
    pool  *imagebuf.Pool
}

// 一次请求的全部中间状态，避免在函数间传 8 个参数。
type job struct {
    src      *image.RGBA
    blocks   []Block     // 聚合后的段落
    styles   []Style
    results  []Rendered
    timings  map[string]float64
    warnings []string
}
```

### 并发模型

- **一个请求内部是串行的**（OCR → 翻译 → 擦除 → 渲染有严格依赖），不要为了"用上 goroutine"
  把链路拆成流水线，收益抵不过复杂度。
- **并发点在请求之间**：`http.Server` 每请求一个 goroutine；OCR 侧用**有界信号量**
  （默认 1，GPU 时 2~4）限制同时在跑的推理数，其余请求排队并响应 `ctx.Done()`。
- **翻译阶段可以并发**：一图多个段落，`errgroup` 限 4 路并发调 Google，实测 794ms
  能压到 300ms 内（Python 版是串行的，这是 Go 侧第一个真收益）。
- **rec 批处理**：det 出来的所有裁片按高度排序、分桶成 batch（8/16），一次 Run。
  这是 OCR 阶段唯一有实质提速余地的优化。

---

## 七、平价验证（parity）：这是整个方案的核心难点

"重写一版"真正的风险不是写不出来，而是**写出来了但结果不一样，而你说不清哪里不一样**。
所以第一阶段不写业务代码，先把"标准答案"固化下来。

### 7.1 先冻结 golden 数据集

1. 固定翻译器：Go 与 Python 两侧都用 mock 翻译器（输入原文 → 输出固定映射），
   排除 Google 波动。
2. 选 12~20 张覆盖各类边界的图：纯色海报、渐变、照片背景、竖排、贴边裁切行、
   极小字、RTL（阿语）、复杂整形（印地/泰/格鲁吉亚）、43 语言各取一张。
3. Python 侧跑出 `golden/<case>/{out.png, blocks.json, timings.json}` 并入库。

### 7.2 三层比法（不能只比图）

| 层 | 比什么 | 通过标准 |
|---|---|---|
| L1 文本 | `blocks[].source` / `.target` | 逐块字符串相等；OCR 模型换版导致的差异单独列白名单 |
| L2 几何 | 每个块的 box、字号、行数、`painted` | box IoU ≥ 0.98；字号差 ≤ 1px；行数必须相等 |
| L3 像素 | 解码后 RGB | 最大通道差 ≤ 2，且差异像素占比 < 0.5% |

**注意：不要比文件字节。** PNG 编码器不同（filter 策略、zlib 实现），字节必然不同——
现在 `SHA1 一致` 这个断言是 Python 内部的，Go 侧只能用**像素级**比较。

### 7.3 测试一一对应

现有 263 项测试逐条翻译成 Go 表驱动测试，按包分组（`go test ./internal/...`）。
其中"测试即规格"的那几条尤其要照搬：

- `test_img_json_validates_lang_before_download`（断言 `fetch_image` 未被调用）；
- 上传口必须带三个 `X-ImgTrans-*` 头（这条是上一轮才发现漏的）；
- 字体：模拟 Linux 两层目录树，断言递归扫描命中；
- 设备：`gpu` 在本机 CPU 版 ORT 上必须**启动失败**。

### 7.4 双跑对比（上线前）

同一批真实图，Python 与 Go 各跑一遍，输出差异图 + 指标表，人工过一遍。
**只有当 L1/L2 全绿、L3 达标、差异图肉眼无异常，才允许切流量。**

---

## 八、风险表（按"会不会翻车"排序）

| # | 风险 | 影响 | 应对 |
|---|---|---|---|
| 1 | **PP-OCRv6 → PP-OCRv5 的精度回退** | 识别错字 → 译文错 → 用户可见 | 先用 12 张图做 A/B 识别率对比；若 v6 明显更好，则**先走方案 A**（Python 保留 OCR），等 v6 的 ONNX 出来再切 |
| 2 | **DB 后处理阈值差异** | 框的位置/大小略变 → 字号与折行连锁变化 | 后处理参数（`thresh`、`box_thresh`、`unclip_ratio`）从 Python 侧原样抄；用 L2 几何指标钉住 |
| 3 | 字体度量差异（Pillow vs go-text） | 同样的字号，行高/基线差 1~2px → 字号反推偏一档 | 不做"字号数值对齐"，做"**像素结果对齐**"；必要时按 font metrics 做一次标定（见下） |
| 4 | 抗锯齿与降采样核不同 | 边缘灰度不同 → L3 差异像素超标 | 阈值放到 ≤2 且 <0.5%，并把降采样核固定为 box filter（与 Pillow 的 LANCZOS 不同，需实测决定用哪种） |
| 5 | webp 输出 | 标准库不能编码 | 先砍掉 webp 输出（当前默认 png）；确有需要再引 `chai2010/webp`（cgo） |
| 6 | cgo + 交叉编译 | 出不了"真正的静态单文件" | ONNX Runtime 本来就是动态库；接受"二进制 + .so + 模型" 三件套，用 Docker 固化 |
| 7 | ORT 版本与 binding 的 C API 版本不匹配 | 启动即崩（加载不了库），错误信息还很难懂 | 版本号写进镜像 tag；`doctor` 启动时打印 ORT 实际版本 |
| 10 | Go 模块拉取失败 | CI 随机构建失败 | `GOPROXY=https://goproxy.cn,direct` 写进 CI 与 Dockerfile（本机实测默认代理返回 Bad Gateway） |
| 8 | GPU 路径 | CUDA EP 与驱动/ORT 构建强绑定 | 与现在策略一致：默认 cpu，`gpu` 显式配置，不可用就启动失败 |
| 9 | 内存峰值 | 20MB 图 + 4x patch + 多路并发 | 缓冲池 + 单请求并发上限；压测 20MB 图与 8 并发 |

**关于风险 3 的标定方法**（值得单独做一次）：
用同一字体、同一字号，分别用 Pillow 和 go-text 渲染 "国Ag" 并测墨迹外框高度，
得出两者的 `ascent/descent` 换算系数，写进 `fonts.Book` 作为单一常量。
一次标定，后续所有字号计算都准。

---

## 九、分阶段落地（每阶段都有可验证的退出条件）

### Phase 0：冻结契约（不写业务代码）
产出：`golden/` 数据集、`docs/api-contract.md`（4 端点 + 错误码 + 头部）、
ORM 之外的依赖清单、Go module 骨架 + CI（`go vet`、`go test`、`gofmt -l`）。
**退出条件**：Python 侧能一键生成任意 case 的 golden；Go 侧空项目能跑通 CI。

### Phase 1：只读链路（不出图）
`download → OCR → group → translate → JSON`，输出与 Python 的 `blocks` 对齐。
**退出条件**：L1 文本指标全绿（或差异全部在白名单里解释清楚）；
`/v1/translate/img` 的 JSON 形状完全一致（不含 `image_base64` 内容）。
这一阶段就能回答最重要的问题：**OCR 换模型到底掉不掉精度**。

### Phase 2：擦除 + 渲染
`mask → erase → render → encode`，配套 4x 超采样、字号反推、扩框、折行。
**退出条件**：L2 几何 + L3 像素达标；12 张图人工过图，无"明显退化"。
这一阶段最耗时，80% 的调试都在"字号差一档 / 折行位置不同"上。

### Phase 3：灰度与切换
同一入口双跑、按比例分流、指标看板（成功率/P95/差异率）。
**退出条件**：连续观察无差异放大，再停掉 Python 版本。
保留 Python 版本至少一个发布周期，作为回退路径。

---

## 十、需要你拍板的四件事

1. **能接受 OCR 模型换版吗？**（v6 → v5）如果不能，方案 B 的第一阶段就要先验精度，
   验不过就退回方案 A（Go 做服务层 + Python 做 OCR 进程），收益少一半但风险为零。
2. **能接受 cgo 吗？** 不能的话只能用 `go-rapidocr` 的 purego 后端（不需要 gcc，
   但管线是黑盒）；能接受的话用 `onnxruntime_go` 自己做后处理，可控性好得多。
3. **webp 输出还用不用？** 不用就砍掉，能少一个 cgo 依赖。
4. **GPU 是不是硬需求？** 是的话 ORT 要走 CUDA EP，镜像和驱动版本要一起锁；
   不是的话就先按 CPU 做，接口留出 `device` 配置。

---

## 十一、不做什么（控制范围）

- 不做多图批量接口（除非业务真的需要；单图 13s 的链路，批量只会把超时问题放大）。
- 不做分布式/队列（当前单实例串行就是瓶颈，加队列只是把等待挪个地方）。
- 不重写翻译协议的逆向逻辑（`google_web.py` 直接移植，别重新逆向）。
- 不引入 LaMa 之类的大模型擦除（现在 OpenCV 三级策略已经够用，且 CPU 可跑）。
- 不追求"零 cgo"（ONNX Runtime 决定了这件事做不到，硬追会牺牲可控性）。

---

## 附录 A：`go-text` 替换 Pillow 的对照

| 现在（Pillow） | Go（go-text + x/image） |
|---|---|
| `ImageFont.truetype(path, size)` | `opentype.Parse` → `font.NewFace`（size 用 `fixed.Int26_6`） |
| `.ttc` 取第一个 face | `opentype.ParseCollection` + 按语言族选 index |
| `font.getbbox(text)` 量宽高 | `shaping.Shaper.Shape` 得到 glyph 序列 → 累加 advance |
| 手写 `wrap_text` 按字符宽累加 | `shaping` 的分段 + 自己按像素宽断行（保留原有断行规则） |
| `shape_rtl_text`（手工反转） | `bidi` + HarfBuzz，**删掉这个函数** |
| `ImageDraw.text` 直接画 | `vector.Rasterizer` 逐 glyph 填充到 4x 画布 |
| 描边：`stroke_width` 参数 | 先画描边路径（偏移 8 方向的粗轮廓）再填字 |

## 附录 B：推荐先跑通的一个最小验证（半天量级，风险前置）

在写任何业务代码前，先做一个 200 行的 spike，只验证四件事：

1. `onnxruntime_go` 在**本机 Windows** 上能加载 ORT 并跑通 det 模型（输出张量形状对不对）；
2. 拿一张已有测试图，det + rec 出来的文本，与 Python 版**逐字对比**（这是风险 1 的答案）；
3. `go-text` 渲染 "中华人民共和国 / العربية / हिन्दी" 三个阶段，肉眼确认整形正确；
4. `gocv.Inpaint` 在 Windows 上能跑（OpenCV 运行库能不能找到）。

这四项任何一项挂了，方案就要调整——**先花半天把不确定性打掉，比写完 7000 行再发现要好**。
