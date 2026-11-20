"""字体发现与回退链。

回填译文的字体必须按**目标语言**选，否则会出一整片"豆腐块"（tofu）。
这个模块做两件事：

1. 在常见系统字体目录里找可用字体，按语言给出优先级链；
2. 缓存 ``ImageFont`` 实例（构造大字号字体是系统调用，很贵）。

**为什么不打包字体**：字体授权各不相同，随代码分发有法律风险。
生产环境建议由部署方显式提供字体目录（``font_dirs`` 参数），
本模块只保证"没配也能跑起来"。

回退顺序的设计原则：宁可字形不美观，也不能出 tofu 或问号。
"""

from __future__ import annotations

import logging
import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from PIL import ImageFont

logger = logging.getLogger(__name__)

#: 字体目录最多往下找几层。
#:
#: Linux 的字体目录**天生是多层的**（``/usr/share/fonts/truetype/dejavu/xx.ttf``），
#: 只扫一层会一个字体都找不到，整张图的译文退化成 Pillow 的 10px 位图字体。
#: 这是把服务从 Windows 搬到 Linux 时最容易漏掉、后果又最严重的一处。
#: 深度上限是为了防病态目录：字体树再深也不该有 5 层。
_MAX_SCAN_DEPTH = 4

def _default_font_dirs() -> tuple[str, ...]:
    """按平台给出默认字体目录。

    分平台而不是把所有路径堆在一起：Windows 路径在 Linux 上
    ``expanduser(r"~\\AppData\\...")`` 会展开成 ``/home/u/\\AppData\\...``
    这种垃圾值（虽然 ``is_dir()`` 会挡住，但日志里很难看，也让人误以为扫过）。
    """
    if sys.platform.startswith("win"):
        return (
            r"C:\Windows\Fonts",
            os.path.expanduser(r"~\AppData\Local\Microsoft\Windows\Fonts"),
        )
    if sys.platform == "darwin":
        return (
            "/System/Library/Fonts",
            "/Library/Fonts",
            os.path.expanduser("~/Library/Fonts"),
        )
    return (
        # 发行版字体根目录（Debian/Ubuntu/RHEL/Alpine 都在这里，且是**多层**的）
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        "/usr/share/local/fonts",
        # 用户级（freedesktop 标准，``fc-cache`` 也认）
        os.path.expanduser("~/.local/share/fonts"),
        os.path.expanduser("~/.fonts"),  # 旧标准，仍有人用
    )


#: 各平台的常见字体目录。测试会整体替换它来模拟 Linux 布局。
_SYSTEM_FONT_DIRS: tuple[str, ...] = _default_font_dirs()

#: 环境变量：额外字体目录，优先级高于系统目录。
#:
#: 容器部署几乎一定要用它——镜像里通常把字体放 ``/opt/fonts``，
#: 而不是往 ``/usr/share/fonts`` 里塞（那个目录可能被基础镜像重置）。
ENV_FONT_DIRS = "IMGTRANS_FONT_DIRS"

_FONT_SUFFIXES = frozenset((".ttf", ".ttc", ".otf", ".otc"))


def _split_dir_list(raw: str) -> list[str]:
    """切分目录列表，同时认 ``;`` 与 ``:``，且不切坏 Windows 盘符。

    不直接用 ``os.pathsep``：配置经常是跨平台复制粘贴的，Linux 上写
    ``/a;/b``、Windows 上写 ``/a:/b`` 都会发生。规则：

    * 出现 ``;`` 就按 ``;`` 切（两种系统的路径里都不会有 ``;``）；
    * 否则按 ``:`` 切，再把"单字母 + 反斜杠开头"的两段拼回盘符。
    """
    if ";" in raw:
        return [p.strip() for p in raw.split(";")]

    merged: list[str] = []
    for part in (p.strip() for p in raw.split(":")):
        if merged and len(merged[-1]) == 1 and merged[-1].isalpha():
            merged[-1] = f"{merged[-1]}:{part}"  # 还原 C:\fonts 这类盘符路径
        else:
            merged.append(part)
    return merged


def font_dirs_from_env() -> tuple[str, ...]:
    """从 ``IMGTRANS_FONT_DIRS`` 读额外字体目录（空则返回空元组）。

    容器部署几乎一定要用它——镜像里字体常放 ``/opt/fonts``，
    而不是塞进 ``/usr/share/fonts``（那个目录可能被基础镜像重置）。
    """
    raw = os.environ.get(ENV_FONT_DIRS, "").strip()
    if not raw:
        return ()
    return tuple(p for p in _split_dir_list(raw) if p)


def _iter_font_files(root: Path) -> Iterator[Path]:
    """递归产出字体文件。

    * 不跟随符号链接：``/usr/share/fonts`` 里常有指回自身的链接，
      跟随会绕成环。
    * 目录与文件名都排序：同一字体在多个子目录出现时，命中结果必须**确定**，
      否则两台机器跑同一张图会选到不同字体。
    * 超过 :data:`_MAX_SCAN_DEPTH` 层就停止下钻，防病态字体树拖慢启动。
    """
    def _onerror(exc: OSError) -> None:  # pragma: no cover - 权限问题少见
        logger.debug("扫描字体目录失败 %s: %s", getattr(exc, "filename", "?"), exc)

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=_onerror):
        base = Path(dirpath)
        try:
            depth = len(base.relative_to(root).parts)
        except ValueError:  # pragma: no cover - 防御性
            depth = 0
        if depth >= _MAX_SCAN_DEPTH:
            dirnames[:] = []
        dirnames.sort()
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in _FONT_SUFFIXES:
                yield base / name


#: 按语言族给出的候选字体文件名（不含目录），按优先级排列。
#: 文件名而非完整路径，便于跨平台查找。
FONT_CANDIDATES: dict[str, tuple[str, ...]] = {
    # 中日韩统一表意文字：优先无衬线黑体（UI/海报最常见）
    "cjk": (
        "msyh.ttc",  # 微软雅黑（Win）
        "msyhbd.ttc",
        "simhei.ttf",  # 黑体
        "SourceHanSansSC-Regular.otf",  # 思源黑体
        "NotoSansCJK-Regular.ttc",
        "NotoSansSC-Regular.otf",
        "PingFang.ttc",  # macOS
        "Hiragino Sans GB.ttc",
        "wqy-zenhei.ttc",  # Linux 文泉驿正黑
        "wqy-microhei.ttc",  # Linux 文泉驿微米黑（更常见的轻量版）
        "simsun.ttc",  # 宋体（保底，有衬线但覆盖全）
    ),
    # 日文：字形与中文有别，能用专用字体更好
    "ja": (
        "YuGothR.ttc",
        "msgothic.ttc",
        "SourceHanSansJP-Regular.otf",
        "NotoSansJP-Regular.otf",
        "meiryo.ttc",
        "msyh.ttc",  # 回退到中文字体（日文假名覆盖 OK）
    ),
    # 韩文
    "ko": (
        "malgun.ttf",
        "malgunbd.ttf",
        "SourceHanSansKR-Regular.otf",
        "NotoSansKR-Regular.otf",
        "gulim.ttc",
        "msyh.ttc",
    ),
    # 拉丁/西里尔/希腊
    "latin": (
        "arial.ttf",
        "Arial.ttf",
        "arialbd.ttf",
        "DejaVuSans.ttf",
        "NotoSans-Regular.ttf",
        "LiberationSans-Regular.ttf",
        "segoeui.ttf",
        "msyh.ttc",  # 兜底：雅黑也含拉丁字形
    ),
    # 阿拉伯（RTL，需要整形；Pillow 需 raqm 支持）
    "ar": (
        "arial.ttf",
        "tahoma.ttf",
        "NotoSansArabic-Regular.ttf",
        "NotoNaskhArabic-Regular.ttf",
        "DejaVuSans.ttf",
    ),
    # 泰文
    "th": (
        "leelawui.ttf",
        "tahoma.ttf",
        "NotoSansThai-Regular.ttf",
        "Garuda.ttf",
    ),
    # 天城文（印地语等）
    # 注意 Nirmala 的文件名是 .ttf 而不是 .ttc——把它写成 .ttc 会让整个
    # 索引查不中，然后静默回退到中文字体，印地语全画成方块。
    "devanagari": (
        "nirmala.ttf",
        "mangal.ttf",
        "NotoSansDevanagari-Regular.ttf",
    ),
    # 孟加拉文（bn）。Nirmala 是 Windows 上覆盖南亚诸文字最全的一款，
    # Vrinda 是更老的孟加拉专用字体，两者都缺才回落到 cjk（会出方块）。
    "bengali": (
        "nirmala.ttf",
        "vrinda.ttf",
        "NotoSansBengali-Regular.ttf",
        "NotoSansBengaliUI-Regular.ttf",
    ),
    # 格鲁吉亚文（ka）。中文/拉丁字体几乎没有这套字形，
    # 不单独列出来必然画成一排豆腐块。Sylfaen 是 Windows 自带。
    "georgian": (
        "sylfaen.ttf",
        "NotoSansGeorgian-Regular.ttf",
        "DejaVuSans.ttf",
    ),
}

#: 目标语言码 -> 字体族。未覆盖的回落到 cjk（覆盖面最广）。
_LANG_TO_FAMILY: dict[str, str] = {
    "zh": "cjk",
    "zh-cn": "cjk",
    "zh-tw": "cjk",
    "zh-hk": "cjk",
    "zh-hans": "cjk",
    "zh-hant": "cjk",
    "chinese": "cjk",
    "ja": "ja",
    "jp": "ja",
    "japanese": "ja",
    "ko": "ko",
    "kr": "ko",
    "korean": "ko",
    "ar": "ar",
    "arabic": "ar",
    "fa": "ar",
    "ur": "ar",
    "th": "th",
    "thai": "th",
    "hi": "devanagari",
    "mr": "devanagari",
    "ne": "devanagari",
    "en": "latin",
    "de": "latin",
    "fr": "latin",
    "es": "latin",
    "it": "latin",
    "pt": "latin",
    "ru": "latin",
    "uk": "latin",
    "pl": "latin",
    "tr": "latin",
    "vi": "latin",
    "id": "latin",
    "nl": "latin",
    "sv": "latin",
    # 以下都是"拉丁字母表 + 少量附加符号"，Arial/DejaVu 已覆盖：
    # 希腊、西里尔（保加利亚语）、克罗地亚/捷克/斯洛伐克/斯洛文尼亚的
    # 变音符（č ć ž š đ）、匈牙利语双锐音符、波罗的海与北欧诸语。
    "el": "latin",
    "bg": "latin",
    "hr": "latin",
    "cs": "latin",
    "sk": "latin",
    "sl": "latin",
    "hu": "latin",
    "ro": "latin",
    "da": "latin",
    "no": "latin",
    "fi": "latin",
    "et": "latin",
    "ms": "latin",
    # 希伯来：Arial 自带希伯来字形，且方向由 is_rtl_text 处理
    "he": "latin",
    "bn": "bengali",
    "ka": "georgian",
}


def family_for_lang(lang: str | None) -> str:
    """把目标语言码映射到字体族名。"""
    if not lang:
        return "cjk"
    return _LANG_TO_FAMILY.get(lang.lower(), "cjk")


#: 回退到 **latin 家族**字体时，这些目标族必定缺字形（直接画成 □）。
#:
#: 判据必须落在"回退到哪个族的字体"上，而不是"目标族重不重要"：
#: 日文回退到 NotoSansCJK 是**完全正常**的（CJK 字体含假名），
#: 但中文/泰文/天城文回退到 DejaVu/Arial 就是铁定豆腐块。
#: DejaVu 恰好带格鲁吉亚与阿拉伯字形，所以 ka/ar 只在 warning 一档。
_LATIN_FALLBACK_LACKS = frozenset({"cjk", "ja", "ko", "th", "devanagari", "bengali"})

#: .ttc 里按 face 名挑字形所需的 token。
#:
#: Linux 的 ``NotoSansCJK-Regular.ttc`` 把 JP/KR/SC/TC/HK 五个字面打进同一个
#: 文件，**face 0 是日文**。中文目标直接用 face 0 会得到日式字形——「直」「骨」
#: 「关」这些字的写法与中文不同，用户看不出原因，只会觉得"字有点怪"。
#: Windows 的 msyh.ttc 只有简体字面，探测不到 token 就回落到 index 0，
#: 与旧行为完全一致，所以这个优化最坏情况等于没做，不会引入回归。
_FACE_TOKENS: dict[str, frozenset[str]] = {
    "cjk": frozenset({"sc", "gb", "hans", "simplified"}),
    "ja": frozenset({"jp", "japan"}),
    "ko": frozenset({"kr", "korea"}),
}

#: ttc 里最多探测多少个 face（Noto CJK 是 5 个，16 足够且不会在坏字体上空转）。
_MAX_FACE_PROBE = 16


class FontBook:
    """字体文件索引与 :class:`ImageFont.FreeTypeFont` 缓存。"""

    def __init__(self, font_dirs: list[str] | tuple[str, ...] | None = None) -> None:
        """
        Args:
            font_dirs: 额外的字体目录（优先于系统目录搜索）。
                生产环境建议显式传入，避免依赖系统装了哪些字体。
        """
        self._extra_dirs = [Path(d) for d in (font_dirs or [])]
        self._index: dict[str, Path] | None = None
        self._cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
        self._face_cache: dict[Path, int] = {}

    def _build_index(self) -> dict[str, Path]:
        """递归扫描字体目录，建立 ``小写文件名 -> 路径`` 的索引。

        为什么必须递归：Linux 发行版把字体放在 ``/usr/share/fonts/<格式>/<厂商>/``
        **两层子目录**里（如 ``truetype/dejavu/DejaVuSans.ttf``）。只扫顶层时
        索引会是 0 个文件（实测），回填阶段所有译文都退化成 Pillow 内置位图
        字体——字能"出来"，但全是 10px 的粗糙字形，整张图等于废掉。
        Windows 的字体目录是扁平的，所以这个坑只在 Linux/macOS 上暴露。
        """
        index: dict[str, Path] = {}
        dirs = [*self._extra_dirs, *(Path(d) for d in _SYSTEM_FONT_DIRS)]
        scanned: list[str] = []
        for directory in dirs:
            if not directory.is_dir():
                continue
            scanned.append(str(directory))
            for entry in _iter_font_files(directory):
                index.setdefault(entry.name.lower(), entry)
        logger.info(
            "字体索引建立完成，共 %d 个字体文件（扫描 %d 个目录：%s）",
            len(index),
            len(scanned),
            ", ".join(scanned) or "无可用目录",
        )
        return index

    @property
    def index(self) -> dict[str, Path]:
        if self._index is None:
            self._index = self._build_index()
        return self._index

    def find(self, lang: str | None) -> Path | None:
        """为指定语言找一个可用字体文件，找不到返回 ``None``。"""
        family = family_for_lang(lang)
        candidates = FONT_CANDIDATES.get(family, ())

        for name in candidates:
            hit = self.index.get(name.lower())
            if hit is not None:
                return hit

        # 该族没命中时，按 cjk -> latin 顺序回退（覆盖面优先）
        for fallback_family in ("cjk", "latin"):
            if fallback_family == family:
                continue
            for name in FONT_CANDIDATES.get(fallback_family, ()):
                hit = self.index.get(name.lower())
                if hit is not None:
                    self._report_fallback(lang, family, fallback_family, name)
                    return hit

        # 最后手段：索引里任意一个字体
        if self.index:
            name, path = next(iter(self.index.items()))
            logger.error("未找到匹配字体，使用任意可用字体 %s（字形很可能全部错乱）", name)
            return path
        return None

    @staticmethod
    def _report_fallback(lang: str | None, family: str, fallback: str, name: str) -> None:
        """按"会不会出豆腐块"分级告警回退事件。

        三档而不是一律 warning：
        * 日/韩回退到 CJK 字体 —— 正常（CJK 含假名/谚文），debug；
        * 中/泰/天城文/孟加拉回退到 latin —— 铁定 □，error；
        * 其余 —— 字形可能不理想，warning。
        级别混在一起，Linux 上少装字体的真问题就会被当成"字体不好看"忽略掉。
        """
        if fallback == "cjk" and family in ("ja", "ko"):
            logger.debug("语言 %s 无专用字体，回退到 %s（CJK 字体含该文字，正常）", lang, name)
        elif fallback == "latin" and family in _LATIN_FALLBACK_LACKS:
            logger.error(
                "语言 %s 缺专用字体，已回退到 %s —— 极可能出现豆腐块(□)。"
                "请安装对应字体（Linux: fonts-noto-cjk / fonts-noto-core）",
                lang,
                name,
            )
        else:
            logger.warning("语言 %s 无专用字体，回退到 %s（字形可能不理想）", lang, name)

    def _face_index(self, path: Path, family: str) -> int:
        """在 .ttc/.otc 里挑出与目标语言族匹配的 face 序号。

        非 ttc、非 CJK 族、探测失败、找不到匹配 —— 一律返回 ``0``
        （取第一个字面，即旧行为）。所以这个函数**最坏等于不生效**，
        不会把能跑的机器搞坏。
        """
        if family not in _FACE_TOKENS or path.suffix.lower() not in (".ttc", ".otc"):
            return 0
        cached = self._face_cache.get(path)
        if cached is not None:
            return cached

        wanted = _FACE_TOKENS[family]
        chosen = 0
        for idx in range(_MAX_FACE_PROBE):
            try:
                probe = ImageFont.truetype(str(path), size=12, index=idx)
                names = " ".join(probe.getname()).lower()
            except (OSError, ValueError):
                break  # index 越界即到底
            if wanted & set(re.split(r"[\s\-_]+", names)):
                chosen = idx
                break

        if chosen:
            logger.debug(".ttc 字面选择：%s -> index %d（族=%s）", path.name, chosen, family)
        self._face_cache[path] = chosen
        return chosen

    def get_font(self, lang: str | None, size: int) -> ImageFont.FreeTypeFont:
        """取字号为 ``size`` 的字体对象（带缓存）。

        极端情况下没有任何字体文件，退回 Pillow 内置位图字体——
        它无法缩放，只保证"能画出来不崩"，字形会很粗糙。
        """
        key = (family_for_lang(lang), max(1, int(size)))
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        path = self.find(lang)
        font: ImageFont.FreeTypeFont
        if path is None:
            logger.error("系统无可用 TrueType 字体，回退到 Pillow 默认位图字体")
            font = ImageFont.load_default()  # type: ignore[assignment]
        else:
            family = key[0]
            index = self._face_index(path, family)
            try:
                font = ImageFont.truetype(str(path), size=key[1], index=index)
            except OSError as exc:
                logger.warning("加载字体 %s 失败(%s)，回退默认字体", path, exc)
                font = ImageFont.load_default()  # type: ignore[assignment]

        self._cache[key] = font
        return font


def has_raqm() -> bool:
    """Pillow 是否编译了 Raqm（复杂文字整形）。

    阿拉伯/泰文/天城文**必须**有 Raqm 才能正确连字与排列，
    否则字母会各自孤立、RTL 不镜像。Pillow 官方 wheel 自带 Raqm，
    系统包管理器安装的版本常常没有。
    """
    try:
        from PIL import features

        return bool(features.check("raqm"))
    except Exception:  # noqa: BLE001
        return False
