"""语言代码映射（PHP ``Google::$auto_list`` / ``convertLang()`` /
``convertLangCode()`` 的移植）。

四套语义要区分清楚，混用会导致 400/504：

* :data:`SUPPORTED_LANGS` —— 服务**对外承诺支持的目标语言码**，唯一权威清单；
* :func:`normalize_lang` —— 把调用方传入的目标语言码归一成清单里的规范码；
* :func:`lookup_auto` —— 语言识别结果 → Google 翻译码，用于 ``auto_list`` 表；
* :func:`convert_lang` / :func:`convert_lang_code` —— 上送 Google 前的归一化。

与原 PHP 实现的差异（已与主理人确认）
------------------------------------
1. ``'vi' => 'iv'`` 是原代码笔误，本模块修正为 ``'vi' => 'vi'``；
2. ``convertLang()`` 保留 ``HE -> IW`` 的历史映射（Google 旧码）；
3. 原 ``auto()`` 里的 ``strtolower($auto)`` 与表中 ``zh-Hans`` / ``zh-Hant``
   两个混合大小写键不匹配（PHP 数组键大小写敏感），导致简繁识别永远失败。
   本移植的 :func:`lookup_auto` 做大小写无关匹配，修正该缺陷；
4. 新增 :data:`SUPPORTED_LANGS` 目标语言白名单（业务侧定的 34 个码），
   PHP 原版没有这个概念，任何字符串都会被透传给 Google，拼错只能等远端报错。
"""

from __future__ import annotations

__all__ = [
    "AUTO_LIST",
    "BAIDU_LANGS",
    "CONVERT_CODES",
    "SUPPORTED_LANGS",
    "is_supported_lang",
    "lookup_auto",
    "normalize_lang",
    "convert_lang",
    "convert_lang_code",
    "is_supported_source",
    "to_baidu_lang",
]

#: 对外支持的**目标**语言码（业务口径，顺序即文档顺序）。
#:
#: 这张表是硬白名单：CLI / HTTP 传进来的 ``target`` 必须能归一到其中之一，
#: 否则直接报 ``E_LANG``，绝不把没见过的字符串原样甩给 Google——
#: 那样只会得到一个 400 或一整批原文占位，排查成本远高于当场拒绝。
#:
#: **不含中文**：本服务的定位是把（中文）原图译成外语，中文只作为源语言，
#: 见 :data:`AUTO_LIST` 里的 ``zh-Hans`` / ``zh-Hant`` 两条。
SUPPORTED_LANGS: tuple[str, ...] = (
    "en",
    "ar",
    "bg",
    "bn",
    "cs",
    "da",
    "de",
    "el",
    "es",
    "et",
    "fi",
    "fr",
    "he",
    "hi",
    "hr",
    "hu",
    "id",
    "it",
    "ja",
    "ka",
    "ko",
    "ms",
    "nl",
    "no",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "sl",
    "sv",
    "th",
    "tr",
    "uk",
    "vi",
)

#: 历史/别名码 → 规范码。调用方常常照着旧文档传 ``iw``、``in`` 这类
#: Google 早已废弃的码，直接拒绝会显得莫名其妙，转一下就通。
_LANG_ALIASES: dict[str, str] = {
    "iw": "he",
    "in": "id",
    "no-bok": "no",
    "no-nyn": "no",
    "zh": "zh-cn",  # 仅用于源语言识别，不在目标白名单内
}

_SUPPORTED_SET = frozenset(SUPPORTED_LANGS)

#: 语言识别结果（小写/BCP-47 风格）→ Google 翻译语言码。
#:
#: 主体由 :data:`SUPPORTED_LANGS` 生成，再补两类条目：
#:
#: * 检测器会输出的细粒度标签（``el-monoton`` / ``pt-br`` / ``ms-arab`` …）；
#: * **只在源侧出现**的中文（``zh-Hans`` / ``zh-Hant``）——目标语言里没有它。
AUTO_LIST: dict[str, str] = {code: code for code in SUPPORTED_LANGS}
AUTO_LIST.update(
    {
        "el-monoton": "el",
        "el-polyton": "el",
        # Google web 接口对希伯来语仍认旧码 iw；对外统一用 he，这里只做上送映射
        "he": "iw",
        "ms-arab": "ms",
        "ms-latn": "ms",
        "nb": "no",
        "nn": "no",
        "pt-br": "pt",
        "pt-pt": "pt",
        "zh-Hans": "zh-cn",
        "zh-Hant": "zh-tw",
    }
)

#: PHP ``getConvertCodes()``：规范码 → 别名列表（均为大写，见 :func:`convert_lang`）。
CONVERT_CODES: dict[str, tuple[str, ...]] = {
    "IW": ("HE",),
}

#: ``AUTO_LIST`` 的小写键索引，供 :func:`lookup_auto` 做大小写无关匹配。
_AUTO_LIST_LOWER: dict[str, str] = {key.lower(): value for key, value in AUTO_LIST.items()}

#: 本项目规范码 → 百度翻译语言码。
#:
#: 百度**不用** ISO 639-1，混用会拿到 ``58001 译文语言方向不支持``：
#: 日语是 ``jp`` 不是 ``ja``，韩语是 ``kor`` 不是 ``ko``，法语是 ``fra``
#: 不是 ``fr``，西班牙语是 ``spa`` 不是 ``es``……这张表逐个核对过官方文档。
#:
#: 与 :data:`AUTO_LIST` 的另一个关键差异：``he`` 在这里是 ``heb``，
#: 而 Google 那边要的是废弃码 ``iw``。**两套映射不能互相借用**。
BAIDU_LANGS: dict[str, str] = {
    "en": "en",
    "ar": "ara",
    "bg": "bul",
    "bn": "ben",
    "cs": "cs",
    "da": "dan",
    "de": "de",
    "el": "el",
    "es": "spa",
    "et": "est",
    "fi": "fin",
    "fr": "fra",
    "he": "heb",
    "hi": "hi",
    "hr": "hrv",
    "hu": "hu",
    "id": "id",
    "it": "it",
    "ja": "jp",
    "ka": "geo",
    "ko": "kor",
    "ms": "may",
    "nl": "nl",
    "no": "nor",
    "pl": "pl",
    "pt": "pt",
    "ro": "rom",
    "ru": "ru",
    "sk": "slo",
    "sl": "slv",
    "sv": "swe",
    "th": "th",
    "tr": "tr",
    "uk": "ukr",
    "vi": "vie",
}

#: 只用于**源**语言（或百度的特殊码）的映射。
#:
#: 中文不在 :data:`SUPPORTED_LANGS` 里——本服务只把中文当源语言。
#: ``zh-tw`` / ``zh-hk`` 在百度那边叫 ``cht``（繁体），不是 ``zh-TW``。
#: ``yue``（粤语）/ ``wyw``（文言文）是百度独有的码，调用方直接传也能过。
_BAIDU_SOURCE_ALIASES: dict[str, str] = {
    "auto": "auto",
    "zh": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "zh-tw": "cht",
    "zh-hk": "cht",
    "zh-hant": "cht",
    "cht": "cht",
    "yue": "yue",
    "wyw": "wyw",
}


def normalize_lang(code: str | None) -> str:
    """把调用方传入的语言码归一成 :data:`SUPPORTED_LANGS` 里的规范码。

    接受并归一的形式：``EN`` / `` en `` / ``pt-BR`` / ``pt_br`` / ``iw``（旧码）。
    归一之后才能拿去查白名单——直接比对原文会把 ``en-US`` 判成不支持。

    Args:
        code: 调用方传入的语言码。

    Returns:
        规范码；无法归一（即不支持）返回 ``""``。
    """
    if not code or not isinstance(code, str):
        return ""
    key = code.strip().lower().replace("_", "-")
    if not key:
        return ""
    key = _LANG_ALIASES.get(key, key)
    if key in _SUPPORTED_SET:
        return key
    # 带地区/文字子标签的（en-US、pt-BR、zh-Hant）退到主语言子标签再试。
    primary = key.split("-", 1)[0]
    primary = _LANG_ALIASES.get(primary, primary)
    return primary if primary in _SUPPORTED_SET else ""


def is_supported_lang(code: str | None) -> bool:
    """判断是否是可作为**目标语言**的受支持码（见 :data:`SUPPORTED_LANGS`）。"""
    return bool(normalize_lang(code))


def lookup_auto(code: str | None) -> str | None:
    """把语言识别结果映射为 Google 语言码。

    Args:
        code: 识别出的语言标签，例如 ``"zh-Hans"`` / ``"pt-BR"`` / ``"EN"``。

    Returns:
        对应的 Google 语言码；无法识别返回 ``None``。
    """
    if not code or not isinstance(code, str):
        return None
    key = code.strip().lower()
    if not key:
        return None
    if key in _AUTO_LIST_LOWER:
        return _AUTO_LIST_LOWER[key]
    # 退一步：只用主语言子标签（"pt-br-x" → "pt"、"en-us" → "en"）。
    primary = key.split("-", 1)[0]
    if primary in _AUTO_LIST_LOWER:
        return _AUTO_LIST_LOWER[primary]
    return None


def convert_lang(code: str | None) -> str:
    """web 通道语言码归一化（PHP ``convertLang()``）。

    先转大写，再按 :data:`CONVERT_CODES` 把别名换成规范码
    （``he`` → ``IW``），其余原样返回大写形式。

    Args:
        code: 语言代码。

    Returns:
        大写语言码；``None``/空串返回 ``""``。
    """
    if not code or not isinstance(code, str):
        return ""
    upper = code.strip().upper()
    if not upper:
        return ""
    for canonical, aliases in CONVERT_CODES.items():
        if upper in aliases:
            return canonical
    return upper


def convert_lang_code(code: str | None) -> str:
    """Cloud API 语言码归一化（PHP ``convertLangCode()``）。

    * ``zh-cn`` → ``zh-CN``
    * ``zh-hk`` / ``zh-tw`` → ``zh-TW``
    * 其余取前两个字符并小写（``en-US`` → ``en``）

    Args:
        code: 语言代码。

    Returns:
        归一化后的语言码；``None``/空串返回 ``""``。
    """
    if not code or not isinstance(code, str):
        return ""
    lowered = code.strip().lower()
    if not lowered:
        return ""
    if lowered == "zh-cn":
        return "zh-CN"
    if lowered in ("zh-hk", "zh-tw"):
        return "zh-TW"
    return lowered[:2]


def is_supported_source(code: str | None) -> bool:
    """判断代码是否在 ``auto_list`` 白名单内（大小写无关）。

    Args:
        code: 语言代码。

    Returns:
        是否支持。
    """
    return lookup_auto(code) is not None


def to_baidu_lang(code: str | None) -> str:
    """把语言码转成百度翻译认的码。

    接受 ``EN`` / `` en `` / ``ja-JP`` / ``zh`` / ``auto`` 等形式。
    先用完整码查 :data:`BAIDU_LANGS` 与 :data:`_BAIDU_SOURCE_ALIASES`，
    再退到主语言子标签（``ja-JP`` → ``ja`` → ``jp``）。

    Args:
        code: 语言代码。

    Returns:
        百度语言码；无法映射返回 ``""``（调用方据此报错或回落 ``auto``）。
    """
    if not code or not isinstance(code, str):
        return ""
    key = code.strip().lower().replace("_", "-")
    if not key:
        return ""
    if key in _BAIDU_SOURCE_ALIASES:
        return _BAIDU_SOURCE_ALIASES[key]
    if key in BAIDU_LANGS:
        return BAIDU_LANGS[key]
    primary = key.split("-", 1)[0]
    if primary in _BAIDU_SOURCE_ALIASES:
        return _BAIDU_SOURCE_ALIASES[primary]
    return BAIDU_LANGS.get(primary, "")
