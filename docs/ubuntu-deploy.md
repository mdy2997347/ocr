# Ubuntu 线上部署手册（imgtrans 图片翻译服务）

> 读者：运维 / 后端。目标：把本仓库部署到一台 Ubuntu 服务器，对外提供
> `POST /v1/translate/img` 图片翻译接口，并能在出问题时自己定位。
>
> 全部结论来自 `docs/linux-deploy.md`（Linux 适配实测）与
> `docs/uv-isolated-deploy.md`（uv 隔离部署实测）。本文只讲"怎么落地 + 怎么验证"，
> 原理请回看那两份。

部署形态：**uv 目录部署 + systemd + nginx 反代**。一个目录装齐解释器、依赖、
模型、配置，`rm -rf` 该目录等于彻底卸载；同机其它 Python 项目完全不受影响。

```
客户端 ──HTTPS/80──> nginx ──127.0.0.1:8000──> systemd: imgtrans(非 root) ──> PaddleOCR + Google 翻译
```

> **关于端口**：下文出现的 `8000` 是默认值。真实值以安装目录下 `.env` 的
> `IMGTRANS_PORT` 为准（`install.sh` 若发现 `.env` 已存在，会以 `.env` 为准并同步 nginx）。
> 排查时先 `sudo grep IMGTRANS_PORT /opt/imgtrans/.env`，别照着文档猜。

---

## 一、前置条件

| 项目 | 要求 | 说明 |
|---|---|---|
| 操作系统 | **Ubuntu 22.04 / 24.04 LTS**（x86_64） | Debian 12 亦可（包名需核对）。**不能用 Alpine**：paddle 只发 glibc（manylinux）wheel |
| CPU | 支持 **AVX** | 2011 年前的 Xeon 会 `Illegal instruction` 直接崩，这是硬件门槛，绕不过 |
| 内存 | **≥ 2GB，推荐 4GB** | 模型常驻约 500MB~1GB；多 worker 会成倍增长（本项目固定单进程） |
| 磁盘 | **≥ 4GB**（留足 uv 下载缓存就是 4GB；缓存清干净 3GB 也能跑） | `.venv` 765MB + `.models` 140MB + 托管解释器 60MB + uv 缓存最多 ~1GB + apt 包 300MB |
| GPU | 可选 | 需要 NVIDIA 驱动 **≥ 550.54.14**（cu126）。没有也能跑，CPU 单图 12~20s |
| 域名 | 可选 | 只有上 HTTPS 才需要；纯 IP 访问可以直接用 |
| 出网 | **必须能访问 `translate.googleapis.com`** | 不能直连就得配 `IMGTRANS_PROXY`，否则翻译全部超时。首次安装还要能访问 `astral.sh`（uv）与 PyPI |
| 权限 | root（或 sudo） | 要装系统包、建用户、写 `/etc` |
| Python | **无需预装，且刻意不用系统 python3** | 由 uv 提供 **3.13**，装进 `<安装目录>/.tools/python`（见下）。`uv.lock` 锁了 98 个包 |

### 关于 Python 版本（22.04 一定要看）

`pyproject.toml` 声明 `requires-python = ">=3.11,<3.14"`。两个 LTS 的系统 Python 都不该直接用：

| 系统 | 自带 python3 | 能否直接用 | 说明 |
|---|---|---|---|
| **Ubuntu 22.04** | **3.10** | **不能** | 低于下限。直接 `uv sync` 会报 `The requested interpreter resolved to ... which is incompatible` |
| Ubuntu 24.04 | 3.12 | 勉强可以 | 满足区间，但会与开发/验证环境（3.13）形成两套小版本行为差异 |

所以安装脚本**固定用 uv 托管的 3.13**，并把 `UV_PYTHON_INSTALL_DIR` 指到 `<安装目录>/.tools/python`：

```bash
UV_PYTHON_INSTALL_DIR=/opt/imgtrans/.tools/python uv python install 3.13
uv sync --frozen --extra cpu --python 3.13 --project /opt/imgtrans
```

为什么一定要指定安装目录（而不是让 uv 装到默认的 `~/.local/share/uv/python`）：

1. 运行用户是 `--no-create-home` 建的，root 装在自己家目录里的解释器它读不到；
2. systemd 单元开了 `ProtectHome=yes`，服务运行时 `/home`、`/root` 直接不可见 ——
   症状是**安装阶段一切正常、启动即崩**，最难查的一类；
3. 装进项目目录 = 解释器属于项目、任何用户可读，也符合
   `docs/uv-isolated-deploy.md` 的"完全自包含"形态。

磁盘代价约 60MB。离线环境可以先在有网机器执行一次
`UV_PYTHON_INSTALL_DIR=... uv python install 3.13`，把整个 `cpython-3.13*` 目录拷过去；
脚本在下载失败时会降级到系统 3.11~3.13 并给出警告（不会硬失败）。

自检一条命令，确认机器够不够格：

```bash
grep -m1 'model name' /proc/cpuinfo; grep -qw avx /proc/cpuinfo && echo "AVX OK" || echo "AVX 缺失——不能跑 paddle"
free -m | awk 'NR==2{print "内存 " $2 "MB"}'; df -h /opt | tail -1
python3 -V                                    # 22.04 会显示 3.10，正常，安装脚本会换成 3.13
curl -s -o /dev/null -w 'google 翻译连通性: %{http_code}\n' -m 8 https://translate.googleapis.com
```

---

## 二、路径 A：一键脚本（推荐）

### A1. 把代码放到服务器

```bash
# 方式一：git（推荐：.env 在 .gitignore 里，天然不会被带上线）
sudo mkdir -p /opt && cd /opt
sudo git clone <仓库地址> imgtrans && cd imgtrans

# 方式二：本地打包上传（离线交付）。--exclude 一个都不能少：
#   .env / .env.key  开发机的代理 127.0.0.1:10809 和真实 Key 会原样上线
#   .venv / .tools   现场 uv sync 生成，拷过去路径必然不对
#   .wheels          离线包 146MB，只在需要离线的机器上单独传
#   本地：
tar --exclude=.venv --exclude=.tools --exclude=.wheels \
    --exclude=.env --exclude=.env.key --exclude=.git \
    -czf imgtrans.tgz ocr/
#   服务器：
sudo tar -C /opt -xzf imgtrans.tgz && sudo mv /opt/ocr /opt/imgtrans
```

**打包后必须验一眼**（这三类东西出现在包里就是事故，尤其 `.env`）：

```bash
tar tzf imgtrans.tgz | grep -E '(^|/)\.env$|(^|/)\.env\.key$|(^|/)\.wheels/' && echo "❌ 包里有敏感文件，重打" || echo "✅ 包干净"
```

必须包含：`pyproject.toml`、`uv.lock`、`imgtrans/`、`scripts/`、`deploy/`。
`.models`（140MB）若已有可一起带上，省一次下载；
离线环境若不想现场下载解释器，可把有网机器上生成的 `.tools/python/cpython-3.13*` 单独拷过去。

### A2. 先 dry-run 看一眼（不动系统，不需要 root）

```bash
sudo bash deploy/install.sh --dry-run
```

它会把每一步将执行的命令打印出来（apt 包清单、useradd、uv sync 参数、sed 渲染、
systemd 操作），确认无误再真跑。

### A3. 正式安装

```bash
# CPU 机器（最常见）
sudo bash deploy/install.sh

# GPU 机器（先确认驱动：nvidia-smi 能看到卡，驱动 >= 550.54.14）
sudo bash deploy/install.sh --gpu

# 自定义目录/端口/运行用户；nginx 自己配
sudo bash deploy/install.sh --dir /srv/imgtrans --port 8100 --user ocr --no-nginx
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--dir <目录>` | `/opt/imgtrans` | 绝对路径；先 `realpath -m` 归一化再判。**黑名单**：拒系统目录及其子路径（`/etc`、`/usr`、`/tmp`…），也拒共享目录本身（`/opt`、`/var/lib`、`/var/www`、`/www/wwwroot`、`/data`…，要再给一层）。`/home` 与 `/root` 被拒的理由**不是权限**，而是单元里的 `ProtectHome=yes` 会让服务看不到自己的 `.venv`（症状：能 `uv sync`、启动即崩）。业务目录一律放行：`/www/wwwroot/ocr`（宝塔）、`/data/imgtrans`、`/var/www/imgtrans`、`/mnt/data/imgtrans` |
| `--port <端口>` | `8000` | 若目标目录已有 `.env`，**以 `.env` 里的 `IMGTRANS_PORT` 为准**（脚本会提示） |
| `--user <用户>` | `imgtrans` | 自动创建系统用户（`--no-create-home --shell /usr/sbin/nologin`）；不能是 root |
| `--gpu` | 关 | `uv sync --extra gpu` + `.env` 里 `IMGTRANS_DEVICE=gpu` |
| `--no-nginx` | 关 | 只装服务；脚本会提示自己反代时必须改的两个参数 |
| `--dry-run` | 关 | 只打印，不改系统 |

脚本做的事（11 步，与路径 B 一一对应）：

| # | 步骤 | 关键点 |
|---|---|---|
| 1 | 前置条件 | 发行版/架构/端口/内存/目录合法性；安装目录在 `/home` 下直接拒 |
| 2 | apt 系统包 | `libgomp1`（paddle 的 OpenMP 运行时）+ 三个字体包 + `curl`/`ca-certificates`/`tar` |
| 3 | 创建运行用户 | 系统用户、nologin、HOME 指向安装目录 |
| 4 | 安装 uv | 官方脚本装到 `/usr/local/bin`（不改 root 的 shell 配置） |
| 5 | 项目文件 | `tar` 管道拷贝（排除 `.venv`/`.tools`/`.env`/`.env.key`/`.wheels`/`out`/`__pycache__`）；代码 `root:组` 且 `go-w`（只读），`.models`/`out`/`.tools` 归运行用户 |
| 6 | 解释器 + `uv sync --frozen` | 固定装 uv 托管的 **3.13** 到 `.tools/python`（22.04 的 3.10 不可用）；`--frozen` 完全信任 lock；缓存落在安装目录内 |
| 7 | 生成 `.env` | 随机 40 位 Key，**只打印一次**，`chmod 600`；**已存在则绝不覆盖**，且对沿用来的 `.env` 做三项体检（开发机代理 / Key 长度 / 监听地址）；写 `.env.tmp` 校验后 `mv` |
| 8 | `doctor.py` 自检 | FAIL 就停在这里，不往下装；提示逐条怎么修 |
| 9 | systemd | 渲染到 `.new` 校验后 `mv` → `enable` + `start` → 轮询 `/healthz`（`.models` 非空 60s，为空 300s）|
| 10 | nginx | 装 nginx、写站点、禁用默认站点（备份）、`nginx -t` 通过才 reload |
| 11 | 收尾 | 打印验证命令、ufw 建议、轮换密钥方法、卸载步骤 |

### A4. 关于 `.env` 里的 API Key

脚本只在**首次生成** `.env` 时打印一次 Key，**且只在终端里回显**：

```
=================== 请立即保存 API Key ===================
  a1b2c3d4e5f6...（40 位十六进制）
  （已写入 /opt/imgtrans/.env，权限 600；本脚本不会再打印它）
=========================================================
```

如果输出不是终端（例如 `sudo bash install.sh 2>&1 | tee install.log`），脚本**不会**
回显明文，而是改写：

```
  API Key 已写入 /opt/imgtrans/.env（600，仅服务用户可读）
  与 /opt/imgtrans/.env.key（600，仅 root 可读）
  查看：sudo cat /opt/imgtrans/.env.key
```

`.env.key` 是密钥留档（`root:root 600`），仅为"日志被 tee、终端被清屏"这种情况准备：

```bash
sudo cat /opt/imgtrans/.env.key        # 取回 Key
sudo shred -u /opt/imgtrans/.env.key   # 存进密码管理器后可删掉留档
```

**立刻存进密码管理器**。丢了不用慌：登录服务器改 `.env` 即可（见 §六 轮换）。
`install.sh` 不会把 Key 写进系统日志或 systemd 单元；排障时也**不要**用 `bash -x`
（xtrace 会把生成的 Key 打进终端和日志）。

### A5. 幂等性

脚本可以重复执行：

* 已存在的 `.env`：**不覆盖**（只修补权限），沿用原 Key；
* 已存在的 `.venv`：只做 `uv sync --frozen` 增量同步；
* 已存在的用户/nginx 站点/服务单元：跳过创建，直接更新内容；
* 服务在跑：`restart` 而不是重复 `start`。

所以"改完代码再跑一遍脚本"就是标准升级动作（§五）。

**退出码**（接 CI / Ansible / `install.sh && 后续动作` 时必须看）：

| 码 | 含义 | 正确动作 |
| --- | --- | --- |
| `0` | 全部步骤成功，服务已通过 `/healthz` | 继续后续动作 |
| `1` | 某一步失败（终端会打印失败步骤与手动回退命令） | 按提示修，别删目录 |
| `2` | 参数错误（未知参数 / 缺参数） | 看 `--help` |
| `3` | **安装完成但服务未就绪**（`/healthz` 在 60s / 300s 内没通） | **不要重装、不要删目录**：全新机器此刻多半还在下载 140MB 模型。先 `journalctl -u imgtrans -f` 看进度，再按收尾提示的三条命令排查 |

`3` 和 `1` 必须分开：`1` 是"东西没装好"，`3` 是"装好了但还没跑起来"。把 `3` 当成功会导致"部署绿灯、其实服务在崩溃循环"；把 `3` 当失败会触发自动重装，反而把正在下载的模型清掉。

---

## 三、路径 B：手动分步（脚本某步失败时照着排查）

与脚本步骤一一对应。命令里的 `/opt/imgtrans`、`imgtrans`、`8000` 按需替换。

### B1. 系统依赖（对应脚本第 2 步）

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    libgomp1 \
    fonts-noto-cjk fonts-noto-core fonts-dejavu-core fonts-liberation \
    curl ca-certificates tar

# 立刻验证 libgomp1 真的装上了 —— 这是最容易被静默跳过的一项
ldconfig -p | grep libgomp.so.1 || echo "缺 libgomp1：paddle 会 import 失败"
```

| 包 | 缺了会怎样 |
|---|---|
| `libgomp1` | `import paddle` 直接报 `libgomp.so.1: cannot open shared object file`，服务起不来 |
| `fonts-noto-cjk` | 中日韩译文全是方块 □，**接口照样返回 200** |
| `fonts-noto-core` | 阿拉伯/希伯来/泰/天城/孟加拉/格鲁吉亚变方块 □ |
| `fonts-dejavu-core` | 拉丁兜底没了，西里尔/希腊可能缺 |

> **不要** `apt install python3-pil` 来代替字体/依赖：系统 Pillow 不带 raqm，
> 阿拉伯语/印地语会断字。本项目用 `uv sync` 装的 pip 版 Pillow（自带 raqm）。

### B2. 创建运行用户（第 3 步）

```bash
sudo useradd --system --no-create-home --home-dir /opt/imgtrans \
     --shell /usr/sbin/nologin --comment "imgtrans" imgtrans
id imgtrans   # uid=999(imgtrans) gid=999(imgtrans) ...
```

服务必须非 root 运行：paddle 要解析不可信图片，root 跑等于把整机押上。

### B3. 安装 uv（第 4 步）

```bash
curl -fsSL -o /tmp/uv-install.sh https://astral.sh/uv/install.sh
sudo env UV_UNMANAGED_INSTALL=/usr/local/bin bash /tmp/uv-install.sh   # 装到 /usr/local/bin，不改 shell 配置
rm -f /tmp/uv-install.sh
uv --version
```

内网无外网时：直接下载 `uv-x86_64-unknown-linux-gnu.tar.gz`（单文件，约 32MB），
解包后 `sudo install -m755 uv /usr/local/bin/uv`。

### B4. 放好项目文件与权限（第 5 步）

```bash
sudo mkdir -p /opt/imgtrans
sudo tar -C /path/to/src -cf - --exclude='./.git' --exclude='./.venv*' \
     --exclude='./.tools' --exclude='./.wheels' --exclude='./out' \
     --exclude='./.env' --exclude='./.env.key' \
     --exclude='__pycache__' . | sudo tar -C /opt/imgtrans -xf -

sudo mkdir -p /opt/imgtrans/.models /opt/imgtrans/out /opt/imgtrans/.tools
sudo chown -R root:imgtrans /opt/imgtrans                 # 属主 root
sudo chmod -R go-w /opt/imgtrans/imgtrans /opt/imgtrans/scripts \
     /opt/imgtrans/docs /opt/imgtrans/deploy              # 模式也要收紧：chown 不改模式，
                                                          # tar 解出来的 g+w 不会被自动去掉
sudo chown -R imgtrans:imgtrans /opt/imgtrans/.models /opt/imgtrans/out /opt/imgtrans/.tools
sudo chmod 750 /opt/imgtrans
```

`.models`、`out`、`.tools`（以及 `uv sync` 生成的 `.venv`）必须可写；其余目录服务只需要读。

### B5. 装解释器与依赖（第 6 步）

```bash
python3 -V        # 22.04 → 3.10（不满足）；24.04 → 3.12（能用但不统一）

# ① 让 uv 装一个 3.13，且必须落在项目目录内（不能是默认的 ~/.local/share/uv/python）
sudo -u imgtrans env HOME=/opt/imgtrans UV_CACHE_DIR=/opt/imgtrans/.tools/uvcache \
    UV_PYTHON_INSTALL_DIR=/opt/imgtrans/.tools/python \
    PATH=/usr/local/bin:/usr/bin:/bin \
    uv python install 3.13

# 复核：解释器真的在项目目录里
ls -d /opt/imgtrans/.tools/python/cpython-3.13*        # 应打印一个目录

# ② 用这个解释器建 venv 并装依赖
sudo -u imgtrans env HOME=/opt/imgtrans UV_CACHE_DIR=/opt/imgtrans/.tools/uvcache \
    UV_PYTHON_INSTALL_DIR=/opt/imgtrans/.tools/python \
    PATH=/usr/local/bin:/usr/bin:/bin \
    uv sync --frozen --extra cpu --project /opt/imgtrans --python 3.13
# GPU 机器：--extra gpu（需要 cu126 索引，pyproject.toml 里已配好）

# ③ 复核 venv 的解释器真实路径也在项目内（否则服务在 ProtectHome 下起不来）
readlink -f /opt/imgtrans/.venv/bin/python             # 应形如 /opt/imgtrans/.tools/python/cpython-3.13.../bin/python3.13
```

* `--frozen`：完全信任 `uv.lock`，不重新解析、不联网校验。线上必加。
* `--python 3.13` 不能省：不指定的话，uv 会优先挑系统 `python3`（22.04 上是 3.10），
  直接报 `The requested interpreter resolved to 3.10.x which is incompatible`。
* `UV_CACHE_DIR` / `UV_PYTHON_INSTALL_DIR` 都指到安装目录内：运行用户没有家目录，
  且 systemd 开了 `ProtectHome=yes`，装在 `~/.local` 里的东西服务读不到。
* 内网离线：加 `--find-links /opt/imgtrans/.wheels --offline`，或设
  `IMGTRANS_DEPLOY_OFFLINE=1` 让脚本自动加；托管解释器需事先从有网机器拷过来。

### B6. 写 `.env`（第 7 步）

```bash
KEY=$(openssl rand -hex 20)     # 无 openssl：tr -dc 'a-f0-9' </dev/urandom | head -c 40

# umask 077 必须在**创建之前**生效：先 tee 出 644 再 chmod 600 的话，
# 中间那一瞬任何本地用户都能读到明文密钥。
( umask 077
  cat > /opt/imgtrans/.env <<EOF
IMGTRANS_API_KEY=$KEY
IMGTRANS_PROXY=
IMGTRANS_HOST=127.0.0.1
IMGTRANS_PORT=8000
IMGTRANS_DEVICE=cpu
IMGTRANS_MODEL_DIR=.models
EOF
)
sudo chown imgtrans:imgtrans /opt/imgtrans/.env
sudo chmod 600 /opt/imgtrans/.env
echo "API Key: $KEY"   # 记下来（最好同时存进密码管理器）
```

* `IMGTRANS_HOST=127.0.0.1` 是对的：对外由 nginx 反代，8000 不暴露公网。
  （只有不用 nginx 又要被外部访问时才改 `0.0.0.0`。）
* `IMGTRANS_PROXY` **留空**。照抄开发机的 `127.0.0.1:10809` 会让翻译全部超时，
  且日志看起来像"网络问题"。
* `IMGTRANS_MODEL_DIR=.models` 相对**项目根**解析（不是 cwd），删目录即清模型。

### B7. 自检（第 8 步）—— 别有侥幸心理，先跑再装服务

```bash
sudo -u imgtrans env HOME=/opt/imgtrans bash -c \
  'cd /opt/imgtrans && .venv/bin/python scripts/doctor.py'
```

有 `[FAIL]` 就返回 1，先修。示例（一台合格的机器）：

```
[ OK ] 依赖 paddleocr: 3.7.0
[ OK ] Pillow raqm: 已启用（阿拉伯/天城文/泰文整形正常）
[ OK ] 字体索引: 42 个文件（系统目录 3 个可用，额外目录 0 个）
[ OK ] 字体 zh: NotoSansCJK-Regular.ttc（cjk 专用）
[ OK ] 推理设备: cpu（配置值 cpu）
[WARN] 监听地址: 127.0.0.1：容器内或需要外部访问时必须改成 0.0.0.0
```

最后那条 WARN 在 nginx 反代场景下是**预期行为**。

### B8. systemd（第 9 步）

```bash
sudo sed -e 's|@INSTALL_DIR@|/opt/imgtrans|g' \
         -e 's|@RUN_USER@|imgtrans|g' \
         -e 's|@RUN_GROUP@|imgtrans|g' \
    deploy/imgtrans.service | sudo tee /etc/systemd/system/imgtrans.service >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now imgtrans
sudo systemctl status imgtrans --no-pager -l
```

关键点：`ExecStart` 用 **venv 里的 python 绝对路径**，不是 `uv run`
（`uv run` 默认会校验 lock/环境、必要时联网，线上启动不该依赖网络）；
`TimeoutStopSec=30` 让 uvicorn 把在途请求（单图 12~20s）跑完再退。

### B9. nginx（第 10 步）

```bash
sudo apt-get install -y --no-install-recommends nginx
sudo sed -e 's|@PORT@|8000|g' deploy/nginx-imgtrans.conf \
   | sudo tee /etc/nginx/sites-available/imgtrans.conf >/dev/null
sudo ln -sfn /etc/nginx/sites-available/imgtrans.conf /etc/nginx/sites-enabled/imgtrans.conf

# 默认站点带 default_server，不删掉的话按 IP 访问会落到 nginx 欢迎页
sudo cp /etc/nginx/sites-available/default /etc/nginx/sites-available/default.bak 2>/dev/null || true
sudo rm -f /etc/nginx/sites-enabled/default

sudo nginx -t && sudo systemctl reload nginx    # 先 -t 再 reload，配置错不至于把现网弄挂
```

两个参数是**必须**的（默认值在线上一定会出故障）：

| 参数 | 默认 | 必须改成 | 不改的症状 |
|---|---|---|---|
| `client_max_body_size` | `1m` | `64m` | 20MB 图 base64 后 27MB，直接 `413`，调用方只看到"上传失败" |
| `proxy_read_timeout` | `60s` | `180s` | OCR 单图 12~20s，长图到 60s 被掐断 → `504`，后端日志里什么都没有 |

> `/v1/` 的鉴权由**服务自身**处理（`X-Api-Key` 或 `Authorization: Bearer`）。
> **不要**在 nginx 再加 `auth_basic`：两套认证叠加后，调用方带了 key 仍被 401 挡在
> nginx 层，排查方向会完全错。

---

## 四、上线验证清单（可复制粘贴）

逐条跑，对比"期望输出"。全绿才算上线完成。

### 1. 服务状态

```bash
systemctl status imgtrans --no-pager -l | head -20
```

期望：`Active: active (running)`，`Main PID` 有值，且**没有**反复重启
（`systemctl show -p NRestarts --value imgtrans` 应为 `0`）。

### 2. 直连探活（绕过 nginx）

```bash
curl -s http://127.0.0.1:8000/healthz
```

期望：`{"ok":true,"device":"cpu"}`

* 返回 `device":"gpu:0"` 说明在用卡；
* 连接拒绝 → 服务没起来：`journalctl -u imgtrans -n 100 --no-pager -l`；
* 有响应但 `ok:false` → 按返回的字段查（通常模型加载失败）。

### 3. 经 nginx 探活

```bash
curl -s http://127.0.0.1/healthz
```

期望同上。不同 → nginx 层问题（端口写错 / `nginx -t` 没通过 / 站点没 enable）。

### 4. 鉴权确实生效

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1/v1/translate/img \
  -H 'Content-Type: application/json' -d '{"lang":"en","img":"https://example.com/a.png"}'
```

期望：`401`。若返回 `200` 或 `500`，说明 `.env` 里的 Key 没生效（服务是 `--dev` 起的？）。

### 5. 真实翻译一次（端到端）

```bash
KEY=$(sudo sed -n 's/^IMGTRANS_API_KEY=//p' /opt/imgtrans/.env)

# 5a. URL 入参（服务端会去下载图，顺带验证出网）
time curl -s -m 180 -X POST http://127.0.0.1/v1/translate/img \
  -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  -d '{"lang":"en","img":"https://example.com/banner.jpg"}' -o /tmp/resp.json
python3 -c "import json;d=json.load(open('/tmp/resp.json'));print({k:(len(v) if isinstance(v,str) else v) for k,v in d.items() if k!='blocks'});print('blocks:',len(d.get('blocks',[])))"

# 5b. base64 入参（最稳，不依赖出网下载）
python3 - <<'PY' > /tmp/req.json
import base64, json, pathlib
b = pathlib.Path("/opt/imgtrans/tests/fixtures/demo_zh.png").read_bytes()
print(json.dumps({"lang": "en", "img": base64.b64encode(b).decode()}))
PY
curl -s -m 180 -X POST http://127.0.0.1/v1/translate/img \
  -H "Content-Type: application/json" -H "X-Api-Key: $KEY" \
  --data-binary @/tmp/req.json -o /tmp/resp2.json
```

期望：`HTTP 200`，JSON 里有 `image_base64`（非空）和 `blocks`（数组，长度 ≥1），
响应含 `total_ms`。**耗时 12~20s 是正常的**（OCR 占 91%），别用 curl 默认超时去试。

业务语义验证：把 `image_base64` 解出来看一眼图，译文应该是**真的文字**而不是
一排方块 □（见 §八 静默失败）。

### 6. 日志里该看到什么

```bash
journalctl -u imgtrans -n 50 --no-pager -l
```

期望能看到（大致按此顺序，中间会夹 uvicorn 自己的启动日志）：

```
INFO imgtrans.env: .env 已加载: /opt/imgtrans/.env（6 项，不覆盖已存在的环境变量）
INFO imgtrans.env: 模型缓存目录: /opt/imgtrans/.models（来自 IMGTRANS_MODEL_DIR）
INFO root: 推理设备 cpu（配置值：cpu）
INFO root: 监听 127.0.0.1:8000，代理 无，鉴权 开启
INFO imgtrans.pipeline: warmup 完成，耗时 6.42s（device=cpu）
INFO uvicorn.error: Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

看 `模型缓存目录` 这一行：如果显示的不是 `/opt/imgtrans/.models`，说明
`IMGTRANS_MODEL_DIR` 没生效（模型会偷偷进 `~/.paddlex`）。
没有 `warmup 完成` 说明预热失败（日志里会有 `OCR 预热失败` 的 WARNING，
服务仍会启动，但首个请求要自己等模型）。

### 7. 磁盘与模型真的落地了

```bash
du -sh /opt/imgtrans/.venv /opt/imgtrans/.models /opt/imgtrans/.tools   # 765MB / 140MB / ~60MB
ls /opt/imgtrans/.models/official_models              # 应有 3 个模型目录
ls -d /opt/imgtrans/.tools/python/cpython-3.13*       # 托管解释器必须在这里
readlink -f /opt/imgtrans/.venv/bin/python            # 必须指向上面的目录，不能是 /home 或 /root
```

### 8. 重启能自愈

```bash
sudo systemctl restart imgtrans && sleep 15 && curl -s http://127.0.0.1:8000/healthz
sudo reboot && sleep 90 && curl -s http://127.0.0.1/healthz    # 开机自启
```

---

## 五、升级与回滚

服务**无状态**（不落库、不上传文件），升级只有三步；风险点在依赖变化。

### 升级

```bash
# 0) 先备份配置（唯一不可再生的东西）
sudo cp /opt/imgtrans/.env /root/imgtrans.env.$(date +%F)

cd /opt/imgtrans
sudo git pull                      # 或覆盖上传新代码
#   ⚠ git pull 不会碰 .env（在 .gitignore 里）；若你手改了 pyproject.toml，注意冲突
#   ⚠ tar 覆盖上传只做"覆盖写入"，没有 --delete 语义：新版本删掉的文件会留在
#     安装目录里成为尸体。介意就先清掉代码目录再解包（保留 .env/.models/.venv）

sudo -u imgtrans env HOME=/opt/imgtrans UV_CACHE_DIR=/opt/imgtrans/.tools/uvcache \
    UV_PYTHON_INSTALL_DIR=/opt/imgtrans/.tools/python \
    PATH=/usr/local/bin:/usr/bin:/bin \
    uv sync --frozen --extra cpu --project /opt/imgtrans    # 依赖没变时几乎瞬间完成

sudo systemctl restart imgtrans
sleep 20 && curl -s http://127.0.0.1:8000/healthz && journalctl -u imgtrans -n 30 --no-pager -l
```

依赖有变化（`uv.lock` 更新过）时，`uv sync --frozen` 会按新 lock 安装；解析失败时
**环境不会被改动**，这是 `--frozen` 相对 `pip install --upgrade` 最实用的差别。

> ⚠ 重跑 `install.sh` 会用 `deploy/imgtrans.service` 模板**覆写** `/etc/systemd/system/imgtrans.service`。
> 如果你手工改过单元（加 `MemoryMax`、改用户等），把定制内容挪到 drop-in，升级时就不会丢：
>
> ```bash
> sudo mkdir -p /etc/systemd/system/imgtrans.service.d
> sudo tee /etc/systemd/system/imgtrans.service.d/override.conf >/dev/null <<'EOF'
> [Service]
> MemoryMax=4G
> EOF
> sudo systemctl daemon-reload
> ```
>
> 同理，`--dir` / `--port` 每次都要带一样的值；`--port` 与 `.env` 冲突时脚本以 `.env` 为准。

### 回滚

**方式一：目录快照（最稳，推荐）**

升级前留一份完整副本，回滚就是换目录 + 重启：

```bash
# 升级前
sudo cp -a /opt/imgtrans /opt/imgtrans.bak.$(date +%F)
# 回滚
sudo systemctl stop imgtrans
sudo mv /opt/imgtrans /opt/imgtrans.broken
sudo mv /opt/imgtrans.bak.2026-11-20 /opt/imgtrans
sudo systemctl start imgtrans
```

注意：`.env` 在副本里，回滚会连配置一起回退；只想回代码就单独换 `imgtrans/`、`scripts/`。

**方式二：只回退依赖（代码没坏、是升级踩到新版本时）**

```bash
cd /opt/imgtrans
sudo git checkout <上一个 commit> -- pyproject.toml uv.lock
sudo -u imgtrans env HOME=/opt/imgtrans UV_CACHE_DIR=/opt/imgtrans/.tools/uvcache \
    UV_PYTHON_INSTALL_DIR=/opt/imgtrans/.tools/python \
    PATH=/usr/local/bin:/usr/bin:/bin uv sync --frozen --extra cpu --project /opt/imgtrans
sudo systemctl restart imgtrans
```

> 更换 Python 小版本（如 3.13 → 3.14）时 `uv sync` 不会自动换解释器：
> 先 `rm -rf /opt/imgtrans/.venv`，再 `uv python install <新版本>` + `uv sync --python <新版本>`。
> 顺带提醒：`paddlepaddle 3.3.1` 只发到 cp313，**3.14 目前没有 wheel**，`requires-python` 的上限就是为此。

**回滚不需要动** systemd 单元和 nginx 站点（除非新版本改了端口/路径）。

---

## 六、安全清单

| 项 | 做法 | 自检命令 |
|---|---|---|
| 服务非 root | `User=imgtrans`，nologin shell | `ps -o user= -p $(systemctl show -p MainPID --value imgtrans)` → `imgtrans` |
| 端口不暴露 | 只监听 `127.0.0.1`，对外仅 nginx 80/443 | `ss -ltnp \| grep 8000` → 只出现 `127.0.0.1:8000` |
| 防火墙 | ufw 只放 22/80/443，**不放 8000** | `sudo ufw status numbered` |
| `.env` 权限 | `600`，属主为运行用户 | `stat -c '%a %U:%G' /opt/imgtrans/.env` → `600 imgtrans:imgtrans` |
| 密钥留档 | `.env.key` 必须 `root:root 600`（只有 root 能读，取走后建议 `shred -u`） | `stat -c '%a %U:%G' /opt/imgtrans/.env.key` → `600 root:root` |
| 不泄露到日志 | 不把安装输出 tee 到 644 文件；不用 `bash -x` 跑安装；`.env` 不进 tar 包 | `tar tzf imgtrans.tgz \| grep -c '\.env'` → `0` |
| 密钥轮换 | 改 `.env` → `systemctl restart`（旧 Key 立即失效） | 见下 |
| 代码只读 | 代码归 `root:imgtrans` **且 `go-w`**（`chown` 只改属主，不改模式；不解 tar 出来的 g+w 就白搭） | `find /opt/imgtrans/imgtrans \( -perm -o+w -o -perm -g+w \) \| head` → 应为空（`-group` 后面要接组名，写成 `-group -w` 会被当成组名而报错） |
| 日志不泄密 | 服务从不打印 Key（`doctor.py` 只报"已配置"） | `journalctl -u imgtrans \| grep -i apikey` → 无输出 |
| 依赖可复现 | 只用 `uv sync --frozen`，`uv.lock` 入库 | `git log --oneline -1 -- uv.lock` |
| SSRF 防护 | 服务自身拒绝内网地址（`127.0.0.1`/`169.254.169.254` 等） | 返回 `E_IMAGE_FETCH` |
| 系统补丁 | 定期 `apt-get update && apt-get -y upgrade` | 建议配 `unattended-upgrades` |

### 密钥轮换步骤

```bash
NEW=$(openssl rand -hex 20)
sudo cp /opt/imgtrans/.env /root/imgtrans.env.$(date +%F)
sudo sed -i "s|^IMGTRANS_API_KEY=.*|IMGTRANS_API_KEY=$NEW|" /opt/imgtrans/.env
sudo chmod 600 /opt/imgtrans/.env
sudo systemctl restart imgtrans
echo "新 Key: $NEW"      # 发给调用方
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1/v1/translate/img \
  -H "X-Api-Key: $NEW" -H 'Content-Type: application/json' -d '{"lang":"en","img":"x"}'   # 期望非 401
```

服务是**单 Key** 模式，没有灰度共存期：轮换瞬间旧 Key 失效。多调用方时提前通知。

### 可选：nginx 限流（防单客户端打满 CPU）

```nginx
# 放在 http 块（/etc/nginx/nginx.conf），或放进站点文件的 server 外
limit_req_zone $binary_remote_addr zone=imgtrans:10m rate=30r/m;

# server 内
location /v1/ {
    limit_req zone=imgtrans burst=3 nodelay;
    limit_req_status 429;
    proxy_pass http://127.0.0.1:8000;
    # ...其余 proxy_set_header 同上
}
```

OCR 是 CPU 密集型，单实例并发能力很有限（模型常驻内存，多 worker 不划算），
限流比扩容更实际。

---

## 七、排错表

| 症状 | 原因 | 处理 |
|---|---|---|
| 服务起不来，日志 `ImportError: libgomp.so.1: cannot open shared object file` | 缺 `libgomp1`（paddle 的 OpenMP 运行时） | `apt-get install -y libgomp1`，再 `ldconfig -p \| grep libgomp` 确认 |
| `uv sync` 报 `The requested interpreter resolved to ... (3.10.x) which is incompatible` | 用了系统 python3（**Ubuntu 22.04 自带 3.10**，低于 `requires-python=">=3.11"`） | 必须用 uv 托管解释器：`uv python install 3.13` + `uv sync ... --python 3.13`，并把 `UV_PYTHON_INSTALL_DIR` 指到项目内 |
| **安装阶段一切正常，`systemctl start` 立刻失败**，日志里有 `Permission denied` / `No such file` 指向 `/root/.local/...` 或 `/home/...` | 解释器/venv 落在 home 目录下，而单元开了 `ProtectHome=yes` | `readlink -f /opt/imgtrans/.venv/bin/python` 看真实路径；不在项目内就 `rm -rf .venv` 后重跑脚本（会把解释器装进 `.tools/python`） |
| `--dir` 被拒：`安装目录不能位于 /xxx 下（系统目录…）` / `不能是 /xxx 本身（共享目录）` | 安全校验：脚本要对安装目录 `chown -R` + 解包，落在系统目录会破坏系统，落在共享目录本身会波及其它服务 | 换到业务目录下的**子目录**：`/opt/imgtrans`、`/srv/imgtrans`、`/www/wwwroot/ocr`、`/data/imgtrans`、`/var/www/imgtrans`。`/etc/.`、`/opt/../etc` 这类写法归一化后等同 `/etc`，一样会被拒 |
| 首次 `uv sync` 卡在 `Resolved ...` 或超时 | 装依赖要访问 PyPI；或没加 `--frozen` 导致 uv 尝试联网校验 lock | 确认出网/配镜像；线上必须 `--frozen`；内网用 `--find-links .wheels --offline` |
| **中文/日文译文全是一排小方块 □，接口 200、图也回来了** | 缺 `fonts-noto-cjk`（字体找不到**不报错**，退化成 Pillow 10px 位图字体） | `apt-get install -y fonts-noto-cjk`；`doctor.py` 的「字体 zh」一项会直接指出回退到了哪个文件 |
| 中文能显示，但字形是日式的（直/骨/关） | `.ttc` 里 face 0 是日文；或用了系统 Pillow | 本项目已按族名自动选 face；确认用的是 pip 版 Pillow（`uv sync` 装的），不是 `apt install python3-pil` |
| 阿拉伯/印地/泰文断字、辅音簇错位 | Pillow 无 raqm | 用 pip 版 Pillow（自带 raqm）；doctor 的「Pillow raqm」应为已启用。别用系统 Pillow |
| 调用方报 **504**，后端日志里连请求记录都没有 | `proxy_read_timeout` 还是默认 60s，OCR 12~20s 被掐断 | nginx 里设 `proxy_read_timeout 180s; proxy_send_timeout 180s;`，`nginx -t && systemctl reload nginx` |
| 调用方报 **413** | `client_max_body_size` 默认 1m | 设 `client_max_body_size 64m;`（20MB 图 base64 后 27MB） |
| 调用方报 **401**，但确实带了 `X-Api-Key` | ① `.env` 里没 Key 或服务以 `--dev` 起；② nginx 又加了一层 `auth_basic`；③ Key 前后有空格/换行 | `journalctl` 看启动日志的「鉴权 开启/关闭」；`curl` 直连 8000 对比；去掉 nginx 的 auth_basic |
| **模型目录写不进去**，模型反复重新下载或落到 `~/.paddlex` | `.models` 属主不是运行用户；或开了 `ProtectSystem`/`ProtectHome` 却没 `ReadWritePaths`；或 `IMGTRANS_MODEL_DIR` 未生效 | `chown -R imgtrans:imgtrans /opt/imgtrans/.models`；单元里保留 `ReadWritePaths=/opt/imgtrans`；日志看「模型缓存目录: ...」是否为安装目录内 |
| 启动报 `E_DEVICE` / 设备不可用，`is_compiled_with_cuda()` 为 False | `.env` 配了 `gpu` 但装的是 **CPU 版 paddle** | `uv sync --frozen --extra gpu`（cu126 索引已在 pyproject 里配好），或先把 `IMGTRANS_DEVICE=cpu` 上线 |
| GPU 机器上服务看不到卡 | 单元里误加了 `PrivateDevices=yes`，`/dev/nvidia*` 被隐藏 | 删掉该行（模板里已注明不要加）；`nvidia-smi` 在宿主机上应正常 |
| **磁盘满**，`uv sync` / 模型下载报 `No space left on device` | `.venv` 765MB + 模型 140MB + apt 缓存 | `du -sh /opt/imgtrans/*` 定位；`apt-get clean`；`uv cache prune`；扩盘或换 `--dir` 到更大分区 |
| 首次请求特别慢（30s+） | 服务未预热或模型未落地 | 正常启动会预热（6~8s）；若 `--no-warmup` 起的，第一个请求会自己下模型 |
| 翻译全部超时 / 返回 `E_INTERNAL`（提示连接失败） | 出网不通，或照抄了开发机的 `IMGTRANS_PROXY=127.0.0.1:10809`（线上没有那个代理） | `curl -m 8 https://translate.googleapis.com` 验证；能直连就把 `IMGTRANS_PROXY` 清空 |
| 打开域名显示 `Welcome to nginx!` | 默认站点没禁用，带 `default_server` 抢走了请求 | `rm -f /etc/nginx/sites-enabled/default && nginx -t && systemctl reload nginx` |
| 服务反复重启（`NRestarts` 持续上涨） | 启动即崩：通常是 Key 缺失、设备不匹配、端口被占 | `journalctl -u imgtrans -n 100 --no-pager -l` 看第一条 traceback；`ss -ltnp \| grep 8000` 查端口占用 |
| 服务起来但 `/healthz` 一直不通，`journalctl` 显示 `Cannot assign requested address` | `IMGTRANS_HOST=0.0.0.0` 写成了具体 IP，或 nginx 反代端口与 `.env` 不一致 | `.env` 用 `127.0.0.1`；`grep IMGTRANS_PORT .env` 与 nginx `proxy_pass` 对齐 |
| 机器很老，`Illegal instruction (core dumped)` | CPU 无 AVX，paddle 跑不了 | 换支持 AVX 的机器（2011 年后的基本都有） |
| `uv sync` 报 `No solution found` | 有人手改了 `pyproject.toml` 的 pin（典型：把 numpy 写死成 2.5.x，paddlex 要求 `<2.4`） | 恢复 `numpy>=1.24,<2.4`，`uv lock` 重新解析；失败时环境不会被破坏 |

---

## 八、静默失败清单（这类才是真坑）

以下问题**都不报错、接口都返回 200**，只在"看图片"时才暴露。上线验证必须看图，
不能只看状态码。

| 现象 | 真实原因 | 一句话识别方法 |
|---|---|---|
| 译文是一排方块 □ / 字很糊很小 | 字体索引为 0 或回退到拉丁字体，退化到 Pillow 位图字体 | `doctor.py` 的「字体 zh/ja/ko」三项必须是「专用」；出现「回退到 DejaVuSans → 会画成豆腐块」就是它 |
| 中文出现日式字形（直、骨、关） | `NotoSansCJK-Regular.ttc` 的 face 0 是日文 | 本项目已自动按族名选 SC face；若仍是日式，检查 Pillow 是不是 pip 版 |
| 阿拉伯字母各自孤立、印地语辅音簇错位 | Pillow 缺 raqm | `doctor.py`「Pillow raqm」= 未启用 |
| 模型每次启动都重新下载 / 落到 `~/.paddlex` | `IMGTRANS_MODEL_DIR` 没生效（PaddleX 在 import 时就把路径求值成常量，晚设置无效） | 启动日志「模型缓存目录: X」，X 必须是 `/opt/imgtrans/.models`；`ls ~/.paddlex` 应为空 |
| 翻译很慢但能成功、偶发超时 | `IMGTRANS_PROXY` 指到了一个不存在的代理，每次连接先等超时 | `grep PROXY /opt/imgtrans/.env` 必须是空 |
| 单图偶发被掐断（长图/大图） | nginx 60s 超时 | `grep read_timeout /etc/nginx/sites-enabled/imgtrans.conf` |

**上线必做的一次人工确认**：拿一张中文图跑一次，把返回的 `image_base64` 存成
png 打开看一眼。这是唯一能发现"字体类静默失败"的方法。

```bash
python3 -c "
import base64,json,pathlib
d=json.loads(pathlib.Path('/tmp/resp2.json').read_text())
pathlib.Path('/tmp/out.png').write_bytes(base64.b64decode(d['image_base64']))
print('写出 /tmp/out.png，', len(d['blocks']), '块，服务端耗时', d.get('total_ms'), 'ms')
"
```

---

## 九、日常运维

| 任务 | 命令 |
|---|---|
| 看日志 | `journalctl -u imgtrans -f`（`-n 200` 看历史，`--since "10 min ago"`） |
| 重启 / 停止 | `systemctl restart imgtrans` / `systemctl stop imgtrans` |
| 改配置 | 改 `/opt/imgtrans/.env` → `systemctl restart imgtrans`（**不要** `daemon-reload`，unit 没变） |
| 改端口 | 改 `.env` 的 `IMGTRANS_PORT` + 改 nginx `proxy_pass` → `systemctl restart imgtrans && nginx -t && systemctl reload nginx` |
| 看真实设备 | `curl -s 127.0.0.1:8000/healthz` → `device` 字段 |
| 备份 | 只有 `.env` 必须备（`/opt/imgtrans/.env`）。模型和 venv 可重建，代码在 git 里 |
| 清理 uv 缓存 | `rm -rf /opt/imgtrans/.tools/uvcache`（不影响已装依赖）。**不要删 `.tools/python`**：venv 的解释器是它里面的符号链接目标，删了整个环境就废了（要重建：`rm -rf .venv .tools/python` 后重跑脚本） |
| 彻底卸载 | `systemctl disable --now imgtrans && rm -f /etc/systemd/system/imgtrans.service && systemctl daemon-reload`<br>再 `rm -rf /opt/imgtrans`（**仅当该目录是装本服务时创建的**）+ `userdel imgtrans`（**仅当用户是脚本创建的**）。<br>目录/账号在安装前就存在时不要整目录删、不要 userdel —— 脚本的收尾提示会按情况区分 |

**监控建议**：`GET /healthz`，间隔 30s，超时 5s，连续 3 次失败告警。
注意它**不校验业务链路**（不验 OCR、不验翻译出网）——想监控端到端，得周期性
打一次真实请求，但那是 12~20s 的重活，一天几次即可，别做成 30s 一次。

**不要做的三件事**：

1. `uvicorn --workers N` / `--workers 4`：每个进程各加载一份模型，8GB 机器直接 OOM；
   要提高吞吐请起多个实例（不同端口 + 不同目录）再前置负载均衡。
2. 把 `IMGTRANS_HOST` 改成 `0.0.0.0` 并把 8000 放进防火墙白名单：绕过了 nginx 就等于
   丢掉 `client_max_body_size` 和超时保护，还多暴露一个端口。
3. 用 `uv run` 当 systemd 的 `ExecStart`：默认会校验/同步依赖，镜像站一挂服务就起不来。

---

## 十、文件清单（部署资产）

| 文件 | 作用 |
|---|---|
| `deploy/install.sh` | Ubuntu 一键安装（幂等，支持 `--dry-run` / `--gpu` / `--no-nginx`） |
| `deploy/imgtrans.service` | systemd 单元模板（`@INSTALL_DIR@` / `@RUN_USER@` / `@RUN_GROUP@` 由脚本替换） |
| `deploy/nginx-imgtrans.conf` | nginx 站点模板（`@PORT@` 由脚本替换；含 HTTPS / certbot 注释示例） |
| `docs/ubuntu-deploy.md` | 本文 |

相关文档：

* `docs/linux-deploy.md` —— Linux 适配结论、字体/raqm/设备逐项实测、Dockerfile
* `docs/uv-isolated-deploy.md` —— uv 隔离部署原理、实测数据（体积/耗时）、离线交付
* `docs/api-usage.md` —— 调用方接入说明（请求字段、错误码、PHP/Node 示例）
