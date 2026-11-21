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
from .google_web import GoogleWebTranslator, mask_proxy
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
    "mask_proxy",
    "normalize_lang",
    "target_unavailable_reason",
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


def target_unavailable_reason(translator: Any, target: str | None) -> str | None:
    """问通道"这个目标语言你翻得了吗"；能翻返回 ``None``，否则返回原因。

    通道可以自愿实现 ``unsupported_target_reason(code)`` 来声明自己的能力边界
    ——目前只有百度需要：它按**账号档位**切语言，标准版/高级版 28 个常见语种，
    非常用语种要企业尊享版。没实现该方法的通道一律视为"全都支持"。

    用 ``getattr`` 探测而不是给 :class:`BaseTranslator` 加方法：新增通道时
    不必为了满足协议写一个永远返回 ``None`` 的空实现，也不会因为忘了实现
    而在运行期才炸。

    Args:
        translator: 翻译器实例，可为 ``None``（表示"还不知道通道"，按支持处理）。
        target: 目标语言码，调用方应传已归一的规范码。

    Returns:
        ``None`` 表示可用；否则是一句可直接展示给调用方的原因。
    """
    checker = getattr(translator, "unsupported_target_reason", None)
    if not callable(checker):
        return None
    return checker(target)


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
    baidu_premium: bool = False,
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
        baidu_premium: baidu 账号是否为企业尊享版。默认 ``False`` 时按标准版
            口径处理：非常用语种（11 个，含 tr/id/hi/ka/uk）前置拒绝并给出
            "需尊享版"的提示，而不是等接口回 ``58001`` 那种看不出所以然的错。

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
            premium=baidu_premium,
        )
    raise ValueError(
        f"unknown translator kind: {kind!r}; "
        "expected one of ['web', 'api', 'free', 'cloud', 'nmt', 'baidu', 'baidu_api']"
    )
