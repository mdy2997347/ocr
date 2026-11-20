# Linux 部署检查与指南

> 结论：**可以部署**。代码层没有 Windows 专有依赖，依赖也全部有 Linux wheel。
> 但有一个**致命前提**：目标机必须装中文字体，否则整张图的译文全是豆腐块，
> 而且**不会报任何错**。
>
> 检查日期：2026-11-19；依据：代码审查 + 逐项实测 + PyPI wheel 核验 + 模拟 Linux 字体树。

## 一、检查结果总览

| 检查项 | 结果 | 说明 |
|---|---|---|
| 路径处理 | ✅ | 全部 `pathlib` / `os.path`，无硬编码 `\` 或盘符 |
| 文本 IO 编码 | ✅ | 读配置用 `utf-8-sig`，其余全是二进制；不依赖系统 locale |
| 导入大小写 | ✅ | 脚本校验全部 61 条包内导入，与文件名精确一致（Linux 区分大小写） |
| 平台专有 API | ✅ | 无 `winreg` / `msvcrt` / `fcntl` / `os.startfile` |
| `.env` 解析 | ✅ | 按行 `strip`，CRLF/BOM 都能吃；Linux 上 LF 更没问题 |
| 依赖 wheel | ✅ | 22 个 pin 全部在 PyPI 存在，关键二进制包均有 `manylinux` wheel（含 cp313） |
| **字体发现** | ❌→✅ | 原本只扫一层目录，**在 Linux 上索引为 0**（已修为递归） |
| **`.ttc` 字面选择** | ❌→✅ | `NotoSansCJK` face 0 是日文，中文会画成日式字形（已修） |
| Pillow 复杂文字 | ⚠️ | 需要 raqm；pip 版自带，系统包版本通常没有 |
| 推理设备 | ✅ | `IMGTRANS_DEVICE` 跨平台，与 GPU 型号无关 |

## 二、两个已修复的 Linux 专有故障

### 1. 字体索引为 0：整张图退化成位图字体（致命）

Windows 的字体目录是扁平的：

```
C:\Windows\Fonts\msyh.ttc
```

Linux 是**两层嵌套**的：

```
/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf
/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
```

原实现 `directory.iterdir()` 只扫一层，在 Linux 上拿到的是 `truetype/`、`opentype/`
这些**目录**，全被 `is_file()` 过滤掉。实测模拟：

```
多层布局 → 索引 0，find('zh') → None     ← 移植前的行为
扁平布局 → 索引 4，find('zh') → 命中
```

后果不是报错，而是 `ImageFont.load_default()`——Pillow 的 10px 位图字体，
无法缩放，整张图所有译文都糊掉。**这是从 Windows 搬到 Linux 最可能踩、也最难
第一时间发现的一个坑**。

修复：

* `_build_index` 改为递归扫描（`os.walk`），深度上限 4 层，不跟随符号链接（防环）；
* 目录与文件名都排序，保证同一图在两台机器上选到同一个字体；
* 系统目录按 `sys.platform` 分支：Linux 用 `/usr/share/fonts`、`/usr/local/share/fonts`、
  `~/.local/share/fonts`（freedesktop 标准）、`~/.fonts`；
* 新增 `IMGTRANS_FONT_DIRS` 环境变量指定额外目录（容器里字体常放 `/opt/fonts`）。

### 2. `.ttc` face 0 是日文：中文画出日式字形

`fonts-noto-cjk` 提供的 `NotoSansCJK-Regular.ttc` 把 5 个字面打进一个文件，
**顺序是 JP / KR / SC / TC / HK，face 0 是日文**。中文目标取 face 0，字形会变成
日式的「直」「骨」「关」——不是错字，但中文用户一眼觉得"哪里不对"。

修复：`_face_index()` 按族名 token（`sc`/`gb`/`hans` / `jp` / `kr`）探测 face 序号。
探测失败、非 ttc、非 CJK 族一律返回 0（旧行为），所以**最坏等于不生效**，
不会把能跑的机器搞坏。Windows 的 `msyh.ttc` 只有简体字面，行为完全不变。

### 3. 顺带修的：回退日志分级

以前所有回退都是 `warning`。现在区分：

* 日/韩回退到 CJK 字体 → `debug`（CJK 含假名/谚文，正常）；
* 中/泰/天城文/孟加拉回退到 latin 字体 → **`error`**（必然 □）；
* 其余 → `warning`。

不加这条，Linux 上少装字体的问题会被当成"字体不好看"忽略掉。

## 三、安装步骤

### 1. 系统依赖

```bash
# Debian 12 / Ubuntu 22.04+
apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    fonts-noto-cjk \
    fonts-noto-core \
    fonts-dejavu-core \
    fonts-liberation
```

各包的用途：

| 包 | 提供 | 缺了会怎样 |
|---|---|---|
| `libgomp1` | OpenMP 运行时 | `import paddle` 直接报 `libgomp.so.1: cannot open shared object file` |
| `fonts-noto-cjk` | `NotoSansCJK-Regular.ttc` | 中日韩全部豆腐块 |
| `fonts-noto-core` | Arabic / Thai / Devanagari / Bengali / Georgian | 对应语言豆腐块 |
| `fonts-dejavu-core` | `DejaVuSans.ttf` | 拉丁兜底缺失，西里尔/希腊也可能缺 |
| `fonts-liberation` | `LiberationSans-Regular.ttf` | 可选，`=Arial` 度量兼容 |

RHEL / Rocky：

```bash
dnf install -y libgomp google-noto-sans-cjk-fonts google-noto-sans-arabic-fonts \
               google-noto-sans-thai-fonts google-noto-sans-devanagari-fonts \
               google-noto-sans-bengali-fonts google-noto-sans-georgian-fonts \
               dejavu-sans-fonts liberation-sans-fonts
```

> **不要用 Alpine**。PaddlePaddle 只发布 `manylinux`（glibc）wheel，musl 上装不上也跑不起来。
> 用 `python:3.13-slim`（Debian bookworm）这类 glibc 基础镜像。

### 2. Python 环境

**推荐用 uv**（依赖锁、模型目录、解释器都能收进项目目录，见 `docs/uv-isolated-deploy.md`）：

```bash
cd /opt/imgtrans
uv sync --frozen --extra cpu        # GPU 机器用 --extra gpu
uv run --no-sync python scripts/serve.py
```

下面是等价的 pip 路径（客户环境已装 pip / 不想引入 uv 时用）：

```bash
python3.13 -m venv /opt/imgtrans/.venv
/opt/imgtrans/.venv/bin/pip install -U pip
/opt/imgtrans/.venv/bin/pip install -r requirements.lock.txt
```

⚠️ `requirements.lock.txt` 只锁了 22 个**直接依赖**，不含传递依赖，严格来说
不可复现。要精确复现请用 `uv sync --frozen`（`uv.lock` 锁了 98 个包 + hash）。

**镜像瘦身**：`torch` / `torchvision` 当前**代码里 0 处引用**（擦除走的是
OpenCV + PIL，不是 LaMa），Linux 上可以跳过，省 2 GB+：

```bash
grep -v -E "^(torch|torchvision)==" requirements.lock.txt > /tmp/req.txt
pip install -r /tmp/req.txt
```

注意 `shapely` / `pyclipper` / `scipy` 虽然也没被直接 import，但它们是
**paddleocr 的传递依赖**，不能删。

### 3. GPU（可选）

```bash
# CUDA 12.6 对应索引，驱动需 >= 550.54.14
pip install paddlepaddle-gpu==3.3.1 \
    -i https://www.paddlepaddle.org.cn/packages/stable/cu126/

python -c "import paddle; print(paddle.device.is_compiled_with_cuda())"   # 必须是 True
```

然后 `.env` 里 `IMGTRANS_DEVICE=gpu`（或多卡用 `gpu:1`，不确定就用 `auto`）。
装错版本时服务会**启动即退出**并说明原因，不会悄悄跑回 CPU。

### 4. 自检（强烈建议先跑这一步）

```bash
python scripts/doctor.py
```

输出示例（本机 Windows，CPU）：

```
[ OK ] 运行环境: Python 3.13.14 / Windows AMD64
[WARN] Pillow raqm: 未启用：阿拉伯语/印地语/孟加拉语/泰文会出现字母孤立…
[ OK ] 字体索引: 358 个文件（系统目录 2 个可用，额外目录 0 个）
[ OK ] 字体 zh: msyh.ttc（cjk 专用）
[ OK ] 字体 ja: YuGothR.ttc（ja 专用）
[ OK ] 推理设备: cpu（配置值 cpu）
[WARN] 监听地址: 127.0.0.1：容器内或需要外部访问时必须改成 0.0.0.0
```

它逐项验证：依赖版本、raqm、**每种文字实际命中哪个字体文件**、推理设备、
配置项、目录可写。有 `FAIL` 时退出码为 1，可以直接塞进 CI 或容器启动脚本。
`--json` 输出机器可读格式。

字体检查是逐语言做的，输出会直接告诉你"中文回退到了 DejaVuSans → 会画成豆腐块"，
不用去猜。

### 5. 配置 `.env`

```ini
IMGTRANS_API_KEY=<40 位随机串>
IMGTRANS_PROXY=                      # Linux 服务器直连出海时留空；不要照抄 Windows 的 127.0.0.1:10809
IMGTRANS_HOST=0.0.0.0                # 容器 / 需要外部访问；仅本机反向代理时可用 127.0.0.1
IMGTRANS_PORT=8000
IMGTRANS_DEVICE=cpu                  # 或 gpu / gpu:0 / auto
# IMGTRANS_FONT_DIRS=/opt/fonts      # 字体不在系统目录时用
```

### 6. 启动

```bash
python scripts/serve.py
curl -s http://127.0.0.1:8000/healthz     # {"ok":true,"device":"cpu"}
```

## 四、Dockerfile

```dockerfile
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    IMGTRANS_HOST=0.0.0.0 \
    IMGTRANS_DEVICE=cpu

# 字体是硬需求：缺了不报错，只会把译文画成豆腐块
# libgomp1 是 paddle 的 OpenMP 运行时，slim 镜像默认没有
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        fonts-noto-cjk \
        fonts-noto-core \
        fonts-dejavu-core \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock.txt .
RUN grep -v -E "^(torch|torchvision)==" requirements.lock.txt > req.txt \
    && pip install -r req.txt && rm req.txt

COPY imgtrans ./imgtrans
COPY scripts ./scripts

# 把模型烘进镜像：否则第一次请求要下载几百 MB，直接把 P95 顶穿
RUN python -c "from imgtrans.ocr.engine import OcrEngine; OcrEngine(device='cpu').get('ch')"

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3).status==200 else 1)"

CMD ["python", "scripts/serve.py"]
```

`.env` **不要 COPY 进镜像**（里面有密钥），运行时注入：

```bash
docker run -d --name imgtrans -p 8000:8000 --env-file .env imgtrans:latest
```

GPU 版把 `IMGTRANS_DEVICE` 改成 `gpu`，并用 `--gpus all` 启动，基础镜像换成
`nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04` 后自行装 Python。

## 五、systemd

```ini
[Unit]
Description=imgtrans 图片翻译服务
After=network-online.target

[Service]
Type=simple
User=imgtrans
WorkingDirectory=/opt/imgtrans
EnvironmentFile=/opt/imgtrans/.env
ExecStart=/opt/imgtrans/.venv/bin/python scripts/serve.py
Restart=always
RestartSec=3
# uvicorn 收到 SIGTERM 会等在途请求结束，30s 足够
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

## 六、故障对照表

| 症状 | 原因 | 处理 |
|---|---|---|
| 译文全是小方块 / 字很粗糙 | 字体索引 0，退化成 Pillow 位图字体 | 装 `fonts-noto-cjk`；`doctor.py` 会直接指出来 |
| 中文出现日式字形（直/骨/关） | ttc face 0 是日文 | 已修（自动按族选 face）；确认用的是 pip 版 Pillow |
| 阿拉伯/印地语字母断开、辅音簇错位 | Pillow 无 raqm | 用 pip 装的 Pillow（自带 raqm），别用 `apt install python3-pil` |
| `ImportError: libgomp.so.1` | 缺 OpenMP 运行时 | `apt install libgomp1` |
| 启动报 `E_DEVICE` | 配了 gpu 但 paddle 是 CPU 版 | 装 `paddlepaddle-gpu`，或改 `IMGTRANS_DEVICE=cpu` |
| 服务起来但外部访问不了 | `IMGTRANS_HOST=127.0.0.1` | 改 `0.0.0.0`（容器内必须） |
| 翻译全部 502 / 超时 | 出网不通 | 配 `IMGTRANS_PROXY`；先 `curl -m 8 https://translate.googleapis.com` 验证 |
| 首次请求特别慢 | 模型首次下载 | 预热进镜像，或首次请求前手动 warmup |
| 中文正常、日文是方块 | 只装了 CJK 的 SC 字面 | `fonts-noto-cjk` 自带全套 face，装了就有 |

## 七、与 Windows 的差异备忘

| 维度 | Windows（开发机） | Linux（部署） |
|---|---|---|
| 字体目录 | `C:\Windows\Fonts`，扁平 | `/usr/share/fonts`，**两层嵌套** |
| 字体丰富度 | 系统自带雅黑/日文/韩文/南亚 | 需手动装，最简镜像一个都没有 |
| raqm | 官方 wheel 常缺（本机实测 False） | pip 版 wheel 自带，通常为 True |
| 大小写 | 不敏感（写错也能跑） | **敏感**（已校验，无问题） |
| 换行 | CRLF | LF（`.env` 两者都能吃） |
| 代理 | 本机 `127.0.0.1:10809` | 服务器直连或独立出口，别照抄 |
| 监听 | `127.0.0.1` 够用 | 容器内必须 `0.0.0.0` |
| 进程管理 | 手动命令行 | systemd / 容器 |
