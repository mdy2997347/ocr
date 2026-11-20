# imgtrans

把图片里的文字替换成目标语言，并**沿用原图的版式**：字号、位置、对齐、颜色、描边、倾斜、竖排都从原图反推，而不是套模板重新排版。

面向"文字嵌在图里"的场景——电商详情页、参数表、海报、banner。输出是替换过文字的图片，同时返回每块的原文与译文，可直接落库做双语对照。

<p align="center">
  <img src="tests/fixtures/demo_zh.png" width="320" alt="原文">
  <img src="tests/fixtures/demo_en.png" width="320" alt="英文译文">
</p>

标题的字号与位置、黄色强调色、红色按钮内的白字都沿用原图；背景渐变与纹理无缝衔接，没有补丁边界。这两张图就是下面第一条命令的输入与输出。

## 流水线

七个阶段，每个阶段最关键的那处取舍见下表：

```
OCR → 段落聚合 → 翻译 → 遮罩 → 擦除 → 样式分析 → 回填
```

| 阶段 | 做什么 | 关键取舍 |
|---|---|---|
| OCR | PaddleOCR 检测 + 识别 | 输出四点框（OBB），保留倾斜角与竖排方向 |
| 段落聚合 | 同一栏、同方向的相邻块合并成段 | 翻译整段才有上下文；单字翻译质量明显更差 |
| 翻译 | Google 通道，35 个目标语言 | 免费 `web` 通道，需能访问 Google |
| 遮罩 | 每块单独估背景色、前景色、边框 | 背景色选错会连带 mask、擦除、字色全错 |
| 擦除 | 纯色填充 → 平面拟合 → `cv2.inpaint` 三级降级 | 优先用能算出来的，算不出来才让 OpenCV 猜 |
| 样式分析 | 字号、颜色、描边、粗细、对齐 | 拿不准时返回保守值：宁可少还原，不要还原错 |
| 回填 | 4× 超采样绘制，LANCZOS 降采样，alpha 合成 | 直接按 1× 画，小字会糊成一团 |

## 快速开始

环境要求：Python 3.11 ~ 3.13（由 uv 托管，本机不需要预装）、能访问 Google 的网络出口。

```bash
# 1. 装 uv（已装则跳过）
curl -LsSf https://astral.sh/uv/install.sh | sh        # Linux / macOS
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows

# 2. 建独立环境：CPU 或 GPU 二选一，必须显式指定
uv sync --extra cpu
# uv sync --extra gpu      # 需要 NVIDIA 驱动 >= 570（cu129 源，含 Blackwell sm_120）

# 3. 配置（Windows 上把 cp 换成 copy）
cp .env.example .env
# 编辑 .env，至少填 IMGTRANS_API_KEY（不填则服务拒绝启动）

# 4. 自检：依赖、raqm、字体、推理设备、鉴权与代理配置逐项过一遍
uv run python scripts/doctor.py

# 5. 启动
uv run python scripts/serve.py --port 8000
```

首次启动会下载 OCR 模型到 `.models/`（项目内目录，删项目即清干净），并预热模型——之后第一个请求才不会撞上 6~8 秒的加载。

> **为什么用 uv**：这条链路的依赖有两处硬约束——`paddlex` 要求 `numpy<2.4`，`paddlepaddle` 只发 `cp39`~`cp313` 的 wheel。装错时报错信息指向的往往是别的地方，很难定位。`uv.lock` 把解析结果锁死，`uv sync --frozen` 能在另一台机器上装出完全相同的环境。仓库里的 `requirements*.txt` 是历史遗留的 pip 路径，不再是真源。

## 配置

全部通过环境变量或 `.env`，优先级：**命令行 > 环境变量 > `.env`**。

| 变量 | 默认 | 说明 |
|---|---|---|
| `IMGTRANS_API_KEY` | 无 | 调用方需带 `X-Api-Key` 或 `Authorization: Bearer` |
| `IMGTRANS_PROXY` | 空 | 访问 Google / 下载图片用的代理，服务器能直连则留空 |
| `IMGTRANS_HOST` / `IMGTRANS_PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `IMGTRANS_DEVICE` | `cpu` | `cpu` / `gpu` / `gpu:0` / `auto`（有卡用卡，无卡回落） |
| `IMGTRANS_TRANSLATOR` | `web` | `web`（免费通道，机房 IP 易被 429）/ `cloud`（官方 v2，需 Key）/ `baidu`（百度通用文本翻译） |
| `IMGTRANS_GOOGLE_KEY` / `_CREDENTIALS` / `_PROJECT` | 空 | `cloud` 通道凭证 |
| `IMGTRANS_BAIDU_APPID` / `_SECRET` | 空 | `baidu` 通道凭证。APP ID 是**纯数字**，密钥是另一串，混填报 `54001` |
| `IMGTRANS_BAIDU_QPS` | `1` | 百度标准版只有 1 QPS，高级版可设 10 |
| `IMGTRANS_BAIDU_PROXY` | 空 | 百度通道专用代理。**不继承** `IMGTRANS_PROXY`（那是给 Google 出海用的） |
| `IMGTRANS_OCR_PROFILE` | `quality` | `quality` / `fast`（只换检测模型，CPU 上快一倍，见下） |
| `IMGTRANS_OCR_DET_SIDE_LEN` | 空 | 检测长边上限，默认 736；调小约再快 15%，密集小字会漏 |
| `IMGTRANS_FONT_DIRS` | 空 | 额外字体目录，多个用 `;` 或 `:` 分隔 |
| `IMGTRANS_MODEL_DIR` | `.models` | 模型缓存位置，相对路径以项目根为基准 |
| `PADDLE_PDX_MODEL_SOURCE` | `bos`（安装脚本写入） | 模型下载源：`bos` / `huggingface` / `modelscope` / `aistudio` |

`fast` 档的检测模型是**额外下载**的。国内/香港机房从 HuggingFace 拉几 MB 的
`inference.pdiparams` 常超时，此时程序会**静默回退 `quality`**、接口照常 200，
只是永远不提速。所以安装脚本默认写入 `PADDLE_PDX_MODEL_SOURCE=bos`；排查时
看 `/healthz` 有没有 `ocr_fallback: true`，或日志里 `Creating model` 的模型名。

`.env` 里有真实密钥，已被 `.gitignore` 排除。仓库里只有 `.env.example`。

### 百度翻译通道

机房直连 Google 常吃 429（表现为接口 200 但图片还是中文），切 `baidu` 可以绕开：

```bash
IMGTRANS_TRANSLATOR=baidu
IMGTRANS_BAIDU_APPID=2023xxxxxxxxxxxxx   # 纯数字，不是密钥
IMGTRANS_BAIDU_SECRET=xxxxxxxxxxxxxxxxx
```

APP ID 与密钥都在「百度翻译开放平台 → 管理控制台 → 我的服务」里，同一行并列显示。
链接：<https://fanyi-api.baidu.com/manage/developer>

百度按请求里 `\n` 分行返回，每行带 `src` 原文，所以**一次请求就能翻完一张图的所有文本块**，
比免费 web 通道稳（不会被合并/拆句），往返次数从几十次降到一两次。
标准版限 1 QPS、单次 `q` 不超过 6000 字节，超了会拿到 `54003` / `54005`。

排错看两个地方：`/healthz` 的 `translator_last_error`（带错误码与中文解释），
以及日志里的 `circuit_open`。凭证类错误（`54001` 签名错、`52003` 未授权、
`54004` 余额不足、`58000` IP 白名单）连续 3 次会熔断 5 分钟——
改完配置要重启服务才会解除。

**`54001` 有两种含义**（实测确认）：签名错（APP ID 与密钥不配对），
或者该 APP ID 没开通通用文本翻译 / 没开通这个语言方向——百度在这两种情况下
回的是同一个码。先用 `apple` 试 `en→zh`（最容易通过的方向）：

```bash
IMGTRANS_BAIDU_SECRET=xxx python -m imgtrans.translator.cli \
  --kind baidu --appid 2023xxxxxxxxx --target zh --source en "apple"
```

返回译文说明凭证没问题，问题在语言方向；仍回 `54001` 基本就是密钥抄错了
（去 <https://fanyi-api.baidu.com/manage/developer> 重新复制整串）。

## 命令行

不起服务也能直接翻译，适合批处理与排查：

```bash
# 本地文件
uv run python scripts/translate_image.py --input tests/fixtures/demo_zh.png --to en --out out/demo_en.png

# 远程图片
uv run python scripts/translate_image.py --url https://example.com/a.jpg --to ja --out out/a_ja.png

# 只看识别与翻译结果，不出图
uv run python scripts/translate_image.py --input a.jpg --to en --dry-run
```

`--to` 取值见 `imgtrans/translator/lang_map.py` 的 `SUPPORTED_LANGS`（35 个，不含中文——中文是源语言）。

**语言码统一按 Google 风格传**（`ja` / `ko` / `fr` / `es` / `he`）。百度通道用的是另一套写法
（`jp` / `kor` / `fra` / `spa` / `heb`），服务内部会自己映射；如果你手边已有百度写法，
直接传也能用，两种写法完全等价，返回与日志里只会出现 Google 风格。

## HTTP API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/healthz` | 探活，**免鉴权** |
| GET | `/v1/languages` | 返回支持的目标语言码 |
| POST | `/v1/translate/img` | **推荐接入**：JSON 入、JSON 出 |
| POST | `/v1/translate` | 老接口：URL 入、图片流出 |
| POST | `/v1/translate/upload` | 老接口：multipart 入、图片流出 |

```bash
KEY=$(grep '^IMGTRANS_API_KEY=' .env | cut -d= -f2)

curl -sS http://127.0.0.1:8000/v1/translate/img \
  -H "X-Api-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"lang":"en","img":"https://example.com/a.jpg"}' \
  | jq -r .image_base64 | base64 -d > out_en.png
```

`img` 字段既可以是 `http(s)://` 地址，也可以是 base64（可带 `data:image/png;base64,` 前缀）。上限 20 MB。

响应固定为 JSON，除 `image_base64` 外还带 `blocks`（每块原文/译文/是否回填/字号/行数）与 `timings`（各阶段耗时），排查问题时不用再猜。完整字段与错误码见 [docs/api-usage.md](docs/api-usage.md)。

PHP 接入见 [examples/php/README.md](examples/php/README.md)。

## 目录

```
imgtrans/
  api/           FastAPI 应用：路由、鉴权、错误码
  ocr/           PaddleOCR 封装、段落聚合
  translator/    Google web / cloud 通道、tk 签名、语言码表
  inpaint/       mask（背景/字色/边框估计）、erase（三级擦除）
  render/        样式分析、字体选择、超采样绘制
  pipeline.py    串起全流程，产出报告
scripts/
  serve.py           启动服务
  translate_image.py 单图翻译（命令行）
  doctor.py          部署自检
  verify_live.py     翻译通道在线探针
  render_lab.py      回填沙盘：合成图集，秒级出图
  render_eval.py     回填几何评估：把"像不像"量化
tests/         300+ 个用例，按模块分文件
deploy/        systemd unit、install.sh、nginx conf
docs/          技术设计、部署、API、Go 重写方案
```

## 开发与验证

```bash
# 全部用例
uv run python -m unittest discover -s tests -t . -q

# 回填沙盘：18 个合成场景（渐变、照片、描边、倾斜、竖排、密集表格…）
uv run python scripts/render_lab.py            # 出图到 out/lab/
uv run python scripts/render_eval.py           # 打印几何指标，有偏差会标出来

# 翻译通道是否真的可用（单测只证明逻辑对，不证明 Google 接受签名）
uv run python scripts/verify_live.py
```

回填这块为什么单独做沙盘：完整链路一次要 60 秒以上，且每次输入都不同，没法稳定复现某一种排版失败。沙盘直接构造 `TextBlock`，只跑"擦除 → 样式 → 回填"，秒级出图、场景可控，`render_eval.py` 再把倾斜角、字号比、中心偏移量成数字——人眼看图容易漏，也难做回归。

## 部署

Linux 一键部署（Ubuntu 22.04 / 24.04，需 root）：

```bash
sudo bash deploy/install.sh --dry-run    # 先看会做什么
sudo bash deploy/install.sh              # 默认 /opt/imgtrans，端口 8000，CPU
sudo bash deploy/install.sh --gpu --user ocr
```

`--gpu` 会在下载任何东西之前先查驱动版本与磁盘空间（cu129 要求驱动 ≥ 570，磁盘 ≥ 10GB）。**别把源改回 cu126**：cu126 的 wheel 只编译到 sm_90，在 RTX 50 系列（Blackwell，sm_120）上能装、`is_compiled_with_cuda()` 返回 True，但一推理就 `no kernel image is available for execution on the device`。

脚本里每一条都对应一个真实踩过的坑：必须装 `libgomp1`（否则 `import paddle` 直接失败）、必须装三个字体包（缺了**不报错**，只是把译文画成一排方块并返回 200）、不能用 Alpine（paddle 只发 glibc wheel）、不能用系统 `python3`（22.04 自带 3.10，低于 `requires-python`）。

手工部署与排障见 [docs/ubuntu-deploy.md](docs/ubuntu-deploy.md) 和 [docs/linux-deploy.md](docs/linux-deploy.md)。

## 已知限制

这些是当前实现的真实边界，接入前值得知道：

- **翻译依赖 Google**。免费 `web` 通道的签名依赖 Google 的 TKK 值，Google 轮换后所有请求会 403。`scripts/verify_live.py` 是唯一能提前发现的手段，建议 cron 每 10 分钟跑一次。
- **相邻的"标签：值"行会被并成一段**。段落聚合按栏宽与行距判断是否同段，同一列的连续短行很容易被判成一段，译文因此变成一句连续长句（首图里"活动时间 / 新用户 / 客服电话"三行就是这样），原有的逐行层次会被抹平。
- **照片背景上的大字**擦除仍走 TELEA 扩散，会留下可见的涂抹感。要根治得做 exemplar / PatchMatch 纹理合成，属于另一个量级的改动。
- **竖排标点不旋转**。日文的 `。，` 应落在字符格右上，目前是正着画的。
- **同排相邻块可能字号不一致**。空间紧张时各自缩字号，`crowded_row` 场景实测会出现 19 与 26 并存。统一字号要牺牲"尽量填满"，暂按前者。
- **中文不能作为目标语言**，只能作为源语言。
- **粗描边**（超过 2px）的颜色与宽度估计不准，`_detect_stroke` 目前假设 1~2px。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/image-translate-technical-design.md](docs/image-translate-technical-design.md) | 技术设计：每个阶段的算法与取舍 |
| [docs/api-usage.md](docs/api-usage.md) | 接口字段、错误码、集成注意事项 |
| [docs/implementation-notes.md](docs/implementation-notes.md) | 实现细节与踩过的坑 |
| [docs/ubuntu-deploy.md](docs/ubuntu-deploy.md) | 线上 Ubuntu 部署 |
| [docs/uv-isolated-deploy.md](docs/uv-isolated-deploy.md) | uv 隔离部署方案 |
| [docs/linux-deploy.md](docs/linux-deploy.md) | Linux 通用部署与排障 |
| [docs/go-rewrite-plan.md](docs/go-rewrite-plan.md) | Go 重写方案（评估稿，未实施） |
