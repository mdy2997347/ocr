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
