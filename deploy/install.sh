#!/usr/bin/env bash
# =============================================================================
# imgtrans 图片翻译服务 —— Ubuntu 一键部署脚本
#
# 用法（必须以 root 运行；--dry-run 可不用 root，用于先看一眼要做什么）：
#   sudo bash deploy/install.sh --dry-run          # 只打印将执行的动作，不动系统
#   sudo bash deploy/install.sh                    # 默认 /opt/imgtrans，端口 8000，CPU
#   sudo bash deploy/install.sh --dir /srv/imgtrans --port 8100
#   sudo bash deploy/install.sh --user ocr --gpu   # GPU 机器（需已装 NVIDIA 驱动 + cu126）
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
  --gpu            安装 GPU 版 paddle（--extra gpu，要求驱动 >= 550.54.14 / cu126）
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
    run runuser -u "${RUN_USER}" -- env \
        "HOME=${INSTALL_DIR}" \
        "UV_CACHE_DIR=${UV_CACHE}" \
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
}

# 去掉结尾斜杠，避免拼出 //opt/imgtrans 这种路径
INSTALL_DIR="${INSTALL_DIR%/}"
RUN_GROUP="${RUN_USER}"
derive_paths

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
    run env DEBIAN_FRONTEND=noninteractive \
        apt-get -o DPkg::Lock::Timeout=120 update -y
    run env DEBIAN_FRONTEND=noninteractive \
        apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends "${packages[@]}"

    # 显式验证 libgomp1：这是最容易漏、报错最难读的一项
    if [ "${DRY_RUN}" -eq 0 ]; then
        ldconfig -p 2>/dev/null | grep -q 'libgomp\.so\.1' \
            || die "libgomp1 装上了但 ldconfig 里没有 libgomp.so.1，paddle 会启动失败。请检查 apt 是否真的成功。"
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

    if command -v uv >/dev/null 2>&1; then
        log "uv 已存在：$(uv --version 2>/dev/null || echo '版本未知')，跳过安装（幂等）"
        ok "uv 已就绪"
        return 0
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
        printf '[dry-run] env UV_UNMANAGED_INSTALL=/usr/local/bin bash /tmp/imgtrans-uv-install.XXXXXX.sh\n'
        printf '[dry-run] rm -f /tmp/imgtrans-uv-install.XXXXXX.sh\n'
        ok "uv 将安装到 /usr/local/bin/uv"
        return 0
    fi

    local installer
    installer="$(mktemp /tmp/imgtrans-uv-install.XXXXXX.sh)"
    run curl -fsSL -o "${installer}" https://astral.sh/uv/install.sh
    run env UV_UNMANAGED_INSTALL=/usr/local/bin bash "${installer}"
    run rm -f "${installer}"

    [ -x /usr/local/bin/uv ] || die "uv 安装后不在 /usr/local/bin/uv。若是内网环境，请手动拷 uv 二进制到 /usr/local/bin/uv 后重跑本脚本。"
    log "uv 版本：$(/usr/local/bin/uv --version)"

    ok "uv 已安装到 /usr/local/bin/uv"
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
        "${INSTALL_DIR}/.venv"
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
        if as_user uv python install "${DESIRED_PY}"; then
            PY_SPEC="${DESIRED_PY}"
            py_managed=1
        else
            # 离线/内网下载失败 → 降级到系统解释器，而不是直接失败。
            # 这一步必须显式提示：版本与验证环境不同，出问题时先想到这一点。
            warn "无法通过 uv 下载 Python ${DESIRED_PY}（离线/内网？），改为使用系统解释器"
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

    local sync_rc=0
    as_user uv "${sync_args[@]}" || sync_rc=$?

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
        # 装错版本时服务会启动即退出并说明原因（不会悄悄回落 CPU），这里先探一次更直观
        if [ "${DRY_RUN}" -eq 0 ]; then
            local cuda_ok
            cuda_ok="$(runuser -u "${RUN_USER}" -- "${INSTALL_DIR}/.venv/bin/python" -c \
                'import paddle; print(paddle.device.is_compiled_with_cuda())' 2>&1 || true)"
            case "${cuda_ok}" in
                *True*) log "GPU 版 paddle 校验通过（is_compiled_with_cuda=True）" ;;
                *)
                    warn "GPU 版 paddle 自检未通过：${cuda_ok}"
                    warn "常见原因：驱动版本低于 550.54.14、或装成了 CPU 版。可改 .env 的 IMGTRANS_DEVICE=cpu 先上线。"
                    ;;
            esac
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

# 翻译通道：web（免费 web 接口）/ cloud（官方 Cloud Translation v2）。
# 免费通道抓的是 Google 网页接口，**机房 IP 会被 429 限流**，症状是接口返回
# 200 + 一张原封不动的中文图，日志里只有 circuit_open。生产建议用 cloud。
IMGTRANS_TRANSLATOR=web
# cloud 通道凭证（用 cloud 时取消注释；另外还需把 google-cloud-translate 加进依赖）：
# IMGTRANS_GOOGLE_KEY=
# IMGTRANS_GOOGLE_CREDENTIALS=
# IMGTRANS_GOOGLE_PROJECT=

# 监听地址：只监听回环，由 nginx 反代，不直接对公网暴露服务端口。
# （若不用 nginx 又要被外部访问，改成 0.0.0.0，并自己承担鉴权/限流）
IMGTRANS_HOST=127.0.0.1
IMGTRANS_PORT=${EFFECTIVE_PORT}

# 推理设备：cpu / gpu / gpu:0 / auto（有卡用卡、无卡回落 cpu）
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
    # 关掉 errexit 才有机会拿到退出码并原样打印输出（doctor 有 FAIL 时返回 1）
    set +e
    output="$(runuser -u "${RUN_USER}" -- env "HOME=${INSTALL_DIR}" \
        bash -c "cd $(q "${INSTALL_DIR}") && exec .venv/bin/python scripts/doctor.py" 2>&1)"
    code=$?
    set -e

    printf '%s\n' "${output}"

    if [ "${code}" -ne 0 ]; then
        err "doctor.py 报了 FAIL（退出码 ${code}），先修再上线。"
        cat >&2 <<'EOF'
常见 FAIL 与处理：
  * “依赖 xxx 未安装”        → 重跑 `uv sync --frozen --extra cpu`（GPU 机器 --extra gpu）
  * “字体索引 一个字体都没找到” → apt install fonts-noto-cjk fonts-dejavu-core fonts-noto-core
  * “字体 zh 回退到 DejaVuSans → 会画成豆腐块” → 同上，装 fonts-noto-cjk
  * “推理设备 xxx 不可用”     → 配了 gpu 但装的是 CPU 版 paddle；改 .env 的 IMGTRANS_DEVICE=cpu 或装 GPU 版
EOF
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

    if ! command -v nginx >/dev/null 2>&1; then
        run env DEBIAN_FRONTEND=noninteractive \
            apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends nginx
    else
        log "nginx 已安装：$(nginx -v 2>&1)，跳过安装（幂等）"
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
