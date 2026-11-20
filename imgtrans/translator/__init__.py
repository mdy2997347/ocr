r"""翻译层：把文本块批量翻译成目标语言。

对外只暴露契约（:mod:`~imgtrans.translator.base`）与三个实现：

* :class:`~imgtrans.translator.google_web.GoogleWebTranslator` —— 免费 web 接口（主用）；
* :class:`~imgtrans.translator.google_cloud.GoogleCloudTranslator` —— 官方 Cloud API（可选）；
* :class:`~imgtrans.translator.baidu.BaiduTranslator` —— 百度通用文本翻译（可选）。

前两者移植自 PHP ``google.php``（namespace ``Otc\Translate``，类 ``Google``）。
百度通道是后加的：香港/内地机房直连 Google 常被 429，百度按 ``\n`` 分行返回
且带 ``src`` 原文，天然适合"一张图几十个文本块"的批量翻译。
"""

from __future__ import annotations

from typing import Any

from .baidu import BaiduTranslator
from .base import BaseTranslator, TranslationError, TranslatorUnavailable
from .google_cloud import GoogleCloudTranslator
from .google_web import GoogleWebTranslator
from .lang_map import (
    AUTO_LIST,
    BAIDU_LANGS,
    SUPPORTED_LANGS,
    convert_lang,
    convert_lang_code,
    is_supported_lang,
    lookup_auto,
    normalize_lang,
    to_baidu_lang,
)
from .google_tk import DEFAULT_TKK, TKKSnapshot, build_tk

__all__ = [
    "AUTO_LIST",
    "BAIDU_LANGS",
    "BaseTranslator",
    "SUPPORTED_LANGS",
    "BaiduTranslator",
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
    "to_baidu_lang",
]

#: 通道别名 → 实现类。
_TRANSLATORS: dict[str, type] = {
    "web": GoogleWebTranslator,
    "api": GoogleWebTranslator,
    "free": GoogleWebTranslator,
    "cloud": GoogleCloudTranslator,
    "nmt": GoogleCloudTranslator,
    "baidu": BaiduTranslator,
    "baidu_api": BaiduTranslator,
}


def get_translator(kind: str = "web", **kwargs: Any) -> Any:
    """翻译器工厂。

    Args:
        kind: 通道类型，可选 ``"web"`` / ``"api"`` / ``"free"``（均为
            :class:`GoogleWebTranslator`，``"free"`` 会把 ``prefer`` 置为
            ``"free"``）、``"cloud"`` / ``"nmt"``
            （:class:`GoogleCloudTranslator`）或 ``"baidu"`` / ``"baidu_api"``
            （:class:`BaiduTranslator`，需 ``appid`` + ``secret``）。
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
    baidu_appid: str | None = None,
    baidu_secret: str | None = None,
    baidu_qps: float = 1.0,
) -> Any:
    """按通道名建翻译器，屏蔽"各条通道构造参数完全不同"的差异。

    存在的理由：``serve.py`` 原来写死 ``get_translator("web", ...)``，
    于是把线上切到官方 Cloud 通道就得改代码再发版。可通道是**部署期**决定的
    （出口 IP 干不干净、有没有付费额度），应该由配置决定，不该由源码决定。

    Args:
        kind: ``web`` / ``api`` / ``free``（免费 web 接口，会被 429）；
            ``cloud`` / ``nmt``（官方 Cloud Translation v2）；
            ``baidu`` / ``baidu_api``（百度通用文本翻译）。
        proxy: 仅 web 与 baidu 通道生效。cloud 走 google-cloud SDK，代理由其
            自身环境变量（``HTTPS_PROXY``）控制，传进来会被忽略。
        timeout: 仅 web / baidu 通道生效。
        google_key: cloud 通道的 API Key。
        google_credentials: cloud 通道的服务账号 JSON 路径或内容。
        google_project: cloud 通道的 GCP 项目 ID。
        baidu_appid: baidu 通道的 APP ID（纯数字，**不是密钥**）。
        baidu_secret: baidu 通道的密钥。
        baidu_qps: baidu 通道的请求速率上限。标准版 1 QPS，高级版可设 10。

    Returns:
        翻译器实例。cloud 通道**构造是惰性的**：缺 ``google-cloud-translate``
        依赖时不在这里失败，而是在首次翻译时抛
        :class:`~imgtrans.translator.base.TranslatorUnavailable` 并给出安装指引。
        baidu 通道相反，**构造期就校验凭证**：缺 APP ID / 密钥直接抛
        ``ValueError``，让服务在启动阶段退出，而不是跑完 OCR 才发现翻译必失败。

    Raises:
        ValueError: 通道名不认识，或 baidu 通道缺凭证。
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
    if key in ("baidu", "baidu_api"):
        return get_translator(
            key,
            appid=baidu_appid or "",
            secret=baidu_secret or "",
            proxy=proxy,
            timeout=timeout,
            qps=baidu_qps,
        )
    raise ValueError(
        f"unknown translator kind: {kind!r}; "
        "expected one of ['web', 'api', 'free', 'cloud', 'nmt', 'baidu', 'baidu_api']"
    )
