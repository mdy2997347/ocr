r"""翻译层：把文本块批量翻译成目标语言。

对外只暴露契约（:mod:`~imgtrans.translator.base`）与两个实现：

* :class:`~imgtrans.translator.google_web.GoogleWebTranslator` —— 免费 web 接口（主用）；
* :class:`~imgtrans.translator.google_cloud.GoogleCloudTranslator` —— 官方 Cloud API（可选）。

移植自 PHP ``google.php``（namespace ``Otc\Translate``，类 ``Google``）。
"""

from __future__ import annotations

from typing import Any

from .base import BaseTranslator, TranslationError, TranslatorUnavailable
from .google_cloud import GoogleCloudTranslator
from .google_web import GoogleWebTranslator
from .lang_map import (
    AUTO_LIST,
    SUPPORTED_LANGS,
    convert_lang,
    convert_lang_code,
    is_supported_lang,
    lookup_auto,
    normalize_lang,
)
from .google_tk import DEFAULT_TKK, TKKSnapshot, build_tk

__all__ = [
    "AUTO_LIST",
    "BaseTranslator",
    "SUPPORTED_LANGS",
    "DEFAULT_TKK",
    "GoogleCloudTranslator",
    "GoogleWebTranslator",
    "TKKSnapshot",
    "TranslationError",
    "TranslatorUnavailable",
    "build_tk",
    "build_translator",
    "convert_lang",
    "convert_lang_code",
    "get_translator",
    "is_supported_lang",
    "lookup_auto",
    "normalize_lang",
]

#: 通道别名 → 实现类。
_TRANSLATORS: dict[str, type] = {
    "web": GoogleWebTranslator,
    "api": GoogleWebTranslator,
    "free": GoogleWebTranslator,
    "cloud": GoogleCloudTranslator,
    "nmt": GoogleCloudTranslator,
}


def get_translator(kind: str = "web", **kwargs: Any) -> Any:
    """翻译器工厂。

    Args:
        kind: 通道类型，可选 ``"web"`` / ``"api"`` / ``"free"``（均为
            :class:`GoogleWebTranslator`，``"free"`` 会把 ``prefer`` 置为
            ``"free"``）或 ``"cloud"`` / ``"nmt"``
            （:class:`GoogleCloudTranslator`）。
        **kwargs: 透传给实现类的构造参数。

    Returns:
        翻译器实例。

    Raises:
        ValueError: 未知的 ``kind``。
    """
    key = (kind or "").strip().lower()
    if key not in _TRANSLATORS:
        raise ValueError(
            f"unknown translator kind: {kind!r}; "
            f"expected one of {sorted(_TRANSLATORS)}"
        )
    factory = _TRANSLATORS[key]
    if key == "free":
        kwargs.setdefault("prefer", "free")
    return factory(**kwargs)


def build_translator(
    kind: str = "web",
    *,
    proxy: str | None = None,
    timeout: float = 20.0,
    google_key: str | None = None,
    google_credentials: str | None = None,
    google_project: str | None = None,
) -> Any:
    """按通道名建翻译器，屏蔽"两条通道构造参数完全不同"的差异。

    存在的理由：``serve.py`` 原来写死 ``get_translator("web", ...)``，
    于是把线上切到官方 Cloud 通道就得改代码再发版。可通道是**部署期**决定的
    （出口 IP 干不干净、有没有付费额度），应该由配置决定，不该由源码决定。

    Args:
        kind: ``web`` / ``api`` / ``free``（免费 web 接口，会被 429）
            或 ``cloud`` / ``nmt``（官方 Cloud Translation v2）。
        proxy: 仅 web 系列生效。cloud 走 google-cloud SDK，代理由其自身
            环境变量（``HTTPS_PROXY``）控制，传进来会被忽略。
        timeout: 仅 web 系列生效。
        google_key: cloud 通道的 API Key。
        google_credentials: cloud 通道的服务账号 JSON 路径或内容。
        google_project: cloud 通道的 GCP 项目 ID。

    Returns:
        翻译器实例。cloud 通道**构造是惰性的**：缺 ``google-cloud-translate``
        依赖时不在这里失败，而是在首次翻译时抛
        :class:`~imgtrans.translator.base.TranslatorUnavailable` 并给出安装指引。

    Raises:
        ValueError: 通道名不认识。启动期就报，好过运行到第一张图才发现拼错。
    """
    key = (kind or "web").strip().lower()
    if key in ("web", "api", "free"):
        return get_translator(key, proxy=proxy, timeout=timeout)
    if key in ("cloud", "nmt"):
        return get_translator(
            key,
            api_key=google_key or None,
            credentials=google_credentials or None,
            project=google_project or None,
        )
    raise ValueError(
        f"unknown translator kind: {kind!r}; "
        "expected one of ['web', 'api', 'free', 'cloud', 'nmt']"
    )
