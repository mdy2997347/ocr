#!/usr/bin/env bash
# =============================================================================
# imgtrans 图片翻译服务 —— Ubuntu 一键部署脚本
#
# 用法（必须以 root 运行；--dry-run 可不用 root，用于先看一眼要做什么）：
#   sudo bash deploy/install.sh --dry-run          # 只打印将执行的动作，不动系统
#   sudo bash deploy/install.sh                    # 默认 /opt/imgtrans，端口 8000，CPU
#   sudo bash deploy/install.sh --dir /srv/imgtrans --port 8100
#   sudo bash deploy/install.sh --user ocr --gpu   # GPU 机器（需已装 NVIDIA 驱动 >= 570）
#   sudo bash deploy/install.sh --no-nginx         # 只装服务，nginx 自己配
#
# 每条"为什么"都对应一个线上真实会踩的坑（详见 docs/linux-deploy.md）：
#   * 必须装 libgomp1        —— 缺了 `import paddle` 直接报 libgomp.so.1 找不到
#   * 必须装三个字体包        —— 缺了**不报错**，只会把译文画成一排方块 □ 并正常返回 200
#   * 不能用 Alpine          —— paddle 只发 glibc（manylinux）wheel
#   * 不能用 apt 的 python3-pil —— 系统 Pillow 不带 raqm，阿拉伯/印地/泰文整形会断字
#   * 不能用系统 python3     —— Ubuntu 22.04 自带 3.10，低于 requires-python>=3.11，
#                               uv sync 会直接拒绝；本脚本固定用 uv 托管的 3.13，
#                               且装进 <安装目录>/.tools/python（落在家目录里会被
#                               systemd 的 ProtectHome=yes 挡住，表现为"能装不能跑"）
#   * uv 必须装到 /usr/local/bin —— 装 ~/.local/bin 在 ProtectHome 下同样不可见
#   * 服务必须非 root 运行
#   * 幂等：重复执行不报错；**已存在的 .env 绝不覆盖**（覆盖等于把线上密钥冲掉）
#
# 排障提示：语法检查用 `bash -n deploy/install.sh`，看会做什么用 `--dry-run`。
#   **不要**用 `bash -x` 跑真安装 —— xtrace 会把生成的 API Key 打进终端和日志。
#
# 退出码：0 成功；1 某一步失败（会打印失败步骤 + 手动回退命令）；2 未知参数；
#         3 = 安装完成但服务未就绪（可能仍在下载模型）—— 不要重装，
#             先按收尾提示看 journalctl；CI/Ansible 必须把 3 与 0 区别对待
# =============================================================================
set -Eeuo pipefail

# ---------------------------------------------------------------------------
# 0. 常量与外观
# ---------------------------------------------------------------------------
readonly SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
# 脚本必须放在项目的 deploy/ 下：靠它反推项目源目录，避免用 cwd 猜
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SRC_DIR="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
readonly UNIT_NAME="imgtrans"
readonly UNIT_PATH="/etc/systemd/system/${UNIT_NAME}.service"

# 有 tty 才上色；否则日志重定向到文件时会带一堆 ESC 序列
c_red=""; c_green=""; c_yellow=""; c_cyan=""; c_reset=""
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    c_red=$'\033[31m'; c_green=$'\033[32m'
    c_yellow=$'\033[33m'; c_cyan=$'\033[36m'; c_reset=$'\033[0m'
fi

# ---------------------------------------------------------------------------
# 1. 默认参数（全部可用命令行覆盖）
# ---------------------------------------------------------------------------
INSTALL_DIR="/opt/imgtrans"
PORT="8000"
RUN_USER="imgtrans"
ENABLE_GPU=0
ENABLE_NGINX=1
DRY_RUN=0

# 运行期派生出来的值
RUN_GROUP=""
EFFECTIVE_PORT=""
API_KEY=""
PY_SPEC=""          # 传给 uv sync --python 的版本号，如 3.13
UV_PYTHON_DIR=""    # 用 uv 托管解释器时的安装目录（必须落在安装目录内，见下）
UV_CACHE=""
ENV_FILE=""
KEY_FILE=""         # 密钥留档（root:root 600），供非 tty 安装后取用
CURRENT_STEP="启动"
ORIG_ARGS=""        # 原始命令行参数，用于报错时给出可直接复制的重跑命令

# 归属标记：决定失败回退指引里是否敢写 rm -rf / userdel。
# 只有"本次确实是我们创建的"才允许提示删除；否则可能删掉别人的站点/账号。
CREATED_DIR=0
CREATED_USER=0
SERVICE_NOT_READY=0 # 服务 60/300s 内没通过 /healthz（可能仍在下载模型），收尾要显著提示

log()  { printf '%s[%s]%s %s\n' "${c_cyan}" "$(date +%H:%M:%S)" "${c_reset}" "$*"; }
warn() { printf '%s[警告]%s %s\n' "${c_yellow}" "${c_reset}" "$*" >&2; }
ok()   { printf '%s[完成]%s %s\n' "${c_green}" "${c_reset}" "$*"; }
err()  { printf '%s[失败]%s %s\n' "${c_red}" "${c_reset}" "$*" >&2; }

die() { err "$*"; exit 1; }

usage() {
    cat <<'USAGE'
imgtrans Ubuntu 部署脚本

  sudo bash deploy/install.sh [选项]

选项：
  --dir <目录>     安装目录（默认 /opt/imgtrans）。绝对路径；不能是系统目录或其子路径
                   （/etc、/usr、/root、/home、/tmp…），也不能是共享目录本身
                   （/opt、/var/lib、/www/wwwroot…要再给一层）。可以位于
                   /opt、/srv、/www/wwwroot、/data、/var/www、/mnt/data 等业务目录下。
                   不能位于源码目录内，也不能是已存在且非空的非本项目目录。
  --port <端口>    服务监听端口（默认 8000；若已存在 .env 则以 .env 里的为准）
  --user <用户>    运行服务的系统用户（默认 imgtrans，自动创建；
                   匹配 ^[a-z_][a-z0-9_-]*$，不能是 root）
  --gpu            安装 GPU 版 paddle（--extra gpu，cu129 源，要求驱动 >= 570；
                   RTX 50 系列/Blackwell 也能用，cu126 的 wheel 没有 sm_120 kernel）
  --no-nginx       不安装/不配置 nginx（自行反代时用；注意 client_max_body_size 与超时）
  --dry-run        只打印将执行的动作，不修改系统（无需 root）
  -h, --help       显示本帮助

幂等性：可重复执行。已存在的 .env 不会被覆盖，已存在的 venv 只做 uv sync。
USAGE
}

# ---------------------------------------------------------------------------
# 2. 命令执行封装（--dry-run 时只打印）
# ---------------------------------------------------------------------------
# 用 printf '%q' 给含空格/特殊字符的路径加引号，拼出来的命令可以原样回放
q() { printf '%q' "$1"; }

# sed 替换串转义：路径里出现 | & \ 时，不转义会让 sed 报错
# （`>` 已经把目标文件截断了 → 单元文件变 0 字节），或静默产出 a@PLACEHOLDER@b。
esc() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }

run() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '[dry-run] %s\n' "$*"
        return 0
    fi
    printf '[exec] %s\n' "$*"
    "$@"
}

# 需要管道或重定向时用这个（写文件、tar 管道等）
run_sh() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '[dry-run] %s\n' "$*"
        return 0
    fi
    printf '[exec] %s\n' "$*"
    bash -c "$*"
}

# 以运行用户身份执行。用 runuser（util-linux 自带）而不是 sudo：
# 精简镜像里常常没有 sudo，而 util-linux 一定存在。
as_user() {
    # PATH 显式给定：runuser 继承的 PATH 里不一定有 /usr/local/bin（uv 装在那儿）
    #
    # HOME 不能用 INSTALL_DIR：uv 拿 HOME 去推 $HOME/.local/bin 放 python 可执行链接，
    # 而安装目录顶层是 root:组 + go-w，imgtrans 建不了子目录，于是
    #   warning: Failed to create directory `/www/.../.local/bin`: Permission denied
    # 只是 warning 但每次 sync 都刷一遍，且哪个 uv 版本改成硬错误就是一次线上失败。
    # 指到 .tools/home（属主是运行用户）后，uv 所有落到 HOME 的东西都在可写区内，
    # 也不会往项目根目录里丢 .local/。
    #
    # -u LD_LIBRARY_PATH 是硬要求，不是洁癖：runuser 不清环境，root shell 里一句
    # export LD_LIBRARY_PATH=/usr/local/cuda/lib64 就会带进来，而那个目录下的
    # compat/libcuda.so.1 是 forward-compat 垫片，排在宿主驱动前面就会报
    #   CUDA error(803) CUDA_ERROR_SYSTEM_DRIVER_MISMATCH
    # 表现是 nvidia-smi 正常、llama-server 也正常，唯独 paddle 的 device_count=0。
    # 实测：清掉后 device_count 从 0 变 1，加载回 /usr/lib/x86_64-linux-gnu/libcuda.so.590.48.01。
    # venv 里所有库都是 pip 装的（自带 rpath），不依赖系统的 LD_LIBRARY_PATH。
    run runuser -u "${RUN_USER}" -- env -u LD_LIBRARY_PATH \
        "HOME=${UV_HOME}" \
        "UV_CACHE_DIR=${UV_CACHE}" \
        "UV_PYTHON_BIN_DIR=${UV_BIN_DIR}" \
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
        ${UV_PYTHON_DIR:+"UV_PYTHON_INSTALL_DIR=${UV_PYTHON_DIR}"} \
        "$@"
}

# ---------------------------------------------------------------------------
# 3. 失败处理：说清哪一步炸了、怎么手动回退
# ---------------------------------------------------------------------------
# 注意：下面的 rm -rf / userdel **只在本次确实由我们创建时**才输出。
# 否则 `--dir /var/www/html`（白名单合法）+ 安装失败，会提示运维去删掉别人的站点；
# `--user www-data` 失败则提示 userdel，把 nginx/php 的运行账号删掉。
on_err() {
    local line="$1"
    local code="$2"
    echo >&2
    err "第 ${line} 行执行失败（退出码 ${code}），停在此步骤：${CURRENT_STEP}"

    {
        printf '\n手动回退指引（按需执行，均不会影响已有的其它服务）：\n'
        printf '  1) 停服务（若已安装单元）：\n'
        printf '       systemctl disable --now %s 2>/dev/null || true\n' "${UNIT_NAME}"
        printf '       rm -f %s\n' "${UNIT_PATH}"
        printf '       systemctl daemon-reload\n'
        printf '  2) 撤销 nginx 站点（若已安装）：\n'
        printf '       rm -f /etc/nginx/sites-enabled/%s.conf\n' "${UNIT_NAME}"
        printf '       rm -f /etc/nginx/sites-available/%s.conf\n' "${UNIT_NAME}"
        printf '       nginx -t && systemctl reload nginx\n'

        if [ "${CREATED_DIR}" = "1" ]; then
            printf '  3) 删安装目录（本次由脚本创建，可整目录删除；先备份 .env！）：\n'
            printf '       cp %q /root/imgtrans.env.bak 2>/dev/null || true\n' "${ENV_FILE}"
            printf '       rm -rf %q\n' "${INSTALL_DIR}"
        else
            printf '  3) 安装目录 %q 在本次执行前就已存在，**不要**整目录删除：\n' "${INSTALL_DIR}"
            printf '       请手动清理本次新增的 .venv / .models / .tools 与 .env\n'
        fi

        if [ "${CREATED_USER}" = "1" ]; then
            printf '  4) 删运行用户（本次由脚本创建）：\n'
            printf '       userdel %q\n' "${RUN_USER}"
        else
            printf '  4) 运行用户 %q 在本次执行前就已存在，**不要** userdel（可能有其它服务在用）\n' "${RUN_USER}"
        fi

        printf '  5) 系统包（libgomp1 / 字体）可以不删：它们是通用运行时，其它程序也在用。\n'
        printf '\n排查完可直接重跑本脚本，它是幂等的。\n'
    } >&2

    exit 1
}
trap 'on_err "${LINENO}" "$?"' ERR

# ---------------------------------------------------------------------------
# 4. 参数解析
# ---------------------------------------------------------------------------
while [ "$#" -gt 0 ]; do
    case "$1" in
        --dir)
            [ "$#" -ge 2 ] || die "--dir 需要一个参数"
            INSTALL_DIR="$2"; shift 2 ;;
        --port)
            [ "$#" -ge 2 ] || die "--port 需要一个参数"
            PORT="$2"; shift 2 ;;
        --user)
            [ "$#" -ge 2 ] || die "--user 需要一个参数"
            RUN_USER="$2"; shift 2 ;;
        --gpu)
            ENABLE_GPU=1; shift ;;
        --no-nginx)
            ENABLE_NGINX=0; shift ;;
        --dry-run)
            DRY_RUN=1; shift ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            err "未知参数：$1"; usage; exit 2 ;;
    esac
done

# 派生路径。INSTALL_DIR 在 preflight 里会被 realpath 归一化，所以必须做成函数、
# 归一化之后再调一次：否则 .env / uv 缓存会按旧路径去写（很容易漏）。
derive_paths() {
    ENV_FILE="${INSTALL_DIR}/.env"
    KEY_FILE="${INSTALL_DIR}/.env.key"
    UV_CACHE="${INSTALL_DIR}/.tools/uvcache"
    UV_PYTHON_DIR="${INSTALL_DIR}/.tools/python"
    # 降权执行时的 HOME。必须是运行用户可写、且不在 /home 或 /root 下
    # （systemd 的 ProtectHome=yes 会让服务看不见那两个目录）。
    UV_HOME="${INSTALL_DIR}/.tools/home"
    # uv 把 python3.x 可执行链接放这里，默认是 $HOME/.local/bin
    UV_BIN_DIR="${INSTALL_DIR}/.tools/bin"
}

# 去掉结尾斜杠，避免拼出 //opt/imgtrans 这种路径
INSTALL_DIR="${INSTALL_DIR%/}"
RUN_GROUP="${RUN_USER}"
derive_paths

# uv 的唯一权威位置。
#
# 为什么写死绝对路径而不是用 `uv`：降权执行走的 PATH 是固定白名单
# （见 as_user），只含 /usr/local/bin 等系统目录。一旦 uv 装在
# /root/.local/bin 之类的地方，root 交互 shell 能 `command -v uv` 找到、
# 而 imgtrans 用户执行时就是 "env: 'uv': No such file or directory"（退出码 127）。
# 把位置收敛成一个变量，检查与执行才是同一个判据。
UV_BIN="${UV_BIN:-/usr/local/bin/uv}"

# ===========================================================================
# GPU 预检（--gpu 时在下载任何东西之前跑）
# ===========================================================================
#
# 为什么必须提前查驱动：cu129 的 wheel 要求能跑 CUDA 12.9 的驱动（>= 570）。
# 驱动不够时**安装不会失败** —— import 正常、is_compiled_with_cuda() 也返回
# True，直到真正推理才抛 "no kernel image is available for execution on the
# device"，而那句话和"我把 GPU 配错了"看起来毫无关系。等 2.9GB 下完再发现，
# 成本是一轮 20 分钟的错误排查。
readonly GPU_MIN_DRIVER_MAJOR=570

check_gpu_preflight() {
    if ! command -v nvidia-smi >/dev/null 2>&1; then
        die "--gpu 需要 NVIDIA 驱动，但没找到 nvidia-smi。
  * 装驱动：ubuntu-drivers install（或从 NVIDIA 官网装 >= ${GPU_MIN_DRIVER_MAJOR} 的版本）
  * 本来就没有卡 / 不打算用卡：去掉 --gpu，按 CPU 部署"
    fi

    local drv
    drv="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null \
        | head -n 1 | tr -d '[:space:]' || true)"
    if [ -z "${drv}" ]; then
        die "nvidia-smi 存在但读不到驱动版本。先单独跑一次 nvidia-smi 看它自己的报错
  （最常见：内核升级后没重启，驱动模块没加载）。"
    fi

    local major="${drv%%.*}"
    case "${major}" in
        ''|*[!0-9]*)
            warn "驱动版本无法解析（${drv}），跳过版本检查" ;;
        *)
            if [ "${major}" -lt "${GPU_MIN_DRIVER_MAJOR}" ]; then
                die "驱动 ${drv} 低于 cu129 要求的 ${GPU_MIN_DRIVER_MAJOR}+。
  装完不会报错，但第一次推理会抛 "no kernel image is available for execution
  on the device" —— 这个报错和驱动版本看起来毫无关系，极难联想到一起。
  处理：升级 NVIDIA 驱动后重跑本脚本；或去掉 --gpu 用 CPU 部署。"
            fi
            log "NVIDIA 驱动 ${drv}（>= ${GPU_MIN_DRIVER_MAJOR}，满足 cu129）"
            ;;
    esac

    # 记录型号就够了：源已固定 cu129，sm_100/sm_120（Blackwell）的 kernel 在里面。
    # 这里不做拦截判断，只让日志能回答"这台机器到底是什么卡"。
    local gpu_name
    gpu_name="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null \
        | head -n 1 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' || true)"
    [ -n "${gpu_name}" ] && log "GPU：${gpu_name}"

    # 磁盘：GPU 版 wheel 2.9GB + 解压出的 site-packages + uv 缓存（在安装目录内，
    # 见 derive_paths 的 UV_CACHE）。实测 CPU 版约 2GB、GPU 版 10GB 起跳。
    # 不查的话，uv sync 会在下载到一半时 "No space left on device"，
    # 而报错指向的是 uv 的临时目录，看不出是磁盘不够。
    local need_mb=10240 avail_mb
    avail_mb="$(df -Pm "$(dirname "${INSTALL_DIR}")" 2>/dev/null | awk 'NR==2 {print $4}' || echo 0)"
    if [ "${avail_mb:-0}" -gt 0 ] && [ "${avail_mb}" -lt "${need_mb}" ]; then
        warn "目标分区可用 ${avail_mb}MB，GPU 版建议 >= ${need_mb}MB（wheel 2.9GB + 解压 + uv 缓存）。
  空间不足最可能的表现是下载中途失败，而不是一开始就报错。"
    fi

    # LD_LIBRARY_PATH 里带 CUDA 路径是 GPU 部署最常见的"隐形地雷"：
    # apt 装的 CUDA toolkit 会在 /usr/local/cuda*/compat 放一份 forward-compat
    # 的 libcuda.so.1，它排在宿主驱动前面时 CUDA 初始化直接报
    #   CUDA error(803) CUDA_ERROR_SYSTEM_DRIVER_MISMATCH
    # 症状极具迷惑性：nvidia-smi 正常、llama-server 之类也正常，
    # 唯独 paddle 的 device_count=0，看起来像"paddle 坏了"。
    # 这里只提示不自动改：这个变量可能被同机其它 GPU 服务依赖，动错影响面更大。
    # 本脚本的 as_user 与生成的 systemd 单元都已显式清空它。
    case "${LD_LIBRARY_PATH:-}" in
        *cuda*|*CUDA*)
            warn "当前 shell 的 LD_LIBRARY_PATH 含有 CUDA 路径：${LD_LIBRARY_PATH}"
            warn "  它可能让 paddle 加载 compat 版 libcuda，表现为 device_count=0 / CUDA error(803)。"
            warn "  本脚本与服务单元已显式清空该变量；但你手动跑 python 调试时仍会踩到。"
            warn "  定位并删掉那一行：grep -rn 'LD_LIBRARY_PATH' /etc/profile.d/ ~/.bashrc /etc/environment"
            ;;
    esac
}

# 找出是谁把 LD_LIBRARY_PATH 注进了子进程环境。
#
# 为什么值得单独写一个函数：803 那轮排查里，父 shell 里 `echo $LD_LIBRARY_PATH`
# 是空的，子进程却带着 CUDA 路径 —— 变量是 runuser 走 PAM 时由 /etc/environment
# 注入的。只 grep ~/.bashrc 永远找不到，于是"看起来没有污染，但就是 803"。
# 所以这里把所有可能的位置一次列全，直接给出 file:line 让用户去删。
ld_library_path_sources() {
    local files=() f
    for f in /etc/environment /etc/security/pam_env.conf /etc/profile /etc/bash.bashrc; do
        [ -f "${f}" ] && files+=("${f}")
    done
    for f in /etc/profile.d/*.sh; do
        [ -f "${f}" ] && files+=("${f}")
    done
    # 调用者的 shell 初始化文件也要查：脚本继承的是"谁启动它"的环境。
    # 实测出现过 /etc 下全部干净、但父 shell 里带着 /usr/local/cuda/lib64 的情况，
    # 那时只查 /etc 会得出"没有注入源"的结论，反而更迷惑。
    for f in "${HOME:-/root}/.bashrc" "${HOME:-/root}/.bash_profile" "${HOME:-/root}/.profile" \
             /root/.bashrc /root/.profile; do
        [ -f "${f}" ] && files+=("${f}")
    done
    if [ "${#files[@]}" -eq 0 ]; then
        return 0
    fi
    # 过滤掉注释行：注释掉的 export 不生效，列出来只会增加噪音。
    # 同一文件可能被重复加入（例如 HOME=/root），sort -u 去重。
    grep -Hn 'LD_LIBRARY_PATH' "${files[@]}" 2>/dev/null \
        | grep -v ':[0-9]*:[[:space:]]*#' \
        | sort -u \
        || true
}

# ===========================================================================
# 步骤 1：前置条件
# ===========================================================================
step_preflight() {
    CURRENT_STEP="前置条件检查"
    log "${CURRENT_STEP}"

    # root 检查：写 /etc、useradd、systemctl 都需要 root。--dry-run 只读，放行。
    if [ "${DRY_RUN}" -eq 0 ] && [ "${EUID}" -ne 0 ]; then
        die "需要 root 权限。请用：sudo bash $(q "${SCRIPT_DIR}/${SCRIPT_NAME}") ${ORIG_ARGS}"
    fi

    # 只支持 Ubuntu（Debian 大概率也行，但 fonts-noto-* 包名需确认）
    [ -r /etc/os-release ] || die "读不到 /etc/os-release，无法识别发行版"
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}" in
        ubuntu) ;;
        debian)
            warn "检测到 Debian（ID=${ID}）。脚本按 Ubuntu 写的，Debian 12+ 一般也可用，请留意 apt 包名。" ;;
        *)
            die "本脚本只支持 Ubuntu / Debian（当前 ID=${ID:-未知}）。Alpine 不可用：paddle 只有 glibc wheel。" ;;
    esac
    log "发行版：${PRETTY_NAME:-${ID}} / $(uname -m)"

    # ---- 安装目录校验（必须在任何 mkdir / chown / tar 之前）----------------
    # 做四件事：① 空/相对路径先拒 ② 归一化（吃掉 /etc/. 、/opt/../etc 这类等价写法）
    #          ③ 拦系统目录 ④ 拦"装在源码目录里/源码在它里面"
    #
    # 为什么必须归一化：安装过程会对 INSTALL_DIR 做 `chown -R` 和 tar 解包。
    # 若 --dir 传了 /etc/. ，等价于把 /etc 整棵树 chown 给运行用户组
    # （/etc/shadow 的属组变 imgtrans → 服务账号可读全部密码哈希）；传 /var/lib
    # 会改掉 mysql/docker/apt 的 state 目录属组。这类误伤不可逆。
    #
    # 为什么用黑名单而不是白名单：真实部署目录五花八门 —— 宝塔的 /www/wwwroot、
    # 云主机的数据盘 /data、Debian 系网站根 /var/www、挂载盘 /mnt/data。
    # 白名单会把它们连同用户的意图一起拒掉，而报错会让人以为脚本坏了。
    # 归一化之后 /etc/. 与 /opt/../etc 都变成 /etc，靠"目录及其所有子路径"命中即可，
    # 原来的绕过面自然消失。
    case "${INSTALL_DIR}" in
        "") die "--dir 不能为空" ;;
        /*) ;;
        *) die "--dir 必须是绝对路径（当前：${INSTALL_DIR}）" ;;
    esac

    local dir_norm
    if command -v realpath >/dev/null 2>&1; then
        # -m：不要求路径存在，纯词法归一化（末尾组件通常还不存在）
        dir_norm="$(realpath -m -- "${INSTALL_DIR}")" || die "无法归一化路径：${INSTALL_DIR}"
    else
        dir_norm="$(readlink -m -- "${INSTALL_DIR}")" || die "无法归一化路径：${INSTALL_DIR}"
    fi
    log "安装目录解析为：${dir_norm}"

    # ---- ① 归一化后立刻拦 /home 与 /root ----
    # 这两处必须单独说清理由：不是权限问题，而是 systemd 单元的 ProtectHome=yes 会让
    # 服务进程完全看不到 /home、/root。装在这里会得到"能 uv sync、启动即崩、日志说
    # 找不到 .venv/bin/python"的静默故障 —— 本项目实测踩过，排查成本极高。
    case "${dir_norm}" in
        /home|/root)
            die "安装目录不能是 ${dir_norm}：systemd 单元的 ProtectHome=yes 会屏蔽该目录，服务读不到自己的 .venv。
  请用 /opt、/srv、/www/wwwroot、/var/www、/data 这类业务目录。" ;;
        /home/*|/root/*)
            die "安装目录不能位于 ${dir_norm} 下：不是权限问题，而是单元的 ProtectHome=yes 会让服务
  看不到 /home 与 /root —— 表现为「启动即崩、日志说找不到 .venv/bin/python」，极难排查。
  请用 /opt、/srv、/www/wwwroot、/var/www、/data 这类业务目录。" ;;
    esac

    # ---- ② 系统目录本身、及其所有子路径 ----
    # 用黑名单而不是白名单：宝塔的 /www/wwwroot、Debian 的 /var/www、云主机的 /data
    # 都是合法安装位置，白名单会把它们连同用户意图一起误杀，报错还让人以为脚本坏了。
    # 归一化已吃掉 /etc/. 、/opt/../etc 这类等价写法，所以"目录本身 + 所有子路径"够用。
    case "${dir_norm}" in
        /|/bin|/sbin|/lib|/lib32|/lib64|/libx32|/usr|/etc|/boot|/proc|/sys|/dev|/run|/tmp)
            die "安装目录不能是 ${dir_norm}（系统目录）" ;;
        /bin/*|/sbin/*|/lib/*|/lib32/*|/lib64/*|/libx32/*|/usr/*|/etc/*|/boot/*|/proc/*|/sys/*|/dev/*|/run/*|/tmp/*)
            die "安装目录不能位于 ${dir_norm} 下（系统目录，会破坏系统）。
  请用 /opt、/srv、/www/wwwroot、/var/www、/data 这类业务目录下的子路径" ;;
    esac

    # ---- ③ 共享"容器"目录本身 ----
    # 它们的子目录必须允许（/www/wwwroot/ocr、/var/lib/imgtrans 都是合法位置），
    # 但目录本身不能当安装目录：chown -R / tar 会扫到别人的站点与数据
    # （/var/lib 下有 mysql/docker/apt 的 state）。
    case "${dir_norm}" in
        /opt|/srv|/data|/mnt|/var|/var/lib|/var/local|/var/opt|/var/www|/var/log|/var/cache|/var/tmp|/var/spool|/var/mail|/var/backups|/www|/www/wwwroot)
            die "${dir_norm} 是被多个服务共用的目录，不能整体作为安装目录（chown -R 会波及其它站点/数据）。
  请改用它的子目录，例如 ${dir_norm}/imgtrans。" ;;
    esac

    # ---- ④ 源码目录关系 ----
    # 注意：下面这个 if **不是安全豁免** —— 黑名单 ①②③ 在上面已经无条件跑完了，
    # 走到这里说明 dir_norm 已经是允许的位置。这个分支只决定"要不要拷贝文件"：
    # 原地部署（clone 到 /opt/imgtrans 再 --dir /opt/imgtrans）时不必自己拷给自己。
    # 不能把它删掉合并进下面的 case：模式 "${SRC_DIR}/"* 的 `*` 可匹配空串，
    # 相等的情况会被误判成"位于源码目录内"，原地部署直接失败。
    if [ "${dir_norm}" = "${SRC_DIR}" ]; then
        log "安装目录就是源码目录（原地部署），跳过文件拷贝"
    else
        case "${dir_norm}/" in
            "${SRC_DIR}/"*) die "${dir_norm} 位于源码目录内，tar 会边读边写把磁盘打满" ;;
        esac
        case "${SRC_DIR}/" in
            "${dir_norm}/"*) die "源码目录位于 ${dir_norm} 内，安装会覆盖/污染源码，请换一个目录" ;;
        esac
        # 已存在且非空、但不像本项目 → 极可能是别人的站点/数据目录，绝不覆盖
        if [ -d "${dir_norm}" ] && [ -n "$(ls -A "${dir_norm}" 2>/dev/null)" ] \
            && [ ! -f "${dir_norm}/pyproject.toml" ]; then
            die "${dir_norm} 已存在且非空，但不含 pyproject.toml（不是 imgtrans 项目）。
  为避免覆盖已有站点/数据，请换一个空目录，或确认后手动清空再重试。"
        fi
    fi

    local dir_changed=0
    if [ "${dir_norm}" != "${INSTALL_DIR}" ]; then
        dir_changed=1
        log "安装目录归一化：${INSTALL_DIR} → ${dir_norm}"
    fi
    INSTALL_DIR="${dir_norm}"
    # 派生变量全部重算，否则 .env / uv 缓存仍指向归一化前的旧路径
    derive_paths
    if [ "${dir_changed}" = "1" ]; then
        log "已同步派生路径：ENV_FILE=${ENV_FILE} UV_CACHE=${UV_CACHE}"
    fi

    # 端口必须是数字
    case "${PORT}" in
        ''|*[!0-9]*) die "--port 必须是数字（当前：${PORT}）" ;;
    esac
    [ "${PORT}" -ge 1 ] && [ "${PORT}" -le 65535 ] || die "--port 超出范围 1-65535（当前：${PORT}）"

    # 不能以 root 跑服务：paddle 解析不可信图片，root 跑等于把整机押上
    if [ "${RUN_USER}" = "root" ]; then
        die "运行用户不能是 root（服务要处理不可信图片，必须降权）。去掉 --user root 即可。"
    fi
    # 完整用户名规则，而不是只看首字符：`www-data` / `ubuntu` 这类名字能过首字符校验，
    # 但它们要么是 nginx/php 的运行账号、要么是能 sudo 的登录用户，都不该拿来跑本服务。
    if ! printf '%s' "${RUN_USER}" | grep -Eq '^[a-z_][a-z0-9_-]{0,31}$'; then
        die "--user 不是合法用户名（需匹配 ^[a-z_][a-z0-9_-]*$，当前：${RUN_USER}）"
    fi

    # 源目录校验：脚本必须待在项目里
    [ -f "${SRC_DIR}/pyproject.toml" ] || die "找不到 ${SRC_DIR}/pyproject.toml —— 本脚本必须放在项目的 deploy/ 子目录下"
    [ -f "${SRC_DIR}/uv.lock" ] || die "找不到 ${SRC_DIR}/uv.lock —— 没有 lock 就无法用 --frozen 复现环境，请先在项目根执行 uv lock"
    [ -d "${SRC_DIR}/imgtrans" ] || die "找不到 ${SRC_DIR}/imgtrans 包目录"

    # 端口占用检查（不阻断：可能就是想复用同一个端口给已有实例）
    if command -v ss >/dev/null 2>&1; then
        if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}\$"; then
            warn "端口 ${PORT} 已被占用（ss 显示 LISTEN）。若已有 imgtrans 在跑，本次会做增量同步并重启。"
        fi
    fi

    # 内存提示：单进程常驻约 500MB~1GB，2GB 机器能跑但会很紧
    local mem_mb
    mem_mb="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)"
    if [ "${mem_mb}" -gt 0 ] && [ "${mem_mb}" -lt 1900 ]; then
        warn "内存仅 ${mem_mb}MB。OCR 常驻约 500MB~1GB，建议 >=2GB（推荐 4GB），否则易 OOM。"
    fi

    # --gpu 的驱动/磁盘检查放在这里：必须在 step_files（拷文件）与 step_sync
    # （下 2.9GB）之前，否则就失去"提前发现"的意义。
    if [ "${ENABLE_GPU}" -eq 1 ]; then
        check_gpu_preflight
    fi

    ok "前置条件全部满足"
}

# ===========================================================================
# 步骤 2：系统依赖（apt）
# ===========================================================================
step_apt() {
    CURRENT_STEP="安装系统依赖"
    log "${CURRENT_STEP}"

    # --no-install-recommends：不拉图形字体/文档，省几十 MB
    # 包清单每一项都是硬需求，缺了会静默出错（字体）或直接崩（libgomp1）
    local -a packages=(
        libgomp1                # paddle 的 OpenMP 运行时，缺了 import paddle 直接失败
        fonts-noto-cjk          # 中日韩：缺了全变方块 □，且不报错
        fonts-noto-core         # 阿拉伯/希伯来/泰/天城/孟加拉/格鲁吉亚
        fonts-dejavu-core       # 拉丁兜底（西里尔/希腊也靠它）
        fonts-liberation       # 可选：=Arial 度量兼容，排版宽度更接近原图
        curl ca-certificates    # 装 uv、以及服务端下载 http(s) 图片时的 CA
        tar                     # 拷贝项目文件用（精简镜像可能没有）
    )

    # DEBIAN_FRONTEND：避免任何 debconf 交互；非交互环境下必加
    # DPkg::Lock::Timeout：Ubuntu 默认开 unattended-upgrades，撞锁时等一会儿就好，
    #   不设的话一次可自愈的竞争会被放大成"安装失败"。
    #
    # 为什么 update 失败要降级而不是 die：这机器上只要装过 CUDA / cuDNN /
    # Docker / 宝塔，就一定带第三方源；其中任何一个被移除或过期，update 都会
    # 返回 100，典型报错是
    #   "The repository 'file:/var/cudnn-local-repo-ubuntu2204-8.9.7.29 Release'
    #    no longer has a Release file"
    # 而要装的 libgomp1 与三个字体包全在 Ubuntu 官方源里 —— 一个与本次安装
    # 无关的坏源不该让整台机器的部署中断。真正的判据交给 apt-get install：
    # 真缺包时它会明确报 "Unable to locate package xxx"，比"某个源坏了"精确。
    if ! run env DEBIAN_FRONTEND=noninteractive \
        apt-get -o DPkg::Lock::Timeout=120 update -y; then
        warn "apt-get update 报错（退出码非 0），已忽略并继续安装。"
        # 把可疑的本地源点出来。不点的话用户得从几十行输出里自己找那一行 E:，
        # 而"file:/var/cudnn-local-repo-..."这句话本身不说明该怎么修。
        local broken_src
        broken_src="$(grep -rlE 'cudnn-local|cuda-local|/var/cudnn|/var/cuda' \
            /etc/apt/sources.list.d/ 2>/dev/null || true)"
        if [ -n "${broken_src}" ]; then
            warn "以下是可能失效的本地源（多为 CUDA/cuDNN 安装包的残留配置）："
            printf '%s\n' "${broken_src}" | sed 's/^/    /' >&2
            warn "它们指向的 .deb 被清掉后会一直报 'no longer has a Release file'。"
            warn "本项目不需要系统 cuDNN（paddle 用 pip 装的 nvidia-cudnn-cu12），可安全移除："
            printf '%s\n' "${broken_src}" | sed 's/^/    rm -f /' >&2
            warn "删掉后重跑本脚本即可。"
        fi
        warn "若下面的 apt-get install 报 'Unable to locate package'，说明官方源索引也没成功，先修源。"
    fi
    run env DEBIAN_FRONTEND=noninteractive \
        apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends "${packages[@]}"

    # 显式验证 libgomp1：这是最容易漏、报错最难读的一项。
    #
    # 不能用 `ldconfig -p | grep` 当唯一判据，它有两个与"库到底在不在"无关的
    # 失败方式：
    #   ① ldconfig 本身不在 PATH 里（它在 /usr/sbin；被裁过的镜像、
    #      或 PATH 被宝塔之类的面板改过时找不到）→ 命令失败，grep 无输入
    #   ② 包刚装上，但 postinst 触发的 ldconfig 没跑到（安装被中断、
    #      容器里没有 ldconfig），缓存里就还没有它
    # 这两种情况下文件明明在位，paddle 也能 dlopen 到，却会被判成"没装上"。
    # 所以主判据用 dpkg 定位真实文件，ldconfig 只作辅助，缺缓存就顺手重建。
    if [ "${DRY_RUN}" -eq 0 ]; then
        local gomp
        gomp="$(dpkg -L libgomp1 2>/dev/null \
            | grep -E '/libgomp\.so\.1(\.[0-9]+)*$' | head -n 1 || true)"

        if [ -z "${gomp}" ] || [ ! -e "${gomp}" ]; then
            # 两种可能：包没装上（dpkg -L 为空），或 dpkg 记录与实际文件不一致
            local st
            st="$(dpkg-query -W -f='${Status}' libgomp1 2>/dev/null || echo '未安装')"
            die "找不到 libgomp.so.1，paddle 会启动即失败（cannot open shared object file）。
  当前 dpkg 状态：${st}
  处理：apt-get install --reinstall -y libgomp1
  若仍失败，看 apt 是否被第三方源挡住（apt-get update 的输出）。"
        fi

        # ldconfig 通常在 /usr/sbin，非 root 交互式 shell 的 PATH 里常没有它
        # （sudo 会补，但宝塔/自建 systemd 环境不一定）。先认绝对路径，
        # 否则 `command -v ldconfig` 会失败，把"PATH 里没有"误报成"系统没装"。
        local ldc=""
        if [ -x /usr/sbin/ldconfig ]; then
            ldc=/usr/sbin/ldconfig
        elif [ -x /sbin/ldconfig ]; then
            ldc=/sbin/ldconfig
        elif command -v ldconfig >/dev/null 2>&1; then
            ldc="$(command -v ldconfig)"
        fi

        if [ -n "${ldc}" ]; then
            if ! "${ldc}" -p 2>/dev/null | grep -q 'libgomp\.so\.1'; then
                warn "ldconfig 缓存里还没有 libgomp.so.1（文件在 ${gomp}），重建缓存"
                "${ldc}" || warn "ldconfig 执行失败；文件在位，动态链接器一般仍能从默认路径找到它"
            fi
        else
            warn "找不到 ldconfig（libc-bin 可能被裁过），跳过缓存检查；文件在位即可正常 dlopen"
        fi

        log "libgomp1 就位：${gomp}"
    fi

    ok "系统依赖已安装（字体缺失不报错、只会画方块，稍后 doctor.py 会逐语言复核）"
}

# ===========================================================================
# 步骤 3：创建运行用户
# ===========================================================================
step_user() {
    CURRENT_STEP="创建运行用户"
    log "${CURRENT_STEP}"

    # 用 id 而不是 getent：getent 属于 libc-bin，极简系统上可能没有；id 一定有
    if id -u "${RUN_USER}" >/dev/null 2>&1; then
        log "用户 ${RUN_USER} 已存在，跳过创建（幂等）"
        # 已存在的账号可能正被别的服务使用（www-data/nginx/mysql 之类），
        # 也可能是个能登录的普通用户。命中时只警告，因为 --user 是运维显式指定的。
        warn "用户 ${RUN_USER} 是已存在的账号，请确认它不承担其它职责"
        warn "（若它属于 nginx/php/数据库，本服务与它们共用身份，日志与权限都会混在一起）"
    else
        # --system：不建家目录在 /home 下、UID < 1000
        # --no-create-home + HOME 指向安装目录：uv / paddle 都会往 HOME 写缓存，
        #   给个真实可写目录比 /nonexistent 稳（systemd 单元里也显式设了 HOME）
        # --shell nologin：这个账号只用来跑服务，不该能登录
        run useradd \
            --system \
            --no-create-home \
            --home-dir "${INSTALL_DIR}" \
            --shell /usr/sbin/nologin \
            --comment "imgtrans 图片翻译服务" \
            "${RUN_USER}"
        CREATED_USER=1
    fi

    # 组名可能与用户名不同（比如用户是 ocr，主组是 nogroup），从 passwd 里取准确的
    if [ "${DRY_RUN}" -eq 0 ]; then
        RUN_GROUP="$(id -gn "${RUN_USER}")"
    else
        RUN_GROUP="${RUN_USER}"
    fi
    log "运行用户：${RUN_USER}:${RUN_GROUP}"

    ok "运行用户已就绪"
}

# ===========================================================================
# 步骤 4：安装 uv
# ===========================================================================
step_uv() {
    CURRENT_STEP="安装 uv"
    log "${CURRENT_STEP}"

    # 判据必须是「降权执行时能不能用」，而不是「root 现在能不能用」。
    # 这两者不等价：root 的 PATH 常含 /root/.local/bin、/root/.cargo/bin，
    # 而 as_user 用的是固定 PATH。按 `command -v uv` 判会漏掉这种情况，
    # 表现为检查通过、跑到 uv sync 才报 "env: 'uv': No such file or directory"。
    if [ -x "${UV_BIN}" ]; then
        log "uv 已就位：${UV_BIN}（$("${UV_BIN}" --version 2>/dev/null || echo '版本未知')），跳过安装（幂等）"
        ok "uv 已就绪"
        return 0
    fi

    # 系统里另有一份 uv（多半在 /root/.local/bin）：直接搬过来，不重复下载。
    # 必须 **复制** 不能做软链 —— /root 是 0700，imgtrans 用户连目录都进不去，
    # 软链会变成"存在但不可执行"，比没有更迷惑。
    local found
    found="$(command -v uv 2>/dev/null || true)"
    if [ -n "${found}" ] && [ -x "${found}" ]; then
        warn "uv 在 ${found}（不属于运行用户可见的 PATH），复制到 ${UV_BIN}"
        run install -m 0755 "${found}" "${UV_BIN}"
        if [ -x "${UV_BIN}" ]; then
            log "已就位：${UV_BIN}（$("${UV_BIN}" --version 2>/dev/null || echo '版本未知')）"
            ok "uv 已就绪"
            return 0
        fi
        warn "复制失败，回退到联网安装"
    fi

    # 官方安装脚本 + UV_UNMANAGED_INSTALL：
    #   * 装到 /usr/local/bin，root 装完所有用户都能用
    #     （装 ~/.local/bin 在 ProtectHome=yes 下对服务不可见）
    #   * 不去改 root 的 .bashrc/.profile（unmanaged 模式下不改 shell 配置）
    # 先下载到临时文件再执行：`curl | sh` 在 pipefail 下失败会被吞掉，
    # 而且半截脚本执行失败的现象极难排查。
    # dry-run 下不 mktemp：否则每跑一次 dry-run 就在 /tmp 留一个空文件。
    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '[dry-run] curl -fsSL -o /tmp/imgtrans-uv-install.XXXXXX.sh https://astral.sh/uv/install.sh\n'
        printf '[dry-run] env UV_UNMANAGED_INSTALL=%s bash /tmp/imgtrans-uv-install.XXXXXX.sh\n' "$(dirname "${UV_BIN}")"
        printf '[dry-run] rm -f /tmp/imgtrans-uv-install.XXXXXX.sh\n'
        ok "uv 将安装到 ${UV_BIN}"
        return 0
    fi

    local installer
    installer="$(mktemp /tmp/imgtrans-uv-install.XXXXXX.sh)"
    run curl -fsSL -o "${installer}" https://astral.sh/uv/install.sh
    run env UV_UNMANAGED_INSTALL="$(dirname "${UV_BIN}")" bash "${installer}"
    run rm -f "${installer}"

    [ -x "${UV_BIN}" ] || die "uv 安装后不在 ${UV_BIN}。若是内网环境，请手动拷 uv 二进制到 ${UV_BIN} 并 chmod 755 后重跑本脚本。"
    log "uv 版本：$("${UV_BIN}" --version)"

    ok "uv 已安装到 ${UV_BIN}"
}

# ===========================================================================
# 步骤 5：拷贝/校验项目文件
# ===========================================================================
step_files() {
    CURRENT_STEP="准备项目文件"
    log "${CURRENT_STEP}"

    # 记录"目录是不是本次创建的"：决定失败回退时敢不敢提示 rm -rf
    if [ ! -d "${INSTALL_DIR}" ]; then
        CREATED_DIR=1
    fi
    run mkdir -p "${INSTALL_DIR}"

    if [ "${SRC_DIR}" = "${INSTALL_DIR}" ]; then
        log "源目录即安装目录（${INSTALL_DIR}），跳过拷贝，只做目录/权限修补"
    else
        log "从 ${SRC_DIR} 拷到 ${INSTALL_DIR}"
        # 用 tar 而不是 rsync：tar 是 base 系统自带，rsync 精简镜像里常常没有。
        # 排除项：
        #   .venv*  每台机器各自 uv sync 生成，拷过去必然路径不对
        #   .tools  uv 缓存与托管解释器，同上
        #   .env    **刻意不拷**：开发机的 .env 里有 IMGTRANS_PROXY=127.0.0.1:10809
        #           和真实 Key；拷到线上会让翻译全部超时（且看起来像网络问题），
        #           而"已存在 .env 不覆盖"的幂等逻辑会让它一路带到生产
        #   .env.key 同上，密钥留档不跨机搬运
        #   .wheels 离线 wheel 包（本地 146MB），只在目标机需要时才单独传
        #   out/    运行产物
        #   .git    不需要，且可能带历史密钥
        #   __pycache__ 解释器版本不同，拷了反而可能命中过期字节码
        # 注意：通配符必须用单引号，否则 bash -c 会把 --exclude=*/__pycache__ 当 glob
        # 在本机 cwd 下展开成多个词（run_sh 是把整串交给 bash -c 执行的）
        local excludes
        excludes="--exclude='./.git' --exclude='./.venv' --exclude='./.venv-*' --exclude='./.tools' \
--exclude='./out' --exclude='./__pycache__' --exclude='*/__pycache__' --exclude='./.pytest_cache' \
--exclude='./.env' --exclude='./.env.key' --exclude='./.wheels' --exclude='./.workbuddy' --exclude='*.pyc'"
        run_sh "tar -C $(q "${SRC_DIR}") -cf - ${excludes} . | tar -C $(q "${INSTALL_DIR}") -xf -"
        log "源目录的 .env / .env.key / .wheels 未被拷贝（避免把开发机配置与密钥带到线上）"
    fi

    # 目录与权限：
    #   代码 root 拥有、组可读、**组不可写** —— 服务降权后只能读，改不了自己的代码
    #   .models / out / .tools / .venv 归运行用户 —— 模型下载、输出、缓存都要写
    #
    # .venv 必须在这里就建出来并交给运行用户，不能等 uv sync 去建：
    #   uv sync 是以运行用户身份执行的，而安装目录本身是 root:组 且组不可写（见下），
    #   运行用户没有权限在安装目录里 mkdir，于是 uv 报
    #   "failed to create directory <dir>/.venv: Permission denied (os error 13)"。
    #   （空 .venv 会被 uv 直接填充；只有当里面的 venv 不合法时才会删了重建，
    #    重建同样需要写父目录，那份临时权限由 step_sync 负责放开。）
    local venv_existed=0
    if [ -d "${INSTALL_DIR}/.venv" ]; then
        venv_existed=1
    fi
    run mkdir -p "${INSTALL_DIR}/.models" "${INSTALL_DIR}/out" "${INSTALL_DIR}/.tools" \
        "${INSTALL_DIR}/.venv" "${UV_HOME}" "${UV_BIN_DIR}"
    run chown -R "root:${RUN_GROUP}" "${INSTALL_DIR}"
    run chown -R "${RUN_USER}:${RUN_GROUP}" \
        "${INSTALL_DIR}/.models" "${INSTALL_DIR}/out" "${INSTALL_DIR}/.tools" "${INSTALL_DIR}/.venv"

    # chown 只改属主，不改模式：tar 解出来的文件若带 g+w（源目录权限松），
    # "代码只读"就落空了。这里把可写位从组和其他人身上摘掉，但跳过四个需要写的目录。
    if [ "${DRY_RUN}" -eq 0 ]; then
        find "${INSTALL_DIR}" -maxdepth 1 -mindepth 1 -type f -exec chmod go-w {} + 2>/dev/null || true
        local d
        for d in "${INSTALL_DIR}"/*/; do
            case "${d}" in
                */.models/|*/.venv/|*/.tools/|*/out/) continue ;;
            esac
            chmod -R go-w "${d}" 2>/dev/null || true
        done
    fi

    # 已存在的 venv 可能是上次以别的用户/umask 建的，uv sync 会因权限失败，先修补
    if [ "${venv_existed}" -eq 1 ]; then
        log "检测到已有 .venv，修补属主后仅做增量 sync（幂等）"
        run chown -R "${RUN_USER}:${RUN_GROUP}" "${INSTALL_DIR}/.venv"
    fi

    ok "项目文件已就位"
}

# ===========================================================================
# 步骤 6：准备解释器（uv 管理的 3.13）+ uv sync
# ===========================================================================
#: 固定用 3.13，而不是"系统里有啥用啥"：
#   * Ubuntu 22.04 自带 3.10，低于 requires-python=">=3.11"，
#     uv 直接报 "The requested interpreter resolved to ... which is incompatible"；
#   * Ubuntu 24.04 的 3.12 虽然够用，但开发/测试与既有实测结论都在 3.13 上，
#     两台机器跑两套小版本 = 两次排查（fastapi/pydantic 的 minor 差异足够制造麻烦）；
#   * 由 uv 提供解释器还顺带解决了"目标机没装 python3-venv"这类系统包依赖。
readonly DESIRED_PY="3.13"

#: 从系统里找一个 3.11~3.13 的解释器（仅用于离线降级）
pick_system_python() {
    local v
    for v in 3.13 3.12 3.11; do
        if command -v "python${v}" >/dev/null 2>&1; then
            printf '%s' "${v}"
            return 0
        fi
    done
    if command -v python3 >/dev/null 2>&1; then
        # python3 可能指向 3.11+（如 24.04 的 3.12），也可能指向 3.10（22.04）
        if python3 -c 'import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)' 2>/dev/null; then
            python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])'
            return 0
        fi
    fi
    return 1
}

step_sync() {
    CURRENT_STEP="准备 Python 解释器"
    log "${CURRENT_STEP}"

    # 整个步骤的所有动作都以绝对路径调 uv（见文件顶部 UV_BIN 的说明）。
    # 这里先判一次：缺失时立刻说清，而不是等 uv python install 报 127 ——
    # 那个退出码的原始信息是 "env: 'uv': No such file or directory"，
    # 极容易被读成"权限不足"或"网络不通"，然后白查一轮。
    if [ "${DRY_RUN}" -eq 0 ] && [ ! -x "${UV_BIN}" ]; then
        die "找不到可执行的 uv：${UV_BIN}
  多半是 uv 装在 root 自己的 PATH 里（如 /root/.local/bin），而运行用户 ${RUN_USER}
  的 PATH 不含它。处理：sudo install -m 0755 \"\$(command -v uv)\" ${UV_BIN}
  若系统里确实没有 uv，重跑本脚本的「安装 uv」步骤（会从 astral.sh 下载）。"
    fi

    # 托管解释器必须落在安装目录内，不能吃 uv 的默认位置（~/.local/share/uv/python）：
    #   * 运行用户是 --no-create-home 建的，root 装在家目录里的那份它读不到；
    #   * systemd 单元开了 ProtectHome=yes，运行时 /home 与 /root 直接不可见。
    # 放进项目目录 = 解释器属于项目、任何用户可读、不会被 ProtectHome 挡住，
    # 也与 uv-isolated-deploy.md 里的"形态 B：完全自包含"一致。
    UV_PYTHON_DIR="${INSTALL_DIR}/.tools/python"
    local py_managed=0

    # 幂等：已经装过就不要再下一次（uv python install 本身也是幂等的，但省一次网络判断）
    if ls -d "${UV_PYTHON_DIR}/cpython-${DESIRED_PY}"* >/dev/null 2>&1; then
        log "已有 uv 托管的 Python ${DESIRED_PY}（${UV_PYTHON_DIR}），跳过下载（幂等）"
        PY_SPEC="${DESIRED_PY}"
        py_managed=1
    else
        log "安装 uv 托管的 Python ${DESIRED_PY} 到 ${UV_PYTHON_DIR}"
        # 不用系统 python3 建 venv：22.04 的 3.10 不满足 requires-python
        local py_rc=0
        as_user "${UV_BIN}" python install "${DESIRED_PY}" || py_rc=$?
        if [ "${py_rc}" -eq 0 ]; then
            PY_SPEC="${DESIRED_PY}"
            py_managed=1
        else
            # 离线/内网下载失败 → 降级到系统解释器，而不是直接失败。
            # 这一步必须显式提示：版本与验证环境不同，出问题时先想到这一点。
            # 退出码写进日志：127 是"命令找不到"，与"网络下载失败"完全无关，
            # 不说清楚会被误读成后者（本机曾因此白查一轮）。
            warn "uv python install 失败（退出码 ${py_rc}），改为使用系统解释器"
            if [ "${py_rc}" -eq 127 ]; then
                warn "退出码 127 = 命令不存在，不是网络问题：检查 ${UV_BIN}"
            fi
            PY_SPEC="$(pick_system_python)" || die "既装不上托管 ${DESIRED_PY}，系统里也没有 3.11~3.13。
  离线环境请在有网机器执行 'uv python install ${DESIRED_PY}'，把生成的
  ${UV_PYTHON_DIR}/cpython-${DESIRED_PY}* 整个目录拷到目标机后重跑本脚本。"
            warn "降级使用系统 Python ${PY_SPEC}：与开发/验证环境（3.13）可能有不一致，请留意。"
            UV_PYTHON_DIR=""   # 用系统解释器时不要把这个变量带进 sync 环境
        fi
    fi

    if [ "${py_managed}" -eq 1 ]; then
        # 复核：uv 必须真的装到了我们指定的目录。若某些 uv 版本忽略了
        # UV_PYTHON_INSTALL_DIR，解释器会落在 ~/.local/share/uv/python ——
        # 那在 ProtectHome=yes 下服务读不到，是典型的"能装不能跑"。
        if [ "${DRY_RUN}" -eq 0 ]; then
            ls -d "${UV_PYTHON_DIR}/cpython-${DESIRED_PY}"* >/dev/null 2>&1 \
                || die "uv python install 成功但 ${UV_PYTHON_DIR}/cpython-${DESIRED_PY}* 不存在。
  解释器可能被装到了别处（uv python dir 会告诉你实际位置）。
  可手动执行：UV_PYTHON_INSTALL_DIR=${UV_PYTHON_DIR} uv python install ${DESIRED_PY}"
        fi
        log "解释器：uv 托管 Python ${PY_SPEC}（${UV_PYTHON_DIR}，属主 ${RUN_USER}，服务可直接读）"
    fi
    log "uv 缓存目录：${UV_CACHE}（放在安装目录内，删目录即清空，且不受 ProtectHome 影响）"

    CURRENT_STEP="安装 Python 依赖（uv sync --frozen）"
    log "${CURRENT_STEP}"

    local extra="cpu"
    if [ "${ENABLE_GPU}" -eq 1 ]; then
        extra="gpu"
    fi

    # --frozen：完全信任 uv.lock，一个字节都不重新解析。线上必须加，
    #   否则 uv 会去 PyPI 校验/协商，网络抖动时表现为"启动很慢最后超时"。
    # --project：显式指定项目目录，不依赖 cwd（我们是以别的用户跑的）
    local -a sync_args=(sync --frozen --extra "${extra}" --project "${INSTALL_DIR}" --no-progress)
    sync_args+=(--python "${PY_SPEC}")
    if [ -d "${INSTALL_DIR}/.wheels" ]; then
        # 内网/离线交付：项目里带了 .wheels/ 就顺手当本地源，不必先配私有仓库
        log "检测到 ${INSTALL_DIR}/.wheels，作为本地包源（--find-links）"
        sync_args+=(--find-links "${INSTALL_DIR}/.wheels")
    fi
    if [ "${IMGTRANS_DEPLOY_OFFLINE:-0}" = "1" ]; then
        log "IMGTRANS_DEPLOY_OFFLINE=1：启用 --offline，全程不联网"
        sync_args+=(--offline)
    fi

    # 安装目录顶层是 root:组、组不可写（服务因此改不了自己的代码）。但 uv 在 .venv
    # 缺失或不合法时需要 **在安装目录里创建/删除 .venv**，这要求父目录可写 ——
    # 之前就以 "failed to create directory .venv: Permission denied" 卡死在这里。
    # 所以只在 uv sync 期间临时放开组写，目录模式先存下来，结束后无论成败都原样收回；
    # 装完的最终权限仍与"代码只读"的设计一致。
    local dir_mode=""
    if [ "${DRY_RUN}" -eq 0 ]; then
        dir_mode="$(stat -c '%a' "${INSTALL_DIR}" 2>/dev/null || true)"
        run chown -R "${RUN_USER}:${RUN_GROUP}" "${INSTALL_DIR}/.venv" 2>/dev/null || true
        run chmod g+w "${INSTALL_DIR}"
    fi

    # 前置校验：走绝对路径，但先确认它真的存在且可执行。
    # 少了这一句，uv 缺失时会一路跑到 sync 才报 127，而 127 的原始信息
    # "env: 'uv': No such file or directory" 会被当成"权限/网络"问题去查。
    if [ "${DRY_RUN}" -eq 0 ]; then
        [ -x "${UV_BIN}" ] || die "找不到可执行的 uv：${UV_BIN}
  处理：sudo install -m 0755 \"\$(command -v uv)\" ${UV_BIN}
  （root 的 PATH 里有 uv，但运行用户 ${RUN_USER} 的 PATH 里没有，属于常见的错位。）"
    fi

    local sync_rc=0
    as_user "${UV_BIN}" "${sync_args[@]}" || sync_rc=$?

    if [ "${DRY_RUN}" -eq 0 ] && [ -n "${dir_mode}" ]; then
        run chmod "${dir_mode}" "${INSTALL_DIR}"
    fi
    if [ "${sync_rc}" -ne 0 ]; then
        die "uv sync 失败（退出码 ${sync_rc}）。
  * Permission denied → 检查 ${INSTALL_DIR} 属主为 root:${RUN_GROUP}，且 ${RUN_USER} 可写：
      chown root:${RUN_GROUP} $(q "${INSTALL_DIR}") && chmod g+w $(q "${INSTALL_DIR}")
  * 网络超时 → 需要能访问 PyPI；内网用 .wheels/ 搭配 IMGTRANS_DEPLOY_OFFLINE=1
  * resolution failed → uv.lock 与 pyproject.toml 不同步，本地 uv lock 后重传"
    fi

    if [ "${DRY_RUN}" -eq 0 ]; then
        [ -x "${INSTALL_DIR}/.venv/bin/python" ] \
            || die "uv sync 结束了但 ${INSTALL_DIR}/.venv/bin/python 不存在，环境没建成"
        log "venv Python：$("${INSTALL_DIR}/.venv/bin/python" -V 2>&1)"

        # 解释器真实路径必须落在安装目录内：落在 /home 或 /root 下的话，
        # systemd 的 ProtectHome=yes 会让服务启动即失败（而 install 阶段一切正常）。
        local real_py
        real_py="$(readlink -f "${INSTALL_DIR}/.venv/bin/python" 2>/dev/null \
            || printf '%s' "${INSTALL_DIR}/.venv/bin/python")"
        log "venv 解释器真实路径：${real_py}"
        case "${real_py}" in
            "${INSTALL_DIR}"/*) ;;
            *) warn "解释器不在安装目录内（${real_py}）。若它位于 /home 或 /root 下，服务会因 ProtectHome=yes 启动失败；
      处理：删除 .venv 后重跑本脚本，让解释器装进 ${INSTALL_DIR}/.tools/python。" ;;
        esac
    fi

    if [ "${ENABLE_GPU}" -eq 1 ]; then
        # is_compiled_with_cuda() 只证明这个 wheel 带 CUDA 编译，不证明当前驱动
        # 能执行 kernel —— RTX 50 系列（sm_120）配 cu126 的 wheel 就典型地过得
        # 了这一项、推理时才炸 "no kernel image is available"。所以这里查运行时
        # 真判据 device_count()，顺便把计算能力打出来备查。
        if [ "${DRY_RUN}" -eq 0 ]; then
            # 探测脚本除了设备数，还把**实际加载的 libcuda.so.1 路径**打出来。
            # 803 = CUDA_ERROR_SYSTEM_DRIVER_MISMATCH，字面意思是"用户态 CUDA
            # 驱动与内核驱动不匹配"。它的头号成因不是驱动装错，而是加载到了
            # bundled/compat 那份 libcuda（CUDA toolkit 的 /compat 目录），
            # 而不是宿主驱动提供的 /usr/lib/x86_64-linux-gnu/libcuda.so.1。
            # 不打路径的话，这一项只能靠人瞎试。
            local probe_py
            probe_py='
import ctypes
import paddle
print("cuda_compiled=%s" % paddle.device.is_compiled_with_cuda())
print("device_count=%s" % paddle.device.cuda.device_count())
try:
    major, minor = paddle.device.cuda.get_device_capability()
    print("capability=%s.%s" % (major, minor))
except Exception as exc:
    print("capability=unknown(%s)" % exc)
try:
    ctypes.CDLL("libcuda.so.1")
    for line in open("/proc/self/maps"):
        if "libcuda.so" in line:
            print("libcuda=%s" % line.split()[-1])
            break
except Exception as exc:
    print("libcuda=unavailable(%s)" % exc)
'

            # 判定口径：**总是**在清空 LD_LIBRARY_PATH 的环境里判，而不是等
            # 发现本进程有这个变量才重试。
            #
            # 这条踩过一次：上一版写成 `if [ -n "${LD_LIBRARY_PATH:-}" ]` 才重试，
            # 结果父 shell 里它是空的（root 的登录环境没设），变量是 runuser 走
            # PAM 时由 /etc/environment 注入子进程的 —— 条件不成立，重试根本没跑，
            # 自检照样报 803，日志里连"已定位/已排除"都没有，看着像没改过。
            #
            # 口径对不对也有依据：systemd 单元里写的是 Environment="LD_LIBRARY_PATH="
            # （置空），服务真正运行的环境就是干净的，自检必须用同一个口径。
            local raw_probe clean_probe count cap
            clean_probe="$(runuser -u "${RUN_USER}" -- env -u LD_LIBRARY_PATH \
                "${INSTALL_DIR}/.venv/bin/python" -c "${probe_py}" 2>&1 || true)"
            count="$(printf '%s\n' "${clean_probe}" | sed -n 's/^device_count=//p' | tail -n 1)"

            if [ "${count:-0}" -ge 1 ] 2>/dev/null; then
                cap="$(printf '%s\n' "${clean_probe}" | sed -n 's/^capability=//p' | tail -n 1)"
                log "GPU 自检通过：${count} 个可用设备，计算能力 sm_${cap}"
                case "${cap}" in
                    12.*|10.*)
                        log "识别到 Blackwell（sm_${cap}）：kernel 来自 pyproject 的 cu129 源，cu126 的 wheel 不含这一代" ;;
                esac

                # 干净环境能过、不清理过不了 → 那个变量就是唯一变量。服务侧不受
                # 影响（unit 已置空），但人手跑 serve.py / ocr_bench.py 还会撞。
                raw_probe="$(runuser -u "${RUN_USER}" -- \
                    "${INSTALL_DIR}/.venv/bin/python" -c "${probe_py}" 2>&1 || true)"
                if ! printf '%s\n' "${raw_probe}" | grep -q '^device_count=[1-9]'; then
                    warn "留意：不清理 LD_LIBRARY_PATH 时 device_count=0（CUDA error 803）。"
                    warn "  服务不受影响——unit 里已 Environment=\"LD_LIBRARY_PATH=\" 置空；"
                    warn "  但手动跑 python scripts/serve.py / scripts/ocr_bench.py 前要先 unset。"
                    # 把"从哪儿来"说清楚：本进程有值 → 继承自启动脚本的 shell；
                    # 本进程没有却传给了子进程 → 只能是 runuser/PAM 注入的。
                    if [ -n "${LD_LIBRARY_PATH:-}" ]; then
                        warn "  当前进程的值：${LD_LIBRARY_PATH}"
                    else
                        warn "  本进程里它是空的 → 是 runuser 走 PAM 注入给子进程的"
                    fi
                    local srcs
                    srcs="$(ld_library_path_sources || true)"
                    if [ -n "${srcs}" ]; then
                        warn "  注入源（删掉或注释掉下面这几行）："
                        printf '%s\n' "${srcs}" | sed 's/^/      /' >&2
                    else
                        warn "  常见位置没找到注入源，用这条查："
                        warn "    grep -rn LD_LIBRARY_PATH /etc/environment /etc/profile.d /root/.bashrc"
                    fi
                fi
            else
                warn "GPU 自检未通过（device_count=${count:-0}）：paddle 能看到驱动，但看不到可用设备。"
                warn "  ↑ 以上是清空 LD_LIBRARY_PATH 之后的输出，也就是服务实际运行的环境"
                printf '%s\n' "${clean_probe}" | sed 's/^/    /' >&2
                case "${clean_probe}" in
                    *803*)
                        warn "803 = CUDA_ERROR_SYSTEM_DRIVER_MISMATCH：用户态 libcuda 与内核驱动版本不一致。" ;;
                esac

                # 清空后仍失败，就不是 LD_LIBRARY_PATH 的事。但还有一处常被漏掉：
                # 有人把 compat 目录写进了 ld.so.conf.d —— 那是全局并进 ld 缓存，
                # env -u 管不到它，所以这里要把加载路径打出来对照。
                local loaded
                loaded="$(printf '%s\n' "${clean_probe}" | sed -n 's/^libcuda=//p' | tail -n 1)"
                if [ -n "${loaded}" ]; then
                    warn "paddle 实际加载的驱动库：${loaded}"
                    case "${loaded}" in
                        */compat/*)
                            warn "  ↑ 这是 CUDA toolkit 的 forward-compat 垫片，不是宿主驱动。"
                            warn "    清空 LD_LIBRARY_PATH 也没甩掉，说明它来自 ld 缓存："
                            warn "    grep -rn compat /etc/ld.so.conf /etc/ld.so.conf.d/ 改完再 ldconfig" ;;
                        /usr/lib/*|/lib/*)
                            log "  ↑ 是宿主驱动那份，路径正常。" ;;
                    esac
                fi
                warn "进一步排查（逐条跑）："
                warn "  cat /proc/driver/nvidia/version            # 内核模块版本，应与 nvidia-smi 一致"
                warn "  nvidia-smi --query-gpu=driver_version --format=csv,noheader"
                warn "  ls -l /usr/lib/x86_64-linux-gnu/libcuda.so*  # 宿主驱动这份的版本后缀"
                warn "  ls -l /usr/local/cuda*/compat/libcuda.so* 2>/dev/null  # 若有，多半就是它"
                warn "先降级上线：把 ${ENV_FILE} 的 IMGTRANS_DEVICE 改成 cpu 再 restart。"
            fi
        fi
    fi

    ok "依赖环境已就绪（uv sync --frozen --extra ${extra}）"
}

# ===========================================================================
# 步骤 7：生成 .env（已存在则绝不覆盖）
# ===========================================================================
step_env() {
    CURRENT_STEP="配置 .env"
    log "${CURRENT_STEP}"

    if [ -f "${ENV_FILE}" ]; then
        log ".env 已存在，**不覆盖**（幂等；覆盖等于把线上密钥冲掉）"
        # 端口以 .env 为准：serve.py 里 .env 会写进环境，systemd 也没注入端口，
        # 所以 nginx 必须反代到 .env 里那个端口，否则 502。
        local existing_port
        existing_port="$(sed -n 's/^IMGTRANS_PORT=//p' "${ENV_FILE}" | tail -n 1 | tr -d '[:space:]' | tr -d '"' | tr -d "'")"
        if [ -n "${existing_port}" ]; then
            EFFECTIVE_PORT="${existing_port}"
            if [ "${existing_port}" != "${PORT}" ]; then
                warn ".env 里 IMGTRANS_PORT=${existing_port}，与 --port ${PORT} 不一致；以 .env 为准，nginx 将反代到 ${existing_port}"
            fi
        else
            EFFECTIVE_PORT="${PORT}"
        fi
        # 权限修补：600 保证只有服务用户能读（systemd 以 root 读 EnvironmentFile，root 不受限）
        run chown "${RUN_USER}:${RUN_GROUP}" "${ENV_FILE}"
        run chmod 600 "${ENV_FILE}"

        # 沿用已有 .env 时必须做三项体检，只 warn 不 die。
        # 场景：离线交付时把开发机的 .env 一起拷了过来 —— 它三项齐全，
        # 于是没有任何告警，症状是"每次翻译先等连接超时"，看起来完全像网络问题。
        local e_proxy e_key e_host
        e_proxy="$(sed -n 's/^IMGTRANS_PROXY=//p' "${ENV_FILE}" | tail -n 1 | tr -d '[:space:]' | tr -d '"' | tr -d "'")"
        e_key="$(sed -n 's/^IMGTRANS_API_KEY=//p' "${ENV_FILE}" | tail -n 1 | tr -d '[:space:]' | tr -d '"' | tr -d "'")"
        e_host="$(sed -n 's/^IMGTRANS_HOST=//p' "${ENV_FILE}" | tail -n 1 | tr -d '[:space:]' | tr -d '"' | tr -d "'")"

        if [ -n "${e_proxy}" ] && printf '%s' "${e_proxy}" | grep -Eq '127\.0\.0\.1|localhost|:1080[0-9]'; then
            warn "IMGTRANS_PROXY=${e_proxy} 看起来是开发机代理：线上没有它，翻译会全部超时（症状像网络故障）"
            warn "  服务器能直连出海就清空该行；需要代理就填线上真实地址"
        fi
        if [ "${#e_key}" -ne 40 ]; then
            warn "IMGTRANS_API_KEY 缺失或长度异常（当前 ${#e_key} 字符，应为 40）：serve.py 会拒绝启动"
            warn "  修复：sed -i \"s|^IMGTRANS_API_KEY=.*|IMGTRANS_API_KEY=\$(openssl rand -hex 20)|\" ${ENV_FILE}"
        fi
        case "${e_host}" in
            127.0.0.1|localhost|"") ;;
            *)
                warn "IMGTRANS_HOST=${e_host}：服务会直接监听该地址（可能是公网），"
                warn "  而本服务只有单 Key 鉴权、没有 TLS。除非确实需要直连，否则改回 127.0.0.1 由 nginx 反代。"
                ;;
        esac
        if ! grep -q '^IMGTRANS_MODEL_DIR=' "${ENV_FILE}"; then
            warn ".env 里没有 IMGTRANS_MODEL_DIR，模型会落到 ~/.paddlex（约 240MB，且与同机其它 paddle 项目共用）"
        fi
        ok ".env 已沿用（未重新生成 API Key；如需查看：sudo cat ${ENV_FILE}）"
        return 0
    fi

    EFFECTIVE_PORT="${PORT}"

    # API Key：openssl rand -hex 20 = 40 个十六进制字符；openssl 不在时退到 /dev/urandom。
    # `|| true` 是必要的：`head -c` 提前关管道会让 tr 收到 SIGPIPE，pipefail 下会误判失败。
    if command -v openssl >/dev/null 2>&1; then
        API_KEY="$(openssl rand -hex 20)"
    else
        API_KEY="$(LC_ALL=C tr -dc 'a-f0-9' < /dev/urandom | head -c 40 || true)"
    fi
    [ "${#API_KEY}" -eq 40 ] || die "API Key 生成失败（长度 ${#API_KEY}）。请检查 openssl 或 /dev/urandom 是否可用。"

    local device="cpu"
    if [ "${ENABLE_GPU}" -eq 1 ]; then
        device="gpu"
    fi

    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '[dry-run] 将生成 %s（内含新随机 40 位 API Key、IMGTRANS_HOST=127.0.0.1、PORT=%s、DEVICE=%s）\n' \
            "${ENV_FILE}" "${EFFECTIVE_PORT}" "${device}"
        ok ".env 内容已确定（dry-run，未落盘）"
        return 0
    fi

    # 原子写入：先写 .env.tmp，校验 Key 行完整，再 mv 覆盖。
    # 为什么不能直接 `> .env`：ENOSPC / 被中断时会留下"存在但 Key 行不全"的文件，
    # 重跑时幂等分支会认为"已有 .env"而跳过生成，而那个 Key 从来没打印过 ——
    # serve.py 拒绝启动 → Restart=always 每 3s 崩一次，且密钥不可恢复。
    # umask 077：从创建那一刻就是 600，避免"先 644 再 chmod"中间那一瞬被读到。
    local env_tmp="${ENV_FILE}.tmp.$$"
    if ! (
        umask 077
        cat > "${env_tmp}" <<EOF
# imgtrans 服务配置 —— 由 deploy/install.sh 生成于 $(date -Iseconds)
#
# 调用方请求头需带 X-Api-Key: <key>（也接受 Authorization: Bearer <key>）
# 本文件权限必须是 600，里面是明文密钥。
IMGTRANS_API_KEY=${API_KEY}

# 访问 Google 翻译 / 下载图片用的代理。服务器能直连出海就留空。
# 千万不要照抄开发机的 127.0.0.1:10809 —— 线上没有那个代理，症状是"翻译全部超时"。
IMGTRANS_PROXY=

# 翻译通道：web（免费 web 接口）/ cloud（官方 Cloud Translation v2）/ baidu（百度通用文本翻译）。
# 免费通道抓的是 Google 网页接口，**机房 IP 会被 429 限流**，症状是接口返回
# 200 + 一张原封不动的中文图，日志里只有 circuit_open。生产建议用 cloud 或 baidu。
IMGTRANS_TRANSLATOR=web
# cloud 通道凭证（用 cloud 时取消注释；另外还需把 google-cloud-translate 加进依赖）：
# IMGTRANS_GOOGLE_KEY=
# IMGTRANS_GOOGLE_CREDENTIALS=
# IMGTRANS_GOOGLE_PROJECT=
# baidu 通道凭证（用 baidu 时取消注释）：APP ID 是「管理控制台 → 我的服务」
# 里那串**纯数字**，密钥是同一行的另一串（不是 APP ID，混填会报 54001 签名错误）。
# IMGTRANS_BAIDU_APPID=
# IMGTRANS_BAIDU_SECRET=
# 百度通道的 QPS：标准版只有 1，高级版可设 10。写大了只会拿到 54003。
# IMGTRANS_BAIDU_QPS=1
# 百度通道的代理。**默认不继承 IMGTRANS_PROXY**——那个通常是给 Google 配的
# 海外代理，百度走它会被判境外 IP（58000）或白绕一圈。确实需要再填。
# IMGTRANS_BAIDU_PROXY=
# 账号是否企业尊享版。百度按档位切语言能力：标准版/高级版只有 28 个常见语种，
# 其余 11 个（土耳其语 tr、印尼语 id、印地语 hi、希伯来语 he、乌克兰语 uk、
# 格鲁吉亚语 ka、马来语 ms、挪威语 no、克罗地亚语 hr、斯洛文尼亚语 sl、
# 孟加拉语 bn）要尊享版才翻得了。留 0 时这些语言会在请求入口就被拒并说明原因
# （而不是跑完 OCR 才拿到 58001）；开通尊享版后改成 1。
# IMGTRANS_BAIDU_PREMIUM=0

# 监听地址：只监听回环，由 nginx 反代，不直接对公网暴露服务端口。
# （若不用 nginx 又要被外部访问，改成 0.0.0.0，并自己承担鉴权/限流）
IMGTRANS_HOST=127.0.0.1
IMGTRANS_PORT=${EFFECTIVE_PORT}

# 推理设备：cpu / gpu / gpu:0 / auto（有卡用卡、无卡回落 cpu）
# 注意：GPU 版 wheel 走 cu129 源（含 Blackwell sm_100/sm_120 的 kernel）。
# 换成 cu126 在 RTX 50 系列上会装得上、跑不动（no kernel image）。
IMGTRANS_DEVICE=${device}

# OCR 速度档位：quality（默认，最准）/ fast（CPU 上实测快一倍）
# 瓶颈在检测阶段，fast 只换检测模型，识别模型不变，识别精度不降。
# 代价：紧邻的两行可能被并成一个框、照片内英文小字召回略降。
IMGTRANS_OCR_PROFILE=quality

# 模型缓存目录（相对项目根，约 140MB）。指向项目内 = 删目录即彻底清除，
# 且不会与同机其它 paddle 项目互相污染。
IMGTRANS_MODEL_DIR=.models

# 模型下载源：huggingface（paddlex 默认）/ bos / modelscope / aistudio。
# 默认填 bos 的原因：huggingface 的元数据（config.json 等几 KB）通常能下，
# 但几 MB 的 inference.pdiparams 在国内/香港机房经常超时，症状是"模型一直
# 没下完"——更麻烦的是代码会**静默回退到慢档**，不看日志发现不了。
# 注意 paddlex 会先做连通性探测，探测不过就跳过该源（不报错），
# 所以这项只是"优先"，不通时仍会退回 huggingface。
PADDLE_PDX_MODEL_SOURCE=bos

# 额外字体目录，可选。字体装到系统目录时不用设。
# IMGTRANS_FONT_DIRS=/opt/fonts
EOF
    ); then
        rm -f "${env_tmp}"
        die ".env 写入失败（磁盘满？df -h 看一下）。已删除半截临时文件，可修复后重跑本脚本。"
    fi

    # 校验再落位：Key 行必须正好 40 字符
    if ! grep -Eq '^IMGTRANS_API_KEY=.{40}$' "${env_tmp}"; then
        rm -f "${env_tmp}"
        die ".env 写入不完整（Key 行校验失败），已删除临时文件。
  常见原因：磁盘满（df -h）、或 /tmp 与目标分区不同导致的空间问题。"
    fi
    run mv -f "${env_tmp}" "${ENV_FILE}"
    run chown "${RUN_USER}:${RUN_GROUP}" "${ENV_FILE}"
    run chmod 600 "${ENV_FILE}"

    # 密钥留档：root:root 600，只有 root 能读。
    # 用途：install 被 `| tee` 到日志、或终端被清屏时，运维仍能取回 Key。
    if [ "${DRY_RUN}" -eq 0 ]; then
        ( umask 077; printf '%s\n' "${API_KEY}" > "${KEY_FILE}" )
        chown root:root "${KEY_FILE}"
        chmod 600 "${KEY_FILE}"
    else
        printf '[dry-run] 将写入密钥留档 %s（root:root 600）\n' "${KEY_FILE}"
    fi

    echo
    printf '%s=================== 请立即保存 API Key ===================%s\n' "${c_yellow}" "${c_reset}"
    if [ -t 1 ]; then
        # 只在终端打印明文：`sudo bash install.sh 2>&1 | tee install.log` 会
        # 把 40 位密钥写进一个 644 的日志文件，等于明文密钥落地。
        printf '  %s\n' "${API_KEY}"
        printf '  （已写入 %s，权限 600；本脚本不会再打印它）\n' "${ENV_FILE}"
    else
        printf '\n  API Key 已写入 %s（600，仅服务用户可读）与 %s（600，仅 root 可读）\n' \
            "${ENV_FILE}" "${KEY_FILE}"
        printf '  当前输出不是终端（可能正在被 tee 到日志），故不回显明文。\n'
        printf '  查看：sudo cat %s\n' "${KEY_FILE}"
    fi
    printf '%s=========================================================%s\n' "${c_yellow}" "${c_reset}"
    echo

    ok ".env 已生成并设为 600"
}

# ===========================================================================
# 步骤 8：自检（doctor.py）
# ===========================================================================
step_doctor() {
    CURRENT_STEP="部署自检（doctor.py）"
    log "${CURRENT_STEP}"

    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '[dry-run] (cd %s && .venv/bin/python scripts/doctor.py)\n' "${INSTALL_DIR}"
        ok "自检已跳过（dry-run）"
        return 0
    fi

    log "以 ${RUN_USER} 身份跑自检：逐项检查依赖 / raqm / 每种文字命中哪个字体 / 设备 / 目录可写"

    local output=""
    local code=0
    # 想拿退出码又不触发 ERR trap，只有 `|| code=$?` 这一种写法。
    # 外面套 set +e 是无效的 —— ERR trap 不受 errexit 控制（实测：set +e 之后
    # 命令失败，trap 照样触发），于是 doctor 报 FAIL 时直接走 on_err 退出，
    # 它自己的输出一个字都没打出来。现场日志就是这个样子：只剩一行
    # "第 1318 行执行失败"，看不到到底哪一项 FAIL，等于把诊断信息丢了。
    # 放在 || 右侧的命令属于"失败不触发 trap"的列表，才真能兜住。
    #
    # env -u LD_LIBRARY_PATH 与 as_user、systemd 单元同一口径：服务运行时这个
    # 变量是空的，自检也必须按空环境判。否则 GPU 机器上会加载 compat 版 libcuda
    # 报 CUDA error(803)，doctor 判 E_DEVICE FAIL，装到最后一个步骤才失败。
    output="$(runuser -u "${RUN_USER}" -- env -u LD_LIBRARY_PATH "HOME=${UV_HOME}" \
        bash -c "cd $(q "${INSTALL_DIR}") && exec .venv/bin/python scripts/doctor.py" 2>&1)" || code=$?

    printf '%s\n' "${output}"

    if [ "${code}" -ne 0 ]; then
        err "doctor.py 报了 FAIL（退出码 ${code}），先修再上线。"
        cat >&2 <<'EOF'
常见 FAIL 与处理：
  * “依赖 xxx 未安装”        → 重跑 `uv sync --frozen --extra cpu`（GPU 机器 --extra gpu）
  * “字体索引 一个字体都没找到” → apt install fonts-noto-cjk fonts-dejavu-core fonts-noto-core
  * “字体 zh 回退到 DejaVuSans → 会画成豆腐块” → 同上，装 fonts-noto-cjk
  * “推理设备 xxx 不可用”     → 配了 gpu 但装的是 CPU 版 paddle；改 .env 的 IMGTRANS_DEVICE=cpu 或装 GPU 版
  * 推理报 “no kernel image is available” → 驱动或 wheel 的架构代对不上：
                              RTX 50 系列（sm_120）要 cu129/cu130 的 wheel，cu126 没有它的 kernel
EOF
        # 给出"不进安装流程也能复跑"的命令：改一行配置再验证时，不必重跑
        # 前面七步（虽然幂等，但 uv sync 与 apt 检查每次都要几十秒）。
        printf '原样复跑（与上面输出一致）：\n  cd %s && sudo -u %s env -u LD_LIBRARY_PATH HOME=%s .venv/bin/python scripts/doctor.py\n\n' \
            "${INSTALL_DIR}" "${RUN_USER}" "${UV_HOME}" >&2
        die "${CURRENT_STEP}未通过"
    fi

    ok "自检通过（WARN 不阻塞；「监听地址 127.0.0.1」的 WARN 是预期内的——由 nginx 反代）"
}

# ===========================================================================
# 步骤 9：安装 systemd 单元
# ===========================================================================
step_systemd() {
    CURRENT_STEP="安装 systemd 服务"
    log "${CURRENT_STEP}"

    local template="${SCRIPT_DIR}/${UNIT_NAME}.service"
    [ -f "${template}" ] || die "找不到单元模板 ${template}"

    if [ "${DRY_RUN}" -eq 0 ]; then
        [ -x "${INSTALL_DIR}/.venv/bin/python" ] \
            || die "找不到 ${INSTALL_DIR}/.venv/bin/python，无法生成 ExecStart。请先让 uv sync 成功。"
    fi

    # 模板里用 @占位符@，sed 用 | 分隔（路径里 / 太多，用 / 会很难读）；
    # 多个表达式用 ; 串在一条 -e 里，避免续行的缩进被算进命令。
    # esc() 必须加：路径里出现 | & \ 时，不转义会让 sed 报错（而 `>` 已经把目标
    # 文件截断了 → 单元文件变 0 字节），或者静默产出 a@INSTALL_DIR@b 这种错路径。
    # 写 .new 再 mv：中途失败不会毁掉正在用的旧单元文件。
    local sed_expr
    sed_expr="s|@INSTALL_DIR@|$(esc "${INSTALL_DIR}")|g"
    sed_expr="${sed_expr};s|@RUN_USER@|$(esc "${RUN_USER}")|g"
    sed_expr="${sed_expr};s|@RUN_GROUP@|$(esc "${RUN_GROUP}")|g"
    local unit_new="${UNIT_PATH}.new"

    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '[dry-run] sed -e %s %s > %s\n' "$(q "${sed_expr}")" "$(q "${template}")" "$(q "${unit_new}")"
        printf '[dry-run] 校验非空 + 无残留占位符后 mv -f %s %s\n' "$(q "${unit_new}")" "$(q "${UNIT_PATH}")"
    else
        run_sh "sed -e $(q "${sed_expr}") $(q "${template}") > $(q "${unit_new}")"
        if [ ! -s "${unit_new}" ]; then
            rm -f "${unit_new}"
            die "渲染出的单元文件为空（sed 失败？）。检查 --dir 里是否有 | & \\ 等特殊字符。"
        fi
        if grep -q '@[A-Z_]*@' "${unit_new}"; then
            rm -f "${unit_new}"
            die "单元文件仍有未替换的占位符，可能是模板与脚本版本不一致：${unit_new}"
        fi
        run chmod 644 "${unit_new}"
        run mv -f "${unit_new}" "${UNIT_PATH}"
    fi

    run systemctl daemon-reload
    # enable --now 对已在运行的服务不会重启；但我们要让新代码生效，所以显式 restart
    run systemctl enable "${UNIT_NAME}"
    if [ "${DRY_RUN}" -eq 0 ] && systemctl is-active --quiet "${UNIT_NAME}"; then
        log "服务已在运行，执行 restart 让新版本生效"
        run systemctl restart "${UNIT_NAME}"
    else
        run systemctl start "${UNIT_NAME}"
    fi

    if [ "${DRY_RUN}" -eq 0 ]; then
        # 就绪等待时间按 .models 是否为空分流：
        #   * 已预热过（.models 非空）：只需加载模型，60s 足够
        #   * 全新机器（.models 为空）：要现场下载约 140MB，60s 根本不够 ——
        #     到点就判死会把一台其实会好的机器赶回原点。
        local ready_timeout=60
        if [ -z "$(ls -A "${INSTALL_DIR}/.models" 2>/dev/null)" ]; then
            ready_timeout=300
            log "模型目录为空：首次启动需现场下载约 140MB，就绪等待放宽到 ${ready_timeout}s"
            log "  想看进度：journalctl -u ${UNIT_NAME} -f（另开一个终端）"
        fi
        log "等待服务就绪（最多 ${ready_timeout}s）"
        local i=0
        local ready=0
        while [ "${i}" -lt "${ready_timeout}" ]; do
            if curl -fsS -m 2 "http://127.0.0.1:${EFFECTIVE_PORT}/healthz" >/dev/null 2>&1; then
                ready=1
                break
            fi
            if ! systemctl is-active --quiet "${UNIT_NAME}"; then
                break
            fi
            i=$((i + 1))
            sleep 1
        done
        if [ "${ready}" -ne 1 ]; then
            SERVICE_NOT_READY=1
            err "服务在 ${ready_timeout}s 内未通过 /healthz —— 但这**不一定是失败**："
            err "  全新机器可能仍在下载模型（见 journalctl -u ${UNIT_NAME} -f）。"
            warn "先别删目录。按顺序看："
            warn "  1) journalctl -u ${UNIT_NAME} -n 100 --no-pager -l   # 有没有 traceback / 下载进度"
            warn "  2) ss -ltnp | grep ${EFFECTIVE_PORT}                 # 端口有没有在听"
            warn "  3) systemctl show -p NRestarts --value ${UNIT_NAME}  # 是否在崩溃循环"
            warn "安装会继续执行（nginx 仍需配置），收尾时会给完整清单。"
            systemctl status "${UNIT_NAME}" --no-pager -l 2>&1 | tail -n 15 >&2 || true
        else
            log "服务已就绪：$(curl -fsS -m 3 "http://127.0.0.1:${EFFECTIVE_PORT}/healthz")"
        fi
    fi

    ok "systemd 服务已配置并启动（日志：journalctl -u ${UNIT_NAME} -f）"
}

# ===========================================================================
# 步骤 10：nginx 反代
# ===========================================================================
step_nginx() {
    CURRENT_STEP="配置 nginx 反代"
    if [ "${ENABLE_NGINX}" -eq 0 ]; then
        log "按 --no-nginx 跳过。自行反代时务必设置："
        log "  client_max_body_size 64m;      # base64 膨胀 33%：20MB 图 → 27MB 请求体"
        log "  proxy_read_timeout 180s;       # 默认 60s 会把 OCR 请求掐断，表现为 504"
        CURRENT_STEP="跳过 nginx"
        return 0
    fi
    log "${CURRENT_STEP}"

    # ---- 布局探测必须在"装包"之前 --------------------------------------------
    # 这台机器的现场：宝塔面板，nginx 在 /www/server/nginx/sbin/nginx，常常不在
    # PATH 里。于是 `command -v nginx` 为假 → 脚本 apt install nginx → 系统里
    # 出现第二份 nginx 去抢 80 端口，整站挂掉。这个判断必须前置。
    if [ -d /www/server/panel/vhost/nginx ]; then
        warn "检测到宝塔/aaPanel 面板（/www/server/panel/vhost/nginx），站点由面板管理。"
        warn "  脚本不写站点配置：同一 server_name 写两份会让 nginx 只认第一份"
        warn "  （conflicting server name），而面板下次保存站点又会整份覆盖。"
        # 顺手确认一下已有反代是不是指向本服务的端口 —— 端口不一致是史上最多的
        # "配置都对但 502"，而且面板里改过端口的人不会想起来通知这里。
        local f pp found=""
        for f in /www/server/panel/vhost/nginx/*.conf; do
            [ -f "${f}" ] || continue
            pp="$(grep -oE 'proxy_pass[[:space:]]+https?://[^;]+' "${f}" 2>/dev/null | head -n 1 || true)"
            case "${pp}" in
                *":${EFFECTIVE_PORT}"*) found="${f}" ;;
            esac
        done
        if [ -n "${found}" ]; then
            ok "已有反代指向本端口：${found} → 127.0.0.1:${EFFECTIVE_PORT}"
            grep -q 'client_max_body_size' "${found}" 2>/dev/null \
                || warn "  该站点没有 client_max_body_size，20MB 图会 413；建议 64m"
            grep -q 'proxy_read_timeout' "${found}" 2>/dev/null \
                || warn "  该站点没有 proxy_read_timeout，OCR 慢时可能 504；建议 180s"
        else
            warn "  没找到指向 127.0.0.1:${EFFECTIVE_PORT} 的 proxy_pass。反代需自行确认："
            warn "    proxy_pass http://127.0.0.1:${EFFECTIVE_PORT};"
            warn "    client_max_body_size 64m;    # base64 膨胀 33%，20MB 图 = 27MB 请求体"
            warn "    proxy_read_timeout 180s;     # 默认 60s 会把 OCR 请求掐断，表现为 504"
        fi
        CURRENT_STEP="配置 nginx 反代（面板管理，已跳过）"
        return 0
    fi

    if ! command -v nginx >/dev/null 2>&1; then
        run env DEBIAN_FRONTEND=noninteractive \
            apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends nginx
    else
        log "nginx 已安装：$(nginx -v 2>&1)，跳过安装（幂等）"
    fi

    # 非 Debian 布局（CentOS 的 conf.d、自编译 nginx）没有 sites-available。
    # 原来直接往 sites-available 写 → "No such file or directory" 让整步以
    # 第 137 行失败收场，而这时服务本身其实已经装好了，纯粹是白死一次。
    if [ ! -d /etc/nginx/sites-available ]; then
        if [ -d /etc/nginx/conf.d ]; then
            warn "本机是 conf.d 布局（无 sites-available），脚本不改动 nginx，请手工加反代："
        else
            warn "本机 nginx 布局不认识（既无 sites-available 也无 conf.d），脚本不改动 nginx。"
        fi
        warn "  在 server 块里加："
        warn "    location / { proxy_pass http://127.0.0.1:${EFFECTIVE_PORT}; }"
        warn "    client_max_body_size 64m;"
        warn "    proxy_read_timeout 180s;"
        warn "  或者用 --no-nginx 重跑，本步骤会直接跳过。"
        CURRENT_STEP="配置 nginx 反代（布局未知，已跳过）"
        return 0
    fi

    local template="${SCRIPT_DIR}/nginx-imgtrans.conf"
    [ -f "${template}" ] || die "找不到 nginx 模板 ${template}"
    local avail="/etc/nginx/sites-available/${UNIT_NAME}.conf"
    local enabled="/etc/nginx/sites-enabled/${UNIT_NAME}.conf"

    local conf_new="${avail}.new"
    run_sh "sed -e $(q "s|@PORT@|$(esc "${EFFECTIVE_PORT}")|g") $(q "${template}") > $(q "${conf_new}")"
    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '[dry-run] 校验非空后 mv -f %s %s\n' "$(q "${conf_new}")" "$(q "${avail}")"
        printf '[dry-run] ln -sfn %s %s\n' "$(q "${avail}")" "$(q "${enabled}")"
    else
        [ -s "${conf_new}" ] || { rm -f "${conf_new}"; die "渲染出的 nginx 站点为空（sed 失败？）"; }
        if grep -q '@[A-Z_]*@' "${conf_new}"; then
            rm -f "${conf_new}"
            die "nginx 站点仍有未替换的占位符（模板与脚本版本不一致）"
        fi
        run mv -f "${conf_new}" "${avail}"
        run ln -sfn "${avail}" "${enabled}"
    fi

    # 关掉默认站点：它带 default_server，任何按 IP 访问的请求都会落到 nginx 欢迎页，
    # 表现为"配置明明对但打开是 Welcome to nginx"。备份后再删，可回滚。
    if [ -e /etc/nginx/sites-enabled/default ]; then
        log "禁用 nginx 默认站点（已备份为 /etc/nginx/sites-available/default.imgtrans.bak）"
        run cp -f /etc/nginx/sites-available/default /etc/nginx/sites-available/default.imgtrans.bak 2>/dev/null || true
        run rm -f /etc/nginx/sites-enabled/default
    fi

    # 先 -t 再 reload：配置错时不要把一个还在正常服务的 nginx 弄挂
    if [ "${DRY_RUN}" -eq 0 ]; then
        if ! nginx -t 2>&1; then
            die "nginx 配置校验失败（见上）。可先 rm -f ${enabled} 回退，再 nginx -t 确认。"
        fi
    else
        printf '[dry-run] nginx -t\n'
    fi
    # reload 要求 nginx 正在运行；刚装好的机器 nginx 可能还没启动过，
    # 那会以"最后一步失败"的形式毁掉前面全部成果。reload-or-restart 两者都覆盖。
    run systemctl reload-or-restart nginx

    ok "nginx 已生效（站点：${enabled} → 127.0.0.1:${EFFECTIVE_PORT}）"
}

# ===========================================================================
# 步骤 11：收尾提示
# ===========================================================================
step_summary() {
    CURRENT_STEP="收尾"
    log "${CURRENT_STEP}"

    # 防火墙只提示不动手：ufw 一开就可能把运维自己的 SSH 关在门外
    local ufw_hint=""
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        ufw_hint="  ufw 已启用。确认只放行 22/80/443，不要放行 ${EFFECTIVE_PORT}：
      ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw delete allow ${EFFECTIVE_PORT}/tcp"
    else
        ufw_hint="  若启用 ufw：ufw allow 22/tcp; ufw allow 80/tcp; ufw allow 443/tcp（**不要**放行 ${EFFECTIVE_PORT}）"
    fi

    # --no-nginx 时对外入口不存在，别让运维照着"经 nginx"那条命令去猜
    local proxy_note
    if [ "${ENABLE_NGINX}" -eq 1 ]; then
        proxy_note="      curl -s http://127.0.0.1/healthz                          # 经 nginx，期望同上"
    else
        proxy_note="      （本次未配置 nginx：服务只监听 127.0.0.1:${EFFECTIVE_PORT}，请自行反代；
        反代时必须设 client_max_body_size 64m 与 proxy_read_timeout 180s）"
    fi

    # 未就绪提示 / 卸载指引都要条件化：卸载段里 `rm -rf ${INSTALL_DIR}` 和
    # `userdel ${RUN_USER}` 一旦被无脑复制，会删掉别人的站点或系统账号。
    local notready_note="" uninstall_note=""
    if [ "${SERVICE_NOT_READY}" -eq 1 ]; then
        notready_note="
$(printf '%s' "${c_red}")!! 服务尚未通过 /healthz —— 先不要重装，按下面顺序看：$(printf '%s' "${c_reset}")
   !! 本脚本以**退出码 3** 结束（安装本身成功，只是服务未就绪；
      接 CI / Ansible / \`&&\` 链条时不要把它当成功，也不要删目录重装）
   journalctl -u ${UNIT_NAME} -n 100 --no-pager -l
   ss -ltnp | grep ${EFFECTIVE_PORT}
   curl -sv http://127.0.0.1:${EFFECTIVE_PORT}/healthz
   常见原因：模型仍在下载（.models 为空时要下 140MB）、端口被占、GPU 配错。
"
    fi

    if [ "${CREATED_DIR}" = "1" ]; then
        uninstall_note="  rm -rf ${INSTALL_DIR}          # 本次由脚本创建，删目录 = 连带清掉 .venv / .models / .env"
    else
        uninstall_note="  # ${INSTALL_DIR} 在安装前就已存在，不要整目录删；只清理 .venv/.models/.tools/.env"
    fi

    # 密钥留档只在本次真的生成过 .env 时才存在，别对着沿用来的 .env 瞎指路
    local key_note="" key_hint=""
    if [ -f "${KEY_FILE}" ]; then
        key_note="密钥留档      : ${KEY_FILE}（600，仅 root 可读；sudo cat 查看）"
        key_hint="  ${KEY_FILE} 是根专用留档，取走后可自行删除"
    fi

    cat <<EOF

$(printf '%s' "${c_green}")================= 部署完成 =================$(printf '%s' "${c_reset}")
${notready_note}
安装目录      : ${INSTALL_DIR}
运行用户      : ${RUN_USER}:${RUN_GROUP}
监听端口      : 127.0.0.1:${EFFECTIVE_PORT}（仅回环，由 nginx 反代）
配置文件      : ${ENV_FILE}（600，含明文密钥）
${key_note}
服务单元      : ${UNIT_PATH}
日志          : journalctl -u ${UNIT_NAME} -f

上线验证（逐条跑一遍）：
  systemctl status ${UNIT_NAME} --no-pager -l
  curl -s http://127.0.0.1:${EFFECTIVE_PORT}/healthz        # 期望 {"ok":true,"device":"cpu"}
${proxy_note}

真实请求（12~20s，OCR 占 91%，别用默认超时）：
  KEY=\$(sed -n 's/^IMGTRANS_API_KEY=//p' ${ENV_FILE})
  curl -s -m 180 -X POST http://127.0.0.1/v1/translate/img \\
    -H "Content-Type: application/json" -H "X-Api-Key: \$KEY" \\
    -d '{"lang":"en","img":"https://example.com/banner.jpg"}' -o resp.json

安全提醒：
${ufw_hint}
  轮换密钥：改 ${ENV_FILE} 里 IMGTRANS_API_KEY → systemctl restart ${UNIT_NAME}
  .env 权限必须是 600：chmod 600 ${ENV_FILE}
${key_hint}

彻底卸载：
  systemctl disable --now ${UNIT_NAME} && rm -f ${UNIT_PATH} && systemctl daemon-reload
  rm -f /etc/nginx/sites-enabled/${UNIT_NAME}.conf /etc/nginx/sites-available/${UNIT_NAME}.conf
  nginx -t && systemctl reload nginx
${uninstall_note}
$(if [ "${CREATED_USER}" = "1" ]; then echo "  userdel ${RUN_USER}"; else echo "  # 用户 ${RUN_USER} 安装前已存在，保留（可能有其它服务在用）"; fi)

详见 docs/ubuntu-deploy.md
EOF

    CURRENT_STEP="完成"
    ok "全部步骤执行完毕"
}

# ===========================================================================
# 主流程
# ===========================================================================
main() {
    ORIG_ARGS="$*"
    log "imgtrans 部署开始（${SCRIPT_NAME}）"
    log "源目录：${SRC_DIR}"
    log "目标目录：${INSTALL_DIR}｜端口：${PORT}｜用户：${RUN_USER}｜GPU：${ENABLE_GPU}｜nginx：${ENABLE_NGINX}｜dry-run：${DRY_RUN}"
    if [ "${DRY_RUN}" -eq 1 ]; then
        warn "dry-run 模式：只打印动作，不修改系统（部分「是否已安装」的判断会显示为需要安装）"
    fi

    step_preflight
    step_apt
    step_user
    step_uv
    step_files
    step_sync
    step_env
    step_doctor
    step_systemd
    step_nginx
    step_summary

    # 退出码 3：装完了但服务没就绪（可能只是模型还在下，300s 档）。
    # 与"某一步失败"(1) 区分开：此时安装本身是成功的，删目录重装是错误动作；
    # 但也不能 exit 0 —— 否则 CI / Ansible / `install.sh && next` 会把
    # "NRestarts 在涨、healthz 从没通"判成部署成功。
    #
    # 这里必须是 exit 而不是 return：main 在脚本末尾以简单命令形式调用，
    # `return 3` 会让 set -e 再触发一次 ERR trap → on_err() 打出"手动回退指引"
    # （里头有 rm -rf 安装目录），并把退出码改成 1 —— 与"不要重装"正好相反。
    # 实测：return 3 → exit 1；exit 3 → exit 3。
    if [ "${SERVICE_NOT_READY}" -eq 1 ]; then
        exit 3
    fi
    return 0
}

main "$@"
