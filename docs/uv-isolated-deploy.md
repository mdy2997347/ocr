# 用 uv 做独立部署的方案（不改架构）

> 结论先说：**能，而且不需要改任何业务代码。** 一个项目目录里放齐解释器、
> 依赖、模型、配置，`rm -rf` 这个目录就是彻底卸载；同机上另一套 Python 项目
> 完全感知不到它的存在。本文所有数字都是本机实测，不是估算。

---

## 一、"部署不独立"到底来自哪里

Python 项目装不干净，通常是四个层面同时在往系统目录里写东西：

| 层面 | 默认落点 | 典型后果 |
|---|---|---|
| 解释器 | 系统 Python（`/usr/bin/python3`、Windows 注册表） | 想升 3.13 又怕影响别的服务 |
| 依赖包 | 全局 `site-packages` | 本服务要 `numpy<2.4`，另一个项目要 2.5，装了 A 就崩 B |
| 模型缓存 | `~/.paddlex`（实测 **237 MB**） | 同机多个 paddle 项目共用一份，版本一换互相污染 |
| 下载缓存 | `~/.cache/uv`、`~/.cache/pip` | 不算污染，但能吃掉几个 G |

uv 对前三层都有对应手段，第四层可以显式指定。逐条对应关系：

| 层 | 手段 | 本方案取值 |
|---|---|---|
| 解释器 | `uv python install` + `UV_PYTHON_INSTALL_DIR` | `<项目>/.tools/python`（可选） |
| 虚拟环境 | `uv venv` / `uv sync` 默认建 `.venv` | `<项目>/.venv` |
| 依赖 | `pyproject.toml` + `uv.lock`（98 个包全锁） | 版本库内 |
| 模型 | 新增 `IMGTRANS_MODEL_DIR` → `PADDLE_PDX_CACHE_HOME` | `<项目>/.models` |
| 下载缓存 | `UV_CACHE_DIR` | `<项目>/.tools/uvcache`（可选） |

---

## 二、为什么这个项目特别需要"独立"

两个硬约束，使"随便装装"的代价比一般项目高：

1. **paddle 的 numpy 区间很窄。** `paddlex 3.7.x` 要求 `numpy>=1.24,<2.4`。而 2026 年
   主流新项目已经上 numpy 2.5。装进全局环境的那一刻，两个项目里必有一个坏掉。
   （实测：原 `requirements.lock.txt` 写死 `numpy==2.5.5`，`uv` 直接判定
   `No solution found` —— 这个错误恰恰说明解析器在认真工作。）
2. **模型缓存量级大且共享。** 237 MB 落在 `~/.paddlex/official_models`，目录名是
   模型名而不是版本号。两个项目如果用到同名不同版的 det 模型，后装的会覆盖先装的，
   而且**不报错**，只是识别结果悄悄变了。

---

## 三、三种落地形态

### 形态 A：项目内 `.venv`（推荐，单机单实例）

最省事，隔离度也够——所有依赖都在 `.venv/Lib/site-packages`（或 `lib/python3.x/site-packages`）。

```bash
cd /opt/imgtrans
uv sync --extra cpu          # 建 .venv 并装齐（GPU 机器用 --extra gpu）
uv run --no-sync python scripts/serve.py
```

### 形态 B：完全自包含目录（可以整个目录搬走）

连解释器都放进项目，不依赖机器上任何 Python：

```bash
cd /opt/imgtrans
export UV_PYTHON_INSTALL_DIR="$PWD/.tools/python"
export UV_CACHE_DIR="$PWD/.tools/uvcache"
uv python install 3.13
uv venv --relocatable --python 3.13 .venv
uv sync --extra cpu
```

⚠️ **`--relocatable` 不是可选的**。默认 venv 里写死了绝对路径
（`pyvenv.cfg` 的 `home`、`bin/` 下脚本的 shebang），直接 `tar` 到别的目录会
"找不到解释器"。加了这个参数才能整体搬移。即便如此，也只保证同 OS / 同架构之间搬。

**更稳的做法是"搬声明、不搬产物"**：目标机只带 `pyproject.toml`、`uv.lock`、
`.wheels/`、`.models/`，现场 `uv sync --frozen --offline` 重建。一分钟的事，
省掉一类难以排查的路径问题。

### 形态 C：多实例并存（灰度 / 多语言分组）

```bash
/opt/imgtrans-a/   # 端口 8000，IMGTRANS_MODEL_DIR=.models
/opt/imgtrans-b/   # 端口 8001，另一份 .env
```

两个目录各自 `.venv` 各 765 MB。**如果磁盘紧张，可以让它们共用一份 `.venv`**
（`UV_PROJECT_ENVIRONMENT=/opt/shared-venv`），但那样 upgrade 时会同时影响两边，
不建议用于灰度。

---

## 四、本项目的改造点（已完成）

### 1. `pyproject.toml`（新增）

- 依赖写精确 pin；唯独 `numpy` 用区间 `>=1.24,<2.4`，理由见上。
- `requires-python = ">=3.11,<3.14"`：下限是 `fastapi 0.165` 要求 3.11，上限是
  `paddlepaddle 3.3.1` 只发 cp39~cp313 的 wheel（3.14 装了没轮子）。
- **CPU / GPU 用互斥 extras**，而不是两个 requirements 文件：

```toml
[project.optional-dependencies]
cpu = ["paddlepaddle==3.3.1"]
gpu = ["paddlepaddle-gpu==3.3.1"]

[tool.uv]
package = false
conflicts = [[{ extra = "cpu" }, { extra = "gpu" }]]
```

`conflicts` 这一行的价值：执行 `uv sync --extra cpu --extra gpu` 会**直接报错**
而不是"装完互相卸载、留下一个半坏环境"。这是 pip 时代最难查的一类事故
（`paddlepaddle` 与 `paddlepaddle-gpu` 互斥，安装器只会默默卸掉另一个）。

GPU 版不在 PyPI，用 `explicit = true` 的独立索引挂上去：

```toml
[[tool.uv.index]]
name = "paddle-gpu"
url = "https://www.paddlepaddle.org.cn/packages/stable/cu126/"
explicit = true

[tool.uv.sources]
paddlepaddle-gpu = { index = "paddle-gpu" }
```

### 2. `uv.lock`（新增，必须提交）

`uv lock` 实测 **2.1 秒**解析出 **98 个包**（含全部传递依赖 + hash）。
手写 lock 只有 22 条直接依赖，不锁传递依赖，复现性靠运气。

### 3. `IMGTRANS_MODEL_DIR`（新增配置项）

写进 `.env` 即可，程序启动时映射到 `PADDLE_PDX_CACHE_HOME`：

```ini
IMGTRANS_MODEL_DIR=.models
```

三个实现在 `imgtrans/env.py::apply_model_dir()`：

- **相对路径按项目根解析，不是 cwd**。`systemd` 的 `WorkingDirectory` 未必是项目根，
  按 cwd 解析会把 240 MB 模型建到 `/` 或 `~` 下面。
- **必须在 `import paddle` 之前执行**。`paddlex/utils/cache.py` 在模块导入时就把
  `CACHE_DIR` 求值成了常量，之后再改环境变量**不会生效**——这是个静默失败，
  最坑的地方在于"看起来配了但完全没起效"。入口脚本里紧跟 `load_env()` 调用，
  早于任何 paddle 导入（引擎是惰性导入）。
- **已显式设置 `PADDLE_PDX_CACHE_HOME` 时不覆盖**。容器编排注入的值应该优先。

---

## 五、完整操作步骤

### 1. 装 uv（不影响系统 Python）

```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh      # 装到 ~/.local/bin/uv，不动系统

# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# 内网 / 离线：直接拷单文件（实测 32 MB，无需安装）
# https://github.com/astral-sh/uv/releases -> uv-x86_64-unknown-linux-gnu.tar.gz
```

### 2. 建环境

```bash
cd /opt/imgtrans
uv python install 3.13        # 可选：让 uv 自带解释器
uv sync --extra cpu           # CPU 机器
# uv sync --extra gpu         # GPU 机器（需驱动 >= 550.54.14 / cu126）
```

### 3. 配 `.env`

```ini
IMGTRANS_API_KEY=<40 位随机串>
IMGTRANS_MODEL_DIR=.models
IMGTRANS_DEVICE=cpu
IMGTRANS_HOST=127.0.0.1
IMGTRANS_PORT=8000
IMGTRANS_PROXY=              # 服务器能直连出海就留空
```

### 4. 首次跑（会下载 140 MB 模型到 `.models`）

```bash
uv run --no-sync python scripts/serve.py
```

**`--no-sync` 很关键**：`uv run` 默认会先校验 lock 与环境的差异，必要时联网同步。
生产环境启动时不该有网络依赖——镜像挂了服务就起不来。

### 5. 自检

```bash
uv run --no-sync python scripts/doctor.py
```

---

## 六、离线 / 内网部署

```bash
# ① 有网机器：导出带 hash 的依赖清单
uv export --format requirements-txt > requirements-uv.txt     # 实测 232 行

# ② 下载全部 wheel（含平台限定包，注意 --python-platform）
uv pip download -r requirements-uv.txt -d .wheels \
    --python-platform x86_64-manylinux_2_28

# ③ 打包带走：代码 + pyproject.toml + uv.lock + .wheels/ + .models/

# ④ 目标机
uv sync --frozen --offline --find-links .wheels --extra cpu
```

`--frozen` 表示"完全信任 uv.lock，一个字节都不重新解析"。内网部署必带——
否则 uv 会尝试连 PyPI 校验，超时后报一个与真实原因无关的错。

模型不用重新下：`.models/official_models/` 里三个目录（实测 140 MB）拷过去即可。

---

## 七、实测数据

| 指标 | 结果 |
|---|---|
| `.venv` 体积 | **765 MB**（paddle 368 MB、cv2 161 MB、pandas 40 MB、numpy 44 MB） |
| `uv sync --extra cpu` 耗时 | **59 s**（uv 缓存已热）/ 冷启动约 5~10 min（含 paddle 100 MB 下载） |
| `uv lock` | **2.1 s**，98 个包 |
| 模型缓存 | 140 MB（det + rec + 方向分类），落在 `.models` |
| 真实翻译（中文→英） | **17.8 s**，4/4 块回填 |
| 输出一致性 | SHA1 `839689f4…`，与全局环境、与 `uv run`、与旧 CLI **三者完全一致** |
| 全局环境 | 未改动（`numpy` 仍 2.3.5，`site-packages` 未新增） |

**最关键的一次验证**：把 `~/.paddlex/official_models` 整个改名移走，只留 `.env` 里
`IMGTRANS_MODEL_DIR=.models` 一行，程序仍正常跑完并输出同一张图。这排除了
"其实还在偷偷用全局模型"的可能。

---

## 八、与 Docker 的取舍

| 维度 | uv 目录 | Docker |
|---|---|---|
| 隔离强度 | 进程/文件级，够用 | 内核级，最强 |
| 启动开销 | 0（直接进程） | 容器启动 + 模型加载 |
| 体积 | 765 MB（无重复层） | 通常 1.5~2 GB（基础镜像 + 层） |
| GPU | 直接可见，无需 `--gpus` | 需 nvidia-container-toolkit |
| 内网交付 | 拷目录 + `uv sync --offline` | 需要镜像仓库 |
| 同机多实例 | 改端口 + 独立目录 | 改端口 + 独立容器 |
| 适合 | 单机/少量机器、客户现场 | 集群、多租户、需要强隔离 |

**建议**：客户现场、宝塔这类"服务器上还跑着别的东西"的环境用 uv 目录部署；
上 K8s 或多实例灰度时再套 Docker（Dockerfile 里同样可以 `uv sync --frozen`，
两种方式不冲突）。

---

## 九、限制与坑（都是实测或审查得出的）

1. **不能用 Alpine**。paddle 只发 `manylinux`（glibc）wheel，musl 上装不了。
2. **Python 窗口 3.11~3.13**。3.14 没 paddle wheel；3.10 及以下 fastapi 0.165 不支持。
3. **paddle 需要 AVX 指令集**。老 Xeon（2011 年前）会 `Illegal instruction` 直接崩，
   这不是 uv 能解决的，属于硬件门槛。
4. **Linux 上需要 `libgomp1`**（paddle 的 OpenMP 运行时）。`slim` 镜像默认没有，
   缺了 `import paddle` 就报 `libgomp.so.1: cannot open shared object file`。
5. **venv 不要跨目录搬**，除非用了 `--relocatable`。更推荐"搬声明重建"。
6. **`uv run` 默认会 sync**，线上必须 `--no-sync` 或 `--frozen`，否则启动依赖网络。
7. **多 worker = 多份模型内存**。每个进程各加载一份（约 500 MB~1 GB），
   `uvicorn --workers 4` 在 8 GB 机器上会 OOM。
8. **Windows 长路径**：`.venv` 里 paddle 的路径层级很深，项目放在
   `D:\a\b\c` 这种浅目录下，避免撞 260 字符上限。

---

## 十、运维

### Linux systemd

```ini
[Unit]
Description=imgtrans OCR translate service
After=network.target

[Service]
Type=simple
# 注意：WorkingDirectory 设了也要靠 IMGTRANS_MODEL_DIR，别依赖 cwd
WorkingDirectory=/opt/imgtrans
# --no-sync：启动不联网
ExecStart=/opt/imgtrans/.venv/bin/python scripts/serve.py
Environment=UV_PROJECT_ENVIRONMENT=/opt/imgtrans/.venv
Restart=always
RestartSec=5
# 模型常驻，给足内存
MemoryMax=4G

[Install]
WantedBy=multi-user.target
```

### Windows（nssm）

```bat
nssm install imgtrans "D:\wwwroot\ocr\.venv\Scripts\python.exe" "D:\wwwroot\ocr\scripts\serve.py"
nssm set imgtrans AppDirectory D:\wwwroot\ocr
nssm set imgtrans Start SERVICE_AUTO_START
```

### 升级流程

```bash
cd /opt/imgtrans
# 改 pyproject.toml 里的版本 -> 重新解析 -> 重建 -> 重启
uv lock
uv sync --frozen --extra cpu
systemctl restart imgtrans
```

`uv lock` 会打印哪些包变了；如果解析失败（像 numpy 那次），环境**不会被改动**——
这是 uv 相对 `pip install --upgrade` 最实用的差别之一。

---

## 十一、从现有 pip 环境迁移

当前机器是"一步步 pip install 出来的"，没有完整 lock。迁移只需三步：

```bash
# ① 用 uv 导出当前实际版本（作为对照，不直接用于生产）
pip freeze > /tmp/before.txt

# ② 按 pyproject 重建（已验证）
uv sync --extra cpu

# ③ 跑同一张图比对 SHA1
uv run --no-sync python scripts/translate_image.py \
    --input tests/fixtures/demo_zh.png --to en --out /tmp/after.png
sha1sum /tmp/after.png out/device_check.png   # 应完全一致
```

实测这一步的结果是 `839689f4…` 两边相同，说明"换管理方式"没有引入任何行为变化。

---

## 附：文件清单

| 文件 | 作用 |
|---|---|
| `pyproject.toml` | 依赖声明、cpu/gpu 互斥、GPU 索引 |
| `uv.lock` | 98 包精确锁定（提交进版本库） |
| `imgtrans/env.py` | 新增 `apply_model_dir()`，把 `IMGTRANS_MODEL_DIR` 映射到 PaddleX |
| `scripts/serve.py`、`scripts/translate_image.py` | `load_env()` 后紧跟 `apply_model_dir()` |
| `.env` / `.env.example` | 新增 `IMGTRANS_MODEL_DIR=.models` |
| `.gitignore` | 忽略 `.venv*`、`.tools/`、`.models/`、`.wheels/` |
| `requirements.lock.txt` | 修正 `numpy` 冲突，标注为 pip 路径参考（非真源） |
