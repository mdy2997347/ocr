r"""``.env`` 加载器（零依赖）。

为什么不直接上 ``python-dotenv``
--------------------------------
只需要读一个文件里的 ``KEY=VALUE``，为此多装一个依赖不值当——而且这个包
最终要复制到 GPU 生产机上，依赖越少越好装。

为什么不在库导入时自动加载
--------------------------
``imgtrans`` 是库，被单元测试和 CLI 共用。如果 ``import imgtrans`` 就偷偷改
``os.environ``，测试之间会互相污染（比如某个测试设置了 ``IMGTRANS_API_KEY``，
后续所有用例都会开始校验鉴权）。所以只由**入口脚本**显式调用
:func:`load_env`：``scripts/serve.py`` / ``scripts/translate_image.py``。

格式支持（够用就好）：:

    # 注释
    IMGTRANS_API_KEY=abc123
    IMGTRANS_PROXY="http://127.0.0.1:10809"
    export IMGTRANS_PORT=8000
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

__all__ = ["parse_env", "load_env", "apply_model_dir", "default_env_path", "ENV_MODEL_DIR"]

logger = logging.getLogger(__name__)

#: 项目内配置项：模型缓存目录。
#: 不直接叫 ``PADDLE_PDX_CACHE_HOME``，是因为那是 PaddleX 的内部变量名，
#: 写在自己的配置里容易被上游改名；由 :func:`apply_model_dir` 做一次映射。
ENV_MODEL_DIR = "IMGTRANS_MODEL_DIR"

#: PaddleX 实际读取的变量（``paddlex/utils/cache.py`` 模块级求值）。
PDX_CACHE_VAR = "PADDLE_PDX_CACHE_HOME"

#: 项目根目录下的默认配置文件（本文件位于 ``imgtrans/``，上一级即根目录）。
default_env_path = Path(__file__).resolve().parents[1] / ".env"


def parse_env(text: str) -> dict[str, str]:
    """解析 ``.env`` 文本，返回键值对。

    刻意不做变量插值（``${OTHER}``）：配置项少，插值带来的"值里藏变量"
    反而会让排障变难。空值（``KEY=``）保留为空串。
    """
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # 去掉成对的引号，但不要动值内部的引号（路径里可能有单引号）
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def load_env(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    """把 ``.env`` 里的键值写进 ``os.environ``。

    Args:
        path: 配置文件路径，默认项目根目录的 ``.env``。
        override: 是否覆盖已存在的环境变量。默认 ``False``——命令行参数和
            systemd 注入的环境变量优先级必须高于配置文件，否则线上想临时
            改一个值还得去改文件。

    Returns:
        实际写入的键值对（未写入的项也会出现在返回值里，便于日志确认）。
    """
    env_path = Path(path) if path is not None else default_env_path
    if not env_path.is_file():
        logger.debug("未找到 %s，跳过 .env 加载", env_path)
        return {}

    try:
        text = env_path.read_text(encoding="utf-8-sig")
    except OSError as exc:  # pragma: no cover - 权限问题极少见
        logger.warning("读取 %s 失败: %s", env_path, exc)
        return {}

    values = parse_env(text)
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    logger.info(
        ".env 已加载: %s（%d 项，%s 已存在的环境变量）",
        env_path,
        len(values),
        "覆盖" if override else "不覆盖",
    )
    return values


def apply_model_dir(path: str | Path | None = None) -> Path | None:
    """把 ``IMGTRANS_MODEL_DIR`` 映射成 PaddleX 认的 ``PADDLE_PDX_CACHE_HOME``。

    为什么需要这一步：模型默认下到 ``~/.paddlex``（几百 MB），"独立部署"就
    破了功——同一台机器上的另一个 paddle 项目会共用这份缓存，版本一换就互相
    干扰。把目录写进项目自己的 ``.env``，删项目目录即彻底清除。

    **必须在 import paddle 之前调用**：``paddlex.utils.cache`` 在模块导入时就
    把 ``CACHE_DIR`` 求值成了常量，之后再改环境变量不会生效。入口脚本里
    ``load_env()`` 之后紧跟这一步，正好早于任何 paddle 导入（引擎是惰性导入）。

    Args:
        path: 显式目录；``None`` 时读环境变量 ``IMGTRANS_MODEL_DIR``。

    Returns:
        实际生效的绝对路径；未配置时返回 ``None``（沿用 PaddleX 默认）。

    已显式设置 ``PADDLE_PDX_CACHE_HOME`` 时不覆盖：那个变量是更底层的逃生口，
    部署脚本或容器编排注入时应该优先。
    """
    raw = path if path is not None else os.environ.get(ENV_MODEL_DIR, "")
    raw = str(raw).strip()
    if not raw:
        return None

    existing = os.environ.get(PDX_CACHE_VAR, "").strip()
    if existing:
        logger.info(
            "已显式设置 %s=%s，忽略 %s", PDX_CACHE_VAR, existing, ENV_MODEL_DIR
        )
        return Path(existing).expanduser()

    target = Path(raw).expanduser()
    if not target.is_absolute():
        # 相对路径按**项目根**解析，而不是 cwd：`python /path/to/ocr/scripts/serve.py`
        # 从别的目录启动是常态（systemd 的 WorkingDirectory 也未必是项目根），
        # 按 cwd 解析会把几百 MB 模型悄悄建到 / 或 ~ 下面。
        target = default_env_path.parent / target
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # 目录建不出来不该让服务起不来，只是模型会落回默认位置
        logger.warning("创建模型目录 %s 失败(%s)，沿用 PaddleX 默认缓存", target, exc)
        return None

    resolved = target.resolve()
    os.environ[PDX_CACHE_VAR] = str(resolved)
    logger.info("模型缓存目录: %s（来自 %s）", resolved, ENV_MODEL_DIR)
    return resolved
