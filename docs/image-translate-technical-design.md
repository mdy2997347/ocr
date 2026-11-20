# 多语言图片翻译服务 技术方案设计

| 项 | 内容 |
|---|---|
| 文档版本 | v1.2 |
| 日期 | 2026-11-19 |
| 作者 | 架构师 高见远 |
| 状态 | 待评审（翻译模块已实现并通过 115 项单测；OCR/擦除/回填仅设计） |
| 目标读者 | 技术负责人、Python 后端工程师、PHP 业务侧对接人 |
| 关联文档 | `docs/sequence-diagram.mermaid`、`docs/class-diagram.mermaid`、`requirements.lock.txt` |
| 变更记录 | v1.0 云翻译 API 适配层方案 → v1.1 改为「移植 PHP Google 翻译客户端」为主通道 → v1.2：OCR 选用 PP-OCRv6 medium（2026-06 发布）；tk 验证改为 Node 交叉验证（本机无 PHP）；出口可用性升为 P0 阻塞前提；熔断器升 P0；`tkk` 口径统一为字符串；`build_tk`/`TkkProvider` 签名与已实现代码对齐；新增 `requirements.lock.txt` 与 P1 三层拆分 |

---

## 0. 结论速览（TL;DR）

如果只有 5 分钟，请读这一节。

| 决策项 | 结论 | 理由摘要 |
|---|---|---|
| 链路 | **OCR 提取（PaddleOCR）→ 自建 Google 通道翻译（移植 `google.php`）→ 原位回填（LaMa + Pillow）** | 用户明确指定；翻译通道自有，不产生按字符计费成本 |
| OCR | **PaddleOCR 3.7.0 + PP-OCRv6 medium**（中/英/日/46 拉丁语系），其他文字系统自动路由到 **PP-OCRv5 多语种 rec** | 唯一同时具备行级多边形 + 多语种 + Apache 2.0 可商用的成熟方案；显存仅数百 MB |
| 翻译主通道 | **`api` 通道（`client=te`）**：一次请求可带多个同名 `q` 参数，**返回数组与入参顺序严格一一对应** | 这是图片翻译最需要的特性——20 个文本块 1 次请求翻完且不错位，比逐条翻译快 20 倍 |
| 翻译降级链 | `api` → `free`（`client=at`）→ 百度/Google Cloud → **原文占位** | Google 免费通道随时可能 403/429/改 tk，必须有确定性兜底，且兜底不能打乱块序 |
| **网络出口** | **本机无法直连 Google（实测 curl 返回 000 / 8 s 超时），必须经海外出口（境外节点代理或商业代理池）** | 这是可用性前提而非优化项；未打通则整条翻译链路不可用，列为 P0 阻塞项 |
| tk 签名 | 硬编码 TKK `406398.2087938574` **不可信**，改为「首页正则抓取 + 内存缓存 1~6h + 403 时强制刷新重试」 | 该常量是 2015 年前后的值，Google 已多次轮换 |
| tk 验证方式 | **用 Node 跑 JS 参考实现做交叉验证（200 条 golden）**，不做 PHP 对拍 | 本机无 PHP 运行环境，`google.php` 只能静态评审；Node 可执行且是同一算法的权威来源 |
| 必须删掉的 PHP 行为 | **`ucfirst(strtolower($text))` 一律删除** | 会把德语名词、全大写标题、品牌名全部破坏，CJK 目标语言下更是无意义 |
| 批量切批 | 累计字符 ≤ 4000 且块数 ≤ 50 为一批；**批内索引严格映射回 block index，禁止跨批重排** | 5000 字符硬上限 + 块级 1:1 对应，错位比翻译差更致命 |
| 擦除 | 默认 **big-lama**；复杂结构升级 **PowerPaint V2**；无 GPU 降级 **OpenCV Telea**；**纯色/渐变背景直接解析式填充（最快最准）** | 40%~60% 的实景文字压在纯色/渐变上，用 LaMa 反而引入噪声 |
| 渲染 | **Pillow 13 + 2~3 倍超采样下采样**，字体按语言路由 Noto Sans 系 | 必须用**带 libraqm 的 Pillow 构建**，否则阿拉伯/泰文/天城文整形错误 |
| 清晰度 | 整图零缩放；**仅 OCR 阶段对超大图切片，像素处理永不分块**；单次编码输出 | 避免二次有损压缩与拼接缝 |
| 服务形态 | FastAPI + 单例模型常驻 + GPU 串行队列（`Semaphore(1)`），同步接口 P95 ≤ 6 s | 8 GB 显存下单卡约 **0.4~0.7 QPS**，诚实排队优于假装高并发 |
| 「背景透明」 | 默认 `mode=full`：译文直接贴在擦净的原图上，**不画任何底色块**；另提供 `mode=text_layer` 输出 RGBA 纯文字图层 | 两种理解都覆盖，但默认只做第一种 |
| 成本（1 万张/天） | 自建 ≈ **1,500~3,000 元/月**（只有 GPU 成本，翻译 0 元） | 对比调百度图片翻译 API ≈ 11,400 元/月，省 70%~85% |
| 最大风险 | 免费通道的**可用性**与**合规性**，不是技术 | 需用户拍板：接受"随时可能换通道"的运维负担，还是付费买确定性 |

**最需要拍板的 5 件事**（详见 §10）：**海外出口由谁提供/预算**、翻译通道可用性 SLA 的容忍度、是否保留付费降级源、单卡吞吐是否满足业务峰值、首期语种范围是否包含竖排与阿拉伯语。

---

## 1. 需求解构与范围

### 1.1 原始需求逐条拆解

| 需求原文 | 技术含义 | 章节 |
|---|---|---|
| 传远程图片 + 目标语言 | 输入 URL 优先，需下载/校验/防盗链/SSRF 防护 | §6、§7.1 |
| 返回翻译后的图片 | 输出图片 + 结构化 JSON | §6.3 |
| 文字翻译成目标语言文字 | OCR + 自建 Google 通道 | §3.1、§3.3 |
| 回填到原位置 | 用检测到的多边形坐标原位渲染，含对齐/行高/字号/竖排还原 | §4.2 |
| 保持文字背景透明 | 不画底色块；擦除必须像素级干净（不是矩形块） | §4.1 |
| 保持图片清晰度 | 全链路无损、不整图缩放、超采样渲染、高质量编码 | §5 |
| 语言为 Python | 技术栈锁定 Python；PHP 翻译客户端需等价移植 | §3.3、§7 |
| 类似百度图片翻译 API | 接口形态 + 参数语义对齐 | §1.2、§6 |
| 已有 PHP 生产项目 | Python 以 HTTP 微服务被 PHP 调用；PHP 侧现有翻译代码迁到 Python | §6.5 |
| 翻译用自建 Google 通道 | 移植 `google.php`，不引入第三方付费 MT 作为主链路 | §3.3 |

### 1.2 与百度图片翻译 API 的能力对齐

百度图片翻译 API V2 已核实：`POST https://fanyi-api.baidu.com/ait/api/picture/translate`，JSON body，Access Token 鉴权，图片 Base64 ≤ 5 MB。

| 百度参数 | 取值 | 我方对应 | 说明 |
|---|---|---|---|
| `from` / `to` | `auto`/`zh`/`en`/`jp`… | `from` / `to` | 需语言码映射层（百度 `jp`/`kor` ↔ ISO `ja`/`ko` ↔ Google `zh-CN`/`iw`/`no`） |
| `content` | Base64 ≤ 5 MB | `image_base64` / `image_url` | 我方额外支持 URL 直传，上限放宽至 10 MB |
| `paste=0` | 只回 OCR + 译文 | `render.mode=json` | 等价 |
| `paste=1` | 整图贴合 | `render.mode=full` | 等价，**默认** |
| `paste=2` | 块区贴合 | `render.mode=blocks` | 等价 |
| `paste=3` | 只回坐标点 | `render.mode=json` + `return.blocks=true` | 我方额外给字号/颜色/描边/对齐 |
| `view_type=0/1` | 通用/高精擦除 | `erase.quality=fast/hq` | 映射 LaMa / PowerPaint V2 |
| `need_intervene` | 术语干预开关 | `glossary_id` | 我方用术语库 ID |
| `model_type=nmt` | 模型类型 | `engine=google_web\|google_cloud\|baidu` | 多引擎 |

**百度的短板 = 我方的差异化空间**（建议写进对外说明）：

1. `paste=1` 只回图片，样式不可得；`paste=3` 只回 4 个坐标点，**没有字号、字色、描边、对齐、字重**，前端拿到也还原不出样式。
2. 不支持 URL 直传，必须 Base64（体积膨胀 33%，5 MB 上限实际只剩 3.7 MB 原图）。
3. 仅支持约 21 种语言。
4. 无任务队列/回调，单次同步，QPS 10 封顶。

我方 `blocks[]` 每块携带 `font` / `stroke` / `align` / `background` / `confidence` / `render` 六个样式子对象，可直接驱动前端 Canvas 编辑器二次修改。这是产品层面的主要护城河。

### 1.3 范围边界（本期不做）

- 不做视频字幕翻译、PDF 版面重排（那是 PP-StructureV3 的领域，另一条产线）。
- 不做实时流式（摄像头取词）。
- 不做人工翻译工作台（只提供 API + 结构化数据供上层做）。

---

## 2. 总体架构

### 2.1 系统上下文

```mermaid
flowchart LR
    subgraph Client["调用方"]
        PHP["PHP 业务站<br/>58api.cn 等"]
        WEB["Web / H5<br/>（可选直连）"]
    end

    subgraph SVC["Python 图片翻译微服务"]
        API["FastAPI<br/>/v1/image/translate"]
        Q["GPU 队列<br/>Semaphore(1)"]
        PIPE["Pipeline Orchestrator"]
        OCR["OcrEngine<br/>PaddleOCR 3.7"]
        MT["Translator<br/>GoogleWebTranslator"]
        INP["InpaintEngine<br/>LaMa / SD / Telea"]
        RND["Renderer<br/>Pillow + Noto"]
        CACHE["CacheStore<br/>Redis / LRU"]
        EGR["EgressPool<br/>★ 海外出口<br/>代理 / 境外节点"]
    end

    subgraph EXT["外部依赖"]
        IMG["图片源站"]
        G1["translate.googleapis.com<br/>client=te（主）"]
        G2["translate.google.com<br/>client=at（降级）"]
        G3["Google Cloud Translation<br/>（可选付费兜底）"]
        BD["百度翻译 API<br/>（境内可直连）"]
    end

    PHP -->|"HTTPS + HMAC"| API
    WEB --> API
    API --> CACHE
    CACHE -.->|miss| Q
    Q --> PIPE
    PIPE --> OCR
    PIPE --> MT
    PIPE --> INP
    PIPE --> RND
    PIPE -->|"下载"| IMG
    MT --> EGR
    EGR --> G1
    EGR -.->|降级| G2
    EGR -.->|降级| G3
    MT -.->|"境内可直连"| BD
```

> ⚠️ **关键事实（表述已精确化，勿望文生义）**：
> `translate.googleapis.com`（web 通道用的域名）与 `translation.googleapis.com`（Cloud Translation v2 **官方参考文档**给的端点）**是两个不同域名，但同属 Google 运营的 `googleapis.com` 域族，境内均不可达**。
> 因此 `api → free → google_cloud` 三级降级**共享同一出口依赖**，出口故障时整条链一起失效，**不构成有效降级**。
> 特别注意：看到"域名不同"不要以为换个域名就能绕——`translation.googleapis.com` 虽然 DNS 可解析，但连接同样超时，实测无救。真正能拿掉出口依赖的只有**境内可直连的第三方 API（如百度翻译）**，这直接影响 §10.1 决策 2 的取舍。

### 2.2 端到端流程（主干）

```mermaid
flowchart TD
    A["输入：image_url / image_base64<br/>from, to, render 选项"] --> B{"参数与语言白名单校验"}
    B -->|失败| B1["40001 / 40002"]
    B -->|通过| C{"缓存命中？<br/>sha256(url|to|options)"}
    C -->|是| C1["直接返回缓存结果"]
    C -->|否| D{"图片来源"}
    D -->|URL| D1["下载<br/>超时5s / 重定向≤3<br/>拒绝内网IP / Content-Type白名单 / ≤10MB"]
    D -->|Base64| D2["解码"]
    D1 --> E["解码为 PIL.Image<br/>EXIF 方向校正 / ICC→sRGB / alpha 处理"]
    D2 --> E
    E --> F{"边长 > 4096？"}
    F -->|是| F1["OCR 专用切片<br/>overlap 192px<br/>【只切 OCR，不动像素】"]
    F -->|否| F2["整图 OCR"]
    F1 --> G["文字检测 det<br/>PP-OCRv6 medium det<br/>输出四点多边形"]
    F2 --> G
    G --> H["行方向分类 cls<br/>0°/180° 校正"]
    H --> I["文字识别 rec<br/>text + score"]
    I --> J["行/段落聚合<br/>y重叠 + 间距 + 对齐 + 分栏<br/>→ TextBlock[]"]
    J --> K["过滤<br/>score<0.5 丢弃<br/>纯符号/图标噪声丢弃<br/>纯数字不送翻译"]
    K --> L["批量翻译<br/>切批(≤4000字符/≤50块)<br/>索引严格映射<br/>失败逐块回填原文"]
    L --> M["Mask 生成<br/>Sauvola二值化 + 闭运算<br/>+ 膨胀(≥描边宽)"]
    M --> N{"背景可解析？"}
    N -->|"近似纯色/线性渐变"| N1["【0级】解析式填充<br/>零伪影 · <5ms"]
    N -->|"纹理/结构区"| N2["【1级】LaMa 擦除<br/>ROI裁剪→batch→贴回"]
    N2 -->|"结构错乱"| N3["【2级】PowerPaint V2<br/>erase.quality=hq"]
    N -->|"无 GPU"| N4["【3级】OpenCV Telea"]
    N1 --> O["样式分析<br/>前景色/描边色宽/背景色<br/>对齐/字号/字重/行高/竖排"]
    N3 --> O
    N4 --> O
    O --> P["译文回填渲染<br/>超采样2~3x → 绘制 → LANCZOS下采样<br/>字号二分搜索 + 膨胀策略"]
    P --> R["后处理<br/>边缘融合 / 越界检查"]
    R --> S["编码输出<br/>JPEG q95 subsampling=0 / PNG / WebP"]
    S --> T["返回：图片 + blocks + timing"]
    C1 --> T
```

### 2.3 关键时序（同步请求，含翻译内部流程）

```mermaid
sequenceDiagram
    autonumber
    participant PHP as PHP 业务站
    participant API as FastAPI
    participant Cache as CacheStore
    participant Q as GPU 队列
    participant P as Pipeline
    participant OCR as OcrEngine
    participant MT as GoogleWebTranslator
    participant G as Google站点
    participant INP as InpaintEngine
    participant R as Renderer

    PHP->>API: POST /v1/image/translate (HMAC)
    API->>API: 验签 / 限流 / 参数校验
    API->>Cache: get(sha256(url|to|opts))

    alt 缓存命中
        Cache-->>API: cached
        API-->>PHP: 200 {code:0, cached:true}
    else 未命中
        API->>Q: acquire() Semaphore(1), 超时 30s
        Q->>P: run(request)
        P->>P: fetch + normalize (EXIF/ICC)
        P->>OCR: detect_and_recognize(img)
        OCR->>OCR: det → cls → rec
        OCR-->>P: RawLine[] (polygon, text, score)
        P->>P: group_blocks() → TextBlock[]

        P->>MT: translate_batch(texts, src, tgt)
        MT->>MT: 预过滤（纯数字/纯符号）
        MT->>MT: plan_batches(≤4000字符, ≤50块)
        MT->>MT: fetch_tkk()  (缓存 TTL 6h)
        loop 每一批
            MT->>MT: tk = build_tk("".join(texts), tkk)
            MT->>G: POST /translate_a/t?client=te&...&tk=
            alt 403 / 429 / 503 / 空结果
                MT->>MT: 刷新 TKK → 重试1次
                MT->>MT: 仍失败 → 指数退避重试(最多3次)
                MT->>G: 降级 free 通道 client=at（一块一请求）
            end
            G-->>MT: ["译文1","译文2",...]
            MT->>MT: 校验 len(out)==len(chunk)；再逐元素体检（非 str 只废该位置）
            MT->>MT: html.unescape()（不做大小写变换）
        end
        MT-->>P: translations[]（长度 == blocks 长度，失败位=原文）

        P->>P: build_mask(blocks)
        P->>INP: erase(img, mask, quality)
        INP-->>P: erased_image
        P->>P: analyze_style(img, blocks, mask)
        P->>R: render(erased_image, blocks, styles)
        R->>R: supersample 2~3x → draw → downsample
        R-->>P: rendered
        P-->>API: PipelineResult
        API->>Cache: set(key, ttl=24h)
        API-->>PHP: 200 {code:0, image, blocks, timing_ms}
    end
```

> 说明：为保持可读性，本文内嵌时序图将 Google 站点合并为单一参与者 `G`（`api` 与 `free` 两个端点均在其上）；完整版含 7 个外部参与者与异步轮询分支，见 `docs/sequence-diagram.mermaid`。

---

## 3. 各环节技术选型

### 3.1 文字检测与识别：PaddleOCR

#### 3.1.1 版本核实结论

**已核实**（2026-11-19 检索）：

- PaddleOCR 最新发布 **3.7.0（2026-06-11）**，默认模型 **PP-OCRv6**。
- PP-OCRv6 三档：`tiny` 1.5M / `small` 7.7M / `medium` 34.5M 参数（det 权重 1.9 / 9.6 / 59.4 MB，rec 权重 4.4 / 20.4 / 73.3 MB）。
- 单模型覆盖 **50 种语言**：简中、繁中、英文、日文 + 46 种拉丁语系。
- **不含**韩文、阿拉伯文、西里尔文、天城文、泰文、希腊文、泰米尔文、泰卢固文 —— 这些走 **PP-OCRv5 多语种 mobile rec**（2025 年 6~10 月发布）。
- 精度：PP-OCRv6 medium det 86.2% / rec 83.2%，优于 PP-OCRv5_server 的 81.6% / 78.1%，且模型更小。
- GPU 安装：PaddlePaddle 3.x 轮子不在 PyPI，需 `paddlepaddle-gpu==3.2.0 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/`（要求驱动 ≥ 550.54.14）。

来源（已核实）：
- <https://paddleocr.dev/models> — PP-OCRv6 分档、50 语种、各语种 rec 模型表
- <https://paddleocr.dev/install> — GPU 索引与 CUDA/驱动对应关系
- <https://github.com/PaddlePaddle/PaddleOCR> — 3.0/3.0.1 变更日志、`use_textline_orientation` 等 3.x 新参数

⚠️ **需确认**：PP-OCRv6 在消费级 8 GB 卡（RTX 3060 / 4060 Ti）上的单图耗时，官方只公布 A100 数据（medium 0.29 s/图）。本方案按 A100 的 1.5~2 倍估算。

> **与早期方案的口径对齐**：团队内部此前以「PP-OCRv5」表述。实际 PP-OCRv5 仍是**其他文字系统**的必需模型（韩/阿/西里尔/天城/泰），且是 PP-OCRv6 的三档覆盖不足时的回退。因此最终口径为「**PP-OCRv6 medium 为默认 + PP-OCRv5 多语种 rec 为补充**」，两者都要装上。

#### 3.1.2 子模型职责

| 子模型 | 作用 | 是否必需 | 配置 |
|---|---|---|---|
| `det`（DBNet 系，PP-HGNetV2 骨干） | 输出**任意四边形/多边形**文字区域 | 必需 | `PP-OCRv6_medium_det` |
| `cls`（`PP-LCNet_x1_0_textline_ori`，99.42%） | 单行 0°/180° 判定并翻转 | 强烈建议 | 开启 |
| `rec`（CRNN+注意力 / EncoderWithLightSVTR） | 行级识别 | 必需 | `PP-OCRv6_medium_rec` |
| `doc_ori`（`PP-LCNet_x1_0_doc_ori`） | 整图 0/90/180/270 | 场景图开启 | 可配 |

**关于 `use_angle_cls`**：这是 PaddleOCR **2.x** 的参数名，3.x 已改为 `use_textline_orientation`。网上大量 2.x 教程仍用旧参数，工程师实现时不要照抄；传旧参数在 3.x 会抛 `ValueError: Unknown argument`。**建议开启**，成本约 5 ms，可挽回倒置文字（招牌、瓶身、翻转截图）整块失效。

#### 3.1.3 语言 → 模型路由表

| 源语言 | det | rec | GPU 单图估算 |
|---|---|---|---|
| 中/英/拉丁语系（fr,de,es,pt,it,pl,tr,id,vi…） | `PP-OCRv6_medium_det` | `PP-OCRv6_medium_rec` | 300~600 ms |
| **日文** | 同上 | `PP-OCRv6_medium_rec`（**禁止 tiny**，见下方警告） | 300~600 ms |
| 韩文 | 同上 | `korean_PP-OCRv5_mobile_rec` | 250~500 ms |
| 阿拉伯/波斯/乌尔都 | 同上 | `arabic_PP-OCRv5_mobile_rec` | 250~500 ms |
| 俄/乌/西里尔 | 同上 | `cyrillic_` 或 `eslav_PP-OCRv5_mobile_rec` | 250~500 ms |
| 印地/天城文 | 同上 | `devanagari_PP-OCRv5_mobile_rec` | 250~500 ms |
| 泰/希/泰米尔/泰卢固 | 同上 | `th_` / `el_` / `ta_` / `te_` | 250~500 ms |
| `from=auto` | 先用 `PP-OCRv6_medium` 试跑；平均 score < 0.6 时用 Google 通道的 `detectLang()` 判定语种，切对应 rec 重跑 | — | +30% |

> ⚠️ **PP-OCRv6 `tiny` 档不含日语假名**：官方实测日语字符错误率约 **47%**，且**不抛错、置信度偏高（>0.7）**，属"静默错"，极难排查。因此 `route_model()` 必须硬编码兜底：`if lang == "ja" and tier == "tiny": tier = "small"`。省显存不能省到日语头上。

实现：`OcrEngine` 持有 `dict[lang_group, PaddleOCR]` 实例池，**启动时全部预热**（各跑一次 dummy 推理），避免首请求 5~10 s 冷加载。8 GB 显存下常驻 3 个 rec 约 1.5 GB，可接受。

#### 3.1.4 竖排文字

- 检测层：DBNet 输出多边形，**竖排块本身能检出**（竖直长条）。
- 识别层：PP-OCRv5 起对竖排中日文有专门优化，`rec` 可识别，但**输出字符串不含"这是竖排"的标记**。
- 我方判定：`polygon.height / polygon.width > 2.5` 且 rec 置信度正常 → `block.vertical = true`。
- **回填必须区分两种情况**：目标语言是 CJK（可竖排）→ 逐字沿 y 轴排版；目标语言是拉丁/西里尔（横向书写）→ **转横排 + 缩字号**，不要强行竖排拉丁字母（视觉上极其怪异，用户会认为是 bug）。

#### 3.1.5 行/段落聚合

`det` 粒度是文本行，但翻译需要语段上下文（单行短词翻译质量差）。规则：

1. 计算每块 `polygon` 的最小外接矩形 `bbox`。
2. **先分栏**：全图若存在 ≥2 条贯穿全高的垂直空白带（宽度 > 图宽 3%），判定为多列，列内独立聚合（否则左右栏会被合成一段，这是最典型的翻车场景）。
3. 块 A、B 的 `x` 重叠率 > 60% 且垂直间距 < 0.6 × 平均行高 → 同段。
4. 块 A、B 的 `y` 重叠率 > 60% 且水平间距 < 0.3 × 平均字高 → 同一行（被 det 切碎）。
5. 段内按 y 排序，生成 `TextBlock{lines[]}`，`block.rect` 取并集。
6. **翻译以 block 为单位，渲染也以 block 为单位**（不是 line），否则无法正确计算对齐与行高。

### 3.2 文字擦除（Inpainting）

#### 3.2.1 三方对比

| 维度 | **big-lama（LaMa）** | **SD-inpaint（PowerPaint V2 / BrushNet）** | **OpenCV Telea / NS** |
|---|---|---|---|
| 模型/来源 | `big-lama.pt` 198 MB fp32<br>`github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt` | `Sanster/PowerPaint-V1-stable-diffusion-inpainting` 等 | `cv2.inpaint()` 内置 |
| 原理 | 快速傅里叶卷积，全局感受野，"填洞" | 扩散模型条件生成，能"补内容" | 邻域像素插值 |
| 质量（文字场景，512²） | 人评 9.5/10 | 人评 9.2/10（综合场景更高） | 纯色背景尚可，纹理必留痕 |
| 单块耗时（RTX 3080） | **45 ms** | **124 ms** | 2~8 ms |
| 显存 | **1.25 GB** | **3.4 GB** | 0 |
| 确定性 | 是（同输入同输出） | 否（需固定 seed 才幂等） | 是 |
| 许可 | Apache 2.0 | PowerPaint Apache 2.0 | Apache 2.0 |

数据来源：IOPaint 仓库 `benchmark.py` 口径（RTX 3080 / CUDA 11.8 / torch 2.1.2 / 512×512）的第三方汇总，以及 LaMa 在浏览器端实测（大洞结构重建优于 MI-GAN +11.6 dB）。⚠️ 具体数值随硬件与 ROI 面积波动，仅作量级参考。

**关于 IOPaint 的现状（已核实，需注意）**：IOPaint 最新版 **1.6.0（2025-03-18）**，其 GitHub 仓库已于 **2025-08-13 归档（read-only）**，原作者不再维护；`lama-cleaner` 包自 2023-01 起未更新。虽然 pip 仍可安装，但**不建议把它作为生产依赖**（Python 3.13 已无法安装，且不会再有修复）。

**本方案结论**：不依赖 `iopaint` 包，**直接加载 `big-lama.pt` 权重，自行实现约 200 行的 LaMa 推理封装**（模型结构稳定、权重可直接复用）。这样既避开依赖地狱，也便于做 batch 推理与 fp16。

#### 3.2.2 四级擦除策略（默认 LaMa，按条件升降）

```mermaid
flowchart TD
    S["待擦除 ROI"] --> A{"环带像素方差 < 6<br/>且可拟合线性渐变？"}
    A -->|是| A1["【0级】解析式填充<br/>中位数 / 双线性渐变<br/>零伪影 · <5ms"]
    A -->|否| B{"GPU 可用？"}
    B -->|否| B1["【3级】OpenCV Telea<br/>radius=3<br/>有痕但可用"]
    B -->|是| C{"ROI 内边缘密度高<br/>或位于人脸/建筑/强纹理区？"}
    C -->|否| C1["【1级】LaMa 擦除<br/>默认 · 45ms/块 · 1.25GB"]
    C -->|是| C2["【2级】PowerPaint V2<br/>仅 erase.quality=hq 启用<br/>124ms/块 · 3.4GB"]
    A1 --> E["擦净图"]
    B1 --> E
    C1 --> E
    C2 --> E
```

**关键洞察：第 0 级（解析式填充）应覆盖 40%~60% 的真实场景**。海报、电商主图、App 截图的文字大多压在纯色块或线性渐变上。这类区域用 LaMa 属于杀鸡用牛刀，而且 LaMa 会**引入肉眼可见的纹理噪声**——比原始纯色更糟。直接填背景色反而**像素级完美**。

```python
def analyze_background(roi: np.ndarray, mask: np.ndarray) -> FillSpec | None:
    """环带（mask 外扩 8px 减去 mask）像素统计，判断能否解析式填充。"""
    ring = dilate(mask, 8) & ~mask
    px = roi[ring]
    if px.std(axis=0).max() < 6.0:
        return SolidFill(color=np.median(px, axis=0))
    if (spec := fit_linear_gradient(ring, px, r2_thresh=0.92)):
        return spec
    return None
```

#### 3.2.3 Mask 生成策略（决定成败的一步）

**错误做法**：用检测框直接当 mask（矩形）。后果是把背景纹理整块擦掉再重生成 → 必然失真，且「文字背景透明」无从谈起，还会出现明显的"方块补丁"。

**正确做法**：像素级文字 mask。

```python
def build_text_mask(image: np.ndarray, block: TextBlock) -> np.ndarray:
    """输出与原图同尺寸的 uint8 mask，255 = 文字像素。"""
    roi = crop(image, block.rect_padded(4))
    gray = to_gray(roi)

    # 1) 局部自适应二值化（Sauvola 优于全局 Otsu，抗光照不均）
    bw = sauvola(gray, window=25, k=0.2)
    if bw.mean() > 0.5:
        bw = ~bw

    # 2) 闭运算连接笔画断点（"认""蟹"这类多部件汉字）
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))

    # 3) 膨胀量必须 >= 描边宽 + 抗锯齿宽，否则留下"描边残影"
    stroke_w = estimate_stroke_width(roi, bw)      # 见 §4.2
    r = 2 + int(round(stroke_w))
    bw = cv2.dilate(bw, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))

    return paste_into_fullsize(bw, block.rect_padded(4))
```

**三个必须遵守的细节**：

1. **膨胀量必须覆盖描边**。实测：白字 + 2 px 黑描边，若 mask 只覆盖字芯，擦除后原地会留一圈清晰黑轮廓——这是"擦不干净"投诉的头号来源。
2. **mask 分块送 LaMa，不要整图送**。LaMa 输入需 8 的倍数，每块 padding 到 512 或 1024；一个 block 一次前向；20 个 block 可 batch 成 `[B,3,512,512]` 一次推理（8 GB 显存下 B ≤ 8）。
3. **羽化只用于合成，不用于 LaMa 输入**。LaMa 要 0/1 硬 mask；结果贴回时对边界做 `alpha = gaussian_blur(mask, sigma=1.0)` 软融合，避免硬边。

#### 3.2.4 各背景类型的表现与补充手段

| 背景类型 | LaMa 表现 | 补充手段 |
|---|---|---|
| 纯色 / 线性渐变 | 会引入噪声 | **走第 0 级填充，不用 LaMa** |
| 照片纹理（草地、砖墙、木纹） | 中等，小面积好 | 控制单块 ROI < 图面积 15% |
| 强结构（人脸、建筑边缘、文字旁有线） | 差，结构会歪 | 升级 PowerPaint V2 |
| 已有阴影/投影 | 阴影会被一起抹掉 | mask 膨胀用各向异性核，避开阴影侧 |
| 半透明水印压在文字上 | 无法分离 | 标记 `skip`，保留原文字不擦（见 §8.1） |

### 3.3 翻译层：移植 PHP Google 客户端（主通道）

#### 3.3.1 定位与设计原则

用户明确要求：**「先用 OCR 提取文字和回填，文字翻译我提供 php 版的 google 翻译，你帮我转换成 python」**。因此翻译层不是"选一个云 API"，而是**等价移植 `D:/wwwroot/ocr/google.php`（761 行，`Otc\Translate\Google`）**。

移植的四条铁律：

| 原则 | 说明 |
|---|---|
| **1. 块级 1:1 严格对应** | 图片翻译的最小单元是"一个文字块"。任何会让块与译文错位的逻辑（段落合并、跨批重排）都必须禁止。翻译错位比翻译质量差严重得多——用户看到文字跑到别的位置，会直接判定服务坏掉。 |
| **2. 失败必须确定性降级** | 任一环节失败，该块回填**原文**并打标记，绝不返回空串或错位译文。 |
| **3. 不保留 PHP 的语义破坏性后处理** | `ucfirst(strtolower())` 必须删除（见 §3.3.5）。 |
| **4. 禁止实例级可变状态** | PHP 单例把 `source`/`target` 存在实例字段上（`$this->target = ...`），在 PHP-FPM 的"每请求独立进程"模型下侥幸不出错；搬到 asyncio 单进程多协程后会**直接串语言**。Python 侧一律改为**方法入参**。 |

#### 3.3.2 通道能力对比与路由

| 通道 | PHP 方法 | 端点 | 批量能力 | 顺序保证 | 结论 |
|---|---|---|---|---|---|
| **`api`**（`client=te`） | `api()` L208 | `{domain}/translate_a/t` | **一个请求可带 N 个同名 `q`** | **严格 1:1**（响应数组下标即入参下标） | **主通道，唯一默认** |
| `free`（`client=at`） | `free()` L455 | `{domain}/translate_a/single` | 用 `\n` 拼接成单条 | 靠 `sentences[].orig` 回匹配，**不可靠** | 仅降级，且强制「一块一请求」 |
| `cn`（`client=t`） | `cn()` L533 | `translate.google.cn/translate_a/single` | 同上 | 同上 | **不实现**（`.cn` 已于 2022 年停止服务，硬编码 NID cookie 早已失效） |
| `nmtApi` | `nmtApi()` L156 | Google Cloud `TranslateClient` | `translateBatch` 128 条/批 | 严格 | 可选付费兜底（需 API Key 与账单） |

**路由策略**：

```
api 成功 ──► 返回
  │
  ├─ 403 / 空结果 / tk 校验失败 ──► 刷新 TKK ──► api 重试 1 次
  │                                                    │
  │                                                    └─ 仍失败 ──► free（逐块）
  │                                                                      │
  └─ 429 / 503 / 超时 ──► 指数退避重试 3 次 ──► 仍失败 ──► free（逐块）
                                                              │
                                                              └─ 仍失败 ──► google_cloud（若配置）
                                                                              │
                                                                              └─ 仍失败 ──► 原文占位 + 标记 mt_failed
```

**为什么 `free` 只能做降级、且必须逐块**：`free` 把多块用 `\n` 拼成一条发送，响应 `sentences[]` 的切分由 Google 决定（一个句子可能跨两个块，一个块也可能被切成两句），PHP 用 `getTransTexts()` 做子串包含匹配来"猜"回填关系。这个算法在文本相似时会串行（如连续三个"确定"），且 `stristr` 是大小写不敏感的子串匹配，误判率高。图片翻译不能赌这个，因此：

- **不移植 `getTransTexts()`**（明确列为"不迁移代码"）。
- `free` 通道强制 `1 block = 1 request`，响应里若 `len(sentences) != 1`，取 `sentences[0].trans` 并在超过 2 句时放弃（回填原文）。

#### 3.3.3 PHP → Python 逐方法映射表

| # | PHP 方法 | 行号 | 职责 | Python 对应 | 注意事项 |
|---|---|---|---|---|---|
| 1 | `getInstance($db)` | L74 | 单例 | 构造注入 `GoogleWebTranslator(cfg, http)` | 保留"单例"用于复用连接池，但**状态不可变** |
| 2 | `setMode($mode)` | L85 | 选通道 | `mode` 构造参数 | `cn` 直接拒绝启动 |
| 3 | `setSource/setTarget` | L93-100 | 设语言 | **方法入参** `translate_batch(texts, source, target)` | 原实例字段 → 并发串语言，必改 |
| 4 | `setAccounts($accounts)` | L81 | 多账号/cookie | `cfg.accounts` | `free` 通道用 |
| 5 | `setProxy($proxy)` | L89 | 代理 | `httpx.AsyncClient(proxy=...)` | 见 §3.3.7 |
| 6 | `setMultiIp($status)` | L101 | 多出口 IP | `cfg.egress.mode = multi_nic` | 见 §3.3.7 |
| 7 | `isAvaliable($text)` | L105 | 可用性 | `health() -> bool` | 原实现恒 `true`（被注释掉的分支），简化为配置检查 |
| 8 | `getMaxSize($mode)` | L115 | 5000 上限 | `MAX_CHARS = 5000` 常量 | 三通道相同 |
| 9 | `getAccount($mode)` | L119 | 随机取账号 | `random.choice(cfg.accounts[mode])` | 用于分散风控 |
| 10 | `translate($text,$langs)` | L126 | 分发 | `translate_batch()` / `translate_multi_target()` | |
| 11 | `auto($text)` | L132 | 本地语种检测 | `detect_language_local()`（`langdetect`/`pycld3`）+ `AUTO_LIST` | **`'vi' => 'iv'` 是 bug，改 `vi`** |
| 12 | `nmtApi()` | L156 | Cloud 批量 | `GoogleCloudTranslator` | 见 #10 组（§3.3.9） |
| 13 | **`api()`** | **L208** | **主通道** | **`GoogleWebTranslator._translate_api()`** | **核心，见 §3.3.4** |
| 14 | `apiTest()` | L298 | 带 IP 回显调试版 | `debug_translate()`（仅测试环境） | 生产禁用（会把 IP 拼进译文） |
| 15 | `api2()` | L362 | 二维数组/单语言并发 | `translate_groups()` | |
| 16 | `api3()` | L408 | 二维数组/每组不同语言 | `translate_multi_target()` | 注意原版在循环里改 `$this->target`，是 #3 的同一问题 |
| 17 | `free()` | L455 | 降级通道 | `_translate_free()` | 见 §3.3.2 |
| 18 | `cn()` | L533 | `.cn` 通道 | **不实现** | 已停服 |
| 19 | `detectLang($text)` | L591 | 远端语种检测 | `detect_language_remote()` | 内部接口，不稳定，仅用于 `from=auto` 二次确认 |
| 20 | `setFreeCookieFile` | L616 | 刷 cookie | `CookieJar` 落盘 + `asyncio.Lock` | 并发写需锁 |
| 21 | `getTransTexts()` | L620 | 段落回填对齐 | **不移植** | 与"块级 1:1"原则冲突 |
| 22 | `convertLang($code)` | L645 | 语言码转换 | `normalize_lang()` | **原实现有 bug**，见 §3.3.6 #3 |
| 23 | `getConvertCodes()` | L657 | `he→iw` 映射 | `LANG_ALIAS = {"he": "iw", ...}` | 修正后真正生效 |
| 24 | `shr32()` | L663 | 32 位逻辑右移 | Python `>>` + `& 0xFFFFFFFF` | 原生支持，无需模拟 |
| 25 | `charCodeAt()` | L682 | 取码点 | `ord(ch)` | 原生 |
| 26 | `RL()` | L691 | 签名中间运算 | `_rl(a, b)` | 见 §3.3.4 |
| 27 | `TKK()` | L704 | 取 TKK | `TkkProvider.get()` | **硬编码不可信，改为动态抓取** |
| 28 | `TL()` | L710 | 生成 tk | `build_tk(text, tkk)` | 见 §3.3.4 |
| 29 | `convertLangCode()` | L749 | Cloud 语言码 | `to_cloud_code()` | `zh-cn→zh-CN`、`zh-hk/zh-tw→zh-TW` |
| 30 | `\Otc\Request::execute` | 外部 | curl 单请求 | `httpx.AsyncClient.request()` | 需设 `timeout`、`Content-Type` |
| 31 | `\Otc\Request::executeMulti` | 外部 | curl 并发 | `asyncio.gather(..., return_exceptions=True)` | 单点异常不能拖垮整批 |

**三个"必须一行行对着抄"的部分**：`build_tk()`、`_rl()`、请求体的构造。其余方法都是工程包装，可以按 Python 习惯重写。

#### 3.3.4 tk 签名算法移植（最关键、最容易错）

##### 算法本质

`tk` 是把待翻译文本 + 站点下发的 TKK 常量做一次混淆摘要，作为 URL 参数发给 Google。伪代码（与 PHP 逐行等价）：

```python
MASK = 0xFFFFFFFF
# 字符串形态，与 Google 接口原生形态一致（&tk=406398.2087938574），也与 PHP TKK() 的返回值同形。
# 元组只是内部计算的中间态，不做对外参数——对外一律传/回字符串。
DEFAULT_TKK = "406398.2087938574"     # 对应 PHP 的 TKK() 返回值，非某个输入的签名结果

def _shr32(x: int, bits: int) -> int:
    """显式移植 PHP/JS 的 >>> 边界语义（Python 的 >> 与之不同）。"""
    if bits <= 0:
        return x & MASK                 # PHP/JS: 移位 0 位返回原值；Python 的 x >> -1 会抛 ValueError
    if bits >= 32:
        return 0                        # PHP/JS: 移满 32 位为 0
    return (x & MASK) >> bits


def _rl(a: int, b: str) -> int:
    """PHP RL() 的等价实现。b 形如 '+-a^+6' 的三元组指令流。"""
    c = 0
    while c < len(b) - 2:
        ch = b[c + 2]
        d = ord(ch) - 87 if "a" <= ch <= "z" else int(ch)
        # 与 PHP 一致：'+' 走右移，否则左移再掩码
        d = _shr32(a, d) if b[c + 1] == "+" else ((a << d) & MASK)
        a = ((a + d) & MASK) if b[c] == "+" else (a ^ d)
        c += 3
    return a & MASK


def _dec_tkk(tkk: str) -> tuple[int, int]:
    """'406398.2087938574' -> (406398, 2087938574)。解析只发生在这里。"""
    kb, _, vb = tkk.partition(".")
    return int(kb), int(vb)


def build_tk(text: str, tkk: str = DEFAULT_TKK) -> str:
    kb, vb = _dec_tkk(tkk)
    a = kb
    # 等价点：text.encode("utf-8") 与 **JS 参考实现** 逐字节相同
    # 注意：PHP 原文对 astral 字符（emoji）产出非法字节序列，不可作为基准，见下方说明 1
    for byte in text.encode("utf-8"):
        a = (a + byte) & MASK
        a = _rl(a, "+-a^+6")
    a = _rl(a, "+-3^+b+-f")
    a = (a ^ vb) & MASK
    a %= 1_000_000
    return f"{a}.{a ^ kb}"          # 注意：后半段没有 % 1000000，可能是 7 位（如 ..1027648）
```

> ⚠️ **返回值的后半段不做 `% 1_000_000`**。只有前半段 `a` 被取模，`a ^ kb` 是完整的 32 位异或结果（**可能 7 位**，例如 `630078.1027648`）。这是 Google 原算法的正确行为，**不是 bug**，不要"顺手修一下"再补一次取模——那会让 golden 全挂。同理不要用 `partition(".")` 以外的字符串处理去拼 tk（后半段本身不含 `.`，但不要靠 split()[1] 之类的位置假设）。

**三个等价性说明（写进代码注释，避免后人"优化"出错）**：

1. **等价对象是「JS 参考实现」，不是「PHP 原文的字节输出」**。
   PHP 的 `charCodeAt()` 用 `mb_convert_encoding($char, "UTF-32BE")` 取码点，**astral 字符（emoji）拿到的是完整码点**（如 `U+1F600`），不是 UTF-16 代理对。因此 PHP 里 `55296 == ($g & 64512)` 这个代理对分支**是死代码、永不进入**，emoji 会掉进"三字节"分支产出 `[0xFF,0x98,0x80]` —— 这是**非法 UTF-8**，而服务端/JS 要的是 `[0xF0,0x9F,0x98,0x80]`。
   → 结论：`text.encode("utf-8")` 与 **JS 语义**完全一致；与 PHP **对 BMP 字符一致、对 astral 字符不一致（PHP 是错的）**。
   → **禁止**把 PHP 的字节输出当基准，否则 emoji 场景会签出错误 tk。
   → 实现建议：保留逐码点 `_utf8_bytes()`，不直接用 `.encode()`。原因有二：① 遇到孤立代理（surrogate）时 `.encode()` 会抛 `UnicodeEncodeError`，而算法在该输入下仍有确定字节序列；② 逐码点写法能与 JS 的三/四字节分支**逐行对位**，出差异时可直接定位。对合法文本两者结果完全一致。

2. **`shr32()` 语义一致，但"边界行为"必须显式移植**。
   Python 的 `>>` 在 `a ∈ [0, 0xFFFFFFFF]` 时与 PHP 一致，**但这是个必须显式维护的不变式，不是自然成立的**；且两处边界 PHP/JS 都有明确定义、Python 没有：
   - `bits <= 0` → PHP/JS 返回原值；Python 的 `x >> -1` 直接抛 `ValueError`；
   - `bits >= 32` → PHP/JS 返回 `0`；若 `a` 未被掩码，Python 会继续右移得到非零。

   → 结论：保留显式 `_shr32()` 处理这两个边界，并把"**每个算术步骤后 `& 0xFFFFFFFF`**"写成注释。这不是可优化项：删掉掩码后，200 条 golden 里的长文本用例会立刻失败。

3. **`& 4294967295` 在 PHP 与 JS 里语义不同，但位模式一致**。
   PHP 的 `$a + $d & 4294967295.0` 是 **无符号** 掩码；JS 的 `& 4294967295` 走 `ToInt32`，是**有符号**解释。两者**位模式相同、符号解释不同**；JS 随后那句 `if (a < 0) a = (a & 2147483647) + 2147483648` 正好把它们重新对齐成同一个无符号值。
   → 结论：Python 用整数 `& MASK` 实现，并**保留这条符号修正分支**（哪怕在 Python 里看似冗余），才能与两边位级一致。
   → 若交叉验证仍出现差异，**以 Node 输出为准**——Google 服务端校验的是前端 JS 那套算法，PHP 与 Node 都只是仿写，而 PHP 在 astral 分支上是实锤错的。

##### 正确性验证：用 Node 参考实现交叉验证（必须做）

⚠️ **环境前提（已实测确认）**：本机**无 PHP 运行环境**，`D:/wwwroot/ocr/google.php` 只能静态阅读、无法实跑，因此**不能做 PHP↔Python 的运行时对拍**。同时本机**无法直连 Google**（`curl` 返回 000 / 8 s 超时），也不能拿线上响应反推正确 tk。

替代方案：**用 Node 跑 JS 参考实现做三方交叉验证**。

```text
参考链：Google 前端 JS 算法（事实标准）
          ├── Node 参考实现（可执行，作为 ground truth 生成器）
          ├── PHP 代码（静态阅读，确认语义一致）
          └── Python 移植（被测对象）
```

样本由两个文件协作生成：**`tools/tk_reference.js`（算法基准，JS）+ `scripts/gen_tk_golden.py`（生成器，Python 驱动）**。

```text
tools/tk_reference.js          # 内嵌 Google 官方 JS 版 tk 算法，TKK 固定 406398.2087938574
                               # stdin: JSON 字符串数组 → stdout: [{"input": "...", "tk": "..."}]
                               # 例: echo '["","hello"]' | node tools/tk_reference.js
                               #   → [{"input":"","tk":"263193.145255"},{"input":"hello","tk":"338590.203232"}]
scripts/gen_tk_golden.py       # subprocess 调 node，喂 200 条样本，落盘 tests/fixtures/tk_golden.json
tests/fixtures/tk_golden.json  # 产物，提交进仓库，CI 直接读
```

**`tools/tk_reference.js`**（核心算法，与 Google 前端 JS 逐行对应）：

```javascript
// 用法: echo '["hello","你好"]' | node tools/tk_reference.js
const RL = (a, b) => {
  for (let c = 0; c < b.length - 2; c += 3) {
    let d = b[c + 2];
    d = d >= 'a' ? d.charCodeAt(0) - 87 : Number(d);
    d = b[c + 1] === '+' ? a >>> d : a << d;
    a = b[c] === '+' ? (a + d) & 4294967295 : a ^ d;
  }
  return a;
};

const TKK = [406398, 2087938574];          // 固定常量，保证与 Python 侧同输入同输出

const buildTk = (text, [kb, vb] = TKK) => {
  let a = kb;
  for (const byte of Buffer.from(text, 'utf8')) {
    a = (a + byte) & 4294967295;
    a = RL(a, '+-a^+6');
  }
  a = RL(a, '+-3^+b+-f');
  a = (a ^ vb) >>> 0;
  a %= 1e6;
  return `${a}.${a ^ kb}`;
};

const inputs = JSON.parse(require('fs').readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(inputs.map(i => ({ input: i, tk: buildTk(i, TKK) }))));
```

**`scripts/gen_tk_golden.py`**（Python 驱动，避免手敲 shell 转义）：

```python
# 生成本地 golden 样本：python scripts/gen_tk_golden.py
import json, subprocess, pathlib

SAMPLES = build_samples()          # 200 条，见下方覆盖清单
raw = subprocess.run(
    ["node", "tools/tk_reference.js"],
    input=json.dumps(SAMPLES, ensure_ascii=False),
    capture_output=True, text=True, check=True,
).stdout
pathlib.Path("tests/fixtures/tk_golden.json").write_text(raw, encoding="utf-8")
```

**`tests/test_tk_golden.py`**：

```python
import json, pathlib, pytest
from imgtrans.translator.google_tk import build_tk

GOLDEN = json.loads((pathlib.Path(__file__).parent / "fixtures/tk_golden.json").read_text("utf-8"))

@pytest.mark.parametrize("case", GOLDEN)
def test_tk_matches_js_reference(case):
    assert build_tk(case["input"]) == case["tk"]          # 默认 tkk 即 JS 侧固定常量
```

**`tests/fixtures/tk_golden.json` 字段结构**：

```jsonc
[
  { "input": "hello",      "tk": "338590.203232" },
  { "input": "你好",        "tk": "916876.773874" },
  { "input": "😀emoji🎉",   "tk": "571082.951732" },
  { "input": "",           "tk": "263193.145255" },   // 空串必须能算出值，不能抛异常
  { "input": "😀🎉",        "tk": "64453.444603" }     // 纯 emoji，验 astral/4 字节分支
]
```

> ⚠️ **别把 `406398.2087938574` 当成空串的 tk** —— 那是 **TKK 常量本身**（`f"{kb}.{vb}"`），不是任何输入的签名结果。早期草案此处的示例值写错过，直接照抄会让断言必挂、并被误判为"算法移植错了"。上表 5 个值是 **Python 实现与 Node 参考实现双向对拍一致**的实测值，可直接用作 fixture 的首批断言。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `input` | string | 是 | 待签名原文，允许空串、含换行/制表符/NBSP/emoji |
| `tk` | string | 是 | JS 参考实现的输出，格式 `"{a}.{a^kb}"`，**断言用字符串全等**（不要转数字，避免精度丢失） |

样本集固定 **200 条**，必须覆盖：空串、单字符、纯 ASCII、中英混排、emoji（含代理对 😀🎉）、5000 字符长文本、重复字符、全角标点、换行/制表符、含 `&`/`+`/`%`/空格的文本、含 NBSP 的文本。**200 条全部一致才算移植完成**，列为 P0 验收硬门槛。

> **为什么用 JS 而不是 PHP 做基准**：本机 `php` 命令不存在（已实测），PHP 侧无法实跑；而 Google 服务端校验的是前端 JS 那套算法，Node 反而是更权威的基准。**PHP↔Python 的行为等价性由"算法一致性"保证（静态评审），而非实跑对拍**。

**三方一致性必须同时成立**：

| 检查 | 方法 | 通过标准 |
|---|---|---|
| Node ↔ Python | golden test 200 条 | 100% 一致 |
| Node ↔ PHP（静态） | 逐行比对 `RL`/`TL` 的控制流与掩码位置，人工评审并留评审记录 | 语义等价（允许浮点写法差异） |
| Python ↔ 线上 Google | **无法在本机验证**，只能在有海外出口的环境做冒烟 | 首次上线时用 10 条文本验证不返回 403 |

⚠️ 第三项是真实风险：**tk 算法对不等于签名被接受**——TKK 值本身也可能过期。因此"能拿到译文"才是最终验收，golden test 只保证"算法没抄错"。


##### TKK 动态刷新（替代硬编码）

```python
TKK_PATTERNS = [
    re.compile(r"tkk\s*[:=]\s*'(\d+)\.(\d+)'"),
    re.compile(r"tkk\s*[:=]\s*(\d+)\.(\d+)"),
    re.compile(r'"tkk"\s*:\s*"(\d+)\.(\d+)"'),
]

class TkkProvider:
    def __init__(self, tkk_ttl: int = 21600, fallback: str = DEFAULT_TKK):
        # 对外形态一律是字符串，解析只在 build_tk() 内部发生
        self._tkk, self._at, self._ttl, self._fallback = fallback, 0.0, tkk_ttl, fallback
        self._lock = asyncio.Lock()

    async def get(self, force: bool = False) -> str:
        async with self._lock:
            if not force and time.monotonic() - self._at < self._ttl:
                return self._tkk
            try:
                html = await self._http.get("https://translate.google.com/", timeout=5)
                for pat in TKK_PATTERNS:
                    if m := pat.search(html.text):
                        self._tkk = f"{m.group(1)}.{m.group(2)}"    # 存字符串
                        self._at = time.monotonic()
                        return self._tkk
            except Exception:
                log.warning("tkk_refresh_failed", exc_info=True)
            self._at = time.monotonic() - self._ttl + 300   # 300s 后允许再试
            return self._tkk            # 沿用上次值或内置常量
```

**策略参数**：
- 常态 TTL **21600 s**（6 h，与 `tkk_ttl: int = 21600` 接口签名一致）。Google 轮换 TKK 的频率是"天/周"级，6 h 足够灵敏且大幅减少对首页的抓取（早先草案写 3600 s / 1 h，属同区间内的更保守取值，可由配置覆盖）。
- 请求返回 **403 / 空数组 / tk 校验类错误**时，`force=True` 强制刷新一次再重试，**只重试 1 次**（防雪崩）。
- 抓取失败 → 用上次成功值 → 再失败用内置常量。三级兜底保证"最坏情况下仍能翻译"。
- ⚠️ **需确认**：`translate.google.com` 首页的 HTML/JS 结构会变，正则可能失效。上线后需监控 `tkk_refresh_failed` 指标；连续失败 > 10 次告警。

#### 3.3.5 批量切批与索引映射（决定"文字会不会跑到别处"）

##### 第一步：预过滤（避免长度错位）

Google 对**空串 `q`** 可能直接丢弃，导致响应数组比 `q` 数组短 1，**整批从该点开始全部错位**。这是最隐蔽的坑。因此：

```python
def prefilter(blocks: list[TextBlock]) -> tuple[list[int], list[str]]:
    """返回 (需要翻译的 block 下标, 对应文本)。不可翻译的直接置 dst=原文。"""
    idx, texts = [], []
    for i, b in enumerate(blocks):
        if not b.text.strip():
            b.dst, b.skipped_reason = b.text, "empty"
        elif is_numeric_or_symbol_only(b.text):        # 纯数字/纯符号/单字符
            b.dst, b.skipped_reason = b.text, "non_linguistic"
        elif b.confidence < cfg.ocr.min_confidence:
            b.dst, b.skipped_reason = b.text, "low_confidence"
        else:
            idx.append(i)
            texts.append(b.text)
    return idx, texts
```

纯数字不送翻译的价值：电商图上的「¥199.00」「2024-11-19」被 Google 改写（比如把 `199,00` 变成 `199.00`）是最常见的低级错误，跳过即可，还省字符。

> **归属定稿**：上图里的 `prefilter(blocks)` 属于**管线层（pipeline）**，不属于翻译层。理由：`confidence` 只有 OCR 层有、`is_numeric_or_symbol_only` 是文案语义判断，两者塞进翻译层都是耦合错位。
> **翻译层的唯一职责是"绝不发出空 `q`"**（空串 / 纯空白不发请求），这是它必须自我保证的正确性底线，不依赖上游。
> **翻译层不引入 `prefilter: Callable[[str], bool]` 之类钩子**——少一个注入点少一类配置错误，且钩子放正文里会诱使后人把业务规则写进翻译层。

##### 第二步：贪心装箱（保持原序）

```python
@dataclass
class Chunk:
    block_idx: list[int]      # 原始 block 下标，顺序不变
    texts: list[str]

def plan_chunks(texts: list[str], idx: list[int],
                max_chars: int = 4000, max_items: int = 50) -> list[Chunk]:
    chunks, cur_t, cur_i, cur_len = [], [], [], 0
    for i, t in zip(idx, texts):
        if len(t) > max_chars:                     # 单块超长，独占一批（极少见）
            if cur_t:
                chunks.append(Chunk(cur_i, cur_t)); cur_t, cur_i, cur_len = [], [], 0
            chunks.append(Chunk([i], [t]))
            continue
        if cur_t and (cur_len + len(t) > max_chars or len(cur_t) >= max_items):
            chunks.append(Chunk(cur_i, cur_t)); cur_t, cur_i, cur_len = [], [], 0
        cur_t.append(t); cur_i.append(i); cur_len += len(t)
    if cur_t:
        chunks.append(Chunk(cur_i, cur_t))
    return chunks
```

| 参数 | 取值 | 依据 |
|---|---|---|
| `max_chars` | **4000** | Google 硬上限 5000（`$maxSize`），留 20% 余量给 `implode` 后的实际长度统计差异 |
| `max_items` | **50** | 单请求 `q` 过多时响应体积与失败代价都上升；单图通常 5~80 块，50 是经验折中 |
| 单块 > 4000 字符 | 独占一批 | 实际只可能出现在超长标题，且大概率 OCR 误合并 |

##### 第三步：结果回填（禁止重排）

```python
async def translate_batch(self, blocks, source, target) -> None:
    idx, texts = prefilter(blocks)
    results: list[str | None] = [None] * len(texts)

    for chunk in plan_chunks(texts, idx):
        out = await self._call_with_retry(chunk.texts, source, target)   # 内部吞掉所有异常
        if out is not None and len(out) == len(chunk.texts):
            # 长度对上了才认，但逐个位置体检：非 str 只废这一个位置
            for j, item in enumerate(out):
                if isinstance(item, str):
                    results[chunk.offset + j] = item
                # 非 str → 该位置保持 None → 后续回填原文
        else:
            # 长度不匹配 / 整批失败：整批作废，逐块标记
            for j in range(len(chunk.texts)):
                results[chunk.offset + j] = None

    for j, bi in enumerate(idx):
        b = blocks[bi]
        dst = results[j]
        if dst is None or not postprocess_ok(b.text, dst, target):
            b.dst, b.mt_status = b.text, "failed"
        else:
            b.dst, b.mt_status = dst, "ok"
```

**失败占位语义（定稿，评审以本节为准）**：

| 情形 | 处理 | 结果 |
|---|---|---|
| `len(out) != len(chunk)` 或整批异常 | **整批作废**（绝不"按顺序猜"补位） | 该批每个块 `dst = 原文`，`mt_status = "failed"` |
| 长度对上、但某个元素不是 `str`（`None` / 空 / 非字符串） | **只废那一个位置**，其余位置正常采用 | 该块 `dst = 原文`，其余块 `"ok"` |
| 任意异常（网络、解析、超时） | **`translate_batch()` 永不抛出**，全部转成占位 | 调用方永远拿到"长度不变、内容可用"的块列表 |
| 返回值 | **`translate_batch()` 原地改 `blocks`，返回 `None`**；输出块数恒等于输入块数 | 上层不需要处理长度变化 |

> "永不抛出"是刻意的契约：翻译是链路上的**可降级环节**，任何一处抛异常都会把整张图打成 5xx。宁可出原文（用户能看懂一部分），也不要整图失败。

**四道防线（每一道都必须有对应测试，缺一不可）**：

| # | 防线 | 规则 | 对应测试 |
|---|---|---|---|
| 1 | **空白预过滤** | 空串 / 纯空白**不发请求**（`q` 为空会被服务端丢弃 → 响应少 1 条 → 整批错位） | `test_google_web_mock`（空块不发 q） |
| 2 | **长度严格校验** | `len(out) != len(chunk)` → **整批作废**，绝不"按顺序猜"；长度对上后仍逐元素体检，非 `str` 只废该位置 | T3 |
| 3 | **全局下标回填** | `results[chunk.offset + j]`，不是批内下标 | T2 |
| 4 | **重试同批同序** | 任何重试必须重发**同一批、同一顺序**，禁止把多批合并重试 | T6 |

> 为什么这四道缺一不可：任何一道缺失都会退化成"译文跑到别的文字块上"。这类 bug 用户一眼就能看见，而且**修不了**（因为错位后无法判断原本对应关系），只能整批重跑。宁可整批回填原文，也不能输出错位译文。

##### 第四步：译文质量闸门（`postprocess_ok`）

```python
def postprocess_ok(src: str, dst: str, target: str) -> bool:
    if not dst.strip():
        return False                                   # 空译文
    ratio = len(dst) / max(len(src), 1)
    if ratio > 6.0 or ratio < 0.08:
        return False                                   # 长度异常（通常是串行/串批）
    if script_of(dst) != expected_script(target) and target not in ("auto",):
        return False                                   # 目标拉丁却返回 CJK 等
    if dst.strip() == src.strip() and src_lang != target:
        return False                                   # 原样返回（Google 常对专有名词如此）
    return True
```

`ratio > 6` 是**串批/错位的强信号**——这是生产环境里最早报警的指标，建议接入监控。

#### 3.3.6 必须修的 PHP 缺陷清单

| # | 位置 | 问题 | 影响 | 修法 |
|---|---|---|---|---|
| 1 | L251/254/283/290/342/397/445 | `ucfirst(strtolower($text))` | 德语名词全小写、全大写标题失效、品牌名（iPhone→Iphone）被毁、CJK 目标语言下 `ucfirst` 无意义 | **整段删除**，只保留 `html.unescape()` |
| 2 | L57 | `'vi' => 'iv'` | 越南语被映射成不存在的语种码 | 改为 `'vi' => 'vi'` |
| 3 | L645-656 | `convertLang()` 先 `strtoupper($code)` 再 `in_array($code, ['he'])` | 大写后永远匹配不到小写数组，**`he→iw` 从未生效** | 统一小写比较：`LANG_ALIAS.get(code.lower(), code.upper())` |
| 4 | L267/326/427 | 循环内修改 `$this->target` | 同一 `langs` 数组内多语言时，若并发执行会取到错误的 `tl` | Python 侧改为局部变量，不落实例 |
| 5 | L244-247 | `json_decode` 后仅判 `empty()` | 若返回 `{"error":...}`，`array_map` 对数组调用 `strtolower` 会抛错/产生脏数据 | 显式校验「是 `list[str]` 且长度匹配」 |
| 6 | L513 | `count($sentences) == $count` 才一一对应 | 不等时走 `getTransTexts` 猜测对齐 | 图片翻译**不用该分支**，直接判失败 |
| 7 | L235/L241 | `timeout => 3` | 图片翻译的文本块多、单批字符多，3 s 偏短，跨境网络下频繁超时 | 连接 2 s / 读 **6~8 s** |
| 8 | L481 | `iid` 硬编码固定 UUID | 可能被判为重放 | 每次请求生成 `uuid4()` |
| 9 | L706-708 | `TKK()` 硬编码 | Google 已轮换，签名校验失败 | 动态抓取 + 三级兜底（§3.3.4） |
| 10 | L122 | `array_rand` 取账号 | 无健康度权重，坏账号会被反复选中 | 加权轮询 + 失败熔断 |

#### 3.3.7 限流、退避与网络出口（含「本机不可直连」硬约束）

##### 令牌桶限流

```python
class TokenBucket:
    """容量 = max(1, qps)。不设独立 burst 参数（见下方决策）。"""
    def __init__(self, qps: float):
        self.rate = float(qps)
        self.capacity = max(1.0, self.rate)
        self.tokens = self.capacity
        self.ts = time.monotonic()

    async def acquire(self, n: int = 1) -> None:
        while True:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.ts) * self.rate)
            self.ts = now
            if self.tokens >= n:
                self.tokens -= n
                return
            await asyncio.sleep((n - self.tokens) / self.rate)
```

| 场景 | 建议值 | 说明 |
|---|---|---|
| 默认 QPS | **`qps: float = 4.0`**，容量 = `max(1, qps)`，**不引入独立 `burst` 参数** | 已定稿：图片翻译是低频批量调用（单图 1~3 次请求），burst 收益不明显，少一个参数少一份配置负担。原"3 QPS / burst 6"的草案作废 |
| 单实例上限 | 5 | 超过 5 QPS 建议加实例/加出口 IP，而不是调高单实例 |
| 每图请求数 | 通常 1~3（按切批） | 100 块以内的图基本 1 批搞定 |

**吞吐换算**：QPS 3、每图 1.2 次请求 → 约 **2.5 图/秒**≈ **21 万张/天**的翻译侧能力。而 GPU 侧只有 **0.4~0.7 QPS**，所以**翻译永远不是瓶颈，OCR/擦除才是**。这个结论很重要——它意味着不需要为翻译做复杂的并发优化。

##### 重试与退避

```python
@dataclass
class RetryPolicy:
    attempts: int = 3
    base_ms: int = 500
    factor: float = 2.0
    jitter_ms: int = 300
    retry_status: tuple = (403, 429, 500, 502, 503, 504)

    def delay(self, n: int) -> float:
        d = self.base_ms * (self.factor ** (n - 1))
        return (d + random.uniform(0, self.jitter_ms)) / 1000
```

| 状态 | 处置 |
|---|---|
| **403** | 先 `refresh_tkk(force=True)`，再原样重试 1 次；仍 403 → 降级 `free` |
| **429 / 503** | 指数退避（0.5 s → 1 s → 2 s + 抖动），最多 3 次；仍失败 → 降级 |
| **5xx 其他** | 同 429 |
| **超时** | 同 429，但**不计入** `403` 的 TKK 刷新 |
| **响应非 JSON / 空数组** | 视为失败，重试 1 次后降级 |
| **降级后仍失败** | 该块 `mt_status=failed`，回填原文，整体响应 `code=0` 但 `warnings` 带 `partial_mt_failure` |

**熔断（P0 必做，非可选项）**：连续 20 次 `403` 或 15 次 `429` → 打开 60 s，避免把出口 IP 彻底打死。三条语义缺一即视为未实现：

1. **打开期间 `translate_batch()` 直接回原文**（不排队、不 sleep、不发请求），保证上层响应时间可预测、且不给出口继续加压；
2. **`circuit_open` 只读属性**（`@property`，无 setter），供 pipeline / 健康检查 / Prometheus 指标读取；
3. **60 s 到期转半开，只放 1 个探测请求**：成功 → 闭合并清零失败计数；失败 → 重新计时 60 s（不做指数增长，避免长尾）。

##### 网络出口：本机无法直连 Google（**硬约束，已实测**）

这是本方案**最容易被低估的前置条件**，必须在开工前解决，否则整条翻译链路不可用。

**实测结论（2026-11-19，部署机）**：

| 目标 | 结果 |
|---|---|
| `curl -m 8 https://translate.googleapis.com/` | 返回码 **000**（连接未建立），8 s 超时 |
| `curl -m 8 https://translate.google.com/` | 返回码 **000**，8 s 超时 |

结论：**web 免费通道（`api` / `free`）必须经由海外出口**，本机直连不可行。这不是"性能优化"，是**可用性前提**。

**三种出口方案对比**：

| 方案 | 说明 | 延迟增量 | 稳定性 | 成本 | 推荐度 |
|---|---|---|---|---|---|
| **A. 境外节点 + 内网隧道** | 在海外 VPS 起 `squid`/`tinyproxy`/`gost`，本机经内网/隧道走该出口 | +80~200 ms | 高（可控） | VPS 约 40~80 元/月 | **首选** |
| **B. 商业代理池** | 购买海外住宅/机房代理，按 IP 或流量计费 | +100~400 ms | 中（第三方质量波动） | 500~3000 元/月 | 备选（需多 IP 分流时） |
| C. 本机多网卡绑定 | 仅解决"本机有多个公网 IP"的分散问题，**不解决可达性** | 0 | — | 0 | 只在已有海外出口时叠加使用 |

⚠️ **A 与 C 是叠加关系不是替代关系**：A 解决"能不能连上"，C 解决"多个 IP 分摊限流"。当前的 `CURLOPT_INTERFACE` 逻辑（PHP 侧 `CURL_MULTI_IP`）只有在 A 已具备时才继续有意义。

```python
# 出口抽象：把"可达性"和"IP 分散"两个维度分开建模
@dataclass
class Endpoint:
    kind: Literal["proxy", "local_address"]
    value: str                 # "http://user:pass@host:port" 或 "10.0.0.12"
    region: str                # "hk" / "sg" / "us"
    healthy: bool = True

class EgressPool:
    """加权随机 + 失败降权 + 冷却。任一端点连续失败 3 次进 120s 冷却。"""
    def pick(self) -> Endpoint: ...
    def report(self, ep: Endpoint, ok: bool) -> None: ...

# 构造
def build_client(ep: Endpoint) -> httpx.AsyncClient:
    if ep.kind == "proxy":
        return httpx.AsyncClient(proxy=ep.value, timeout=TIMEOUT,
                                 limits=httpx.Limits(max_connections=32))
    transport = httpx.AsyncHTTPTransport(local_address=ep.value)
    return httpx.AsyncClient(transport=transport, timeout=TIMEOUT)
```

**超时预算（跨境外呼必须比内网宽）**：

| 参数 | 值 | 理由 |
|---|---|---|
| `connect_timeout` | **3 s** | 代理握手 + TLS，比直连多一跳 |
| `read_timeout` | **8 s** | 原 PHP 3 s 在跨境场景频繁超时（§3.3.6 #7） |
| `write_timeout` | 3 s | 请求体小 |
| `pool_timeout` | 2 s | 拿连接 |
| **单请求总预算** | **10 s** | |
| **单图翻译总预算** | **20 s** | 覆盖 3 批 × 重试；超预算则该图剩余块回填原文 |
| **端到端请求超时** | 30 s（同步接口） | 超出转异步 |

**故障切换（三级，按顺序自动降级）**：

```
出口 A（境外节点）──失败──► 出口 B（备用节点/代理池）
                              │
                              └──失败──► 直连（本机不通，仅作探测确认）
                                            │
                                            └──失败──► 通道降级：
                                                        api → free → google_cloud → 原文占位
```

**健康探测**（必须做，否则故障发现延迟以"小时"计）：
- 每 30 s 对一个**极短文本**（如 `"ok"`）发一次探测请求，记录 RTT 与状态
- 连续 3 次失败 → 端点置为 `healthy=False`，冷却 120 s 后半开探测
- 探测本身**不计入**业务 QPS 令牌桶，但计入出口端点的独立限流（避免探测把配额打满）

##### 稳定性与合规风险（必须让用户知情）

| 风险 | 具体表现 | 缓解 |
|---|---|---|
| **tk/TKK 轮换** | 算法或常量变更 → 全量 403 | 动态抓 TKK（§3.3.4）+ 403 即刷新 + 熔断 |
| **限流/封 IP** | 429 激增、返回空数组 | 令牌桶 3 QPS + 多出口 + 熔断 + 指数退避 |
| **接口变更** | `translate_a/t` 参数或响应结构变化 | 显式校验响应结构，异常即 `E_PARSE` P1 告警 |
| **出口节点单点** | 海外 VPS 宕机 → 全链路不可用 | 至少两个节点 + 健康探测 + 自动切换 |
| **ToS 灰区** | 自动化调用非公开 API，Google 可随时封禁 | 详见 §8.2；对外**不承诺 SLA** |
| **法律与数据出境** | 图片文字内容传至境外服务器 | 敏感业务提供"仅 OCR 不翻译"模式；详见 §8.2 |

**降级开关设计（配置驱动，不改代码即可切换）**：

```yaml
translate:
  # 一刀切开关：关闭 web 通道，全部走付费官方 API
  web_enabled: false

  fallback:
    order: [api, free, google_cloud, passthrough]
    # 紧急时改成：[google_cloud] 即强制走官方 API
    # 或 [passthrough] 即只 OCR 不翻译
```

| 场景 | 配置动作 | 生效时间 |
|---|---|---|
| 出口节点全挂 | `web_enabled: false` | 下一个请求（配置热加载） |
| tk 大面积 403 | 自动熔断 → 走 `free` → 仍失败走 `google_cloud` | 自动，≤60 s |
| 法务要求停止境外传输 | `order: [passthrough]` | 下一个请求 |
| 成本失控 | `order: [google_cloud]` + 提高缓存 TTL | 下一个请求 |

**多网卡绑定 / 代理池的实现细节**：

```python
# 路径 A：多网卡绑定（与 PHP 的 CURLOPT_INTERFACE 完全等价）
transport = httpx.AsyncHTTPTransport(local_address="10.0.0.12")   # httpx >= 0.27
# aiohttp 等价：aiohttp.TCPConnector(local_addr=("10.0.0.12", 0))

# 路径 B：代理
client = httpx.AsyncClient(proxy="http://user:pass@1.2.3.4:8080")
```

| 实现 | 前提 | 注意 |
|---|---|---|
| 多网卡绑定（与现状一致） | 本机已有多个公网 IP，**且这些 IP 能到达 Google** | 需系统层配好路由；`local_address` 必须是本机已存在地址，否则 `OSError` |
| 代理 | 有一个能出海的 HTTP/SOCKS5 代理 | 注意 `httpx` 对 SOCKS 需装 `httpx[socks]`；认证走 URL userinfo |
| 代理池 | 多个代理 + 健康度 | 需处理代理不可用、返回 407、被目标站拉黑三种情况 |

实现建议：`EgressPool` 持有一组"IP/代理 → 健康度"记录，加权随机选择 + 失败降权 + 120 s 冷却（比原来的 60 s 更长，因为跨境外呼恢复慢）。

##### 本机联调的真实做法（无海外出口时）

本机连不上 Google，但不代表不能开发。三种手段：

| 手段 | 用途 | 成本 |
|---|---|---|
| **Mock Server**（`respx` / 自建 FastAPI 桩） | 单测与 CI：模拟 200/403/429/空数组/长度不匹配等 8 种响应 | 0.5 人天，**必做** |
| **录制回放**（VCR 风格） | 在能出海的环境录一次真实请求/响应，回放给单测 | 0.2 人天 |
| **境外节点冒烟** | 上线前在 VPS 上跑 `python -m imgtrans.translator.cli -t en "你好"` | 需先备好节点 |

**结论**：CI 全绿只能证明"逻辑对"，**不能证明"能连通"**。上线前必须有一次真实出海冒烟，这一条写进 P0 验收。


#### 3.3.8 后处理与幂等

```python
def postprocess(text: str) -> str:
    text = html.unescape(text)                  # &#039; → '  &#39; → '
    text = UNDEFINED_ENTITY.sub("", text)       # 清掉 &amp;lt; 这类双重转义残留
    text = text.replace("\u00a0", " ")          # NBSP → 空格
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return text
```

**明确不做的事**：
- ❌ 不做大小写变换（见 §3.3.6 #1）
- ❌ 不做全角/半角转换（会破坏 CJK 标点）
- ❌ 不做标点替换（德语引号、法语空格规则不同）
- ❌ 不做"去重空格后再拼接"（会改变用户原文语义）

**幂等性**：同一 `(url, to, options)` 的两次请求必须返回相同结果，才能安全缓存。
- LaMa 确定性 ✓
- 渲染确定性 ✓（同字体同字号）
- Google 翻译**非严格确定**（同一文本两次可能微调）→ 所以**必须缓存，且缓存 key 不含随机量**。缓存 TTL 建议 24 h。

**文本级翻译缓存**（强烈建议，独立于图片缓存）：

```python
key = f"mt:{sha1(f'{src}|{source}|{target}|{glossary}')}"
```
电商场景下"加入购物车""限时特价""包邮"这类文案跨图上万次重复，**实测命中率可达 30%~50%**，直接省掉一半请求，也就一半被限流的风险。

#### 3.3.9 可选付费兜底：Google Cloud Translation

对应 PHP 的 `nmtApi()`。Python 用官方 SDK `google-cloud-translate==3.x`：

```python
from google.cloud import translate_v2 as translate

class GoogleCloudTranslator(BaseTranslator):
    def translate_batch(self, texts, source, target):
        client = translate.Client()
        out: list[str] = []
        for chunk in chunked(texts, 128):                 # 与原 PHP 一致：128/批
            resp = client.translate(
                chunk,
                source_language=to_cloud_code(source),
                target_language=to_cloud_code(target),
                model="nmt",                              # 与原 PHP 一致
                format_="text",
            )
            out.extend(r["translatedText"] for r in resp)
        return out
```

| 项 | 值 |
|---|---|
| 单价 | **$20/百万字符**（Google Cloud Translation v2/v3 标准 NMT，已核实） |
| 免费额度 | 50 万字符/月 |
| 适用 | 仅在 `api` 与 `free` 都不可用、且业务要求 SLA 时启用 |
| 注意 | `translateBatch` 在 v2 SDK 中实际是循环调用，**不是真批量**；128/批是原代码的切分粒度 |

**建议**：默认不启用。它是一个"保险"，只有用户愿意为确定性付费时才开。

⚠️ **重要前提**：Cloud Translation v2 的官方端点写作 `translation.googleapis.com/language/translate/v2`，而 Google 自家 NMT 文档的示例又写 `translate.googleapis.com/language/translate/v2` —— **两个域名都出现过、都能服务 v2，但它们是不同域名**；且同属 `googleapis.com` 域族，**境内均不可达**。所以**付费并不等于能连通**，实测下 `translate.googleapis.com` 与 `translation.googleapis.com` 同样超时。因此：

| 需求 | 能满足的通道 |
|---|---|
| 拿掉"海外出口"依赖 | **只有境内 API**（百度翻译 49 元/百万字符，境内可直连） |
| 拿掉"tk 可能失效"风险 | Google Cloud（仍需出口）或百度 |
| 都不拿掉，只要便宜 | 当前 web 通道方案 |

若用户的核心诉求是"不要再为网络操心"，则应直接改用**百度通用翻译**，而不是 Google Cloud。这条建议请与 §10.1 决策 2 一起看。

#### 3.3.10 配置项清单

```yaml
translate:
  driver: google_web              # google_web | google_cloud
  mode: api                       # api（主）| free（降级）  cn 已废弃，配置即报错

  endpoints:
    api:  "http://translate.googleapis.com"
    free: "https://translate.google.com"
    tkk_source: "https://translate.google.com/"

  timeouts:
    connect_ms: 2000
    read_ms: 6000                 # 原 PHP 3s 偏短
    total_ms: 8000

  batching:
    max_chars_per_chunk: 4000     # 硬上限 5000，留余量
    max_items_per_chunk: 50
    single_item_max_chars: 4000

  tkk:
    fallback: "406398.2087938574"     # 字符串，与 Google 接口原生形态一致
    tkk_ttl: 21600                    # 6h；与 TkkProvider(tkk_ttl=21600) 同名同义，无别名
    refresh_on_403: true
    refresh_retry: 1

  rate_limit:
    qps: 4.0                      # 桶容量 = max(1, qps)；不设独立 burst

  retry:
    attempts: 3
    base_ms: 500
    factor: 2.0
    jitter_ms: 300
    retry_status: [403, 429, 500, 502, 503, 504]

  circuit_breaker:
    fail_threshold: 20
    open_seconds: 60

  egress:
    mode: multi_nic               # single | multi_nic | proxy_pool
    interfaces: ["10.0.0.12", "10.0.0.13"]
    proxies: []

  fallback:
    order: [api, free, google_cloud, passthrough]   # 最后一级 = 回填原文

  postprocess:
    html_unescape: true
    normalize_nbsp: true
    lowercase_ucfirst: false      # 显式关闭，注释说明这是刻意为之

  cache:
    text_cache_ttl_s: 604800      # 文本级缓存 7 天
    text_cache_max_entries: 200000

  accounts: []                    # free 通道的 domain+cookie 列表
  proxy: null
  multi_ip: true
```

---

## 4. 「文字背景透明」与样式还原（本方案最难的部分）

### 4.1 先厘清"透明"的三种含义

用户说的"保持文字背景透明"，在不同人脑中是三件不同的事。**必须先对齐，否则做完了也不满意**。

| 理解 | 具体形态 | 百度对应 | 我方实现 | 是否默认 |
|---|---|---|---|---|
| **① 译文不画底色块** | 擦除原文字后，译文直接"浮"在原背景上，文字周围是原始图像内容，没有白色/彩色矩形 | `paste=1` | `mode=full`：`erased_image` 上直接 draw text，**不 fill 任何背景** | **是（默认）** |
| **② 输出带 alpha 的文字图层** | 输出 PNG，只有译文文字不透明，其余全透明，供前端/设计工具叠加 | `paste=3` 只给坐标 | `mode=text_layer`：RGBA 图层，`text` 的 alpha=255，其余 alpha=0 | 否（可选） |
| **③ 原文字区域变透明** | 把原图文字擦成"洞"，输出带 alpha 的图 | 无 | `mode=cutout`：擦除区域 alpha=0 | 否（很少用） |

**本方案的默认与推荐是 ①**。它同时满足"背景透明"（没有新增色块）和"保持原图观感"（背景纹理保留），也是绝大多数用户真正想要的。

②③ 作为可选模式提供，成本很低（都是同一条渲染管线换 composition 方式），但能覆盖设计师/前端二次编辑的场景。

**关键结论**：①的成败 **90% 取决于擦除质量，10% 取决于渲染**。如果擦除后留下方块补丁或文字残影，那么"文字背景透明"就是空话。这就是为什么 §3.2 的像素级 mask 是重中之重。

### 4.2 样式分析算法

每个 `TextBlock` 要复原 6 个属性。**全部只用原图像素估计，不引入外部模型**（快、可控、可解释）。

#### 4.2.1 前景色（文字填充色）

朴素做法（取框内所有像素均值）是错的——会被背景和描边污染。正确做法是**只采笔画中心像素**：

```python
def estimate_foreground(roi: np.ndarray, mask: np.ndarray) -> tuple[RGB, RGB | None, int]:
    # 1) 距离变换：得到每个文字像素到背景的距离
    dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    # 2) 只取"内切核心"像素（距离 >= 0.6*局部最大），避开抗锯齿混色带
    core = dist >= 0.6 * dist.max()
    core_px = roi[core]                                   # (N,3) RGB

    # 3) KMeans(k=2)：若存在明显双峰 → 一簇是字芯、一簇是描边
    k = 2 if len(core_px) > 200 else 1
    labels, centers = kmeans(core_px, k=k, seed=42)
    fill = centers[np.argmax(np.bincount(labels))]        # 像素多的是填充色
    stroke = centers[np.argmin(np.bincount(labels))] if k == 2 else None

    # 4) 与背景色对比度校验：若 fill 与背景色差 < 30，说明估计失败，回退
    if color_distance(fill, estimate_background(roi, mask)) < 30:
        fill = pick_by_contrast(roi, mask)
    return fill, stroke, 0
```

#### 4.2.2 描边色与描边宽度

```python
def estimate_stroke_width(roi, mask, stroke_color) -> int:
    """mask 边缘一圈若被 stroke 色占据，其环厚即描边宽。"""
    edge_ring = mask & ~erode(mask, 1)                    # mask 的 1px 边界
    if stroke_color is None:
        return 0
    hit = color_distance(roi[edge_ring], stroke_color) < 40
    if hit.mean() < 0.5:                                  # 边界不是描边色 → 无描边
        return 0
    # 逐像素向内探测，统计连续命中 stroke 色的深度
    depths = []
    for _ in range(6):
        edge_ring = mask & ~erode(mask, 1)
        ...
    return int(np.median(depths))
```

描绘边带来两个直接收益：
1. **mask 膨胀量有据可依**（避免留描边残影，见 §3.2.3）；
2. **回填时能画同样的描边**，译文在深色背景上的可读性与原文一致。

> 实践经验：白字黑描边是场景图最常见的组合。缺少描边还原时，译文在浅色背景上会"糊掉"，用户第一反应是"你这翻译不清晰"。

#### 4.2.3 背景色 / 渐变

取 **mask 外扩 4~8 px 的环带**，用**中位数**而非均值（抗 JPEG 块噪声与孤立亮点）。

```python
def estimate_background(roi, mask) -> BGSpec:
    ring = dilate(mask, 6) & ~dilate(mask, 1)
    px = roi[ring]
    if px.std(axis=0).max() < 8:
        return SolidSpec(median(px))
    if (g := fit_linear_gradient(ring, px)).r2 > 0.9:
        return g
    return TextureSpec(sample_grid=px[::max(1, len(px)//64)])   # 纹理，仅作参考
```

#### 4.2.4 对齐方式

```python
def estimate_align(lines: list[BBox]) -> Align:
    lefts  = np.array([l.x0 for l in lines])
    rights = np.array([l.x1 for l in lines])
    if len(lines) == 1:
        return Align.LEFT                 # 单行无法判断，默认左对齐
    l_var = lefts.std()
    r_var = rights.std()
    tol = 0.06 * (max(rights) - min(lefts))
    if l_var < tol and r_var > tol:  return Align.LEFT
    if r_var < tol and l_var > tol:  return Align.RIGHT
    if l_var < tol and r_var < tol:  return Align.CENTER
    return Align.LEFT                     # 两端不齐 → 左对齐（最安全）
```

⚠️ 阿拉伯/希伯来等 RTL 语言，`Align.LEFT/RIGHT` 需在渲染时镜像（RTL 的"左对齐"视觉上是从右起排）。

#### 4.2.5 字号、字重、行高

| 属性 | 估计方法 |
|---|---|
| 字号 | `font_size ≈ (block.height / line_count) * k`，`k` 按文字系统取：CJK 0.86、拉丁 0.72、阿拉伯 0.78。再用**实测 cap-height 校正**：渲染一个候选字号，量其实际墨迹高度与 `line.height` 的比值，误差 < 5% 即接受 |
| 字重 | `stroke_width / font_size`：< 0.055 → Regular；0.055~0.085 → Medium；> 0.085 → Bold。**只支持三档**（Regular/Medium/Bold），不做连续插值 |
| 行高 | 多行 block 的相邻基线间距中位数；单行取 `1.2 × font_size` |
| 斜体 | 对 mask 做 Hough/PCA 求笔画的平均倾斜角，> 6° 判定斜体。**默认关闭**（误判率较高，误判会让正文字变斜，比不还原更糟） |

### 4.3 译文膨胀的应对策略（中→德/俄会涨 30%~40%）

这是**必然发生**的问题，不是异常。必须有一套有序的降级策略，而不是"塞不下就算了"。

```
测量：required_width(译文, font_size) vs box.width
required_height(译文, font_size, line_height) vs box.height
```

**策略优先级（从上到下依次尝试，一旦满足即停止）**：

| 优先级 | 策略 | 约束条件 | 副作用 |
|---|---|---|---|
| **P1** | **横向扩框** | 左右各扩 ≤ 12% 框宽，且扩后不与相邻 block 的 bbox 相交（间距 > 8 px） | 无（场景图通常有留白） |
| **P2** | **增加行数** | 上方/下方有空白，扩后高度 ≤ 原高 × 1.5，不压盖相邻块 | 轻微破坏版式 |
| **P3** | **缩字号** | 下限 `0.72 ×` 原字号，且不小于 10 px | 可读性下降，但仍在可接受范围 |
| **P4** | **语义压缩重译** | 仅当引擎为 LLM 时可用（NMT 不可控） | 译文信息量可能损失，**需在 JSON 里标记 `compressed=true`** |
| **P5** | **截断 + 省略号** | 最后手段，仅用于 `priority=low` 的块（水印式小字） | 信息丢失，**必须标记 `truncated=true`** |
| **P6** | **回填原文 + 附译文** | 极端情况（框极小 + 译文极长） | 视觉上未翻译，但 JSON 里有译文，前端可换行展示 |

**实现要点**：

```python
def fit_text(text, box, style, policy) -> RenderPlan:
    size = style.font_size
    for step in policy:                       # P1→P6 依次
        plan = step.try_apply(text, box, style, size)
        if plan.fits():
            plan.degraded = step.name != "P1"
            return plan
    return policy.last_resort(text, box, style)   # P6
```

**"一行一译"vs"整段重排"**：默认 **块内整段重排**（因为翻译后的断句位置和原文完全不同，逐行对应必然错位）。只有当 block 被判定为"独立标签"（如按钮文字、单行标题，`line_count == 1`）时，才 1:1 替换。

**渲染前的宽度实测**（不能用估算）：Pillow 的 `font.getbbox()` / `draw.textlength()` 给出的是真实排版宽度，必须用它做二分搜索，不要用"字符数 × 平均字宽"估算——后者在 CJK 与拉丁混排时误差可达 30%。

### 4.4 超采样渲染（保证边缘清晰）

```python
SCALE = clamp(2, 4, round(target_font_px / 12))     # 小字多倍，大字 2 倍即可

def render_block(base: Image.Image, block, plan) -> Image.Image:
    s = SCALE
    layer = Image.new("RGBA", (block.w * s, block.h * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = font_registry.get(block.target_lang, plan.font_size * s, plan.weight)
    for i, line in enumerate(plan.lines):
        x = align_x(line, block.w * s, plan.align, block.target_lang)
        y = plan.pad_top * s + i * plan.line_height * s
        if plan.stroke_width:
            draw.text((x, y), line, font=font, fill=plan.fill,
                      stroke_width=int(plan.stroke_width * s), stroke_fill=plan.stroke_color,
                      anchor="la")
        else:
            draw.text((x, y), line, font=font, fill=plan.fill, anchor="la")
    layer = layer.resize((block.w, block.h), Image.LANCZOS)     # 下采样 = 抗锯齿
    return Image.alpha_composite(base.crop(block.box), layer)
```

**为什么这样做**：FreeType 在目标字号已有抗锯齿，但**边缘的"虚实过渡"只有 1 px**，缩小看会显得发灰发软。渲染到 2~3 倍再 LANCZOS 下采样，过渡带宽度变成 2~3 px，视觉上明显更锐利、更接近原图印刷字体的观感。这是"保持清晰度"里唯一有主观感知收益的一步。

---

## 5. 清晰度保障（像素保真）

| 环节 | 措施 | 为什么 |
|---|---|---|
| 下载 | 原样字节读取，**不解码后再编码** | 避免无意义的一次重编码 |
| 解码 | 只解码一次，之后全程 `np.ndarray` / `PIL.Image` 内存对象 | 中间结果**永不落盘为 JPEG** |
| EXIF | 读入时 `ImageOps.exif_transpose()`，输出**剥离 EXIF** | 否则部分手机会显示旋转 90°；同时避免泄露 GPS |
| ICC | 有 profile 则 `ImageCms` 转换到 sRGB；输出写回 sRGB | 否则颜色发灰/偏色，用户会认为"画质变差" |
| 缩放 | **整图零缩放**。OCR 需要放大时**只放大 crop**，坐标除回原尺寸 | 整图 resize 再还原 = 两次重采样 = 必然掉清晰度 |
| 超大图 | 仅 OCR 阶段切 tile（overlap 192 px），**像素处理阶段永远在全图坐标上进行** | 避免拼接缝与二次重采样 |
| 擦除 | 只替换 mask 内像素，mask 外用 `alpha_composite` 软融合 | 保证未擦除区域**逐字节不变** |
| 渲染 | 超采样 + LANCZOS 下采样（§4.4） | 边缘锐利 |
| 编码 | **单次**输出编码：JPEG `quality=95, subsampling=0, optimize=True`；或 PNG（无损）；或 WebP `quality=92, method=6` | `subsampling=0` 即 4:4:4，不做色度抽样（默认 4:2:0 会让文字边缘发糊） |

**输出格式建议**：

| 格式 | 参数 | 体积（1200×800 实景图） | 适用 |
|---|---|---|---|
| **JPEG（默认）** | `quality=95, subsampling=0, optimize=True` | ~320 KB | 通用；兼容性最好 |
| PNG | `optimize=True, compress_level=6` | ~1.2 MB | 需要无损 / 带 alpha（`text_layer` 模式强制 PNG） |
| WebP | `quality=92, method=6` | ~180 KB | 带宽敏感；需确认调用方支持 |

⚠️ **不要用 `quality=100`**：JPEG 在 100 时体积翻倍但视觉无提升，反而因量化表退化可能出现振铃。

**超大图阈值**：`max(width, height) > 4096` 触发 OCR 切片；`> 8000` 或字节 `> 20 MB` 直接拒绝（`40003`），避免 OOM。

---

## 6. 接口设计

### 6.1 同步 vs 异步：对比与推荐

| 维度 | 同步 `POST /v1/image/translate` | 异步 `POST /v1/image/translate/async` + `GET /v1/tasks/{id}` |
|---|---|---|
| 实现复杂度 | 低 | 中（需任务存储、状态机、回调） |
| 客户端体验 | 简单，一次请求拿结果 | 需轮询/回调 |
| 长任务 | 受 HTTP 超时限制（建议 30 s） | 无限制 |
| 失败重试 | 客户端整体重试 | 服务端可控重试 |
| GPU 排队 | 请求占着连接等 | 不占连接 |
| 适用 | 单图、P95 ≤ 6 s | 批量、大图、`erase.quality=hq` |

**推荐：双通道，默认同步。** 判定规则：

- 预估耗时 ≤ 15 s → 同步（绝大多数单图）
- 预估 > 15 s（超大图 / `hq` / `mode=blocks` 且块数 > 100）→ 接口自动转异步，返回 `202 + task_id`
- 批量（一次多图）→ 强制异步

预估方式：`est = 0.4*ocr + 0.3*blocks + 0.5*(hq?3:1)` 秒，简单线性模型足够。

### 6.2 请求 / 响应契约

> **实现现状（2026-11-19）**：下面这版带 HMAC 签名、嵌套 `render`/`erase`/`ocr` 选项的契约是
> **设计目标**，尚未落地。当前实际交付的是 **API Key 鉴权的 4 个入口**，字段刻意精简
> （少一个参数就少一类线上配置错误）：
>
> | 方法与路径 | 请求体 | 响应 |
> |---|---|---|
> | `POST /v1/translate/img` | JSON `{"lang","img"[,"source"]}`，`img` 支持 URL 或 base64 | JSON（`image_base64` + `blocks` + `timings`） |
> | `POST /v1/translate` | JSON `{"url","target"[,"source"]}` | 图片二进制；`?detailed=1` 转 JSON |
> | `POST /v1/translate/upload` | multipart `file` + `target` | 图片二进制；`?detailed=1` 转 JSON |
> | `GET /v1/languages` | — | `{"targets":[…],"count":35}` |
>
> 鉴权：请求头 `X-Api-Key: <key>` 或 `Authorization: Bearer <key>`，key 来自 `.env` 的
> `IMGTRANS_API_KEY`。错误统一为 `{"error":{"code","message"}}`，`code` 取值见
> `imgtrans/api/app.py::_ERROR_STATUS`。
>
> **推理设备**（2026-11-19 补充）：`.env` 的 `IMGTRANS_DEVICE` 决定全链路跑在哪，
> 取值 `cpu`（默认）/ `gpu` / `gpu:0` / `gpu:1` / `auto`（有卡就用卡，无卡回落 cpu），
> 命令行 `--device` 优先级更高。`GET /healthz` 会如实返回最终生效的 `"device"`，
> 部署时 `curl` 一下就能确认，不必翻日志。配了 `gpu` 但当前 paddle 是 CPU 版时
> **启动即失败**（`E_DEVICE`），不静默降级——静默降级会让人以为在吃卡，实际慢十几倍。

#### 请求

```http
POST /v1/image/translate
Content-Type: application/json
X-App-Id: your_app_id
X-Timestamp: 1763558400
X-Nonce: 8f3a1c2e
X-Sign: <hex(hmac_sha256(secret, app_id + timestamp + nonce + sha256(body)))>
```

```jsonc
{
  "image_url": "https://example.com/a.jpg",   // 与 image_base64 二选一（二选一校验）
  "image_base64": null,
  "from": "auto",                              // auto | zh | en | ja | ko | ...
  "to": "en",                                  // 必填，目标语言

  "render": {
    "mode": "full",                            // full | blocks | json | text_layer | cutout
    "text_background": "transparent",          // transparent(默认) | sampled | solid
    "stroke": "auto",                          // auto | none | {color,width}
    "font_scale_max": 1.0,                     // 允许的最大放大倍数（膨胀策略 P1 用）
    "output_format": "jpeg",                   // jpeg | png | webp
    "quality": 95
  },

  "erase": {
    "quality": "fast",                         // fast(LaMa) | hq(PowerPaint V2) | auto
    "engine": "auto"                           // auto | lama | sd | analytic | telea
  },

  "ocr": {
    "min_confidence": 0.5,
    "vertical": "auto",                        // auto | on | off
    "drop_non_linguistic": true
  },

  "translate": {
    "engine": "google_web",                    // google_web | google_cloud
    "glossary_id": null,
    "cache": true
  },

  "return": {
    "image": true,
    "blocks": true,
    "mask": false,
    "timing": false
  },

  "callback_url": null,                        // 异步模式回调
  "request_id": "optional-client-trace-id"
}
```

#### 响应（成功）

```jsonc
{
  "code": 0,
  "message": "ok",
  "request_id": "optional-client-trace-id",
  "cached": false,
  "data": {
    "from": "zh",
    "to": "en",
    "image": {
      "base64": "/9j/4AAQSkZJRg...",
      "format": "jpeg",
      "width": 1200,
      "height": 800,
      "size_bytes": 318442
    },
    "blocks": [
      {
        "id": 1,
        "src": "限时特价",
        "dst": "Limited-time offer",
        "rect": [236, 71, 311, 101],
        "polygon": [[236,71],[311,71],[311,101],[236,101]],
        "line_count": 1,
        "vertical": false,
        "confidence": 0.97,
        "mt_status": "ok",
        "font": {
          "size": 24,
          "color": "#FFFFFF",
          "weight": "bold",
          "italic": false,
          "family": "NotoSansCJKsc-Bold"
        },
        "stroke": { "color": "#1A1A1A", "width": 2 },
        "align": "center",
        "background": { "type": "solid", "colors": ["#C8102E"] },
        "render": {
          "font_size": 22,
          "scale": 0.92,
          "lines": 1,
          "overflow": false,
          "degraded": null,
          "truncated": false,
          "compressed": false
        }
      }
    ],
    "stats": {
      "blocks_total": 12,
      "translated": 11,
      "failed": 1,
      "skipped": 0,
      "chars_sent": 186,
      "chunks": 1
    },
    "warnings": ["partial_mt_failure"]
  },
  "timing_ms": {
    "total": 1590, "download": 120, "normalize": 40, "ocr": 480,
    "group": 5, "translate": 310, "mask": 35, "inpaint": 420,
    "style": 20, "render": 90, "encode": 60, "queue_wait": 10
  }
}
```

#### 响应（部分失败）

`code` 仍为 `0`（图已产出），但 `warnings` 与 `blocks[].mt_status` 标记：

```jsonc
{ "code": 0, "warnings": ["partial_mt_failure"], "data": { ... } }
```

**这是有意的设计**：翻译失败但图片已生成，返回 500 会让调用方丢弃结果，用户体验更差。调用方按 `warnings` 决定是否提示。

### 6.3 错误码表

| code | HTTP | 含义 | 调用方处置 |
|---|---|---|---|
| 0 | 200 | 成功 | — |
| 40001 | 400 | 参数缺失/非法 | 修参数 |
| 40002 | 400 | 语言不支持 | 换语言 |
| 40003 | 413 | 图片超限（>10 MB 或 >8000 px） | 压缩后重试 |
| 40004 | 415 | 图片格式不支持 | 转 JPEG/PNG |
| 40005 | 400 | URL 不可达 / 超时 / 内网地址 | 换可公网访问的 URL |
| 40101 | 401 | 签名错误 / AppId 无效 | 检查密钥与签名串拼接顺序 |
| 40102 | 401 | 时间戳过期（±300 s） | 校时 |
| 40301 | 403 | 无该语言对权限 / 配额用尽 | 申请权限 |
| 42901 | 429 | 调用方 QPS 超限 | 退避重试 |
| 42902 | 429 | 服务端队列已满 | 退避重试 / 转异步 |
| 50001 | 500 | OCR 失败 | 重试；持续失败则报障 |
| 50201 | 502 | 翻译通道全部不可用（未降级成功） | 重试；观察 `warnings` |
| 50301 | 500 | 擦除模型失败 | 重试 |
| 50401 | 504 | 处理超时 | 转异步 |
| 50404 | 404 | 任务不存在（异步） | — |

> 注意：`50201` 只在**所有块**都翻译失败且无法回填时返回；部分失败仍返回 `code=0` + `warnings`。

### 6.4 鉴权与限流

**鉴权（HMAC-SHA256，PHP 侧 5 行代码可实现）**：

```
sign = hex( HMAC_SHA256( secret, app_id + timestamp + nonce + hex(SHA256(raw_body)) ) )
```

- `timestamp`：Unix 秒，允许 ±300 s 偏移
- `nonce`：随机串，Redis 记录 10 min 防重放
- 为什么不用 JWT：调用方是 PHP 单体，HMAC 无需引入库，且不依赖时钟同步以外的状态

**限流（三级，全部基于 Redis 令牌桶）**：

| 层级 | 维度 | 建议值 | 超限返回 |
|---|---|---|---|
| 全局 | 服务总 QPS | 按 GPU 容量反推（≈ `卡片数 × 0.7`） | 42902 |
| 应用 | 每个 `app_id` | 默认 5 QPS | 42901 |
| 日配额 | 每个 `app_id` | 按套餐 | 40301 |

### 6.5 与 PHP 业务站的集成

**形态**：Python 服务监听内网 `127.0.0.1:8080`（同机）或独立内网 IP，PHP 通过 `curl` 调用。

```php
// PHP 侧调用示例（与现有 \Otc\Request 风格一致）
$body = json_encode([
    'image_url' => $url,
    'from'      => 'auto',
    'to'        => $to,
    'render'    => ['mode' => 'full', 'output_format' => 'jpeg', 'quality' => 95],
], JSON_UNESCAPED_UNICODE);

$ts    = time();
$nonce = bin2hex(random_bytes(8));
$sign  = hash_hmac('sha256', $appId . $ts . $nonce . hash('sha256', $body), $secret);

$resp = \Otc\Request::execute('http://127.0.0.1:8080/v1/image/translate', [
    'post_data' => $body,
    'header'    => [
        'Content-Type: application/json',
        "X-App-Id: {$appId}",
        "X-Timestamp: {$ts}",
        "X-Nonce: {$nonce}",
        "X-Sign: {$sign}",
    ],
    'timeout'   => 35,          // 略大于服务端 30s
]);
```

**迁移路径（重要，避免 PHP 业务中断）**：

| 阶段 | PHP 侧 | Python 侧 |
|---|---|---|
| 1. 并行期 | 保留现有 `Google::translate()` 调用，新功能走 Python | 上线，灰度 5%~10% 流量 |
| 2. 切换期 | 改为调用 Python `/v1/image/translate` | 全量 |
| 3. 收敛期 | 删除 `Otc\Translate\Google` 中的 web 通道代码，仅保留 `nmtApi`（若有其他业务在用） | 稳定运行 |

**PHP 侧必须保留的能力**：现有 `api()` 若被其他业务（纯文本翻译）复用，**不要直接删**。建议先让 PHP 文本翻译也调 Python 的 `/v1/text/translate`（同一套 `GoogleWebTranslator`，零成本复用），再统一收敛。

---

## 7. 工程与性能

### 7.1 核心数据模型与类关系

```mermaid
classDiagram
    class TextBlock {
        +int id
        +str src
        +str dst
        +list polygon
        +tuple~int,int,int,int~ rect
        +int line_count
        +bool vertical
        +float confidence
        +str mt_status
        +BlockStyle style
        +RenderPlan render
    }
    class BlockStyle {
        +int font_size
        +str color
        +str weight
        +bool italic
        +StrokeSpec stroke
        +str align
        +BGSpec background
    }
    class StrokeSpec {
        +str color
        +int width
    }
    class BGSpec {
        +str type
        +list~str~ colors
    }
    class RenderPlan {
        +int font_size
        +float scale
        +int lines
        +bool overflow
        +bool truncated
        +bool compressed
    }
    class Pipeline {
        +run(req) PipelineResult
        -fetch()
        -normalize()
        -ocr()
        -group()
        -translate()
        -erase()
        -analyze()
        -render()
        -encode()
    }
    class OcrEngine {
        +dict engines
        +warmup()
        +detect_and_recognize(img, lang) RawLine[]
        +route_model(lang) str
    }
    class BaseTranslator {
        <<protocol>>
        +translate_batch(texts, source, target) list~str~
    }
    class GoogleWebTranslator {
        -httpx.AsyncClient http
        -TkkProvider tkk
        -TokenBucket bucket
        -EgressPool egress
        +bool circuit_open
        +translate_batch(texts, source, target) list~str~
        -_translate_api(chunk, src, tgt) list~str~
        -_translate_free(chunk, src, tgt) list~str~
        -_call_with_retry(chunk, src, tgt) list~str~
    }
    class GoogleCloudTranslator {
        +translate_batch(texts, source, target) list~str~
    }
    class TkkProvider {
        -str tkk
        -float at
        -int tkk_ttl
        +get(force) str
    }
    class TokenBucket {
        +float rate
        +int capacity
        +acquire(n)
    }
    class EgressPool {
        +list endpoints
        +pick() str
        +report_failure(ep)
    }
    class MaskGenerator {
        +build(blocks) ndarray
        -estimate_stroke_width()
    }
    class InpaintEngine {
        +erase(img, mask, quality) Image
        -can_use_analytic(roi, mask) bool
    }
    class StyleAnalyzer {
        +analyze(blocks, img, mask) BlockStyle[]
    }
    class Renderer {
        +render(img, blocks, plans) Image
        +fit_text(text, box, style) RenderPlan
    }
    class FontRegistry {
        +get(lang, size, weight) ImageFont
        +resolve_chain(lang) list~str~
    }
    class CacheStore {
        +get(key) PipelineResult
        +set(key, val, ttl)
        +get_text(text, src, tgt) str
    }

    Pipeline --> OcrEngine
    Pipeline --> BaseTranslator
    Pipeline --> MaskGenerator
    Pipeline --> InpaintEngine
    Pipeline --> StyleAnalyzer
    Pipeline --> Renderer
    Pipeline --> CacheStore
    BaseTranslator <|.. GoogleWebTranslator
    BaseTranslator <|.. GoogleCloudTranslator
    GoogleWebTranslator --> TkkProvider
    GoogleWebTranslator --> TokenBucket
    GoogleWebTranslator --> EgressPool
    OcrEngine ..> TextBlock : produces
    StyleAnalyzer ..> TextBlock : fills BlockStyle
    Renderer ..> TextBlock : reads
    TextBlock *-- BlockStyle
    TextBlock *-- RenderPlan
    BlockStyle *-- StrokeSpec
    BlockStyle *-- BGSpec
    Renderer --> FontRegistry
```

### 7.2 服务形态与模型常驻

```python
# app/main.py 关键片段（示意）
@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    app.state.ocr = OcrEngine(cfg.ocr)          # 各语种模型全部加载
    app.state.ocr.warmup()                       # dummy 推理，避免首请求 5~10s
    app.state.inpaint = InpaintEngine(cfg.erase, device="cuda:0")
    app.state.translator = build_translator(cfg.translate)
    await app.state.translator.warmup()          # 预热 TKK + 连接池
    app.state.fence = asyncio.Semaphore(cfg.gpu.parallel)   # 默认 1
    yield
    await app.state.translator.aclose()
```

| 决策 | 取值 | 理由 |
|---|---|---|
| 进程模型 | **uvicorn 单 worker**（`--workers 1`） | GPU 模型必须常驻；多 worker 会各自加载一份模型 → 显存 OOM |
| GPU 并发 | `asyncio.Semaphore(1)` | 8 GB 卡上 LaMa + OCR 同时跑会撞显存上限；串行最稳 |
| 推理模式 | `torch.inference_mode()` / `paddle.no_grad()` | 省显存、提速 10%~20% |
| fp16 | LaMa 用 `torch.float16` | 显存 1.25 GB → 约 0.7 GB，速度 +30%，质量无可见差异 |
| 队列等待 | 最大 20，等待 ≤ 30 s，超时 `42902` | 宁可快速失败，不无限堆积 |
| 多卡（未来） | 每卡一个进程 + 前置 Nginx 反代 / 独立端口 | 进程内多卡调度复杂度高，不值得 |

**CPU 降级路径**（用户要求明确设计）：
- OCR：`paddleocr` 支持 `device="cpu"`，PP-OCRv6 small 在 4 核上约 0.9 s/页
- 擦除：OpenCV Telea（2~8 ms/块）
- 渲染：不变
- 整体：约 **2~4 s/图**，单核串行约 **0.25~0.5 QPS**
- 触发方式：配置 `gpu.enabled=false` 或启动时检测 `torch.cuda.is_available()`

### 7.3 耗时分解与吞吐估算

假设：RTX 4060 Ti 8 GB / 1200×800 实景图 / 12 个文字块 / 每图 1 批翻译。

| 阶段 | P50 | 占比 | P95 | 备注 |
|---|---|---|---|---|
| 下载 | 120 ms | 8% | 800 ms | 强依赖源站，建议源站侧缓存 |
| 解码 + 规范化 | 40 ms | 3% | 80 ms | EXIF/ICC |
| **OCR（det+cls+rec）** | **480 ms** | **30%** | 900 ms | 最大瓶颈 |
| 行/段落聚合 | 5 ms | 0.3% | 10 ms | 纯 CPU |
| **翻译（1 批）** | **310 ms** | **20%** | 1200 ms | 强依赖 Google 与网络 |
| Mask 生成 | 35 ms | 2% | 60 ms | 12 块 |
| **擦除（LaMa，12 块）** | **420 ms** | **26%** | 700 ms | 分块 + batch |
| 样式分析 | 20 ms | 1% | 30 ms | |
| 渲染（超采样） | 90 ms | 6% | 150 ms | |
| 编码 | 60 ms | 4% | 120 ms | JPEG q95 |
| **合计** | **≈ 1.58 s** | 100% | **≈ 3.5~4.5 s** | P95 目标 ≤ 6 s 有充足余量 |

**吞吐结论**：

| 指标 | 单卡（8 GB） | 说明 |
|---|---|---|
| 串行 QPS | **≈ 0.55**（1/1.58s） | 加 P95 长尾后实际 **0.4~0.5** |
| 单日上限 | **≈ 3.5~4.3 万张/天** | 24h 满载，实际按 70% 利用率算 **2.5~3 万张/天** |
| 1 万张/天所需 | **0.12 QPS** → **单卡绰绰有余** | 峰值若为均值 5 倍（0.6 QPS）则刚好卡在单卡上限 |
| 翻译侧能力 | ≈ 2.5 图/秒（QPS 3） | **远大于 GPU 侧，不是瓶颈** |

⚠️ 若业务峰值 > 1.5 倍单卡容量，方案是**加卡加进程**（线性扩展），而非优化单卡。

### 7.4 目录结构

```text
D:/wwwroot/ocr/imgtrans/
├── __init__.py
├── translator/                     # ★ 翻译模块（结构与实现已约定，工程师并行开发中）
│   ├── __init__.py
│   ├── base.py                     # BaseTranslator 协议 + 异常
│   ├── lang_map.py                 # auto_list 映射 / convert_lang / convert_lang_code
│   ├── google_tk.py                # TL/RL/shr32/charCodeAt 的 32 位移植 + TKK 动态刷新
│   ├── google_web.py               # 主通道 api(client=te) + 降级 free(client=at)
│   ├── google_cloud.py             # 对应 nmtApi，Google Cloud Translate V2 可选实现
│   └── cli.py                      # 冒烟/调试命令行
├── ocr/                            # PaddleOCR 封装（待实现）
├── inpaint/                        # LaMa 擦除（待实现）
├── render/                         # 样式分析 + 回填渲染（待实现）
├── pipeline/                       # 端到端编排（待实现）
├── api/                            # FastAPI 路由（待实现）
├── egress/                         # ★ 出口池（独立关注点，不属 translator）
│   ├── pool.py                     # Endpoint 池 + 健康探测 + 故障切换
│   └── transport.py                # proxy / 自定义 transport 构造
└── utils/                          # 图像 IO / 下载 / 日志（待实现）
```

**本结构为团队约定，实现时以此为准**（替代本方案 v1.0 中的 `app/translate/` 写法）。

#### 翻译模块文件职责对照

| 文件 | 职责 | 对应 PHP | 本文章节 |
|---|---|---|---|
| `base.py` | `BaseTranslator` 协议、`TranslateError` 异常族、`translate_batch` 契约 | — | §3.3.1 |
| `lang_map.py` | `AUTO_LIST`（含 `vi` 修正）、`convert_lang()`（修 `strtoupper` bug）、`convert_lang_code()` | `$auto_list` / `convertLang()` / `getConvertCodes()` / `convertLangCode()` | §3.3.3 #22-23、附录 A |
| `google_tk.py` | `_rl()` / `build_tk()` / `TkkProvider`（动态抓取 + 三级兜底） | `RL()` / `TL()` / `TKK()` / `shr32()` / `charCodeAt()` | §3.3.4 |
| `google_web.py` | `_translate_api()`（`client=te`，多 `q` 批量）、`_translate_free()`（`client=at`）、重试/熔断/切批调度 | `api()` / `free()` | §3.3.2、§3.3.5、§3.3.7 |
| `google_cloud.py` | `GoogleCloudTranslator`（`translate` + `model="nmt"`，128/批） | `nmtApi()` | §3.3.9 |
| `cli.py` | 冒烟 / 单块调试 / TKK 探活。**已交付形式**：`python -m imgtrans.translator.cli -t zh-CN "你好"`（无 `smoke` 子命令；支持 stdin 逐行，`--plain` / JSON 两种输出） | — | §7.4 末 |

> `cn()` 通道按 §3.3.2 结论**不实现**，`google_web.py` 内若保留 `mode` 参数，需在 `cn` 时显式抛 `NotImplementedError` 并提示"`.google.cn` 已于 2022 年停止服务"。

#### `GoogleWebTranslator` 构造签名（定稿，以代码为准）

```python
GoogleWebTranslator(
    *,
    timeout: float = 8.0,
    max_chars_per_request: int = 4000,
    max_items_per_request: int = 50,
    qps: float = 4.0,
    proxy: str | None = None,
    local_address: str | None = None,
    tkk_ttl: int = 21600,
    prefer: Literal["api", "free"] = "api",
    transport: Any | None = None,
)
```

| 参数 | 默认 | 落地语义（评审时逐条核对） |
|---|---|---|
| `timeout` | `8.0` | 单请求总超时（秒）。**不是** PHP 的 3 s；出口 RTT 高，3 s 必然误判超时 |
| `max_chars_per_request` | `4000` | Google 硬上限 5000（**按 URL 编码后字节算**），留 20% 余量 |
| `max_items_per_request` | `50` | 单请求 `q` 个数上限，防 URL 过长 |
| `qps` | `4.0` | 令牌桶速率，容量 = `max(1, qps)`；**无 `burst` 参数**（§3.3.6 已定稿） |
| `proxy` | `None` | 出口代理，直接传给 `httpx.AsyncClient(proxy=...)`。**与 `local_address` 互不覆盖** |
| `local_address` | `None` | **非空时抛 `NotImplementedError`**。原因：`httpx` 不支持源 IP 绑定（实测会被静默丢弃），多网卡出口必须由出口层注入自定义 `transport`。这是 P0 修复点——早期实现里 `local_address` 会把 `proxy` 覆盖掉，导致代理配置静默失效 |
| `tkk_ttl` | `21600` | TKK 缓存时长（秒）。**命名唯一，不提供 `refresh_ttl_s` 别名**（两套命名必然漂移） |
| `prefer` | `"api"` | 首选通道 `client=te`；`"free"` 走 `client=at` |
| `transport` | `None` | 测试/出口层注入点。契约：可调用对象，返回 **2 元组 `(status: int, text: str)`**，**无 headers**；内部自行维护 cookiejar（见下方偏差说明） |

**熔断器（P0 必做，非可选项）**：连续 20 次 `403` 或 15 次 `429` → 打开 60 s。语义三条，缺一即视为未实现：

1. **打开期间 `translate_batch()` 立即返回原文**，不排队、不等待、不发请求（保证降级链上层的响应时间可预测）；
2. **`circuit_open` 为只读属性**（`@property`，无 setter），供 pipeline / 健康检查 / 指标读取；
3. **60 s 到期转半开，只放 1 个探测请求**：成功则闭合并清零计数，失败则重新计时 60 s（不做指数增长，避免长尾雪崩）。

> **与 PHP 的有意偏差：免费通道不刷 cookie**。PHP `setFreeCookieFile()`（L616）会把 NID cookie 落盘并定期更换。本实现**不做**这一步，理由是：① `transport` 契约固定为 `(status, text)` 2 元组、不含 headers，刷 cookie 需要读 `Set-Cookie`，破坏契约；② 内部 `httpx` cookiejar 已覆盖同会话内的 cookie 维持，落盘只在多进程共享时才有意义；③ 这类风控字段靠"十行代码"刷不出来，真正的解法是换出口或换通道（`free` 本就是降级档）。**这是有意为之的简化，不是漏实现**，评审时不要当成 bug 提。

#### 三块必须可独立单测的逻辑：**不新增文件，以模块级类暴露**

`google_web.py` 里会自然长出三块彼此独立、必须能脱离网络单测的逻辑。**本期不拆文件**（避免与已落地代码冲突），但要求它们以**模块级类 / 函数**暴露，禁止写成嵌套闭包或依赖实例私有状态——即测试里可以 `from imgtrans.translator.google_web import _TokenBucket` 直接实例化：

| 逻辑块 | 约定暴露形式 | 为什么必须可独立测 |
|---|---|---|
| **切批 + 索引映射** | `BatchPlanner`（或类方法 `_make_batches(pending)`） | 这是"文字会不会错位"的唯一防线，§9.4 T2/T3 必须在无网络条件下断言 `offset + j` 映射 |
| **限流 + 退避** | `_TokenBucket` + `RetryPolicy`（纯计算 `backoff_delay()` 与执行 `sleep` 分离） | §9.4 T5 要断言实际 sleep 序列严格等于 0.5/1.0；计算与睡眠耦合会导致"双重睡眠"这类 bug |
| **出口注入** | `proxy` / `local_address` 两个**构造参数**（注入点） | 出口健康探测与故障切换**属出口层职责**，不在翻译层；翻译层只负责"接受一个已知可用的出口配置" |

> **是否物理拆成 `batching.py` / `ratelimit.py` / `egress.py`**：方向认可，但需在代码评审后由 team-lead 决定，届时本文档再跟进。当前以"模块级可调用"为验收标准，不以文件数为标准。

> **`egress` 归属已定稿**：出口池从翻译层移出，放 `imgtrans/egress/`（独立关注点，未必由翻译模块负责人实现）。翻译层只提供 **`proxy` 注入点**，不感知出口健康度与切换。
> 原因：`httpx` 的 `local_address` 实测存在被静默丢弃的情况（与 transport 实现相关），多网卡出口需注入自定义 transport 或改用 `proxy`——这属于传输层能力，塞进翻译层会让"翻译正确性"与"网络可达性"两个问题纠缠在一起，排查困难。
> 因此 **T8「出口故障切换 ≤10 s」从翻译模块验收移到出口层验收**。

#### 其余模块的规划子结构（P0 之后按需展开）

| 模块 | 建议文件 | 说明 |
|---|---|---|
| `ocr/` | `engine.py`（引擎池 + 预热）、`router.py`（语言→模型路由）、`group.py`（行/段落聚合 + 分栏） | §3.1 |
| `inpaint/` | `selector.py`（四级策略）、`lama.py`、`sd_inpaint.py`、`analytic.py`、`telea.py` | §3.2 |
| `render/` | `mask.py`、`style.py`、`layout.py`、`fit.py`（P1~P6 策略链）、`fonts.py`、`text.py`（超采样） | §4 |
| `pipeline/` | `orchestrator.py` + `stages/`（fetch / normalize / ocr / translate / erase / style / render / encode） | §2.2 |
| `api/` | `main.py`（lifespan）、`deps.py`（HMAC 验签/限流）、`v1/translate.py`、`v1/tasks.py`、`v1/health.py` | §6 |
| `utils/` | `image.py`（EXIF/ICC/编解码）、`http.py`（下载 + SSRF 防护）、`logging.py`、`metrics.py`、`cache.py` | §5、§7.6 |

#### 工程附属目录（与 `imgtrans/` 同级，不入包）

```text
D:/wwwroot/ocr/
├── imgtrans/                       # 业务包（上表）
├── assets/fonts/                   # Noto Sans / Noto Sans CJK / Noto Naskh Arabic…
├── configs/config.yaml             # 对应 §3.3.10
├── tools/
│   └── tk_reference.js             # ★ JS 版 tk 算法（ground truth），stdin 数组 → stdout [{input,tk}]
├── tests/
│   ├── fixtures/
│   │   ├── tk_golden.json          # ★ JS 参考实现生成的 200 条对拍样本（提交进仓库）
│   │   └── http/                   # 真实响应录制（200/403/429/空数组/非 JSON）
│   ├── test_tk_golden.py           # ★ P0 硬门槛：Python build_tk ↔ JS 逐条一致
│   ├── test_lang_map.py            # vi / he→iw / zh-Hant 映射
│   ├── test_batching_order.py      # ★ 137 块极端用例，验证索引不错位
│   ├── test_google_web_mock.py     # respx 模拟 200/403/429/空数组/长度不匹配
│   ├── test_web_transport_inject.py # 注入的 proxy/local_address 被正确传给 transport
│   └── test_pipeline_e2e.py        # 端到端（mock 外部依赖）
├── scripts/
│   ├── gen_tk_golden.py            # ★ 调 node tools/tk_reference.js 生成 tk_golden.json
│   ├── download_models.py
│   └── warmup.py
├── requirements.txt                # 直接依赖 + 版本范围
├── requirements.lock.txt           # ★ 精确 pin（pip freeze 产出），生产镜像用这个
├── requirements-dev.txt            # pytest / respx / ruff / mypy
└── Dockerfile
```

**已废弃的旧路径**（v1.0 方案中的写法，不要再使用）：

| 旧路径 | 新路径 |
|---|---|
| `app/translate/base.py` | `imgtrans/translator/base.py` |
| `app/translate/tk.py` | `imgtrans/translator/google_tk.py` |
| `app/translate/google_web.py` | `imgtrans/translator/google_web.py`（路径不变，父目录变） |
| `app/translate/lang.py` | `imgtrans/translator/lang_map.py` |
| `app/core/config.py` | `imgtrans/utils/`（配置放 `configs/` + `utils/config.py`） |
| `tests/fixtures/php_tk_golden.json` | `tests/fixtures/tk_golden.json`（由 `tools/tk_reference.js` 生成，非 PHP） |
| `scripts/gen_tk_golden.php` | `scripts/gen_tk_golden.py`（Python 驱动 `node`，本机无 PHP） |
| `tests/fixtures/gen_tk_golden.mjs` | `tools/tk_reference.js`（算法基准与生成器分离） |

### 7.5 依赖与版本口径

**唯一版本真源：仓库根目录 `D:/wwwroot/ocr/requirements.lock.txt`。** 本文档不重复罗列全部版本号，避免两处各写一遍必然漂移。

| 文件 | 作用 | 谁用 |
|---|---|---|
| `requirements.txt` | 声明**直接依赖 + 版本范围**（如 `fastapi>=0.165,<0.166`） | 开发者装环境、看"需要什么" |
| **`requirements.lock.txt`** | **精确 pin，由项目 venv 内 `pip freeze` 产出（含传递依赖）** | 生产镜像、CI。`pip install -r requirements.lock.txt` |
| `requirements-dev.txt` | 测试与静态检查工具 | CI |

#### 版本口径

| 项 | 值 |
|---|---|
| 核实方式 | 本机 `python -m pip index versions <pkg>` 逐条实测（非网页检索、非记忆） |
| **核实日期** | **2026-11-19** |
| 核实范围 | 40 项（生产 22 + 开发 6 + 可选 12），全部为当日 PyPI 最新稳定版 |
| 无法实证项 | `paddlepaddle-gpu==3.2.0`、`torch` 的 cu126 轮子版本 —— 二者不在 PyPI，仅在官方索引，已标 ⚠️ 需确认 |

#### 升级策略

| 场景 | 动作 |
|---|---|
| **常规** | 每月一次评估，按 `patch > minor > major` 顺序升；**major 升级必须单独 PR + 回归** |
| **安全修补** | 立即升 patch，走热修流程，不等月度窗口 |
| **禁止** | ❌ 生产环境 `pip install -U` ❌ `requirements.txt` 用 `*` 或 `==x.y.*` 松散约束 |
| **升级后必做** | ① 跑全量单测 ② 跑 200 张验收集 ③ 对比 P95 耗时（±15% 内） |
| **回滚** | lock 文件进版本控制，回滚 = 回滚 lock + 重建镜像 |

#### 关键约束（这几条不能只看版本号，必须看注意事项）

1. **`paddleocr` 硬锁 3.7.0**，不允许 `>=`。3.x 与 2.x API 不兼容：`use_angle_cls` → `use_textline_orientation`、`use_gpu=True` → `device="gpu:0"`，传旧参数直接 `ValueError: Unknown argument`。网上大量 2.x 教程不可照抄。

2. **⚠️ PP-OCRv6 `tiny` 档不含日语假名**——官方实测日语字符错误率约 **47%**，且**不报错、置信度偏高（>0.7）**，属"静默错"。
   → 凡涉及日语（源语言为 `ja`，或 `auto` 检测到日语），**必须 `small` 或 `medium`，禁止 `tiny`**。
   → `OcrEngine.route_model()` 里硬编码兜底：`if lang == "ja" and tier == "tiny": tier = "small"`。

3. **`opencv-python-headless` 已进入 5.x**（核实值 5.1.0.95，从 4.x 跨大版本）。方案重度使用 `distanceTransform` / `morphologyEx` / `inpaint` / 自实现 Sauvola。**上线前必须跑 `tests/test_mask.py`**；若 5.x 有行为变更，回退 4.x 末版（版本号需在 PyPI 现场确认）。

4. **Pillow 必须验证 raqm**：`python -c "from PIL import features; print(features.check('raqm'))"` 必须为 `True`，否则阿拉伯/泰文/天城文整形错乱。官方 wheel 自 8.2 起默认内置 raqm。另注意 Pillow 13 已移除 FreeType 3.x 支持。

5. **CUDA 运行时必须一致**：`paddlepaddle-gpu` 与 `torch` 都用 cu126，要求驱动 ≥ 550.54.14。同一镜像里两者 CUDA 版本不一致会出现"Paddle 能跑、torch 报 CUDA error"（或反之），且报错信息指向无关方向，极难排查。

6. **不依赖 `iopaint`**：`iopaint` 最新 1.6.0（2025-03-18），GitHub 仓库已于 **2025-08-13 归档（read-only）**，不支持 Python 3.13，且不会再有修复。方案改为直接加载 `big-lama.pt` 权重自封装推理。

7. **若出口走 SOCKS5**，需额外装 extra：`pip install "httpx[socks]"`。

### 7.6 可观测性

| 指标 | 类型 | 用途 |
|---|---|---|
| `imgtrans_requests_total{status,lang_pair,mode}` | Counter | 总量与分布 |
| `imgtrans_duration_seconds{stage}` | Histogram | 各阶段耗时（对应 §7.3 表） |
| `imgtrans_queue_wait_seconds` | Histogram | GPU 排队压力 |
| **`translate_tk_refresh_total{result}`** | Counter | TKK 抓取成功率（**最关键的外部依赖健康指标**） |
| **`translate_http_status_total{status}`** | Counter | 403/429 趋势，提前发现被封 |
| `translate_fallback_total{from,to}` | Counter | 降级频率 |
| `translate_chunk_mismatch_total` | Counter | **长度不匹配次数（应为 0，非 0 立即告警）** |
| `translate_quality_gate_reject_total{reason}` | Counter | 译文质量闸门拒绝数 |
| `ocr_blocks_total` / `ocr_confidence_avg` | Histogram | OCR 质量趋势 |
| `erase_residual_ratio` | Histogram | mask 外 3 px 环带的残留差异（自动化的"残影"代理指标） |
| `render_overflow_total` | Counter | 回填越界次数 |

**日志**：`structlog` JSON 行，每请求一条 `request_id` 贯穿全链路；**不记录图片内容与原文全文**（合规），只记块数、字符数、状态。

**错误分类**（用于告警分级）：

```
E_TRANSPORT  网络/超时        → 重试可解，P3
E_TK         签名被拒         → 需刷 TKK，P2
E_RATE       429/503          → 需扩容/换 IP，P1
E_PARSE      响应结构异常     → 代码/上游变更，P1
E_ORDER      长度不匹配       → 错位风险，P0（立即告警）
E_OCR        识别失败         → P2
E_RENDER     渲染异常         → P2
```

**`E_ORDER` 定为 P0**：它是唯一会导致"文字跑到别处"的错误，用户感知极强。

---

## 8. 风险、边界与成本

### 8.1 做不好 / 做不了的场景（必须提前告知用户）

| 场景 | 表现 | 我们的处置 | 能否改善 |
|---|---|---|---|
| **艺术字 / 花体 / 书法** | OCR 识别率低，mask 不准，擦除留痕 | 识别置信度 < 0.5 的块**直接跳过**，不擦不译，JSON 里标记 `skipped` | 难（需专用模型） |
| **曲面 / 透视文字**（圆柱、包装盒） | 检测框是斜四边形；擦除尚可，**回填无法贴合曲面** | 做透视校正后识别，回填时**只做水平渲染** | 部分（需纹理映射，成本高） |
| **竖排文字（中日）** | 需逐字排版 | 目标语言为 CJK 时竖排还原；否则转横排 | 可以 |
| **图标内嵌小字**（< 8 px 字高） | 识别不出 | 跳过 | 不可行 |
| **手写体** | PP-OCRv5 起有提升，但仍不稳 | 置信度阈值更严 | 部分 |
| **半透明水印压在文字上** | 文字与水印不可分离，mask 会同时覆盖 | **不擦**，保留原文字，JSON 标记 | 不可行 |
| **极低分辨率**（短边 < 300 px） | 无法识别 | 拒绝或仅返回 OCR | 不可行 |
| **密集小字**（说明书、参数表） | 行粘连，det 合并成一行 | 调小 det 的 `limit_side_len`，仍可能失败 | 部分 |
| **复杂花纹背景** | LaMa 生成伪纹理 | 升级 PowerPaint V2 + 人工抽检 | 部分 |
| **长截图**（聊天记录，宽 1080 高 12000） | 内存与耗时 | 切片 OCR；比例 > 1:8 建议拒绝 | 可缓解 |
| **图内文字 > 200 块** | 耗时激增、翻译切批多 | 超过阈值改异步 | 可以 |

**产品化建议**：在 API 响应里返回 `quality_hint`（`high`/`medium`/`low`），低质量时提示用户"该图文字样式复杂，建议人工核对"。比默默输出烂图更好。

### 8.2 合规风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| **Google 服务条款** | 免费 web 通道非官方公开 API，自动化调用在 ToS 灰区；Google 可随时限流/封 IP/改签名算法 | ① 明确告知用户这是"技术风险自担"；② 保留付费通道（Google Cloud / 百度）作为合规替代；③ 不对外承诺 SLA |
| **生成内容标识** | 中国《人工智能生成合成内容标识办法》（2025-09-01 生效）要求对 AI 生成/合成内容做标识。图片文字擦除+重绘属于"合成"边界情形 | 建议在响应头/元数据加隐式标识（如 EXIF `Software` 字段、`X-AI-Generated: image-translate`），并在用户协议中告知 |
| **内容安全审核** | 图片 OCR 出的文本会送往 Google（境外） | ① 敏感场景前置本地审核（关键词过滤）；② 提供"仅 OCR 不翻译"模式；③ 用户协议明确数据出境 |
| **数据出境** | 图片文字内容传至境外服务器 | 若客户有等保/数据出境要求，**必须**改用境内通道（百度/自建 MT） |
| **版权** | 用户上传图片的授权、译文的二次使用 | 用户协议约定；不做图片留存（除非用户开启缓存） |
| **隐私** | 图片可能含人脸/证件/手机号 | 不持久化原图（缓存只存结果 + TTL 24h）；日志不记录文本内容 |

⚠️ **这一节建议让法务过一遍**，尤其"数据出境"和"生成内容标识"两条。

### 8.3 成本模型（1 万张/天）

**假设**：每图平均 12 个文字块、约 200 字符（含短标签）。

| 方案 | 翻译成本 | GPU 成本 | 合计/月 | 备注 |
|---|---|---|---|---|
| **本方案（自建 Google web 通道）** | **0 元** | 1,500~3,000 元 | **1,500~3,000 元** | 自有机房仅电费，则 < 500 元 |
| 自建 + Google Cloud 兜底（20% 流量走付费） | 60M×20%×$20 = $240 ≈ 1,720 元 | 1,500~3,000 元 | 3,200~4,700 元 | 兜底比例越低越省 |
| 自建 OCR + 百度通用翻译 | 60M×49/1M ≈ **2,940 元** | 1,500~3,000 元 | 4,400~5,900 元 | 百度 49 元/百万字符（已核实） |
| 直接调**百度图片翻译 API** | 30 万次/月 × 0.038 元 ≈ **11,400 元** | 0 | **11,400 元** | 次数包 1 万次 380 元（已核实） |
| 直接调百度图片翻译（按量） | 30 万次 × 0.04 元 = **12,000 元** | 0 | 12,000 元 | 0.04 元/次（已核实） |
| DeepL 全量 | 60M×$25 = $1,500 ≈ 10,800 元 | 1,500~3,000 元 | 12,300~13,800 元 | 仅欧洲语种质量优势，不适合全量 |

**GPU 成本口径**：
- 云上按需（RTX 4090 / A10 级）：约 2~4 元/小时 → 720 h ≈ 1,440~2,880 元/月
- 包月：约 1,500~2,500 元/月
- 自有卡：仅电费 + 折旧，约 200~500 元/月

**规模拐点**：

| 日处理量 | 推荐方案 | 理由 |
|---|---|---|
| < 500 张 | **直接调百度图片翻译 API** | 自建不划算（GPU 成本 >> API 成本），且省开发 |
| 500 ~ 3,000 张 | 自建可行，但需评估运维投入 | 月省约 2,000~8,000 元 |
| **> 3,000 张** | **自建明显划算** | 1 万张/天省约 8,000~10,000 元/月 |

**隐性成本（容易被忽略）**：

| 项 | 估算 |
|---|---|
| 通道失效的运维响应 | 每月 1~2 次 × 0.5 人天 = **0.5~1 人天/月** |
| 质量抽检 | 0.5 人天/月 |
| 模型/依赖升级 | 0.5 人天/月 |
| **合计人力** | **约 1.5~2 人天/月** |

⚠️ 这笔人力成本可能超过省下的 API 费用（若日均 < 1,000 张）。**这是要用户拍板的核心问题**。

### 8.4 验收指标建议

| 指标 | 定义 | 目标 | 测量方式 |
|---|---|---|---|
| **OCR 召回率** | 人工标注文字块中被检出的比例 | ≥ 95%（清晰印刷体）/ ≥ 85%（实景图） | 200 张标注集 |
| **OCR 字符准确率** | `1 - CER` | ≥ 95%（清晰）/ ≥ 88%（实景） | 同上 |
| **文字块对齐正确率** | 译文落在正确 block 的比例 | **≥ 99.9%**（近乎不允许错） | 自动化：块数一致性 + 抽样人工 |
| **擦除残影率** | 抽检中有可见残影的图占比 | ≤ 8% | 200 张人工抽检 |
| **回填越界率** | 译文超出原框 > 5% 面积的比例 | ≤ 3% | 自动化计算 |
| **人工可读率** | 抽检中"译文清晰可读"的图占比 | ≥ 90% | 200 张人工 |
| **整图清晰度** | 与仅做 JPEG 重编码的基线对比 PSNR | ≥ 42 dB（擦除区外） | 自动化 |
| **端到端 P50 / P95** | 单图耗时 | ≤ 2 s / ≤ 6 s | 线上 metrics |
| **翻译通道成功率** | 单批请求成功率（含重试） | ≥ 97% | 线上 metrics |
| **降级率** | 走 `free` 或回填原文的比例 | ≤ 3% | 线上 metrics |
| **缓存命中率** | 图片级 + 文本级 | ≥ 25% | 线上 metrics |

**上线门槛（P0 必须全绿）**：

1. `test_tk_golden` 200/200 通过（**Python ↔ Node 参考实现**；本机无 PHP 环境，见 §3.3.4）
2. `test_batching_order` 通过（构造 137 块的极端用例，验证每个 block 拿到自己的译文）
3. `test_web_transport_inject` 通过（注入的出口配置被正确传给 transport；**出口故障切换本身由出口/代理层验收**）
4. **真实出海冒烟通过**：在具备海外出口的环境执行 `python -m imgtrans.translator.cli -t en "你好"`，返回非 403、译文非空、且响应不是 HTML（同意页/拦截页）。**CI 全绿不能替代这一项**
5. 200 张标注集的 OCR 召回率达标
6. 端到端 P95 ≤ 6 s
7. 连续 24 h 压测无 OOM、无队列堆积

---

## 9. 实施拆分

### 9.1 阶段路线（P0 → P3）

```mermaid
gantt
    title 实施路线（人天，1 名后端；粗估，不含模型调优）
    dateFormat  X
    axisFormat  %s
    section P0 主链路
    项目骨架与配置           :p0a, 0, 3
    翻译通道移植(api+tk)     :p0b, 3, 5
    OCR 接入与聚合           :p0c, 6, 4
    LaMa 擦除与 mask         :p0d, 9, 4
    基础渲染与编码           :p0e, 12, 3
    P0 联调验收              :p0f, 15, 3
    section P1 样式还原
    样式分析                 :p1a, 18, 4
    膨胀策略与适配字号       :p1b, 22, 4
    超采样与字体回退         :p1c, 26, 3
    降级通道(free/cloud)     :p1d, 29, 3
    section P2 工程化
    异步任务与队列           :p2a, 32, 4
    缓存与限流鉴权           :p2b, 36, 3
    可观测性与告警           :p2c, 39, 3
    容器化部署               :p2d, 42, 3
    section P3 质量调优
    实景集调参与抽检         :p3a, 45, 5
    分级擦除策略上线         :p3b, 50, 4
    竖排/阿拉伯/RTL          :p3c, 54, 5
    文档与交付               :p3d, 59, 3
```

### 9.2 各阶段交付物与验收

> **关于人天数字的限定**：以下 P0~P3 的人天均为**粗估，假设已具备本文档的实现基础；不含模型调优与真实语料验证**。模型选型、擦除质量、语料验证这三项的不确定性远大于编码本身，实际工期请按本文档的"验收标准"倒排，不要把这里的数字当承诺。

#### P0：主链路跑通（约 18 人天）

**范围**：`URL 输入 → OCR → 翻译 → 擦除 → 回填 → 输出 JPEG`，仅支持 `zh/en/ja`，仅 `mode=full`。

| 交付物 | 内容 |
|---|---|
| 工程骨架 | `imgtrans/` 包结构、`configs/config.yaml`、`requirements.txt`、Dockerfile |
| `imgtrans/translator/google_tk.py` | `_rl` / `build_tk` / `TkkProvider` + **200 条 golden test（Node 生成）** |
| `imgtrans/translator/google_web.py` | `_translate_api` / 切批 / 索引映射 / 重试 / 令牌桶 / **熔断器（P0 必做：打开即回原文 + `circuit_open` 只读属性 + 60 s 半开单探测）** |
| `imgtrans/translator/{base,lang_map,cli}.py` | 协议、语言码映射（`vi`/`he→iw` 修正）、冒烟 CLI |
| `imgtrans/egress/pool.py`（独立模块） | 出口池 + 健康探测 + 故障切换（本机不可直连，**P0 阻塞项**；不由 translator 认领，只向 translator 注入 `proxy`） |
| `imgtrans/ocr/` | PP-OCRv6 接入、预热、`from=auto` 基础路由、聚合 |
| `imgtrans/inpaint/lama.py` | big-lama 自封装（fp16、batch） |
| `imgtrans/pipeline/` | 各 stage 串联 |
| 测试 | `test_tk_golden` / `test_lang_map` / `test_batching_order` / `test_web_transport_inject` / `test_google_web_mock` |

**验收标准**：
- 给定 50 张图，人工检查译文位置正确率 100%（位置错一律打回）
- 端到端 P95 ≤ 6 s
- `test_tk_golden` 200/200 全绿（Node 参考实现）
- **真实出海冒烟通过**（`python -m imgtrans.translator.cli -t zh-CN "你好"`，非 403、译文非空、返回非 HTML）
- 翻译通道成功率 ≥ 95%

#### P1：样式还原（约 15 人天）

按**感知 → 决策 → 呈现**三层拆分，层间通过纯数据对象（`BlockStyle` / `RenderPlan`）解耦，任一层可独立单测与替换。

**P1-A 感知层：从原图估计样式**（约 5 人天）

| 交付物 | 内容 | 产出 |
|---|---|---|
| `imgtrans/render/style.py` | `StyleAnalyzer`：前景色（笔画中心采样）、描边色与宽度（环带探测）、背景色/渐变（环带中位数 + 线性拟合）、对齐（左右边界方差）、字号（行高 × 字系统系数）、字重（stroke/size 比值）、行高 | `BlockStyle` |

验收：在 50 张标注样本上，前景色误差 ΔE ≤ 8 的比例 ≥ 90%；描边宽度误差 ≤ 1 px 的比例 ≥ 85%。

**P1-B 决策层：算怎么摆**（约 5 人天）

| 交付物 | 内容 | 产出 |
|---|---|---|
| `imgtrans/render/fit.py` | `FitPlanner`：P1 扩框 → P2 增行 → P3 缩字号 → P4 语义压缩 → P5 截断 → P6 回填原文的策略链；断行（CJK 逐字 / 拉丁按词 + 连字符）；用 `font.getbbox()` 实测宽度做二分搜索（禁止按字符数估算） | `RenderPlan` |

验收：回填越界率 ≤ 3%；策略命中分布可观测（各档占比写入 metrics）。

**P1-C 呈现层：画上去**（约 3 人天）

| 交付物 | 内容 | 产出 |
|---|---|---|
| `imgtrans/render/text.py` | 超采样绘制（`SCALE = clamp(2, 4, round(size/12))`）→ LANCZOS 下采样；描边绘制 | 合成后的图 |
| `imgtrans/render/fonts.py` | `FontRegistry`：按语言路由 Noto 族 + 回退链；`check_raqm()` 启动自检（False 直接 fail-fast） | `ImageFont` |

验收：人工可读率 ≥ 90%（200 张抽检）；"白字黑边"样本集主观评分 ≥ 4/5；arabic/Thai/Devanagari 渲染无整形错误。

**P1 附带项（不属上述三层，随 P1 一并交付）**

| 交付物 | 内容 |
|---|---|
| `imgtrans/translator/google_web.py` | `free` 降级通道（逐块请求，非批量） |
| `imgtrans/translator/google_cloud.py` | `GoogleCloudTranslator` 付费兜底（可选，默认关闭） |

#### P2：并发与工程化（约 13 人天）

| 交付物 | 内容 |
|---|---|
| 异步任务 | `/async` + `/tasks/{id}` + 回调 |
| `imgtrans/utils/cache.py` | 图片级 + **文本级**缓存 |
| `imgtrans/api/deps.py`（HMAC 验签 / nonce） | HMAC 验签 + nonce 防重放 |
| `imgtrans/pipeline/queue.py`（信号量 + 等待队列） | 信号量 + 等待队列 + 超时 |
| 可观测性 | 11 个指标 + 告警规则 + structlog |
| 部署 | Dockerfile + compose + 健康检查 + 优雅退出 |

**验收标准**：
- 24 h 压测（3 万张）无 OOM、无内存泄漏、队列不失控
- 缓存命中率 ≥ 25%
- 所有 P0/P1 告警可触发并通知

#### P3：质量调优（约 17 人天）

| 交付物 | 内容 |
|---|---|
| 实景集调参 | det 阈值、mask 膨胀系数、膨胀策略阈值 |
| 分级擦除 | 第 0 级解析式填充上线（目标覆盖 ≥ 40%） |
| 语种扩展 | 竖排、阿拉伯 RTL、泰文/天城文整形 |
| 文档 | 接口文档、运维手册、故障处置 SOP |

**验收标准**：
- 擦除残影率 ≤ 8%
- 解析式填充覆盖率 ≥ 40%
- 全语种样本集（12 语种 × 10 图）无渲染错乱

### 9.3 关键路径与风险

**关键路径**：`出口打通` → `tk 移植` → `切批索引映射` → `OCR 聚合` → `mask` → `渲染适配`。任一环卡住都会阻塞后续。

**最高风险项**：

| # | 风险 | 概率 | 影响 | 预案 |
|---|---|---|---|---|
| 1 | **海外出口未就绪**（本机实测不通） | 高 | **P0 阻塞，整条翻译链路不可用** | 开工第一件事就申请/配置境外节点；未就绪前用 mock server 并行开发其余模块（§3.3.7 末） |
| 2 | tk 交叉验证失败 | 中 | 全量 403 | 以 Node 参考实现为准逐位修正；仍不一致时临时改用 `google_cloud` 或百度 |
| 3 | Google 改接口 / 轮换 TKK | 中 | 短时不可用 | `free` → `google_cloud` → 百度 → 原文四级降级；`web_enabled` 一键关闭 |
| 4 | 出口节点被封 IP | 中 | 429 激增 | 多节点 + 令牌桶 3 QPS + 熔断 + 30 s 健康探测 |
| 5 | 擦除质量不达预期 | 中 | 观感差 | 分级策略 + `quality_hint` + 人工抽检闭环 |
| 6 | **无 PHP 环境导致行为对齐偏差** | 中 | 隐性差异（如后处理） | 见 §9.4：用 Node 参考实现 + 静态代码评审 + 响应录制回放三重保障 |

### 9.4 验证策略（本机无 PHP / 无外网出口的前提下如何保证正确）

这是本项目**最特殊的工程约束**：参考实现 `google.php` **无法在本机执行**，目标站点**无法在本机访问**。因此验证必须分层设计，明确每一层能证明什么、不能证明什么。

#### 9.4.1 三层验证矩阵

| 层 | 验证对象 | 手段 | 能证明 | **不能证明** |
|---|---|---|---|---|
| **L1 单元** | tk 算法、语言码映射、切批、后处理 | pytest + Node 参考实现 golden + mock 响应 | 算法抄对了、索引不错位、后处理符合预期 | 真实服务端是否接受 |
| **L2 契约** | HTTP 请求形态（URL/body/header）、响应解析、错误分支 | `respx` 拦截并断言请求形态；录制真实响应回放 | 请求拼装与解析逻辑正确 | 线上真实行为 |
| **L3 集成** | 端到端联通性 | 境外节点上跑 `python -m imgtrans.translator.cli -t en "你好"` | 真的能翻译 | 高并发下的稳定性 |

**核心原则：L1/L2 全绿只是"必要条件"，L3 才是"充分条件"。** 不要在 L1 全绿时就宣布"翻译模块完成"。

#### 9.4.2 无 PHP 环境下的等价性保障

| 手段 | 具体做法 | 产出 |
|---|---|---|
| **Node 参考实现** | `tools/tk_reference.js` 内嵌 Google 官方 JS 版 `RL`/`buildTk`（TKK `406398.2087938574`），`scripts/gen_tk_golden.py` 用 `subprocess` 调它生成 200 条样本 | `tests/fixtures/tk_golden.json` |
| **静态代码评审** | 逐行比对 `google.php` 的 `RL()`/`TL()` 与 Node 实现，记录在 `docs/review/php-vs-node-tk.md`：控制流、掩码位置、移位方向、负数修正 | 评审记录（**必须留档**） |
| **响应录制回放** | 在境外节点用 `mitmproxy`/`curl -v` 录 6 种真实响应（正常多块 / 单块 / 403 / 429 / 空数组 / 非 JSON），存 `tests/fixtures/http/*.json` | 回放样本 |
| **契约断言** | 请求形态断言：URL 参数集合、`q` 重复次数与顺序、`Content-Type`、body 编码。**推荐用自写 `FakeTransport` 而非 respx**——前者能直接造"长度少 1 / 非 JSON / HTML 同意页 / 传输层抛异常"这类业务级畸形响应，比在 httpx 层拦截更直接；respx 仅作可选替代 | 请求形态测试 |
| **差异清单** | 明确记录"故意不移植"的行为（`ucfirst(strtolower)`、`getTransTexts`、`cn` 通道），避免后人误以为是漏抄 | §3.3.6 缺陷清单 |

##### 三方对拍：定位为"可选加固"，**不作为门禁**

本机无 PHP 运行时是硬约束（已实测 `php` 命令不存在），因此 **PHP ↔ JS ↔ Python 三方实跑对拍不做**，Node 单向基准即为权威（Google 服务端校验的就是前端 JS 那套算法）。

若将来需要在**装有 PHP 的环境**严格证明三角等价，可执行：

```bash
# 可选加固步骤，本仓库不以它为门禁
php scripts/gen_tk_golden.php > /tmp/php_tk.json      # 在装有 PHP 的机器上一次性生成
diff <(jq -S . /tmp/php_tk.json) <(jq -S . tests/fixtures/tk_golden.json)
```

预期结果：**除 astral 字符（emoji）相关样本外全部一致**；emoji 样本**必然不一致**，因为 PHP 原文该分支不可达、会产出非法 UTF-8（见 §3.3.4 说明 1）。差异需人工确认"是 PHP 的 bug 而非移植的 bug"。

> 该步骤的定位是**可选加固**，写入运维手册而非 CI 门禁。P0 验收只认 `test_tk_golden`（Python ↔ Node）。

#### 9.4.3 必须写进测试的用例（翻译模块 7 项 + 出口层 1 项）

| # | 用例 | 断言 |
|---|---|---|
| T1 | tk 200 条 golden | 逐条等于 Node 输出 |
| T2 | `plan_chunks` 137 块 / 单块超长 / 全空块 | 每块 `offset+idx` 映射正确；空块不进入 q |
| T3 | 响应长度少 1 | 整批判定失败，`chunk_mismatch_total` +1，**不产生错位译文** |
| T4 | 403 一次后成功 | 触发恰 1 次 TKK 刷新，总请求 2 次 |
| T5 | 连续 429 | 退避序列符合 `0.5/1/2s ± jitter`，第 4 次放弃并降级 |
| T6 | `q` 数量与顺序 | 请求 body 中 `q` 出现次数 == 块数，顺序 == 原文顺序 |
| T7 | 后处理 | `&#039;` → `'`；**大小写与原文一致**（回归 `ucfirst` bug） |
| ~~T8~~ | ~~出口故障切换~~ | **移出翻译模块**：出口/代理层验收项（见 §7.4 末）。翻译层只验证「注入的 `proxy`/`local_address` 被正确传给 transport」 |

#### 9.4.4 CI 与准入门禁

```yaml
# .github/workflows/ci.yml（示意）
# 注意：tk_golden.json 已提交进仓库，CI 默认不重新生成（避免 Node 版本差异导致抖动）
- run: pytest tests/ -q --cov=imgtrans --cov-fail-under=80
- run: ruff check imgtrans/ && mypy imgtrans/
# 仅当 google_tk.py 或 tools/tk_reference.js 变更时，手动/定时重新生成并 commit：
#   python scripts/gen_tk_golden.py && git diff --exit-code tests/fixtures/tk_golden.json
```

**准入门禁（PR 合并前）**：L1 + L2 全绿、覆盖率 ≥ 80%、`ruff`/`mypy` 无新增告警。若 PR 动了 `google_tk.py` 或 `tools/tk_reference.js`，必须附 `python scripts/gen_tk_golden.py` 后 `git diff` 为空的证据。
**发布门禁（上线前）**：额外必须完成 L3 真实出海冒烟，并把结果（请求/响应时间戳、返回码、译文）附在发布单。

---

## 10. 待确认与需拍板事项

### 10.1 需用户拍板（5 项）

| # | 问题 | 选项 | 我的建议 |
|---|---|---|---|
| **0** | **海外出口由谁提供**（本机不可直连 Google，这是开工前必须解决的阻塞项） | A. 用户自有境外 VPS / 已有代理<br>B. 新采购境外节点（约 40~80 元/月）<br>C. 买商业代理池（500~3,000 元/月）<br>D. 放弃 web 通道，只用付费官方 API | **A 优先，无则 B**。单节点即可满足 QPS 3；需**至少 1 个备用出口**，否则节点宕机即全链路中断 |
| **1** | **翻译通道可用性的容忍度** | A. 接受"免费通道随时可能失效"，靠降级链兜底，出问题当天修<br>B. 必须有 SLA，愿意为付费通道付 1,700~3,000 元/月买确定性 | **A + 保留 B 作为开关**。日均 < 3,000 张时，B 的性价比低；但要把降级链做扎实（P1 完成） |
| **2** | **是否保留付费降级源** | A. 只保留 Google Cloud（**仍需海外出口**）<br>B. Google Cloud + 百度（**百度是唯一境内可直连**）<br>C. 都不要 | **B**。Google Cloud 解决不了「出口挂了」的问题；百度同时解决出口依赖 + 数据出境合规。接入成本约 1 人天 |
| **3** | **单卡吞吐是否满足峰值** | 单卡 0.4~0.5 QPS ≈ 2.5~3 万张/天（70% 利用率）。若日均 1 万张、峰值 5 倍 → 接近上限 | 若峰值 > 1.5 倍单卡容量，**加第二张卡**（成本 +1,500 元/月，比优化代码划算） |
| **4** | **首期语种范围** | A. 只做中/英/日（拉丁语系顺带支持）<br>B. 加韩/俄<br>C. 全语种含竖排与阿拉伯 RTL | **A 首发，B 随 P1，C 放 P3**。RTL 与竖排的渲染工作量约 5 人天，首期做会拖慢主链路 |

### 10.2 需进一步确认的技术项

| # | 事项 | 当前假设 | 确认方式 |
|---|---|---|---|
| 1 | PP-OCRv6 在 8 GB 消费卡的实测耗时 | 300~600 ms/图 | 部署后实测，据此修正 §7.3 |
| 2 | Google 首页 TKK 的正则能否稳定抓到 | 能，TTL 6 h | 上线后监控 `translate_tk_refresh_total` |
| 3 | `client=te` 的多 `q` 上限 | 50 个/批 | 压测逐步加到 100 看是否截断 |
| 4 | `translate.googleapis.com` 的 HTTP（非 HTTPS）是否仍可用 | 是（PHP 现网在用） | 建议改 HTTPS，先灰度验证 |
| 5 | 现有 PHP 生产项目对 `Otc\Translate\Google` 的其他调用方 | 仅图片/文本翻译 | **需用户确认**，避免误删影响其他业务 |
| 6 | Pillow 构建是否含 raqm | **实测本机为 `False`**（官方 wheel 并不总带 raqm） | 影响印地语/孟加拉语的连写整形；阿拉伯语走 `arabic_reshaper` + `bidi` 不依赖 raqm |
| 7 | 图片源站是否有防盗链/Referer 校验 | 无 | 抽样验证 20 个真实 URL |
| 8 | 是否需要"仅 OCR 不翻译"模式 | 需要（合规场景） | 已按需要设计，成本为零 |
| 9 | **海外出口的实际可达性与 RTT** | 未验证（本机不通） | 拿到出口后立即 `curl -m 8 https://translate.googleapis.com/` 验证，RTT 决定 §3.3.7 的超时预算 |
| 10 | 出口节点是否会被 Google 判为机房流量而额外限流 | 未验证 | 上线首周重点观察 429 比例；过高则考虑住宅代理 |

### 10.3 明确不在本期范围

- 视频/PDF 版面重排
- 人工校对工作台
- 术语库管理后台（接口预留 `glossary_id`，本期传 `null`）
- 多租户计费

---

## 附录 A：语言码三方映射表

**对外支持的目标语言共 35 个**（权威清单见代码 `imgtrans.translator.SUPPORTED_LANGS`，
可直接 `GET /v1/languages` 拉取）：

```
en ar bg bn cs da de el es et fi fr he hi hr hu id it ja ka ko
ms nl no pl pt ro ru sk sl sv th tr uk vi
```

`zh` / `zh-Hant` **只作为源语言**存在（本服务把中文原图译成外语），不在目标白名单内；
传 `target=zh-CN` 会被入口拒绝并返回 `400 E_LANG`，不会跑到 OCR 才失败。

| ISO（对外） | 百度 | Google web | Google Cloud | 备注 |
|---|---|---|---|---|
| `zh` | `zh` | `zh-CN` | `zh-CN` | 简体 |
| `zh-Hant` | `cht` | `zh-TW` | `zh-TW` | 繁体 |
| `en` | `en` | `en` | `en` | |
| `ja` | `jp` | `ja` | `ja` | 百度用 `jp` |
| `ko` | `kor` | `ko` | `ko` | 百度用 `kor` |
| `he` | `he` | **`iw`** | `iw` | Google 历史遗留码 |
| `no` | `no` | `no` | `no` | `nb`/`nn` 均映射到 `no` |
| `vi` | `vie` | `vi` | `vi` | **PHP 原代码误写 `iv`，已修** |
| `pt` | `pt` | `pt` | `pt` | `pt-BR`/`pt-PT` 均归一 |
| `el` | `el` | `el` | `el` | |
| `hr` | `hr` | `hr` | `hr` | 目标白名单新增；变音符 `č ć đ š ž` 由 Arial 覆盖 |
| `bg` | `bg` | `bg` | `bg` | 西里尔字母 |
| `ar` | `ara` | `ar` | `ar` | RTL，需整形 + bidi |
| `hi` | `hi` | `hi` | `hi` | 天城文，连写需 libraqm |
| `bn` | `bn` | `bn` | `bn` | 孟加拉文，连写需 libraqm |
| `ka` | `ka` | `ka` | `ka` | 格鲁吉亚文，字体用 Sylfaen |
| `th` | `th` | `th` | `th` | 泰文 |
| `auto` | `auto` | `auto` | — | Google Cloud 不支持 `auto` 作为 target |

## 附录 B：本文档引用与核实状态

| 结论 | 状态 | 来源 |
|---|---|---|
| PaddleOCR 3.7.0 / PP-OCRv6 分档与 50 语种 | **已核实** | <https://paddleocr.dev/models>、<https://paddleocr.dev/install> |
| PP-OCRv5 多语种 rec 覆盖韩/阿/西里尔/天城/泰 | **已核实** | 同上（各语种 rec 模型表） |
| PaddlePaddle GPU 安装索引 cu126 / 驱动 ≥550 | **已核实** | <https://paddleocr.dev/install> |
| IOPaint 1.6.0 + 仓库 2025-08-13 归档 | **已核实** | PyPI / GitHub 仓库状态 |
| big-lama.pt 权重地址 | **已核实** | `Sanster/models` release |
| LaMa / PowerPaint V2 耗时与显存 | 第三方实测，量级参考 | IOPaint `benchmark.py` 口径汇总 |
| 百度图片翻译 API 参数/响应/错误码 | **已核实** | <https://fanyi-api.baidu.com/product/233> |
| 百度图片翻译价格 0.04 元/次、1 万次 380 元、QPS 10 | **已核实** | <https://fanyi-api.baidu.com/product/222>、<https://ai.baidu.com/ai-doc/MT/dkifdqg54> |
| 百度通用翻译 49 元/百万字符、QPS 1/10/100、6000 字符 | **已核实** | <https://fanyi-api.baidu.com/product/112> |
| Google Cloud Translation $20/百万字符 | **已核实**（第三方汇总） | 多家 2026 年价格对比文 |
| 全部 Python 依赖的精确 pin（22 项生产依赖） | **实证** | 本机 `pip index versions <pkg>` 逐条实测，2026-11-19；版本真源见 `requirements.lock.txt`，口径见 §7.5 |
| **部署机无法直连 `translate.googleapis.com` / `translate.google.com`**（curl 返回 000，8 s 超时） | **实测确认（2026-11-19）** | 部署机 `curl -m 8` 实测；结论：必须经海外出口 |
| **部署机无 PHP 运行环境**，`google.php` 无法实跑 | **实测确认（2026-11-19）** | 结论：tk 改为 Node 参考实现交叉验证（§3.3.4、§9.4） |
| PP-OCRv6 在消费级卡的实际耗时 | **需确认** | 官方仅公布 A100 数据 |
| PP-OCRv6 `tiny` 档不含日文假名（日语 CER ≈47%，且置信度偏高不报错） | **已核实**（第三方实测引用） | paddleocr.dev/models；结论见 §3.1.3、§7.5 注意 2 |
| `client=te` 单请求 `q` 数量上限 | **需确认** | 需压测 |
| Google 首页 TKK 正则的长期有效性 | **需确认** | 需线上监控 |
| `paddlepaddle-gpu` 3.2.0 / torch cu126 的版本对应 | **需确认** | 不在 PyPI，需按官方索引实际内容确认 |
| 海外出口的实际 RTT 与可达性 | **需确认** | 拿到出口后 `curl -m 8` 验证 |
| 出口节点是否被 Google 判定为机房流量而额外限流 | **需确认** | 上线首周观察 429 比例 |

---

**文档结束。**

